"""Tests des entités créées par `auto_backup` et de leur rattachement au device.

La liste des entités attendues est **stricte** : toute entité ajoutée ou
retirée doit y être reportée (cf. `docs/tests.md`). Elle est déclinée en deux
états, car le fork ajoute des entités par destination distante configurée
(issue #16) : sans destination, seules les entités upstream existent ; avec une
destination, trois entités supplémentaires la décrivent.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.auto_backup.const import CONF_DESTINATIONS, DOMAIN
from destinations_factices import config_factice

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

# Suffixes des entités ajoutées par le fork pour chaque destination distante
# configurée (issue #16). L'identifiant unique complet vaut
# `<entry_id>_<destination_id>_<suffixe>`.
SUFFIXES_PAR_DESTINATION = {
    "sensor": {"dernier_televersement", "sauvegardes_distantes"},
    "binary_sensor": {"probleme"},
}

DESTINATION_ATTENDUE = "destination_test"


def entites_attendues_avec_destination(entry_id: str) -> dict[str, set[str]]:
    """Liste stricte attendue quand une destination distante est configurée."""
    attendues = {domaine: set(ids) for domaine, ids in ENTITES_ATTENDUES.items()}
    for domaine, suffixes in SUFFIXES_PAR_DESTINATION.items():
        attendues[domaine] |= {
            f"{entry_id}_{DESTINATION_ATTENDUE}_{suffixe}" for suffixe in suffixes
        }
    return attendues


@pytest.fixture
async def entree_avec_destination(
    hass: HomeAssistant, integration_backup: None, fournisseur_factice: str
) -> AsyncIterator[MockConfigEntry]:
    """Entrée `auto_backup` initialisée avec une destination distante factice."""
    entree = MockConfigEntry(
        domain=DOMAIN,
        title="Auto Backup",
        data={},
        options={CONF_DESTINATIONS: [config_factice()]},
    )
    entree.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entree.entry_id)
    await hass.async_block_till_done()

    yield entree


def _entites(hass: HomeAssistant, entree: MockConfigEntry) -> list[er.RegistryEntry]:
    registre = er.async_get(hass)
    return er.async_entries_for_config_entry(registre, entree.entry_id)


def _par_domaine(hass: HomeAssistant, entree: MockConfigEntry) -> dict[str, set[str]]:
    """Identifiants uniques des entités de l'entrée, groupés par plateforme."""
    par_domaine: dict[str, set[str]] = {}
    for entite in _entites(hass, entree):
        par_domaine.setdefault(entite.domain, set()).add(entite.unique_id)
    return par_domaine


async def test_les_entites_upstream_sont_creees(
    hass: HomeAssistant, entree_auto_backup: MockConfigEntry
) -> None:
    """Sans destination distante, seules les entités upstream sont créées."""
    assert _par_domaine(hass, entree_auto_backup) == ENTITES_ATTENDUES


async def test_une_destination_ajoute_exactement_trois_entites(
    hass: HomeAssistant, entree_avec_destination: MockConfigEntry
) -> None:
    """Avec une destination, le fork ajoute ses trois entités, et rien d'autre."""
    assert _par_domaine(hass, entree_avec_destination) == (
        entites_attendues_avec_destination(entree_avec_destination.entry_id)
    )


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
