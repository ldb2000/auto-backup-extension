"""Critères d'acceptation de l'issue #66, vérifiés indépendamment du codeur.

Un test par critère Given-When-Then :

1. L'enquête sur `HA-Frontend-Base` (code de Home Assistant, frontend et
   cœur, à la version épinglée) est consignée, preuve à l'appui, dans l'ADR
   0001, décision 4.
2. Sans URL externe mais avec une URL interne `http://localhost:8123`,
   l'ajout d'une destination utilise cette adresse comme URI de redirection —
   ici de bout en bout, avec les **deux** fournisseurs réels (Dropbox et
   Google Drive), jusqu'au retour reçu par la **vraie vue HTTP** du fork
   (`hass_client_no_auth`, échange du code simulé par `aioclient_mock`).
3. Avec une URL externe HTTPS, l'ajout et la ré-autorisation d'une
   destination restent inchangés (non-régression).
4. Une URL interne en `http://` qui n'est pas `localhost` fait abandonner le
   flux avec le motif `url_de_retour_http`, sans jamais fabriquer d'adresse.
5. La docstring de `url_de_retour()`, les guides Dropbox et Google Drive et
   la FAQ décrivent ce comportement réel, marche à suivre pour un test local
   comprise.
6. Les cas listés par le critère (en-tête présent, URL externe seule, URL
   interne `localhost` seule, aucune URL) sont couverts par la suite —
   attesté ici en s'assurant que ces tests unitaires existent bien dans
   `tests/test_destinations_oauth.py`.

Ce module est indépendant de `tests/test_destinations_oauth.py` et de
`tests/test_destinations_flux_options.py`, écrits par le codeur : les
critères 2 à 4 y sont déjà éprouvés à leur granularité (unitaire pour
`url_de_retour()`, flux d'options avec un fournisseur factice jusqu'à l'étape
externe). Celui-ci rejoue le critère 2 par le point d'entrée le plus complet
qui existe pour l'utilisateur — les fournisseurs réels livrés avec
l'intégration, et la vraie requête HTTP de retour — plutôt que par les
fonctions internes ou un fournisseur factice.

**Aucune valeur réelle n'y figure** : identifiants d'application, code
d'autorisation et jetons sont des chaînes reconnaissables (« -test66 »), et
tous les appels aux fournisseurs sont simulés par `aioclient_mock`.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from homeassistant.const import CONF_CLIENT_ID, CONF_CLIENT_SECRET, CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.core_config import async_process_ha_core_config
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker
from yarl import URL

from custom_components.auto_backup.const import (
    CONF_DESTINATIONS,
    CONF_FOLDER,
    CONF_PROVIDER,
    DATA_DESTINATIONS,
    DOMAIN,
    OAUTH_CALLBACK_PATH,
)
from custom_components.auto_backup.destinations import DestinationManager
from custom_components.auto_backup.destinations.providers.dropbox import (
    CLE_ACCOUNT_ID,
    PROVIDER_DROPBOX,
)
from custom_components.auto_backup.destinations.providers.dropbox import (
    PORTEES as PORTEES_DROPBOX,
)
from custom_components.auto_backup.destinations.providers.dropbox import (
    URL_COMPTE as URL_COMPTE_DROPBOX,
)
from custom_components.auto_backup.destinations.providers.dropbox import (
    URL_JETON as URL_JETON_DROPBOX,
)
from custom_components.auto_backup.destinations.providers.google_drive import (
    CLE_EMAIL_DU_COMPTE,
    PORTEE_DRIVE_FILE,
    PROVIDER_GOOGLE_DRIVE,
)
from custom_components.auto_backup.destinations.providers.google_drive import (
    URL_ABOUT as URL_ABOUT_GOOGLE,
)
from custom_components.auto_backup.destinations.providers.google_drive import (
    URL_JETON as URL_JETON_GOOGLE,
)

RACINE_DEPOT = Path(__file__).resolve().parent.parent
DOCS = RACINE_DEPOT / "docs"
ADR = DOCS / "adr" / "0001-destinations-distantes.md"

URL_INTERNE_LOCALE = "http://localhost:8123"
URI_DE_REDIRECTION_LOCALE = f"{URL_INTERNE_LOCALE}{OAUTH_CALLBACK_PATH}"

CODE_AUTORISATION = "code-autorisation-test66"

type OuvrirLesOptions = Callable[[str, str], Awaitable[dict[str, Any]]]


def _gestionnaire(hass: HomeAssistant) -> DestinationManager:
    """Gestionnaire de destinations de l'entrée chargée."""
    return hass.data[DATA_DESTINATIONS]


async def _entree_sans_url_externe(hass: HomeAssistant) -> MockConfigEntry:
    """Entrée `auto_backup` chargée sur une instance sans URL externe."""
    entree = MockConfigEntry(domain=DOMAIN, title="Auto Backup", data={})
    entree.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entree.entry_id)
    await hass.async_block_till_done()
    return entree


### CRITÈRE 1 : l'enquête sur `HA-Frontend-Base` est consignée dans l'ADR ###


def _section_decision_4() -> str:
    """Texte de la décision 4 de l'ADR 0001, sans le reste du document.

    Isole la section pour vérifier que l'enquête et sa conclusion sont bien
    rattachées à **cette** décision, comme l'exige le critère, et non
    ailleurs dans le fichier.
    """
    texte = ADR.read_text(encoding="utf-8")
    debut = texte.index("## Décision 4")
    # La décision suivante ouvre la prochaine section de premier niveau.
    fin = texte.index("\n## ", debut + 1)
    return texte[debut:fin]


def _normalise(texte: str) -> str:
    """Espaces et retours à la ligne réduits à un seul espace.

    Un habillage Markdown différent (largeur de ligne) ne doit pas faire
    échouer une recherche de phrase qui, à l'affichage, reste continue.
    """
    return " ".join(texte.split())


def test_critere1_l_enquete_sur_ha_frontend_base_est_consignee_avec_preuve() -> None:
    """Given le code de Home Assistant (frontend et cœur) à la version épinglée,
    When on trace l'appel d'une étape de flux d'options, Then l'ADR 0001,
    décision 4, établit — preuve à l'appui — que `HA-Frontend-Base` n'est
    jamais transmis dans un flux d'options.
    """
    section = _normalise(_section_decision_4())

    # La section porte bien le sous-titre dédié à l'issue #66, sous la
    # décision 4 — pas une note isolée ailleurs dans l'ADR.
    assert "### Adresse de retour (issue #66)" in section

    # Preuve : les versions épinglées sont citées, pas une affirmation à nu.
    assert (
        "**Enquête (Home Assistant 2026.9.0, frontend "
        "`home-assistant-frontend==20260826.4` épinglé par "
        "`homeassistant/components/frontend/manifest.json`).**" in section
    )

    # Preuve : le frontend n'envoie l'en-tête que dans un flux de
    # configuration (`config_flow.ts`, `sub_config_flow.ts`), jamais dans un
    # flux d'options (`options_flow.ts`).
    assert "src/data/config_flow.ts" in section
    assert "src/data/options_flow.ts" in section
    assert (
        "`createOptionsFlow`, `fetchOptionsFlow`, `handleOptionsFlowStep` et "
        "`deleteOptionsFlow` appellent `callApi` **sans en-têtes**" in section
    )

    # Preuve côté cœur : `HEADER_FRONTEND_BASE` n'est lu que par un config
    # flow OAuth2, et `get_url(allow_internal=False, ...)` ne renvoie jamais
    # d'URL interne.
    assert "homeassistant/helpers/config_entry_oauth2_flow.py" in section
    assert "homeassistant/helpers/network.py" in section

    # Conclusion explicite, sans ambiguïté : c'est elle que le critère exige.
    assert (
        "**Conclusion.** `HA-Frontend-Base` n'est transmis que pour les flux "
        "de configuration (et de sous-entrées), jamais pour les flux "
        "d'options où vivent l'ajout et la ré-autorisation d'une "
        "destination." in section
    )


### CRITÈRE 2 : bout en bout, sans URL externe, via `http://localhost:8123` ###


async def _jusqu_a_l_autorisation(
    hass: HomeAssistant,
    entree: MockConfigEntry,
    ouvrir_les_options: OuvrirLesOptions,
    *,
    provider: str,
    client_id: str,
    client_secret: str,
) -> dict[str, Any]:
    """Déroule l'ajout jusqu'à l'étape externe, avec un fournisseur réel."""
    resultat = await ouvrir_les_options(entree.entry_id, "ajouter_destination")
    assert resultat["step_id"] == "ajouter_destination"

    resultat = await hass.config_entries.options.async_configure(
        resultat["flow_id"], {CONF_PROVIDER: provider}
    )
    assert resultat["step_id"] == "identifiants"
    # Rien n'est fabriqué : l'adresse proposée à l'utilisateur est la vraie
    # URI de redirection à déclarer chez le fournisseur.
    assert resultat["description_placeholders"]["url_de_retour"] == (
        URI_DE_REDIRECTION_LOCALE
    )

    resultat = await hass.config_entries.options.async_configure(
        resultat["flow_id"],
        {CONF_CLIENT_ID: client_id, CONF_CLIENT_SECRET: client_secret},
    )
    assert resultat["type"] is FlowResultType.EXTERNAL_STEP
    return resultat


async def _retour_par_la_vraie_vue_http(
    hass: HomeAssistant,
    resultat: dict[str, Any],
    hass_client_no_auth: Callable[[], Awaitable[Any]],
) -> None:
    """Simule la redirection du navigateur vers la vue HTTP réelle du fork.

    Contrairement à `hass.config_entries.options.async_configure()` (utilisé
    par le codeur pour simuler le retour), ceci passe par un vrai client HTTP
    aiohttp et par `RetourAutorisationOAuthView`, la route effectivement
    déclarée sur l'URI de redirection localhost.
    """
    assert await async_setup_component(hass, "http", {})
    url = URL(resultat["url"])
    etat = url.query["state"]
    assert url.query["redirect_uri"] == URI_DE_REDIRECTION_LOCALE

    client = await hass_client_no_auth()
    reponse = await client.get(
        f"{OAUTH_CALLBACK_PATH}?code={CODE_AUTORISATION}&state={etat}"
    )

    assert reponse.status == 200
    assert "window.close()" in await reponse.text()


async def test_critere2_ajout_dropbox_sans_url_externe_via_localhost_aboutit(
    hass: HomeAssistant,
    integration_backup: None,
    aioclient_mock: AiohttpClientMocker,
    ouvrir_les_options: OuvrirLesOptions,
    hass_client_no_auth: Callable[[], Awaitable[Any]],
) -> None:
    """Given une instance sans URL externe mais avec `http://localhost:8123`
    comme URL interne, When j'ajoute une destination **Dropbox** (fournisseur
    réel livré avec l'intégration), Then l'URL d'autorisation envoyée à
    Dropbox porte `redirect_uri=http://localhost:8123/auth/auto_backup/
    callback`, et le retour reçu par la vraie vue HTTP du fork termine
    l'ajout — le code est échangé contre un jeton (simulé).
    """
    await async_process_ha_core_config(hass, {"internal_url": URL_INTERNE_LOCALE})
    entree = await _entree_sans_url_externe(hass)

    client_id = "identifiant-application-dropbox-test66"
    client_secret = "secret-application-dropbox-test66"
    aioclient_mock.post(
        URL_JETON_DROPBOX,
        json={
            "access_token": "acces-dropbox-test66",
            "refresh_token": "rafraichissement-dropbox-test66",
            "token_type": "bearer",
            "expires_in": 14400,
            "scope": " ".join(PORTEES_DROPBOX),
            "account_id": "dbid:compte-test66",
        },
    )
    aioclient_mock.post(
        URL_COMPTE_DROPBOX,
        json={
            "account_id": "dbid:compte-test66",
            "name": {"display_name": "Compte Test 66"},
            "email": "test66@exemple.test",
            "email_verified": True,
        },
    )

    resultat = await _jusqu_a_l_autorisation(
        hass,
        entree,
        ouvrir_les_options,
        provider=PROVIDER_DROPBOX,
        client_id=client_id,
        client_secret=client_secret,
    )
    # THEN (1) : l'URL envoyée à Dropbox porte bien l'adresse locale.
    assert URL(resultat["url"]).query["redirect_uri"] == URI_DE_REDIRECTION_LOCALE

    await _retour_par_la_vraie_vue_http(hass, resultat, hass_client_no_auth)

    # THEN (2) : reprendre le flux d'options aboutit à l'étape de nommage —
    # le code a donc bien été échangé contre un jeton.
    suite = await hass.config_entries.options.async_configure(resultat["flow_id"])
    assert suite["step_id"] == "destination"

    suite = await hass.config_entries.options.async_configure(
        suite["flow_id"], {CONF_NAME: "Dropbox test66", CONF_FOLDER: "Sauvegardes"}
    )
    await hass.async_block_till_done()

    assert suite["type"] is FlowResultType.CREATE_ENTRY
    (persistee,) = entree.options[CONF_DESTINATIONS]
    assert persistee[CONF_PROVIDER] == PROVIDER_DROPBOX
    assert persistee[CONF_CLIENT_ID] == client_id
    assert persistee["token"]["access_token"] == "acces-dropbox-test66"
    assert persistee["provider_data"] == {CLE_ACCOUNT_ID: "dbid:compte-test66"}
    assert [d.name for d in _gestionnaire(hass)] == ["Dropbox test66"]


async def test_critere2_ajout_google_drive_sans_url_externe_via_localhost_aboutit(
    hass: HomeAssistant,
    integration_backup: None,
    aioclient_mock: AiohttpClientMocker,
    ouvrir_les_options: OuvrirLesOptions,
    hass_client_no_auth: Callable[[], Awaitable[Any]],
) -> None:
    """Même critère 2, avec **Google Drive** comme second fournisseur réel.

    Le critère exige que le comportement vaille pour Dropbox *et* Google
    Drive : deux implémentations d'`OAUTH2_SPEC` distinctes, deux fournisseurs
    aux points d'accès différents, chacun rejoué de bout en bout.
    """
    await async_process_ha_core_config(hass, {"internal_url": URL_INTERNE_LOCALE})
    entree = await _entree_sans_url_externe(hass)

    client_id = "identifiant-application-google-test66"
    client_secret = "secret-application-google-test66"
    aioclient_mock.post(
        URL_JETON_GOOGLE,
        json={
            "access_token": "acces-google-test66",
            "refresh_token": "rafraichissement-google-test66",
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": PORTEE_DRIVE_FILE,
        },
    )
    aioclient_mock.get(
        URL_ABOUT_GOOGLE,
        json={
            "user": {
                "displayName": "Compte Test 66",
                "emailAddress": "test66@exemple.test",
                "kind": "drive#user",
            }
        },
    )

    resultat = await _jusqu_a_l_autorisation(
        hass,
        entree,
        ouvrir_les_options,
        provider=PROVIDER_GOOGLE_DRIVE,
        client_id=client_id,
        client_secret=client_secret,
    )
    # THEN (1) : l'URL envoyée à Google porte bien l'adresse locale.
    assert URL(resultat["url"]).query["redirect_uri"] == URI_DE_REDIRECTION_LOCALE

    await _retour_par_la_vraie_vue_http(hass, resultat, hass_client_no_auth)

    # THEN (2) : reprendre le flux d'options aboutit à l'étape de nommage —
    # le code a donc bien été échangé contre un jeton.
    suite = await hass.config_entries.options.async_configure(resultat["flow_id"])
    assert suite["step_id"] == "destination"

    suite = await hass.config_entries.options.async_configure(
        suite["flow_id"], {CONF_NAME: "Google Drive test66", CONF_FOLDER: "Sauvegardes"}
    )
    await hass.async_block_till_done()

    assert suite["type"] is FlowResultType.CREATE_ENTRY
    (persistee,) = entree.options[CONF_DESTINATIONS]
    assert persistee[CONF_PROVIDER] == PROVIDER_GOOGLE_DRIVE
    assert persistee[CONF_CLIENT_ID] == client_id
    assert persistee["token"]["access_token"] == "acces-google-test66"
    assert persistee["provider_data"] == {CLE_EMAIL_DU_COMPTE: "test66@exemple.test"}
    assert [d.name for d in _gestionnaire(hass)] == ["Google Drive test66"]


### CRITÈRE 5 : la doc décrit le comportement réel, test local compris ###


def test_critere5_la_docstring_decrit_l_ordre_reel_des_bases_et_le_test_local() -> None:
    """Given la docstring de `url_de_retour()`, When je la lis, Then elle
    décrit l'ordre réellement appliqué (en-tête, puis URL externe, puis URL
    interne) et la marche à suivre pour un test local par
    `http://localhost:8123/auth/auto_backup/callback`.
    """
    import custom_components.auto_backup.destinations.oauth as module_oauth

    docstring = _normalise(module_oauth.url_de_retour.__doc__ or "")

    assert "HA-Frontend-Base" in docstring
    assert "ne l'envoie que dans les flux de configuration" in docstring
    assert "jamais dans les flux d'options" in docstring
    assert "http://localhost:8123" in docstring
    assert "AdresseDeRetourRefusee" in docstring
    assert "NoURLAvailableError" in docstring


def test_critere5_les_guides_et_la_faq_decrivent_le_test_local_sans_url_externe() -> (
    None
):
    """Given les guides Dropbox et Google Drive et la FAQ, When je les lis,
    Then ils décrivent la marche à suivre pour un test local, sans URL
    externe, avec l'URI de redirection réellement écoutée par le fork.
    """
    for chemin in (
        DOCS / "destinations" / "dropbox.md",
        DOCS / "destinations" / "google-drive.md",
        DOCS / "faq.md",
    ):
        texte = _normalise(chemin.read_text(encoding="utf-8"))
        assert URI_DE_REDIRECTION_LOCALE in texte, (
            f"« {URI_DE_REDIRECTION_LOCALE} » absent de {chemin.name}"
        )
        assert "localhost" in texte


### CRITÈRE 6 : chaque cas de figure est couvert par un test unitaire ###


def test_critere6_chaque_cas_de_url_de_retour_a_son_test_unitaire() -> None:
    """Given la suite de tests, When je l'exécute (`uv run pytest`), Then
    chacun des quatre cas du critère — en-tête présent, URL externe seule,
    URL interne `localhost` seule, aucune URL — est bien couvert par
    `tests/test_destinations_oauth.py`.

    Ce test ne rejoue pas ces cas (déjà éprouvés par le codeur) : il constate
    que les fonctions de test correspondantes existent réellement dans le
    fichier, de sorte qu'une suppression accidentelle d'un cas ferait
    échouer *cette* preuve du critère plutôt que de passer inaperçue.
    """
    texte = (RACINE_DEPOT / "tests" / "test_destinations_oauth.py").read_text(
        encoding="utf-8"
    )
    noms_de_fonctions = set(re.findall(r"^(?:async )?def (test_\w+)", texte, re.M))

    cas_exiges = {
        "en-tête `HA-Frontend-Base` présent": (
            "test_l_url_de_retour_suit_l_en_tete_du_frontal"
        ),
        "URL externe seule": "test_l_url_de_retour_derive_de_l_url_externe",
        "URL interne `localhost` seule": (
            "test_sans_url_externe_une_url_interne_acceptable_sert_de_base"
        ),
        "aucune URL": "test_sans_url_externe_l_autorisation_est_impossible",
    }
    for cas, nom_attendu in cas_exiges.items():
        assert nom_attendu in noms_de_fonctions, (
            f"cas « {cas} » non couvert : « {nom_attendu} » absent de "
            "tests/test_destinations_oauth.py"
        )
