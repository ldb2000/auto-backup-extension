"""Component to create and automatically remove Home Assistant backups."""

import logging
from os import getenv

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.components.backup.const import DATA_MANAGER
try:
    from homeassistant.components.hassio.const import (  # HA 2026.5+
        ATTR_FOLDERS,
        ATTR_ADDONS,
        ATTR_PASSWORD,
    )
except ImportError:
    from homeassistant.components.hassio import (  # HA < 2026.5
        ATTR_FOLDERS,
        ATTR_ADDONS,
        ATTR_PASSWORD,
    )
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_NAME, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.hassio import is_hassio

from .const import (
    ATTR_KEEP_DAYS,
    ATTR_DOWNLOAD_PATH,
    ATTR_COMPRESSED,
    ATTR_LOCATION,
    ATTR_EXCLUDE,
    ATTR_INCLUDE,
    ATTR_INCLUDE_ADDONS,
    ATTR_INCLUDE_FOLDERS,
    ATTR_EXCLUDE_ADDONS,
    ATTR_EXCLUDE_FOLDERS,
    SERVICE_BACKUP,
    SERVICE_BACKUP_FULL,
    SERVICE_BACKUP_PARTIAL,
    SERVICE_PURGE,
    CONF_AUTO_PURGE,
    CONF_BACKUP_TIMEOUT,
    DEFAULT_BACKUP_TIMEOUT,
    DATA_AUTO_BACKUP,
    DOMAIN,
    ATTR_ENCRYPTED,
    ATTR_EXCLUDE_DATABASE,
    ATTR_UPLOAD_TO,  # téléversement distant (fork)
)
from .handlers import SupervisorHandler, BackupHandler
from .helpers import is_backup
from .manager import AutoBackup
from .destinations import async_setup_destinations
from .destinations.notifications import async_setup_notifications  # fork (#17)
from .destinations.upload import (  # téléversement distant (fork)
    async_prepare_upload,
    async_release_upload,
    async_setup_upload,
)
from .destinations.retention import (  # purge distante (fork)
    async_setup_remote_purge,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.BUTTON]
CONFIG_SCHEMA = cv.empty_config_schema(DOMAIN)

SCHEMA_BACKUP_BASE = vol.Schema(
    {
        vol.Optional(ATTR_NAME): vol.Any(None, cv.string),
        vol.Optional(ATTR_PASSWORD): vol.Any(None, cv.string),
        vol.Optional(ATTR_KEEP_DAYS): vol.Any(None, vol.Coerce(float)),
        vol.Optional(ATTR_DOWNLOAD_PATH): vol.All(cv.ensure_list, [cv.isdir]),
        vol.Optional(ATTR_ENCRYPTED, default=False): cv.boolean,
        vol.Optional(ATTR_EXCLUDE_DATABASE, default=False): cv.boolean,
        vol.Optional(ATTR_COMPRESSED, default=True): cv.boolean,
        vol.Optional(ATTR_LOCATION): vol.All(
            cv.string, lambda v: None if v == "/backup" else v
        ),
        # Destinations distantes du fork (#8) : identifiants ou noms.
        vol.Optional(ATTR_UPLOAD_TO): vol.All(cv.ensure_list, [cv.string]),
    },
)

SCHEMA_LIST_STRING = vol.All(cv.ensure_list, [cv.string])

SCHEMA_ADDONS_FOLDERS = {
    vol.Optional(ATTR_FOLDERS, default=[]): SCHEMA_LIST_STRING,
    vol.Optional(ATTR_ADDONS, default=[]): SCHEMA_LIST_STRING,
}

SCHEMA_BACKUP_FULL = SCHEMA_BACKUP_BASE.extend(
    {vol.Optional(ATTR_EXCLUDE): SCHEMA_ADDONS_FOLDERS}
)

SCHEMA_BACKUP_PARTIAL = SCHEMA_BACKUP_BASE.extend(SCHEMA_ADDONS_FOLDERS)

SCHEMA_BACKUP = vol.Any(
    SCHEMA_BACKUP_BASE.extend(
        {
            vol.Optional(ATTR_INCLUDE): SCHEMA_ADDONS_FOLDERS,
            vol.Optional(ATTR_EXCLUDE): SCHEMA_ADDONS_FOLDERS,
        }
    ),
    SCHEMA_BACKUP_BASE.extend(
        {
            vol.Optional(ATTR_INCLUDE_ADDONS): SCHEMA_LIST_STRING,
            vol.Optional(ATTR_INCLUDE_FOLDERS): SCHEMA_LIST_STRING,
            vol.Optional(ATTR_EXCLUDE_ADDONS): SCHEMA_LIST_STRING,
            vol.Optional(ATTR_EXCLUDE_FOLDERS): SCHEMA_LIST_STRING,
        }
    ),
)

MAP_SERVICES = {
    SERVICE_BACKUP: SCHEMA_BACKUP,
    SERVICE_BACKUP_FULL: SCHEMA_BACKUP_FULL,
    SERVICE_BACKUP_PARTIAL: SCHEMA_BACKUP_PARTIAL,
    SERVICE_PURGE: None,
}


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up Auto Backup from a config entry."""
    _LOGGER.info("Setting up Auto Backup config entry %s", entry.entry_id)

    # check backup integration or supervisor is available
    if not is_hassio(hass) and not is_backup(hass):
        _LOGGER.error(
            "You must be running Home Assistant Supervised or have the 'backup' integration enabled."
        )
        return False

    options = {
        CONF_AUTO_PURGE: entry.options.get(CONF_AUTO_PURGE, True),
        CONF_BACKUP_TIMEOUT: entry.options.get(
            CONF_BACKUP_TIMEOUT, DEFAULT_BACKUP_TIMEOUT
        ),
    }

    if is_hassio(hass):
        handler = SupervisorHandler(getenv("SUPERVISOR"), async_get_clientsession(hass))
    else:
        handler = BackupHandler(hass, hass.data[DATA_MANAGER])

    auto_backup = AutoBackup(hass, options, handler)
    hass.data[DATA_AUTO_BACKUP] = auto_backup
    async_setup_destinations(hass, entry)  # destinations distantes (fork)
    async_setup_upload(hass, entry)  # téléversement après création (fork)
    async_setup_notifications(hass, entry)  # notifications d'échec (fork)
    entry.async_on_unload(entry.add_update_listener(auto_backup.update_listener))

    await auto_backup.load_snapshots_expiry()

    ### REGISTER SERVICES ###
    async def async_service_handler(call: ServiceCall):
        """Handle Auto Backup service calls."""
        if call.service == SERVICE_PURGE:
            await auto_backup.purge_backups()
        else:
            data = call.data.copy()
            if call.service == SERVICE_BACKUP_PARTIAL:
                data[ATTR_INCLUDE] = {
                    ATTR_FOLDERS: data.pop(ATTR_FOLDERS, []),
                    ATTR_ADDONS: data.pop(ATTR_ADDONS, []),
                }
            elif call.service == SERVICE_BACKUP:
                if ATTR_INCLUDE_ADDONS in data or ATTR_INCLUDE_FOLDERS in data:
                    data[ATTR_INCLUDE] = {
                        ATTR_FOLDERS: data.pop(ATTR_INCLUDE_FOLDERS, []),
                        ATTR_ADDONS: data.pop(ATTR_INCLUDE_ADDONS, []),
                    }
                if ATTR_EXCLUDE_ADDONS in data or ATTR_EXCLUDE_FOLDERS in data:
                    data[ATTR_EXCLUDE] = {
                        ATTR_FOLDERS: data.pop(ATTR_EXCLUDE_FOLDERS, []),
                        ATTR_ADDONS: data.pop(ATTR_EXCLUDE_ADDONS, []),
                    }

            # Fork (#8) : `upload_to` est validé et retiré des données avant la
            # création ; la demande n'est corrélable à une sauvegarde que le
            # temps de cet appel de service. Le `finally` la rend donc même
            # quand la création lève — sans quoi elle resterait en attente et
            # une sauvegarde homonyme ultérieure la réclamerait. Seule entorse
            # du fork au code upstream : la ligne d'appel ci-dessous est
            # ré-indentée, sans autre changement (cf. docs/UPSTREAM.md).
            demande_televersement = async_prepare_upload(hass, data)
            try:
                await auto_backup.async_create_backup(data)
            finally:
                async_release_upload(hass, demande_televersement)

    for service, schema in MAP_SERVICES.items():
        hass.services.async_register(DOMAIN, service, async_service_handler, schema)

    # Fork (#9) : la purge distante s'ajoute au service `purge` sans toucher au
    # gestionnaire upstream ci-dessus. La ré-inscription, faite ici après la
    # boucle, enveloppe celui-ci : la purge locale s'exécute d'abord, à
    # l'identique, puis chaque destination distante est purgée. Le service est
    # retiré par `async_unload_entry`, upstream et inchangé.
    await async_setup_remote_purge(hass, entry, async_service_handler)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Unload a config entry."""
    for service in MAP_SERVICES.keys():
        hass.services.async_remove(DOMAIN, service)

    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
