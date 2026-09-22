"""Fournisseur de destination factice, entièrement en mémoire (issue #6).

Ce module ne fait aucune entrée-sortie : rien n'est lu sur le disque, rien n'est
envoyé sur le réseau. Il sert à démontrer qu'un fournisseur s'ajoute par le seul
registre, sans modification du cœur de l'intégration, et à éprouver la remontée
des erreurs typées.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant

from custom_components.auto_backup.destinations import (
    DestinationConfig,
    DestinationError,
    DestinationNotFoundError,
    RemoteBackup,
    RemoteDestination,
)

PROVIDER_FACTICE = "factice"

DATE_FACTICE = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)


def config_factice(**surcharges: Any) -> dict[str, Any]:
    """Configuration brute d'une destination factice, telle que persistée."""
    return {
        "destination_id": "destination_test",
        "provider": PROVIDER_FACTICE,
        "name": "Destination de test",
        "folder": "Sauvegardes",
        "retention_days": 7,
        "retention_count": 3,
    } | surcharges


class DestinationEnMemoire(RemoteDestination):
    """Destination factice qui conserve les sauvegardes dans un dictionnaire.

    Attributs utiles aux tests :

    - `sauvegardes` : les sauvegardes distantes, par identifiant ;
    - `erreur_a_lever` : erreur levée par toutes les opérations quand elle est
      définie, pour simuler un échec du fournisseur ;
    - `connexions_verifiees` : nombre d'appels réussis à `async_check_connection`.
    """

    def __init__(self, hass: HomeAssistant, config: DestinationConfig) -> None:
        """Prépare une destination vide."""
        super().__init__(hass, config)
        self.sauvegardes: dict[str, RemoteBackup] = {}
        self.erreur_a_lever: DestinationError | None = None
        self.connexions_verifiees = 0
        self._compteur = 0

    def _verifier_erreur(self) -> None:
        """Lève l'erreur simulée si le test en a programmé une."""
        if self.erreur_a_lever is not None:
            raise self.erreur_a_lever

    async def async_check_connection(self) -> None:
        """Simule une vérification d'accès au fournisseur."""
        self._verifier_erreur()
        self.connexions_verifiees += 1

    async def async_upload(
        self,
        source: Path | str,
        *,
        name: str,
        slug: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> RemoteBackup:
        """Mémorise une sauvegarde distante et la renvoie."""
        self._verifier_erreur()
        self._compteur += 1
        sauvegarde = RemoteBackup(
            remote_id=f"{self.destination_id}-{self._compteur}",
            name=name,
            slug=slug,
            size=len(str(source)),
            created_at=DATE_FACTICE,
            path=f"{self.folder}/{Path(source).name}",
            metadata=dict(metadata or {}),
        )
        self.sauvegardes[sauvegarde.remote_id] = sauvegarde
        return sauvegarde

    async def async_list_backups(self) -> list[RemoteBackup]:
        """Renvoie les sauvegardes mémorisées."""
        self._verifier_erreur()
        return list(self.sauvegardes.values())

    async def async_delete_backup(self, remote_id: str) -> None:
        """Supprime une sauvegarde mémorisée."""
        self._verifier_erreur()
        if remote_id not in self.sauvegardes:
            raise DestinationNotFoundError(
                f"sauvegarde distante inconnue : « {remote_id} »"
            )
        del self.sauvegardes[remote_id]
