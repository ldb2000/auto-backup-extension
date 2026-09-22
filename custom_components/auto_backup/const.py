from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.util.hass_dict import HassKey

if TYPE_CHECKING:
    from .manager import AutoBackup
    from .destinations import DestinationManager
    from .destinations.oauth import EtatOAuth
    from .destinations.upload import CoordinateurTeleversement

DOMAIN = "auto_backup"
DATA_AUTO_BACKUP: HassKey[AutoBackup] = HassKey(DOMAIN)
UNSUB_LISTENER = "unsub_listener"

CONF_AUTO_PURGE = "auto_purge"
CONF_BACKUP_TIMEOUT = "backup_timeout"

DEFAULT_BACKUP_TIMEOUT_SECONDS = 1200
DEFAULT_BACKUP_TIMEOUT = 20

EVENT_BACKUP_SUCCESSFUL = f"{DOMAIN}.backup_successful"
EVENT_BACKUP_START = f"{DOMAIN}.backup_start"
EVENT_BACKUP_FAILED = f"{DOMAIN}.backup_failed"
EVENT_BACKUPS_PURGED = f"{DOMAIN}.purged_backups"

STORAGE_KEY = "snapshots_expiry"
STORAGE_VERSION = 1

ATTR_KEEP_DAYS = "keep_days"
ATTR_INCLUDE = "include"
ATTR_INCLUDE_ADDONS = "include_addons"
ATTR_INCLUDE_FOLDERS = "include_folders"
ATTR_EXCLUDE = "exclude"
ATTR_EXCLUDE_ADDONS = "exclude_addons"
ATTR_EXCLUDE_FOLDERS = "exclude_folders"
ATTR_EXCLUDE_DATABASE = "exclude_database"
ATTR_DOWNLOAD_PATH = "download_path"
ATTR_COMPRESSED = "compressed"
ATTR_ENCRYPTED = "encrypted"
ATTR_LOCATION = "location"

ATTR_LAST_FAILURE = "last_failure"
ATTR_PURGEABLE = "purgeable_backups"
ATTR_MONITORED = "monitored_backups"
ATTR_ERROR = "error"
ATTR_SLUG = "slug"

DEFAULT_BACKUP_FOLDERS = {
    "ssl": "ssl",
    "share": "share",
    "media": "media",
    "addons": "addons/local",
    "config": "homeassistant",
    "local add-ons": "addons/local",
    "home assistant configuration": "homeassistant",
}

SERVICE_PURGE = "purge"
SERVICE_BACKUP = "backup"
SERVICE_BACKUP_FULL = "backup_full"
SERVICE_BACKUP_PARTIAL = "backup_partial"

### DESTINATIONS DISTANTES ###
# Ajouts du fork (cf. docs/UPSTREAM.md) : constantes du sous-paquet `destinations`.
DATA_DESTINATIONS: HassKey[DestinationManager] = HassKey(f"{DOMAIN}_destinations")

CONF_DESTINATIONS = "destinations"
CONF_DESTINATION_ID = "destination_id"
CONF_PROVIDER = "provider"
CONF_FOLDER = "folder"
CONF_RETENTION_DAYS = "retention_days"
CONF_RETENTION_COUNT = "retention_count"

DEFAULT_DESTINATION_FOLDER = "Home Assistant"

# Les noms d'événements suivent la convention upstream `<domaine>.<événement>`
# (cf. EVENT_BACKUP_* ci-dessus) pour rester homogènes dans les automatisations.
EVENT_UPLOAD_START = f"{DOMAIN}.upload_start"
EVENT_UPLOAD_SUCCESSFUL = f"{DOMAIN}.upload_successful"
EVENT_UPLOAD_FAILED = f"{DOMAIN}.upload_failed"
EVENT_REMOTE_PURGE = f"{DOMAIN}.remote_purge"

### TÉLÉVERSEMENT APRÈS CRÉATION (issue #8) ###
# Coordinateur qui corrèle un appel de service avec la sauvegarde créée, puis
# téléverse celle-ci en tâche de fond (cf. `destinations/upload.py`).
DATA_UPLOADS: HassKey[CoordinateurTeleversement] = HassKey(f"{DOMAIN}_uploads")

# Option des services `backup`, `backup_full` et `backup_partial` : la ou les
# destinations distantes vers lesquelles envoyer la sauvegarde créée.
ATTR_UPLOAD_TO = "upload_to"

# Champs des événements `auto_backup.upload_*`, en complément d'ATTR_NAME,
# ATTR_SLUG et ATTR_ERROR ci-dessus.
ATTR_DESTINATION = "destination"
ATTR_DESTINATION_NAME = "destination_name"
ATTR_SIZE = "size"
ATTR_REMOTE_ID = "remote_id"

# Délai maximum d'un téléversement, en secondes. Lu dans `entry.options` ; le
# formulaire d'options ne l'expose pas encore (cf. docs/UPSTREAM.md).
CONF_UPLOAD_TIMEOUT = "upload_timeout"
DEFAULT_UPLOAD_TIMEOUT = 1800

### AUTORISATION OAUTH2 DES DESTINATIONS (issue #7) ###
# Ajouts du fork (cf. docs/UPSTREAM.md) : flux d'autorisation OAuth2 conduit depuis
# le flux d'options. Les identifiants d'application (`client_id`, `client_secret`) et
# le jeton (`token`) sont portés par chaque destination ; leurs clés viennent de
# `homeassistant.const` (CONF_CLIENT_ID, CONF_CLIENT_SECRET, CONF_TOKEN).

# Chemin de la vue qui reçoit le retour d'autorisation du fournisseur. Il est propre
# au fork : la vue standard de Home Assistant (`/auth/external/callback`) ne sait
# reprendre qu'un *config flow*, pas un flux d'options.
OAUTH_CALLBACK_PATH = f"/auth/{DOMAIN}/callback"

# États d'autorisation en attente : jeton aléatoire -> flux d'options à reprendre.
DATA_OAUTH_STATES: HassKey[dict[str, EtatOAuth]] = HassKey(f"{DOMAIN}_oauth_states")
# Vrai une fois la vue de retour enregistrée auprès du serveur HTTP.
DATA_OAUTH_VIEW: HassKey[bool] = HassKey(f"{DOMAIN}_oauth_view")

# Durée de vie d'un état d'autorisation, en secondes : au-delà, le retour du
# fournisseur est refusé et l'utilisateur doit relancer le flux.
OAUTH_STATE_TTL = 900

# Délais des deux appels sortants du flux, en secondes.
OAUTH_AUTHORIZE_URL_TIMEOUT = 30
OAUTH_TOKEN_TIMEOUT = 30

# Préfixe de l'identifiant du problème (« repair issue ») signalant qu'une
# destination doit être ré-autorisée.
ISSUE_REAUTH_PREFIX = "reauthentification_requise_"
