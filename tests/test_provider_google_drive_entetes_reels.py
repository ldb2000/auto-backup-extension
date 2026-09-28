"""En-têtes réellement émis vers Google Drive par aiohttp (issue #67, critère 3).

`aioclient_mock` (utilisé par `tests/test_provider_google_drive_upload.py`)
enregistre les en-têtes que le code **passe** à aiohttp, pas ceux qu'aiohttp
**envoie** : c'est précisément ce qui avait laissé passer le défaut Dropbox
(cf. `tests/test_provider_dropbox_entetes_reels.py`). Ces tests relisent, pour
Google Drive, les deux seuls appels `POST` du fournisseur — la création d'un
dossier et l'ouverture d'une session d'envoi resumable — contre un **vrai**
serveur HTTP local, pour vérifier qu'aucun ne part sans corps en comptant sur ce
qu'aiohttp ajouterait de lui-même.

À la différence de Dropbox, les deux appels passent toujours par le paramètre
`json=` d'aiohttp (`google_drive_upload._async_requete`), qui sérialise lui-même
le corps et pose `Content-Type: application/json` : aucune correction n'était
donc nécessaire ici. Ces tests le **prouvent** plutôt que de se fier à la seule
lecture du code.

`pytest-homeassistant-custom-component` interdit la création même d'un socket
(`pytest-socket`) ; la fixture `socket_enabled` la réautorise le temps de ces
tests seulement, et le serveur n'écoute que sur `127.0.0.1`. Aucune valeur
réelle n'y figure : jetons et identifiants sont factices, et rien ne sort de la
machine.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from unittest.mock import patch

import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer
from homeassistant.core import HomeAssistant

from custom_components.auto_backup.destinations.oauth import DestinationOAuth2Session
from custom_components.auto_backup.destinations.providers import google_drive_upload
from custom_components.auto_backup.destinations.providers.google_drive_upload import (
    TeleversementDrive,
    _async_creer_le_dossier,
)
from test_provider_google_drive_upload import DOSSIER, _destination, _entree

pytestmark = pytest.mark.usefixtures("socket_enabled")

DOSSIER_ID_FACTICE = "dossier-drive-factice"
PARENT_FACTICE = "root"


@dataclass
class RequeteRecue:
    """Ce que le serveur local a réellement reçu."""

    chemin: str
    entetes: dict[str, str]
    corps: bytes


@dataclass
class ServeurDrive:
    """Serveur local qui imite Google Drive et garde trace des requêtes reçues."""

    serveur: TestServer
    requetes: list[RequeteRecue] = field(default_factory=list)

    def url(self, chemin: str) -> str:
        """URL locale d'un chemin d'API."""
        return str(self.serveur.make_url(chemin))


@pytest.fixture
async def serveur_drive() -> AsyncIterator[ServeurDrive]:
    """Vrai serveur HTTP sur `127.0.0.1` répondant aux deux points `POST` testés."""
    etat: dict[str, ServeurDrive] = {}

    async def repondre(requete: web.Request) -> web.Response:
        etat["serveur"].requetes.append(
            RequeteRecue(
                chemin=requete.path,
                entetes=dict(requete.headers),
                corps=await requete.read(),
            )
        )
        # Un `id` pour la création de dossier, une `Location` pour l'ouverture
        # de session : chaque appelant ne regarde que ce qui le concerne.
        return web.json_response(
            {"id": "dossier-cree-factice", "name": "peu importe"},
            headers={"Location": "https://exemple.test/session-factice"},
        )

    application = web.Application()
    application.router.add_post("/{chemin:.*}", repondre)
    serveur = TestServer(application, host="127.0.0.1")
    await serveur.start_server()
    etat["serveur"] = ServeurDrive(serveur)
    try:
        yield etat["serveur"]
    finally:
        await serveur.close()


@pytest.fixture
async def session_drive(
    hass: HomeAssistant, integration_backup: None
) -> AsyncIterator[DestinationOAuth2Session]:
    """Session OAuth2 d'une destination Google Drive déjà autorisée.

    Le client HTTP de Home Assistant est remplacé par une `ClientSession` nue,
    comme pour le test Dropbox équivalent : on veut observer ce qu'aiohttp
    ajoute réellement à la requête, pas ce qu'un mock laisse passer.
    """
    await _entree(hass)
    destination = _destination(hass)
    client = ClientSession()
    try:
        with patch.object(
            google_drive_upload, "async_get_clientsession", return_value=client
        ):
            yield destination.session
    finally:
        await client.close()


async def test_l_ouverture_de_session_resumable_envoie_un_corps_json(
    session_drive: DestinationOAuth2Session, serveur_drive: ServeurDrive
) -> None:
    """L'ouverture d'une session d'envoi part avec un corps JSON explicite.

    Le corps est transmis par le paramètre `json=` d'aiohttp
    (`google_drive_upload._async_requete`), qui sérialise lui-même la charge et
    pose `Content-Type: application/json` : ce n'est jamais l'absence de corps
    qui vaudrait à ce point d'entrée le `Content-Type: application/octet-stream`
    qu'aiohttp ajoute d'office à un POST sans corps (issue #67).
    """
    with patch.object(
        google_drive_upload, "URL_ENVOI", serveur_drive.url("/upload/drive/v3/files")
    ):
        envoi = TeleversementDrive(
            session=session_drive, dossier=DOSSIER, dossier_id=DOSSIER_ID_FACTICE
        )
        url_session = await envoi._async_demander_une_session(
            DOSSIER_ID_FACTICE,
            "Sauvegarde [abc123].tar",
            nom="Sauvegarde",
            slug="abc123",
            taille=100,
        )

    assert url_session == "https://exemple.test/session-factice"
    (requete,) = serveur_drive.requetes
    assert requete.chemin == "/upload/drive/v3/files"
    assert requete.entetes["Content-Type"] == "application/json"
    assert requete.entetes["X-Upload-Content-Type"] == "application/x-tar"
    assert requete.entetes["X-Upload-Content-Length"] == "100"
    charge = json.loads(requete.corps)
    assert charge["name"] == "Sauvegarde [abc123].tar"
    assert charge["parents"] == [DOSSIER_ID_FACTICE]
    assert charge["appProperties"]["slug"] == "abc123"


async def test_la_creation_d_un_dossier_envoie_un_corps_json(
    session_drive: DestinationOAuth2Session, serveur_drive: ServeurDrive
) -> None:
    """La création d'un dossier part elle aussi avec un corps JSON explicite.

    Même mécanisme que l'ouverture de session : `json=` fait porter le corps et
    son `Content-Type` par aiohttp, sans dépendre de ce qu'il ajouterait de
    lui-même à une requête qui en serait dépourvue.
    """
    with patch.object(
        google_drive_upload, "URL_FICHIERS", serveur_drive.url("/drive/v3/files")
    ):
        identifiant = await _async_creer_le_dossier(
            session_drive, "Home Assistant", PARENT_FACTICE
        )

    assert identifiant == "dossier-cree-factice"
    (requete,) = serveur_drive.requetes
    assert requete.chemin == "/drive/v3/files"
    assert requete.entetes["Content-Type"] == "application/json"
    charge = json.loads(requete.corps)
    assert charge["name"] == "Home Assistant"
    assert charge["parents"] == [PARENT_FACTICE]
    assert charge["mimeType"] == "application/vnd.google-apps.folder"
