"""Tests des entités créées par `auto_backup` et de leur rattachement au device."""

from __future__ import annotations

from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.auto_backup.const import DOMAIN

# Identifiants uniques des entités upstream, par domaine de plateforme.
ENTITES_ATTENDUES = {
    "sensor": {
        "sensor-auto-backup",
        "monitored",
        "purgeable",
        "last-failure",
        "last-success",
        "next-expiration",
    },
    "binary_sensor": {"status", "problem"},
    "button": {"purge"},
}


def _entites(hass: HomeAssistant, entree: MockConfigEntry) -> list[er.RegistryEntry]:
    registre = er.async_get(hass)
    return er.async_entries_for_config_entry(registre, entree.entry_id)


async def test_les_entites_upstream_sont_creees(
    hass: HomeAssistant, entree_auto_backup: MockConfigEntry
) -> None:
    """Capteurs, capteurs binaires et bouton upstream sont dans le registre."""
    entites = _entites(hass, entree_auto_backup)

    par_domaine: dict[str, set[str]] = {}
    for entite in entites:
        par_domaine.setdefault(entite.domain, set()).add(entite.unique_id)

    assert par_domaine == ENTITES_ATTENDUES


async def test_les_entites_sont_rattachees_au_device_de_service(
    hass: HomeAssistant, entree_auto_backup: MockConfigEntry
) -> None:
    """Toutes les entités pointent vers le device de service « Auto Backup »."""
    registre_devices = dr.async_get(hass)
    device = registre_devices.async_get_device_by_identifier(
        (DOMAIN, entree_auto_backup.entry_id), entree_auto_backup.entry_id
    )

    assert device is not None
    assert device.entry_type is DeviceEntryType.SERVICE
    assert device.name == "Auto Backup"
    assert device.manufacturer == "Auto Backup"
    assert entree_auto_backup.entry_id in device.config_entries

    entites = _entites(hass, entree_auto_backup)
    assert entites, "aucune entité enregistrée pour l'entrée de configuration"
    for entite in entites:
        assert entite.device_id == device.id, (
            f"{entite.entity_id} n'est pas rattachée au device de service"
        )


async def test_les_entites_activees_par_defaut_ont_un_etat(
    hass: HomeAssistant, entree_auto_backup: MockConfigEntry
) -> None:
    """Les entités activées par défaut exposent un état après initialisation."""
    entites = _entites(hass, entree_auto_backup)
    activees = [entite for entite in entites if not entite.disabled]

    assert activees
    for entite in activees:
        assert hass.states.get(entite.entity_id) is not None, (
            f"aucun état pour {entite.entity_id}"
        )


async def test_les_entites_deviennent_indisponibles_au_dechargement(
    hass: HomeAssistant, entree_auto_backup: MockConfigEntry
) -> None:
    """Le déchargement de l'entrée rend les entités indisponibles."""
    entites = _entites(hass, entree_auto_backup)
    activees = [entite.entity_id for entite in entites if not entite.disabled]

    assert await hass.config_entries.async_unload(entree_auto_backup.entry_id)
    await hass.async_block_till_done()

    for entity_id in activees:
        etat = hass.states.get(entity_id)
        assert etat is not None and etat.state == STATE_UNAVAILABLE, (
            f"{entity_id} aurait dû devenir indisponible"
        )
