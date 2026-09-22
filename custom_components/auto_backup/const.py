from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.util.hass_dict import HassKey

if TYPE_CHECKING:
    from .manager import AutoBackup
    from .destinations import DestinationManager

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
