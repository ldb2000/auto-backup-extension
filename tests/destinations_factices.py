"""Fournisseur de destination factice, entièrement en mémoire (issue #6).

Ce module ne fait aucune entrée-sortie : rien n'est lu sur le disque, rien n'est
envoyé sur le réseau. Il sert à démontrer qu'un fournisseur s'ajoute par le seul
registre, sans modification du cœur de l'intégration, et à éprouver la remontée
des erreurs typées.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant

from custom_components.auto_backup.destinations import (
    DestinationConfig,
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
    - `connexions_verifiees` : nombre d'appels réussis à `async_check_connection` ;
    - `attente_secondes` : durée d'attente simulée pendant un téléversement,
      pour éprouver le délai maximum (issue #8) ;
    - `octets_recus` : contenu du dernier flux consommé, `None` si le
      téléversement n'a reçu qu'un chemin ;
    - `taille_recue` : nombre d'octets réellement lus dans le flux ;
    - `taille_annoncee` : valeur du paramètre `size` du dernier téléversement.
    """

    def __init__(self, hass: HomeAssistant, config: DestinationConfig) -> None:
        """Prépare une destination vide."""
        super().__init__(hass, config)
        self.sauvegardes: dict[str, RemoteBackup] = {}
        # Une erreur quelconque est acceptée, pas seulement une `DestinationError` :
        # un fournisseur réel peut aussi laisser filer une erreur inattendue,
        # que l'orchestrateur doit traiter sans compromettre les autres destinations.
        self.erreur_a_lever: Exception | None = None
        self.connexions_verifiees = 0
        self.attente_secondes = 0.0
        self.octets_recus: bytes | None = None
        self.taille_recue: int | None = None
        self.taille_annoncee: int | None = None
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
        source: Path | str | None = None,
        *,
        name: str,
        slug: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        stream: AsyncIterator[bytes] | None = None,
        size: int | None = None,
        filename: str | None = None,
    ) -> RemoteBackup:
        """Mémorise une sauvegarde distante et la renvoie.

        Quand un flux est fourni, il est consommé morceau par morceau — comme
        le ferait un vrai fournisseur — et la taille réellement reçue est
        enregistrée. Sans flux, l'ancien comportement (chemin seul) est
        conservé, ce qui garde les tests du socle (#6) valides.
        """
        self._verifier_erreur()
        if self.attente_secondes:
            await asyncio.sleep(self.attente_secondes)

        self.taille_annoncee = size
        if stream is None:
            self.octets_recus = None
            self.taille_recue = None
            taille = len(str(source))
        else:
            morceaux = [morceau async for morceau in stream]
            self.octets_recus = b"".join(morceaux)
            self.taille_recue = len(self.octets_recus)
            taille = self.taille_recue

        nom_fichier = filename or (Path(source).name if source is not None else name)
        self._compteur += 1
        sauvegarde = RemoteBackup(
            remote_id=f"{self.destination_id}-{self._compteur}",
            name=name,
            slug=slug,
            size=taille,
            created_at=DATE_FACTICE,
            path=f"{self.folder}/{nom_fichier}",
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
