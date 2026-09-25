"""Destination Google Drive, autorisée en OAuth2 (issues #13 et #14).

Ce module branche Google Drive sur le socle des destinations distantes : il
déclare son autorisation OAuth2 (`OAUTH2_SPEC`), identifie le compte autorisé,
vérifie l'accès et téléverse une sauvegarde. Le listage et la suppression (#15)
viendront le compléter sans rien changer ici d'autre que leurs deux méthodes.

Le téléversement lui-même — dossier cible et envoi resumable — vit dans
`google_drive_upload.py`, importé **dans** `async_upload()` : ce module-ci
fournit les primitives partagées (appel authentifié, traduction des erreurs,
signalement d'un accès révoqué) et ne peut donc pas l'importer au niveau du
module sans refermer un cycle.

**Aucun SDK Google n'est utilisé** : les appels passent par la session aiohttp
partagée de Home Assistant (`async_get_clientsession`), ce qui évite d'ajouter
une dépendance à l'intégration et laisse le cœur gérer le pool de connexions.

## Ce que le fournisseur demande, et pourquoi

- **Portée `drive.file` uniquement.** Elle ne donne accès **qu'aux fichiers
  créés par l'application** : Auto Backup ne voit jamais le reste du Drive de
  l'utilisateur. C'est à la fois la portée la plus sûre et celle qui évite la
  vérification de Google imposée aux portées étendues.
- **`access_type=offline`.** Sans lui, Google ne renvoie aucun
  `refresh_token` : l'accès expirerait au bout d'une heure et la destination
  serait à ré-autoriser à la main chaque jour.
- **`prompt=consent`.** Google ne renvoie le `refresh_token` qu'au **premier**
  consentement d'un couple compte/application. Un utilisateur qui reconfigure sa
  destination obtiendrait donc un jeton sans renouvellement possible ; forcer
  l'écran de consentement garantit un `refresh_token` à chaque autorisation.
- **`include_granted_scopes=false`.** L'autorisation demandée reste exactement
  celle décrite ci-dessus, sans hériter des portées accordées auparavant au même
  projet Google Cloud pour une autre application.

## Prérequis côté utilisateur

L'utilisateur crée son propre projet Google Cloud, y active l'API Drive et y
déclare l'URI de redirection du fork
(`https://<instance>/auth/auto_backup/callback`). Google n'accepte que des URI
**HTTPS sur un domaine public** : une instance joignable seulement en `.local`,
par adresse IP ou en HTTP ne peut pas connecter Google Drive. La procédure
complète est décrite dans `docs/destinations/google-drive.md`.

Aucun identifiant réel n'est livré ici : les identifiants d'application sont
saisis par l'utilisateur et vivent dans son entrée de configuration.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from aiohttp import ClientError
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from ..config_entry import async_persist_provider_data
from ..destination import RemoteDestination
from ..errors import (
    DestinationAuthError,
    DestinationError,
    DestinationNotFoundError,
    DestinationQuotaError,
)
from ..models import DestinationConfig, RemoteBackup
from ..oauth import (
    DestinationOAuth2Session,
    OAuth2ProviderSpec,
    async_session_de_la_destination,
)
from ..reauth import async_signaler_la_reauthentification

_LOGGER = logging.getLogger(__name__)

PROVIDER_GOOGLE_DRIVE = "google_drive"
LIBELLE_GOOGLE_DRIVE = "Google Drive"

# Points d'accès de Google. `v2/auth` est l'écran de consentement recommandé par
# Google pour les applications Web ; `oauth2.googleapis.com/token` est le point
# d'échange et de rafraîchissement des jetons.
URL_AUTORISATION = "https://accounts.google.com/o/oauth2/v2/auth"
URL_JETON = "https://oauth2.googleapis.com/token"
URL_ABOUT = "https://www.googleapis.com/drive/v3/about"

# Seule portée demandée : les fichiers créés par l'application, rien d'autre.
PORTEE_DRIVE_FILE = "https://www.googleapis.com/auth/drive.file"

# Projections demandées à `about` : Google exige un paramètre `fields` explicite.
CHAMPS_COMPTE = "user(displayName,emailAddress)"
CHAMPS_CONNEXION = "user"

# Clés sous lesquelles le fournisseur conserve ce qu'il a besoin de retenir dans
# les données de fournisseur de la destination (`DestinationConfig.provider_data`) :
# l'adresse du compte autorisé (issue #13) et l'identifiant du dossier cible une
# fois qu'il a été retrouvé ou créé (issue #14).
CLE_EMAIL_DU_COMPTE = "account_email"
CLE_ID_DU_DOSSIER = "folder_id"

# Tiret demi-cadratin du nom par défaut, écrit en séquence d'échappement : `ruff`
# refuse les caractères ambigus dans le code (RUF001).
SEPARATEUR_DU_NOM = "\u2013"

# Délai d'un appel à l'API Drive, en secondes.
DELAI_REQUETE = 30

# Motifs d'erreur renvoyés par Google (`error.errors[].reason`) que le
# fournisseur sait qualifier.
RAISON_API_DESACTIVEE = "accessNotConfigured"
RAISONS_DE_QUOTA = frozenset({"storageQuotaExceeded", "quotaExceeded"})

SPEC_OAUTH_GOOGLE_DRIVE = OAuth2ProviderSpec(
    authorize_url=URL_AUTORISATION,
    token_url=URL_JETON,
    scopes=(PORTEE_DRIVE_FILE,),
    extra_authorize_data={
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "false",
    },
)

# Message affiché quand l'API Drive n'est pas activée sur le projet de
# l'utilisateur : c'est de loin la cause la plus fréquente d'un 403 au premier
# essai, et elle se corrige en une minute dans la console Google Cloud.
MESSAGE_API_DESACTIVEE = (
    "l'API Google Drive n'est pas activée sur votre projet Google Cloud. "
    "Ouvrez « API et services » > « Bibliothèque », activez « Google Drive API », "
    "puis relancez l'ajout de la destination"
)


@dataclass(frozen=True, slots=True)
class CompteGoogle:
    """Compte Google ayant accordé l'autorisation.

    Les deux champs sont facultatifs : Google ne renvoie `displayName` et
    `emailAddress` que si le compte les expose. La destination reste utilisable
    sans eux, avec un nom par défaut générique.
    """

    nom: str | None = None
    email: str | None = None


def texte_optionnel(valeur: Any) -> str | None:
    """Renvoie une chaîne non vide, nettoyée, ou `None` pour tout le reste."""
    if not isinstance(valeur, str):
        return None
    return valeur.strip() or None


def raisons_de_l_erreur(charge: Any) -> set[str]:
    """Motifs d'erreur (`error.errors[].reason`) présents dans une réponse."""
    if not isinstance(charge, Mapping):
        return set()
    erreur = charge.get("error")
    if not isinstance(erreur, Mapping):
        return set()
    details = erreur.get("errors")
    if not isinstance(details, list):
        return set()
    return {
        str(detail["reason"])
        for detail in details
        if isinstance(detail, Mapping) and detail.get("reason")
    }


def erreur_de_la_reponse(statut: int, charge: Any) -> DestinationError:
    """Traduit une réponse en échec de l'API Drive en erreur typée du socle.

    Le message est en français et nomme la cause probable ; il ne reprend que le
    motif technique de Google (`reason`), jamais le corps entier de la réponse,
    qui n'apporterait rien à l'utilisateur.
    """
    raisons = raisons_de_l_erreur(charge)
    if statut == 401:
        return DestinationAuthError(
            "Google a refusé le jeton d'accès : l'autorisation a été révoquée ou "
            "le projet Google Cloud a changé. Ré-autorisez la destination"
        )
    if statut == 403 and RAISON_API_DESACTIVEE in raisons:
        return DestinationError(MESSAGE_API_DESACTIVEE)
    if statut == 403 and raisons & RAISONS_DE_QUOTA:
        return DestinationQuotaError(
            "l'espace de stockage du compte Google est épuisé : libérez de la "
            "place dans Google Drive, puis réessayez"
        )
    if statut == 403:
        return DestinationError(
            "Google Drive a refusé l'accès (403). Vérifiez que l'API Drive est "
            "activée et que le compte autorisé est bien celui attendu"
            + (f" (motif : {', '.join(sorted(raisons))})" if raisons else "")
        )
    if statut == 404:
        return DestinationNotFoundError(
            "la ressource demandée n'existe pas (ou plus) sur Google Drive"
        )
    return DestinationError(
        f"Google Drive a renvoyé une réponse inattendue (HTTP {statut})"
        + (f" : {', '.join(sorted(raisons))}" if raisons else "")
    )


@callback
def signaler_si_acces_revoque(session: DestinationOAuth2Session, statut: int) -> None:
    """Signale la destination à ré-autoriser quand Drive refuse le jeton (401).

    Le jeton porté par la requête était valide du point de vue de la session
    (non expiré, rafraîchi si besoin) : si Drive le refuse quand même, c'est que
    l'autorisation a été révoquée côté Google ou que le projet Cloud a changé.
    Seule une nouvelle autorisation en sort, comme pour Dropbox (401/403) : la
    destination est signalée à ré-autoriser, elle seule. Pendant le flux
    d'ajout, le signalement porte sur la destination provisoire et le flux
    l'efface aussitôt (`destinations/flow.py`).

    Un `403` ne le déclenche **pas** : API non activée ou quota épuisé se
    corrigent dans la console Google, ré-autoriser n'y changerait rien.
    """
    if statut == 401:
        async_signaler_la_reauthentification(session.hass, session.config)


async def async_appel_drive(
    session: DestinationOAuth2Session,
    methode: str,
    url: str,
    *,
    params: Mapping[str, str] | None = None,
) -> Any:
    """Appelle l'API Drive avec un jeton valide et renvoie la réponse décodée.

    Le jeton est obtenu — et rafraîchi si besoin — par la session du socle : un
    accès révoqué lève `DestinationAuthError` et déclenche la ré-authentification
    sans que ce module ait à s'en occuper.

    Ni le jeton ni l'en-tête `Authorization` ne sont journalisés, y compris en
    niveau `debug`.
    """
    jeton = await session.async_get_access_token()
    client = async_get_clientsession(session.hass)
    try:
        async with (
            asyncio.timeout(DELAI_REQUETE),
            client.request(
                methode,
                url,
                params=dict(params or {}),
                headers={"Authorization": f"Bearer {jeton}"},
            ) as reponse,
        ):
            statut = reponse.status
            charge: Any = None
            try:
                charge = await reponse.json()
            except (ClientError, ValueError, UnicodeDecodeError) as err:
                # Une réponse vide ou non JSON reste exploitable : seul le statut
                # décide alors du succès ou de l'échec. Les parenthèses sont
                # obligatoires : la forme sans parenthèses (PEP 758) n'existe qu'à
                # partir de Python 3.14 et lèverait une SyntaxError au chargement
                # de l'intégration chez les utilisateurs de Home Assistant 2025.1
                # (Python 3.12). Le `as err` maintient ces parenthèses en place
                # face à `ruff format`, dont la cible est py314.
                _LOGGER.debug(
                    "Réponse Drive %s %s non décodable en JSON (%s) : "
                    "seul le statut sera exploité",
                    methode.upper(),
                    url,
                    type(err).__name__,
                )
                charge = None
    except TimeoutError as err:
        raise DestinationError(
            f"Google Drive n'a pas répondu en moins de {DELAI_REQUETE} secondes"
        ) from err
    except ClientError as err:
        raise DestinationError(f"Google Drive est injoignable : {err}") from err

    if statut >= 400:
        signaler_si_acces_revoque(session, statut)
        erreur = erreur_de_la_reponse(statut, charge)
        _LOGGER.debug(
            "Appel Drive %s %s en échec : HTTP %s (%s)",
            methode.upper(),
            url,
            statut,
            ", ".join(sorted(raisons_de_l_erreur(charge))) or "sans motif",
        )
        raise erreur
    return charge


async def async_lire_le_compte(
    session: DestinationOAuth2Session, *, champs: str = CHAMPS_COMPTE
) -> CompteGoogle:
    """Interroge `drive/v3/about` et renvoie le compte qui a autorisé l'accès."""
    charge = await async_appel_drive(
        session, "GET", URL_ABOUT, params={"fields": champs}
    )
    utilisateur = charge.get("user") if isinstance(charge, Mapping) else None
    if not isinstance(utilisateur, Mapping):
        return CompteGoogle()
    return CompteGoogle(
        nom=texte_optionnel(utilisateur.get("displayName")),
        email=texte_optionnel(utilisateur.get("emailAddress")),
    )


class GoogleDriveDestination(RemoteDestination):
    """Destination « Google Drive » d'une entrée Auto Backup.

    Une instance correspond à un compte Google autorisé et à un dossier cible.
    L'autorisation elle-même est conduite par le flux d'options du fork
    (cf. `destinations/flow.py`) : ce fournisseur ne fait que déclarer ce que
    Google attend et consommer le jeton obtenu.
    """

    OAUTH2_SPEC: ClassVar[OAuth2ProviderSpec] = SPEC_OAUTH_GOOGLE_DRIVE

    # Libellé affiché dans le sélecteur de fournisseur du flux d'options et dans
    # ses formulaires, à la place de l'identifiant technique `google_drive`.
    LABEL: ClassVar[str] = LIBELLE_GOOGLE_DRIVE

    def __init__(self, hass: HomeAssistant, config: DestinationConfig) -> None:
        """Prépare la destination et la session qui porte son jeton."""
        super().__init__(hass, config)
        self._session = async_session_de_la_destination(hass, config)
        self._compte: CompteGoogle | None = None
        self._dossier_id: str | None = None

    @property
    def session(self) -> DestinationOAuth2Session:
        """Session OAuth2 de la destination, seule porte d'accès au jeton."""
        return self._session

    @property
    def account_email(self) -> str | None:
        """Adresse du compte Google autorisé, si elle a été mémorisée."""
        return texte_optionnel(
            (self._config.provider_data or {}).get(CLE_EMAIL_DU_COMPTE)
        )

    @property
    def folder_id(self) -> str | None:
        """Identifiant du dossier cible chez Google, s'il est déjà connu.

        Il vient de la mémoire de l'instance quand un téléversement l'a déjà
        résolu, sinon des données persistées avec la destination : un
        redémarrage de Home Assistant ne fait donc pas recréer le dossier.
        """
        if self._dossier_id is not None:
            return self._dossier_id
        return texte_optionnel(
            (self._config.provider_data or {}).get(CLE_ID_DU_DOSSIER)
        )

    @callback
    def _memoriser_le_dossier(self, dossier_id: str) -> None:
        """Mémorise l'identifiant du dossier cible, et le persiste s'il change.

        La mémorisation sur l'instance sert le téléversement en cours ; la
        persistance sert les suivants, y compris après un redémarrage. Une
        écriture qui ne changerait rien est ignorée par
        `async_persist_provider_data()` lui-même : rien à filtrer ici. Un échec
        d'écriture (destination supprimée entre-temps) n'interrompt pas l'envoi :
        au pire, le dossier sera cherché à nouveau au prochain téléversement.
        """
        self._dossier_id = dossier_id
        try:
            async_persist_provider_data(
                self._hass, self.destination_id, {CLE_ID_DU_DOSSIER: dossier_id}
            )
        except DestinationError as err:
            _LOGGER.warning(
                "Identifiant du dossier cible non persisté pour la destination "
                "« %s » : %s",
                self.destination_id,
                err,
            )

    ### Crochets du flux d'ajout ###

    async def async_nom_par_defaut(self) -> str | None:
        """Nom proposé à l'utilisateur juste après l'autorisation.

        Nommer la destination d'après le compte autorisé évite la confusion
        quand plusieurs comptes Google sont connectés.
        """
        compte = await self._async_compte()
        if compte.nom is None:
            return LIBELLE_GOOGLE_DRIVE
        return f"{LIBELLE_GOOGLE_DRIVE} {SEPARATEUR_DU_NOM} {compte.nom}"

    async def async_donnees_du_fournisseur(self) -> Mapping[str, Any] | None:
        """Données propres au fournisseur à persister avec la destination.

        Seule l'adresse du compte autorisé y figure : elle permet de savoir,
        plus tard, quel compte une destination utilise sans redemander Google.
        """
        compte = await self._async_compte()
        if compte.email is None:
            return None
        return {CLE_EMAIL_DU_COMPTE: compte.email}

    async def _async_compte(self, *, forcer: bool = False) -> CompteGoogle:
        """Compte autorisé, interrogé une fois puis mémorisé.

        Le flux d'ajout appelle les deux crochets sur la **même** instance : la
        mémorisation leur fait partager un unique aller-retour vers `about`, le
        nom affiché et l'adresse sortant de la même réponse.
        """
        if self._compte is not None and not forcer:
            return self._compte
        compte = await async_lire_le_compte(self._session)
        self._compte = compte
        return compte

    ### Cycle de vie d'une sauvegarde distante ###

    async def async_check_connection(self) -> None:
        """Vérifie que le jeton est valide et que l'API Drive répond."""
        await async_appel_drive(
            self._session, "GET", URL_ABOUT, params={"fields": CHAMPS_CONNEXION}
        )
        _LOGGER.debug(
            "Accès à Google Drive vérifié pour la destination « %s »",
            self.destination_id,
        )

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
        """Dépose une sauvegarde dans le dossier cible du Drive (issue #14).

        Le contenu est lu **en flux** et envoyé en mode « resumable », fragment
        par fragment : une sauvegarde de plusieurs gigaoctets ne charge jamais
        plus d'un fragment en mémoire. Le dossier cible est retrouvé ou créé au
        premier appel, et son identifiant est mémorisé pour les suivants.

        La signature suit celle du socle depuis l'issue #8 : le coordinateur
        (`destinations/upload.py`) appelle toujours avec `stream`, `size` et
        `filename`, et ne fournit `source` que sur Home Assistant Core. Seul
        `stream` est exploité : la portée `drive.file` et l'envoi resumable ne
        demandent rien d'autre, et rien n'est jamais recopié sur le disque.
        """
        # Import différé : `google_drive_upload` s'appuie sur les primitives
        # définies ici, l'importer au niveau du module refermerait un cycle.
        from .google_drive_upload import TeleversementDrive

        if stream is None:
            raise DestinationError(
                "le téléversement vers Google Drive attend le flux de la "
                "sauvegarde (« stream ») : aucun contenu n'a été fourni"
            )

        envoi = TeleversementDrive(
            session=self._session,
            dossier=self.folder,
            dossier_id=self.folder_id,
            memoriser=self._memoriser_le_dossier,
        )
        distante = await envoi.async_executer(
            nom=name, slug=slug, flux=stream, taille=size
        )
        _LOGGER.debug(
            "Sauvegarde « %s » déposée sur Google Drive dans « %s »",
            distante.name,
            self.folder,
        )
        return distante

    async def async_list_backups(self) -> list[RemoteBackup]:
        """Listage : implémenté par l'issue #15."""
        raise NotImplementedError(
            "le listage des sauvegardes Google Drive est implémenté par l'issue #15"
        )

    async def async_delete_backup(self, remote_id: str) -> None:
        """Suppression : implémentée par l'issue #15."""
        raise NotImplementedError(
            "la suppression d'une sauvegarde Google Drive est implémentée par "
            "l'issue #15"
        )


__all__ = [
    "CLE_EMAIL_DU_COMPTE",
    "CLE_ID_DU_DOSSIER",
    "LIBELLE_GOOGLE_DRIVE",
    "PORTEE_DRIVE_FILE",
    "PROVIDER_GOOGLE_DRIVE",
    "SPEC_OAUTH_GOOGLE_DRIVE",
    "URL_ABOUT",
    "URL_AUTORISATION",
    "URL_JETON",
    "CompteGoogle",
    "GoogleDriveDestination",
    "async_appel_drive",
    "async_lire_le_compte",
    "erreur_de_la_reponse",
    "raisons_de_l_erreur",
    "signaler_si_acces_revoque",
    "texte_optionnel",
]
