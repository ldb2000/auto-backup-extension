"""Rétention et purge des sauvegardes distantes (issue #9).

Tout passe par le fournisseur factice de `tests/destinations_factices.py` :
aucun accès réseau, aucun fichier lu. Les sauvegardes distantes sont déposées
directement dans le fournisseur (`ajouter_sauvegarde()`), et inscrites — ou non
— au registre persistant du fork : c'est cette distinction qui décide de ce qui
est purgeable.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import ATTR_NAME
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)

from custom_components.auto_backup.const import (
    ATTR_DESTINATION,
    ATTR_DESTINATION_NAME,
    ATTR_REMOTE_ID,
    ATTR_REMOTE_IDS,
    ATTR_SIZE,
    ATTR_SLUG,
    ATTR_UPLOAD_TO,
    CONF_AUTO_PURGE,
    CONF_BACKUP_TIMEOUT,
    CONF_DESTINATIONS,
    DATA_AUTO_BACKUP,
    DATA_DESTINATIONS,
    DATA_REMOTE_BACKUPS,
    DATA_REMOTE_PURGE,
    DEFAULT_BACKUP_TIMEOUT,
    DOMAIN,
    EVENT_BACKUPS_PURGED,
    EVENT_REMOTE_PURGE,
    EVENT_UPLOAD_SUCCESSFUL,
    SERVICE_BACKUP,
    SERVICE_PURGE,
    STORAGE_KEY_REMOTE_BACKUPS,
)
from custom_components.auto_backup.destinations import (
    DestinationError,
    DestinationNotFoundError,
    RemoteBackup,
)
from custom_components.auto_backup.destinations.retention import (
    CLE_MARQUEUR,
    CoordinateurPurgeDistante,
    EntreeRegistre,
    RegistreSauvegardesDistantes,
    marqueur_auto_backup,
    porte_le_marqueur,
)
from destinations_factices import DestinationEnMemoire, config_factice

SLUG = "abc123"
CONTENU_SAUVEGARDE = b"auto-backup"
CLE_STOCKAGE = f"{DOMAIN}.{STORAGE_KEY_REMOTE_BACKUPS}"


### MONTAGE ###


def _faux_backup_manager(chemin: Path) -> MagicMock:
    """`BackupManager` minimal : une sauvegarde locale et son agent."""
    sauvegarde = MagicMock()
    sauvegarde.backup_id = SLUG
    agent = MagicMock()
    agent.get_backup_path = MagicMock(return_value=chemin)
    manager = MagicMock()
    manager.async_get_backup = AsyncMock(return_value=(sauvegarde, {}))
    manager.local_backup_agents = {"backup.local": agent}
    return manager


async def _demarrer(
    hass: HomeAssistant,
    *,
    destinations: list[dict[str, Any]] | None = None,
    options: dict[str, Any] | None = None,
    fichier: Path | None = None,
) -> MockConfigEntry:
    """Initialise l'intégration avec des destinations factices."""
    entree = MockConfigEntry(
        domain=DOMAIN,
        title="Auto Backup",
        data={},
        options={
            CONF_AUTO_PURGE: True,
            CONF_BACKUP_TIMEOUT: DEFAULT_BACKUP_TIMEOUT,
            CONF_DESTINATIONS: (
                [config_factice()] if destinations is None else destinations
            ),
            **(options or {}),
        },
    )
    entree.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entree.entry_id)
    await hass.async_block_till_done()

    if fichier is not None:
        handler = hass.data[DATA_AUTO_BACKUP]._handler
        handler._manager = _faux_backup_manager(fichier)
        handler.create_backup = AsyncMock(return_value={"slug": SLUG})
    return entree


def _destination(hass: HomeAssistant, destination_id: str) -> DestinationEnMemoire:
    """Destination factice chargée pour cet identifiant."""
    return hass.data[DATA_DESTINATIONS].async_get(destination_id)


def _registre(hass: HomeAssistant) -> RegistreSauvegardesDistantes:
    """Registre persistant des sauvegardes distantes déposées."""
    return hass.data[DATA_REMOTE_BACKUPS]


def _coordinateur(hass: HomeAssistant) -> CoordinateurPurgeDistante:
    """Coordinateur de purge distante de l'entrée chargée."""
    return hass.data[DATA_REMOTE_PURGE]


def _inscrits(
    hass: HomeAssistant, destination_id: str = "destination_test"
) -> list[str]:
    """Identifiants distants inscrits au registre pour cette destination."""
    return [entree.remote_id for entree in _registre(hass).entrees(destination_id)]


async def _deposer(
    hass: HomeAssistant,
    destination: DestinationEnMemoire,
    remote_id: str,
    *,
    jours: float = 0,
    date: datetime | None = None,
    sans_date: bool = False,
    inscrire: bool = True,
    **surcharges: Any,
) -> RemoteBackup:
    """Dépose une sauvegarde chez le fournisseur et l'inscrit au registre.

    `inscrire=False` simule un fichier que l'utilisateur aurait déposé
    lui-même : présent chez le fournisseur, inconnu du fork. `sans_date=True`
    simule un fournisseur qui ne date pas ses fichiers.
    """
    if sans_date:
        date = None
    elif date is None:
        date = dt_util.utcnow() - timedelta(days=jours)
    sauvegarde = destination.ajouter_sauvegarde(
        remote_id, created_at=date, **surcharges
    )
    if inscrire:
        await _registre(hass).async_enregistrer(
            destination.destination_id,
            EntreeRegistre(
                remote_id=sauvegarde.remote_id,
                name=sauvegarde.name,
                slug=sauvegarde.slug,
                created_at=date,
                size=sauvegarde.size,
            ),
        )
    return sauvegarde


def _delai_de_purge(secondes: float) -> Any:
    """Raccourcit le filet de sécurité de la purge le temps d'un test.

    `DEFAULT_PURGE_TIMEOUT` vaut 300 s : un test qui l'attendrait vraiment
    n'en finirait pas. Le délai est donc ramené à quelques millisecondes, et
    c'est le fournisseur factice qui simule l'appel qui ne revient pas
    (`attente_de_listage`, `attentes_de_suppression`).
    """
    return patch(
        "custom_components.auto_backup.destinations.retention.DEFAULT_PURGE_TIMEOUT",
        secondes,
    )


@pytest.fixture
def fichier_de_sauvegarde(tmp_path: Path) -> Path:
    """Fichier `.tar` local tenant lieu de sauvegarde Home Assistant."""
    chemin = tmp_path / f"{SLUG}.tar"
    chemin.write_bytes(CONTENU_SAUVEGARDE)
    return chemin


@pytest.fixture
async def entree(
    hass: HomeAssistant, integration_backup: None, fournisseur_factice: str
) -> MockConfigEntry:
    """Intégration démarrée avec une destination factice unique."""
    return await _demarrer(hass)


### RÉTENTION EN JOURS ###


async def test_la_retention_en_jours_supprime_les_sauvegardes_expirees(
    hass: HomeAssistant, entree: MockConfigEntry
) -> None:
    """Au-delà de `retention_days`, une sauvegarde déposée est supprimée."""
    destination = _destination(hass, "destination_test")  # 7 jours, 3 sauvegardes
    await _deposer(hass, destination, "vieille", jours=10)
    await _deposer(hass, destination, "recente", jours=1)

    supprimes = await _coordinateur(hass).async_purger_toutes()

    assert supprimes == {"destination_test": ["vieille"]}
    assert set(destination.sauvegardes) == {"recente"}
    assert _inscrits(hass) == ["recente"]


async def test_une_sauvegarde_sans_date_connue_n_est_jamais_reputee_expiree(
    hass: HomeAssistant, entree: MockConfigEntry
) -> None:
    """Sans date, l'ancienneté est inconnue : la sauvegarde reste en place.

    Ni le fournisseur (`created_at` absent) ni le registre (entrée déposée sans
    date) ne savent quand cette sauvegarde est arrivée.
    """
    destination = _destination(hass, "destination_test")
    await _deposer(hass, destination, "sans_date", sans_date=True)
    assert _registre(hass).entree("destination_test", "sans_date").created_at is None

    assert await _coordinateur(hass).async_purger_toutes() == {}
    assert set(destination.sauvegardes) == {"sans_date"}


async def test_une_date_sans_fuseau_est_reputee_utc(
    hass: HomeAssistant, entree: MockConfigEntry
) -> None:
    """Un fournisseur qui date en « Zulu » sans le dire reste exploitable."""
    destination = _destination(hass, "destination_test")
    naive = (dt_util.utcnow() - timedelta(days=30)).replace(tzinfo=None)
    await _deposer(hass, destination, "vieille", date=naive)

    assert await _coordinateur(hass).async_purger_toutes() == {
        "destination_test": ["vieille"]
    }


### RÉTENTION EN NOMBRE ###


async def test_la_retention_en_nombre_supprime_les_plus_anciennes(
    hass: HomeAssistant, integration_backup: None, fournisseur_factice: str
) -> None:
    """Au-delà de `retention_count`, les plus anciennes partent d'abord."""
    await _demarrer(
        hass,
        destinations=[config_factice(retention_days=None, retention_count=2)],
    )
    destination = _destination(hass, "destination_test")
    for jours in (4, 3, 2, 1):
        await _deposer(hass, destination, f"j{jours}", jours=jours)

    supprimes = await _coordinateur(hass).async_purger_toutes()

    assert supprimes == {"destination_test": ["j4", "j3"]}
    assert set(destination.sauvegardes) == {"j2", "j1"}


async def test_la_retention_en_nombre_ne_supprime_rien_sous_la_limite(
    hass: HomeAssistant, integration_backup: None, fournisseur_factice: str
) -> None:
    """Exactement `retention_count` sauvegardes : aucune n'est supprimée."""
    await _demarrer(
        hass,
        destinations=[config_factice(retention_days=None, retention_count=2)],
    )
    destination = _destination(hass, "destination_test")
    await _deposer(hass, destination, "a", jours=100)
    await _deposer(hass, destination, "b", jours=200)

    assert await _coordinateur(hass).async_purger_toutes() == {}
    assert destination.suppressions == []


async def test_les_deux_retentions_se_combinent(
    hass: HomeAssistant, integration_backup: None, fournisseur_factice: str
) -> None:
    """L'âge condamne d'abord, le nombre borne ensuite ce qui reste."""
    await _demarrer(
        hass,
        destinations=[config_factice(retention_days=7, retention_count=2)],
    )
    destination = _destination(hass, "destination_test")
    for jours in (30, 20, 5, 3, 1):
        await _deposer(hass, destination, f"j{jours}", jours=jours)

    supprimes = await _coordinateur(hass).async_purger_toutes()

    # Deux expirées (30 et 20 jours), puis la plus ancienne des trois
    # survivantes, la limite de deux sauvegardes portant sur ce qui reste.
    assert supprimes == {"destination_test": ["j30", "j20", "j5"]}
    assert set(destination.sauvegardes) == {"j3", "j1"}


async def test_sans_retention_configuree_rien_n_est_supprime(
    hass: HomeAssistant, integration_backup: None, fournisseur_factice: str
) -> None:
    """Une destination sans rétention n'est même pas listée."""
    await _demarrer(
        hass,
        destinations=[config_factice(retention_days=None, retention_count=None)],
    )
    destination = _destination(hass, "destination_test")
    await _deposer(hass, destination, "tres_vieille", jours=3650)

    assert await _coordinateur(hass).async_purger_toutes() == {}
    assert destination.listages == 0
    assert set(destination.sauvegardes) == {"tres_vieille"}


### PROVENANCE : REGISTRE ET MARQUEUR ###


async def test_un_fichier_etranger_n_est_jamais_supprime(
    hass: HomeAssistant, entree: MockConfigEntry
) -> None:
    """Un fichier que le fork n'a pas déposé est invisible pour la purge."""
    destination = _destination(hass, "destination_test")
    await _deposer(hass, destination, "la_notre", jours=365)
    await _deposer(hass, destination, "photos_vacances", jours=3650, inscrire=False)

    supprimes = await _coordinateur(hass).async_purger_toutes()

    assert supprimes == {"destination_test": ["la_notre"]}
    assert destination.suppressions == ["la_notre"]
    assert set(destination.sauvegardes) == {"photos_vacances"}


@pytest.mark.parametrize(
    ("metadonnees", "purgeable"),
    [
        ({CLE_MARQUEUR: True}, True),
        ({CLE_MARQUEUR: "true"}, True),
        ({CLE_MARQUEUR: "1"}, True),
        ({CLE_MARQUEUR: DOMAIN}, True),
        ({CLE_MARQUEUR: False}, False),
        ({CLE_MARQUEUR: "non"}, False),
        ({"autre_outil": True}, False),
        ({}, False),
    ],
)
async def test_le_marqueur_seul_suffit_a_rendre_purgeable(
    hass: HomeAssistant,
    entree: MockConfigEntry,
    metadonnees: dict[str, Any],
    purgeable: bool,
) -> None:
    """Hors registre, seul le marqueur du fork autorise une suppression."""
    destination = _destination(hass, "destination_test")
    await _deposer(
        hass,
        destination,
        "hors_registre",
        jours=365,
        inscrire=False,
        metadata=metadonnees,
    )

    supprimes = await _coordinateur(hass).async_purger_toutes()

    assert bool(supprimes) is purgeable
    assert (destination.sauvegardes == {}) is purgeable


async def test_une_purge_hors_registre_laisse_le_registre_intact(
    hass: HomeAssistant, entree: MockConfigEntry
) -> None:
    """Supprimer une sauvegarde marquée mais non inscrite ne touche à rien."""
    destination = _destination(hass, "destination_test")
    await _deposer(hass, destination, "inscrite", jours=1)
    await _deposer(
        hass,
        destination,
        "marquee",
        jours=365,
        inscrire=False,
        metadata=marqueur_auto_backup(),
    )

    assert await _coordinateur(hass).async_purger_toutes() == {
        "destination_test": ["marquee"]
    }
    assert _inscrits(hass) == ["inscrite"]


def test_le_marqueur_est_celui_que_les_fournisseurs_posent() -> None:
    """`marqueur_auto_backup()` produit bien des métadonnées reconnues."""
    sauvegarde = RemoteBackup(remote_id="x", name="x", metadata=marqueur_auto_backup())

    assert porte_le_marqueur(sauvegarde)
    assert not porte_le_marqueur(RemoteBackup(remote_id="x", name="x"))
    assert not porte_le_marqueur(
        RemoteBackup(remote_id="x", name="x", metadata={CLE_MARQUEUR: 0})
    )


### TOLÉRANCE AUX ERREURS ###


async def test_une_erreur_de_suppression_n_interrompt_pas_la_purge(
    hass: HomeAssistant, entree: MockConfigEntry, caplog: pytest.LogCaptureFixture
) -> None:
    """Un échec de suppression est journalisé, les suivantes sont tentées."""
    destination = _destination(hass, "destination_test")
    await _deposer(hass, destination, "recalcitrante", jours=100)
    await _deposer(hass, destination, "docile", jours=99)
    destination.erreurs_de_suppression["recalcitrante"] = DestinationError(
        "le fournisseur refuse"
    )

    with caplog.at_level(logging.ERROR):
        supprimes = await _coordinateur(hass).async_purger_toutes()

    assert supprimes == {"destination_test": ["docile"]}
    assert destination.suppressions == ["recalcitrante", "docile"]
    # Le fichier est toujours là : son entrée reste au registre pour être
    # retentée à la purge suivante.
    assert _inscrits(hass) == ["recalcitrante"]
    assert "le fournisseur refuse" in caplog.text


async def test_une_sauvegarde_deja_absente_est_retiree_du_registre(
    hass: HomeAssistant, entree: MockConfigEntry, caplog: pytest.LogCaptureFixture
) -> None:
    """Un fichier disparu entre le listage et la suppression n'est pas un échec."""
    destination = _destination(hass, "destination_test")
    await _deposer(hass, destination, "envolee", jours=100)
    await _deposer(hass, destination, "presente", jours=99)
    destination.erreurs_de_suppression["envolee"] = DestinationNotFoundError(
        "sauvegarde distante inconnue"
    )

    with caplog.at_level(logging.WARNING):
        supprimes = await _coordinateur(hass).async_purger_toutes()

    assert supprimes == {"destination_test": ["envolee", "presente"]}
    assert _registre(hass).entrees("destination_test") == []
    assert "déjà absente" in caplog.text


async def test_une_purge_entierement_en_echec_n_emet_aucun_evenement(
    hass: HomeAssistant, entree: MockConfigEntry
) -> None:
    """Rien n'a disparu chez le fournisseur : rien n'est annoncé."""
    evenements = async_capture_events(hass, EVENT_REMOTE_PURGE)
    destination = _destination(hass, "destination_test")
    await _deposer(hass, destination, "vieille", jours=100)
    destination.erreurs_de_suppression["vieille"] = DestinationError("refus")

    assert await _coordinateur(hass).async_purger_toutes() == {}
    assert evenements == []
    assert _inscrits(hass) == ["vieille"]


@pytest.mark.parametrize("phase", ["listage", "suppression"])
async def test_une_erreur_inattendue_est_journalisee_sans_tout_arreter(
    hass: HomeAssistant,
    entree: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
    phase: str,
) -> None:
    """Une erreur non typée d'un fournisseur ne remonte pas jusqu'à l'appelant."""
    destination = _destination(hass, "destination_test")
    await _deposer(hass, destination, "vieille", jours=100)
    panne = RuntimeError("le fournisseur a paniqué")
    if phase == "listage":
        destination.erreur_a_lever = panne
    else:
        destination.erreurs_de_suppression["vieille"] = panne

    with caplog.at_level(logging.ERROR):
        assert await _coordinateur(hass).async_purger_toutes() == {}

    assert "le fournisseur a paniqué" in caplog.text
    assert _inscrits(hass) == ["vieille"]


async def test_un_listage_impossible_n_empeche_pas_les_autres_destinations(
    hass: HomeAssistant,
    integration_backup: None,
    fournisseur_factice: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Une destination injoignable est abandonnée, pas les suivantes."""
    await _demarrer(
        hass,
        destinations=[
            config_factice(destination_id="muette", name="Muette"),
            config_factice(destination_id="bavarde", name="Bavarde"),
        ],
    )
    muette = _destination(hass, "muette")
    bavarde = _destination(hass, "bavarde")
    await _deposer(hass, muette, "jamais_vue", jours=100)
    await _deposer(hass, bavarde, "vieille", jours=100)
    muette.erreur_a_lever = DestinationError("service indisponible")

    with caplog.at_level(logging.ERROR):
        supprimes = await _coordinateur(hass).async_purger_toutes()

    assert supprimes == {"bavarde": ["vieille"]}
    assert set(muette.sauvegardes) == {"jamais_vue"}
    assert "service indisponible" in caplog.text


async def test_un_listage_qui_ne_revient_pas_est_coupe_par_le_delai(
    hass: HomeAssistant,
    integration_backup: None,
    fournisseur_factice: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Un fournisseur muet au listage est sauté, pas les destinations suivantes.

    Sans le filet de sécurité, l'appel pendrait indéfiniment : la purge de
    cette destination ne se terminerait jamais, et le verrou qui sérialise les
    purges resterait pris.
    """
    await _demarrer(
        hass,
        destinations=[
            config_factice(destination_id="figee", name="Figée"),
            config_factice(destination_id="vive", name="Vive"),
        ],
    )
    figee = _destination(hass, "figee")
    vive = _destination(hass, "vive")
    await _deposer(hass, figee, "jamais_listee", jours=100)
    await _deposer(hass, vive, "vieille", jours=100)
    figee.attente_de_listage = 30

    with caplog.at_level(logging.ERROR), _delai_de_purge(0.01):
        supprimes = await _coordinateur(hass).async_purger_toutes()

    # La destination figée est abandonnée sans rien supprimer ; la suivante est
    # purgée normalement.
    assert supprimes == {"vive": ["vieille"]}
    assert figee.suppressions == []
    assert set(figee.sauvegardes) == {"jamais_listee"}
    assert _inscrits(hass, "figee") == ["jamais_listee"]
    assert "délai de listage dépassé" in caplog.text


async def test_une_suppression_qui_ne_revient_pas_est_coupee_par_le_delai(
    hass: HomeAssistant, entree: MockConfigEntry, caplog: pytest.LogCaptureFixture
) -> None:
    """Une suppression qui pend est abandonnée, les suivantes sont tentées."""
    destination = _destination(hass, "destination_test")
    await _deposer(hass, destination, "figee", jours=100)
    await _deposer(hass, destination, "docile", jours=99)
    destination.attentes_de_suppression["figee"] = 30

    with caplog.at_level(logging.ERROR), _delai_de_purge(0.01):
        supprimes = await _coordinateur(hass).async_purger_toutes()

    assert supprimes == {"destination_test": ["docile"]}
    assert destination.suppressions == ["figee", "docile"]
    # Le fichier est peut-être toujours là : son entrée reste au registre pour
    # être retentée à la purge suivante.
    assert _inscrits(hass) == ["figee"]
    assert set(destination.sauvegardes) == {"figee"}
    assert "délai dépassé" in caplog.text


async def test_une_destination_a_reautoriser_n_est_pas_jointe(
    hass: HomeAssistant, entree: MockConfigEntry, caplog: pytest.LogCaptureFixture
) -> None:
    """Une destination en attente d'autorisation est sautée sans appel."""
    destination = _destination(hass, "destination_test")
    await _deposer(hass, destination, "vieille", jours=100)
    hass.data[DATA_DESTINATIONS].async_marquer_la_reauthentification("destination_test")

    with caplog.at_level(logging.WARNING):
        supprimes = await _coordinateur(hass).async_purger_toutes()

    assert supprimes == {}
    assert destination.listages == 0
    assert destination.suppressions == []
    assert _inscrits(hass) == ["vieille"]
    assert "ré-authentification requise" in caplog.text


async def test_sans_gestionnaire_de_destinations_la_purge_ne_leve_pas(
    hass: HomeAssistant, entree: MockConfigEntry
) -> None:
    """Un déchargement en cours de route laisse la purge sans rien à faire."""
    coordinateur = _coordinateur(hass)
    hass.data.pop(DATA_DESTINATIONS)

    assert await coordinateur.async_purger_toutes() == {}
    assert await coordinateur.async_purger_destination("destination_test") == []


async def test_une_destination_supprimee_entre_temps_est_ignoree(
    hass: HomeAssistant, entree: MockConfigEntry
) -> None:
    """Purger une destination disparue ne lève pas."""
    assert await _coordinateur(hass).async_purger_destination("jamais_creee") == []


### ÉVÉNEMENT ET REGISTRE ###


async def test_l_evenement_de_purge_distante_est_emis(
    hass: HomeAssistant, entree: MockConfigEntry
) -> None:
    """`auto_backup.remote_purge` nomme la destination et les identifiants."""
    evenements = async_capture_events(hass, EVENT_REMOTE_PURGE)
    destination = _destination(hass, "destination_test")
    await _deposer(hass, destination, "vieille", jours=100)

    await _coordinateur(hass).async_purger_toutes()

    assert len(evenements) == 1
    assert evenements[0].data == {
        ATTR_DESTINATION: "destination_test",
        ATTR_DESTINATION_NAME: "Destination de test",
        ATTR_REMOTE_IDS: ["vieille"],
    }


async def test_aucun_evenement_quand_rien_n_est_supprime(
    hass: HomeAssistant, entree: MockConfigEntry
) -> None:
    """Une purge sans suppression reste silencieuse."""
    evenements = async_capture_events(hass, EVENT_REMOTE_PURGE)
    destination = _destination(hass, "destination_test")
    await _deposer(hass, destination, "recente", jours=1)

    await _coordinateur(hass).async_purger_toutes()

    assert evenements == []


async def test_le_registre_survit_au_redemarrage(
    hass: HomeAssistant, entree: MockConfigEntry, hass_storage: dict[str, Any]
) -> None:
    """Les entrées du registre sont persistées puis relues au rechargement."""
    destination = _destination(hass, "destination_test")
    await _deposer(hass, destination, "conservee", jours=1, slug=SLUG, size=42)

    assert hass_storage[CLE_STOCKAGE]["data"]["destination_test"][0] == {
        ATTR_REMOTE_ID: "conservee",
        ATTR_NAME: "conservee",
        ATTR_SLUG: SLUG,
        "created_at": hass_storage[CLE_STOCKAGE]["data"]["destination_test"][0][
            "created_at"
        ],
        ATTR_SIZE: 42,
    }

    assert await hass.config_entries.async_reload(entree.entry_id)
    await hass.async_block_till_done()

    relue = _registre(hass).entree("destination_test", "conservee")
    assert relue is not None
    assert relue.slug == SLUG
    assert relue.size == 42
    assert relue.created_at is not None


async def test_un_registre_illisible_ne_bloque_pas_le_demarrage(
    hass: HomeAssistant,
    integration_backup: None,
    fournisseur_factice: str,
    hass_storage: dict[str, Any],
) -> None:
    """Un fichier de stockage abîmé est ignoré, entrée par entrée."""
    hass_storage[CLE_STOCKAGE] = {
        "version": 1,
        "data": {
            "destination_test": [
                {
                    "remote_id": "valable",
                    "name": "Valable",
                    "created_at": "pas_une_date",
                },
                {"name": "sans identifiant distant"},
                "pas un dictionnaire",
            ],
            "autre": "pas une liste",
        },
    }

    await _demarrer(hass)

    entrees = _registre(hass).entrees("destination_test")
    assert _inscrits(hass) == ["valable"]
    assert entrees[0].created_at is None
    assert _registre(hass).entrees("autre") == []


async def test_un_registre_qui_n_est_pas_un_dictionnaire_est_ignore(
    hass: HomeAssistant,
    integration_backup: None,
    fournisseur_factice: str,
    hass_storage: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Un stockage dont la racine n'est pas un dictionnaire repart à vide."""
    hass_storage[CLE_STOCKAGE] = {"version": 1, "data": ["pas un dictionnaire"]}

    with caplog.at_level(logging.WARNING):
        await _demarrer(hass)

    assert _inscrits(hass) == []
    assert "Registre des sauvegardes distantes illisible" in caplog.text


def test_une_entree_de_registre_sans_identifiant_est_rejetee() -> None:
    """`EntreeRegistre.from_dict()` refuse ce qui n'a pas de `remote_id`."""
    assert EntreeRegistre.from_dict({"name": "x"}) is None
    assert EntreeRegistre.from_dict({"remote_id": "   "}) is None

    entree = EntreeRegistre.from_dict({"remote_id": "x", "size": -1, "slug": ""})
    assert entree is not None
    assert entree.name == "x"
    assert entree.slug is None
    assert entree.size is None


### DÉCLENCHEMENTS : TÉLÉVERSEMENT ET SERVICE ###


async def test_le_televersement_inscrit_la_sauvegarde_puis_purge(
    hass: HomeAssistant,
    integration_backup: None,
    fournisseur_factice: str,
    fichier_de_sauvegarde: Path,
) -> None:
    """`auto_purge` actif : chaque téléversement réussi purge la destination."""
    await _demarrer(hass, fichier=fichier_de_sauvegarde)
    destination = _destination(hass, "destination_test")
    await _deposer(hass, destination, "vieille", jours=100)
    evenements = async_capture_events(hass, EVENT_REMOTE_PURGE)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_BACKUP,
        {ATTR_UPLOAD_TO: "destination_test"},
        blocking=True,
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    # La sauvegarde téléversée est inscrite au registre, l'ancienne a disparu.
    assert _inscrits(hass) == ["destination_test-1"]
    assert set(destination.sauvegardes) == {"destination_test-1"}
    assert [evenement.data[ATTR_REMOTE_IDS] for evenement in evenements] == [
        ["vieille"]
    ]


async def test_la_purge_automatique_desactivee_ne_purge_pas(
    hass: HomeAssistant,
    integration_backup: None,
    fournisseur_factice: str,
    fichier_de_sauvegarde: Path,
) -> None:
    """`auto_purge` désactivé : le registre est tenu, rien n'est supprimé."""
    await _demarrer(
        hass, options={CONF_AUTO_PURGE: False}, fichier=fichier_de_sauvegarde
    )
    destination = _destination(hass, "destination_test")
    await _deposer(hass, destination, "vieille", jours=100)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_BACKUP,
        {ATTR_UPLOAD_TO: "destination_test"},
        blocking=True,
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    assert destination.suppressions == []
    assert set(destination.sauvegardes) == {"vieille", "destination_test-1"}
    assert _inscrits(hass) == ["vieille", "destination_test-1"]


async def test_un_evenement_de_televersement_incomplet_est_ignore(
    hass: HomeAssistant, entree: MockConfigEntry
) -> None:
    """Un événement sans destination ni identifiant distant ne casse rien."""
    hass.bus.async_fire(EVENT_UPLOAD_SUCCESSFUL, {ATTR_NAME: "Sauvegarde"})
    await hass.async_block_till_done()

    assert _registre(hass).entrees("destination_test") == []


async def test_le_service_purge_purge_le_local_et_le_distant(
    hass: HomeAssistant, entree: MockConfigEntry
) -> None:
    """`auto_backup.purge` enchaîne la purge upstream et la purge distante."""
    gestionnaire = hass.data[DATA_AUTO_BACKUP]
    gestionnaire._snapshots = {"locale": dt_util.utcnow() - timedelta(days=1)}
    gestionnaire._handler.remove_backup = AsyncMock()
    purges_locales = async_capture_events(hass, EVENT_BACKUPS_PURGED)

    destination = _destination(hass, "destination_test")
    await _deposer(hass, destination, "distante", jours=100)

    await hass.services.async_call(DOMAIN, SERVICE_PURGE, blocking=True)
    await hass.async_block_till_done()

    gestionnaire._handler.remove_backup.assert_awaited_once_with("locale")
    assert [evenement.data["backups"] for evenement in purges_locales] == [["locale"]]
    assert destination.sauvegardes == {}


async def test_le_service_purge_reste_disponible_apres_rechargement(
    hass: HomeAssistant, entree: MockConfigEntry
) -> None:
    """Le service enveloppé est retiré au déchargement et remis au rechargement."""
    assert await hass.config_entries.async_unload(entree.entry_id)
    await hass.async_block_till_done()
    assert not hass.services.has_service(DOMAIN, SERVICE_PURGE)
    assert DATA_REMOTE_PURGE not in hass.data

    assert await hass.config_entries.async_setup(entree.entry_id)
    await hass.async_block_till_done()

    destination = _destination(hass, "destination_test")
    await _deposer(hass, destination, "vieille", jours=100)
    await hass.services.async_call(DOMAIN, SERVICE_PURGE, blocking=True)
    await hass.async_block_till_done()

    assert destination.sauvegardes == {}
