"""Listage, suppression et purge des sauvegardes Dropbox (issue #12).

Ces tests couvrent le cycle de vie d'une sauvegarde déjà déposée : le listage
**paginé** du dossier de la destination, la reconnaissance de ce qu'Auto Backup a
lui-même déposé, la suppression idempotente, la traduction des refus de Dropbox
en erreurs typées, et la purge distante de bout en bout par le service
`auto_backup.purge`.

La provenance est le cœur du sujet. L'API Dropbox v2 n'offre **aucune métadonnée
libre** sur un fichier : le marqueur `auto_backup` ne survit pas au dépôt, et le
listage doit reconstituer la provenance depuis le registre persistant de #9 puis,
à défaut, depuis la convention de nommage « <nom> [<slug>].tar » posée par #11.
Les deux voies sont éprouvées séparément, et le fait qu'un fichier étranger ou un
dossier ne soit **jamais** ni listé ni supprimé l'est de bout en bout.

**Aucune valeur réelle n'y figure** : les identifiants d'application, les jetons
et le compte sont des chaînes reconnaissables (« -factice »), les appels HTTP sont
simulés par `aioclient_mock`, et `asyncio.sleep` est neutralisé pour que les
nouvelles tentatives ne fassent pas patienter la suite de tests.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import (
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_NAME,
    CONF_TOKEN,
)
from homeassistant.core import HomeAssistant
from homeassistant.core_config import async_process_ha_core_config
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
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
    ATTR_DESTINATION_NAME,
    ATTR_REMOTE_IDS,
    CONF_DESTINATION_ID,
    CONF_DESTINATIONS,
    CONF_FOLDER,
    CONF_PROVIDER,
    CONF_PROVIDER_DATA,
    CONF_RETENTION_COUNT,
    CONF_RETENTION_DAYS,
    DATA_DESTINATIONS,
    DATA_REMOTE_BACKUPS,
    DOMAIN,
    EVENT_REMOTE_PURGE,
    SERVICE_PURGE,
)
from custom_components.auto_backup.destinations import (
    DestinationAuthError,
    DestinationError,
    DestinationManager,
    DestinationNotFoundError,
    identifiant_du_probleme,
)
from custom_components.auto_backup.destinations.providers.dropbox import (
    CLE_ACCOUNT_ID,
    LIMITE_PAR_PAGE,
    PROVIDER_DROPBOX,
    TENTATIVES_MAX,
    URL_LISTAGE,
    URL_LISTAGE_SUITE,
    URL_SUPPRESSION,
    DropboxDestination,
    nom_de_fichier_dropbox,
    slug_de_la_convention,
)
from custom_components.auto_backup.destinations.retention import (
    EntreeRegistre,
    RegistreSauvegardesDistantes,
    porte_le_marqueur,
)

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

# Trois sauvegardes déposées par le fork, une par page du listage simulé.
SLUGS = ("aaa11111", "bbb22222", "ccc33333")
IDS = tuple(f"id:fichier-factice-{numero}" for numero in range(1, 4))
NOMS = tuple(
    f"Sauvegarde {numero} [{slug}].tar" for numero, slug in enumerate(SLUGS, 1)
)

TAILLE = 4096

# Réponse lente et délai minuscule : de quoi observer le garde-fou du listage
# sans faire patienter la suite de tests.
DUREE_REPONSE_LENTE = 0.5
DELAI_MINUSCULE = 0.05


### Réponses et configuration factices ###


def entree_de_fichier(
    identifiant: str,
    nom: str,
    *,
    taille: int | None = TAILLE,
    date: str | None = None,
    **surcharges: Any,
) -> dict[str, Any]:
    """Entrée de `files/list_folder` décrivant un fichier."""
    entree: dict[str, Any] = {
        ".tag": "file",
        "id": identifiant,
        "name": nom,
        "path_display": f"{DOSSIER_DISTANT}/{nom}",
        "path_lower": f"{DOSSIER_DISTANT}/{nom}".lower(),
        "size": taille,
        "server_modified": date or "2026-09-22T12:00:00Z",
        "client_modified": date or "2026-09-22T12:00:00Z",
        "content_hash": "empreinte-factice",
        "rev": "0123456789abcdef",
    }
    return entree | surcharges


def entree_de_dossier(nom: str) -> dict[str, Any]:
    """Entrée de `files/list_folder` décrivant un sous-dossier."""
    return {
        ".tag": "folder",
        "id": f"id:dossier-{nom}",
        "name": nom,
        "path_display": f"{DOSSIER_DISTANT}/{nom}",
    }


def page(
    *entrees: dict[str, Any], curseur: str | None = None, suite: bool = False
) -> dict[str, Any]:
    """Page de listage : ses entrées, son curseur et s'il en reste."""
    return {
        "entries": list(entrees),
        # Une chaîne vide est un cas de test à part entière (une page qui annonce
        # une suite sans dire où la chercher) : seule l'absence prend le défaut.
        "cursor": "curseur-factice" if curseur is None else curseur,
        "has_more": suite,
    }


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
async def demarrer(
    hass: HomeAssistant, integration_backup: None, instance_joignable: None
) -> Callable[..., Awaitable[MockConfigEntry]]:
    """Fabrique une entrée `auto_backup` portant une destination Dropbox.

    La rétention se choisit **avant** le chargement : réécrire les options
    ensuite rechargerait le gestionnaire et remplacerait l'instance de
    destination sous les pieds du test.
    """

    async def _demarrer(**retention: Any) -> MockConfigEntry:
        entree = MockConfigEntry(
            domain=DOMAIN,
            title="Auto Backup",
            data={},
            options={CONF_DESTINATIONS: [config_dropbox(**retention)]},
        )
        entree.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entree.entry_id)
        await hass.async_block_till_done()
        return entree

    return _demarrer


@pytest.fixture
async def destination(
    hass: HomeAssistant, demarrer: Callable[..., Awaitable[MockConfigEntry]]
) -> DropboxDestination:
    """Destination Dropbox chargée par le gestionnaire, sans rétention."""
    await demarrer()
    return _destination(hass)


@pytest.fixture
def sommeil() -> Any:
    """Neutralise l'attente entre deux tentatives et mémorise ses appels."""
    with patch(f"{MODULE_DROPBOX}.asyncio.sleep", AsyncMock()) as faux_sommeil:
        yield faux_sommeil


def _destination(hass: HomeAssistant) -> DropboxDestination:
    """Destination Dropbox chargée pour cette instance."""
    gestionnaire: DestinationManager = hass.data[DATA_DESTINATIONS]
    instance = gestionnaire.async_get(DESTINATION_ID)
    assert isinstance(instance, DropboxDestination)
    return instance


def _registre(hass: HomeAssistant) -> RegistreSauvegardesDistantes:
    """Registre persistant des sauvegardes distantes."""
    registre = hass.data[DATA_REMOTE_BACKUPS]
    assert isinstance(registre, RegistreSauvegardesDistantes)
    return registre


async def _inscrire(
    hass: HomeAssistant,
    identifiant: str,
    nom: str,
    *,
    slug: str | None = None,
    age_en_jours: int = 0,
) -> None:
    """Inscrit une sauvegarde au registre, comme l'aurait fait un dépôt réussi."""
    await _registre(hass).async_enregistrer(
        DESTINATION_ID,
        EntreeRegistre(
            remote_id=identifiant,
            name=nom,
            slug=slug,
            created_at=dt_util.utcnow() - timedelta(days=age_en_jours),
            size=TAILLE,
        ),
    )


### Outils de simulation HTTP ###


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


def servir_lentement(
    reponse_finale: AiohttpClientMockResponse, duree: float = DUREE_REPONSE_LENTE
) -> Callable[..., Any]:
    """Sert une réponse après avoir tardé, comme un fournisseur qui traîne."""

    async def _servir(method: str, url: URL, data: Any) -> AiohttpClientMockResponse:
        await asyncio.sleep(duree)
        return reponse_finale

    return _servir


def simuler_le_listage(
    aioclient_mock: AiohttpClientMocker, *pages: dict[str, Any]
) -> None:
    """Simule un listage : la première page, puis les suivantes en cascade."""
    premiere, *suivantes = pages
    aioclient_mock.post(URL_LISTAGE, json=premiere)
    if suivantes:
        aioclient_mock.post(
            URL_LISTAGE_SUITE,
            side_effect=servir(
                *(reponse(URL_LISTAGE_SUITE, charge=page_) for page_ in suivantes)
            ),
        )


def simuler_la_suppression(aioclient_mock: AiohttpClientMocker) -> None:
    """Simule `files/delete_v2` : la suppression aboutit."""
    aioclient_mock.post(
        URL_SUPPRESSION,
        json={"metadata": {".tag": "file", "id": IDS[0], "name": NOMS[0]}},
    )


def appels(aioclient_mock: AiohttpClientMocker, url: str) -> list[Any]:
    """Appels simulés vers cette URL, dans l'ordre."""
    return [appel for appel in aioclient_mock.mock_calls if str(appel[1]) == url]


def argument(appel: Any) -> dict[str, Any]:
    """Corps JSON d'un appel RPC simulé."""
    return json.loads(appel[2])


### Listage paginé ###


async def test_le_listage_interroge_le_dossier_de_la_destination(
    destination: DropboxDestination, aioclient_mock: AiohttpClientMocker
) -> None:
    """Le listage porte sur le dossier configuré, sans descendre en dessous.

    Une descente récursive ferait entrer dans le périmètre de la rétention tout
    ce que l'utilisateur range sous le dossier de la destination.
    """
    simuler_le_listage(aioclient_mock, page(entree_de_fichier(IDS[0], NOMS[0])))

    await destination.async_list_backups()

    (appel,) = appels(aioclient_mock, URL_LISTAGE)
    assert argument(appel) == {
        "path": DOSSIER_DISTANT,
        "recursive": False,
        "limit": LIMITE_PAR_PAGE,
        "include_deleted": False,
        "include_media_info": False,
        "include_mounted_folders": False,
    }


async def test_le_listage_parcourt_toutes_les_pages(
    destination: DropboxDestination, aioclient_mock: AiohttpClientMocker
) -> None:
    """Critère : les sauvegardes au-delà de la première page sont renvoyées.

    Trois pages, chacune portant une sauvegarde : une rétention qui s'arrêterait
    à la première conserverait indéfiniment les deux autres.
    """
    simuler_le_listage(
        aioclient_mock,
        page(entree_de_fichier(IDS[0], NOMS[0]), curseur="curseur-1", suite=True),
        page(entree_de_fichier(IDS[1], NOMS[1]), curseur="curseur-2", suite=True),
        page(entree_de_fichier(IDS[2], NOMS[2])),
    )

    sauvegardes = await destination.async_list_backups()

    assert [sauvegarde.remote_id for sauvegarde in sauvegardes] == list(IDS)
    # Le curseur de chaque page est bien celui que la précédente a rendu.
    suites = appels(aioclient_mock, URL_LISTAGE_SUITE)
    assert [argument(appel)["cursor"] for appel in suites] == [
        "curseur-1",
        "curseur-2",
    ]


async def test_chaque_sauvegarde_porte_identifiant_nom_taille_et_date(
    destination: DropboxDestination, aioclient_mock: AiohttpClientMocker
) -> None:
    """Critère : le listage rend tout ce dont la rétention a besoin."""
    simuler_le_listage(
        aioclient_mock,
        page(entree_de_fichier(IDS[0], NOMS[0], date="2026-09-20T08:30:00Z")),
    )

    (sauvegarde,) = await destination.async_list_backups()

    assert sauvegarde.remote_id == IDS[0]
    assert sauvegarde.name == NOMS[0]
    assert sauvegarde.size == TAILLE
    assert sauvegarde.created_at == dt_util.parse_datetime("2026-09-20T08:30:00Z")
    assert sauvegarde.path == f"{DOSSIER_DISTANT}/{NOMS[0]}"
    assert sauvegarde.slug == SLUGS[0]


async def test_une_page_qui_ne_rend_pas_de_curseur_termine_le_listage(
    destination: DropboxDestination, aioclient_mock: AiohttpClientMocker
) -> None:
    """Un `has_more` sans curseur exploitable arrête le parcours.

    Suivre une page suivante sans curseur ferait redemander la première
    indéfiniment : mieux vaut rendre ce qui est connu.
    """
    simuler_le_listage(
        aioclient_mock,
        page(entree_de_fichier(IDS[0], NOMS[0]), curseur="", suite=True),
    )

    sauvegardes = await destination.async_list_backups()

    assert [sauvegarde.remote_id for sauvegarde in sauvegardes] == [IDS[0]]
    assert not appels(aioclient_mock, URL_LISTAGE_SUITE)


async def test_le_listage_borne_le_nombre_de_pages_parcourues(
    destination: DropboxDestination,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Un curseur qui ne se termine jamais ne fait pas tourner le listage sans fin.

    Le contrat de `RemoteDestination` demande au fournisseur de borner lui-même
    ses appels : le filet du coordinateur de purge n'a pas à servir ici.
    """
    aioclient_mock.post(
        URL_LISTAGE,
        json=page(entree_de_fichier(IDS[0], NOMS[0]), curseur="c", suite=True),
    )
    aioclient_mock.post(
        URL_LISTAGE_SUITE,
        json=page(entree_de_fichier(IDS[1], NOMS[1]), curseur="c", suite=True),
    )

    with patch(f"{MODULE_DROPBOX}.PAGES_MAX", 2), caplog.at_level(logging.WARNING):
        sauvegardes = await destination.async_list_backups()

    assert len(sauvegardes) == 2
    assert len(appels(aioclient_mock, URL_LISTAGE_SUITE)) == 1
    assert "tronqué" in caplog.text
    assert NOM_DESTINATION in caplog.text


async def test_un_listage_qui_traine_est_coupe_par_le_garde_fou(
    destination: DropboxDestination, aioclient_mock: AiohttpClientMocker
) -> None:
    """Le listage entier est borné, et pas seulement chaque requête.

    Sans cette borne, une pagination lente dépasserait le filet du coordinateur
    de purge, qui couperait alors à la place du fournisseur.
    """
    aioclient_mock.post(
        URL_LISTAGE,
        side_effect=servir_lentement(
            reponse(URL_LISTAGE, charge=page(entree_de_fichier(IDS[0], NOMS[0])))
        ),
    )

    # Le message est celui du listage entier, et non celui d'une requête isolée :
    # les deux parlent de « temps imparti », seule la phrase les distingue.
    with (
        patch(f"{MODULE_DROPBOX}.DELAI_LISTAGE", DELAI_MINUSCULE),
        pytest.raises(DestinationError, match=r"listage.*n'a pas abouti"),
    ):
        await destination.async_list_backups()


async def test_un_dossier_encore_absent_donne_un_listage_vide(
    destination: DropboxDestination, aioclient_mock: AiohttpClientMocker
) -> None:
    """Un dossier qui n'existe pas n'est pas une erreur : rien n'a été déposé."""
    aioclient_mock.post(
        URL_LISTAGE, status=409, json=erreur_dropbox("path/not_found/.")
    )

    assert await destination.async_list_backups() == []


async def test_une_reponse_de_listage_inexploitable_est_refusee(
    destination: DropboxDestination, aioclient_mock: AiohttpClientMocker
) -> None:
    """Une page sans liste d'entrées est une erreur, pas un dossier vide.

    La confondre avec un dossier vide ferait croire à la rétention que toutes
    les sauvegardes ont disparu.
    """
    aioclient_mock.post(URL_LISTAGE, json={"cursor": "c", "has_more": False})

    with pytest.raises(DestinationError, match="inexploitable"):
        await destination.async_list_backups()


async def test_un_refus_de_listage_est_une_erreur_de_destination(
    destination: DropboxDestination,
    aioclient_mock: AiohttpClientMocker,
    sommeil: Any,
) -> None:
    """Une panne passagère de Dropbox est rejouée, puis abandonnée proprement."""
    aioclient_mock.post(URL_LISTAGE, status=503, text="service indisponible")

    with pytest.raises(DestinationError, match="panne passagère") as echec:
        await destination.async_list_backups()

    assert "listage" in str(echec.value)
    assert len(appels(aioclient_mock, URL_LISTAGE)) == TENTATIVES_MAX


async def test_une_limitation_de_debit_persistante_est_signalee_telle_quelle(
    destination: DropboxDestination,
    aioclient_mock: AiohttpClientMocker,
    sommeil: Any,
) -> None:
    """Un `429` que les tentatives n'épuisent pas reste une erreur passagère.

    Le message le dit — la prochaine purge reprendra — et surtout il ne demande
    **pas** de ré-autoriser : une limitation de débit n'est pas un accès refusé.
    """
    aioclient_mock.post(
        URL_LISTAGE, status=429, json=erreur_dropbox("too_many_requests/...")
    )

    with pytest.raises(DestinationError, match="limite les appels") as echec:
        await destination.async_list_backups()

    assert not isinstance(echec.value, DestinationAuthError)
    assert len(appels(aioclient_mock, URL_LISTAGE)) == TENTATIVES_MAX


### Provenance : registre, convention de nommage, fichiers étrangers ###


async def test_ni_un_fichier_etranger_ni_un_dossier_ne_sont_listes(
    destination: DropboxDestination,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Critère : seules les sauvegardes créées par Auto Backup sont renvoyées.

    Le dossier contient aussi une photo, une archive au nom quelconque et un
    sous-dossier : aucun ne doit jamais devenir supprimable.
    """
    simuler_le_listage(
        aioclient_mock,
        page(
            entree_de_fichier("id:photo", "vacances.jpg"),
            entree_de_fichier("id:archive", "documents.tar"),
            entree_de_fichier("id:presque", "Sauvegarde [].tar"),
            entree_de_dossier("Anciennes"),
            entree_de_fichier(IDS[0], NOMS[0]),
        ),
    )

    with caplog.at_level(logging.DEBUG):
        sauvegardes = await destination.async_list_backups()

    assert [sauvegarde.remote_id for sauvegarde in sauvegardes] == [IDS[0]]
    assert "vacances.jpg" in caplog.text
    assert "Anciennes" in caplog.text


async def test_le_registre_reconnait_une_sauvegarde_au_nom_hors_convention(
    hass: HomeAssistant,
    destination: DropboxDestination,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Critère : la provenance vient d'abord du registre du fork.

    Le nom ne suit pas la convention — un dépôt sans slug, par exemple — mais le
    registre sait que cette sauvegarde est la sienne : elle reste purgeable.
    """
    await _inscrire(hass, "id:sans-slug", "Sauvegarde.tar", slug="zzz99999")
    simuler_le_listage(
        aioclient_mock, page(entree_de_fichier("id:sans-slug", "Sauvegarde.tar"))
    )

    (sauvegarde,) = await destination.async_list_backups()

    assert sauvegarde.remote_id == "id:sans-slug"
    # Le slug du registre est repris : le nom ne le porte pas.
    assert sauvegarde.slug == "zzz99999"
    assert sauvegarde.metadata["provenance"] == "registre"


async def test_le_nom_suffit_a_reconnaitre_une_sauvegarde_hors_registre(
    destination: DropboxDestination, aioclient_mock: AiohttpClientMocker
) -> None:
    """Critère : à défaut de registre, la convention de nommage établit la provenance.

    C'est ce qui rend purgeable l'**orphelin** d'un dépôt qui a abouti chez
    Dropbox mais que le fork a rapporté en échec : rien ne l'a inscrit au
    registre, et aucune métadonnée ne survit au dépôt chez Dropbox.
    """
    simuler_le_listage(aioclient_mock, page(entree_de_fichier(IDS[0], NOMS[0])))

    (sauvegarde,) = await destination.async_list_backups()

    assert sauvegarde.remote_id == IDS[0]
    assert sauvegarde.slug == SLUGS[0]
    assert sauvegarde.metadata["provenance"] == "nom"


async def test_toute_sauvegarde_listee_porte_le_marqueur_de_la_retention(
    hass: HomeAssistant,
    destination: DropboxDestination,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Le marqueur atteste la provenance que le listage vient d'établir.

    Il n'a pas été relu chez Dropbox, qui ne sait pas le stocker : c'est le
    fournisseur qui le pose sur ce qu'il a reconnu, et sans lui la purge
    refuserait de toucher une sauvegarde absente du registre.
    """
    await _inscrire(hass, IDS[1], NOMS[1], slug=SLUGS[1])
    simuler_le_listage(
        aioclient_mock,
        page(
            entree_de_fichier(IDS[0], NOMS[0]),
            entree_de_fichier(IDS[1], NOMS[1]),
        ),
    )

    sauvegardes = await destination.async_list_backups()

    assert len(sauvegardes) == 2
    assert all(porte_le_marqueur(sauvegarde) for sauvegarde in sauvegardes)


async def test_un_registre_indisponible_laisse_le_nom_decider(
    hass: HomeAssistant,
    destination: DropboxDestination,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Sans registre chargé, la convention de nommage reste seule juge.

    Le registre ne vit que le temps d'une entrée de configuration chargée : le
    listage doit alors retomber sur son autre indice de provenance, et non
    échouer ni tout considérer comme étranger.
    """
    hass.data.pop(DATA_REMOTE_BACKUPS)
    simuler_le_listage(
        aioclient_mock,
        page(
            entree_de_fichier(IDS[0], NOMS[0]),
            entree_de_fichier("id:photo", "vacances.jpg"),
        ),
    )

    (sauvegarde,) = await destination.async_list_backups()

    assert sauvegarde.remote_id == IDS[0]
    assert sauvegarde.metadata["provenance"] == "nom"


async def test_une_entree_inexploitable_n_interrompt_pas_le_listage(
    destination: DropboxDestination, aioclient_mock: AiohttpClientMocker
) -> None:
    """Une entrée sans identifiant est écartée, les autres sont listées.

    Écarter au lieu d'échouer est délibéré : une entrée inattendue ne doit pas
    empêcher la rétention de faire son travail sur les autres — et une entrée
    qu'on n'a pas su identifier ne doit surtout pas devenir supprimable.
    """
    simuler_le_listage(
        aioclient_mock,
        page(
            entree_de_fichier("", NOMS[1]),
            {".tag": "file", "id": IDS[2]},
            "pas un dictionnaire",
            entree_de_fichier(IDS[0], NOMS[0]),
        ),
    )

    sauvegardes = await destination.async_list_backups()

    assert [sauvegarde.remote_id for sauvegarde in sauvegardes] == [IDS[0]]


async def test_une_taille_inexploitable_ne_disqualifie_pas_la_sauvegarde(
    destination: DropboxDestination, aioclient_mock: AiohttpClientMocker
) -> None:
    """Une taille absente laisse la sauvegarde listée, sans taille connue."""
    simuler_le_listage(
        aioclient_mock,
        page(entree_de_fichier(IDS[0], NOMS[0], taille=None, server_modified="")),
    )

    (sauvegarde,) = await destination.async_list_backups()

    assert sauvegarde.size is None
    assert sauvegarde.created_at is None


@pytest.mark.parametrize(
    ("nom", "slug"),
    [
        ("Sauvegarde du soir", "a1b2c3d4"),
        ("Core 2026.9.1", "abc"),
        ("Sauvegarde [ancienne]", "deadbeef"),
        ("Sauvegardé à Noël", "éàü"),
        ("N" * 400, "0123456789"),
        # Slug « inhabituel » : ponctuation, espace et accent mêlés — rien de
        # tout cela n'est un caractère hexadécimal, mais `_assaini()` ne filtre
        # que ce que Dropbox refuse dans un nom de fichier, pas ce qu'un slug
        # « normal » contiendrait.
        ("Sauvegarde du matin", "a1-b_2.c 3!éàü"),
    ],
)
def test_la_convention_reconnait_exactement_ce_que_le_depot_produit(
    nom: str, slug: str
) -> None:
    """Le listage reconnaît les noms que le dépôt (#11) fabrique, et leur slug.

    Les deux fonctions sont le seul lien entre un fichier déposé et sa
    provenance chez un fournisseur qui n'en garde aucune trace : les comparer
    ici évite qu'une évolution du nommage ne rende en silence les dépôts
    précédents méconnaissables — donc jamais purgés.
    """
    nom_depose = nom_de_fichier_dropbox(nom, slug)

    assert slug_de_la_convention(nom_depose) == slug


@pytest.mark.parametrize(
    "nom",
    [
        "",
        "vacances.jpg",
        "documents.tar",
        "Sauvegarde [].tar",
        "[].tar",
        "[a1b2c3d4].tar",
        "Sauvegarde [a1b2c3d4].tar.txt",
        "Sauvegarde a1b2c3d4.tar",
        "Sauvegarde [a1b2c3d4]",
        # Crochets imbriqués : le groupe capturé ne peut pas contenir de
        # crochet ouvrant, donc aucune des deux lectures possibles du nom
        # n'aboutit à un groupe valide suivi de « .tar ».
        "Sauvegarde [[imbriqué]].tar",
        "Sauvegarde [ext[erne]].tar",
        f"Sauvegarde [{'s' * 80}].tar",
        "S" * 250 + " [a1b2c3d4].tar",
    ],
)
def test_la_convention_refuse_ce_que_le_depot_ne_produit_pas(nom: str) -> None:
    """Un nom qui ne suit pas la convention n'établit aucune provenance."""
    assert slug_de_la_convention(nom) is None


def test_un_depot_sans_slug_n_est_reconnaissable_que_par_le_registre() -> None:
    """Limite assumée : sans slug, le nom déposé ne porte plus de crochets.

    Le cas ne se produit que si Home Assistant ne donne pas de slug à la
    sauvegarde ; le registre reste alors la seule voie, et le test précédent
    montre qu'elle suffit.
    """
    assert slug_de_la_convention(nom_de_fichier_dropbox("Sauvegarde")) is None


### Risque assumé : un nom d'utilisateur conforme à la convention ###


@pytest.mark.parametrize(
    "nom",
    [
        # Un nom de vacances suivi d'une année entre crochets : rien à voir
        # avec un dépôt d'Auto Backup, et pourtant la forme est exactement
        # celle que `slug_de_la_convention()` reconnaît.
        "photos [2026].tar",
        "backup [x].tar",
    ],
)
def test_un_nom_d_utilisateur_conforme_a_la_convention_reste_reconnu(
    nom: str,
) -> None:
    """Risque assumé et documenté : la forme du nom est le seul juge.

    `docs/destinations/dropbox.md` (« Ce qui n'est jamais touché ») avertit
    explicitement l'utilisateur : un fichier qu'il aurait lui-même nommé
    « … [quelque chose].tar » serait pris pour une sauvegarde d'Auto Backup.
    Ce test ne dénonce pas un défaut — il fige la frontière exacte du risque
    accepté, pour qu'une évolution du motif ne l'élargisse pas en silence : le
    risque reste borné au dossier de la destination (jamais récursif) et à
    cette forme précise, ni plus ni moins permissive.
    """
    assert slug_de_la_convention(nom) is not None


async def test_la_purge_supprime_un_nom_d_utilisateur_conforme_a_la_convention(
    hass: HomeAssistant,
    demarrer: Callable[..., Awaitable[MockConfigEntry]],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Conséquence de bout en bout du risque assumé ci-dessus.

    Le fichier n'a jamais été déposé par Auto Backup (il n'est pas au
    registre) mais son nom suit la convention par coïncidence : la purge le
    supprime comme n'importe quel orphelin. C'est exactement ce que la
    documentation demande à l'utilisateur d'éviter en ne nommant pas ses
    propres fichiers de cette façon.
    """
    await demarrer(**{CONF_RETENTION_DAYS: 1})
    ancienne = dt_util.utcnow() - timedelta(days=30)
    simuler_le_listage(
        aioclient_mock,
        page(entree_de_fichier(IDS[0], "photos [2026].tar", date=ancienne.isoformat())),
    )
    simuler_la_suppression(aioclient_mock)

    await hass.services.async_call(DOMAIN, SERVICE_PURGE, blocking=True)
    await hass.async_block_till_done()

    supprimes = [
        argument(appel)["path"] for appel in appels(aioclient_mock, URL_SUPPRESSION)
    ]
    assert supprimes == [IDS[0]]


### Renommage d'un fichier déjà déposé ###


async def test_un_orphelin_renomme_cesse_d_etre_reconnu(
    destination: DropboxDestination, aioclient_mock: AiohttpClientMocker
) -> None:
    """Conséquence assumée, documentée dans les « Limites connues ».

    L'orphelin n'est pas au registre : sa seule voie de reconnaissance est son
    nom. Une fois renommé chez Dropbox en dehors de la convention, plus rien
    ne le rattache au fork — il redevient un fichier étranger comme un autre,
    et ne sera plus jamais purgé automatiquement.
    """
    simuler_le_listage(
        aioclient_mock, page(entree_de_fichier(IDS[0], "mon fichier renommé"))
    )

    assert await destination.async_list_backups() == []


async def test_un_fichier_du_registre_reste_reconnu_apres_renommage(
    hass: HomeAssistant,
    destination: DropboxDestination,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Nuance de la limite ci-dessus : le registre, lui, survit au renommage.

    L'identifiant Dropbox est stable même si le fichier est déplacé ou
    renommé (cf. `async_delete_backup()`) : une sauvegarde inscrite au
    registre par son `remote_id` reste donc reconnue quel que soit son nom
    actuel. Seul l'orphelin reconnu par son seul nom perd la partie.
    """
    await _inscrire(hass, IDS[0], NOMS[0], slug=SLUGS[0])
    simuler_le_listage(
        aioclient_mock, page(entree_de_fichier(IDS[0], "mon fichier renommé"))
    )

    (sauvegarde,) = await destination.async_list_backups()

    assert sauvegarde.remote_id == IDS[0]
    assert sauvegarde.metadata["provenance"] == "registre"


### Suppression ###


async def test_la_suppression_demande_delete_v2_sur_l_identifiant(
    destination: DropboxDestination, aioclient_mock: AiohttpClientMocker
) -> None:
    """Critère : la sauvegarde est supprimée du compte Dropbox.

    L'identifiant opaque est passé là où l'API attend un chemin : il reste
    valide même si le fichier a été déplacé ou renommé depuis le dépôt.
    """
    simuler_la_suppression(aioclient_mock)

    await destination.async_delete_backup(IDS[0])

    (appel,) = appels(aioclient_mock, URL_SUPPRESSION)
    assert argument(appel) == {"path": IDS[0]}


async def test_une_sauvegarde_deja_absente_est_signalee_comme_introuvable(
    destination: DropboxDestination, aioclient_mock: AiohttpClientMocker
) -> None:
    """Critère : un fichier déjà absent ne provoque pas d'erreur bloquante.

    L'erreur typée dédiée est ce que la purge distante traite comme « déjà
    purgé » : elle retire l'entrée du registre et poursuit.
    """
    aioclient_mock.post(
        URL_SUPPRESSION, status=409, json=erreur_dropbox("path_lookup/not_found/.")
    )

    with pytest.raises(DestinationNotFoundError, match="n'existe plus"):
        await destination.async_delete_backup(IDS[0])


async def test_une_suppression_sans_identifiant_est_refusee(
    destination: DropboxDestination, aioclient_mock: AiohttpClientMocker
) -> None:
    """Un identifiant vide n'atteint jamais l'API : `delete_v2` y verrait un chemin."""
    with pytest.raises(DestinationError, match="aucun identifiant"):
        await destination.async_delete_backup("   ")

    assert not appels(aioclient_mock, URL_SUPPRESSION)


async def test_une_suppression_rejouee_apres_une_limitation_aboutit(
    destination: DropboxDestination,
    aioclient_mock: AiohttpClientMocker,
    sommeil: Any,
) -> None:
    """Un `429` est rejoué en respectant le délai demandé par Dropbox."""
    aioclient_mock.post(
        URL_SUPPRESSION,
        side_effect=servir(
            reponse(
                URL_SUPPRESSION,
                status=429,
                charge=erreur_dropbox("too_many_requests/..."),
                entetes={"Retry-After": "5"},
            ),
            reponse(URL_SUPPRESSION, charge={"metadata": {"id": IDS[0]}}),
        ),
    )

    await destination.async_delete_backup(IDS[0])

    assert len(appels(aioclient_mock, URL_SUPPRESSION)) == 2
    sommeil.assert_awaited_once_with(5.0)


async def test_une_suppression_refusee_reste_une_erreur_de_destination(
    destination: DropboxDestination, aioclient_mock: AiohttpClientMocker
) -> None:
    """Un refus définitif nomme la sauvegarde et la destination concernées.

    Aucune nouvelle tentative n'est attendue : un `409` de nom interdit ne
    s'arrangera pas en attendant une minute.
    """
    aioclient_mock.post(
        URL_SUPPRESSION, status=409, json=erreur_dropbox("path_write/disallowed_name/.")
    )

    with pytest.raises(DestinationError) as echec:
        await destination.async_delete_backup(IDS[0])

    assert not isinstance(echec.value, DestinationNotFoundError)
    assert IDS[0] in str(echec.value)
    assert NOM_DESTINATION in str(echec.value)
    assert len(appels(aioclient_mock, URL_SUPPRESSION)) == 1


### Erreur d'authentification : ré-autorisation, pas erreur générique ###


@pytest.mark.parametrize("statut", [401, 403])
async def test_un_acces_refuse_pendant_le_listage_demande_une_reautorisation(
    hass: HomeAssistant,
    destination: DropboxDestination,
    aioclient_mock: AiohttpClientMocker,
    statut: int,
) -> None:
    """Critère : l'erreur est typée et déclenche la ré-authentification.

    Une `DestinationError` générique laisserait la purge journaliser un incident
    quelconque, sans jamais demander à l'utilisateur de ré-autoriser.
    """
    aioclient_mock.post(
        URL_LISTAGE, status=statut, json=erreur_dropbox("expired_access_token/...")
    )

    with pytest.raises(DestinationAuthError):
        await destination.async_list_backups()

    registre = ir.async_get(hass)
    assert registre.async_get_issue(DOMAIN, identifiant_du_probleme(DESTINATION_ID))


@pytest.mark.parametrize("statut", [401, 403])
async def test_un_acces_refuse_pendant_la_suppression_demande_une_reautorisation(
    hass: HomeAssistant,
    destination: DropboxDestination,
    aioclient_mock: AiohttpClientMocker,
    statut: int,
) -> None:
    """Critère : même traitement à la suppression qu'au listage."""
    aioclient_mock.post(
        URL_SUPPRESSION, status=statut, json=erreur_dropbox("missing_scope/...")
    )

    with pytest.raises(DestinationAuthError):
        await destination.async_delete_backup(IDS[0])

    registre = ir.async_get(hass)
    assert registre.async_get_issue(DOMAIN, identifiant_du_probleme(DESTINATION_ID))


async def test_une_destination_a_reautoriser_n_est_plus_jointe_par_la_purge(
    hass: HomeAssistant,
    demarrer: Callable[..., Awaitable[MockConfigEntry]],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Après un accès refusé, la purge ne rappelle plus Dropbox.

    C'est la garantie de #9 vérifiée sur un fournisseur réel : une destination
    en attente de ré-autorisation est sautée **avant** tout appel réseau.
    """
    await demarrer(**{CONF_RETENTION_DAYS: 1})
    aioclient_mock.post(
        URL_LISTAGE, status=401, json=erreur_dropbox("expired_access_token/...")
    )

    with pytest.raises(DestinationAuthError):
        await _destination(hass).async_list_backups()
    appels_avant = len(appels(aioclient_mock, URL_LISTAGE))

    await hass.services.async_call(DOMAIN, SERVICE_PURGE, blocking=True)
    await hass.async_block_till_done()

    assert len(appels(aioclient_mock, URL_LISTAGE)) == appels_avant


### Purge distante de bout en bout ###


async def test_la_purge_supprime_les_sauvegardes_expirees_et_emet_l_evenement(
    hass: HomeAssistant,
    demarrer: Callable[..., Awaitable[MockConfigEntry]],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Critère : une rétention Dropbox supprime réellement, et le dit.

    Deux sauvegardes inscrites au registre, une ancienne et une récente, et une
    rétention de sept jours : seule l'ancienne doit partir, et l'événement de
    purge distante doit la nommer.
    """
    await demarrer(**{CONF_RETENTION_DAYS: 7})
    await _inscrire(hass, IDS[0], NOMS[0], slug=SLUGS[0], age_en_jours=30)
    await _inscrire(hass, IDS[1], NOMS[1], slug=SLUGS[1])
    ancienne = dt_util.utcnow() - timedelta(days=30)
    simuler_le_listage(
        aioclient_mock,
        page(
            entree_de_fichier(IDS[0], NOMS[0], date=ancienne.isoformat()),
            entree_de_fichier(IDS[1], NOMS[1], date=dt_util.utcnow().isoformat()),
        ),
    )
    simuler_la_suppression(aioclient_mock)
    evenements = async_capture_events(hass, EVENT_REMOTE_PURGE)

    await hass.services.async_call(DOMAIN, SERVICE_PURGE, blocking=True)
    await hass.async_block_till_done()

    supprimes = [
        argument(appel)["path"] for appel in appels(aioclient_mock, URL_SUPPRESSION)
    ]
    assert supprimes == [IDS[0]]
    assert len(evenements) == 1
    assert evenements[0].data == {
        ATTR_DESTINATION: DESTINATION_ID,
        ATTR_DESTINATION_NAME: NOM_DESTINATION,
        ATTR_REMOTE_IDS: [IDS[0]],
    }
    # L'entrée purgée quitte le registre, celle qui survit y reste.
    assert [entree.remote_id for entree in _registre(hass).entrees(DESTINATION_ID)] == [
        IDS[1]
    ]


async def test_la_purge_supprime_un_orphelin_reconnu_a_son_seul_nom(
    hass: HomeAssistant,
    demarrer: Callable[..., Awaitable[MockConfigEntry]],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """L'arbitrage des orphelins, vérifié de bout en bout.

    Le registre est vide — le fichier vient d'un dépôt rapporté en échec, ou
    d'une instance qui a perdu son `.storage` —, et aucune métadonnée ne survit
    au dépôt chez Dropbox. Seule la convention de nommage le rattache au fork :
    sans elle, il grossirait le dossier de l'utilisateur pour toujours.
    """
    await demarrer(**{CONF_RETENTION_COUNT: 1})
    assert _registre(hass).entrees(DESTINATION_ID) == []
    ancienne = dt_util.utcnow() - timedelta(days=30)
    simuler_le_listage(
        aioclient_mock,
        page(
            entree_de_fichier(IDS[0], NOMS[0], date=ancienne.isoformat()),
            entree_de_fichier(IDS[1], NOMS[1], date=dt_util.utcnow().isoformat()),
        ),
    )
    simuler_la_suppression(aioclient_mock)

    await hass.services.async_call(DOMAIN, SERVICE_PURGE, blocking=True)
    await hass.async_block_till_done()

    supprimes = [
        argument(appel)["path"] for appel in appels(aioclient_mock, URL_SUPPRESSION)
    ]
    assert supprimes == [IDS[0]]


async def test_la_purge_ne_touche_jamais_un_fichier_etranger(
    hass: HomeAssistant,
    demarrer: Callable[..., Awaitable[MockConfigEntry]],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Critère : un fichier qu'Auto Backup n'a pas déposé n'est jamais supprimé.

    Il est plus vieux que la rétention et seul dans le dossier : rien ne doit
    partir pour autant.
    """
    await demarrer(**{CONF_RETENTION_DAYS: 1})
    ancienne = dt_util.utcnow() - timedelta(days=365)
    simuler_le_listage(
        aioclient_mock,
        page(
            entree_de_fichier("id:photo", "vacances.jpg", date=ancienne.isoformat()),
            entree_de_fichier("id:archive", "documents.tar", date=ancienne.isoformat()),
            entree_de_dossier("Anciennes"),
        ),
    )
    simuler_la_suppression(aioclient_mock)

    await hass.services.async_call(DOMAIN, SERVICE_PURGE, blocking=True)
    await hass.async_block_till_done()

    assert not appels(aioclient_mock, URL_SUPPRESSION)


async def test_une_sauvegarde_deja_absente_est_comptee_comme_purgee(
    hass: HomeAssistant,
    demarrer: Callable[..., Awaitable[MockConfigEntry]],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Critère : la suppression est idempotente jusqu'au bout de la purge.

    Le fichier a disparu entre le listage et la suppression — purgé à la main,
    ou par une autre instance : l'entrée quitte tout de même le registre, sinon
    la purge la retenterait à chaque sauvegarde.
    """
    await demarrer(**{CONF_RETENTION_DAYS: 1})
    await _inscrire(hass, IDS[0], NOMS[0], slug=SLUGS[0], age_en_jours=30)
    ancienne = dt_util.utcnow() - timedelta(days=30)
    simuler_le_listage(
        aioclient_mock,
        page(entree_de_fichier(IDS[0], NOMS[0], date=ancienne.isoformat())),
    )
    aioclient_mock.post(
        URL_SUPPRESSION, status=409, json=erreur_dropbox("path_lookup/not_found/.")
    )
    evenements = async_capture_events(hass, EVENT_REMOTE_PURGE)

    await hass.services.async_call(DOMAIN, SERVICE_PURGE, blocking=True)
    await hass.async_block_till_done()

    assert _registre(hass).entrees(DESTINATION_ID) == []
    assert evenements[0].data[ATTR_REMOTE_IDS] == [IDS[0]]


async def test_une_purge_sans_rien_a_supprimer_ne_journalise_aucune_trace(
    hass: HomeAssistant,
    demarrer: Callable[..., Awaitable[MockConfigEntry]],
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Une rétention Dropbox n'écrit plus ni erreur ni trace d'appel.

    Jusqu'à cette issue, la purge d'une destination Dropbox s'arrêtait sur une
    erreur explicite à chaque sauvegarde. Le listage aboutit désormais : le
    journal ne doit plus porter la moindre erreur.
    """
    await demarrer(**{CONF_RETENTION_DAYS: 1})
    simuler_le_listage(aioclient_mock, page())
    caplog.clear()

    with caplog.at_level(logging.DEBUG):
        await hass.services.async_call(DOMAIN, SERVICE_PURGE, blocking=True)
        await hass.async_block_till_done()

    assert [enr.message for enr in caplog.records if enr.exc_info] == []
    assert "Traceback" not in caplog.text
    assert [
        enr.getMessage() for enr in caplog.records if enr.levelno >= logging.ERROR
    ] == []
    assert not appels(aioclient_mock, URL_SUPPRESSION)


async def test_une_troncature_de_pagination_ne_fait_pas_echouer_la_retention_par_nombre(
    hass: HomeAssistant,
    demarrer: Callable[..., Awaitable[MockConfigEntry]],
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Une pagination tronquée ne fait pas planter la rétention par nombre.

    `PAGES_MAX` coupe le listage après la première page : la deuxième page,
    qui porterait une troisième sauvegarde, n'est jamais interrogée. La
    rétention par nombre (`retention_count`) ne voit donc que deux
    sauvegardes — elle doit calculer sur ce qui est **visible**, sans jamais
    lever d'exception ni supprimer plus que ce que le nombre de survivants
    autorise. La sauvegarde de la page non vue reste simplement en place
    jusqu'à la prochaine purge, comme le documente `docs/destinations/
    dropbox.md`.
    """
    await demarrer(**{CONF_RETENTION_COUNT: 1})
    await _inscrire(hass, IDS[0], NOMS[0], slug=SLUGS[0], age_en_jours=20)
    await _inscrire(hass, IDS[1], NOMS[1], slug=SLUGS[1], age_en_jours=10)
    await _inscrire(hass, IDS[2], NOMS[2], slug=SLUGS[2], age_en_jours=30)
    simuler_le_listage(
        aioclient_mock,
        page(
            entree_de_fichier(
                IDS[0],
                NOMS[0],
                date=(dt_util.utcnow() - timedelta(days=20)).isoformat(),
            ),
            entree_de_fichier(
                IDS[1],
                NOMS[1],
                date=(dt_util.utcnow() - timedelta(days=10)).isoformat(),
            ),
            curseur="curseur-page-2",
            suite=True,
        ),
        # Jamais interrogée : la troncature intervient avant.
        page(
            entree_de_fichier(
                IDS[2],
                NOMS[2],
                date=(dt_util.utcnow() - timedelta(days=30)).isoformat(),
            )
        ),
    )
    simuler_la_suppression(aioclient_mock)

    with patch(f"{MODULE_DROPBOX}.PAGES_MAX", 1), caplog.at_level(logging.WARNING):
        await hass.services.async_call(DOMAIN, SERVICE_PURGE, blocking=True)
        await hass.async_block_till_done()

    assert not appels(aioclient_mock, URL_LISTAGE_SUITE)
    assert "tronqué" in caplog.text
    supprimes = [
        argument(appel)["path"] for appel in appels(aioclient_mock, URL_SUPPRESSION)
    ]
    # Sur les deux sauvegardes vues, une seule est en trop pour un
    # `retention_count` de 1 : la plus ancienne des deux visibles part, la
    # troisième (page non vue) n'est même pas candidate ce tour-ci.
    assert supprimes == [IDS[0]]
    assert [entree.remote_id for entree in _registre(hass).entrees(DESTINATION_ID)] == [
        IDS[1],
        IDS[2],
    ]


### Journaux ###


async def test_aucun_secret_n_est_journalise_pendant_le_cycle_de_vie(
    hass: HomeAssistant,
    destination: DropboxDestination,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Ni jeton, ni clé d'application, ni identifiant de compte dans les journaux.

    Le niveau `debug` est celui qui en dirait le plus : c'est donc lui qui est
    éprouvé, sur un listage paginé suivi d'une suppression.
    """
    simuler_le_listage(
        aioclient_mock,
        page(entree_de_fichier(IDS[0], NOMS[0]), curseur="curseur-1", suite=True),
        page(entree_de_fichier(IDS[1], NOMS[1])),
    )
    simuler_la_suppression(aioclient_mock)

    with caplog.at_level(logging.DEBUG):
        sauvegardes = await destination.async_list_backups()
        await destination.async_delete_backup(sauvegardes[0].remote_id)

    for secret in SECRETS_A_NE_PAS_JOURNALISER:
        assert secret not in caplog.text, f"valeur sensible journalisée : {secret}"
    assert "Authorization" not in caplog.text
    assert "Bearer" not in caplog.text
