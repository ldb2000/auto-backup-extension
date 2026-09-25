"""Persistance des destinations distantes dans l'entrée de configuration (issue #6).

Le support retenu est `entry.options[CONF_DESTINATIONS]`, justifié dans
`docs/adr/0001-destinations-distantes.md`. Ces tests vérifient qu'une
destination configurée est rechargée à l'identique après un redémarrage de
Home Assistant, simulé par le déchargement puis le rechargement de l'entrée.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.auto_backup.const import (
    CLES_DU_FORK,
    CONF_AUTO_PURGE,
    CONF_BACKUP_TIMEOUT,
    CONF_DESTINATIONS,
    CONF_NOTIFY_ON_FAILURE,
    CONF_UPLOAD_TIMEOUT,
    DATA_DESTINATIONS,
    DEFAULT_BACKUP_TIMEOUT,
    DOMAIN,
)
from custom_components.auto_backup.destinations import (
    DestinationConfig,
    DestinationConfigError,
    DestinationManager,
    async_persist_destinations,
    preserve_fork_options,
)
from destinations_factices import PROVIDER_FACTICE, config_factice


@pytest.fixture
async def entree_sans_destination(
    hass: HomeAssistant, integration_backup: None, fournisseur_factice: str
) -> MockConfigEntry:
    """Entrée `auto_backup` initialisée, fournisseur factice enregistré."""
    entree = MockConfigEntry(domain=DOMAIN, title="Auto Backup", data={})
    entree.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entree.entry_id)
    await hass.async_block_till_done()

    return entree


@pytest.fixture
async def entree_avec_destination(
    hass: HomeAssistant, integration_backup: None, fournisseur_factice: str
) -> MockConfigEntry:
    """Entrée `auto_backup` dont les options portent déjà une destination."""
    entree = MockConfigEntry(
        domain=DOMAIN,
        title="Auto Backup",
        data={},
        options={CONF_DESTINATIONS: [config_factice()]},
    )
    entree.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entree.entry_id)
    await hass.async_block_till_done()

    return entree


def _gestionnaire(hass: HomeAssistant) -> DestinationManager:
    """Gestionnaire de destinations exposé par l'intégration."""
    return hass.data[DATA_DESTINATIONS]


async def test_le_setup_expose_les_destinations_persistees(
    hass: HomeAssistant, entree_avec_destination: MockConfigEntry
) -> None:
    """Au démarrage, les destinations des options sont instanciées et exposées."""
    gestionnaire = _gestionnaire(hass)

    assert isinstance(gestionnaire, DestinationManager)
    assert gestionnaire.configs == [DestinationConfig.from_dict(config_factice())]
    assert gestionnaire.async_get("destination_test").provider == PROVIDER_FACTICE


async def test_une_destination_ajoutee_survit_au_redemarrage(
    hass: HomeAssistant, entree_sans_destination: MockConfigEntry
) -> None:
    """Une destination ajoutée est rechargée à l'identique après redémarrage."""
    config = DestinationConfig.from_dict(config_factice())
    assert _gestionnaire(hass).configs == []

    async_persist_destinations(hass, entree_sans_destination, [config])
    await hass.async_block_till_done()

    # Ce que Home Assistant réécrirait dans `.storage` doit être sérialisable.
    options = dict(entree_sans_destination.options)
    assert json.loads(json.dumps(options)) == options
    assert entree_sans_destination.options[CONF_DESTINATIONS] == [config_factice()]

    # Redémarrage simulé : l'entrée est déchargée puis rechargée.
    assert await hass.config_entries.async_reload(entree_sans_destination.entry_id)
    await hass.async_block_till_done()

    assert entree_sans_destination.state is ConfigEntryState.LOADED
    assert _gestionnaire(hass).configs == [config]


async def test_la_mise_a_jour_des_options_recharge_les_destinations(
    hass: HomeAssistant, entree_avec_destination: MockConfigEntry
) -> None:
    """Modifier les options met à jour les destinations sans rechargement."""
    config = DestinationConfig.from_dict(config_factice(destination_id="autre"))

    async_persist_destinations(hass, entree_avec_destination, [config])
    await hass.async_block_till_done()

    assert _gestionnaire(hass).configs == [config]


async def test_la_persistance_complete_les_options_upstream(
    hass: HomeAssistant, entree_sans_destination: MockConfigEntry
) -> None:
    """L'écriture des destinations préserve et complète les options upstream.

    L'écouteur de mise à jour upstream lit `entry.options[CONF_AUTO_PURGE]` sans
    valeur de repli : les options upstream absentes sont donc complétées par
    leurs valeurs par défaut au moment d'écrire les destinations.
    """
    async_persist_destinations(
        hass,
        entree_sans_destination,
        [DestinationConfig.from_dict(config_factice())],
    )
    await hass.async_block_till_done()

    assert entree_sans_destination.options[CONF_AUTO_PURGE] is True
    assert entree_sans_destination.options[CONF_BACKUP_TIMEOUT] == (
        DEFAULT_BACKUP_TIMEOUT
    )


async def test_la_persistance_conserve_les_options_upstream_existantes(
    hass: HomeAssistant, integration_backup: None, fournisseur_factice: str
) -> None:
    """Les options upstream déjà choisies par l'utilisateur ne sont pas écrasées."""
    entree = MockConfigEntry(
        domain=DOMAIN,
        title="Auto Backup",
        data={},
        options={CONF_AUTO_PURGE: False, CONF_BACKUP_TIMEOUT: 30},
    )
    entree.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entree.entry_id)
    await hass.async_block_till_done()

    async_persist_destinations(
        hass, entree, [DestinationConfig.from_dict(config_factice())]
    )
    await hass.async_block_till_done()

    assert entree.options[CONF_AUTO_PURGE] is False
    assert entree.options[CONF_BACKUP_TIMEOUT] == 30
    assert entree.options[CONF_DESTINATIONS] == [config_factice()]


async def test_la_persistance_refuse_deux_destinations_de_meme_identifiant(
    hass: HomeAssistant, entree_sans_destination: MockConfigEntry
) -> None:
    """Deux destinations homonymes ne peuvent pas être persistées."""
    config = DestinationConfig.from_dict(config_factice())

    with pytest.raises(DestinationConfigError):
        async_persist_destinations(hass, entree_sans_destination, [config, config])

    assert CONF_DESTINATIONS not in entree_sans_destination.options


async def test_le_flux_d_options_conserve_les_destinations(
    hass: HomeAssistant,
    entree_avec_destination: MockConfigEntry,
    ouvrir_les_options: Callable[[str, str], Awaitable[dict]],
) -> None:
    """Enregistrer le formulaire d'options ne perd pas les destinations."""
    resultat = await ouvrir_les_options(entree_avec_destination.entry_id, "init")
    resultat = await hass.config_entries.options.async_configure(
        resultat["flow_id"],
        user_input={CONF_AUTO_PURGE: False, CONF_BACKUP_TIMEOUT: 45},
    )
    await hass.async_block_till_done()

    assert resultat["type"] is FlowResultType.CREATE_ENTRY
    assert entree_avec_destination.options[CONF_AUTO_PURGE] is False
    assert entree_avec_destination.options[CONF_DESTINATIONS] == [config_factice()]
    assert _gestionnaire(hass).configs == [
        DestinationConfig.from_dict(config_factice())
    ]


def test_le_report_des_options_du_fork_est_neutre_sans_option_du_fork() -> None:
    """Sans option du fork configurée, les options soumises ne sont pas modifiées."""
    assert preserve_fork_options({}, {CONF_AUTO_PURGE: True}) == {CONF_AUTO_PURGE: True}


def test_le_report_couvre_toutes_les_options_du_fork() -> None:
    """Chaque clé de `CLES_DU_FORK` est reportée, et aucune n'est inventée.

    Le test part de la liste elle-même : une option ajoutée par une issue
    suivante est donc couverte sans qu'il faille revenir ici, et elle échouera
    tant qu'elle n'est pas reportée.
    """
    assert set(CLES_DU_FORK) == {
        CONF_DESTINATIONS,
        CONF_UPLOAD_TIMEOUT,
        CONF_NOTIFY_ON_FAILURE,
    }

    existantes = {cle: f"valeur de {cle}" for cle in CLES_DU_FORK}
    reportees = preserve_fork_options(
        {**existantes, "cle_upstream": "remplacée par le formulaire"},
        {CONF_AUTO_PURGE: True},
    )

    assert reportees == {CONF_AUTO_PURGE: True, **existantes}


async def test_les_destinations_disparaissent_au_dechargement(
    hass: HomeAssistant, entree_avec_destination: MockConfigEntry
) -> None:
    """Le déchargement de l'entrée retire le gestionnaire de `hass.data`."""
    assert await hass.config_entries.async_unload(entree_avec_destination.entry_id)
    await hass.async_block_till_done()

    assert DATA_DESTINATIONS not in hass.data


async def test_une_destination_inutilisable_n_empeche_pas_le_demarrage(
    hass: HomeAssistant, integration_backup: None, caplog: pytest.LogCaptureFixture
) -> None:
    """Un fournisseur non installé est ignoré : l'intégration démarre quand même.

    C'est le cas après la suppression d'un fournisseur ou la restauration d'une
    sauvegarde de configuration sur une installation qui ne l'a pas.
    """
    entree = MockConfigEntry(
        domain=DOMAIN,
        title="Auto Backup",
        data={},
        options={CONF_DESTINATIONS: [config_factice(provider="jamais_installe")]},
    )
    entree.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entree.entry_id)
    await hass.async_block_till_done()

    assert entree.state is ConfigEntryState.LOADED
    assert _gestionnaire(hass).configs == []
    assert "fournisseur inconnu" in caplog.text
