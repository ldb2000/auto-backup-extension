"""Tests du flux de configuration et du flux d'options d'`auto_backup`."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.auto_backup.const import (
    CONF_AUTO_PURGE,
    CONF_BACKUP_TIMEOUT,
    DATA_AUTO_BACKUP,
    DEFAULT_BACKUP_TIMEOUT,
    DOMAIN,
)
from custom_components.auto_backup.manager import AutoBackup

# Signature de la fixture `ouvrir_les_options` (cf. `tests/conftest.py`).
type OuvrirLesOptions = Callable[[str, str], Awaitable[dict]]


async def test_le_flux_user_cree_l_entree_auto_backup(
    hass: HomeAssistant, integration_backup: None
) -> None:
    """Le flux « user » affiche un formulaire puis crée l'entrée « Auto Backup »."""
    resultat = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert resultat["type"] is FlowResultType.FORM
    assert resultat["step_id"] == "user"

    resultat = await hass.config_entries.flow.async_configure(resultat["flow_id"], {})
    await hass.async_block_till_done()

    assert resultat["type"] is FlowResultType.CREATE_ENTRY
    assert resultat["title"] == "Auto Backup"

    entrees = hass.config_entries.async_entries(DOMAIN)
    assert len(entrees) == 1
    assert entrees[0].title == "Auto Backup"


async def test_une_seconde_entree_est_refusee(
    hass: HomeAssistant, entree_auto_backup: MockConfigEntry
) -> None:
    """Une seconde configuration est refusée avec le motif `single_instance`."""
    resultat = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert resultat["type"] is FlowResultType.FORM

    resultat = await hass.config_entries.flow.async_configure(resultat["flow_id"], {})

    assert resultat["type"] is FlowResultType.ABORT
    assert resultat["reason"] == "single_instance"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_le_flux_est_refuse_sans_supervisor_ni_backup(
    hass: HomeAssistant,
) -> None:
    """Sans Supervisor ni intégration `backup`, le flux abandonne.

    L'intégration `backup` n'est volontairement pas chargée ici : c'est le seul
    cas où `validate_input()` échoue sur une installation Home Assistant Core.
    """
    assert "backup" not in hass.config.components
    assert "hassio" not in hass.config.components

    resultat = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    resultat = await hass.config_entries.flow.async_configure(resultat["flow_id"], {})

    assert resultat["type"] is FlowResultType.ABORT
    assert resultat["reason"] == "missing_service"
    assert hass.config_entries.async_entries(DOMAIN) == []


async def test_le_flux_d_options_ouvre_sur_le_menu_des_destinations(
    hass: HomeAssistant, entree_auto_backup: MockConfigEntry
) -> None:
    """Le flux d'options s'ouvre sur un menu (issue #7).

    Sans destination configurée, seules l'addition d'une destination et les
    réglages upstream sont proposées : ré-autoriser ou supprimer n'aurait
    aucun sens.
    """
    resultat = await hass.config_entries.options.async_init(entree_auto_backup.entry_id)

    assert resultat["type"] is FlowResultType.MENU
    assert resultat["step_id"] == "menu"
    assert list(resultat["menu_options"]) == ["ajouter_destination", "init"]


async def test_le_flux_d_options_propose_les_valeurs_par_defaut(
    hass: HomeAssistant,
    entree_auto_backup: MockConfigEntry,
    ouvrir_les_options: OuvrirLesOptions,
) -> None:
    """Le formulaire upstream `init` reste accessible depuis le menu."""
    resultat = await ouvrir_les_options(entree_auto_backup.entry_id, "init")

    assert resultat["type"] is FlowResultType.FORM
    assert resultat["step_id"] == "init"


@pytest.mark.parametrize(
    ("auto_purge", "backup_timeout"),
    [(False, 45), (True, 5)],
)
async def test_le_flux_d_options_persiste_et_applique_les_valeurs(
    hass: HomeAssistant,
    entree_auto_backup: MockConfigEntry,
    gestionnaire_auto_backup: AutoBackup,
    ouvrir_les_options: OuvrirLesOptions,
    auto_purge: bool,
    backup_timeout: int,
) -> None:
    """Les options modifiées sont persistées et reprises par le gestionnaire."""
    # État initial : les valeurs par défaut de l'intégration.
    assert gestionnaire_auto_backup._auto_purge is True
    assert gestionnaire_auto_backup._backup_timeout == DEFAULT_BACKUP_TIMEOUT * 60

    resultat = await ouvrir_les_options(entree_auto_backup.entry_id, "init")
    resultat = await hass.config_entries.options.async_configure(
        resultat["flow_id"],
        user_input={
            CONF_AUTO_PURGE: auto_purge,
            CONF_BACKUP_TIMEOUT: backup_timeout,
        },
    )
    await hass.async_block_till_done()

    assert resultat["type"] is FlowResultType.CREATE_ENTRY
    assert entree_auto_backup.options == {
        CONF_AUTO_PURGE: auto_purge,
        CONF_BACKUP_TIMEOUT: backup_timeout,
    }

    # Le gestionnaire chargé est notifié par l'écouteur de mise à jour de l'entrée :
    # le délai d'expiration est converti en secondes.
    assert gestionnaire_auto_backup._auto_purge is auto_purge
    assert gestionnaire_auto_backup._backup_timeout == backup_timeout * 60


async def test_les_options_sont_relues_au_demarrage(
    hass: HomeAssistant, integration_backup: None
) -> None:
    """Une entrée déjà porteuse d'options configure le gestionnaire au démarrage."""
    entree = MockConfigEntry(
        domain=DOMAIN,
        title="Auto Backup",
        data={},
        options={CONF_AUTO_PURGE: False, CONF_BACKUP_TIMEOUT: 30},
    )
    entree.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entree.entry_id)
    await hass.async_block_till_done()

    gestionnaire: AutoBackup = hass.data[DATA_AUTO_BACKUP]
    assert gestionnaire._auto_purge is False
    assert gestionnaire._backup_timeout == 30 * 60
