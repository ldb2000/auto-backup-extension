"""Téléversement d'une sauvegarde après sa création (issue #8).

Deux niveaux sont éprouvés :

- la **lecture en flux** d'une sauvegarde locale, pour les deux handlers
  upstream : `SupervisorHandler` (téléchargement HTTP simulé par
  `aioclient_mock`) et `BackupHandler` (fichier temporaire lu par `aiofiles`) ;
- l'**orchestration** complète, depuis l'appel de service jusqu'aux événements
  `auto_backup.upload_*`, avec le fournisseur factice de
  `tests/destinations_factices.py`.

Aucun test ne touche un fournisseur réel ni le réseau.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from homeassistant.const import ATTR_NAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.auto_backup.const import (
    ATTR_DESTINATION,
    ATTR_DESTINATION_NAME,
    ATTR_ERROR,
    ATTR_EXCLUDE,
    ATTR_SIZE,
    ATTR_SLUG,
    ATTR_UPLOAD_TO,
    CONF_AUTO_PURGE,
    CONF_BACKUP_TIMEOUT,
    CONF_DESTINATIONS,
    CONF_UPLOAD_TIMEOUT,
    DATA_AUTO_BACKUP,
    DATA_DESTINATIONS,
    DATA_UPLOADS,
    DEFAULT_UPLOAD_TIMEOUT,
    DOMAIN,
    EVENT_BACKUP_FAILED,
    EVENT_BACKUP_START,
    EVENT_BACKUP_SUCCESSFUL,
    EVENT_UPLOAD_FAILED,
    EVENT_UPLOAD_START,
    EVENT_UPLOAD_SUCCESSFUL,
    SERVICE_BACKUP,
    SERVICE_BACKUP_FULL,
    SERVICE_BACKUP_PARTIAL,
)
from custom_components.auto_backup.destinations import (
    DestinationConfigError,
    DestinationError,
)
from custom_components.auto_backup.destinations.flow import _delai_de_televersement
from custom_components.auto_backup.destinations.upload import (
    DELAI_CONFIRMATION_DEMANDE,
    ErreurLectureSauvegarde,
    async_ouvrir_sauvegarde,
    async_prepare_upload,
    async_release_upload,
    async_resoudre_destinations,
    nom_de_fichier_sauvegarde,
)
from custom_components.auto_backup.handlers import (
    BackupHandler,
    HandlerBase,
    HassioAPIError,
    SupervisorHandler,
)
from destinations_factices import DestinationEnMemoire, config_factice

# Signature de la fixture `ouvrir_les_options` (cf. `tests/conftest.py`).
type OuvrirLesOptions = Callable[[str, str], Awaitable[dict[str, Any]]]

# Plus grand qu'un morceau de lecture (64 Kio) : la lecture doit rendre
# plusieurs morceaux, preuve qu'elle n'avale pas le fichier d'un bloc.
CONTENU_SAUVEGARDE = b"auto-backup" * 20_000

SLUG = "abc123"
ADRESSE_SUPERVISOR = "supervisor"
URL_TELECHARGEMENT = f"http://{ADRESSE_SUPERVISOR}/backups/{SLUG}/download"


def _faux_backup_manager(chemin: Path | None) -> MagicMock:
    """`BackupManager` minimal : une sauvegarde locale et son agent."""
    manager = MagicMock()
    if chemin is None:
        manager.async_get_backup = AsyncMock(return_value=(None, {}))
        manager.local_backup_agents = {}
        return manager

    sauvegarde = MagicMock()
    sauvegarde.backup_id = SLUG
    agent = MagicMock()
    agent.get_backup_path = MagicMock(return_value=chemin)
    manager.async_get_backup = AsyncMock(return_value=(sauvegarde, {}))
    manager.local_backup_agents = {"backup.local": agent}
    return manager


@pytest.fixture
def fichier_de_sauvegarde(tmp_path: Path) -> Path:
    """Fichier `.tar` local tenant lieu de sauvegarde Home Assistant."""
    chemin = tmp_path / f"{SLUG}.tar"
    chemin.write_bytes(CONTENU_SAUVEGARDE)
    return chemin


class _Instance:
    """Intégration démarrée, prête à créer puis téléverser une sauvegarde."""

    def __init__(
        self, hass: HomeAssistant, entree: MockConfigEntry, creation: AsyncMock
    ) -> None:
        self.hass = hass
        self.entree = entree
        self.creation = creation

    def destination(self, destination_id: str) -> DestinationEnMemoire:
        """Instance factice chargée pour cet identifiant."""
        return self.hass.data[DATA_DESTINATIONS].async_get(destination_id)

    @property
    def donnees_de_creation(self) -> dict[str, Any]:
        """Données transmises au handler de création de sauvegarde."""
        return self.creation.await_args.args[0]


async def _demarrer(
    hass: HomeAssistant,
    fichier: Path | None,
    *,
    destinations: list[dict[str, Any]] | None = None,
    options: dict[str, Any] | None = None,
) -> _Instance:
    """Initialise l'intégration avec des destinations et un handler simulé."""
    entree = MockConfigEntry(
        domain=DOMAIN,
        title="Auto Backup",
        data={},
        options={
            CONF_DESTINATIONS: (
                [config_factice()] if destinations is None else destinations
            ),
            **(options or {}),
        },
    )
    entree.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entree.entry_id)
    await hass.async_block_till_done()

    handler = hass.data[DATA_AUTO_BACKUP]._handler
    handler._manager = _faux_backup_manager(fichier)
    creation = AsyncMock(return_value={"slug": SLUG})
    handler.create_backup = creation
    return _Instance(hass, entree, creation)


@pytest.fixture
async def instance(
    hass: HomeAssistant,
    integration_backup: None,
    fournisseur_factice: str,
    fichier_de_sauvegarde: Path,
) -> _Instance:
    """Intégration démarrée avec une destination factice unique."""
    return await _demarrer(hass, fichier_de_sauvegarde)


### LECTURE EN FLUX D'UNE SAUVEGARDE ###


@pytest.mark.parametrize(
    ("nom", "slug", "attendu"),
    [
        ("Sauvegarde du 22", SLUG, "Sauvegarde_du_22.tar"),
        # `slugify` remplace le point, exactement comme pour `download_path` :
        # le suffixe `.tar` est donc bien ajouté une fois et une seule.
        ("deja.tar", SLUG, "deja_tar.tar"),
        (None, SLUG, f"{SLUG}.tar"),
        ("", SLUG, f"{SLUG}.tar"),
    ],
)
def test_le_nom_de_fichier_suit_la_convention_upstream(
    nom: str | None, slug: str, attendu: str
) -> None:
    """Le nom d'archive est celui qu'utiliserait `download_path`."""
    assert nom_de_fichier_sauvegarde(nom, slug) == attendu


async def test_le_handler_supervisor_fournit_un_flux_et_une_taille(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Sous Supervisor, la sauvegarde est lue en streaming HTTP."""
    aioclient_mock.get(
        URL_TELECHARGEMENT,
        content=CONTENU_SAUVEGARDE,
        headers={"Content-Length": str(len(CONTENU_SAUVEGARDE))},
    )
    handler = SupervisorHandler(ADRESSE_SUPERVISOR, async_get_clientsession(hass))

    async with async_ouvrir_sauvegarde(
        hass, handler, SLUG, nom="Sauvegarde du 22"
    ) as contenu:
        assert contenu.taille == len(CONTENU_SAUVEGARDE)
        assert contenu.nom_fichier == "Sauvegarde_du_22.tar"
        assert contenu.chemin is None
        morceaux = [morceau async for morceau in contenu.flux]

    assert b"".join(morceaux) == CONTENU_SAUVEGARDE
    assert len(morceaux) > 1, "la sauvegarde doit arriver en plusieurs morceaux"


async def test_le_handler_supervisor_tolere_une_taille_non_annoncee(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Sans en-tête `Content-Length`, la taille est inconnue mais le flux passe."""
    aioclient_mock.get(URL_TELECHARGEMENT, content=CONTENU_SAUVEGARDE)
    handler = SupervisorHandler(ADRESSE_SUPERVISOR, async_get_clientsession(hass))

    async with async_ouvrir_sauvegarde(hass, handler, SLUG) as contenu:
        assert contenu.taille is None
        morceaux = [morceau async for morceau in contenu.flux]

    assert b"".join(morceaux) == CONTENU_SAUVEGARDE


async def test_le_handler_supervisor_signale_un_code_d_erreur(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Un code HTTP inattendu lève `ErreurLectureSauvegarde`."""
    aioclient_mock.get(URL_TELECHARGEMENT, status=404)
    handler = SupervisorHandler(ADRESSE_SUPERVISOR, async_get_clientsession(hass))

    with pytest.raises(ErreurLectureSauvegarde, match="404"):
        async with async_ouvrir_sauvegarde(hass, handler, SLUG):
            pass


async def test_le_handler_supervisor_signale_une_erreur_reseau(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Une erreur de transport lève `ErreurLectureSauvegarde`."""
    aioclient_mock.get(URL_TELECHARGEMENT, exc=aiohttp.ClientError("connexion perdue"))
    handler = SupervisorHandler(ADRESSE_SUPERVISOR, async_get_clientsession(hass))

    with pytest.raises(ErreurLectureSauvegarde, match="connexion perdue"):
        async with async_ouvrir_sauvegarde(hass, handler, SLUG):
            pass


async def test_le_handler_core_fournit_un_flux_et_une_taille(
    hass: HomeAssistant, fichier_de_sauvegarde: Path
) -> None:
    """Sur Home Assistant Core, le fichier de l'agent local est lu par morceaux."""
    handler = BackupHandler(hass, _faux_backup_manager(fichier_de_sauvegarde))

    async with async_ouvrir_sauvegarde(hass, handler, SLUG, nom="Core 2026") as contenu:
        assert contenu.taille == len(CONTENU_SAUVEGARDE)
        assert contenu.nom_fichier == "Core_2026.tar"
        assert contenu.chemin == fichier_de_sauvegarde
        morceaux = [morceau async for morceau in contenu.flux]

    assert b"".join(morceaux) == CONTENU_SAUVEGARDE
    assert len(morceaux) > 1, "la sauvegarde doit arriver en plusieurs morceaux"


async def test_le_handler_core_signale_une_sauvegarde_absente(
    hass: HomeAssistant,
) -> None:
    """Une sauvegarde inconnue du `BackupManager` lève une erreur explicite."""
    handler = BackupHandler(hass, _faux_backup_manager(None))

    with pytest.raises(ErreurLectureSauvegarde, match="introuvable"):
        async with async_ouvrir_sauvegarde(hass, handler, SLUG):
            pass


async def test_le_handler_core_signale_un_fichier_illisible(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """Un fichier de sauvegarde disparu lève `ErreurLectureSauvegarde`."""
    handler = BackupHandler(hass, _faux_backup_manager(tmp_path / "absent.tar"))

    with pytest.raises(ErreurLectureSauvegarde, match="impossible"):
        async with async_ouvrir_sauvegarde(hass, handler, SLUG):
            pass


async def test_un_handler_inconnu_est_refuse(hass: HomeAssistant) -> None:
    """Un handler hors des deux handlers upstream n'est pas exploitable."""
    with pytest.raises(ErreurLectureSauvegarde, match="handler"):
        async with async_ouvrir_sauvegarde(hass, HandlerBase(), SLUG):
            pass


### RÉSOLUTION DES DESTINATIONS DEMANDÉES ###


async def test_une_destination_se_designe_par_identifiant_ou_par_nom(
    hass: HomeAssistant, instance: _Instance
) -> None:
    """`upload_to` accepte l'identifiant comme le nom de la destination."""
    assert async_resoudre_destinations(hass, ["destination_test"]) == (
        "destination_test",
    )
    assert async_resoudre_destinations(hass, ["Destination de test"]) == (
        "destination_test",
    )
    # Le nom est comparé sans tenir compte de la casse ni des espaces de bordure.
    assert async_resoudre_destinations(hass, ["  destination DE TEST "]) == (
        "destination_test",
    )


async def test_les_destinations_demandees_sont_dedoublonnees(
    hass: HomeAssistant, instance: _Instance
) -> None:
    """Demander deux fois la même destination ne la téléverse qu'une fois."""
    assert async_resoudre_destinations(
        hass, ["destination_test", "Destination de test"]
    ) == ("destination_test",)


async def test_un_nom_porte_par_plusieurs_destinations_est_refuse(
    hass: HomeAssistant,
    integration_backup: None,
    fournisseur_factice: str,
    fichier_de_sauvegarde: Path,
) -> None:
    """Un nom ambigu lève une erreur qui invite à préciser l'identifiant."""
    await _demarrer(
        hass,
        fichier_de_sauvegarde,
        destinations=[
            config_factice(),
            config_factice(destination_id="destination_bis"),
        ],
    )

    with pytest.raises(ServiceValidationError, match="Plusieurs destinations"):
        async_resoudre_destinations(hass, ["Destination de test"])


### APPEL DE SERVICE ###


async def test_le_televersement_emet_le_debut_puis_le_succes(
    hass: HomeAssistant, instance: _Instance
) -> None:
    """Le cas nominal téléverse la sauvegarde et émet les deux événements."""
    debuts = async_capture_events(hass, EVENT_UPLOAD_START)
    succes = async_capture_events(hass, EVENT_UPLOAD_SUCCESSFUL)
    echecs = async_capture_events(hass, EVENT_UPLOAD_FAILED)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_BACKUP,
        {ATTR_NAME: "Sauvegarde du 22", ATTR_UPLOAD_TO: "destination_test"},
        blocking=True,
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    assert not echecs
    assert len(debuts) == 1
    assert debuts[0].data == {
        ATTR_NAME: "Sauvegarde du 22",
        ATTR_SLUG: SLUG,
        ATTR_DESTINATION: "destination_test",
        ATTR_DESTINATION_NAME: "Destination de test",
    }

    assert len(succes) == 1
    assert succes[0].data[ATTR_NAME] == "Sauvegarde du 22"
    assert succes[0].data[ATTR_SLUG] == SLUG
    assert succes[0].data[ATTR_DESTINATION] == "destination_test"
    assert succes[0].data[ATTR_SIZE] == len(CONTENU_SAUVEGARDE)

    destination = instance.destination("destination_test")
    assert destination.octets_recus == CONTENU_SAUVEGARDE
    assert destination.taille_annoncee == len(CONTENU_SAUVEGARDE)
    sauvegarde = next(iter(destination.sauvegardes.values()))
    assert sauvegarde.slug == SLUG
    assert sauvegarde.path == "Sauvegardes/Sauvegarde_du_22.tar"


async def test_l_option_upload_to_ne_part_jamais_vers_le_handler(
    hass: HomeAssistant, instance: _Instance
) -> None:
    """`upload_to` est retiré des données avant la création de la sauvegarde."""
    await hass.services.async_call(
        DOMAIN,
        SERVICE_BACKUP,
        {ATTR_UPLOAD_TO: ["destination_test"]},
        blocking=True,
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    assert ATTR_UPLOAD_TO not in instance.donnees_de_creation


async def test_une_sauvegarde_sans_nom_est_correlee_par_le_nom_genere(
    hass: HomeAssistant, instance: _Instance
) -> None:
    """Sans nom explicite, celui que l'upstream aurait généré est utilisé."""
    succes = async_capture_events(hass, EVENT_UPLOAD_SUCCESSFUL)
    attendu = hass.data[DATA_AUTO_BACKUP].generate_backup_name()

    await hass.services.async_call(
        DOMAIN, SERVICE_BACKUP, {ATTR_UPLOAD_TO: ["destination_test"]}, blocking=True
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    assert instance.donnees_de_creation[ATTR_NAME] == attendu
    assert len(succes) == 1
    assert succes[0].data[ATTR_NAME] == attendu


@pytest.mark.parametrize(
    ("service", "donnees"),
    [
        (SERVICE_BACKUP, {}),
        (SERVICE_BACKUP_FULL, {}),
        (SERVICE_BACKUP_PARTIAL, {"folders": ["config"]}),
    ],
)
async def test_les_trois_services_de_sauvegarde_acceptent_upload_to(
    hass: HomeAssistant,
    instance: _Instance,
    service: str,
    donnees: dict[str, Any],
) -> None:
    """`upload_to` est accepté par `backup`, `backup_full` et `backup_partial`."""
    succes = async_capture_events(hass, EVENT_UPLOAD_SUCCESSFUL)

    await hass.services.async_call(
        DOMAIN,
        service,
        {**donnees, ATTR_UPLOAD_TO: "destination_test"},
        blocking=True,
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    assert len(succes) == 1


async def test_une_destination_inconnue_bloque_avant_la_creation(
    hass: HomeAssistant, instance: _Instance
) -> None:
    """Une destination inconnue lève une erreur française, sans rien créer."""
    debuts = async_capture_events(hass, EVENT_UPLOAD_START)

    with pytest.raises(ServiceValidationError) as erreur:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_BACKUP,
            {ATTR_UPLOAD_TO: ["dropbox_perso"]},
            blocking=True,
        )

    assert "Destination inconnue" in str(erreur.value)
    assert "dropbox_perso" in str(erreur.value)
    # Le message oriente l'utilisateur vers ce qui est réellement configuré.
    assert "Destination de test" in str(erreur.value)

    instance.creation.assert_not_awaited()
    assert not debuts


async def test_sans_destination_configuree_le_message_le_dit(
    hass: HomeAssistant,
    integration_backup: None,
    fournisseur_factice: str,
    fichier_de_sauvegarde: Path,
) -> None:
    """Sans aucune destination, l'erreur le signale explicitement."""
    instance = await _demarrer(hass, fichier_de_sauvegarde, destinations=[])

    with pytest.raises(ServiceValidationError, match="Aucune destination"):
        await hass.services.async_call(
            DOMAIN, SERVICE_BACKUP, {ATTR_UPLOAD_TO: "dropbox"}, blocking=True
        )

    instance.creation.assert_not_awaited()


async def test_sans_upload_to_le_comportement_reste_celui_de_l_upstream(
    hass: HomeAssistant, instance: _Instance
) -> None:
    """Sans `upload_to`, aucun événement de téléversement n'est émis."""
    debuts = async_capture_events(hass, EVENT_UPLOAD_START)
    succes = async_capture_events(hass, EVENT_UPLOAD_SUCCESSFUL)
    echecs = async_capture_events(hass, EVENT_UPLOAD_FAILED)

    await hass.services.async_call(
        DOMAIN, SERVICE_BACKUP, {ATTR_NAME: "Sans destination"}, blocking=True
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    instance.creation.assert_awaited_once()
    assert instance.donnees_de_creation[ATTR_NAME] == "Sans destination"
    assert ATTR_UPLOAD_TO not in instance.donnees_de_creation
    assert not debuts and not succes and not echecs
    assert instance.destination("destination_test").sauvegardes == {}
    assert not hass.data[DATA_UPLOADS].demandes_en_attente


async def test_un_echec_de_televersement_laisse_la_sauvegarde_locale_intacte(
    hass: HomeAssistant,
    instance: _Instance,
    fichier_de_sauvegarde: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Un échec du fournisseur est signalé sans toucher à la sauvegarde locale."""
    echecs = async_capture_events(hass, EVENT_UPLOAD_FAILED)
    succes = async_capture_events(hass, EVENT_UPLOAD_SUCCESSFUL)
    instance.destination("destination_test").erreur_a_lever = DestinationError(
        "réseau indisponible"
    )

    with caplog.at_level("ERROR"):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_BACKUP,
            {ATTR_NAME: "Sauvegarde du 22", ATTR_UPLOAD_TO: "destination_test"},
            blocking=True,
        )
        await hass.async_block_till_done(wait_background_tasks=True)

    assert not succes
    assert len(echecs) == 1
    assert echecs[0].data[ATTR_SLUG] == SLUG
    assert echecs[0].data[ATTR_DESTINATION] == "destination_test"
    assert echecs[0].data[ATTR_ERROR] == "réseau indisponible"

    # L'échec est aussi journalisé, avec le slug et le message d'erreur.
    assert any(
        record.levelname == "ERROR"
        and SLUG in record.message
        and "réseau indisponible" in record.message
        for record in caplog.records
    )

    assert fichier_de_sauvegarde.read_bytes() == CONTENU_SAUVEGARDE
    instance.creation.assert_awaited_once()


async def test_l_echec_d_une_destination_n_empeche_pas_les_autres(
    hass: HomeAssistant,
    integration_backup: None,
    fournisseur_factice: str,
    fichier_de_sauvegarde: Path,
) -> None:
    """Les destinations demandées sont toutes traitées, même après un échec."""
    instance = await _demarrer(
        hass,
        fichier_de_sauvegarde,
        destinations=[
            config_factice(destination_id="en_panne", name="En panne"),
            config_factice(destination_id="fonctionnelle", name="Fonctionnelle"),
        ],
    )
    instance.destination("en_panne").erreur_a_lever = DestinationError("quota atteint")

    echecs = async_capture_events(hass, EVENT_UPLOAD_FAILED)
    succes = async_capture_events(hass, EVENT_UPLOAD_SUCCESSFUL)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_BACKUP,
        {ATTR_UPLOAD_TO: ["en_panne", "fonctionnelle"]},
        blocking=True,
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    assert [evenement.data[ATTR_DESTINATION] for evenement in echecs] == ["en_panne"]
    assert [evenement.data[ATTR_DESTINATION] for evenement in succes] == [
        "fonctionnelle"
    ]
    assert instance.destination("fonctionnelle").octets_recus == CONTENU_SAUVEGARDE


async def test_le_televersement_ne_bloque_pas_l_appel_de_service(
    hass: HomeAssistant,
    integration_backup: None,
    fournisseur_factice: str,
    fichier_de_sauvegarde: Path,
) -> None:
    """Le téléversement tourne en tâche de fond : l'appel de service revient
    sans attendre sa fin, même invoqué en mode bloquant.
    """
    instance = await _demarrer(hass, fichier_de_sauvegarde)
    instance.destination("destination_test").attente_secondes = 0.2

    succes = async_capture_events(hass, EVENT_UPLOAD_SUCCESSFUL)

    await hass.services.async_call(
        DOMAIN, SERVICE_BACKUP, {ATTR_UPLOAD_TO: "destination_test"}, blocking=True
    )

    # L'appel bloquant est revenu : la sauvegarde est créée, mais le
    # téléversement, lui, tourne encore en tâche de fond.
    assert not succes

    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(succes) == 1


async def test_un_televersement_trop_long_est_interrompu(
    hass: HomeAssistant,
    integration_backup: None,
    fournisseur_factice: str,
    fichier_de_sauvegarde: Path,
) -> None:
    """Le délai maximum des options coupe un téléversement qui s'éternise."""
    instance = await _demarrer(
        hass, fichier_de_sauvegarde, options={CONF_UPLOAD_TIMEOUT: 0.01}
    )
    instance.destination("destination_test").attente_secondes = 30

    echecs = async_capture_events(hass, EVENT_UPLOAD_FAILED)
    succes = async_capture_events(hass, EVENT_UPLOAD_SUCCESSFUL)

    await hass.services.async_call(
        DOMAIN, SERVICE_BACKUP, {ATTR_UPLOAD_TO: "destination_test"}, blocking=True
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    assert not succes
    assert len(echecs) == 1
    assert "délai" in echecs[0].data[ATTR_ERROR]
    assert fichier_de_sauvegarde.read_bytes() == CONTENU_SAUVEGARDE


async def test_une_sauvegarde_illisible_est_signalee_sans_televersement(
    hass: HomeAssistant,
    integration_backup: None,
    fournisseur_factice: str,
    tmp_path: Path,
) -> None:
    """Si la sauvegarde ne peut pas être lue, l'échec est émis proprement."""
    instance = await _demarrer(hass, tmp_path / "jamais_ecrit.tar")
    echecs = async_capture_events(hass, EVENT_UPLOAD_FAILED)

    await hass.services.async_call(
        DOMAIN, SERVICE_BACKUP, {ATTR_UPLOAD_TO: "destination_test"}, blocking=True
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    assert len(echecs) == 1
    assert "impossible" in echecs[0].data[ATTR_ERROR]
    assert instance.destination("destination_test").sauvegardes == {}


async def test_une_creation_en_echec_ne_laisse_pas_de_demande_en_attente(
    hass: HomeAssistant, instance: _Instance
) -> None:
    """Quand la sauvegarde échoue, la demande de téléversement est oubliée."""
    debuts = async_capture_events(hass, EVENT_UPLOAD_START)
    instance.creation.side_effect = HassioAPIError("sauvegarde déjà en cours")

    await hass.services.async_call(
        DOMAIN, SERVICE_BACKUP, {ATTR_UPLOAD_TO: "destination_test"}, blocking=True
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    assert not debuts
    assert not hass.data[DATA_UPLOADS].demandes_en_attente


async def test_une_destination_supprimee_entre_temps_est_signalee(
    hass: HomeAssistant, instance: _Instance
) -> None:
    """Une destination retirée pendant la création provoque un échec explicite."""
    echecs = async_capture_events(hass, EVENT_UPLOAD_FAILED)
    coordinateur = hass.data[DATA_UPLOADS]
    demande = coordinateur.async_enregistrer("Sauvegarde du 22", ("disparue",))
    assert demande in coordinateur.demandes_en_attente

    await hass.services.async_call(
        DOMAIN, SERVICE_BACKUP, {ATTR_NAME: "Sauvegarde du 22"}, blocking=True
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    assert len(echecs) == 1
    assert echecs[0].data[ATTR_DESTINATION] == "disparue"
    assert "introuvable" in echecs[0].data[ATTR_ERROR]


async def test_une_destination_a_reautoriser_n_est_pas_jointe(
    hass: HomeAssistant, instance: _Instance
) -> None:
    """Une destination en attente de ré-autorisation échoue sans appel réseau.

    Point d'intégration entre les issues #7 et #8 : le marquage porté par le
    gestionnaire est consulté **avant** d'ouvrir la sauvegarde. L'accès étant
    révoqué, joindre le fournisseur ne ferait qu'échouer plus tard, après avoir
    relu toute l'archive pour rien.
    """
    debuts = async_capture_events(hass, EVENT_UPLOAD_START)
    succes = async_capture_events(hass, EVENT_UPLOAD_SUCCESSFUL)
    echecs = async_capture_events(hass, EVENT_UPLOAD_FAILED)
    hass.data[DATA_DESTINATIONS].async_marquer_la_reauthentification("destination_test")
    destination = instance.destination("destination_test")

    await hass.services.async_call(
        DOMAIN,
        SERVICE_BACKUP,
        {ATTR_NAME: "Sauvegarde du 22", ATTR_UPLOAD_TO: "destination_test"},
        blocking=True,
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    assert not debuts, "aucun téléversement ne démarre vers une destination révoquée"
    assert not succes
    assert len(echecs) == 1
    assert echecs[0].data[ATTR_NAME] == "Sauvegarde du 22"
    assert echecs[0].data[ATTR_SLUG] == SLUG
    assert echecs[0].data[ATTR_DESTINATION] == "destination_test"
    assert echecs[0].data[ATTR_DESTINATION_NAME] == "Destination de test"
    assert "ré-authentification requise" in echecs[0].data[ATTR_ERROR]

    # Ni le fournisseur ni la sauvegarde locale n'ont été sollicités.
    assert not destination.sauvegardes
    assert destination.octets_recus is None
    hass.data[DATA_AUTO_BACKUP]._handler._manager.async_get_backup.assert_not_called()


async def test_une_destination_revoquee_n_empeche_pas_les_autres(
    hass: HomeAssistant,
    integration_backup: None,
    fournisseur_factice: str,
    fichier_de_sauvegarde: Path,
) -> None:
    """Le marquage ne vaut que pour la destination concernée."""
    instance = await _demarrer(
        hass,
        fichier_de_sauvegarde,
        destinations=[
            config_factice(),
            config_factice(destination_id="secondaire", name="Destination secondaire"),
        ],
    )
    succes = async_capture_events(hass, EVENT_UPLOAD_SUCCESSFUL)
    echecs = async_capture_events(hass, EVENT_UPLOAD_FAILED)
    hass.data[DATA_DESTINATIONS].async_marquer_la_reauthentification("destination_test")

    await hass.services.async_call(
        DOMAIN,
        SERVICE_BACKUP,
        {ATTR_UPLOAD_TO: ["destination_test", "secondaire"]},
        blocking=True,
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    assert len(echecs) == 1
    assert echecs[0].data[ATTR_DESTINATION] == "destination_test"
    assert len(succes) == 1
    assert succes[0].data[ATTR_DESTINATION] == "secondaire"
    assert instance.destination("secondaire").octets_recus == CONTENU_SAUVEGARDE


async def test_la_destination_resolue_au_depart_sert_jusqu_au_bout(
    hass: HomeAssistant, instance: _Instance
) -> None:
    """Un rechargement en cours d'opération ne fait pas changer d'instance.

    Point d'intégration entre les issues #7 et #8 : un rafraîchissement de jeton
    réécrit les options de l'entrée, ce qui recrée **toutes** les destinations.
    Le téléversement doit continuer sur la référence obtenue au début de
    l'opération ; la retrouver en route s'adresserait à un autre objet, dont
    la session OAuth2 n'est pas celle qui a ouvert le flux.

    Le rechargement est ici déclenché par l'ouverture de la sauvegarde, c'est-à-dire
    après la résolution de la destination et avant que le flux ne lui soit confié :
    exactement la fenêtre où une seconde résolution changerait d'objet.
    """
    succes = async_capture_events(hass, EVENT_UPLOAD_SUCCESSFUL)
    echecs = async_capture_events(hass, EVENT_UPLOAD_FAILED)
    gestionnaire = hass.data[DATA_DESTINATIONS]
    initiale = instance.destination("destination_test")

    backup_manager = hass.data[DATA_AUTO_BACKUP]._handler._manager
    sauvegarde_locale = backup_manager.async_get_backup.return_value

    async def recharger_puis_repondre(*_args: Any, **_kwargs: Any) -> Any:
        """Recrée les destinations au moment d'ouvrir la sauvegarde."""
        gestionnaire.async_load(instance.entree.options[CONF_DESTINATIONS])
        return sauvegarde_locale

    backup_manager.async_get_backup = AsyncMock(side_effect=recharger_puis_repondre)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_BACKUP,
        {ATTR_NAME: "Sauvegarde du 22", ATTR_UPLOAD_TO: "destination_test"},
        blocking=True,
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    remplacante = gestionnaire.async_get("destination_test")
    assert remplacante is not initiale, "le rechargement doit avoir recréé l'instance"

    assert not echecs
    assert len(succes) == 1
    assert succes[0].data[ATTR_DESTINATION] == "destination_test"
    assert succes[0].data[ATTR_SIZE] == len(CONTENU_SAUVEGARDE)

    # Le flux est arrivé en entier sur la destination du départ ; la nouvelle
    # instance, elle, n'a rien reçu.
    assert initiale.octets_recus == CONTENU_SAUVEGARDE
    assert len(initiale.sauvegardes) == 1
    assert not remplacante.sauvegardes
    assert remplacante.octets_recus is None


async def test_une_erreur_inattendue_du_fournisseur_est_capturee(
    hass: HomeAssistant, instance: _Instance
) -> None:
    """Une erreur non typée ne remonte pas jusqu'à Home Assistant."""
    echecs = async_capture_events(hass, EVENT_UPLOAD_FAILED)
    instance.destination("destination_test").erreur_a_lever = RuntimeError(
        "panne interne du fournisseur"
    )

    await hass.services.async_call(
        DOMAIN, SERVICE_BACKUP, {ATTR_UPLOAD_TO: "destination_test"}, blocking=True
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    assert len(echecs) == 1
    assert echecs[0].data[ATTR_ERROR] == "panne interne du fournisseur"


async def test_un_delai_illisible_retombe_sur_la_valeur_par_defaut(
    hass: HomeAssistant,
    integration_backup: None,
    fournisseur_factice: str,
    fichier_de_sauvegarde: Path,
) -> None:
    """Une option `upload_timeout` inexploitable n'empêche pas le téléversement."""
    await _demarrer(
        hass, fichier_de_sauvegarde, options={CONF_UPLOAD_TIMEOUT: "jamais"}
    )
    succes = async_capture_events(hass, EVENT_UPLOAD_SUCCESSFUL)

    await hass.services.async_call(
        DOMAIN, SERVICE_BACKUP, {ATTR_UPLOAD_TO: "destination_test"}, blocking=True
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    assert len(succes) == 1


async def test_sans_agent_local_la_sauvegarde_est_inaccessible(
    hass: HomeAssistant, fichier_de_sauvegarde: Path
) -> None:
    """Sans agent de sauvegarde local, le fichier ne peut pas être atteint."""
    manager = _faux_backup_manager(fichier_de_sauvegarde)
    manager.local_backup_agents = {}
    handler = BackupHandler(hass, manager)

    with pytest.raises(ErreurLectureSauvegarde, match="agent de sauvegarde local"):
        async with async_ouvrir_sauvegarde(hass, handler, SLUG):
            pass


async def test_une_taille_annoncee_illisible_est_ignoree(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Un `Content-Length` non numérique n'empêche pas la lecture du flux."""
    aioclient_mock.get(
        URL_TELECHARGEMENT,
        content=CONTENU_SAUVEGARDE,
        headers={"Content-Length": "inconnu"},
    )
    handler = SupervisorHandler(ADRESSE_SUPERVISOR, async_get_clientsession(hass))

    async with async_ouvrir_sauvegarde(hass, handler, SLUG) as contenu:
        assert contenu.taille is None
        morceaux = [morceau async for morceau in contenu.flux]

    assert b"".join(morceaux) == CONTENU_SAUVEGARDE


async def test_un_evenement_de_sauvegarde_sans_slug_est_ignore(
    hass: HomeAssistant, instance: _Instance
) -> None:
    """Sans slug, la sauvegarde n'est pas téléversable : l'échec est journalisé."""
    debuts = async_capture_events(hass, EVENT_UPLOAD_START)
    coordinateur = hass.data[DATA_UPLOADS]
    coordinateur.async_enregistrer("Sauvegarde sans slug", ("destination_test",))
    hass.bus.async_fire(EVENT_BACKUP_START, {ATTR_NAME: "Sauvegarde sans slug"})

    # Un événement sans nom ne réclame aucune demande ; un événement nommé mais
    # dépourvu de slug en réclame une, sans pouvoir la mener à bien.
    hass.bus.async_fire(EVENT_BACKUP_SUCCESSFUL, {ATTR_SLUG: SLUG})
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(coordinateur.demandes_en_attente) == 1

    hass.bus.async_fire(EVENT_BACKUP_SUCCESSFUL, {ATTR_NAME: "Sauvegarde sans slug"})
    await hass.async_block_till_done(wait_background_tasks=True)

    assert not coordinateur.demandes_en_attente
    assert not debuts


async def test_une_demande_non_confirmee_n_est_jamais_televersee(
    hass: HomeAssistant, instance: _Instance
) -> None:
    """Sans `backup_start`, une demande ne peut être réclamée par aucune sauvegarde.

    C'est ce qui protège une sauvegarde homonyme, créée plus tard sans
    `upload_to`, d'être téléversée à l'insu de l'utilisateur.
    """
    debuts = async_capture_events(hass, EVENT_UPLOAD_START)
    coordinateur = hass.data[DATA_UPLOADS]
    demande = coordinateur.async_enregistrer(
        "Sauvegarde orpheline", ("destination_test",)
    )
    assert not demande.confirmee

    hass.bus.async_fire(
        EVENT_BACKUP_SUCCESSFUL, {ATTR_NAME: "Sauvegarde orpheline", ATTR_SLUG: SLUG}
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    assert not debuts
    assert coordinateur.demandes_en_attente == [demande]

    # Une fois confirmée par le démarrage d'une sauvegarde, elle est honorée.
    hass.bus.async_fire(EVENT_BACKUP_START, {ATTR_NAME: "Sauvegarde orpheline"})
    hass.bus.async_fire(
        EVENT_BACKUP_SUCCESSFUL, {ATTR_NAME: "Sauvegarde orpheline", ATTR_SLUG: SLUG}
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    assert len(debuts) == 1
    assert not coordinateur.demandes_en_attente


async def test_une_demande_orpheline_expire_apres_30_secondes(
    hass: HomeAssistant, instance: _Instance
) -> None:
    """Passé `DELAI_CONFIRMATION_DEMANDE`, une demande jamais confirmée est
    oubliée : aucune sauvegarde, même homonyme, ne peut plus la réclamer.
    """
    debuts = async_capture_events(hass, EVENT_UPLOAD_START)
    coordinateur = hass.data[DATA_UPLOADS]

    with patch(
        "custom_components.auto_backup.destinations.upload.monotonic",
        return_value=1_000.0,
    ):
        demande = coordinateur.async_enregistrer(
            "Sauvegarde orpheline", ("destination_test",)
        )
    assert coordinateur.demandes_en_attente == [demande]

    with patch(
        "custom_components.auto_backup.destinations.upload.monotonic",
        return_value=1_000.0 + DELAI_CONFIRMATION_DEMANDE + 1,
    ):
        hass.bus.async_fire(EVENT_BACKUP_START, {ATTR_NAME: "Sauvegarde orpheline"})
        hass.bus.async_fire(
            EVENT_BACKUP_SUCCESSFUL,
            {ATTR_NAME: "Sauvegarde orpheline", ATTR_SLUG: SLUG},
        )
        await hass.async_block_till_done(wait_background_tasks=True)

    assert not debuts
    assert not coordinateur.demandes_en_attente


async def test_une_sauvegarde_homonyme_sans_upload_to_n_est_pas_televersee(
    hass: HomeAssistant, instance: _Instance
) -> None:
    """Une demande orpheline ne doit jamais être récupérée par une sauvegarde
    homonyme distincte, créée sans `upload_to`.

    L'orphelinage est produit ici par un chemin de production réel : sur Core,
    `validate_backup_config()` refuse `exclude` (fonctionnalité réservée à
    Supervisor) *après* que `async_prepare_upload()` a déjà enregistré la
    demande, mais *avant* que `auto_backup.backup_start` ne soit émis — la
    demande reste donc enregistrée sans jamais être confirmée. Comme Core nomme
    toutes les sauvegardes sans nom explicite de façon identique
    (`generate_backup_name()`), un appel parfaitement normal, sans
    `upload_to`, obtient ensuite le même nom de corrélation.
    """
    debuts = async_capture_events(hass, EVENT_UPLOAD_START)
    succes = async_capture_events(hass, EVENT_UPLOAD_SUCCESSFUL)

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_BACKUP_FULL,
            {ATTR_UPLOAD_TO: "destination_test", ATTR_EXCLUDE: {}},
            blocking=True,
        )

    await hass.services.async_call(DOMAIN, SERVICE_BACKUP, {}, blocking=True)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert not debuts
    assert not succes


async def test_une_creation_echouee_apres_le_demarrage_ne_laisse_rien_a_reclamer(
    hass: HomeAssistant, instance: _Instance
) -> None:
    """Une création confirmée puis échouée libère sa demande.

    L'échec survient ici **après** `auto_backup.backup_start` : la demande a
    donc été confirmée, et l'expiration ne purge pas les demandes confirmées.
    C'est `auto_backup.backup_failed` qui doit la libérer, sans quoi la
    prochaine sauvegarde homonyme — créée sans `upload_to` — la consommerait.
    """
    debuts = async_capture_events(hass, EVENT_UPLOAD_START)
    succes = async_capture_events(hass, EVENT_UPLOAD_SUCCESSFUL)
    creations_echouees = async_capture_events(hass, EVENT_BACKUP_FAILED)
    coordinateur = hass.data[DATA_UPLOADS]
    instance.creation.side_effect = HassioAPIError("sauvegarde déjà en cours")

    await hass.services.async_call(
        DOMAIN, SERVICE_BACKUP, {ATTR_UPLOAD_TO: "destination_test"}, blocking=True
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    # La création a bien démarré (donc confirmé la demande) avant d'échouer.
    assert len(creations_echouees) == 1
    assert not coordinateur.demandes_en_attente

    instance.creation.side_effect = None
    await hass.services.async_call(DOMAIN, SERVICE_BACKUP, {}, blocking=True)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert not debuts
    assert not succes


async def test_un_echec_de_creation_libere_la_demande_confirmee(
    hass: HomeAssistant, instance: _Instance
) -> None:
    """`auto_backup.backup_failed` retire la demande confirmée qu'il concerne."""
    debuts = async_capture_events(hass, EVENT_UPLOAD_START)
    coordinateur = hass.data[DATA_UPLOADS]
    demande = coordinateur.async_enregistrer("Sauvegarde du 22", ("destination_test",))

    hass.bus.async_fire(EVENT_BACKUP_START, {ATTR_NAME: "Sauvegarde du 22"})
    await hass.async_block_till_done()
    assert demande.confirmee

    hass.bus.async_fire(
        EVENT_BACKUP_FAILED,
        {ATTR_NAME: "Sauvegarde du 22", ATTR_ERROR: "disque plein"},
    )
    # Un échec qui ne correspond à aucune demande ne fait rien de plus.
    hass.bus.async_fire(EVENT_BACKUP_FAILED, {ATTR_NAME: "Une autre sauvegarde"})
    await hass.async_block_till_done()

    assert not coordinateur.demandes_en_attente

    # La sauvegarde homonyme suivante n'a donc plus rien à réclamer.
    hass.bus.async_fire(
        EVENT_BACKUP_SUCCESSFUL, {ATTR_NAME: "Sauvegarde du 22", ATTR_SLUG: SLUG}
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    assert not debuts


async def test_liberer_une_demande_non_confirmee_la_supprime_aussitot(
    hass: HomeAssistant, instance: _Instance
) -> None:
    """`async_release_upload()` referme la fenêtre d'armement de la demande.

    Non confirmée, la demande disparaît sur-le-champ, sans attendre son
    expiration : c'est ce qui rend l'appel de service étanche, y compris quand
    la création lève avant `auto_backup.backup_start`.
    """
    debuts = async_capture_events(hass, EVENT_UPLOAD_START)
    coordinateur = hass.data[DATA_UPLOADS]
    donnees: dict[str, Any] = {ATTR_UPLOAD_TO: ["destination_test"]}

    demande = async_prepare_upload(hass, donnees)

    assert demande is not None
    assert demande.en_cours and not demande.confirmee
    assert coordinateur.demandes_en_attente == [demande]

    async_release_upload(hass, demande)

    assert not demande.en_cours
    assert not coordinateur.demandes_en_attente

    # Même une sauvegarde portant exactement le nom corrélé ne la ressuscite pas.
    hass.bus.async_fire(EVENT_BACKUP_START, {ATTR_NAME: donnees[ATTR_NAME]})
    hass.bus.async_fire(
        EVENT_BACKUP_SUCCESSFUL, {ATTR_NAME: donnees[ATTR_NAME], ATTR_SLUG: SLUG}
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    assert not debuts


async def test_liberer_une_demande_confirmee_laisse_la_creation_aboutir(
    hass: HomeAssistant, instance: _Instance
) -> None:
    """Une demande déjà confirmée survit à la fermeture de sa fenêtre.

    La création a démarré : son succès reste à venir et doit encore pouvoir
    déclencher le téléversement, même si l'appel de service a rendu la main.
    """
    succes = async_capture_events(hass, EVENT_UPLOAD_SUCCESSFUL)
    coordinateur = hass.data[DATA_UPLOADS]
    donnees: dict[str, Any] = {
        ATTR_NAME: "Sauvegarde du 22",
        ATTR_UPLOAD_TO: ["destination_test"],
    }

    demande = async_prepare_upload(hass, donnees)
    hass.bus.async_fire(EVENT_BACKUP_START, {ATTR_NAME: "Sauvegarde du 22"})
    await hass.async_block_till_done()

    async_release_upload(hass, demande)
    assert coordinateur.demandes_en_attente == [demande]

    hass.bus.async_fire(
        EVENT_BACKUP_SUCCESSFUL, {ATTR_NAME: "Sauvegarde du 22", ATTR_SLUG: SLUG}
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    assert len(succes) == 1
    assert not coordinateur.demandes_en_attente


async def test_chaque_demande_porte_un_identifiant_unique(
    hass: HomeAssistant, instance: _Instance
) -> None:
    """Deux demandes homonymes restent distinguables dans les journaux."""
    coordinateur = hass.data[DATA_UPLOADS]
    premiere = coordinateur.async_enregistrer("Core 2026.9", ("destination_test",))
    seconde = coordinateur.async_enregistrer("Core 2026.9", ("destination_test",))

    assert premiere.identifiant and seconde.identifiant
    assert premiere.identifiant != seconde.identifiant


async def test_le_slug_est_encode_dans_l_url_du_supervisor(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Un slug inattendu ne peut pas déborder du chemin appelé sur le Supervisor."""
    chemin_encode = "/backups/..%2Faddons%2Fself%2Foptions/download"
    aioclient_mock.get(
        f"http://{ADRESSE_SUPERVISOR}{chemin_encode}", content=CONTENU_SAUVEGARDE
    )
    handler = SupervisorHandler(ADRESSE_SUPERVISOR, async_get_clientsession(hass))

    async with async_ouvrir_sauvegarde(
        hass, handler, "../addons/self/options"
    ) as contenu:
        morceaux = [morceau async for morceau in contenu.flux]

    assert b"".join(morceaux) == CONTENU_SAUVEGARDE
    assert aioclient_mock.mock_calls[0][1].raw_path == chemin_encode


async def test_une_chaine_unique_vaut_une_destination(
    hass: HomeAssistant, instance: _Instance
) -> None:
    """`upload_to` donné en chaîne n'est pas itéré caractère par caractère."""
    donnees = {ATTR_NAME: "Sauvegarde", ATTR_UPLOAD_TO: "destination_test"}

    demande = async_prepare_upload(hass, donnees)

    assert demande is not None
    assert demande.destinations == ("destination_test",)
    async_release_upload(hass, demande)


async def test_sans_entree_chargee_le_televersement_est_refuse(
    hass: HomeAssistant, instance: _Instance
) -> None:
    """`upload_to` est refusé, en français, si l'entrée n'est plus chargée."""
    coordinateur = hass.data.pop(DATA_UPLOADS)

    with pytest.raises(ServiceValidationError, match="n'est pas chargée"):
        async_prepare_upload(hass, {ATTR_UPLOAD_TO: ["destination_test"]})

    hass.data[DATA_UPLOADS] = coordinateur
    hass.data.pop(DATA_AUTO_BACKUP)

    with pytest.raises(ServiceValidationError, match="n'est pas chargée"):
        async_prepare_upload(hass, {ATTR_UPLOAD_TO: ["destination_test"]})


async def test_une_demande_sans_destination_ne_change_rien(
    hass: HomeAssistant, instance: _Instance
) -> None:
    """`upload_to` vide n'enregistre aucune demande et ne laisse aucune trace."""
    donnees = {ATTR_NAME: "Sauvegarde", ATTR_UPLOAD_TO: []}

    assert async_prepare_upload(hass, donnees) is None

    assert donnees == {ATTR_NAME: "Sauvegarde"}
    assert not hass.data[DATA_UPLOADS].demandes_en_attente
    # Oublier une demande inexistante est sans effet.
    async_release_upload(hass, None)


async def test_le_coordinateur_disparait_au_dechargement(
    hass: HomeAssistant, instance: _Instance
) -> None:
    """Le coordinateur est retiré de `hass.data` quand l'entrée est déchargée."""
    assert DATA_UPLOADS in hass.data

    assert await hass.config_entries.async_unload(instance.entree.entry_id)
    await hass.async_block_till_done()

    assert DATA_UPLOADS not in hass.data


### RÉGLAGE DU DÉLAI DEPUIS L'INTERFACE ###
#
# Critère 6 de l'issue #8 : « son délai maximum est configurable dans les
# options ». Le formulaire upstream (`init`) ignore cette option ; c'est une
# étape propre au fork, atteinte depuis le menu du flux d'options.


# Délai choisi dans l'interface par les tests : assez grand pour qu'aucun
# téléversement factice ne l'atteigne, assez singulier pour être reconnu.
DELAI_CHOISI = 45


def _valeur_proposee(resultat: dict[str, Any], cle: str) -> Any:
    """Valeur pré-remplie par le formulaire pour ce champ."""
    for marqueur in resultat["data_schema"].schema:
        if marqueur == cle:
            return (marqueur.description or {}).get("suggested_value")
    raise AssertionError(f"champ « {cle} » absent du formulaire")


async def _regler_le_delai(
    hass: HomeAssistant,
    entree: MockConfigEntry,
    ouvrir_les_options: OuvrirLesOptions,
    delai: Any,
) -> dict[str, Any]:
    """Ouvre les réglages du téléversement et soumet ce délai."""
    resultat = await ouvrir_les_options(entree.entry_id, "reglages_televersement")
    assert resultat["step_id"] == "reglages_televersement"

    resultat = await hass.config_entries.options.async_configure(
        resultat["flow_id"], user_input={CONF_UPLOAD_TIMEOUT: delai}
    )
    await hass.async_block_till_done()
    return resultat


async def test_le_formulaire_propose_le_delai_en_vigueur(
    hass: HomeAssistant,
    integration_backup: None,
    fournisseur_factice: str,
    fichier_de_sauvegarde: Path,
    ouvrir_les_options: OuvrirLesOptions,
) -> None:
    """Le champ est pré-rempli : valeur par défaut, puis valeur configurée."""
    instance = await _demarrer(hass, fichier_de_sauvegarde)

    resultat = await ouvrir_les_options(
        instance.entree.entry_id, "reglages_televersement"
    )
    assert resultat["type"] is FlowResultType.FORM
    assert _valeur_proposee(resultat, CONF_UPLOAD_TIMEOUT) == DEFAULT_UPLOAD_TIMEOUT

    await _regler_le_delai(hass, instance.entree, ouvrir_les_options, DELAI_CHOISI)

    resultat = await ouvrir_les_options(
        instance.entree.entry_id, "reglages_televersement"
    )
    assert _valeur_proposee(resultat, CONF_UPLOAD_TIMEOUT) == DELAI_CHOISI


async def test_le_delai_saisi_dans_l_interface_est_celui_du_coordinateur(
    hass: HomeAssistant,
    integration_backup: None,
    fournisseur_factice: str,
    fichier_de_sauvegarde: Path,
    ouvrir_les_options: OuvrirLesOptions,
) -> None:
    """Le délai réglé dans l'UI est celui qui borne le téléversement suivant.

    La preuve est prise au plus près : `asyncio.timeout()` est observé pendant
    l'envoi, et reçoit bien la valeur saisie — sans redémarrage de l'entrée.
    """
    instance = await _demarrer(hass, fichier_de_sauvegarde)

    resultat = await _regler_le_delai(
        hass, instance.entree, ouvrir_les_options, DELAI_CHOISI
    )
    assert resultat["type"] is FlowResultType.CREATE_ENTRY

    # Le sélecteur numérique renvoie un flottant ; l'option persistée est un
    # entier de secondes, et les options upstream restent complétées.
    assert instance.entree.options[CONF_UPLOAD_TIMEOUT] == DELAI_CHOISI
    assert isinstance(instance.entree.options[CONF_UPLOAD_TIMEOUT], int)
    assert CONF_AUTO_PURGE in instance.entree.options
    assert CONF_BACKUP_TIMEOUT in instance.entree.options

    succes = async_capture_events(hass, EVENT_UPLOAD_SUCCESSFUL)
    with patch(
        "custom_components.auto_backup.destinations.upload.asyncio.timeout",
        wraps=asyncio.timeout,
    ) as chronometre:
        await hass.services.async_call(
            DOMAIN, SERVICE_BACKUP, {ATTR_UPLOAD_TO: "destination_test"}, blocking=True
        )
        await hass.async_block_till_done(wait_background_tasks=True)

    assert len(succes) == 1
    delais = [appel.args[0] for appel in chronometre.call_args_list if appel.args]
    assert float(DELAI_CHOISI) in delais


async def test_le_delai_regle_survit_aux_reglages_de_sauvegarde(
    hass: HomeAssistant,
    integration_backup: None,
    fournisseur_factice: str,
    fichier_de_sauvegarde: Path,
    ouvrir_les_options: OuvrirLesOptions,
) -> None:
    """Enregistrer le formulaire upstream n'efface plus le délai ni les destinations.

    Le formulaire upstream remplace l'intégralité des options par son contenu :
    sans le report des clés du fork, le délai réglé juste avant disparaîtrait
    en silence.
    """
    instance = await _demarrer(hass, fichier_de_sauvegarde)
    await _regler_le_delai(hass, instance.entree, ouvrir_les_options, DELAI_CHOISI)

    resultat = await ouvrir_les_options(instance.entree.entry_id, "init")
    resultat = await hass.config_entries.options.async_configure(
        resultat["flow_id"],
        user_input={CONF_AUTO_PURGE: False, CONF_BACKUP_TIMEOUT: 45},
    )
    await hass.async_block_till_done()

    assert resultat["type"] is FlowResultType.CREATE_ENTRY
    assert instance.entree.options[CONF_AUTO_PURGE] is False
    assert instance.entree.options[CONF_UPLOAD_TIMEOUT] == DELAI_CHOISI
    assert instance.entree.options[CONF_DESTINATIONS] == [config_factice()]
    assert hass.data[DATA_UPLOADS]._delai == float(DELAI_CHOISI)


@pytest.mark.parametrize("delai", [0, -30, 0.4])
async def test_un_delai_nul_ou_negatif_est_refuse(
    hass: HomeAssistant,
    integration_backup: None,
    fournisseur_factice: str,
    fichier_de_sauvegarde: Path,
    ouvrir_les_options: OuvrirLesOptions,
    delai: float,
) -> None:
    """Un délai qui ne laisse aucune chance au téléversement est refusé."""
    instance = await _demarrer(hass, fichier_de_sauvegarde)

    resultat = await _regler_le_delai(hass, instance.entree, ouvrir_les_options, delai)

    assert resultat["type"] is FlowResultType.FORM
    assert resultat["errors"] == {CONF_UPLOAD_TIMEOUT: "delai_invalide"}
    assert CONF_UPLOAD_TIMEOUT not in instance.entree.options
    # La saisie refusée reste affichée, pour être corrigée plutôt que retapée.
    assert _valeur_proposee(resultat, CONF_UPLOAD_TIMEOUT) == delai


@pytest.mark.parametrize(
    ("saisie", "attendu"),
    [(1, 1), (DELAI_CHOISI, DELAI_CHOISI), ("120", 120), (1800.0, 1800)],
)
def test_un_delai_valide_devient_un_entier_de_secondes(
    saisie: Any, attendu: int
) -> None:
    """Le flottant du sélecteur devient un nombre entier de secondes."""
    assert _delai_de_televersement(saisie) == attendu


@pytest.mark.parametrize("saisie", [None, "", "jamais"])
def test_un_delai_non_numerique_est_refuse(saisie: Any) -> None:
    """Une valeur qui n'est pas un nombre est refusée, pas interprétée."""
    with pytest.raises(DestinationConfigError):
        _delai_de_televersement(saisie)
