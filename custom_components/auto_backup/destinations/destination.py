"""Contrat commun à toutes les destinations distantes."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant

from .models import DestinationConfig, RemoteBackup


class RemoteDestination(ABC):
    """Destination distante vers laquelle une sauvegarde peut être envoyée.

    Une instance correspond à une destination configurée par l'utilisateur :
    un compte, un dossier cible et sa propre rétention. Les fournisseurs
    (Dropbox, Google Drive...) en dérivent et n'implémentent que les quatre
    opérations du cycle de vie d'une sauvegarde distante.

    Toutes les méthodes asynchrones lèvent une `DestinationError` (ou l'une de
    ses sous-classes) en cas d'échec : aucune ne renvoie de code d'erreur.
    """

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
