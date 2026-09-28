"""En-têtes réellement émis vers Dropbox par aiohttp (issue #67).

`aioclient_mock` enregistre les en-têtes que le code **passe** à aiohttp, pas
ceux qu'aiohttp **envoie** : il ne voit donc pas le `Content-Type:
application/octet-stream` qu'aiohttp ajoute d'office à un POST sans corps, et que
Dropbox refuse sur un point RPC (`400 Bad Request`). Ces tests montent un vrai
serveur HTTP sur `127.0.0.1`, y redirigent les URL de Dropbox, et inspectent la
requête telle qu'elle arrive sur le fil.

`pytest-homeassistant-custom-component` interdit la création même d'un socket
(`pytest-socket`) ; la fixture `socket_enabled` la réautorise le temps de ces
tests seulement, et le serveur n'écoute que sur `127.0.0.1`.

Aucune valeur réelle n'y figure : jetons et identifiants sont factices, et rien
ne sort de la machine.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer
from homeassistant.core import HomeAssistant
from homeassistant.core_config import async_process_ha_core_config
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.auto_backup.const import (
    CONF_DESTINATIONS,
    DATA_DESTINATIONS,
    DOMAIN,
)
from custom_components.auto_backup.destinations import DestinationManager
from custom_components.auto_backup.destinations.providers import dropbox
from custom_components.auto_backup.destinations.providers.dropbox import (
    DropboxDestination,
)
from test_provider_dropbox_upload import DESTINATION_ID, URL_EXTERNE, config_dropbox

pytestmark = pytest.mark.usefixtures("socket_enabled")

REPONSE_COMPTE = {
    "account_id": "dbid:compte-factice",
    "email": "jeanne@exemple.test",
    "name": {"display_name": "Jeanne Factice"},
}


@dataclass
class RequeteRecue:
    """Ce que le serveur local a réellement reçu."""

    chemin: str
    entetes: dict[str, str]
    corps: bytes


@dataclass
class ServeurDropbox:
    """Serveur local qui répond comme Dropbox et garde trace des requêtes."""

    serveur: TestServer
    requetes: list[RequeteRecue] = field(default_factory=list)

    def url(self, chemin: str) -> str:
        """URL locale d'un chemin d'API."""
        return str(self.serveur.make_url(chemin))


@pytest.fixture
async def serveur_dropbox() -> AsyncIterator[ServeurDropbox]:
    """Vrai serveur HTTP sur `127.0.0.1` imitant les points RPC et de contenu."""
    etat: dict[str, ServeurDropbox] = {}

    async def repondre(requete: web.Request) -> web.Response:
        etat["serveur"].requetes.append(
            RequeteRecue(
                chemin=requete.path,
                entetes=dict(requete.headers),
                corps=await requete.read(),
            )
        )
        # Même règle que Dropbox sur un point RPC : un `Content-Type` autre que
        # JSON (ou texte) est refusé.
        type_recu = requete.headers.get("Content-Type")
        if requete.path.startswith("/2/users/") and type_recu not in (
            None,
            "application/json",
        ):
            return web.Response(
                status=400,
                text=f'Bad HTTP "Content-Type" header: {type_recu}',
            )
        return web.json_response(REPONSE_COMPTE)

    application = web.Application()
    application.router.add_post("/{chemin:.*}", repondre)
    serveur = TestServer(application, host="127.0.0.1")
    await serveur.start_server()
    etat["serveur"] = ServeurDropbox(serveur)
    try:
        yield etat["serveur"]
    finally:
        await serveur.close()


@pytest.fixture
async def destination(
    hass: HomeAssistant, integration_backup: None
) -> AsyncIterator[DropboxDestination]:
    """Destination Dropbox autorisée, dont le client HTTP est un vrai aiohttp.

    La session de Home Assistant est remplacée par une `ClientSession` nue : on
    veut précisément observer ce qu'aiohttp ajoute de lui-même.
    """
    await async_process_ha_core_config(hass, {"external_url": URL_EXTERNE})
    entree = MockConfigEntry(
        domain=DOMAIN,
        title="Auto Backup",
        data={},
        options={CONF_DESTINATIONS: [config_dropbox()]},
    )
    entree.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entree.entry_id)
    await hass.async_block_till_done()
    gestionnaire: DestinationManager = hass.data[DATA_DESTINATIONS]
    instance = gestionnaire.async_get(DESTINATION_ID)
    assert isinstance(instance, DropboxDestination)

    client = ClientSession()
    try:
        with patch.object(dropbox, "async_get_clientsession", return_value=client):
            yield instance
    finally:
        await client.close()


async def test_l_identification_du_compte_envoie_un_corps_json_null(
    destination: DropboxDestination, serveur_dropbox: ServeurDropbox
) -> None:
    """`users/get_current_account` part avec `null` en `application/json`.

    Sur l'ancien code (POST sans corps), aiohttp émettait `Content-Type:
    application/octet-stream` : le serveur local, comme Dropbox, répondait 400 et
    l'identification échouait.
    """
    url = serveur_dropbox.url("/2/users/get_current_account")
    with patch.object(dropbox, "URL_COMPTE", url):
        assert await destination.async_nom_par_defaut() is not None

    (requete,) = serveur_dropbox.requetes
    assert requete.chemin == "/2/users/get_current_account"
    assert requete.entetes["Content-Type"] == "application/json"
    assert requete.corps == b"null"
    assert requete.entetes["Content-Length"] == "4"


async def test_un_appel_rpc_avec_argument_envoie_son_json(
    destination: DropboxDestination, serveur_dropbox: ServeurDropbox
) -> None:
    """Les appels du cycle de vie portent leur charge JSON et son type."""
    url = serveur_dropbox.url("/2/files/delete_v2")
    reponse = await destination._async_appel_json(url, {"path": "id:factice"})

    assert reponse.statut == 200
    (requete,) = serveur_dropbox.requetes
    assert requete.entetes["Content-Type"] == "application/json"
    assert requete.corps == b'{"path": "id:factice"}'


async def test_un_fragment_vide_part_en_octet_stream_explicite(
    destination: DropboxDestination, serveur_dropbox: ServeurDropbox
) -> None:
    """Un point de contenu au fragment vide garde son type et son argument.

    C'est le cas de `upload_session/finish`, et du `start` d'une sauvegarde
    vide : le corps est vide, mais le `Content-Type` binaire est **déclaré** par
    le code, comme Dropbox l'attend sur ses points de contenu, et non ajouté par
    hasard par aiohttp.
    """
    url = serveur_dropbox.url("/2/files/upload_session/finish")
    argument: dict[str, Any] = {"cursor": {"session_id": "s", "offset": 0}}
    await destination._async_envoyer(
        url, argument, b"", chemin="/factice", rejouable=True
    )

    (requete,) = serveur_dropbox.requetes
    assert requete.entetes["Content-Type"] == "application/octet-stream"
    assert requete.entetes["Content-Length"] == "0"
    assert requete.corps == b""
    assert "Dropbox-API-Arg" in requete.entetes


async def test_une_requete_sans_corps_est_refusee_avant_de_partir(
    destination: DropboxDestination, serveur_dropbox: ServeurDropbox
) -> None:
    """Aucune requête Dropbox ne peut partir sans corps explicite."""
    with pytest.raises(ValueError, match="corps explicite"):
        await destination._async_requete(
            serveur_dropbox.url("/2/users/get_current_account"),
            entetes={},
            corps=None,
            delai=5,
        )
    assert serveur_dropbox.requetes == []
