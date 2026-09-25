"""Téléversement d'une sauvegarde vers Google Drive (issue #14).

Ce module contient tout ce qui se passe entre « la sauvegarde est prête » et
« le fichier est dans le Drive de l'utilisateur » :

1. **le dossier cible** : la hiérarchie de `RemoteDestination.folder` est
   retrouvée ou créée segment par segment, et l'identifiant du dossier final est
   mémorisé (cf. `CLE_ID_DU_DOSSIER`) pour ne plus jamais être recherché ;
2. **l'envoi resumable** : une session d'envoi est ouverte, puis le contenu y
   est poussé par fragments de 8 Mio, l'un après l'autre.

## Pourquoi un envoi « resumable » et pas un simple POST

L'envoi simple (`uploadType=media` ou `multipart`) impose de connaître la taille
à l'avance et de tenir le transfert d'un seul trait : une sauvegarde de plusieurs
gigaoctets sur une liaison domestique n'y survit pas. Le mode resumable découpe
le transfert en fragments indépendants, chacun confirmé par Google (HTTP 308 et
en-tête `Range`), ce qui permet de **reprendre à l'octet près** après un incident
réseau ou un refus passager, et d'envoyer un contenu dont la taille n'est pas
annoncée (l'API Supervisor ne renvoie pas toujours `Content-Length`).

## Pourquoi le dossier est créé par l'intégration

La portée demandée est `drive.file` : Auto Backup n'a accès **qu'aux fichiers
qu'il a lui-même créés**. Un dossier créé à la main par l'utilisateur lui est
donc invisible — `files.list` ne le renverra jamais. Le dossier cible est par
conséquent toujours créé par l'intégration, et son identifiant conservé avec la
destination : c'est la seule façon de le retrouver au téléversement suivant.

## Mémoire

Au plus **un fragment** (8 Mio) est détenu en mémoire, plus le morceau de lecture
en cours (64 Kio côté coordinateur) : le flux est consommé au fur et à mesure et
rien n'est recopié sur le disque.

## Journaux

Ni le jeton, ni l'en-tête `Authorization`, ni l'**URL de session** ne sont
journalisés, à aucun niveau : l'URL de session porte un identifiant d'envoi qui
autorise, à lui seul, à écrire dans le fichier en cours de dépôt.
"""

from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from aiohttp import ClientError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util
from multidict import CIMultiDict

from ..errors import DestinationError, DestinationNotFoundError
from ..models import RemoteBackup
from ..oauth import DestinationOAuth2Session
from .google_drive import (
    DELAI_REQUETE,
    erreur_de_la_reponse,
    raisons_de_l_erreur,
    signaler_si_acces_revoque,
    texte_optionnel,
)

_LOGGER = logging.getLogger(__name__)

# Points d'accès de l'API Drive v3. Le second est celui du service d'envoi : il
# ne vit pas sous le même domaine de chemin que le premier.
URL_FICHIERS = "https://www.googleapis.com/drive/v3/files"
URL_ENVOI = "https://www.googleapis.com/upload/drive/v3/files"

# Type MIME des dossiers Drive, et celui annoncé pour une archive de sauvegarde.
MIME_DOSSIER = "application/vnd.google-apps.folder"
MIME_SAUVEGARDE = "application/x-tar"

# Projections demandées à Google : l'API ne renvoie que les champs cités.
CHAMPS_FICHIER = "id,name,size,createdTime,md5Checksum,appProperties"
CHAMPS_DOSSIER = "files(id,name)"

# Propriétés privées posées sur chaque fichier et chaque dossier créés. Elles
# sont invisibles pour l'utilisateur dans l'interface de Drive, mais requêtables :
# le listage et la purge distante (#15, #9) s'en servent pour ne jamais toucher
# à un fichier qu'Auto Backup n'a pas déposé.
MARQUEUR_AUTO_BACKUP = "auto_backup"
PROPRIETE_SLUG = "slug"
PROPRIETE_NOM = "name"
VRAI_DRIVE = "true"

# Taille d'un fragment d'envoi. Google impose un multiple de 256 Kio pour tous
# les fragments sauf le dernier, et recommande au moins 8 Mio : plus petit, le
# nombre d'allers-retours devient le facteur limitant ; plus gros, c'est autant
# de mémoire immobilisée et de travail perdu en cas de reprise.
UNITE_FRAGMENT = 256 * 1024
TAILLE_FRAGMENT = 32 * UNITE_FRAGMENT  # 8 Mio

# Bornes de nommage : un nom de fichier Drive peut aller jusqu'à 32 Ko, mais un
# nom pareil ne se lit plus. La valeur d'une propriété privée est bornée par
# Google (124 octets pour le couple clé + valeur).
LONGUEUR_MAX_NOM = 120
LONGUEUR_MAX_PROPRIETE = 100

# Nouvelles tentatives : trois essais au total, délai doublé à chaque reprise et
# plafonné. Un `Retry-After` envoyé par Google l'emporte sur ce calcul.
TENTATIVES_MAX = 3
DELAI_INITIAL = 1.0
DELAI_MAX = 60.0

# Délai d'un envoi de fragment. Il est bien plus long que celui d'un appel
# ordinaire : 8 Mio sur une liaison domestique lente peuvent demander plusieurs
# minutes. La durée **totale** du téléversement reste bornée par l'option
# `upload_timeout` du coordinateur (cf. `destinations/upload.py`).
DELAI_FRAGMENT = 300

# Réponses que Google renvoie quand l'incident est passager : réessayer a un sens.
STATUTS_REESSAYABLES = frozenset({408, 429, 500, 502, 503, 504})
RAISONS_REESSAYABLES = frozenset({"rateLimitExceeded", "userRateLimitExceeded"})

# « 308 Resume Incomplete » : le fragment est accepté, l'envoi continue. Ce n'est
# pas une redirection, malgré le code — d'où `allow_redirects=False`.
CODE_REPRISE = 308

# `Range: bytes=0-8388607` — Google n'annonce jamais qu'une plage, à partir de 0.
_PLAGE_RECUE = re.compile(r"bytes=(\d+)-(\d+)")

# Caractères remplacés dans un nom de fichier : séparateurs de chemin et
# caractères réservés par les systèmes de fichiers, pour que le fichier reste
# téléchargeable tel quel sous Windows comme sous Linux.
_CARACTERES_REMPLACES = '/\\:*?"<>|'


class _ErreurTransport(DestinationError):
    """Échec de transport : Google n'a pas répondu, ou la liaison a cédé.

    Distincte des réponses en erreur : elle est réessayable, et si elle finit
    par remonter, elle reste une `DestinationError` porteuse d'un message clair.
    """


@dataclass(frozen=True, slots=True)
class ReponseDrive:
    """Réponse de l'API Drive réduite à ce que l'envoi a besoin d'en savoir."""

    statut: int
    entetes: Mapping[str, str] = field(default_factory=dict)
    charge: Any = None


@dataclass(frozen=True, slots=True)
class _Etape:
    """Résultat d'une requête d'envoi : un offset atteint, ou le fichier final."""

    offset: int = 0
    fichier: Mapping[str, Any] | None = None


### Primitives HTTP ###


async def _async_requete(
    session: DestinationOAuth2Session,
    methode: str,
    url: str,
    *,
    etiquette: str,
    params: Mapping[str, str] | None = None,
    corps_json: Any = None,
    donnees: bytes | None = None,
    entetes: Mapping[str, str] | None = None,
    delai: int = DELAI_REQUETE,
) -> ReponseDrive:
    """Appelle Drive avec un jeton valide et renvoie statut, en-têtes et corps.

    `etiquette` décrit l'opération en français pour les journaux et les messages
    d'erreur : c'est elle qui est écrite, jamais l'URL, qui peut être celle d'une
    session d'envoi.
    """
    jeton = await session.async_get_access_token()
    client = async_get_clientsession(session.hass)
    entetes_complets = {"Authorization": f"Bearer {jeton}", **(entetes or {})}
    try:
        async with (
            asyncio.timeout(delai),
            client.request(
                methode,
                url,
                params=dict(params or {}),
                json=corps_json,
                data=donnees,
                headers=entetes_complets,
                # Un 308 n'est pas une redirection ici : aiohttp ne doit pas
                # tenter de la suivre.
                allow_redirects=False,
            ) as reponse,
        ):
            statut = reponse.status
            entetes_reponse = CIMultiDict(reponse.headers)
            charge: Any = None
            try:
                charge = await reponse.json()
            except (ClientError, ValueError, UnicodeDecodeError) as err:
                # Un corps vide ou non JSON est la normale pour un 308 : seuls le
                # statut et les en-têtes comptent alors. Les parenthèses de
                # l'`except` sont obligatoires (cf. `google_drive.py`).
                _LOGGER.debug(
                    "Réponse Drive sans corps JSON (%s) pour %s : "
                    "statut et en-têtes seuls exploités",
                    type(err).__name__,
                    etiquette,
                )
                charge = None
    except TimeoutError as err:
        raise _ErreurTransport(
            f"Google Drive n'a pas répondu en moins de {delai} secondes ({etiquette})"
        ) from err
    except ClientError as err:
        raise _ErreurTransport(
            f"Google Drive est injoignable ({etiquette}) : {err}"
        ) from err
    return ReponseDrive(statut=statut, entetes=entetes_reponse, charge=charge)


def _reessayable(reponse: ReponseDrive) -> bool:
    """Indique si l'échec est passager et mérite une nouvelle tentative."""
    if reponse.statut in STATUTS_REESSAYABLES:
        return True
    return reponse.statut == 403 and bool(
        raisons_de_l_erreur(reponse.charge) & RAISONS_REESSAYABLES
    )


def _retry_after(entetes: Mapping[str, str] | None) -> float | None:
    """Délai demandé par Google dans `Retry-After`, en secondes, s'il y en a un."""
    brut = (entetes or {}).get("Retry-After")
    if brut is None:
        return None
    try:
        secondes = float(brut)
    except (TypeError, ValueError) as err:
        # `Retry-After` peut aussi être une date HTTP : inexploitable telle
        # quelle, le délai calculé prend alors le relais. Les parenthèses sont
        # obligatoires pour rester chargeable en Python 3.12, et le `as err`
        # les maintient en place face à `ruff format` (cf. `docs/UPSTREAM.md`
        # et `tests/test_compatibilite_python.py`).
        _LOGGER.debug("En-tête Retry-After inexploitable (%r) : %s", brut, err)
        return None
    return secondes if secondes >= 0 else None


def delai_avant_reprise(
    tentative: int, entetes: Mapping[str, str] | None = None
) -> float:
    """Délai à respecter avant la `tentative`-ième reprise (1 pour la première).

    Exponentiel et borné : 1 s, 2 s, 4 s... sans jamais dépasser `DELAI_MAX`. Un
    `Retry-After` envoyé par Google l'emporte — c'est lui qui sait quand la
    limitation de débit sera levée —, plafonné de la même façon.
    """
    demande = _retry_after(entetes)
    if demande is not None:
        return min(demande, DELAI_MAX)
    return min(DELAI_INITIAL * 2 ** (tentative - 1), DELAI_MAX)


async def async_attendre_avant_reprise(delai: float) -> None:
    """Patiente avant une nouvelle tentative.

    Fonction dédiée plutôt qu'un `asyncio.sleep()` en ligne : les tests
    l'observent pour vérifier les délais sans attendre réellement.
    """
    await asyncio.sleep(delai)


async def async_appel_drive_json(
    session: DestinationOAuth2Session,
    methode: str,
    url: str,
    *,
    etiquette: str,
    params: Mapping[str, str] | None = None,
    corps_json: Any = None,
    entetes: Mapping[str, str] | None = None,
) -> ReponseDrive:
    """Appelle Drive, réessaie les échecs passagers, traduit les autres.

    Renvoie la réponse complète : l'ouverture d'une session d'envoi a besoin de
    l'en-tête `Location`, les autres appels du seul corps JSON.
    """
    tentative = 0
    while True:
        tentative += 1
        try:
            reponse = await _async_requete(
                session,
                methode,
                url,
                etiquette=etiquette,
                params=params,
                corps_json=corps_json,
                entetes=entetes,
            )
        except _ErreurTransport:
            if tentative >= TENTATIVES_MAX:
                raise
            await async_attendre_avant_reprise(delai_avant_reprise(tentative))
            continue

        if reponse.statut < 400:
            return reponse

        signaler_si_acces_revoque(session, reponse.statut)
        if _reessayable(reponse) and tentative < TENTATIVES_MAX:
            _LOGGER.debug(
                "Google Drive a répondu HTTP %s (%s) : nouvelle tentative",
                reponse.statut,
                etiquette,
            )
            await async_attendre_avant_reprise(
                delai_avant_reprise(tentative, reponse.entetes)
            )
            continue

        _LOGGER.debug(
            "Échec Drive pour %s : HTTP %s (%s)",
            etiquette,
            reponse.statut,
            ", ".join(sorted(raisons_de_l_erreur(reponse.charge))) or "sans motif",
        )
        raise erreur_de_la_reponse(reponse.statut, reponse.charge)


### Dossier cible ###


def _echapper(valeur: str) -> str:
    """Échappe une valeur littérale d'une requête `q` de l'API Drive."""
    return valeur.replace("\\", "\\\\").replace("'", "\\'")


def requete_de_dossier(nom: str, parent: str) -> str:
    """Requête `q` retrouvant un sous-dossier non supprimé, par son nom.

    `trashed = false` est indispensable : un dossier mis à la corbeille reste
    listé par défaut, et y déposer une sauvegarde reviendrait à la jeter.
    """
    return (
        f"name = '{_echapper(nom)}' and mimeType = '{MIME_DOSSIER}' "
        f"and '{_echapper(parent)}' in parents and trashed = false"
    )


async def _async_creer_le_dossier(
    session: DestinationOAuth2Session, nom: str, parent: str
) -> str:
    """Crée un sous-dossier marqué Auto Backup et renvoie son identifiant."""
    reponse = await async_appel_drive_json(
        session,
        "POST",
        URL_FICHIERS,
        params={"fields": "id,name"},
        corps_json={
            "name": nom,
            "mimeType": MIME_DOSSIER,
            "parents": [parent],
            "appProperties": {MARQUEUR_AUTO_BACKUP: VRAI_DRIVE},
        },
        etiquette=f"création du dossier « {nom} »",
    )
    charge = reponse.charge if isinstance(reponse.charge, Mapping) else {}
    identifiant = texte_optionnel(charge.get("id"))
    if identifiant is None:
        raise DestinationError(
            f"Google Drive n'a pas renvoyé l'identifiant du dossier « {nom} » créé"
        )
    _LOGGER.debug("Dossier « %s » créé sur Google Drive", nom)
    return identifiant


async def _async_sous_dossier(
    session: DestinationOAuth2Session, nom: str, parent: str
) -> str:
    """Identifiant du sous-dossier `nom` de `parent`, créé s'il n'existe pas.

    Seuls les dossiers **créés par l'intégration** sont visibles avec la portée
    `drive.file` : un dossier homonyme créé à la main par l'utilisateur n'est
    jamais renvoyé par la recherche, et n'est donc jamais réutilisé.
    """
    reponse = await async_appel_drive_json(
        session,
        "GET",
        URL_FICHIERS,
        params={
            "q": requete_de_dossier(nom, parent),
            "fields": CHAMPS_DOSSIER,
            "spaces": "drive",
            "pageSize": "10",
        },
        etiquette=f"recherche du dossier « {nom} »",
    )
    charge = reponse.charge if isinstance(reponse.charge, Mapping) else {}
    trouves = charge.get("files")
    if isinstance(trouves, list):
        for fichier in trouves:
            if not isinstance(fichier, Mapping):
                continue
            if (identifiant := texte_optionnel(fichier.get("id"))) is not None:
                return identifiant
    return await _async_creer_le_dossier(session, nom, parent)


async def async_resoudre_le_dossier(
    session: DestinationOAuth2Session, dossier: str
) -> str:
    """Retrouve ou crée la hiérarchie de `dossier` et renvoie le dossier final.

    `dossier` est un chemin relatif POSIX validé par le socle
    (`chemin_de_dossier()`) : chaque segment est traité l'un après l'autre, à
    partir de la racine du Drive.
    """
    parent = "root"
    for segment in dossier.split("/"):
        parent = await _async_sous_dossier(session, segment, parent)
    return parent


### Nommage ###


def _assainir(valeur: Any) -> str:
    """Réduit un texte à ce qui peut figurer dans un nom de fichier."""
    if not isinstance(valeur, str):
        return ""
    normalise = unicodedata.normalize("NFKC", valeur)
    sans_controle = "".join(
        " " if unicodedata.category(caractere).startswith("C") else caractere
        for caractere in normalise
    )
    for interdit in _CARACTERES_REMPLACES:
        sans_controle = sans_controle.replace(interdit, "-")
    return " ".join(sans_controle.split()).strip(" .")


def nom_du_fichier(nom: str, slug: str | None = None) -> str:
    """Nom du fichier déposé sur Drive : « <nom> [<slug>].tar ».

    Le nom reste **lisible** — c'est celui que l'utilisateur verra dans son
    Drive —, et le slug le rend unique : deux sauvegardes homonymes (le cas
    normal sur Home Assistant Core, où toutes s'appellent « Core <version> »)
    ne se recouvrent pas.
    """
    base = _assainir(nom)
    identifiant = _assainir(slug)
    if not base:
        base = identifiant or "sauvegarde"
    if len(base) > LONGUEUR_MAX_NOM:
        base = base[:LONGUEUR_MAX_NOM].strip()
    if identifiant:
        return f"{base} [{identifiant}].tar"
    return f"{base}.tar"


def proprietes_du_fichier(nom: str, slug: str | None) -> dict[str, str]:
    """Propriétés privées posées sur le fichier déposé.

    `auto_backup` marque l'origine du fichier — c'est lui qui autorise le
    listage et la purge distante à agir — et `slug` fait le lien avec la
    sauvegarde locale correspondante.
    """
    proprietes = {MARQUEUR_AUTO_BACKUP: VRAI_DRIVE}
    lisible = _assainir(nom)[:LONGUEUR_MAX_PROPRIETE].strip()
    if lisible:
        proprietes[PROPRIETE_NOM] = lisible
    if slug:
        proprietes[PROPRIETE_SLUG] = slug[:LONGUEUR_MAX_PROPRIETE]
    return proprietes


### Envoi resumable ###


def entete_content_range(debut: int, longueur: int, total: int | None) -> str:
    """En-tête `Content-Range` d'un fragment, ou d'une demande d'état.

    La taille totale n'est écrite que lorsqu'elle est connue : tant qu'elle ne
    l'est pas, Google accepte `*` et n'apprend la taille qu'au dernier fragment.
    Un fragment vide (demande d'état, fichier vide) s'écrit `bytes */<total>`.
    """
    taille = "*" if total is None else str(total)
    if longueur <= 0:
        return f"bytes */{taille}"
    return f"bytes {debut}-{debut + longueur - 1}/{taille}"


def offset_du_range(entetes: Mapping[str, str]) -> int:
    """Nombre d'octets que Google déclare avoir reçus, d'après `Range`.

    Absence de `Range` : Google n'a **rien** reçu (c'est sa convention). Le
    supposer plutôt que de faire confiance à ce qui a été envoyé évite d'écrire
    un fichier troué.
    """
    plage = _PLAGE_RECUE.search((entetes or {}).get("Range", ""))
    if plage is None:
        return 0
    return int(plage.group(2)) + 1


def _entier(valeur: Any) -> int | None:
    """Convertit en entier une valeur numérique de Drive, souvent du texte."""
    if isinstance(valeur, bool):
        return None
    if isinstance(valeur, int):
        return valeur
    if isinstance(valeur, str):
        try:
            return int(valeur.strip())
        except ValueError:
            return None
    return None


@dataclass(slots=True)
class TeleversementDrive:
    """Un téléversement vers Google Drive, du dossier cible au fichier déposé.

    L'objet ne sert qu'une fois : il porte l'état de l'envoi en cours (dossier
    résolu, octets acceptés) et le laisse derrière lui. `memoriser` est appelée
    dès que l'identifiant du dossier est connu, et non à la fin : un envoi qui
    échoue après la création du dossier ne doit pas en faire créer un second au
    téléversement suivant.
    """

    session: DestinationOAuth2Session
    dossier: str
    dossier_id: str | None = None
    memoriser: Callable[[str], None] | None = None

    async def async_executer(
        self,
        *,
        nom: str,
        flux: AsyncIterator[bytes],
        slug: str | None = None,
        taille: int | None = None,
    ) -> RemoteBackup:
        """Dépose le contenu de `flux` et renvoie la sauvegarde distante créée."""
        nom_fichier = nom_du_fichier(nom, slug)
        url_session = await self._async_ouvrir_la_session(
            nom_fichier, nom=nom, slug=slug, taille=taille
        )
        fichier, octets = await self._async_envoyer_le_contenu(
            url_session, flux, taille
        )
        return self._sauvegarde_distante(
            fichier, octets, slug=slug, nom_fichier=nom_fichier
        )

    ### Dossier ###

    async def _async_dossier(self, *, forcer: bool = False) -> str:
        """Identifiant du dossier cible, résolu au plus une fois par envoi."""
        if not forcer and self.dossier_id:
            return self.dossier_id
        identifiant = await async_resoudre_le_dossier(self.session, self.dossier)
        self.dossier_id = identifiant
        if self.memoriser is not None:
            self.memoriser(identifiant)
        return identifiant

    ### Session d'envoi ###

    async def _async_ouvrir_la_session(
        self, nom_fichier: str, *, nom: str, slug: str | None, taille: int | None
    ) -> str:
        """Ouvre la session d'envoi, en recréant le dossier s'il a disparu."""
        dossier_id = await self._async_dossier()
        try:
            return await self._async_demander_une_session(
                dossier_id, nom_fichier, nom=nom, slug=slug, taille=taille
            )
        except DestinationNotFoundError:
            # L'identifiant mémorisé ne désigne plus rien : le dossier a été
            # supprimé ou mis à la corbeille depuis le dernier envoi.
            _LOGGER.info(
                "Le dossier « %s » n'existe plus sur Google Drive : il est recréé",
                self.dossier,
            )
        dossier_id = await self._async_dossier(forcer=True)
        return await self._async_demander_une_session(
            dossier_id, nom_fichier, nom=nom, slug=slug, taille=taille
        )

    async def _async_demander_une_session(
        self,
        dossier_id: str,
        nom_fichier: str,
        *,
        nom: str,
        slug: str | None,
        taille: int | None,
    ) -> str:
        """Demande une session d'envoi et renvoie son URL (jamais journalisée)."""
        entetes = {"X-Upload-Content-Type": MIME_SAUVEGARDE}
        if taille is not None:
            entetes["X-Upload-Content-Length"] = str(taille)
        reponse = await async_appel_drive_json(
            self.session,
            "POST",
            URL_ENVOI,
            params={"uploadType": "resumable", "fields": CHAMPS_FICHIER},
            corps_json={
                "name": nom_fichier,
                "parents": [dossier_id],
                "appProperties": proprietes_du_fichier(nom, slug),
            },
            entetes=entetes,
            etiquette=f"ouverture de la session d'envoi de « {nom_fichier} »",
        )
        url_session = texte_optionnel(reponse.entetes.get("Location"))
        if url_session is None:
            raise DestinationError(
                "Google Drive n'a pas ouvert de session d'envoi pour "
                f"« {nom_fichier} » (en-tête « Location » absent)"
            )
        _LOGGER.debug(
            "Session d'envoi ouverte pour « %s » (son URL n'est pas journalisée)",
            nom_fichier,
        )
        return url_session

    ### Contenu ###

    async def _async_envoyer_le_contenu(
        self,
        url_session: str,
        flux: AsyncIterator[bytes],
        taille_annoncee: int | None,
    ) -> tuple[Mapping[str, Any], int]:
        """Pousse le flux fragment par fragment et renvoie (fichier, octets).

        Un fragment est gardé en attente tant que le flux n'est pas épuisé : le
        **dernier** fragment doit annoncer la taille totale, et on ne sait qu'un
        fragment est le dernier qu'en ayant lu la suite.
        """
        offset = 0
        tampon = bytearray()
        async for morceau in flux:
            tampon.extend(morceau)
            while len(tampon) > TAILLE_FRAGMENT:
                fragment = bytes(tampon[:TAILLE_FRAGMENT])
                del tampon[:TAILLE_FRAGMENT]
                etape = await self._async_pousser(
                    url_session, fragment, offset, taille_annoncee
                )
                if etape.fichier is not None:
                    # Google a clos la session plus tôt que prévu : la taille
                    # annoncée était inférieure au contenu réellement lu.
                    raise DestinationError(
                        "Google Drive a clos l'envoi avant la fin de la "
                        "sauvegarde : la taille annoncée était erronée"
                    )
                offset = etape.offset

        total = offset + len(tampon)
        if taille_annoncee is not None and taille_annoncee != total:
            _LOGGER.warning(
                "La sauvegarde lue fait %s octets alors que %s étaient annoncés : "
                "c'est la taille réellement lue qui est déclarée à Google Drive",
                total,
                taille_annoncee,
            )
        etape = await self._async_pousser(url_session, bytes(tampon), offset, total)
        if etape.fichier is None:
            raise DestinationError(
                "Google Drive n'a pas confirmé la fin de l'envoi de la sauvegarde"
            )
        return etape.fichier, total

    async def _async_pousser(
        self, url_session: str, fragment: bytes, debut: int, total: int | None
    ) -> _Etape:
        """Pousse un fragment entier, en reprenant à l'offset annoncé si besoin."""
        fin = debut + len(fragment)
        position = debut
        tentative = 0
        while True:
            reste = fragment[position - debut :]
            etiquette = f"envoi des octets {position} à {max(fin - 1, position)}"
            try:
                reponse = await _async_requete(
                    self.session,
                    "PUT",
                    url_session,
                    etiquette=etiquette,
                    donnees=reste,
                    entetes={
                        "Content-Range": entete_content_range(
                            position, len(reste), total
                        )
                    },
                    delai=DELAI_FRAGMENT,
                )
            except _ErreurTransport as err:
                tentative += 1
                if tentative >= TENTATIVES_MAX:
                    raise
                _LOGGER.debug("Reprise de l'envoi après un échec réseau : %s", err)
                await async_attendre_avant_reprise(delai_avant_reprise(tentative))
                position = await self._async_reprise(url_session, total, debut, fin)
                continue

            if reponse.statut == CODE_REPRISE:
                atteint = offset_du_range(reponse.entetes)
                self._verifier_la_position(atteint, debut)
                if atteint >= fin:
                    # L'offset retenu est celui des octets réellement poussés :
                    # la suite du flux est lue à partir de là, quoi que Google
                    # annonce au-delà.
                    return _Etape(offset=fin)
                if atteint <= position:
                    tentative += 1
                    if tentative >= TENTATIVES_MAX:
                        raise DestinationError(
                            "Google Drive n'accepte plus d'octets de la sauvegarde : "
                            f"l'envoi s'arrête à {atteint} octets"
                        )
                    await async_attendre_avant_reprise(delai_avant_reprise(tentative))
                position = atteint
                continue

            if reponse.statut < 400:
                charge = reponse.charge if isinstance(reponse.charge, Mapping) else {}
                return _Etape(offset=fin, fichier=charge)

            signaler_si_acces_revoque(self.session, reponse.statut)
            tentative += 1
            if _reessayable(reponse) and tentative < TENTATIVES_MAX:
                _LOGGER.debug(
                    "Google Drive a répondu HTTP %s pendant l'envoi : "
                    "reprise dans quelques secondes",
                    reponse.statut,
                )
                await async_attendre_avant_reprise(
                    delai_avant_reprise(tentative, reponse.entetes)
                )
                position = await self._async_reprise(url_session, total, debut, fin)
                continue
            raise erreur_de_la_reponse(reponse.statut, reponse.charge)

    async def _async_reprise(
        self, url_session: str, total: int | None, debut: int, fin: int
    ) -> int:
        """Offset auquel reprendre l'envoi du fragment courant.

        Google ne peut pas avoir reçu plus que ce qui lui a été envoyé : la
        borne haute évite qu'une réponse aberrante ne fasse sauter des octets.
        """
        return min(await self._async_position(url_session, total, debut), fin)

    async def _async_position(
        self, url_session: str, total: int | None, debut: int
    ) -> int:
        """Demande à Google combien d'octets la session a déjà reçus.

        C'est la procédure de reprise recommandée par l'API : une requête vide
        portant `Content-Range: bytes */<taille>` renvoie un 308 dont l'en-tête
        `Range` donne l'état réel de la session.
        """
        reponse = await _async_requete(
            self.session,
            "PUT",
            url_session,
            etiquette="état de la session d'envoi",
            donnees=b"",
            entetes={"Content-Range": entete_content_range(0, 0, total)},
        )
        if reponse.statut == CODE_REPRISE:
            atteint = offset_du_range(reponse.entetes)
            self._verifier_la_position(atteint, debut)
            return atteint
        if reponse.statut < 400:
            raise DestinationError(
                "Google Drive considère l'envoi terminé alors qu'il reste des "
                "octets à transmettre"
            )
        signaler_si_acces_revoque(self.session, reponse.statut)
        raise erreur_de_la_reponse(reponse.statut, reponse.charge)

    @staticmethod
    def _verifier_la_position(atteint: int, debut: int) -> None:
        """Refuse de poursuivre si Google a perdu des octets déjà transmis.

        Les fragments précédents ont été libérés : ils ne peuvent plus être
        renvoyés. Mieux vaut un échec explicite qu'une archive trouée.
        """
        if atteint < debut:
            raise DestinationError(
                "la session d'envoi de Google Drive a perdu des octets déjà "
                f"transmis ({atteint} octets reçus au lieu de {debut}) : "
                "le téléversement est abandonné"
            )

    ### Résultat ###

    def _sauvegarde_distante(
        self,
        fichier: Mapping[str, Any],
        octets: int,
        *,
        slug: str | None,
        nom_fichier: str,
    ) -> RemoteBackup:
        """Construit la `RemoteBackup` décrivant le fichier déposé.

        La taille renvoyée par Google est comparée à celle réellement envoyée :
        une archive tronquée ne doit pas être déclarée comme une sauvegarde
        valide, la rétention finirait par supprimer la copie locale.
        """
        identifiant = texte_optionnel(fichier.get("id"))
        if identifiant is None:
            raise DestinationError(
                "Google Drive n'a pas renvoyé l'identifiant du fichier téléversé"
            )
        taille = _entier(fichier.get("size"))
        if taille is not None and taille != octets:
            raise DestinationError(
                f"la sauvegarde déposée sur Google Drive fait {taille} octets au "
                f"lieu de {octets} : l'envoi est incomplet"
            )
        nom_distant = texte_optionnel(fichier.get("name")) or nom_fichier
        proprietes = fichier.get("appProperties")
        proprietes = proprietes if isinstance(proprietes, Mapping) else {}
        return RemoteBackup(
            remote_id=identifiant,
            name=nom_distant,
            slug=slug or texte_optionnel(proprietes.get(PROPRIETE_SLUG)),
            size=taille if taille is not None else octets,
            created_at=dt_util.parse_datetime(
                texte_optionnel(fichier.get("createdTime")) or ""
            ),
            path=f"{self.dossier}/{nom_distant}",
            metadata={
                PROPRIETE_SLUG: slug,
                "md5Checksum": texte_optionnel(fichier.get("md5Checksum")),
                MARQUEUR_AUTO_BACKUP: True,
            },
        )


__all__ = [
    "CHAMPS_FICHIER",
    "DELAI_FRAGMENT",
    "MARQUEUR_AUTO_BACKUP",
    "MIME_DOSSIER",
    "MIME_SAUVEGARDE",
    "PROPRIETE_SLUG",
    "TAILLE_FRAGMENT",
    "TENTATIVES_MAX",
    "UNITE_FRAGMENT",
    "URL_ENVOI",
    "URL_FICHIERS",
    "ReponseDrive",
    "TeleversementDrive",
    "async_appel_drive_json",
    "async_attendre_avant_reprise",
    "async_resoudre_le_dossier",
    "delai_avant_reprise",
    "entete_content_range",
    "nom_du_fichier",
    "offset_du_range",
    "proprietes_du_fichier",
    "requete_de_dossier",
]
