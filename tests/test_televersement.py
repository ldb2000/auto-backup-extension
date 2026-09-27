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
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from homeassistant.components import persistent_notification
from homeassistant.const import ATTR_NAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
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
    ATTR_ERROR_CODE,
    ATTR_EXCLUDE,
    ATTR_LAST_ERROR,
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
    DestinationAuthError,
    DestinationConfigError,
    DestinationError,
    DestinationNotFoundError,
    DestinationQuotaError,
)
from custom_components.auto_backup.destinations.entities import (
    SUFFIXE_PROBLEME,
    identifiant_unique,
)
from custom_components.auto_backup.destinations.errors import CodeErreur
from custom_components.auto_backup.destinations.flow import _delai_de_televersement
from custom_components.auto_backup.destinations.masquage import masquer
from custom_components.auto_backup.destinations.notifications import (
    identifiant_de_notification_d_echec,
)
from custom_components.auto_backup.destinations.providers.google_drive import (
    erreur_de_la_reponse,
)
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
from messages_attendus import message_d_erreur

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

    with pytest.raises(ServiceValidationError, match="Several destinations") as err:
        async_resoudre_destinations(hass, ["Destination de test"])

    assert err.value.translation_domain == DOMAIN
    assert err.value.translation_key == "destination_ambigue"
    assert err.value.translation_placeholders == {
        "destination": "Destination de test",
        "identifiants": "destination_bis, destination_test",
    }


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
    """Une destination inconnue lève une erreur traduisible, sans rien créer.

    L'erreur porte son domaine et sa clé de traduction (#46) : l'interface de
    Home Assistant l'affiche dans la langue de l'utilisateur. Son texte brut,
    lui, est l'anglais — `str()` d'une erreur traduisible.
    """
    debuts = async_capture_events(hass, EVENT_UPLOAD_START)

    with pytest.raises(ServiceValidationError) as erreur:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_BACKUP,
            {ATTR_UPLOAD_TO: ["dropbox_perso"]},
            blocking=True,
        )

    assert erreur.value.translation_domain == DOMAIN
    assert erreur.value.translation_key == "destination_inconnue"
    # Le message oriente l'utilisateur vers ce qui est réellement configuré.
    assert erreur.value.translation_placeholders == {
        "destination": "dropbox_perso",
        "disponibles": "Destination de test (destination_test)",
    }
    assert str(erreur.value) == (
        "Unknown destination: “dropbox_perso”. Configured destinations: "
        "Destination de test (destination_test)"
    )

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

    with pytest.raises(ServiceValidationError, match="No remote destination") as err:
        await hass.services.async_call(
            DOMAIN, SERVICE_BACKUP, {ATTR_UPLOAD_TO: "dropbox"}, blocking=True
        )

    assert err.value.translation_domain == DOMAIN
    assert err.value.translation_key == "destination_inconnue_sans_destination"
    assert err.value.translation_placeholders == {"destination": "dropbox"}

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
    # Une erreur que le fork ne qualifie pas : message générique traduit, et le
    # détail au journal seulement (#46).
    assert echecs[0].data[ATTR_ERROR] == message_d_erreur(CodeErreur.INCONNUE)
    assert echecs[0].data[ATTR_ERROR_CODE] == "unknown"

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
    assert echecs[0].data[ATTR_ERROR_CODE] == "timeout"
    assert echecs[0].data[ATTR_ERROR] == message_d_erreur(CodeErreur.DELAI_DEPASSE)
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
    assert echecs[0].data[ATTR_ERROR_CODE] == "local_backup_unreadable"
    assert echecs[0].data[ATTR_ERROR] == message_d_erreur(
        CodeErreur.SAUVEGARDE_ILLISIBLE
    )
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
    assert echecs[0].data[ATTR_ERROR_CODE] == "unknown_destination"
    assert echecs[0].data[ATTR_ERROR] == message_d_erreur(
        CodeErreur.DESTINATION_INCONNUE
    )


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
    assert echecs[0].data[ATTR_ERROR_CODE] == "access_revoked"
    assert echecs[0].data[ATTR_ERROR] == message_d_erreur(CodeErreur.ACCES_REVOQUE)

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
    assert echecs[0].data[ATTR_ERROR_CODE] == "unknown"
    assert echecs[0].data[ATTR_ERROR] == message_d_erreur(CodeErreur.INCONNUE)


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
    """`upload_to` est refusé, traduisible (#46), si l'entrée n'est plus chargée."""
    coordinateur = hass.data.pop(DATA_UPLOADS)

    with pytest.raises(ServiceValidationError, match="is not loaded") as err:
        async_prepare_upload(hass, {ATTR_UPLOAD_TO: ["destination_test"]})
    assert err.value.translation_key == "televersement_indisponible"

    hass.data[DATA_UPLOADS] = coordinateur
    hass.data.pop(DATA_AUTO_BACKUP)

    with pytest.raises(ServiceValidationError, match="is not loaded") as err:
        async_prepare_upload(hass, {ATTR_UPLOAD_TO: ["destination_test"]})
    assert err.value.translation_key == "televersement_indisponible"


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


@pytest.mark.parametrize("delai", [float("inf"), float("-inf")])
async def test_un_delai_infini_est_refuse(
    hass: HomeAssistant,
    integration_backup: None,
    fournisseur_factice: str,
    fichier_de_sauvegarde: Path,
    ouvrir_les_options: OuvrirLesOptions,
    delai: float,
) -> None:
    """Un délai infini est refusé par un message, pas par une exception qui fuit.

    `NumberSelector` ne borne volontairement pas la saisie (cf. la docstring de
    `_delai_de_televersement`) : rien n'empêche `float("inf")` de l'atteindre.
    Le critère 6 promet alors l'erreur `delai_invalide`, pas une exception qui
    ferait échouer le flux d'options sans explication pour l'utilisateur.
    """
    instance = await _demarrer(hass, fichier_de_sauvegarde)

    resultat = await _regler_le_delai(hass, instance.entree, ouvrir_les_options, delai)

    assert resultat["type"] is FlowResultType.FORM
    assert resultat["errors"] == {CONF_UPLOAD_TIMEOUT: "delai_invalide"}


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


### AUCUN SECRET DANS LE JOURNAL (#35) ###

# Valeurs inventées, aux formes de celles qu'un fournisseur renvoie réellement :
# un jeton Dropbox, un jeton Google, et l'URI de session d'un envoi reprenable
# Google Drive, dont l'`upload_id` suffit à écrire dans le compte.
JETON_DROPBOX_FACTICE = "sl.B1a2C3FaCtIcE-0123456789abcdefghij"
JETON_GOOGLE_FACTICE = "ya29.a0AfH6FaCtIcE-0123456789abcdef"
IDENTIFIANT_ENVOI_FACTICE = "AAAAFaCtIcE0123456789abcdefghijkl"
URI_DE_SESSION_FACTICE = (
    "https://www.googleapis.invalid/upload/drive/v3/files"
    f"?uploadType=resumable&upload_id={IDENTIFIANT_ENVOI_FACTICE}"
)
SECRETS_FACTICES = (
    JETON_DROPBOX_FACTICE,
    JETON_GOOGLE_FACTICE,
    IDENTIFIANT_ENVOI_FACTICE,
)
MESSAGE_AVEC_SECRETS = (
    f"envoi refusé : Authorization: Bearer {JETON_GOOGLE_FACTICE}, "
    f"access_token={JETON_DROPBOX_FACTICE}, session {URI_DE_SESSION_FACTICE}"
)


def _fuites(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Secrets factices présents dans le journal, tous niveaux confondus."""
    return [secret for secret in SECRETS_FACTICES if secret in caplog.text]


@pytest.mark.parametrize(
    "erreur",
    [
        pytest.param(DestinationError(MESSAGE_AVEC_SECRETS), id="erreur-typee"),
        pytest.param(RuntimeError(MESSAGE_AVEC_SECRETS), id="erreur-inattendue"),
        pytest.param(
            aiohttp.ClientConnectionError(
                f"Connection reset while PUT {URI_DE_SESSION_FACTICE}"
            ),
            id="erreur-reseau-inattendue",
        ),
    ],
)
async def test_un_echec_de_fournisseur_ne_journalise_aucun_secret(
    hass: HomeAssistant,
    instance: _Instance,
    caplog: pytest.LogCaptureFixture,
    erreur: Exception,
) -> None:
    """Critère 1 : ni jeton ni URI de session dans le journal, même en debug."""
    echecs = async_capture_events(hass, EVENT_UPLOAD_FAILED)
    instance.destination("destination_test").erreur_a_lever = erreur

    with caplog.at_level(logging.DEBUG):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_BACKUP,
            {ATTR_NAME: "Sauvegarde du 22", ATTR_UPLOAD_TO: "destination_test"},
            blocking=True,
        )
        await hass.async_block_till_done(wait_background_tasks=True)

    assert len(echecs) == 1
    assert not _fuites(caplog), f"secrets journalisés : {_fuites(caplog)}"
    # L'échec reste journalisé et diagnosticable : slug et cause masquée.
    erreurs = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert any(SLUG in message and "***" in message for message in erreurs)
    # Aucune trace brute : l'enregistrement ne transporte pas l'exception.
    assert all(record.exc_info is None for record in caplog.records)


async def test_une_erreur_inattendue_garde_sa_trace_masquee_en_debug(
    hass: HomeAssistant, instance: _Instance, caplog: pytest.LogCaptureFixture
) -> None:
    """La trace sert encore au diagnostic, en `debug`, sans le secret."""
    instance.destination("destination_test").erreur_a_lever = RuntimeError(
        MESSAGE_AVEC_SECRETS
    )

    with caplog.at_level(logging.DEBUG):
        await hass.services.async_call(
            DOMAIN, SERVICE_BACKUP, {ATTR_UPLOAD_TO: "destination_test"}, blocking=True
        )
        await hass.async_block_till_done(wait_background_tasks=True)

    traces = [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.DEBUG and "Traceback" in record.getMessage()
    ]
    assert len(traces) == 1
    assert "RuntimeError: envoi refusé" in traces[0]
    assert "async_upload" in traces[0]
    assert not _fuites(caplog)


async def test_une_erreur_inattendue_n_a_pas_de_trace_hors_debug(
    hass: HomeAssistant, instance: _Instance, caplog: pytest.LogCaptureFixture
) -> None:
    """Au niveau par défaut, seule la ligne d'erreur masquée est écrite."""
    instance.destination("destination_test").erreur_a_lever = RuntimeError(
        MESSAGE_AVEC_SECRETS
    )

    with caplog.at_level(logging.INFO):
        await hass.services.async_call(
            DOMAIN, SERVICE_BACKUP, {ATTR_UPLOAD_TO: "destination_test"}, blocking=True
        )
        await hass.async_block_till_done(wait_background_tasks=True)

    assert "Traceback" not in caplog.text
    assert "RuntimeError: envoi refusé" in caplog.text
    assert not _fuites(caplog)


async def test_la_cause_d_une_creation_echouee_est_masquee_en_debug(
    hass: HomeAssistant, instance: _Instance, caplog: pytest.LogCaptureFixture
) -> None:
    """La cause relayée par `auto_backup.backup_failed` n'est pas maîtrisée."""
    coordinateur = hass.data[DATA_UPLOADS]
    coordinateur.async_enregistrer("Sauvegarde du 22", ("destination_test",))
    hass.bus.async_fire(EVENT_BACKUP_START, {ATTR_NAME: "Sauvegarde du 22"})
    await hass.async_block_till_done()

    with caplog.at_level(logging.DEBUG):
        hass.bus.async_fire(
            EVENT_BACKUP_FAILED,
            {ATTR_NAME: "Sauvegarde du 22", ATTR_ERROR: MESSAGE_AVEC_SECRETS},
        )
        await hass.async_block_till_done()

    assert "abandonnée" in caplog.text
    assert not _fuites(caplog)


### AUCUN SECRET DANS L'ÉVÉNEMENT `auto_backup.upload_failed` (#44, #46) ###

# Schéma public de l'événement (ADR 0001) : les cinq champs d'origine, plus le
# code stable ajouté par #46. Ni retrait, ni renommage.
CHAMPS_DE_L_EVENEMENT_D_ECHEC = frozenset(
    {
        ATTR_NAME,
        ATTR_SLUG,
        ATTR_DESTINATION,
        ATTR_DESTINATION_NAME,
        ATTR_ERROR,
        ATTR_ERROR_CODE,
    }
)


async def _declencher_un_echec(
    hass: HomeAssistant, instance: _Instance, erreur: Exception
) -> list[Any]:
    """Fait échouer un téléversement et renvoie les événements d'échec émis."""
    echecs = async_capture_events(hass, EVENT_UPLOAD_FAILED)
    instance.destination("destination_test").erreur_a_lever = erreur
    await hass.services.async_call(
        DOMAIN,
        SERVICE_BACKUP,
        {ATTR_NAME: "Sauvegarde du 22", ATTR_UPLOAD_TO: "destination_test"},
        blocking=True,
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    return echecs


@pytest.mark.parametrize(
    "erreur",
    [
        pytest.param(DestinationError(MESSAGE_AVEC_SECRETS), id="erreur-typee"),
        pytest.param(
            DestinationQuotaError(MESSAGE_AVEC_SECRETS), id="erreur-typee-connue"
        ),
        pytest.param(RuntimeError(MESSAGE_AVEC_SECRETS), id="erreur-inattendue"),
        pytest.param(
            aiohttp.ClientConnectionError(
                f"Connection reset while PUT {URI_DE_SESSION_FACTICE}"
            ),
            id="erreur-reseau-inattendue",
        ),
    ],
)
async def test_l_evenement_d_echec_ne_transporte_aucun_secret(
    hass: HomeAssistant,
    instance: _Instance,
    erreur: Exception,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Critère 1 de #44, renforcé par #46 : `error` ne porte plus aucun détail.

    L'événement est visible des outils de développement, de toute automatisation
    qui l'écoute, et stocké par l'enregistreur. Depuis #46, `error` est le
    message traduit du code de l'échec : le texte du fournisseur n'y entre plus
    du tout, même masqué, et ne va qu'au journal — masqué.
    """
    with caplog.at_level(logging.ERROR):
        echecs = await _declencher_un_echec(hass, instance, erreur)

    assert len(echecs) == 1
    cause = echecs[0].data[ATTR_ERROR]
    assert not [secret for secret in SECRETS_FACTICES if secret in cause]
    assert cause == message_d_erreur(echecs[0].data[ATTR_ERROR_CODE])
    assert masquer(str(erreur)) in caplog.text
    assert not _fuites(caplog)


@pytest.mark.parametrize(
    ("erreur", "code"),
    [
        pytest.param(
            DestinationAuthError("jeton refusé"), "access_revoked", id="acces-revoque"
        ),
        pytest.param(
            DestinationQuotaError("quota dépassé"), "quota_exceeded", id="quota"
        ),
        pytest.param(
            DestinationNotFoundError("absente"), "not_found", id="introuvable"
        ),
        pytest.param(
            DestinationConfigError("folder invalide", code=CodeErreur.DOSSIER_INVALIDE),
            "invalid_folder",
            id="dossier-invalide",
        ),
        pytest.param(
            DestinationError("portée", code=CodeErreur.PORTEE_MANQUANTE),
            "missing_scope",
            id="portee-manquante",
        ),
        pytest.param(DestinationError("quota dépassé"), "unknown", id="non-qualifiee"),
    ],
)
async def test_l_evenement_porte_le_code_stable_de_l_erreur(
    hass: HomeAssistant, instance: _Instance, erreur: DestinationError, code: str
) -> None:
    """Critère 3 de #46 : `error_code` est le code du fork, `error` sa traduction.

    Une erreur que rien ne qualifie reste `unknown`, même si son texte parle de
    quota : c'est le code, attribué là où l'échec est qualifié, qui fait foi.
    """
    echecs = await _declencher_un_echec(hass, instance, erreur)

    assert len(echecs) == 1
    assert echecs[0].data[ATTR_ERROR_CODE] == code
    assert type(echecs[0].data[ATTR_ERROR_CODE]) is str
    assert echecs[0].data[ATTR_ERROR] == message_d_erreur(code)


# Les erreurs telles que les fournisseurs du fork les construisent réellement
# (`google_drive.py::erreur_de_la_reponse`, corps d'erreur de forme Google
# habituelle), plutôt qu'un texte retapé à la main : c'est cette fonction de
# production qui décide du code.
def _charge_google(raison: str) -> dict[str, Any]:
    """Corps d'erreur de l'API Google Drive, dans sa forme habituelle."""
    return {
        "error": {
            "code": 0,
            "message": "erreur simulée",
            "errors": [
                {"domain": "global", "reason": raison, "message": "erreur simulée"}
            ],
        }
    }


@pytest.mark.parametrize(
    ("erreur", "code", "code_du_fournisseur"),
    [
        pytest.param(
            erreur_de_la_reponse(429, _charge_google("rateLimitExceeded")),
            "rate_limited",
            "rateLimitExceeded",
            id="google-limitation-429",
        ),
        pytest.param(
            erreur_de_la_reponse(403, _charge_google("userRateLimitExceeded")),
            "rate_limited",
            "userRateLimitExceeded",
            id="google-limitation-403",
        ),
        pytest.param(
            erreur_de_la_reponse(403, _charge_google("insufficientPermissions")),
            "missing_scope",
            # Hors de la liste blanche de #48 : masqué au journal.
            None,
            id="google-portee-manquante",
        ),
        pytest.param(
            erreur_de_la_reponse(403, _charge_google("accessNotConfigured")),
            "api_disabled",
            None,
            id="google-api-desactivee",
        ),
        pytest.param(
            erreur_de_la_reponse(403, _charge_google("storageQuotaExceeded")),
            "quota_exceeded",
            None,
            id="google-quota-stockage",
        ),
        pytest.param(
            erreur_de_la_reponse(401, _charge_google("authError")),
            "access_revoked",
            None,
            id="google-acces-revoque",
        ),
        pytest.param(
            erreur_de_la_reponse(404, _charge_google("notFound")),
            "not_found",
            None,
            id="google-ressource-absente",
        ),
        pytest.param(
            erreur_de_la_reponse(503, _charge_google("backendError")),
            "provider_unavailable",
            "backendError",
            id="google-panne-serveur",
        ),
        pytest.param(
            erreur_de_la_reponse(403, _charge_google("too_many_write_operations")),
            "unknown",
            "too_many_write_operations",
            id="google-403-non-qualifie",
        ),
    ],
)
async def test_les_erreurs_reelles_de_google_drive_portent_leur_code(
    hass: HomeAssistant,
    instance: _Instance,
    caplog: pytest.LogCaptureFixture,
    erreur: DestinationError,
    code: str,
    code_du_fournisseur: str | None,
) -> None:
    """Le code du fork, pas celui de Google ; le code de Google reste au journal.

    Avant #46, les codes connus des fournisseurs (liste blanche de #48) étaient
    lisibles dans `error`. Ils le restent dans le journal, où l'événement
    renvoie : une automatisation filtre désormais sur `error_code`.
    """
    with caplog.at_level(logging.ERROR):
        echecs = await _declencher_un_echec(hass, instance, erreur)

    assert len(echecs) == 1
    assert echecs[0].data[ATTR_ERROR_CODE] == code
    assert echecs[0].data[ATTR_ERROR] == message_d_erreur(code)
    assert f"[{code}]" in caplog.text
    if code_du_fournisseur is not None:
        assert code_du_fournisseur in caplog.text
        assert code_du_fournisseur not in echecs[0].data[ATTR_ERROR]


async def test_un_code_connu_reste_lisible_au_journal_mais_le_secret_voisin_non(
    hass: HomeAssistant, instance: _Instance, caplog: pytest.LogCaptureFixture
) -> None:
    """Critère 2 de #48, par le journal : le code passe, le jeton non."""
    jeton = "sl.B1a2C3FaCtIcE-0123456789abcdefghij"
    erreur = DestinationAuthError(
        f"Dropbox refuse l'accès (HTTP 401) : expired_access_token/ ({jeton})"
    )

    with caplog.at_level(logging.ERROR):
        echecs = await _declencher_un_echec(hass, instance, erreur)

    assert len(echecs) == 1
    assert echecs[0].data[ATTR_ERROR_CODE] == "access_revoked"
    assert "Dropbox refuse l'accès (HTTP 401) : expired_access_token/ (***)" in (
        caplog.text
    )
    assert jeton not in caplog.text


@pytest.mark.parametrize(
    ("langue", "attendu"),
    [
        pytest.param("fr", "l'espace de stockage du compte est plein", id="fr"),
        pytest.param("en", "the account's storage is full", id="en"),
    ],
)
async def test_le_message_de_l_evenement_suit_la_langue_de_l_instance(
    hass: HomeAssistant, instance: _Instance, langue: str, attendu: str
) -> None:
    """Critère 1 de #46 : `error` est traduit dans la langue de l'instance.

    Le code, lui, ne change pas d'une langue à l'autre : c'est lui que les
    automatisations comparent.
    """
    hass.config.language = langue

    echecs = await _declencher_un_echec(
        hass, instance, DestinationQuotaError("espace saturé")
    )

    assert len(echecs) == 1
    assert echecs[0].data[ATTR_ERROR].startswith(attendu)
    assert echecs[0].data[ATTR_ERROR] == message_d_erreur("quota_exceeded", langue)
    assert echecs[0].data[ATTR_ERROR_CODE] == "quota_exceeded"


async def test_le_delai_depasse_porte_son_code_par_le_vrai_chemin(
    hass: HomeAssistant,
    integration_backup: None,
    fournisseur_factice: str,
    fichier_de_sauvegarde: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Le délai du fork : code `timeout`, et sa durée au journal seulement."""
    instance = await _demarrer(
        hass, fichier_de_sauvegarde, options={CONF_UPLOAD_TIMEOUT: 0.01}
    )
    instance.destination("destination_test").attente_secondes = 30
    echecs = async_capture_events(hass, EVENT_UPLOAD_FAILED)

    with caplog.at_level(logging.ERROR):
        await hass.services.async_call(
            DOMAIN, SERVICE_BACKUP, {ATTR_UPLOAD_TO: "destination_test"}, blocking=True
        )
        await hass.async_block_till_done(wait_background_tasks=True)

    assert len(echecs) == 1
    assert echecs[0].data[ATTR_ERROR_CODE] == "timeout"
    assert echecs[0].data[ATTR_ERROR] == message_d_erreur("timeout")
    assert "délai de téléversement dépassé (0.01 s)" in caplog.text


@pytest.mark.parametrize(
    "erreur",
    [
        pytest.param(DestinationError("quota dépassé"), id="sans-secret"),
        pytest.param(DestinationError(MESSAGE_AVEC_SECRETS), id="avec-secrets"),
    ],
)
async def test_le_schema_de_l_evenement_d_echec_ne_perd_aucun_champ(
    hass: HomeAssistant, instance: _Instance, erreur: Exception
) -> None:
    """Critère 3 de #46 : les cinq champs d'avant, mêmes noms, plus `error_code`."""
    echecs = await _declencher_un_echec(hass, instance, erreur)

    assert len(echecs) == 1
    donnees = echecs[0].data
    assert set(donnees) == CHAMPS_DE_L_EVENEMENT_D_ECHEC
    assert donnees[ATTR_NAME] == "Sauvegarde du 22"
    assert donnees[ATTR_SLUG] == SLUG
    assert donnees[ATTR_DESTINATION] == "destination_test"
    assert (
        donnees[ATTR_DESTINATION_NAME] == instance.destination("destination_test").name
    )
    assert isinstance(donnees[ATTR_ERROR], str) and donnees[ATTR_ERROR]


### CE QUE L'UTILISATEUR LIT, DE BOUT EN BOUT (#46) ###


def _last_error(hass: HomeAssistant, instance: _Instance) -> str | None:
    """Attribut `last_error` du capteur « problème » de la destination de test."""
    entity_id = er.async_get(hass).async_get_entity_id(
        "binary_sensor",
        DOMAIN,
        identifiant_unique(
            instance.entree.entry_id, "destination_test", SUFFIXE_PROBLEME
        ),
    )
    assert entity_id is not None
    etat = hass.states.get(entity_id)
    assert etat is not None
    return etat.attributes[ATTR_LAST_ERROR]


def _notification_d_echec(hass: HomeAssistant) -> dict[str, Any]:
    notification = persistent_notification._async_get_or_create_notifications(hass).get(
        identifiant_de_notification_d_echec("destination_test")
    )
    assert notification is not None
    return notification


@pytest.mark.parametrize(
    ("langue", "cause"),
    [
        pytest.param("fr", "**Cause** : ", id="fr"),
        pytest.param("en", "**Cause**: ", id="en"),
    ],
)
async def test_une_erreur_connue_est_affichee_traduite_partout(
    hass: HomeAssistant, instance: _Instance, langue: str, cause: str
) -> None:
    """Critère 1 de #46 : événement, notification et `last_error` sont traduits.

    Les trois affichent le même texte, celui du code dans la langue de
    l'instance ; le texte du fournisseur (français, ici) n'apparaît nulle part.
    """
    hass.config.language = langue
    message = message_d_erreur(CodeErreur.QUOTA_DEPASSE, langue)

    echecs = await _declencher_un_echec(
        hass, instance, DestinationQuotaError("espace Dropbox saturé (motif interne)")
    )

    assert echecs[0].data[ATTR_ERROR] == message
    assert _last_error(hass, instance) == message
    assert f"{cause}{message}" in _notification_d_echec(hass)["message"]
    for affiche in (
        echecs[0].data[ATTR_ERROR],
        _last_error(hass, instance),
        _notification_d_echec(hass)["message"],
    ):
        assert "motif interne" not in affiche


async def test_une_erreur_inconnue_affiche_un_message_generique(
    hass: HomeAssistant, instance: _Instance, caplog: pytest.LogCaptureFixture
) -> None:
    """Critère 4 de #46 : message générique traduit ; détail masqué, au journal.

    Le détail porte un jeton et l'URI de session d'un envoi Google Drive : il
    n'atteint ni l'événement, ni la notification, ni `last_error`, et le journal
    ne le reçoit que masqué.
    """
    hass.config.language = "fr"

    with caplog.at_level(logging.ERROR):
        echecs = await _declencher_un_echec(
            hass, instance, RuntimeError(MESSAGE_AVEC_SECRETS)
        )

    generique = message_d_erreur(CodeErreur.INCONNUE, "fr")
    assert echecs[0].data[ATTR_ERROR_CODE] == "unknown"
    assert echecs[0].data[ATTR_ERROR] == generique
    assert _last_error(hass, instance) == generique
    notification = _notification_d_echec(hass)["message"]
    assert f"**Cause** : {generique}" in notification
    for affiche in (
        echecs[0].data[ATTR_ERROR],
        _last_error(hass, instance),
        notification,
    ):
        assert "envoi refusé" not in affiche
    assert "[unknown] : " + masquer(MESSAGE_AVEC_SECRETS) in caplog.text
    assert not _fuites(caplog)
