"""Registre des sauvegardes distantes rangé par dossier (issue #58).

Depuis #51, le dossier distant d'une destination peut changer. Ces tests
vérifient que le registre retient le dossier de chaque dépôt, que le capteur et
la purge ne considèrent que le dossier configuré, que l'historique de l'ancien
dossier est conservé (un retour en arrière le rend de nouveau géré), et que le
registre écrit avant cette évolution est migré sans perte.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import pytest
from homeassistant.const import ATTR_NAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.auto_backup.const import (
    ATTR_DESTINATION,
    ATTR_FOLDER,
    ATTR_REMOTE_ID,
    ATTR_SLUG,
    CONF_AUTO_PURGE,
    CONF_BACKUP_TIMEOUT,
    CONF_DESTINATIONS,
    DATA_DESTINATIONS,
    DATA_REMOTE_BACKUPS,
    DATA_REMOTE_PURGE,
    DEFAULT_BACKUP_TIMEOUT,
    DOMAIN,
    EVENT_UPLOAD_SUCCESSFUL,
    STORAGE_KEY_REMOTE_BACKUPS,
    STORAGE_MINOR_VERSION_REMOTE_BACKUPS,
    STORAGE_VERSION_REMOTE_BACKUPS,
)
from custom_components.auto_backup.destinations.entities import (
    SUFFIXE_SAUVEGARDES_DISTANTES,
)
from custom_components.auto_backup.destinations.retention import (
    EntreeRegistre,
    RegistreSauvegardesDistantes,
    entrees_du_registre,
    migrer_le_registre,
)
from destinations_factices import DestinationEnMemoire, config_factice

DESTINATION = "destination_test"
ANCIEN = "Sauvegardes"
NOUVEAU = "Sauvegardes/Nouveau"
CLE_STOCKAGE = f"{DOMAIN}.{STORAGE_KEY_REMOTE_BACKUPS}"


### MONTAGE ###


async def _demarrer(
    hass: HomeAssistant, destinations: list[dict[str, Any]] | None = None
) -> MockConfigEntry:
    """Initialise l'intégration ; purge automatique désactivée par défaut.

    Sans purge automatique, un `upload_successful` ne fait qu'inscrire la
    sauvegarde : le test décide lui-même quand la rétention s'applique.
    """
    entree = MockConfigEntry(
        domain=DOMAIN,
        title="Auto Backup",
        data={},
        options={
            CONF_AUTO_PURGE: False,
            CONF_BACKUP_TIMEOUT: DEFAULT_BACKUP_TIMEOUT,
            CONF_DESTINATIONS: (
                [config_factice()] if destinations is None else destinations
            ),
        },
    )
    entree.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entree.entry_id)
    await hass.async_block_till_done()
    return entree


def _registre(hass: HomeAssistant) -> RegistreSauvegardesDistantes:
    """Registre persistant des sauvegardes distantes."""
    return hass.data[DATA_REMOTE_BACKUPS]


def _destination(hass: HomeAssistant) -> DestinationEnMemoire:
    """Destination factice chargée."""
    return hass.data[DATA_DESTINATIONS].async_get(DESTINATION)


def _compteur(hass: HomeAssistant, entree: MockConfigEntry) -> str:
    """État du capteur « sauvegardes distantes » de la destination."""
    entity_id = er.async_get(hass).async_get_entity_id(
        Platform.SENSOR,
        DOMAIN,
        f"{entree.entry_id}_{DESTINATION}_{SUFFIXE_SAUVEGARDES_DISTANTES}",
    )
    assert entity_id is not None
    etat = hass.states.get(entity_id)
    assert etat is not None
    return etat.state


async def _changer_de_dossier(
    hass: HomeAssistant, entree: MockConfigEntry, dossier: str, **surcharges: Any
) -> None:
    """Change le dossier de la destination dans les options, sans rechargement."""
    hass.config_entries.async_update_entry(
        entree,
        options={
            **entree.options,
            CONF_DESTINATIONS: [config_factice(folder=dossier, **surcharges)],
        },
    )
    await hass.async_block_till_done()


def _televerser(hass: HomeAssistant, remote_id: str, **donnees: Any) -> None:
    """Émet un téléversement réussi, tel que le coordinateur de #8 le fait."""
    hass.bus.async_fire(
        EVENT_UPLOAD_SUCCESSFUL,
        {
            ATTR_DESTINATION: DESTINATION,
            ATTR_REMOTE_ID: remote_id,
            ATTR_NAME: remote_id,
            ATTR_SLUG: remote_id,
            **donnees,
        },
    )


@pytest.fixture
async def entree(
    hass: HomeAssistant, integration_backup: None, fournisseur_factice: str
) -> MockConfigEntry:
    """Intégration démarrée avec une destination factice dans `ANCIEN`."""
    return await _demarrer(hass)


### INSCRIPTION DU DOSSIER ###


async def test_un_depot_retient_le_dossier_annonce_par_l_evenement(
    hass: HomeAssistant, entree: MockConfigEntry
) -> None:
    """Le dossier de l'événement fait foi, normalisé comme à la configuration."""
    _televerser(hass, "a", **{ATTR_FOLDER: "\uff33auvegardes"})  # S pleine chasse
    await hass.async_block_till_done()

    assert _registre(hass).entree(DESTINATION, "a").folder == ANCIEN


@pytest.mark.parametrize("annonce", [None, "../evasion", 42])
async def test_sans_dossier_exploitable_le_dossier_configure_est_retenu(
    hass: HomeAssistant, entree: MockConfigEntry, annonce: Any
) -> None:
    """Un émetteur plus ancien, ou un dossier invalide : repli sur la config."""
    donnees = {} if annonce is None else {ATTR_FOLDER: annonce}
    _televerser(hass, "a", **donnees)
    await hass.async_block_till_done()

    assert _registre(hass).entree(DESTINATION, "a").folder == ANCIEN


### CAPTEUR ###


async def test_le_capteur_ne_compte_que_le_dossier_configure(
    hass: HomeAssistant, entree: MockConfigEntry
) -> None:
    """Critères 1 et 4 : 0 après le changement, l'ancien compte au retour."""
    for remote_id in ("a", "b"):
        _televerser(hass, remote_id, **{ATTR_FOLDER: ANCIEN})
    await hass.async_block_till_done()
    assert _compteur(hass, entree) == "2"

    await _changer_de_dossier(hass, entree, NOUVEAU)
    assert _compteur(hass, entree) == "0"
    # L'historique n'est pas effacé : les fichiers existent toujours.
    assert {e.remote_id for e in _registre(hass).entrees(DESTINATION)} == {"a", "b"}

    _televerser(hass, "c", **{ATTR_FOLDER: NOUVEAU})
    await hass.async_block_till_done()
    assert _compteur(hass, entree) == "1"

    await _changer_de_dossier(hass, entree, ANCIEN)
    assert _compteur(hass, entree) == "2"


### PURGE ###


async def test_la_purge_ignore_les_entrees_d_un_autre_dossier(
    hass: HomeAssistant, entree: MockConfigEntry
) -> None:
    """Critère 2 : ni supprimées, ni comptées dans `retention_count`.

    La destination factice liste tout ce qu'elle contient, quel que soit le
    dossier : c'est le pire cas, où une entrée d'un autre dossier serait
    présentée à la purge. Seul le registre du dossier configuré compte.
    """
    await _changer_de_dossier(
        hass, entree, NOUVEAU, retention_days=None, retention_count=1
    )
    destination = _destination(hass)
    maintenant = dt_util.utcnow()
    for heures, remote_id, dossier in (
        (4, "ancienne-1", ANCIEN),
        (3, "ancienne-2", ANCIEN),
        (2, "nouvelle-1", NOUVEAU),
        (1, "nouvelle-2", NOUVEAU),
    ):
        date = maintenant - timedelta(hours=heures)
        destination.ajouter_sauvegarde(remote_id, created_at=date)
        await _registre(hass).async_enregistrer(
            DESTINATION,
            EntreeRegistre(
                remote_id=remote_id, name=remote_id, created_at=date, folder=dossier
            ),
        )

    supprimes = await hass.data[DATA_REMOTE_PURGE].async_purger_destination(DESTINATION)

    # Avec les entrées de l'ancien dossier, `retention_count=1` aurait supprimé
    # trois sauvegardes ; seule la plus ancienne du nouveau dossier part.
    assert supprimes == ["nouvelle-1"]
    assert destination.suppressions == ["nouvelle-1"]
    assert {e.remote_id for e in _registre(hass).entrees(DESTINATION)} == {
        "ancienne-1",
        "ancienne-2",
        "nouvelle-2",
    }


async def test_un_retour_a_l_ancien_dossier_rend_ses_sauvegardes_purgeables(
    hass: HomeAssistant, entree: MockConfigEntry
) -> None:
    """Critère 4 : revenir au dossier précédent le fait de nouveau gérer.

    Chaque modification recharge les destinations : la destination factice
    repart vide, les fichiers sont donc redéposés après chaque changement.
    """
    maintenant = dt_util.utcnow()
    dates = {"a": 3, "b": 2, "c": 1}
    for remote_id, heures in dates.items():
        await _registre(hass).async_enregistrer(
            DESTINATION,
            EntreeRegistre(
                remote_id=remote_id,
                name=remote_id,
                created_at=maintenant - timedelta(hours=heures),
                folder=ANCIEN,
            ),
        )

    def deposer() -> DestinationEnMemoire:
        destination = _destination(hass)
        for remote_id, heures in dates.items():
            destination.ajouter_sauvegarde(
                remote_id, created_at=maintenant - timedelta(hours=heures)
            )
        return destination

    purge = hass.data[DATA_REMOTE_PURGE]
    await _changer_de_dossier(
        hass, entree, NOUVEAU, retention_days=None, retention_count=1
    )
    destination = deposer()
    assert await purge.async_purger_destination(DESTINATION) == []
    assert destination.suppressions == []

    await _changer_de_dossier(
        hass, entree, ANCIEN, retention_days=None, retention_count=1
    )
    deposer()
    assert sorted(await purge.async_purger_destination(DESTINATION)) == ["a", "b"]
    assert [e.remote_id for e in _registre(hass).entrees(DESTINATION)] == ["c"]


### LISTAGE DES FOURNISSEURS ###


async def test_les_fournisseurs_ne_voient_que_le_dossier_demande(
    hass: HomeAssistant, entree: MockConfigEntry
) -> None:
    """`entrees_du_registre()` filtre par dossier, normalisé en NFKC."""
    for remote_id, dossier in (("a", ANCIEN), ("b", NOUVEAU), ("c", None)):
        await _registre(hass).async_enregistrer(
            DESTINATION,
            EntreeRegistre(remote_id=remote_id, name=remote_id, folder=dossier),
        )

    assert [e.remote_id for e in entrees_du_registre(hass, DESTINATION, ANCIEN)] == [
        "a"
    ]
    assert [
        e.remote_id for e in entrees_du_registre(hass, DESTINATION, "\uff33auvegardes")
    ] == ["a"]
    assert entrees_du_registre(hass, DESTINATION, "../x") == []


### MIGRATION DU STOCKAGE ###


async def test_un_registre_d_avant_58_est_migre_sans_perte(
    hass: HomeAssistant,
    integration_backup: None,
    fournisseur_factice: str,
    hass_storage: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Critère 3 : entrées rattachées au dossier configuré, rien de perdu."""
    hass_storage[CLE_STOCKAGE] = {
        "version": 1,
        "key": CLE_STOCKAGE,
        "data": {
            DESTINATION: [
                {"remote_id": "a", "name": "a", "slug": "s", "size": 1},
                {"remote_id": "b", "name": "b", "folder": ANCIEN},
            ],
            "destination_supprimee": [{"remote_id": "z", "name": "z"}],
        },
    }

    with caplog.at_level(logging.WARNING):
        entree = await _demarrer(hass, [config_factice(folder=NOUVEAU)])

    assert "Traceback" not in caplog.text
    registre = _registre(hass)
    assert registre.entree(DESTINATION, "a").folder == NOUVEAU
    assert registre.entree(DESTINATION, "a").slug == "s"
    # Idempotence : une entrée qui avait déjà un dossier le garde.
    assert registre.entree(DESTINATION, "b").folder == ANCIEN
    # Destination absente : l'entrée est conservée, dossier inconnu.
    assert registre.entree("destination_supprimee", "z").folder is None
    assert _compteur(hass, entree) == "1"

    stocke = hass_storage[CLE_STOCKAGE]
    assert stocke["version"] == STORAGE_VERSION_REMOTE_BACKUPS
    assert stocke["minor_version"] == STORAGE_MINOR_VERSION_REMOTE_BACKUPS
    assert stocke["data"][DESTINATION][0][ATTR_FOLDER] == NOUVEAU

    # Un second démarrage ne change rien.
    assert await hass.config_entries.async_reload(entree.entry_id)
    await hass.async_block_till_done()
    assert _registre(hass).entree(DESTINATION, "a").folder == NOUVEAU
    assert _registre(hass).entree("destination_supprimee", "z").folder is None


@pytest.mark.parametrize(
    "donnees",
    [
        None,
        ["pas un dictionnaire"],
        {"d": "pas une liste"},
        {"d": ["pas un dictionnaire", {"remote_id": "a"}]},
    ],
)
def test_la_migration_tolere_un_contenu_inattendu(donnees: Any) -> None:
    """Un contenu abîmé est rendu tel quel, jamais une exception."""
    migrees = migrer_le_registre(donnees, {"d": ANCIEN})
    if isinstance(donnees, dict) and isinstance(donnees["d"], list):
        assert migrees == {
            "d": ["pas un dictionnaire", {"remote_id": "a", "folder": ANCIEN}]
        }
    else:
        assert migrees == donnees


def test_la_migration_est_idempotente() -> None:
    """Migrer deux fois, même avec un autre dossier, ne change plus rien."""
    une_fois = migrer_le_registre({"d": [{"remote_id": "a"}]}, {"d": ANCIEN})
    assert migrer_le_registre(une_fois, {"d": NOUVEAU}) == une_fois


def test_une_entree_au_dossier_illisible_est_conservee_sans_dossier() -> None:
    """Un fichier modifié à la main : l'entrée reste, son dossier est inconnu."""
    entree = EntreeRegistre.from_dict({"remote_id": "a", "folder": "../x"})
    assert entree is not None
    assert entree.folder is None
    assert entree.as_dict()[ATTR_FOLDER] is None
