"""Destinations distantes d'Auto Backup (Dropbox, Google Drive, ...).

Ce sous-paquet est propre à ce fork : il n'existe pas dans l'upstream
(cf. `docs/UPSTREAM.md`). Il définit le contrat commun aux fournisseurs
(`RemoteDestination`), leurs types de données (`DestinationConfig`,
`RemoteBackup`), leurs erreurs typées, le registre des fournisseurs, le
gestionnaire qui charge les destinations d'une entrée de configuration, et —
depuis l'issue #7 — l'autorisation OAuth2 (`oauth`), le signalement des
destinations à ré-autoriser (`reauth`) et les étapes d'interface qui étendent
le flux d'options upstream (`flow`).

Les fournisseurs concrets sont ajoutés par les issues suivantes ; aucun accès
réseau n'est réalisé ici.
"""

from __future__ import annotations

from .config_entry import (
    async_destination_configs,
    async_entree_auto_backup,
    async_persist_destinations,
    async_persist_token,
    async_setup_destinations,
    jeton_persiste,
    options_avec_destinations,
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
from .models import VALEUR_MASQUEE, DestinationConfig, RemoteBackup
from .oauth import (
    DestinationOAuth2Implementation,
    DestinationOAuth2Session,
    OAuth2ProviderSpec,
    async_enregistrer_la_vue_de_retour,
    async_session_de_la_destination,
    jeton_valide,
    normaliser_le_jeton,
    spec_oauth_du_fournisseur,
    url_de_retour,
)
from .reauth import (
    async_effacer_la_reauthentification,
    async_signaler_la_reauthentification,
    identifiant_du_probleme,
    reauthentification_requise,
)
from .registry import (
    DestinationFactory,
    create_destination,
    get_provider,
    list_providers,
    provider,
    register_provider,
    unregister_provider,
)
from .schema import DESTINATION_SCHEMA, DESTINATIONS_SCHEMA, TOKEN_SCHEMA

__all__ = [
    "DESTINATIONS_SCHEMA",
    "DESTINATION_SCHEMA",
    "TOKEN_SCHEMA",
    "VALEUR_MASQUEE",
    "DestinationAuthError",
    "DestinationConfig",
    "DestinationConfigError",
    "DestinationError",
    "DestinationFactory",
    "DestinationManager",
    "DestinationNotFoundError",
    "DestinationOAuth2Implementation",
    "DestinationOAuth2Session",
    "DestinationQuotaError",
    "DuplicateProviderError",
    "OAuth2ProviderSpec",
    "RemoteBackup",
    "RemoteDestination",
    "UnknownProviderError",
    "async_destination_configs",
    "async_effacer_la_reauthentification",
    "async_enregistrer_la_vue_de_retour",
    "async_entree_auto_backup",
    "async_persist_destinations",
    "async_persist_token",
    "async_session_de_la_destination",
    "async_setup_destinations",
    "async_signaler_la_reauthentification",
    "create_destination",
    "get_provider",
    "identifiant_du_probleme",
    "jeton_persiste",
    "jeton_valide",
    "list_providers",
    "normaliser_le_jeton",
    "options_avec_destinations",
    "preserve_destinations",
    "provider",
    "reauthentification_requise",
    "register_provider",
    "spec_oauth_du_fournisseur",
    "unregister_provider",
    "url_de_retour",
]
