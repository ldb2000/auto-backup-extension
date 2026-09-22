"""Contrat commun à toutes les destinations distantes."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from homeassistant.core import HomeAssistant

from .models import DestinationConfig, RemoteBackup

if TYPE_CHECKING:
    # Import différé : `oauth` dépend de `config_entry`, qui dépend du
    # gestionnaire, qui dépend de ce module. L'annotation suffit au typage.
    from .oauth import OAuth2ProviderSpec


class RemoteDestination(ABC):
    """Destination distante vers laquelle une sauvegarde peut être envoyée.

    Une instance correspond à une destination configurée par l'utilisateur :
    un compte, un dossier cible et sa propre rétention. Les fournisseurs
    (Dropbox, Google Drive...) en dérivent et n'implémentent que les quatre
    opérations du cycle de vie d'une sauvegarde distante.

    Toutes les méthodes asynchrones lèvent une `DestinationError` (ou l'une de
    ses sous-classes) en cas d'échec : aucune ne renvoie de code d'erreur.

    Un fournisseur qui s'authentifie en OAuth2 le déclare en surchargeant
    `OAUTH2_SPEC` (issue #7) : le flux d'options y lit les URL d'autorisation et
    de jeton, et conduit alors l'utilisateur dans son navigateur avant de créer
    la destination. Laissé à `None`, le fournisseur est réputé ne demander aucune
    autorisation externe.

    `LABEL` (issue #10) est le nom du service tel qu'il est montré à
    l'utilisateur (« Dropbox »). Laissé à `None`, le sélecteur du flux d'options
    retombe sur l'identifiant technique du fournisseur.
    """

    OAUTH2_SPEC: ClassVar[OAuth2ProviderSpec | None] = None
    LABEL: ClassVar[str | None] = None

    def __init__(self, hass: HomeAssistant, config: DestinationConfig) -> None:
        """Mémorise l'instance Home Assistant et la configuration figée."""
        self._hass = hass
        self._config = config

    @property
    def config(self) -> DestinationConfig:
        """Configuration persistée de la destination."""
        return self._config

    @property
    def destination_id(self) -> str:
        """Identifiant stable de la destination, unique dans l'entrée."""
        return self._config.destination_id

    @property
    def provider(self) -> str:
        """Identifiant du fournisseur, tel qu'enregistré dans le registre."""
        return self._config.provider

    @property
    def name(self) -> str:
        """Nom lisible choisi par l'utilisateur."""
        return self._config.name

    @property
    def folder(self) -> str:
        """Dossier cible chez le fournisseur."""
        return self._config.folder

    @property
    def retention_days(self) -> int | None:
        """Durée de conservation distante en jours, `None` si illimitée."""
        return self._config.retention_days

    @property
    def retention_count(self) -> int | None:
        """Nombre maximum de sauvegardes distantes, `None` si illimité."""
        return self._config.retention_count

    @property
    def provider_data(self) -> Mapping[str, Any]:
        """Description du compte autorisé, telle qu'elle a été persistée."""
        return self._config.provider_data or {}

    ### Crochets facultatifs du flux d'ajout (issue #10) ###
    #
    # Ils sont appelés une fois le jeton obtenu, avant le formulaire de nommage,
    # sur une **même** destination provisoire : un fournisseur qui mémorise la
    # réponse du service n'a donc qu'un appel réseau à faire pour les deux. Un
    # fournisseur qui ne les surcharge pas n'en fait aucun — le comportement
    # d'avant l'issue #10 est conservé. Un échec n'interrompt pas l'ajout : le
    # flux se contente de ne rien proposer (cf. `destinations/flow.py`).

    async def async_nom_par_defaut(self) -> str | None:
        """Nom proposé par défaut pour cette destination, `None` si aucun.

        Un fournisseur qui sait nommer le compte autorisé (« Dropbox - Jean
        Dupont ») le renvoie ici : le formulaire de nommage le pré-remplit.
        """
        return None

    async def async_donnees_du_fournisseur(self) -> Mapping[str, Any] | None:
        """Données **non secrètes** du compte à persister, `None` si aucune.

        Elles sont validées par `PROVIDER_DATA_SCHEMA` (scalaires JSON) puis
        écrites dans `DestinationConfig.provider_data`.
        """
        return None

    @abstractmethod
    async def async_check_connection(self) -> None:
        """Vérifie que la destination est joignable et l'accès valide.

        Ne renvoie rien en cas de succès ; lève `DestinationAuthError` si
        l'accès est refusé ou révoqué, `DestinationError` sinon.
        """

    @abstractmethod
    async def async_upload(
        self,
        source: Path | str,
        *,
        name: str,
        slug: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> RemoteBackup:
        """Téléverse le fichier `source` dans le dossier cible.

        Renvoie la sauvegarde distante créée. Lève `DestinationQuotaError` si
        l'espace de stockage est épuisé, `DestinationAuthError` si l'accès est
        refusé, `DestinationError` pour tout autre échec.
        """

    @abstractmethod
    async def async_list_backups(self) -> list[RemoteBackup]:
        """Liste les sauvegardes déposées par l'intégration.

        Seules les sauvegardes créées par Auto Backup sont renvoyées : les
        fichiers déposés par l'utilisateur ne doivent jamais être purgés.
        """

    @abstractmethod
    async def async_delete_backup(self, remote_id: str) -> None:
        """Supprime la sauvegarde distante `remote_id`.

        Lève `DestinationNotFoundError` si elle n'existe pas (ou plus).
        """

    def __repr__(self) -> str:
        """Représentation lisible dans les journaux, sans donnée sensible."""
        return (
            f"<{type(self).__name__} {self.destination_id} "
            f"provider={self.provider} name={self.name!r}>"
        )
