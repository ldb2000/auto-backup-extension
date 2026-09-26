"""Destination Dropbox : compte connecté en OAuth2 (#10) et dépôt (#11).

Ce module apporte **l'accès au compte** — déclarer ce que Dropbox attend pour
autoriser l'application, obtenir un jeton durable, vérifier que l'accès
fonctionne en identifiant le compte connecté — et le **dépôt d'une sauvegarde**
dans le dossier de la destination (issue #11). Le listage et la suppression
(#12) restent hors périmètre et lèvent `NotImplementedError`.

Le dépôt suit les deux modes imposés par l'API : une requête unique
(`files/upload`) en deçà de 150 Mo, une **session fragmentée**
(`files/upload_session/*`) au-delà, ou quand la taille de la sauvegarde n'est
pas annoncée. Dans les deux cas l'archive traverse le processus en flux : au
plus un fragment de 8 Mio est tenu en mémoire, jamais la sauvegarde entière.

**Aucun SDK Dropbox n'est utilisé** : l'API v2 est une API HTTP JSON, appelée par
la session aiohttp partagée de Home Assistant (`async_get_clientsession`). Ajouter
une dépendance au seul profit de quelques appels REST alourdirait l'installation
de l'intégration pour tous les utilisateurs, y compris ceux qui n'utilisent pas
Dropbox.

Les identifiants de l'application (clé et secret) sont ceux que l'utilisateur crée
lui-même sur <https://www.dropbox.com/developers/apps> : rien n'est livré dans le
dépôt (règle de `CLAUDE.md`). La procédure est décrite dans
`docs/destinations/dropbox.md`.

Rien de sensible n'est journalisé ici, même en niveau `debug` : ni les
identifiants d'application, ni les jetons, ni l'identifiant de compte, et
l'en-tête `Authorization` ne sort jamais de la fonction qui le construit.
"""

from __future__ import annotations

import asyncio
import json
import logging
import unicodedata
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

from aiohttp import ClientError, ClientTimeout
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from ...const import DEFAULT_UPLOAD_TIMEOUT
from ..config_entry import async_entree_auto_backup, delai_de_televersement
from ..destination import RemoteDestination
from ..errors import DestinationAuthError, DestinationError, DestinationQuotaError
from ..models import DestinationConfig, RemoteBackup
from ..oauth import OAuth2ProviderSpec, async_session_de_la_destination
from ..reauth import async_signaler_la_reauthentification

_LOGGER = logging.getLogger(__name__)

PROVIDER_DROPBOX = "dropbox"
LIBELLE_DROPBOX = "Dropbox"

# Points d'entrée de l'API Dropbox v2. L'autorisation vit sur le domaine public
# `www.dropbox.com` (c'est la page que voit l'utilisateur), les appels
# programmatiques sur `api.dropboxapi.com`.
URL_AUTORISATION = "https://www.dropbox.com/oauth2/authorize"
URL_JETON = "https://api.dropboxapi.com/oauth2/token"
URL_COMPTE = "https://api.dropboxapi.com/2/users/get_current_account"
URL_CREATION_DOSSIER = "https://api.dropboxapi.com/2/files/create_folder_v2"

# Les transferts vivent sur un domaine distinct (`content.dropboxapi.com`) : le
# corps de la requête y est le fichier lui-même, et les arguments voyagent dans
# l'en-tête `Dropbox-API-Arg`, sérialisés en JSON.
URL_ENVOI = "https://content.dropboxapi.com/2/files/upload"
URL_SESSION_DEBUT = "https://content.dropboxapi.com/2/files/upload_session/start"
URL_SESSION_AJOUT = "https://content.dropboxapi.com/2/files/upload_session/append_v2"
URL_SESSION_FIN = "https://content.dropboxapi.com/2/files/upload_session/finish"

# Portées demandées, et **uniquement** celles-ci (principe de moindre privilège).
# Chacune est nécessaire au cycle de vie complet d'une sauvegarde distante :
#
# - `account_info.read`     : identifier le compte connecté, pour proposer un nom
#                             de destination et vérifier l'accès (issue #10) ;
# - `files.content.write`   : déposer une sauvegarde (#11) et la supprimer
#                             (`files/delete_v2`, qui relève de cette portée, #12) ;
# - `files.metadata.read`   : lister les sauvegardes déjà déposées, avec leur date
#                             et leur taille, ce qui conditionne la rétention (#12).
#
# `files.content.read` n'est **pas** demandée : aucune opération du périmètre ne
# relit le contenu d'une sauvegarde déposée — l'envoi, le listage et la
# suppression s'en passent — et la restauration depuis le nuage est hors
# périmètre de l'epic #1. Le jour où elle deviendrait nécessaire, elle sera
# ajoutée avec la fonctionnalité qui la justifie, et l'utilisateur ré-autorisera.
#
# Ne sont pas demandées non plus : `sharing.*` (aucun partage n'est créé),
# `file_requests.*`, `contacts.*`, ni la moindre portée d'équipe (`team_*`) —
# Dropbox Business est explicitement hors périmètre de l'epic.
PORTEES: tuple[str, ...] = (
    "account_info.read",
    "files.metadata.read",
    "files.content.write",
)

# `token_access_type=offline` est **indispensable** : sans lui, Dropbox ne renvoie
# qu'un jeton d'accès de quatre heures, sans jeton de rafraîchissement, et la
# destination cesserait de fonctionner sans intervention de l'utilisateur.
DONNEES_AUTORISATION_SUPPLEMENTAIRES = MappingProxyType(
    {"token_access_type": "offline"}
)

SPEC_OAUTH_DROPBOX = OAuth2ProviderSpec(
    authorize_url=URL_AUTORISATION,
    token_url=URL_JETON,
    scopes=PORTEES,
    extra_authorize_data=DONNEES_AUTORISATION_SUPPLEMENTAIRES,
)

# Délai d'un appel à l'API, en secondes.
DELAI_APPEL = 30

# Longueur maximale du fragment de réponse repris dans un message d'erreur : de
# quoi diagnostiquer, pas de quoi recopier une réponse entière dans un journal.
LONGUEUR_MAX_RESUME = 200

# Clé sous laquelle l'identifiant du compte est persisté (`provider_data`).
CLE_ACCOUNT_ID = "account_id"

### Téléversement (issue #11) ###

# Seuil imposé par Dropbox : `files/upload` refuse au-delà de 150 Mo, il faut
# alors ouvrir une session d'envoi. La comparaison est stricte — 150 Mo pile
# relèvent déjà de la session.
SEUIL_ENVOI_SIMPLE = 150 * 1024 * 1024

# Taille d'un fragment de session. Dropbox recommande un multiple de 4 Mio ;
# 8 Mio est le compromis retenu : deux fois moins de requêtes qu'à 4 Mio pour une
# empreinte mémoire qui reste négligeable devant une sauvegarde Home Assistant,
# et très en deçà des 150 Mo qu'un fragment peut atteindre.
MULTIPLE_FRAGMENT = 4 * 1024 * 1024
TAILLE_FRAGMENT = 2 * MULTIPLE_FRAGMENT

# Nouvelles tentatives sur un échec **passager** (429, 5xx). Trois tentatives au
# total, jamais plus d'une minute d'attente : au-delà, l'échec est signalé et la
# sauvegarde locale reste intacte — la prochaine sauvegarde repartira de zéro,
# ce qui vaut mieux qu'une tâche de fond qui s'éternise.
TENTATIVES_MAX = 3
ATTENTE_INITIALE = 1.0
ATTENTE_MAX = 60.0

# Le délai maximum d'une requête de transfert n'est pas une constante : il suit
# l'option `upload_timeout` de l'entrée (cf. `_delai_de_requete`). Seule la
# valeur de repli, quand l'entrée est introuvable, vient de `const.py`.

# Délais réseau explicites : sans eux, la valeur par défaut d'aiohttp (cinq
# minutes **au total**) couperait le dépôt d'une grosse sauvegarde en plein
# transfert. Seule l'absence prolongée de données est fatale ici — un transfert
# lent mais vivant doit pouvoir aller à son terme.
DELAI_CONNEXION = 30
DELAI_SANS_DONNEES = 120
TIMEOUT_TRANSFERT = ClientTimeout(
    total=None, connect=DELAI_CONNEXION, sock_read=DELAI_SANS_DONNEES
)

TYPE_BINAIRE = "application/octet-stream"
TYPE_JSON = "application/json"

# Fragments d'`error_summary` reconnus. Dropbox les préfixe du champ fautif
# (`path/conflict/file/...`) : la comparaison se fait donc par inclusion.
MOTIF_ESPACE = "insufficient_space"
MOTIF_CONFLIT = "conflict"
MOTIF_CONFLIT_DOSSIER = "conflict/folder"

# Nommage du fichier déposé : « <nom de la sauvegarde> [<slug>].tar ».
SUFFIXE_ARCHIVE = ".tar"
NOM_DE_REPLI = "sauvegarde"
# Caractères que Dropbox refuse dans un nom de fichier.
CARACTERES_REFUSES_PAR_DROPBOX = '/\\:?*<>"|'

# Confusables du séparateur de chemin qui **survivent** à NFKC. La normalisation
# ne ramène à « / » et « \ » que les formes de compatibilité (pleine chasse
# U+FF0F et U+FF3C, petite forme U+FE68) ; ces dix caractères-ci, eux, la
# traversent intacts, tout en étant visuellement indiscernables d'une barre
# oblique dans l'explorateur Dropbox.
#
# Aucun n'est exploitable en l'état — le nom de fichier forme un segment unique,
# et le fournisseur ne le découpe pas — mais les laisser passer ferait mentir la
# garantie que ce module annonce, et un découpage introduit plus tard (listage
# #12, purge #9) hériterait d'un nom déjà trompeur. Ils sont écrits en séquences
# d'échappement : `ruff` refuse un confusable écrit littéralement (RUF001).
SOLIDUS_CONFUSABLES = (
    "\N{FRACTION SLASH}"
    "\N{DIVISION SLASH}"
    "\N{BIG SOLIDUS}"
    "\N{DOTTED SOLIDUS}"
    "\N{VERY HEAVY SOLIDUS}"
    "\N{BOX DRAWINGS LIGHT DIAGONAL UPPER RIGHT TO LOWER LEFT}"
    "\N{SET MINUS}"
    "\N{REVERSE SOLIDUS OPERATOR}"
    "\N{BIG REVERSE SOLIDUS}"
    "\N{BOX DRAWINGS LIGHT DIAGONAL UPPER LEFT TO LOWER RIGHT}"
)

# Un segment de chemin Dropbox est borné à 255 caractères. La borne retenue est
# plus basse : un nom plus long ne dit rien de plus et compliquerait l'affichage
# comme la recherche.
CARACTERES_INTERDITS = frozenset(CARACTERES_REFUSES_PAR_DROPBOX + SOLIDUS_CONFUSABLES)
LONGUEUR_MAX_NOM_FICHIER = 200
LONGUEUR_MAX_SLUG = 60

# Clés des métadonnées attachées à une sauvegarde distante. `auto_backup` est le
# marqueur qui distingue les sauvegardes déposées par l'intégration de tout autre
# fichier du dossier : la purge distante (#9) et le listage (#12) s'y fient pour
# ne jamais supprimer un fichier de l'utilisateur.
CLE_MARQUEUR = "auto_backup"
CLE_SLUG = "slug"
CLE_EMPREINTE = "content_hash"

# Séparateur du nom proposé par défaut. Il est écrit en séquence d'échappement :
# le tiret demi-cadratin est un confusable du trait d'union, que `ruff` refuse
# de voir écrit littéralement dans le code (RUF001).
SEPARATEUR_NOM = "\N{EN DASH}"

MESSAGE_LISTAGE = (
    "le listage et la suppression des sauvegardes Dropbox ne sont pas encore "
    "implémentés (issue #12)"
)


@dataclass(frozen=True, slots=True)
class CompteDropbox:
    """Compte Dropbox connecté, tel que `users/get_current_account` le décrit."""

    account_id: str
    display_name: str
    email: str | None = None

    def __repr__(self) -> str:
        """Représentation journalisable : ni nom, ni adresse, ni identifiant."""
        return f"<{type(self).__name__} compte connecté>"


def _texte(valeur: Any) -> str:
    """Renvoie une chaîne nettoyée, ou une chaîne vide si la valeur n'en est pas."""
    return valeur.strip() if isinstance(valeur, str) else ""


def _compte_depuis(charge: Mapping[str, Any]) -> CompteDropbox:
    """Construit un `CompteDropbox` à partir de la réponse de l'API.

    Dropbox renvoie le nom dans `name.display_name`. La réponse est traitée comme
    une donnée externe : l'identifiant de compte est exigé, le reste est toléré
    absent — une destination ne doit pas devenir inutilisable parce que le compte
    n'a pas de nom affiché.
    """
    identifiant = _texte(charge.get(CLE_ACCOUNT_ID))
    if not identifiant:
        raise DestinationError(
            "réponse Dropbox inexploitable : le compte n'a pas d'identifiant"
        )
    nom = charge.get("name")
    return CompteDropbox(
        account_id=identifiant,
        display_name=_texte(nom.get("display_name"))
        if isinstance(nom, Mapping)
        else "",
        email=_texte(charge.get("email")) or None,
    )


def _resume_d_erreur(corps: str) -> str:
    """Résume le corps d'une réponse d'erreur, borné en longueur.

    Dropbox renvoie un objet portant `error_summary` (par exemple
    `expired_access_token/...`) ; à défaut, le texte brut est repris tronqué.
    Aucun jeton ne transite dans un corps de réponse : seule la requête en porte
    un, dans son en-tête.
    """
    texte = corps.strip()
    try:
        charge = json.loads(texte)
    except ValueError:
        charge = None
    if isinstance(charge, Mapping):
        texte = _texte(charge.get("error_summary")) or texte
    texte = " ".join(texte.split())
    if len(texte) > LONGUEUR_MAX_RESUME:
        return f"{texte[:LONGUEUR_MAX_RESUME]}..."
    return texte or "réponse vide"


def _motif_d_erreur(corps: str) -> str:
    """Renvoie l'`error_summary` en minuscules, pour reconnaître un motif.

    Distinct de `_resume_d_erreur()`, qui borne le texte pour l'afficher : la
    reconnaissance travaille sur la chaîne entière, sans quoi un motif situé
    au-delà de la troncature passerait inaperçu.
    """
    try:
        charge = json.loads(corps.strip())
    except ValueError:
        return ""
    if not isinstance(charge, Mapping):
        return ""
    return _texte(charge.get("error_summary")).lower()


def _est_transitoire(statut: int) -> bool:
    """Indique si un statut HTTP relève d'un incident passager.

    Seuls la limitation de débit et les pannes serveur en relèvent : rejouer un
    `409` (conflit de nom) ou un `401` (accès refusé) ne ferait que répéter le
    même refus.
    """
    return statut == 429 or 500 <= statut < 600


def _merite_une_nouvelle_tentative(reponse: _ReponseDropbox) -> bool:
    """Indique si une requête refusée vaut la peine d'être rejouée.

    Le statut ne suffit pas : Dropbox signale un espace saturé par un `507`,
    qui est bien un `5xx` sans avoir la moindre chance de s'arranger en une
    minute. Le motif du corps a donc le dernier mot.
    """
    if not _est_transitoire(reponse.statut):
        return False
    motif = _motif_d_erreur(reponse.corps)
    return MOTIF_ESPACE not in motif and MOTIF_CONFLIT not in motif


def _attente_demandee(entetes: Mapping[str, str]) -> float | None:
    """Durée demandée par l'en-tête `Retry-After`, en secondes, ou `None`.

    Dropbox l'exprime en secondes. Une valeur illisible ou négative est ignorée
    plutôt que devinée : le délai de repli (exponentiel) prend alors le relais.
    """
    brut = entetes.get("Retry-After")
    if brut is None:
        return None
    try:
        attente = float(brut)
    except (TypeError, ValueError) as err:
        # Les parenthèses sont obligatoires : sans elles, `ruff format` écrirait
        # la forme PEP 758, indisponible avant Python 3.14 (cf. `docs/tests.md`).
        _LOGGER.debug("En-tête Retry-After inexploitable (%r) : %s", brut, err)
        return None
    return attente if attente >= 0 else None


def _delai_avant_nouvelle_tentative(
    attente_demandee: float | None, tentative: int
) -> float:
    """Délai à observer avant de rejouer une requête, borné à `ATTENTE_MAX`.

    Le délai demandé par Dropbox prime — c'est lui qui évite d'aggraver une
    limitation de débit. À défaut, l'attente double à chaque tentative.
    """
    if attente_demandee is not None:
        return min(attente_demandee, ATTENTE_MAX)
    return min(ATTENTE_INITIALE * 2 ** (tentative - 1), ATTENTE_MAX)


def _horodatage(valeur: Any) -> datetime | None:
    """Convertit une date Dropbox (`server_modified`) en `datetime`, ou `None`."""
    texte = _texte(valeur)
    return dt_util.parse_datetime(texte) if texte else None


def _entier(valeur: Any) -> int | None:
    """Renvoie un entier si la valeur en est un, `None` sinon.

    Les booléens sont écartés bien qu'ils soient des entiers en Python : une
    taille `True` trahirait une réponse inattendue, pas une taille de 1 octet.
    """
    if isinstance(valeur, bool) or not isinstance(valeur, int):
        return None
    return valeur


def _assaini(valeur: str) -> str:
    """Réduit un texte à ce que Dropbox accepte dans un nom de fichier.

    La valeur est d'abord normalisée en **NFKC**, comme le dossier distant
    (cf. `destinations/schema.py`) : sans cela une barre oblique pleine chasse
    (U+FF0F) survivrait au filtrage pour redevenir un séparateur de chemin chez
    le fournisseur. La normalisation ne suffit pourtant pas : elle ne ramène à
    « / » que les formes de compatibilité, et laisse passer intacts les autres
    confusables de la barre oblique (U+2215, U+2044, U+29F8...), d'où la liste
    `SOLIDUS_CONFUSABLES`, filtrée comme les caractères que Dropbox refuse.

    Ces caractères et les caractères non imprimables deviennent `_`, les espaces
    sont ramenées à une seule, et les points comme les espaces de bordure sont
    retirés — Dropbox refuse un nom qui s'y termine.
    """
    normalise = unicodedata.normalize("NFKC", valeur)
    filtre = "".join(
        "_"
        if caractere in CARACTERES_INTERDITS or not caractere.isprintable()
        else caractere
        for caractere in normalise
    )
    return " ".join(filtre.split()).strip(" .")


def nom_de_fichier_dropbox(
    nom: str, slug: str | None = None, filename: str | None = None
) -> str:
    """Nom du fichier déposé chez Dropbox : « <nom> [<slug>].tar ».

    Le nom de la sauvegarde seul ne l'identifie pas : sur une installation Core,
    l'upstream nomme toutes les sauvegardes automatiques `Core <version>`. Le
    slug, qui est unique, est donc accolé entre crochets — le fichier reste
    lisible dans l'explorateur Dropbox tout en désignant une sauvegarde précise.

    Le nom est assaini, puis **tronqué** pour tenir dans
    `LONGUEUR_MAX_NOM_FICHIER` caractères : c'est la partie libre qui cède, le
    slug et le suffixe `.tar` étant ce qui permet de s'y retrouver.
    """
    identifiant = _assaini(slug or "")[:LONGUEUR_MAX_SLUG].strip(" .")
    suffixe = f" [{identifiant}]{SUFFIXE_ARCHIVE}" if identifiant else SUFFIXE_ARCHIVE

    base = _assaini(nom or "")
    if not base and filename:
        base = _assaini(Path(filename).stem)
    if not base:
        base = identifiant or NOM_DE_REPLI

    disponible = LONGUEUR_MAX_NOM_FICHIER - len(suffixe)
    base = base[:disponible].strip(" .") or NOM_DE_REPLI[:disponible]
    return f"{base}{suffixe}"


def _chemin_du_dossier(dossier: str) -> str:
    """Chemin Dropbox du dossier de la destination, toujours absolu.

    Avec une application de type « App folder », Dropbox traduit lui-même ce
    chemin en un chemin relatif au dossier de l'application : il n'y a rien à
    faire de particulier ici (cf. `docs/destinations/dropbox.md`).
    """
    return f"/{dossier}"


def _chemin_distant(dossier: str, nom_fichier: str) -> str:
    """Chemin Dropbox complet du fichier déposé."""
    return f"{_chemin_du_dossier(dossier)}/{nom_fichier}"


def _argument_de_depot(chemin: str) -> dict[str, Any]:
    """Arguments de dépôt d'un fichier, identiques à l'envoi et à la session.

    `mode: add` et `autorename: false` sont le cœur de la garantie « aucun
    écrasement silencieux » : Dropbox refuse le dépôt si le chemin est déjà pris,
    au lieu de remplacer le fichier (`overwrite`) ou d'en créer un second sous un
    nom voisin (`autorename`). `mute: true` évite de notifier l'utilisateur sur
    tous ses appareils à chaque sauvegarde.
    """
    return {"path": chemin, "mode": "add", "autorename": False, "mute": True}


async def _flux(stream: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    """Réémet un flux morceau par morceau, en ignorant les morceaux vides.

    Le passage par ce générateur garantit à `aiohttp` un itérable asynchrone —
    un flux peut n'être qu'un itérateur — et met le fournisseur à l'abri d'une
    source qui rendrait des morceaux vides.
    """
    async for morceau in stream:
        if morceau:
            yield morceau


async def _fragments(
    stream: AsyncIterator[bytes], taille: int = TAILLE_FRAGMENT
) -> AsyncIterator[bytes]:
    """Regroupe les morceaux d'un flux en fragments de `taille` octets.

    Le flux arrive par tranches de 64 Kio (cf. `destinations/upload.py`), bien
    trop petites pour une requête Dropbox. Les morceaux sont donc accumulés dans
    un tampon unique, vidé dès qu'il atteint la taille d'un fragment : **un seul
    fragment** est tenu en mémoire à la fois, quelle que soit la taille de la
    sauvegarde. Le dernier fragment est plus court, et un flux vide ne produit
    aucun fragment.
    """
    tampon = bytearray()
    async for morceau in stream:
        tampon += morceau
        while len(tampon) >= taille:
            fragment = bytes(tampon[:taille])
            del tampon[:taille]
            yield fragment
    if tampon:
        yield bytes(tampon)


@dataclass(frozen=True, slots=True)
class _ReponseDropbox:
    """Réponse d'un appel Dropbox, réduite à ce qui sert à décider de la suite."""

    statut: int
    corps: str
    attente_demandee: float | None = None


class DropboxDestination(RemoteDestination):
    """Destination Dropbox d'un utilisateur, rattachée à un compte autorisé.

    L'instance ne conserve aucun jeton : elle le demande à la session OAuth2
    générique avant chaque appel, qui le rafraîchit si besoin et signale une
    ré-autorisation nécessaire quand Dropbox refuse de le renouveler (issue #7).
    """

    LABEL = LIBELLE_DROPBOX
    OAUTH2_SPEC = SPEC_OAUTH_DROPBOX

    def __init__(self, hass: HomeAssistant, config: DestinationConfig) -> None:
        """Prépare la destination et sa session d'autorisation."""
        super().__init__(hass, config)
        self._session = async_session_de_la_destination(hass, config)
        self._compte: CompteDropbox | None = None

    ### Accès au compte ###

    async def async_check_connection(self) -> None:
        """Vérifie l'accès en interrogeant `users/get_current_account`.

        L'appel est refait à chaque vérification : c'est tout son intérêt, un
        résultat mémorisé ne dirait rien de l'état courant de l'autorisation.
        """
        await self._async_compte(forcer=True)
        # Ni le nom du compte ni son identifiant ne sont journalisés : seul le
        # fait que la vérification a abouti l'est.
        _LOGGER.debug(
            "Accès Dropbox vérifié pour la destination « %s »", self.destination_id
        )

    async def async_nom_par_defaut(self) -> str | None:
        """Nom proposé au formulaire de nommage : « Dropbox - <nom affiché> »."""
        compte = await self._async_compte()
        if not compte.display_name:
            return LIBELLE_DROPBOX
        return f"{LIBELLE_DROPBOX} {SEPARATEUR_NOM} {compte.display_name}"

    async def async_donnees_du_fournisseur(self) -> Mapping[str, Any] | None:
        """Identifiant du compte autorisé, à persister avec la destination."""
        compte = await self._async_compte()
        return {CLE_ACCOUNT_ID: compte.account_id}

    async def _async_compte(self, *, forcer: bool = False) -> CompteDropbox:
        """Compte connecté, interrogé une fois puis mémorisé.

        La mémorisation évite trois appels réseau là où le flux d'ajout en
        demande un seul : le nom proposé et l'identifiant de compte sortent de la
        même réponse.
        """
        if self._compte is not None and not forcer:
            return self._compte
        compte = _compte_depuis(await self._async_appel_rpc(URL_COMPTE))
        self._compte = compte
        return compte

    ### Appels HTTP ###

    @property
    def _delai_de_requete(self) -> float:
        """Garde-fou d'une seule requête de transfert, en secondes.

        Le téléversement complet est déjà borné par le coordinateur, qui applique
        l'option `upload_timeout` ; cette borne-ci n'est qu'un filet pour une
        requête isolée qui ne rendrait jamais la main. Elle **suit ce même
        réglage** au lieu de rester figée sur la valeur livrée par défaut : sur
        une connexion lente, un budget global relevé à plusieurs heures serait
        sinon coupé au bout de trente minutes par le garde-fou d'une requête, et
        l'utilisateur n'aurait aucun moyen de s'en sortir.

        Elle vaut exactement le budget global, donc lui reste par construction
        **inférieure ou égale** : c'est toujours le coordinateur qui tranche le
        premier, jamais ce filet. Le poser plus haut le rendrait inopérant ; le
        poser plus bas interdirait à une requête unique d'utiliser le budget que
        l'utilisateur lui a accordé — un envoi simple, qui part en **une** seule
        requête, a précisément besoin de tout ce budget.

        Sans entrée de configuration — la destination n'est alors plus reliée à
        rien — le repli est la valeur par défaut de `const.py`.
        """
        entry = async_entree_auto_backup(self._hass)
        if entry is None:
            return float(DEFAULT_UPLOAD_TIMEOUT)
        return delai_de_televersement(entry)

    async def _async_appel_rpc(self, url: str) -> Mapping[str, Any]:
        """Appelle un point RPC de l'API Dropbox et renvoie sa réponse JSON.

        `users/get_current_account` ne prend aucun argument : Dropbox demande
        alors un corps vide et **aucun** en-tête `Content-Type`, faute de quoi il
        répond `400 Bad Request`.
        """
        jeton = await self._session.async_get_access_token()
        reponse = await self._async_requete(
            url, entetes={"Authorization": f"Bearer {jeton}"}, delai=DELAI_APPEL
        )
        self._verifier_le_statut(reponse.statut, reponse.corps)
        return self._charge_de_la_reponse(reponse.corps)

    async def _async_requete(
        self,
        url: str,
        *,
        entetes: Mapping[str, str],
        corps: Any = None,
        delai: float,
        timeout_client: ClientTimeout | None = None,
    ) -> _ReponseDropbox:
        """Envoie une requête à Dropbox et renvoie sa réponse brute.

        Les seules erreurs traduites ici sont celles du **transport** : une
        panne réseau et une absence de réponse ne disent rien de l'autorisation
        et ne doivent donc jamais la remettre en cause. Le statut HTTP, lui, est
        interprété par l'appelant, qui seul sait ce qu'il tentait de faire.

        `corps` n'est transmis que s'il existe : un appel RPC sans argument
        exige un corps vide **et** aucun `Content-Type`, faute de quoi Dropbox
        répond `400 Bad Request`.
        """
        options: dict[str, Any] = {"headers": dict(entetes)}
        if corps is not None:
            options["data"] = corps
        if timeout_client is not None:
            options["timeout"] = timeout_client
        client = async_get_clientsession(self._hass)
        try:
            async with (
                asyncio.timeout(delai),
                client.post(url, **options) as reponse,
            ):
                return _ReponseDropbox(
                    statut=reponse.status,
                    corps=await reponse.text(),
                    attente_demandee=_attente_demandee(reponse.headers),
                )
        except TimeoutError as err:
            raise DestinationError(
                f"Dropbox n'a pas répondu dans le temps imparti pour la "
                f"destination « {self.name} »"
            ) from err
        except ClientError as err:
            raise DestinationError(
                f"Dropbox est injoignable pour la destination « {self.name} » : {err}"
            ) from err

    def _charge_de_la_reponse(
        self, corps: str, *, permettre_vide: bool = False
    ) -> Mapping[str, Any]:
        """Décode la réponse JSON de Dropbox, traitée comme donnée externe.

        `permettre_vide` sert aux points d'entrée qui répondent par un corps
        vide en cas de succès, comme `upload_session/append_v2`.
        """
        texte = corps.strip()
        if not texte and permettre_vide:
            return {}
        try:
            charge = json.loads(texte)
        except ValueError as err:
            raise DestinationError(
                f"réponse Dropbox illisible pour la destination « {self.name} »"
            ) from err
        if not isinstance(charge, Mapping):
            raise DestinationError(
                f"réponse Dropbox inattendue pour la destination « {self.name} »"
            )
        return charge

    def _verifier_le_statut(self, statut: int, corps: str) -> None:
        """Traduit un statut HTTP Dropbox en erreur typée du socle.

        - `401` : jeton refusé (expiré côté Dropbox, ou accès révoqué) ;
        - `403` : portée manquante ou compte désactivé.

        Dans les deux cas une nouvelle autorisation est la seule issue : la
        destination est donc signalée à ré-autoriser, ce qui crée le problème
        Home Assistant correspondant — pour elle seule.
        """
        if statut < 400:
            return

        resume = _resume_d_erreur(corps)
        if statut in (401, 403):
            async_signaler_la_reauthentification(self._hass, self._config)
            raise DestinationAuthError(
                f"Dropbox refuse l'accès de la destination « {self.name} » "
                f"(HTTP {statut}) : {resume}"
            )
        if statut == 429:
            raise DestinationError(
                f"Dropbox limite temporairement les appels de la destination "
                f"« {self.name} » (HTTP 429) : {resume}"
            )
        raise DestinationError(
            f"Dropbox a refusé la requête de la destination « {self.name} » "
            f"(HTTP {statut}) : {resume}"
        )

    ### Dépôt d'une sauvegarde : issue #11 ###

    async def async_upload(
        self,
        source: Path | str | None = None,
        *,
        name: str,
        slug: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        stream: AsyncIterator[bytes] | None = None,
        size: int | None = None,
        filename: str | None = None,
    ) -> RemoteBackup:
        """Dépose la sauvegarde dans le dossier Dropbox de la destination.

        Le mode d'envoi dépend de la taille **annoncée** :

        - en deçà de `SEUIL_ENVOI_SIMPLE`, une seule requête `files/upload` ;
        - au-delà, ou quand la taille est inconnue, une session fragmentée.

        `source` est ignoré : la sauvegarde est toujours lue en flux, ce qui
        fait fonctionner la destination aussi bien sous Supervisor, où le
        fichier n'existe que derrière l'API, que sur une installation Core.
        """
        if stream is None:
            raise DestinationError(
                f"téléversement Dropbox impossible pour la destination "
                f"« {self.name} » : aucun flux de lecture n'a été fourni"
            )

        nom_fichier = nom_de_fichier_dropbox(name, slug, filename)
        chemin = _chemin_distant(self.folder, nom_fichier)
        await self._async_preparer_le_dossier()

        if size is not None and 0 <= size < SEUIL_ENVOI_SIMPLE:
            charge = await self._async_envoi_simple(chemin, stream, size)
            envoyes = size
        else:
            charge, envoyes = await self._async_envoi_fragmente(chemin, stream)

        self._verifier_la_taille(charge, envoyes, chemin)
        distante = self._sauvegarde_depuis(
            charge,
            slug=slug,
            chemin=chemin,
            nom_fichier=nom_fichier,
            metadata=metadata,
        )
        _LOGGER.debug(
            "Sauvegarde déposée chez Dropbox pour « %s » : %s (%s octets)",
            self.name,
            distante.path,
            distante.size,
        )
        return distante

    async def _async_preparer_le_dossier(self) -> None:
        """Crée le dossier cible s'il n'existe pas encore.

        Dropbox crée bien les dossiers manquants au moment du dépôt, mais pas
        avant : le créer explicitement le fait apparaître dès la première
        sauvegarde, et signale l'échec éventuel (chemin déjà occupé par un
        fichier, espace saturé) **avant** d'avoir transféré le moindre octet.
        Un dossier déjà présent n'est pas une erreur : c'est même le cas normal
        à partir de la deuxième sauvegarde.
        """
        chemin = _chemin_du_dossier(self.folder)
        reponse = await self._async_poster(
            URL_CREATION_DOSSIER,
            charge_json={"path": chemin, "autorename": False},
            rejouable=True,
        )
        if reponse.statut < 400:
            _LOGGER.debug(
                "Dossier Dropbox « %s » créé pour la destination « %s »",
                chemin,
                self.name,
            )
            return
        if MOTIF_CONFLIT_DOSSIER in _motif_d_erreur(reponse.corps):
            _LOGGER.debug("Dossier Dropbox « %s » déjà présent", chemin)
            return
        raise self._erreur_de_televersement(reponse, chemin)

    async def _async_envoi_simple(
        self, chemin: str, stream: AsyncIterator[bytes], taille: int
    ) -> Mapping[str, Any]:
        """Dépose la sauvegarde en une seule requête, corps en flux.

        La taille annoncée est reprise dans `Content-Length` : sans elle
        `aiohttp` basculerait en `Transfer-Encoding: chunked`, que les points
        d'entrée de contenu de Dropbox ne garantissent pas.

        Cette requête n'est **pas rejouable** : le flux d'une sauvegarde ne se
        lit qu'une fois (cf. `destinations/upload.py`), il n'y a donc rien à
        renvoyer après un échec passager. C'est précisément ce que la session
        fragmentée corrige pour les grosses sauvegardes, dont l'envoi coûte
        trop cher pour être abandonné.
        """
        _LOGGER.debug(
            "Envoi simple vers Dropbox : %s (%d octets)",
            chemin,
            taille,
        )
        return await self._async_envoyer(
            URL_ENVOI,
            _argument_de_depot(chemin),
            _flux(stream),
            chemin=chemin,
            taille=taille,
            rejouable=False,
        )

    async def _async_envoi_fragmente(
        self, chemin: str, stream: AsyncIterator[bytes]
    ) -> tuple[Mapping[str, Any], int]:
        """Dépose la sauvegarde par une session d'envoi et renvoie sa réponse.

        La session tient en trois temps : `start` ouvre la session avec le
        premier fragment, `append_v2` envoie les suivants en indiquant à chaque
        fois l'**offset** déjà reçu, `finish` valide le dépôt à l'emplacement
        voulu. L'offset explicite est ce qui rend un fragment rejouable : une
        tentative refusée par une limitation de débit peut être renvoyée telle
        quelle, Dropbox sachant exactement où elle s'insère.

        Renvoie la réponse de `finish` et le nombre d'octets réellement envoyés.
        """
        session_id: str | None = None
        envoyes = 0
        fragments = 0

        # La taille de fragment est relue ici, et non figée en valeur par défaut
        # de `_fragments()` : les tests la réduisent pour éprouver la session
        # sans fabriquer des centaines de mégaoctets.
        async for fragment in _fragments(stream, TAILLE_FRAGMENT):
            fragments += 1
            if session_id is None:
                charge = await self._async_envoyer(
                    URL_SESSION_DEBUT,
                    {"close": False},
                    fragment,
                    chemin=chemin,
                    rejouable=True,
                )
                session_id = self._identifiant_de_session(charge)
            else:
                await self._async_envoyer(
                    URL_SESSION_AJOUT,
                    {
                        "cursor": {"session_id": session_id, "offset": envoyes},
                        "close": False,
                    },
                    fragment,
                    chemin=chemin,
                    rejouable=True,
                )
            envoyes += len(fragment)

        if session_id is None:
            # Sauvegarde vide : la session doit tout de même être ouverte pour
            # que `finish` ait un curseur à valider.
            charge = await self._async_envoyer(
                URL_SESSION_DEBUT, {"close": False}, b"", chemin=chemin, rejouable=True
            )
            session_id = self._identifiant_de_session(charge)

        charge = await self._async_envoyer(
            URL_SESSION_FIN,
            {
                "cursor": {"session_id": session_id, "offset": envoyes},
                "commit": _argument_de_depot(chemin),
            },
            b"",
            chemin=chemin,
            rejouable=True,
        )
        _LOGGER.debug(
            "Session d'envoi Dropbox terminée : %s (%d fragments, %d octets)",
            chemin,
            fragments,
            envoyes,
        )
        return charge, envoyes

    def _sauvegarde_depuis(
        self,
        charge: Mapping[str, Any],
        *,
        slug: str | None,
        chemin: str,
        nom_fichier: str,
        metadata: Mapping[str, Any] | None,
    ) -> RemoteBackup:
        """Construit la sauvegarde distante à partir de la réponse de Dropbox.

        Tout ce dont la rétention distante (#9) et le listage (#12) ont besoin
        vient de là : l'identifiant opaque (`id:...`), seule clé de suppression,
        le chemin affichable, la taille et la date enregistrées **par Dropbox**,
        et des métadonnées portant le slug de la sauvegarde, l'empreinte du
        contenu et le marqueur `auto_backup`. Ce marqueur est ce qui autorisera
        la purge à ne supprimer que les fichiers déposés par l'intégration.

        La réponse est traitée comme une donnée externe : seul l'identifiant est
        exigé, le reste retombe sur ce que le dépôt connaît déjà.
        """
        identifiant = _texte(charge.get("id"))
        if not identifiant:
            raise DestinationError(
                f"réponse Dropbox inexploitable pour la destination "
                f"« {self.name} » : le fichier déposé n'a pas d'identifiant"
            )
        metadonnees: dict[str, Any] = dict(metadata or {})
        metadonnees[CLE_SLUG] = slug
        metadonnees[CLE_MARQUEUR] = True
        empreinte = _texte(charge.get(CLE_EMPREINTE))
        if empreinte:
            metadonnees[CLE_EMPREINTE] = empreinte
        return RemoteBackup(
            remote_id=identifiant,
            name=_texte(charge.get("name")) or nom_fichier,
            slug=slug,
            size=_entier(charge.get("size")),
            created_at=_horodatage(charge.get("server_modified")),
            path=_texte(charge.get("path_display")) or chemin,
            metadata=metadonnees,
        )

    def _identifiant_de_session(self, charge: Mapping[str, Any]) -> str:
        """Identifiant renvoyé par `upload_session/start`, exigé non vide."""
        identifiant = _texte(charge.get("session_id"))
        if not identifiant:
            raise DestinationError(
                f"réponse Dropbox inexploitable pour la destination "
                f"« {self.name} » : la session d'envoi n'a pas d'identifiant"
            )
        return identifiant

    def _verifier_la_taille(
        self, charge: Mapping[str, Any], envoyes: int, chemin: str
    ) -> None:
        """Compare la taille annoncée par Dropbox aux octets envoyés.

        Un écart signifie que le fichier déposé n'est pas la sauvegarde : mieux
        vaut un échec bruyant qu'une archive tronquée que la rétention distante
        compterait comme une sauvegarde valide.
        """
        taille = _entier(charge.get("size"))
        if taille is None or taille == envoyes:
            return
        raise DestinationError(
            f"le dépôt de « {chemin} » vers la destination « {self.name} » est "
            f"incomplet : Dropbox a enregistré {taille} octets pour {envoyes} "
            f"octets envoyés"
        )

    ### Requêtes de transfert ###

    async def _async_envoyer(
        self,
        url: str,
        argument: Mapping[str, Any],
        corps: Any,
        *,
        chemin: str,
        taille: int | None = None,
        rejouable: bool,
    ) -> Mapping[str, Any]:
        """Appelle un point d'entrée de contenu et renvoie sa réponse JSON."""
        reponse = await self._async_poster(
            url, argument=argument, corps=corps, taille=taille, rejouable=rejouable
        )
        if reponse.statut >= 400:
            raise self._erreur_de_televersement(reponse, chemin)
        return self._charge_de_la_reponse(reponse.corps, permettre_vide=True)

    async def _async_poster(
        self,
        url: str,
        *,
        argument: Mapping[str, Any] | None = None,
        charge_json: Mapping[str, Any] | None = None,
        corps: Any = None,
        taille: int | None = None,
        rejouable: bool,
    ) -> _ReponseDropbox:
        """Poste une requête Dropbox, en la rejouant si l'échec est passager.

        Le jeton est redemandé à **chaque** tentative : une attente d'une minute
        peut suffire à le périmer, et la session OAuth2 le rafraîchit alors
        d'elle-même.

        `rejouable` dit si le corps de la requête peut être renvoyé : c'est vrai
        d'un fragment, qui est encore en mémoire, et faux d'un flux, qui ne se
        lit qu'une fois.
        """
        argument_json = (
            json.dumps(argument, ensure_ascii=True) if argument is not None else None
        )
        corps_json = (
            json.dumps(charge_json, ensure_ascii=True)
            if charge_json is not None
            else None
        )
        # Le garde-fou est lu une fois pour toutes les tentatives de cette
        # requête : le relire entre deux tentatives ferait cohabiter deux bornes
        # différentes dans un même dépôt si l'utilisateur change le réglage.
        delai = self._delai_de_requete
        tentative = 1
        while True:
            jeton = await self._session.async_get_access_token()
            # L'en-tête `Authorization` est construit ici et nulle part ailleurs :
            # il ne doit apparaître dans aucun journal, à aucun niveau.
            entetes = {"Authorization": f"Bearer {jeton}"}
            if argument_json is not None:
                entetes["Dropbox-API-Arg"] = argument_json
                entetes["Content-Type"] = TYPE_BINAIRE
            if corps_json is not None:
                entetes["Content-Type"] = TYPE_JSON
            if taille is not None:
                entetes["Content-Length"] = str(taille)

            # Le chemin distant porte le nom du dossier et celui de la
            # sauvegarde : il n'est journalisé qu'en `debug`.
            _LOGGER.debug(
                "Appel Dropbox %s pour la destination « %s » : %s",
                url,
                self.name,
                argument_json or corps_json or "sans argument",
            )
            reponse = await self._async_requete(
                url,
                entetes=entetes,
                corps=corps_json if corps_json is not None else corps,
                delai=delai,
                timeout_client=TIMEOUT_TRANSFERT,
            )
            if (
                reponse.statut < 400
                or not rejouable
                or tentative >= TENTATIVES_MAX
                or not _merite_une_nouvelle_tentative(reponse)
            ):
                return reponse
            await self._async_patienter(reponse, tentative)
            tentative += 1

    async def _async_patienter(self, reponse: _ReponseDropbox, tentative: int) -> None:
        """Observe le délai demandé par Dropbox avant une nouvelle tentative."""
        attente = _delai_avant_nouvelle_tentative(reponse.attente_demandee, tentative)
        _LOGGER.warning(
            "Dropbox a répondu HTTP %s à la destination « %s » : nouvelle "
            "tentative dans %.1f s (%d sur %d)",
            reponse.statut,
            self.name,
            attente,
            tentative,
            TENTATIVES_MAX,
        )
        await asyncio.sleep(attente)

    def _erreur_de_televersement(
        self, reponse: _ReponseDropbox, chemin: str
    ) -> DestinationError:
        """Traduit un refus de Dropbox en erreur typée, en français.

        Les motifs métier sont reconnus **avant** le statut : Dropbox répond
        `409 Conflict` aussi bien pour un espace saturé que pour un nom déjà
        pris, deux situations que l'utilisateur ne corrige pas de la même façon.
        """
        resume = _resume_d_erreur(reponse.corps)
        motif = _motif_d_erreur(reponse.corps)

        if MOTIF_ESPACE in motif:
            return DestinationQuotaError(
                f"l'espace de stockage Dropbox de la destination « {self.name} » "
                f"est saturé : libérez de la place ou réduisez la rétention "
                f"({resume})"
            )
        if MOTIF_CONFLIT in motif:
            return DestinationError(
                f"un fichier nommé « {chemin} » existe déjà chez Dropbox pour la "
                f"destination « {self.name} » : Auto Backup n'écrase jamais un "
                f"fichier existant ({resume})"
            )
        if reponse.statut in (401, 403):
            async_signaler_la_reauthentification(self._hass, self._config)
            return DestinationAuthError(
                f"Dropbox refuse l'accès de la destination « {self.name} » "
                f"(HTTP {reponse.statut}) : {resume}"
            )
        if reponse.statut == 429:
            return DestinationError(
                f"Dropbox limite les appels de la destination « {self.name} » et "
                f"les a refusés après {TENTATIVES_MAX} tentatives : le dépôt de "
                f"« {chemin} » repartira à la prochaine sauvegarde ({resume})"
            )
        if 500 <= reponse.statut < 600:
            return DestinationError(
                f"Dropbox est en panne passagère (HTTP {reponse.statut}) et n'a "
                f"pas accepté « {chemin} » pour la destination « {self.name} » "
                f"après {TENTATIVES_MAX} tentatives : {resume}"
            )
        return DestinationError(
            f"Dropbox a refusé le dépôt de « {chemin} » pour la destination "
            f"« {self.name} » (HTTP {reponse.statut}) : {resume}"
        )

    ### Cycle de vie des sauvegardes : issue #12 ###

    async def async_list_backups(self) -> list[RemoteBackup]:
        """Hors périmètre de l'issue #10 : implémenté par l'issue #12."""
        raise NotImplementedError(MESSAGE_LISTAGE)

    async def async_delete_backup(self, remote_id: str) -> None:
        """Hors périmètre de l'issue #10 : implémenté par l'issue #12."""
        raise NotImplementedError(MESSAGE_LISTAGE)


__all__ = [
    "CLE_ACCOUNT_ID",
    "CLE_MARQUEUR",
    "LIBELLE_DROPBOX",
    "PORTEES",
    "PROVIDER_DROPBOX",
    "SEUIL_ENVOI_SIMPLE",
    "SPEC_OAUTH_DROPBOX",
    "TAILLE_FRAGMENT",
    "URL_AUTORISATION",
    "URL_COMPTE",
    "URL_CREATION_DOSSIER",
    "URL_ENVOI",
    "URL_JETON",
    "URL_SESSION_AJOUT",
    "URL_SESSION_DEBUT",
    "URL_SESSION_FIN",
    "CompteDropbox",
    "DropboxDestination",
    "nom_de_fichier_dropbox",
]
