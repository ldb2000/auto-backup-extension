"""Destination Dropbox : connexion d'un compte en OAuth2 (issue #10).

Ce module n'apporte que **l'accès au compte** : déclarer ce que Dropbox attend
pour autoriser l'application, obtenir un jeton durable, et vérifier que l'accès
fonctionne en identifiant le compte connecté. Le téléversement (#11) et le
listage/la suppression (#12) sont hors périmètre et lèvent `NotImplementedError`.

**Aucun SDK Dropbox n'est utilisé** : l'API v2 est une API HTTP JSON, appelée par
la session aiohttp partagée de Home Assistant (`async_get_clientsession`). Ajouter
une dépendance au seul profit de trois appels REST alourdirait l'installation de
l'intégration pour tous les utilisateurs, y compris ceux qui n'utilisent pas
Dropbox.

Les identifiants de l'application (clé et secret) sont ceux que l'utilisateur crée
lui-même sur <https://www.dropbox.com/developers/apps> : rien n'est livré dans le
dépôt (règle de `CLAUDE.md`). La procédure est décrite dans
`docs/destinations/dropbox.md`.

Rien de sensible n'est journalisé ici, même en niveau `debug` : ni les
identifiants d'application, ni les jetons, ni l'identifiant de compte.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from aiohttp import ClientError
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from ..destination import RemoteDestination
from ..errors import DestinationAuthError, DestinationError
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

# Portées demandées, et **uniquement** celles-ci (principe de moindre privilège).
# Chacune est nécessaire au cycle de vie complet d'une sauvegarde distante :
#
# - `account_info.read`     : identifier le compte connecté, pour proposer un nom
#                             de destination et vérifier l'accès (issue #10) ;
# - `files.content.write`   : déposer une sauvegarde et la supprimer (#11, #12) ;
# - `files.metadata.read`   : lister les sauvegardes déjà déposées, avec leur date
#                             et leur taille, ce qui conditionne la rétention (#12) ;
# - `files.content.read`    : relire une sauvegarde déposée — vérification d'un
#                             envoi et restauration (#12).
#
# Ne sont pas demandées : `sharing.*` (aucun partage n'est créé),
# `file_requests.*`, `contacts.*`, ni la moindre portée d'équipe (`team_*`) —
# Dropbox Business est explicitement hors périmètre de l'epic.
PORTEES: tuple[str, ...] = (
    "account_info.read",
    "files.metadata.read",
    "files.content.write",
    "files.content.read",
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

# Séparateur du nom proposé par défaut. Il est écrit en séquence d'échappement :
# le tiret demi-cadratin est un confusable du trait d'union, que `ruff` refuse
# de voir écrit littéralement dans le code (RUF001).
SEPARATEUR_NOM = "\N{EN DASH}"

MESSAGE_TELEVERSEMENT = (
    "le téléversement vers Dropbox n'est pas encore implémenté (issue #11)"
)
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

    async def _async_appel_rpc(self, url: str) -> Mapping[str, Any]:
        """Appelle un point RPC de l'API Dropbox et renvoie sa réponse JSON.

        `users/get_current_account` ne prend aucun argument : Dropbox demande
        alors un corps vide et **aucun** en-tête `Content-Type`, faute de quoi il
        répond `400 Bad Request`.
        """
        jeton = await self._session.async_get_access_token()
        client = async_get_clientsession(self._hass)
        try:
            async with (
                asyncio.timeout(DELAI_APPEL),
                client.post(
                    url, headers={"Authorization": f"Bearer {jeton}"}
                ) as reponse,
            ):
                statut = reponse.status
                corps = await reponse.text()
        except TimeoutError as err:
            raise DestinationError(
                f"Dropbox n'a pas répondu dans le temps imparti pour la "
                f"destination « {self.name} »"
            ) from err
        except ClientError as err:
            raise DestinationError(
                f"Dropbox est injoignable pour la destination « {self.name} » : {err}"
            ) from err

        self._verifier_le_statut(statut, corps)

        try:
            charge = json.loads(corps)
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

    ### Cycle de vie des sauvegardes : issues #11 et #12 ###

    async def async_upload(
        self,
        source: Path | str,
        *,
        name: str,
        slug: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> RemoteBackup:
        """Hors périmètre de l'issue #10 : implémenté par l'issue #11."""
        raise NotImplementedError(MESSAGE_TELEVERSEMENT)

    async def async_list_backups(self) -> list[RemoteBackup]:
        """Hors périmètre de l'issue #10 : implémenté par l'issue #12."""
        raise NotImplementedError(MESSAGE_LISTAGE)

    async def async_delete_backup(self, remote_id: str) -> None:
        """Hors périmètre de l'issue #10 : implémenté par l'issue #12."""
        raise NotImplementedError(MESSAGE_LISTAGE)


__all__ = [
    "CLE_ACCOUNT_ID",
    "LIBELLE_DROPBOX",
    "PORTEES",
    "PROVIDER_DROPBOX",
    "SPEC_OAUTH_DROPBOX",
    "URL_AUTORISATION",
    "URL_COMPTE",
    "URL_JETON",
    "CompteDropbox",
    "DropboxDestination",
]
