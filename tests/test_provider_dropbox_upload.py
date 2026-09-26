"""Dépôt d'une sauvegarde chez Dropbox (issue #11).

Ces tests couvrent les deux modes d'envoi imposés par l'API — requête unique en
deçà de 150 Mo, session fragmentée au-delà ou quand la taille est inconnue — la
création du dossier cible, la traduction des refus de Dropbox en erreurs typées,
les nouvelles tentatives et ce que la sauvegarde distante renvoie à la rétention.

**Aucune valeur réelle n'y figure** : les identifiants d'application, les jetons
et le compte sont des chaînes reconnaissables (« -factice »), les appels HTTP
sont simulés par `aioclient_mock`, et `asyncio.sleep` est neutralisé pour que
les tentatives ne fassent pas patienter la suite de tests. Rien ne sort du
processus de test.

Deux constantes du module sont réduites par `patch` dans les tests de session :
`SEUIL_ENVOI_SIMPLE` et `TAILLE_FRAGMENT`. Fabriquer 150 Mo d'octets pour
éprouver la fragmentation coûterait plus cher que ce qu'il prouve ; ce sont les
**mécanismes** (offsets, nombre d'appels, validation finale) qui sont vérifiés,
et un test dédié garde la valeur livrée sous surveillance.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import unicodedata
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import (
    ATTR_NAME,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_NAME,
    CONF_TOKEN,
)
from homeassistant.core import HomeAssistant
from homeassistant.core_config import async_process_ha_core_config
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)
from yarl import URL

from custom_components.auto_backup.const import (
    ATTR_DESTINATION,
    ATTR_ERROR,
    ATTR_REMOTE_ID,
    ATTR_SIZE,
    ATTR_UPLOAD_TO,
    CONF_DESTINATION_ID,
    CONF_DESTINATIONS,
    CONF_FOLDER,
    CONF_PROVIDER,
    CONF_PROVIDER_DATA,
    CONF_UPLOAD_TIMEOUT,
    DATA_AUTO_BACKUP,
    DATA_DESTINATIONS,
    DEFAULT_UPLOAD_TIMEOUT,
    DOMAIN,
    EVENT_UPLOAD_FAILED,
    EVENT_UPLOAD_SUCCESSFUL,
    SERVICE_BACKUP,
)
from custom_components.auto_backup.destinations import (
    DestinationAuthError,
    DestinationConfig,
    DestinationError,
    DestinationManager,
    DestinationQuotaError,
    identifiant_du_probleme,
)
from custom_components.auto_backup.destinations.providers.dropbox import (
    CLE_ACCOUNT_ID,
    MULTIPLE_FRAGMENT,
    PROVIDER_DROPBOX,
    SEUIL_ENVOI_SIMPLE,
    SOLIDUS_CONFUSABLES,
    TAILLE_FRAGMENT,
    TENTATIVES_MAX,
    URL_CREATION_DOSSIER,
    URL_ENVOI,
    URL_SESSION_AJOUT,
    URL_SESSION_DEBUT,
    URL_SESSION_FIN,
    DropboxDestination,
    nom_de_fichier_dropbox,
)
from custom_components.auto_backup.destinations.retention import (
    CLE_MARQUEUR,
    porte_le_marqueur,
)

type OuvrirLesOptions = Callable[[str, str], Awaitable[dict[str, Any]]]

MODULE_DROPBOX = "custom_components.auto_backup.destinations.providers.dropbox"

URL_EXTERNE = "https://auto-backup.exemple.test"

CLE_APPLICATION = "cle-application-dropbox-factice"
SECRET_APPLICATION = "secret-application-dropbox-factice"
ACCES = "acces-dropbox-factice-1"
RAFRAICHISSEMENT = "rafraichissement-dropbox-factice"
ACCOUNT_ID = "dbid:compte-factice-0000"
DUREE_JETON = 14400

SECRETS_A_NE_PAS_JOURNALISER = (
    CLE_APPLICATION,
    SECRET_APPLICATION,
    ACCES,
    RAFRAICHISSEMENT,
    ACCOUNT_ID,
)

DESTINATION_ID = "dropbox_jeanne"
NOM_DESTINATION = "Dropbox de Jeanne"
DOSSIER = "Sauvegardes/HA"
DOSSIER_DISTANT = f"/{DOSSIER}"

NOM_SAUVEGARDE = "Sauvegarde du 22"
SLUG = "abc123"
NOM_FICHIER = f"{NOM_SAUVEGARDE} [{SLUG}].tar"
CHEMIN_DISTANT = f"{DOSSIER_DISTANT}/{NOM_FICHIER}"

CONTENU = b"auto-backup-dropbox" * 1000
TAILLE_MORCEAU = 4096

# Délais du garde-fou par requête. Le délai relevé imite une connexion lente
# (deux heures), le délai minuscule sert à observer la coupure sans faire
# patienter la suite de tests.
DELAI_RELEVE = 7200
DELAI_MINUSCULE = 0.05
DUREE_REPONSE_LENTE = 0.5

ID_DISTANT = "id:fichier-factice-0001"
EMPREINTE = "empreinte-factice-0001"
DATE_DEPOT = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)


### Réponses et configuration factices ###


def metadonnees_de_fichier(**surcharges: Any) -> dict[str, Any]:
    """Métadonnées d'un fichier déposé, telles que Dropbox les renvoie."""
    return {
        "id": ID_DISTANT,
        "name": NOM_FICHIER,
        "path_display": CHEMIN_DISTANT,
        "path_lower": CHEMIN_DISTANT.lower(),
        "size": len(CONTENU),
        "server_modified": "2026-09-22T12:00:00Z",
        "client_modified": "2026-09-22T12:00:00Z",
        "content_hash": EMPREINTE,
        "rev": "0123456789abcdef",
    } | surcharges


def erreur_dropbox(resume: str) -> dict[str, Any]:
    """Corps d'erreur de l'API : un résumé et la balise correspondante."""
    return {"error_summary": resume, "error": {".tag": resume.split("/")[0]}}


def config_dropbox(**surcharges: Any) -> dict[str, Any]:
    """Configuration brute d'une destination Dropbox déjà autorisée."""
    return {
        CONF_DESTINATION_ID: DESTINATION_ID,
        CONF_PROVIDER: PROVIDER_DROPBOX,
        CONF_NAME: NOM_DESTINATION,
        CONF_FOLDER: DOSSIER,
        CONF_CLIENT_ID: CLE_APPLICATION,
        CONF_CLIENT_SECRET: SECRET_APPLICATION,
        CONF_TOKEN: {
            "access_token": ACCES,
            "refresh_token": RAFRAICHISSEMENT,
            "token_type": "bearer",
            "expires_in": DUREE_JETON,
            "expires_at": time.time() + DUREE_JETON,
        },
        CONF_PROVIDER_DATA: {CLE_ACCOUNT_ID: ACCOUNT_ID},
    } | surcharges


@pytest.fixture
async def instance_joignable(hass: HomeAssistant) -> None:
    """Donne une URL externe à l'instance, comme le flux d'ajout l'exige."""
    await async_process_ha_core_config(hass, {"external_url": URL_EXTERNE})


@pytest.fixture
async def entree_dropbox(
    hass: HomeAssistant, integration_backup: None, instance_joignable: None
) -> MockConfigEntry:
    """Entrée `auto_backup` portant une destination Dropbox autorisée."""
    entree = MockConfigEntry(
        domain=DOMAIN,
        title="Auto Backup",
        data={},
        options={CONF_DESTINATIONS: [config_dropbox()]},
    )
    entree.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entree.entry_id)
    await hass.async_block_till_done()
    return entree


@pytest.fixture
def destination(
    hass: HomeAssistant, entree_dropbox: MockConfigEntry
) -> DropboxDestination:
    """Destination Dropbox chargée par le gestionnaire."""
    gestionnaire: DestinationManager = hass.data[DATA_DESTINATIONS]
    instance = gestionnaire.async_get(DESTINATION_ID)
    assert isinstance(instance, DropboxDestination)
    return instance


@pytest.fixture
async def destination_configuree(
    hass: HomeAssistant, integration_backup: None, instance_joignable: None
) -> Callable[..., Awaitable[DropboxDestination]]:
    """Fabrique une destination Dropbox dont l'entrée porte ces options.

    Distincte de la fixture `destination` : les tests du garde-fou par requête
    ont besoin de choisir l'option `upload_timeout` **avant** le chargement de
    l'entrée, plutôt que de la réécrire ensuite — une écriture d'options
    recharge le gestionnaire et remplace l'instance de destination.
    """

    async def _construire(
        options: Mapping[str, Any] | None = None,
    ) -> DropboxDestination:
        entree = MockConfigEntry(
            domain=DOMAIN,
            title="Auto Backup",
            data={},
            options={CONF_DESTINATIONS: [config_dropbox()], **(options or {})},
        )
        entree.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entree.entry_id)
        await hass.async_block_till_done()
        gestionnaire: DestinationManager = hass.data[DATA_DESTINATIONS]
        instance = gestionnaire.async_get(DESTINATION_ID)
        assert isinstance(instance, DropboxDestination)
        return instance

    return _construire


@pytest.fixture
def sommeil() -> Any:
    """Neutralise l'attente entre deux tentatives et mémorise ses appels."""
    with patch(f"{MODULE_DROPBOX}.asyncio.sleep", AsyncMock()) as faux_sommeil:
        yield faux_sommeil


### Outils de simulation HTTP ###


async def flux_de(
    contenu: bytes, morceau: int = TAILLE_MORCEAU
) -> AsyncIterator[bytes]:
    """Flux de lecture d'une sauvegarde, découpé comme le fait l'orchestrateur."""
    for debut in range(0, len(contenu), morceau):
        yield contenu[debut : debut + morceau]


async def flux_vide() -> AsyncIterator[bytes]:
    """Flux d'une sauvegarde sans le moindre octet."""
    return
    yield b""  # pragma: no cover - rend la fonction génératrice


def reponse(
    url: str,
    *,
    status: int = 200,
    charge: Any = None,
    texte: str | None = None,
    entetes: dict[str, str] | None = None,
) -> AiohttpClientMockResponse:
    """Réponse simulée, prête à être servie par un `side_effect`."""
    return AiohttpClientMockResponse(
        method="post",
        url=URL(url),
        status=status,
        json=charge,
        text=texte,
        headers=entetes,
    )


def servir(*reponses: AiohttpClientMockResponse) -> Callable[..., Any]:
    """Sert les réponses l'une après l'autre, la dernière valant pour la suite."""
    restantes = list(reponses)

    async def _servir(method: str, url: URL, data: Any) -> AiohttpClientMockResponse:
        return restantes.pop(0) if len(restantes) > 1 else restantes[0]

    return _servir


def servir_en_consommant(
    reponse_finale: AiohttpClientMockResponse, recu: list[bytes]
) -> Callable[..., Any]:
    """Sert une réponse après avoir lu le corps de la requête.

    Le flux d'une sauvegarde n'est lisible que **pendant** la requête : une
    fois le contexte de lecture refermé, le fichier local l'est aussi. Un test
    qui veut vérifier ce qui est parti doit donc le consommer ici, comme le
    ferait un vrai serveur.
    """

    async def _servir(method: str, url: URL, data: Any) -> AiohttpClientMockResponse:
        if data is None:
            recu.append(b"")
        elif isinstance(data, bytes):
            recu.append(data)
        else:
            recu.append(b"".join([morceau async for morceau in data]))
        return reponse_finale

    return _servir


def servir_lentement(
    reponse_finale: AiohttpClientMockResponse, duree: float = DUREE_REPONSE_LENTE
) -> Callable[..., Any]:
    """Sert une réponse après avoir tardé, comme un fournisseur qui traîne."""

    async def _servir(method: str, url: URL, data: Any) -> AiohttpClientMockResponse:
        await asyncio.sleep(duree)
        return reponse_finale

    return _servir


def simuler_l_envoi(
    aioclient_mock: AiohttpClientMocker, charge: Any = None
) -> list[bytes]:
    """Simule `files/upload` en **lisant** le corps de la requête.

    Un vrai serveur consomme le flux qu'on lui envoie ; le simulateur, lui, se
    contente de mémoriser l'objet reçu sans jamais l'itérer. Sans cette lecture,
    le compteur d'octets réellement transmis du fournisseur resterait à zéro, et
    la vérification finale de la taille échouerait sur un dépôt pourtant intact
    — le simulateur mentirait, pas le code testé.

    Renvoie la liste des corps reçus, dans l'ordre, pour les tests qui vérifient
    ce qui est réellement parti.
    """
    recu: list[bytes] = []
    aioclient_mock.post(
        URL_ENVOI,
        side_effect=servir_en_consommant(
            reponse(
                URL_ENVOI,
                charge=metadonnees_de_fichier() if charge is None else charge,
            ),
            recu,
        ),
    )
    return recu


def simuler_le_dossier(
    aioclient_mock: AiohttpClientMocker, *, deja_present: bool = False
) -> None:
    """Simule `files/create_folder_v2`, créé ou déjà présent."""
    if deja_present:
        aioclient_mock.post(
            URL_CREATION_DOSSIER,
            status=409,
            json=erreur_dropbox("path/conflict/folder/..."),
        )
        return
    aioclient_mock.post(
        URL_CREATION_DOSSIER,
        json={
            "metadata": {"id": "id:dossier-factice", "path_display": DOSSIER_DISTANT}
        },
    )


def appels(aioclient_mock: AiohttpClientMocker, url: str) -> list[Any]:
    """Appels simulés vers cette URL, dans l'ordre."""
    return [appel for appel in aioclient_mock.mock_calls if str(appel[1]) == url]


def argument(appel: Any) -> dict[str, Any]:
    """Argument JSON transporté par l'en-tête `Dropbox-API-Arg`."""
    return json.loads(appel[3]["Dropbox-API-Arg"])


async def octets_envoyes(appel: Any) -> bytes:
    """Corps d'une requête, que ce soit un bloc d'octets ou un flux.

    Le simulateur ramène un corps vide à `None` : c'est le cas de `finish`, qui
    valide la session sans transporter la moindre donnée.
    """
    corps = appel[2]
    if corps is None:
        return b""
    if isinstance(corps, bytes):
        return corps
    return b"".join([morceau async for morceau in corps])


async def televerser(
    destination: DropboxDestination,
    *,
    contenu: bytes = CONTENU,
    taille: int | None = -1,
    nom: str = NOM_SAUVEGARDE,
    slug: str | None = SLUG,
    flux: AsyncIterator[bytes] | None = None,
    **extras: Any,
) -> Any:
    """Appelle `async_upload()` comme le fait l'orchestrateur de l'issue #8."""
    return await destination.async_upload(
        None,
        name=nom,
        slug=slug,
        stream=flux if flux is not None else flux_de(contenu),
        size=len(contenu) if taille == -1 else taille,
        filename=f"{nom}.tar",
        **extras,
    )


### Nommage du fichier déposé ###


@pytest.mark.parametrize(
    ("nom", "slug", "attendu"),
    [
        ("Sauvegarde du 22", "abc123", "Sauvegarde du 22 [abc123].tar"),
        # Les caractères que Dropbox refuse dans un nom deviennent « _ ».
        ('Core 2026.9: "test"/prod', "abc", "Core 2026.9_ _test__prod [abc].tar"),
        # Espaces multiples ramenées à une seule, bordures nettoyées.
        ("  Core   2026.9  ", "abc", "Core 2026.9 [abc].tar"),
        # Un nom vide retombe sur le slug, puis sur un nom de repli.
        ("", "abc123", "abc123 [abc123].tar"),
        ("", "", "sauvegarde.tar"),
        # Sans slug, le nom seul suffit à former une archive valide.
        ("Core 2026.9", None, "Core 2026.9.tar"),
        # Un nom qui ne serait que ponctuation refusée ne peut pas rester vide.
        ("...", "abc", "abc [abc].tar"),
    ],
)
def test_le_nom_du_fichier_identifie_la_sauvegarde(
    nom: str, slug: str | None, attendu: str
) -> None:
    """Critère : le nom du fichier déposé porte le nom et le slug, assainis."""
    assert nom_de_fichier_dropbox(nom, slug) == attendu


def test_le_nom_du_fichier_est_borne_en_longueur() -> None:
    """Un nom démesuré est tronqué, sans perdre le slug ni le suffixe."""
    obtenu = nom_de_fichier_dropbox("nom" * 500, SLUG)

    assert len(obtenu) <= 200
    assert obtenu.endswith(f" [{SLUG}].tar")


def test_le_nom_du_fichier_ne_peut_pas_sortir_du_dossier() -> None:
    """Un nom de sauvegarde ne doit jamais pouvoir changer le chemin visé."""
    obtenu = nom_de_fichier_dropbox("../../etc/passwd", "a/b")

    assert "/" not in obtenu
    assert "\\" not in obtenu


def test_le_nom_du_fichier_neutralise_les_confusables() -> None:
    """La normalisation NFKC précède le filtrage, comme pour le dossier."""
    # U+FF0F est une barre oblique pleine chasse : elle vaut « / » après NFKC.
    obtenu = nom_de_fichier_dropbox("dossier\uff0ffichier", SLUG)

    assert obtenu.startswith("dossier_fichier ")


@pytest.mark.parametrize(
    "confusable",
    [
        pytest.param(caractere, id=f"U+{ord(caractere):04X}")
        for caractere in SOLIDUS_CONFUSABLES
    ],
)
def test_aucun_confusable_de_barre_oblique_ne_survit(confusable: str) -> None:
    """NFKC ne neutralise pas tous les confusables du séparateur de chemin.

    Elle ne ramène à « / » et « \\ » que les formes de compatibilité (U+FF0F,
    U+FF3C, U+FE68). La barre oblique de division (U+2215), la barre de fraction
    (U+2044), le grand solidus (U+29F8) et leurs voisins la traversent intacts :
    ils doivent donc être filtrés explicitement, dans le nom comme dans le slug,
    faute de quoi le fichier déposé afficherait ce qui ressemble à un séparateur
    de chemin.
    """
    assert unicodedata.normalize("NFKC", confusable) == confusable, (
        "ce caractère est déjà neutralisé par NFKC : il n'a rien à faire dans "
        "SOLIDUS_CONFUSABLES"
    )

    assert (
        nom_de_fichier_dropbox(f"avant{confusable}apres", SLUG)
        == f"avant_apres [{SLUG}].tar"
    )
    assert nom_de_fichier_dropbox(NOM_SAUVEGARDE, f"a{confusable}b") == (
        f"{NOM_SAUVEGARDE} [a_b].tar"
    )


def test_le_repli_se_fonde_sur_le_nom_d_archive_propose() -> None:
    """Sans nom ni slug, le nom d'archive de l'orchestrateur prend le relais."""
    assert nom_de_fichier_dropbox("", None, "Core_2026_9.tar") == "Core_2026_9.tar"


### Envoi simple ###


async def test_une_petite_sauvegarde_part_en_une_seule_requete(
    destination: DropboxDestination, aioclient_mock: AiohttpClientMocker
) -> None:
    """Critère : en deçà de 150 Mo, une seule requête `files/upload`."""
    simuler_le_dossier(aioclient_mock)
    recu = simuler_l_envoi(aioclient_mock)

    await televerser(destination)

    envois = appels(aioclient_mock, URL_ENVOI)
    assert len(envois) == 1
    assert not appels(aioclient_mock, URL_SESSION_DEBUT)
    assert recu == [CONTENU]

    entetes = envois[0][3]
    assert entetes["Content-Type"] == "application/octet-stream"
    # La taille annoncée évite un corps en « chunked », que les points d'entrée
    # de contenu de Dropbox ne garantissent pas.
    assert entetes["Content-Length"] == str(len(CONTENU))
    assert argument(envois[0]) == {
        "path": CHEMIN_DISTANT,
        "mode": "add",
        "autorename": False,
        "mute": True,
    }


async def test_un_nom_accentue_reste_transportable_par_un_en_tete(
    destination: DropboxDestination, aioclient_mock: AiohttpClientMocker
) -> None:
    """L'argument voyage dans un en-tête HTTP : il doit rester en ASCII pur.

    Un nom de sauvegarde accentué est parfaitement légitime ; recopié tel quel
    dans `Dropbox-API-Arg`, il produirait un en-tête non ASCII que le transport
    refuse. Dropbox attend précisément un JSON échappé.
    """
    simuler_le_dossier(aioclient_mock)
    simuler_l_envoi(aioclient_mock)

    await televerser(destination, nom="Sauvegarde d'été")

    brut = appels(aioclient_mock, URL_ENVOI)[0][3]["Dropbox-API-Arg"]
    assert brut.isascii()
    # Le nom reste pourtant intact une fois le JSON décodé.
    assert (
        json.loads(brut)["path"] == f"{DOSSIER_DISTANT}/Sauvegarde d'été [{SLUG}].tar"
    )


async def test_le_seuil_de_la_session_est_celui_de_l_api() -> None:
    """Le basculement se fait bien à 150 Mo, et le fragment est un multiple."""
    assert SEUIL_ENVOI_SIMPLE == 150 * 1024 * 1024
    assert TAILLE_FRAGMENT == 8 * 1024 * 1024
    assert TAILLE_FRAGMENT % MULTIPLE_FRAGMENT == 0


### Session fragmentée ###


async def test_une_grosse_sauvegarde_passe_par_une_session_fragmentee(
    destination: DropboxDestination, aioclient_mock: AiohttpClientMocker
) -> None:
    """Critère : au-delà du seuil, une session par fragments de taille fixe."""
    fragment = 8192
    contenu = b"x" * (fragment * 2 + 100)
    simuler_le_dossier(aioclient_mock)
    aioclient_mock.post(URL_SESSION_DEBUT, json={"session_id": "session-factice"})
    aioclient_mock.post(URL_SESSION_AJOUT, text="")
    aioclient_mock.post(URL_SESSION_FIN, json=metadonnees_de_fichier(size=len(contenu)))

    with (
        patch(f"{MODULE_DROPBOX}.SEUIL_ENVOI_SIMPLE", fragment),
        patch(f"{MODULE_DROPBOX}.TAILLE_FRAGMENT", fragment),
    ):
        distante = await televerser(destination, contenu=contenu)

    assert not appels(aioclient_mock, URL_ENVOI)

    debuts = appels(aioclient_mock, URL_SESSION_DEBUT)
    ajouts = appels(aioclient_mock, URL_SESSION_AJOUT)
    fins = appels(aioclient_mock, URL_SESSION_FIN)
    assert (len(debuts), len(ajouts), len(fins)) == (1, 2, 1)

    # Le premier fragment ouvre la session, les suivants s'ajoutent à l'offset
    # exact déjà reçu par Dropbox.
    assert await octets_envoyes(debuts[0]) == contenu[:fragment]
    assert argument(ajouts[0])["cursor"]["offset"] == fragment
    assert await octets_envoyes(ajouts[0]) == contenu[fragment : fragment * 2]
    assert argument(ajouts[1])["cursor"]["offset"] == fragment * 2
    assert await octets_envoyes(ajouts[1]) == contenu[fragment * 2 :]

    # `finish` valide le dépôt à l'emplacement voulu, tout le contenu reçu.
    arguments_de_fin = argument(fins[0])
    assert arguments_de_fin["cursor"] == {
        "session_id": "session-factice",
        "offset": len(contenu),
    }
    assert arguments_de_fin["commit"] == {
        "path": CHEMIN_DISTANT,
        "mode": "add",
        "autorename": False,
        "mute": True,
    }
    assert await octets_envoyes(fins[0]) == b""
    assert distante.size == len(contenu)


async def test_une_taille_inconnue_passe_par_une_session(
    destination: DropboxDestination, aioclient_mock: AiohttpClientMocker
) -> None:
    """Critère : sans taille annoncée, la session est le seul mode sûr."""
    simuler_le_dossier(aioclient_mock)
    aioclient_mock.post(URL_SESSION_DEBUT, json={"session_id": "session-factice"})
    aioclient_mock.post(URL_SESSION_FIN, json=metadonnees_de_fichier())

    await televerser(destination, taille=None)

    assert not appels(aioclient_mock, URL_ENVOI)
    debuts = appels(aioclient_mock, URL_SESSION_DEBUT)
    assert len(debuts) == 1
    assert await octets_envoyes(debuts[0]) == CONTENU
    assert argument(appels(aioclient_mock, URL_SESSION_FIN)[0])["cursor"] == {
        "session_id": "session-factice",
        "offset": len(CONTENU),
    }


async def test_une_sauvegarde_vide_ouvre_tout_de_meme_la_session(
    destination: DropboxDestination, aioclient_mock: AiohttpClientMocker
) -> None:
    """Un flux sans octet produit un fichier vide, pas une erreur."""
    simuler_le_dossier(aioclient_mock)
    aioclient_mock.post(URL_SESSION_DEBUT, json={"session_id": "session-factice"})
    aioclient_mock.post(URL_SESSION_FIN, json=metadonnees_de_fichier(size=0))

    distante = await televerser(destination, taille=None, flux=flux_vide())

    assert len(appels(aioclient_mock, URL_SESSION_DEBUT)) == 1
    assert argument(appels(aioclient_mock, URL_SESSION_FIN)[0])["cursor"]["offset"] == 0
    assert distante.size == 0


async def test_une_session_sans_identifiant_est_refusee(
    destination: DropboxDestination, aioclient_mock: AiohttpClientMocker
) -> None:
    """Une réponse d'ouverture inexploitable ne part pas en envoi aveugle."""
    simuler_le_dossier(aioclient_mock)
    aioclient_mock.post(URL_SESSION_DEBUT, json={})

    with pytest.raises(DestinationError, match="identifiant"):
        await televerser(destination, taille=None)


async def test_la_taille_finale_est_verifiee(
    destination: DropboxDestination, aioclient_mock: AiohttpClientMocker
) -> None:
    """Critère : un dépôt dont la taille ne correspond pas est un échec."""
    simuler_le_dossier(aioclient_mock)
    aioclient_mock.post(URL_SESSION_DEBUT, json={"session_id": "session-factice"})
    aioclient_mock.post(URL_SESSION_FIN, json=metadonnees_de_fichier(size=12))

    with pytest.raises(DestinationError, match="incomplet"):
        await televerser(destination, taille=None)


async def test_l_envoi_simple_compte_les_octets_reellement_transmis(
    destination: DropboxDestination, aioclient_mock: AiohttpClientMocker
) -> None:
    """La voie simple compte le flux, elle ne recopie pas la taille annoncée.

    Elle reprenait l'annonce de l'appelant comme nombre d'octets envoyés : la
    vérification finale comparait alors cette annonce à elle-même dès que
    Dropbox était d'accord avec le flux réel. Ici Dropbox confirme exactement ce
    que le flux contenait, et c'est l'appelant qui se trompe : l'écart doit être
    imputé à l'annonce, et le message citer les octets réellement lus.
    """
    simuler_le_dossier(aioclient_mock)
    recu = simuler_l_envoi(aioclient_mock)

    with pytest.raises(DestinationError) as echec:
        await televerser(destination, taille=10)

    message = str(echec.value)
    assert f"{len(CONTENU)} octets ont été lus" in message
    assert "10 octets annoncés" in message
    # Le flux est bien parti en entier : ce n'est pas une troncature réseau.
    assert recu == [CONTENU]


async def test_un_depot_tronque_par_dropbox_est_detecte_en_envoi_simple(
    destination: DropboxDestination, aioclient_mock: AiohttpClientMocker
) -> None:
    """La voie simple compare aussi ce que Dropbox a enregistré au flux envoyé.

    Pendant de `test_la_taille_finale_est_verifiee`, qui n'éprouvait que la
    session : les deux voies vérifient désormais la même chose, de la même
    façon, et distinguent une annonce fausse d'un transfert interrompu.
    """
    simuler_le_dossier(aioclient_mock)
    simuler_l_envoi(aioclient_mock, metadonnees_de_fichier(size=12))

    with pytest.raises(DestinationError, match="Dropbox a enregistré 12 octets"):
        await televerser(destination)


async def test_une_taille_annoncee_fausse_est_aussi_detectee_en_session(
    destination: DropboxDestination, aioclient_mock: AiohttpClientMocker
) -> None:
    """La session vérifie elle aussi l'annonce, et pas seulement ses fragments."""
    fragment = 8192
    contenu = b"z" * (fragment * 2 + 7)
    simuler_le_dossier(aioclient_mock)
    aioclient_mock.post(URL_SESSION_DEBUT, json={"session_id": "session-factice"})
    aioclient_mock.post(URL_SESSION_AJOUT, text="")
    aioclient_mock.post(URL_SESSION_FIN, json=metadonnees_de_fichier(size=len(contenu)))

    with (
        patch(f"{MODULE_DROPBOX}.SEUIL_ENVOI_SIMPLE", fragment),
        patch(f"{MODULE_DROPBOX}.TAILLE_FRAGMENT", fragment),
        pytest.raises(DestinationError, match="octets annoncés"),
    ):
        await televerser(destination, contenu=contenu, taille=len(contenu) + 1)


async def test_une_taille_annoncee_negative_passe_par_la_session(
    destination: DropboxDestination, aioclient_mock: AiohttpClientMocker
) -> None:
    """Une taille négative ne dit rien : elle vaut une taille inconnue.

    Elle ne doit ni servir de `Content-Length`, ni être confrontée aux octets
    envoyés — elle ferait échouer un dépôt pourtant intact.
    """
    simuler_le_dossier(aioclient_mock)
    aioclient_mock.post(URL_SESSION_DEBUT, json={"session_id": "session-factice"})
    aioclient_mock.post(URL_SESSION_FIN, json=metadonnees_de_fichier())

    distante = await televerser(destination, taille=-1000)

    assert not appels(aioclient_mock, URL_ENVOI)
    assert distante.size == len(CONTENU)


### Dossier cible ###


async def test_le_dossier_cible_est_cree_avant_le_transfert(
    destination: DropboxDestination, aioclient_mock: AiohttpClientMocker
) -> None:
    """Critère : le dossier configuré est créé s'il n'existe pas."""
    simuler_le_dossier(aioclient_mock)
    simuler_l_envoi(aioclient_mock)

    await televerser(destination)

    creations = appels(aioclient_mock, URL_CREATION_DOSSIER)
    assert len(creations) == 1
    assert json.loads(creations[0][2]) == {
        "path": DOSSIER_DISTANT,
        "autorename": False,
    }
    assert creations[0][3]["Content-Type"] == "application/json"


async def test_un_dossier_deja_present_n_interrompt_rien(
    destination: DropboxDestination, aioclient_mock: AiohttpClientMocker
) -> None:
    """Critère : `path/conflict/folder` est le cas normal, pas une erreur."""
    simuler_le_dossier(aioclient_mock, deja_present=True)
    simuler_l_envoi(aioclient_mock)

    distante = await televerser(destination)

    assert distante.remote_id == ID_DISTANT


async def test_un_dossier_impossible_a_creer_arrete_le_televersement(
    destination: DropboxDestination, aioclient_mock: AiohttpClientMocker
) -> None:
    """Un refus qui n'est pas un conflit de dossier échoue avant tout transfert."""
    aioclient_mock.post(
        URL_CREATION_DOSSIER,
        status=409,
        json=erreur_dropbox("path/malformed_path/..."),
    )

    with pytest.raises(DestinationError, match="malformed_path"):
        await televerser(destination)

    assert not appels(aioclient_mock, URL_ENVOI)


### Refus de Dropbox ###


async def test_un_conflit_de_nom_n_ecrase_jamais_le_fichier_existant(
    destination: DropboxDestination, aioclient_mock: AiohttpClientMocker
) -> None:
    """Critère : un nom déjà pris lève une erreur explicite, en français."""
    simuler_le_dossier(aioclient_mock)
    aioclient_mock.post(
        URL_ENVOI, status=409, json=erreur_dropbox("path/conflict/file/...")
    )

    with pytest.raises(DestinationError) as echec:
        await televerser(destination)

    message = str(echec.value)
    assert "existe déjà" in message
    assert "n'écrase jamais" in message
    assert CHEMIN_DISTANT in message
    assert not isinstance(echec.value, DestinationQuotaError)


async def test_un_espace_insuffisant_est_une_erreur_de_quota(
    destination: DropboxDestination, aioclient_mock: AiohttpClientMocker
) -> None:
    """Critère : `insufficient_space` devient `DestinationQuotaError`."""
    simuler_le_dossier(aioclient_mock)
    aioclient_mock.post(
        URL_ENVOI, status=409, json=erreur_dropbox("path/insufficient_space/...")
    )

    with pytest.raises(DestinationQuotaError, match="saturé"):
        await televerser(destination)


async def test_un_espace_insuffisant_n_est_jamais_rejoue(
    destination: DropboxDestination,
    aioclient_mock: AiohttpClientMocker,
    sommeil: Any,
) -> None:
    """Un `507` a beau être un `5xx`, un disque plein ne se vide pas tout seul."""
    simuler_le_dossier(aioclient_mock)
    aioclient_mock.post(
        URL_SESSION_DEBUT,
        status=507,
        json=erreur_dropbox("path/insufficient_space/..."),
    )

    with pytest.raises(DestinationQuotaError, match="saturé"):
        await televerser(destination, taille=None)

    assert len(appels(aioclient_mock, URL_SESSION_DEBUT)) == 1
    sommeil.assert_not_awaited()


async def test_un_corps_d_erreur_qui_n_est_pas_un_objet_reste_exploitable(
    destination: DropboxDestination, aioclient_mock: AiohttpClientMocker
) -> None:
    """Une réponse d'erreur hors contrat ne fait pas dérailler la traduction."""
    simuler_le_dossier(aioclient_mock)
    aioclient_mock.post(URL_ENVOI, status=400, text="[]")

    with pytest.raises(DestinationError, match="HTTP 400"):
        await televerser(destination)


async def test_un_acces_refuse_demande_une_reautorisation(
    hass: HomeAssistant,
    destination: DropboxDestination,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Critère : un `401` lève `DestinationAuthError` et crée le problème."""
    simuler_le_dossier(aioclient_mock)
    aioclient_mock.post(
        URL_ENVOI, status=401, json=erreur_dropbox("expired_access_token/...")
    )

    with pytest.raises(DestinationAuthError, match="refuse l'accès"):
        await televerser(destination)

    registre = ir.async_get(hass)
    assert registre.async_get_issue(DOMAIN, identifiant_du_probleme(DESTINATION_ID))


async def test_un_refus_inattendu_reste_une_erreur_de_destination(
    hass: HomeAssistant,
    destination: DropboxDestination,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Un `400` ne remet pas l'autorisation en cause : rien à ré-autoriser."""
    simuler_le_dossier(aioclient_mock)
    aioclient_mock.post(
        URL_ENVOI, status=400, json=erreur_dropbox("path/malformed_path/...")
    )

    with pytest.raises(DestinationError, match="HTTP 400"):
        await televerser(destination)

    registre = ir.async_get(hass)
    assert not registre.async_get_issue(DOMAIN, identifiant_du_probleme(DESTINATION_ID))


### Nouvelles tentatives ###


async def test_une_limitation_de_debit_est_respectee_puis_l_envoi_reussit(
    destination: DropboxDestination,
    aioclient_mock: AiohttpClientMocker,
    sommeil: Any,
) -> None:
    """Critère : un `429` est rejoué après le délai demandé par Dropbox."""
    simuler_le_dossier(aioclient_mock)
    aioclient_mock.post(URL_SESSION_DEBUT, json={"session_id": "session-factice"})
    aioclient_mock.post(
        URL_SESSION_FIN,
        side_effect=servir(
            reponse(
                URL_SESSION_FIN,
                status=429,
                charge=erreur_dropbox("too_many_requests/..."),
                entetes={"Retry-After": "7"},
            ),
            reponse(URL_SESSION_FIN, charge=metadonnees_de_fichier()),
        ),
    )

    distante = await televerser(destination, taille=None)

    assert distante.remote_id == ID_DISTANT
    assert len(appels(aioclient_mock, URL_SESSION_FIN)) == 2
    sommeil.assert_awaited_once_with(7.0)


async def test_le_delai_demande_est_plafonne(
    destination: DropboxDestination,
    aioclient_mock: AiohttpClientMocker,
    sommeil: Any,
) -> None:
    """Critère : la limite maximale prime sur un `Retry-After` démesuré."""
    simuler_le_dossier(aioclient_mock)
    aioclient_mock.post(
        URL_SESSION_DEBUT,
        side_effect=servir(
            reponse(
                URL_SESSION_DEBUT,
                status=429,
                charge=erreur_dropbox("too_many_requests/..."),
                entetes={"Retry-After": "3600"},
            ),
            reponse(URL_SESSION_DEBUT, charge={"session_id": "session-factice"}),
        ),
    )
    aioclient_mock.post(URL_SESSION_FIN, json=metadonnees_de_fichier())

    await televerser(destination, taille=None)

    sommeil.assert_awaited_once_with(60.0)


async def test_une_erreur_serveur_est_rejouee_avec_un_delai_croissant(
    destination: DropboxDestination,
    aioclient_mock: AiohttpClientMocker,
    sommeil: Any,
) -> None:
    """Critère : un `5xx` est rejoué, avec une attente bornée et croissante."""
    simuler_le_dossier(aioclient_mock)
    aioclient_mock.post(
        URL_SESSION_DEBUT,
        side_effect=servir(
            reponse(URL_SESSION_DEBUT, status=503, texte="service indisponible"),
            reponse(URL_SESSION_DEBUT, status=500, texte="erreur interne"),
            reponse(URL_SESSION_DEBUT, charge={"session_id": "session-factice"}),
        ),
    )
    aioclient_mock.post(URL_SESSION_FIN, json=metadonnees_de_fichier())

    distante = await televerser(destination, taille=None)

    assert distante.remote_id == ID_DISTANT
    assert len(appels(aioclient_mock, URL_SESSION_DEBUT)) == 3
    assert [appel.args[0] for appel in sommeil.await_args_list] == [1.0, 2.0]


async def test_les_tentatives_sont_bornees(
    destination: DropboxDestination,
    aioclient_mock: AiohttpClientMocker,
    sommeil: Any,
) -> None:
    """Critère : au-delà de la limite, l'échec est signalé sans s'éterniser."""
    simuler_le_dossier(aioclient_mock)
    aioclient_mock.post(
        URL_SESSION_DEBUT,
        status=429,
        json=erreur_dropbox("too_many_requests/..."),
        headers={"Retry-After": "2"},
    )

    with pytest.raises(DestinationError, match="limite les appels"):
        await televerser(destination, taille=None)

    assert len(appels(aioclient_mock, URL_SESSION_DEBUT)) == TENTATIVES_MAX
    assert sommeil.await_count == TENTATIVES_MAX - 1


async def test_un_retry_after_illisible_retombe_sur_le_delai_par_defaut(
    destination: DropboxDestination,
    aioclient_mock: AiohttpClientMocker,
    sommeil: Any,
) -> None:
    """Un en-tête inexploitable n'empêche pas de réessayer raisonnablement."""
    simuler_le_dossier(aioclient_mock)
    aioclient_mock.post(
        URL_SESSION_DEBUT,
        side_effect=servir(
            reponse(
                URL_SESSION_DEBUT,
                status=429,
                charge=erreur_dropbox("too_many_requests/..."),
                entetes={"Retry-After": "bientot"},
            ),
            reponse(URL_SESSION_DEBUT, charge={"session_id": "session-factice"}),
        ),
    )
    aioclient_mock.post(URL_SESSION_FIN, json=metadonnees_de_fichier())

    await televerser(destination, taille=None)

    sommeil.assert_awaited_once_with(1.0)


async def test_un_envoi_simple_n_est_pas_rejoue(
    destination: DropboxDestination,
    aioclient_mock: AiohttpClientMocker,
    sommeil: Any,
) -> None:
    """Le flux d'une sauvegarde ne se lit qu'une fois : rien à renvoyer.

    C'est la limite assumée de l'envoi simple, et la raison d'être de la
    session fragmentée, dont chaque fragment est encore en mémoire.
    """
    simuler_le_dossier(aioclient_mock)
    aioclient_mock.post(
        URL_ENVOI,
        status=429,
        json=erreur_dropbox("too_many_requests/..."),
        headers={"Retry-After": "2"},
    )

    with pytest.raises(DestinationError, match="limite les appels"):
        await televerser(destination)

    assert len(appels(aioclient_mock, URL_ENVOI)) == 1
    sommeil.assert_not_awaited()


### Garde-fou d'une requête de transfert ###


@pytest.mark.parametrize(
    ("options", "attendu"),
    [
        # Option absente : la valeur livrée par défaut fait office de budget.
        ({}, float(DEFAULT_UPLOAD_TIMEOUT)),
        # Relevée pour une connexion lente, c'est elle qui vaut, et non 1800 s.
        ({CONF_UPLOAD_TIMEOUT: DELAI_RELEVE}, float(DELAI_RELEVE)),
        # Abaissée : le garde-fou suit aussi vers le bas.
        ({CONF_UPLOAD_TIMEOUT: 120}, 120.0),
        # Hors contrat ou nulle : repli, jamais une requête sans borne.
        ({CONF_UPLOAD_TIMEOUT: "jamais"}, float(DEFAULT_UPLOAD_TIMEOUT)),
        ({CONF_UPLOAD_TIMEOUT: 0}, float(DEFAULT_UPLOAD_TIMEOUT)),
    ],
)
async def test_le_garde_fou_par_requete_suit_le_delai_configure(
    destination_configuree: Callable[..., Awaitable[DropboxDestination]],
    options: Mapping[str, Any],
    attendu: float,
) -> None:
    """Le garde-fou d'une requête vaut le budget global, jamais une constante.

    Il était figé sur la valeur livrée par défaut : un utilisateur relevant
    `upload_timeout` pour une connexion lente voyait sa requête unique coupée
    au bout de trente minutes, alors que son budget global ne l'était pas.
    """
    destination = await destination_configuree(options)

    assert destination._delai_de_requete == attendu


async def test_le_garde_fou_par_requete_se_replie_sans_entree(
    hass: HomeAssistant,
) -> None:
    """Sans entrée de configuration, le garde-fou vaut la valeur livrée.

    Une destination construite hors du gestionnaire n'est reliée à aucune entrée :
    l'option `upload_timeout` est alors introuvable, et une requête sans borne
    serait pire que le repli.
    """
    destination = DropboxDestination(
        hass, DestinationConfig.from_dict(config_dropbox())
    )

    assert destination._delai_de_requete == float(DEFAULT_UPLOAD_TIMEOUT)


async def test_le_garde_fou_par_requete_ne_depasse_pas_le_budget_global(
    destination_configuree: Callable[..., Awaitable[DropboxDestination]],
) -> None:
    """Le garde-fou reste inférieur ou égal au budget global du coordinateur.

    C'est ce qui garantit que le coordinateur tranche le premier : un garde-fou
    plus généreux que le budget serait inopérant.
    """
    coordinateur_et_requete = [
        (delai, await destination_configuree({CONF_UPLOAD_TIMEOUT: delai}))
        for delai in (60, DEFAULT_UPLOAD_TIMEOUT, DELAI_RELEVE)
    ]

    for budget, destination in coordinateur_et_requete:
        assert destination._delai_de_requete <= float(budget)


async def test_une_requete_qui_traine_est_coupee_par_le_delai_configure(
    destination_configuree: Callable[..., Awaitable[DropboxDestination]],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Le garde-fou configuré coupe pour de bon une requête qui ne rend rien.

    Le réglage est réduit à une fraction de seconde et la réponse simulée tarde
    bien davantage : c'est la borne par requête qui doit trancher, avec le
    message que l'utilisateur lira dans l'événement d'échec.
    """
    destination = await destination_configuree({CONF_UPLOAD_TIMEOUT: DELAI_MINUSCULE})
    aioclient_mock.post(
        URL_CREATION_DOSSIER,
        side_effect=servir_lentement(
            reponse(URL_CREATION_DOSSIER, charge={"metadata": {}})
        ),
    )

    with pytest.raises(DestinationError, match="temps imparti"):
        await televerser(destination)

    assert not appels(aioclient_mock, URL_ENVOI)


async def test_une_requete_lente_aboutit_sous_un_delai_confortable(
    destination_configuree: Callable[..., Awaitable[DropboxDestination]],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """La même lenteur ne gêne pas quand le réglage laisse le temps de répondre.

    Symétrique du test précédent : le garde-fou ne coupe rien de légitime.
    """
    destination = await destination_configuree({CONF_UPLOAD_TIMEOUT: DELAI_RELEVE})
    aioclient_mock.post(
        URL_CREATION_DOSSIER,
        side_effect=servir_lentement(
            reponse(URL_CREATION_DOSSIER, charge={"metadata": {}})
        ),
    )
    simuler_l_envoi(aioclient_mock)

    distante = await televerser(destination)

    assert distante.remote_id == ID_DISTANT


### Sauvegarde distante renvoyée ###


async def test_la_sauvegarde_distante_porte_ce_qu_attend_la_retention(
    destination: DropboxDestination, aioclient_mock: AiohttpClientMocker
) -> None:
    """Critère : identifiant, chemin, taille, date et métadonnées sont fournis."""
    simuler_le_dossier(aioclient_mock)
    simuler_l_envoi(aioclient_mock)

    distante = await televerser(destination)

    assert distante.remote_id == ID_DISTANT
    assert distante.name == NOM_FICHIER
    assert distante.slug == SLUG
    assert distante.size == len(CONTENU)
    assert distante.created_at == DATE_DEPOT
    assert distante.path == CHEMIN_DISTANT
    # Le marqueur distingue les sauvegardes déposées par l'intégration de tout
    # autre fichier du dossier. Sa clé n'a qu'une définition, celle de la
    # rétention distante : le dépôt est relu par `porte_le_marqueur()` lui-même,
    # de sorte qu'un renommage côté rétention casse ce test au lieu d'orpheliner
    # en silence tous les dépôts Dropbox.
    assert distante.metadata == {
        "slug": SLUG,
        "content_hash": EMPREINTE,
        CLE_MARQUEUR: True,
    }
    assert porte_le_marqueur(distante)


async def test_une_taille_non_numerique_est_ignoree(
    destination: DropboxDestination, aioclient_mock: AiohttpClientMocker
) -> None:
    """Une taille hors contrat n'invente rien et ne fait pas échouer le dépôt."""
    simuler_le_dossier(aioclient_mock)
    simuler_l_envoi(aioclient_mock, metadonnees_de_fichier(size="beaucoup"))

    distante = await televerser(destination)

    assert distante.size is None


async def test_une_reponse_sans_identifiant_est_refusee(
    destination: DropboxDestination, aioclient_mock: AiohttpClientMocker
) -> None:
    """Sans identifiant distant, rien ne pourra plus désigner la sauvegarde."""
    simuler_le_dossier(aioclient_mock)
    charge = metadonnees_de_fichier()
    del charge["id"]
    simuler_l_envoi(aioclient_mock, charge)

    with pytest.raises(DestinationError, match="identifiant"):
        await televerser(destination)


async def test_un_televersement_sans_flux_est_refuse(
    destination: DropboxDestination,
) -> None:
    """La sauvegarde est toujours lue en flux, y compris sous Supervisor."""
    with pytest.raises(DestinationError, match="flux"):
        await destination.async_upload("/backup/ha.tar", name=NOM_SAUVEGARDE)


### Journaux ###


async def test_aucun_jeton_ni_en_tete_d_autorisation_dans_les_journaux(
    destination: DropboxDestination,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Critère : un téléversement en `debug` ne divulgue ni jeton ni en-tête."""
    simuler_le_dossier(aioclient_mock)
    simuler_l_envoi(aioclient_mock)

    with caplog.at_level(logging.DEBUG):
        await televerser(destination)

    journaux = caplog.text
    assert journaux, "le téléversement doit journaliser quelque chose en debug"
    assert "Authorization" not in journaux
    assert "Bearer" not in journaux
    for secret in SECRETS_A_NE_PAS_JOURNALISER:
        assert secret not in journaux


async def test_un_echec_ne_divulgue_pas_davantage(
    destination: DropboxDestination,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
    sommeil: Any,
) -> None:
    """Les tentatives et l'échec final restent muets sur l'autorisation."""
    simuler_le_dossier(aioclient_mock)
    aioclient_mock.post(URL_SESSION_DEBUT, status=503, text="service indisponible")

    with caplog.at_level(logging.DEBUG), pytest.raises(DestinationError):
        await televerser(destination, taille=None)

    journaux = caplog.text
    assert "nouvelle tentative" in journaux
    assert "Authorization" not in journaux
    assert "Bearer" not in journaux
    for secret in SECRETS_A_NE_PAS_JOURNALISER:
        assert secret not in journaux


### Intégration : du service au dépôt ###


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


@pytest.fixture
def sauvegarde_locale(tmp_path: Path) -> Path:
    """Fichier `.tar` local tenant lieu de sauvegarde Home Assistant."""
    chemin = tmp_path / f"{SLUG}.tar"
    chemin.write_bytes(CONTENU)
    return chemin


async def test_le_service_backup_depose_la_sauvegarde_chez_dropbox(
    hass: HomeAssistant,
    entree_dropbox: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    sauvegarde_locale: Path,
) -> None:
    """Critère : `upload_to` vers Dropbox émet `upload_successful` avec l'id."""
    handler = hass.data[DATA_AUTO_BACKUP]._handler
    handler._manager = _faux_backup_manager(sauvegarde_locale)
    handler.create_backup = AsyncMock(return_value={"slug": SLUG})

    simuler_le_dossier(aioclient_mock)
    recu = simuler_l_envoi(aioclient_mock)

    succes = async_capture_events(hass, EVENT_UPLOAD_SUCCESSFUL)
    echecs = async_capture_events(hass, EVENT_UPLOAD_FAILED)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_BACKUP,
        {ATTR_NAME: NOM_SAUVEGARDE, ATTR_UPLOAD_TO: DESTINATION_ID},
        blocking=True,
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    assert not echecs
    assert len(succes) == 1
    assert succes[0].data[ATTR_DESTINATION] == DESTINATION_ID
    assert succes[0].data[ATTR_REMOTE_ID] == ID_DISTANT
    assert succes[0].data[ATTR_SIZE] == len(CONTENU)

    envois = appels(aioclient_mock, URL_ENVOI)
    assert len(envois) == 1
    # Le nom du fichier déposé identifie la sauvegarde créée par le service.
    assert argument(envois[0])["path"] == CHEMIN_DISTANT
    assert recu == [CONTENU]


async def test_un_echec_de_depot_emet_un_message_comprehensible(
    hass: HomeAssistant,
    entree_dropbox: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    sauvegarde_locale: Path,
) -> None:
    """Critère : l'événement d'échec porte un message lisible par l'utilisateur."""
    handler = hass.data[DATA_AUTO_BACKUP]._handler
    handler._manager = _faux_backup_manager(sauvegarde_locale)
    handler.create_backup = AsyncMock(return_value={"slug": SLUG})

    simuler_le_dossier(aioclient_mock)
    aioclient_mock.post(
        URL_ENVOI, status=507, json=erreur_dropbox("path/insufficient_space/...")
    )

    echecs = async_capture_events(hass, EVENT_UPLOAD_FAILED)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_BACKUP,
        {ATTR_NAME: NOM_SAUVEGARDE, ATTR_UPLOAD_TO: DESTINATION_ID},
        blocking=True,
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    assert len(echecs) == 1
    message = echecs[0].data[ATTR_ERROR]
    assert "saturé" in message
    assert NOM_DESTINATION in message
