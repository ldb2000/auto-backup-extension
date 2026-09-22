"""Destinations distantes d'Auto Backup (Dropbox, Google Drive, ...).

Ce sous-paquet est propre à ce fork : il n'existe pas dans l'upstream
(cf. `docs/UPSTREAM.md`). Il définit le contrat commun aux fournisseurs
(`RemoteDestination`), leurs types de données (`DestinationConfig`,
`RemoteBackup`), leurs erreurs typées, le registre des fournisseurs et le
gestionnaire qui charge les destinations d'une entrée de configuration.

Les fournisseurs concrets sont ajoutés par les issues suivantes ; aucun accès
réseau n'est réalisé ici.
"""

from __future__ import annotations

from .config_entry import (
    async_destination_configs,
    async_persist_destinations,
    async_setup_destinations,
    preserve_destinations,
)
from .destination import RemoteDestination
from .errors import (
    DestinationAuthError,
    DestinationConfigError,
    DestinationError,
    DestinationNotFoundError,
    DestinationQuotaError,
    DuplicateProviderError,
    UnknownProviderError,
)
from .manager import DestinationManager
from .models import DestinationConfig, RemoteBackup
from .registry import (
    DestinationFactory,
    create_destination,
    get_provider,
    list_providers,
    provider,
    register_provider,
    unregister_provider,
)
from .schema import DESTINATION_SCHEMA, DESTINATIONS_SCHEMA

__all__ = [
    "DESTINATIONS_SCHEMA",
    "DESTINATION_SCHEMA",
    "DestinationAuthError",
    "DestinationConfig",
    "DestinationConfigError",
    "DestinationError",
    "DestinationFactory",
    "DestinationManager",
    "DestinationNotFoundError",
    "DestinationQuotaError",
    "DuplicateProviderError",
    "RemoteBackup",
    "RemoteDestination",
    "UnknownProviderError",
    "async_destination_configs",
    "async_persist_destinations",
    "async_setup_destinations",
    "create_destination",
    "get_provider",
    "list_providers",
    "preserve_destinations",
    "provider",
    "register_provider",
    "unregister_provider",
]
