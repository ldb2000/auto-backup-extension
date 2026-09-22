"""Tests du cycle de vie de l'entrée de configuration et des services."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.auto_backup.const import (
    DATA_AUTO_BACKUP,
    DOMAIN,
    SERVICE_BACKUP,
    SERVICE_BACKUP_FULL,
    SERVICE_BACKUP_PARTIAL,
    SERVICE_PURGE,
)
from custom_components.auto_backup.handlers import BackupHandler
from custom_components.auto_backup.manager import AutoBackup

SERVICES_ATTENDUS = (
    SERVICE_BACKUP,
    SERVICE_BACKUP_FULL,
    SERVICE_BACKUP_PARTIAL,
    SERVICE_PURGE,
)


async def test_l_entree_est_chargee(
    hass: HomeAssistant, entree_auto_backup: MockConfigEntry
) -> None:
    """L'entrée de configuration est chargée et expose son gestionnaire."""
    assert entree_auto_backup.state is ConfigEntryState.LOADED

    gestionnaire = hass.data[DATA_AUTO_BACKUP]
    assert isinstance(gestionnaire, AutoBackup)
    # Hors Supervisor, l'intégration s'appuie sur le `BackupManager` du cœur.
    assert isinstance(gestionnaire._handler, BackupHandler)


async def test_les_services_sont_enregistres_apres_le_setup(
    hass: HomeAssistant, entree_auto_backup: MockConfigEntry
) -> None:
    """Les quatre services du domaine sont enregistrés après initialisation."""
    services = hass.services.async_services().get(DOMAIN, {})
    assert set(services) == set(SERVICES_ATTENDUS)


async def test_les_services_sont_retires_au_dechargement(
    hass: HomeAssistant, entree_auto_backup: MockConfigEntry
) -> None:
    """Les services sont retirés quand l'entrée est déchargée."""
    assert await hass.config_entries.async_unload(entree_auto_backup.entry_id)
    await hass.async_block_till_done()

    assert entree_auto_backup.state is ConfigEntryState.NOT_LOADED
    for service in SERVICES_ATTENDUS:
        assert not hass.services.has_service(DOMAIN, service), (
            f"le service {DOMAIN}.{service} aurait dû être retiré"
        )


async def test_le_service_purge_atteint_le_gestionnaire(
    hass: HomeAssistant, entree_auto_backup: MockConfigEntry
) -> None:
    """Appeler `auto_backup.purge` déclenche la purge côté gestionnaire."""
    gestionnaire = hass.data[DATA_AUTO_BACKUP]
    with patch.object(gestionnaire, "purge_backups", AsyncMock()) as purge_mock:
        await hass.services.async_call(DOMAIN, SERVICE_PURGE, blocking=True)

    purge_mock.assert_awaited_once()


async def test_le_rechargement_reenregistre_les_services(
    hass: HomeAssistant, entree_auto_backup: MockConfigEntry
) -> None:
    """Un rechargement de l'entrée remet les services en place."""
    assert await hass.config_entries.async_reload(entree_auto_backup.entry_id)
    await hass.async_block_till_done()

    assert entree_auto_backup.state is ConfigEntryState.LOADED
    for service in SERVICES_ATTENDUS:
        assert hass.services.has_service(DOMAIN, service)


async def test_le_setup_echoue_sans_supervisor_ni_backup(hass: HomeAssistant) -> None:
    """Sans Supervisor ni intégration `backup`, l'entrée n'est pas initialisée."""
    entree = MockConfigEntry(domain=DOMAIN, title="Auto Backup", data={})
    entree.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(entree.entry_id)
    await hass.async_block_till_done()

    assert entree.state is ConfigEntryState.SETUP_ERROR
    assert hass.services.async_services().get(DOMAIN, {}) == {}
