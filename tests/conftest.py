"""Socle de tests Home Assistant de l'intégration `auto_backup` (issue #4).

Ce fichier fournit tout ce qu'il faut pour démarrer l'intégration dans une
instance Home Assistant de test en quelques lignes :

- le paquet `custom_components` du dépôt est rendu chargeable par Home Assistant ;
- la fixture `auto_enable_custom_integrations` est appliquée automatiquement ;
- `entree_auto_backup` initialise l'intégration depuis une `MockConfigEntry` ;
- `gestionnaire_auto_backup` donne accès au gestionnaire stocké dans `hass.data`.

Les tests marqués `network` (comparaison avec le dépôt upstream) ne sont pas
exécutés par défaut : ajouter `--tests-reseau` à la ligne de commande.
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

RACINE_DEPOT = Path(__file__).resolve().parent.parent

# Home Assistant découvre les intégrations personnalisées en important le paquet
# `custom_components` depuis `sys.path` (cf. `homeassistant.loader`). Or
# `pytest-homeassistant-custom-component` ajoute son propre répertoire de
# configuration de test à `sys.path` au démarrage de chaque instance, et le
# `custom_components` qu'il embarque est un paquet régulier (il contient un
# `__init__.py`) : il l'emporterait sur celui du dépôt, qui est un paquet d'espace
# de noms. On importe donc le paquet du dépôt ici, avant toute création d'instance
# Home Assistant. Il est alors figé dans `sys.modules`, et l'intégration est chargée
# depuis ses fichiers réels — ce dont la mesure de couverture a besoin elle aussi.
if str(RACINE_DEPOT) not in sys.path:
    sys.path.insert(0, str(RACINE_DEPOT))

import custom_components  # noqa: E402
from custom_components.auto_backup.const import DATA_AUTO_BACKUP, DOMAIN  # noqa: E402
from custom_components.auto_backup.manager import AutoBackup  # noqa: E402

_CHEMIN_PAQUET = Path(next(iter(custom_components.__path__))).resolve()
if _CHEMIN_PAQUET != RACINE_DEPOT / "custom_components":
    raise RuntimeError(
        "le paquet `custom_components` chargé n'est pas celui du dépôt : "
        f"{_CHEMIN_PAQUET}"
    )


def pytest_addoption(parser: pytest.Parser) -> None:
    """Déclare l'option qui active les tests réseau."""
    parser.addoption(
        "--tests-reseau",
        action="store_true",
        default=False,
        help="Exécuter aussi les tests marqués « network » (accès à GitHub via `gh`).",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Ignore les tests marqués « network » sauf si `--tests-reseau` est passé."""
    if config.getoption("--tests-reseau"):
        return
    ignorer = pytest.mark.skip(
        reason="test réseau : relancer avec `--tests-reseau` pour l'exécuter"
    )
    for item in items:
        if "network" in item.keywords:
            item.add_marker(ignorer)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Rend `custom_components/auto_backup` chargeable par les instances de test."""
    return None


@pytest.fixture
async def integration_backup(hass: HomeAssistant) -> None:
    """Charge l'intégration `backup` du cœur de Home Assistant.

    Sur une installation Home Assistant Core (non supervisée), `auto_backup`
    s'appuie sur cette intégration : elle conditionne `helpers.is_backup()` et
    fournit le `BackupManager` utilisé par `BackupHandler`.
    """
    assert await async_setup_component(hass, "backup", {})
    await hass.async_block_till_done()


@pytest.fixture
async def entree_auto_backup(
    hass: HomeAssistant, integration_backup: None
) -> AsyncIterator[MockConfigEntry]:
    """Initialise `auto_backup` à partir d'une entrée de configuration factice."""
    entree = MockConfigEntry(domain=DOMAIN, title="Auto Backup", data={})
    entree.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entree.entry_id)
    await hass.async_block_till_done()

    yield entree


@pytest.fixture
def gestionnaire_auto_backup(
    hass: HomeAssistant, entree_auto_backup: MockConfigEntry
) -> AutoBackup:
    """Renvoie le gestionnaire `AutoBackup` stocké dans `hass.data`."""
    return hass.data[DATA_AUTO_BACKUP]
