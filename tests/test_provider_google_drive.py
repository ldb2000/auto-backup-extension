"""Connexion d'un compte Google Drive en OAuth2 (issue #13).

Ces tests parcourent le fournisseur `google_drive` de bout en bout : sa
déclaration OAuth2, l'URL d'autorisation construite pour Google, l'ajout complet
d'une destination depuis les options, l'identification du compte, les échecs
caractéristiques (identifiants invalides, consentement refusé, API Drive non
activée), le rafraîchissement du jeton et sa révocation.

**Aucun appel réseau n'est fait et aucune valeur réelle n'est employée** : les
points d'accès de Google sont simulés par `aioclient_mock`, les identifiants
d'application et les jetons sont des chaînes reconnaissables, et le compte de
test vit dans le domaine réservé `.test` (RFC 2606).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from aiohttp import ClientError
from homeassistant.const import (
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_NAME,
    CONF_TOKEN,
)
from homeassistant.core import HomeAssistant
from homeassistant.core_config import async_process_ha_core_config
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker
from yarl import URL

from custom_components.auto_backup.const import (
    CONF_AUTO_PURGE,
    CONF_BACKUP_TIMEOUT,
    CONF_DESTINATION_ID,
    CONF_DESTINATIONS,
    CONF_FOLDER,
    CONF_PROVIDER,
    CONF_PROVIDER_DATA,
    DATA_DESTINATIONS,
    DOMAIN,
    OAUTH_CALLBACK_PATH,
)
from custom_components.auto_backup.destinations import (
    DestinationAuthError,
    DestinationConfig,
    DestinationError,
    DestinationManager,
    DestinationNotFoundError,
    DestinationQuotaError,
    get_provider,
    identifiant_du_probleme,
    list_providers,
    spec_oauth_du_fournisseur,
)
from custom_components.auto_backup.destinations.oauth import (
    implementation_de_la_destination,
)
from custom_components.auto_backup.destinations.providers.google_drive import (
    CLE_EMAIL_DU_COMPTE,
    LIBELLE_GOOGLE_DRIVE,
    PORTEE_DRIVE_FILE,
    PROVIDER_GOOGLE_DRIVE,
    SPEC_OAUTH_GOOGLE_DRIVE,
    URL_ABOUT,
    URL_AUTORISATION,
    URL_JETON,
    CompteGoogle,
    GoogleDriveDestination,
    async_lire_le_compte,
)

type OuvrirLesOptions = Callable[[str, str], Awaitable[dict[str, Any]]]

URL_EXTERNE = "https://auto-backup.exemple.test"
URL_DE_RETOUR = f"{URL_EXTERNE}{OAUTH_CALLBACK_PATH}"

# Identifiants de l'application Google Cloud de l'utilisateur, tels qu'il les
# saisirait. Ils sont inventés : aucun projet réel ne porte ces valeurs.
CLIENT_ID_FACTICE = "identifiant-application-google-factice"
CLIENT_SECRET_FACTICE = "secret-application-google-factice"
CODE_AUTORISATION_FACTICE = "code-autorisation-google-factice"

DUREE_JETON = 3600

# Compte Google de test : domaine réservé `.test`, jamais routable.
NOM_DU_COMPTE = "Camille Martin"
EMAIL_DU_COMPTE = "camille.martin@exemple.test"

IDENTIFIANT_DESTINATION = "mon_drive"

# Tiret demi-cadratin du nom par défaut, en séquence d'échappement : `ruff`
# refuse les caractères ambigus dans le code (RUF001).
NOM_PAR_DEFAUT_ATTENDU = f"{LIBELLE_GOOGLE_DRIVE} \u2013 {NOM_DU_COMPTE}"


def jeton_google(**surcharges: Any) -> dict[str, Any]:
    """Jeton déjà normalisé (`expires_at` renseigné), tel que persisté."""
    return {
        "access_token": "acces-google-factice-1",
        "refresh_token": "rafraichissement-google-factice-1",
        "token_type": "Bearer",
        "expires_in": DUREE_JETON,
        "expires_at": time.time() + DUREE_JETON,
        "scope": PORTEE_DRIVE_FILE,
    } | surcharges


def reponse_de_jeton(**surcharges: Any) -> dict[str, Any]:
    """Réponse brute du point de jeton de Google : durée relative."""
    return {
        "access_token": "acces-google-factice-2",
        "refresh_token": "rafraichissement-google-factice-2",
        "token_type": "Bearer",
        "expires_in": DUREE_JETON,
        "scope": PORTEE_DRIVE_FILE,
    } | surcharges


def reponse_about(**surcharges: Any) -> dict[str, Any]:
    """Réponse de `drive/v3/about` décrivant le compte autorisé."""
    return {
        "user": {
            "displayName": NOM_DU_COMPTE,
            "emailAddress": EMAIL_DU_COMPTE,
            "kind": "drive#user",
        }
    } | surcharges


def erreur_google(statut: int, raison: str, message: str = "erreur simulée") -> dict:
    """Corps d'erreur de l'API Google, dans sa forme habituelle."""
    return {
        "error": {
            "code": statut,
            "message": message,
            "errors": [{"domain": "global", "reason": raison, "message": message}],
        }
    }


def config_google(**surcharges: Any) -> dict[str, Any]:
    """Configuration brute d'une destination Google Drive, telle que persistée."""
    return {
        CONF_DESTINATION_ID: IDENTIFIANT_DESTINATION,
        CONF_PROVIDER: PROVIDER_GOOGLE_DRIVE,
        CONF_NAME: "Mon Drive",
        CONF_FOLDER: "Sauvegardes/HA",
        CONF_CLIENT_ID: CLIENT_ID_FACTICE,
        CONF_CLIENT_SECRET: CLIENT_SECRET_FACTICE,
        CONF_TOKEN: jeton_google(),
        CONF_PROVIDER_DATA: {CLE_EMAIL_DU_COMPTE: EMAIL_DU_COMPTE},
    } | surcharges


@pytest.fixture
async def instance_joignable(hass: HomeAssistant) -> None:
    """Donne une URL externe à l'instance : sans elle, aucune redirection."""
    await async_process_ha_core_config(hass, {"external_url": URL_EXTERNE})


@pytest.fixture
async def entree(
    hass: HomeAssistant, integration_backup: None, instance_joignable: None
) -> MockConfigEntry:
    """Entrée `auto_backup` initialisée, sans aucune destination."""
    entree = MockConfigEntry(domain=DOMAIN, title="Auto Backup", data={})
    entree.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entree.entry_id)
    await hass.async_block_till_done()
    return entree


@pytest.fixture
async def entree_google(
    hass: HomeAssistant, integration_backup: None, instance_joignable: None
) -> MockConfigEntry:
    """Entrée portant une destination Google Drive déjà autorisée."""
    entree = MockConfigEntry(
        domain=DOMAIN,
        title="Auto Backup",
        data={},
        options={
            CONF_AUTO_PURGE: True,
            CONF_BACKUP_TIMEOUT: 20,
            CONF_DESTINATIONS: [config_google()],
        },
    )
    entree.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entree.entry_id)
    await hass.async_block_till_done()
    return entree


def _gestionnaire(hass: HomeAssistant) -> DestinationManager:
    """Gestionnaire de destinations de l'entrée chargée."""
    return hass.data[DATA_DESTINATIONS]


def _destination(hass: HomeAssistant) -> GoogleDriveDestination:
    """Destination Google Drive chargée par le gestionnaire."""
    return _gestionnaire(hass).async_get(IDENTIFIANT_DESTINATION)


def _destination_persistee(entree: MockConfigEntry) -> dict[str, Any]:
    """Configuration brute de la destination, telle qu'écrite dans l'entrée."""
    (persistee,) = entree.options[CONF_DESTINATIONS]
    return persistee


def _expirer_le_jeton(hass: HomeAssistant, entree: MockConfigEntry) -> None:
    """Rend le jeton de la destination périmé depuis une minute."""
    hass.config_entries.async_update_entry(
        entree,
        options={
            **entree.options,
            CONF_DESTINATIONS: [
                config_google(token=jeton_google(expires_at=time.time() - 60))
            ],
        },
    )


def _valeur_suggeree(resultat: dict[str, Any], cle: str) -> Any:
    """Valeur proposée par le formulaire pour un champ, s'il y en a une."""
    for marqueur in resultat["data_schema"].schema:
        if marqueur == cle:
            return (marqueur.description or {}).get("suggested_value")
    return None


def _appels(aioclient_mock: AiohttpClientMocker, url: str) -> list[tuple]:
    """Appels enregistrés vers un point d'accès, quels que soient ses paramètres."""
    return [
        appel for appel in aioclient_mock.mock_calls if str(appel[1]).startswith(url)
    ]


def _mock_google(
    aioclient_mock: AiohttpClientMocker, *, about: dict[str, Any] | None = None
) -> None:
    """Simule les deux points d'accès de Google utilisés par l'ajout."""
    aioclient_mock.post(URL_JETON, json=reponse_de_jeton())
    aioclient_mock.get(URL_ABOUT, json=about if about is not None else reponse_about())


async def _jusqu_a_l_autorisation(
    hass: HomeAssistant, entree: MockConfigEntry, ouvrir_les_options: OuvrirLesOptions
) -> dict[str, Any]:
    """Déroule l'ajout d'une destination Google Drive jusqu'à l'étape externe."""
    resultat = await ouvrir_les_options(entree.entry_id, "ajouter_destination")
    assert resultat["step_id"] == "ajouter_destination"

    resultat = await hass.config_entries.options.async_configure(
        resultat["flow_id"], {CONF_PROVIDER: PROVIDER_GOOGLE_DRIVE}
    )
    assert resultat["step_id"] == "identifiants"

    resultat = await hass.config_entries.options.async_configure(
        resultat["flow_id"],
        {CONF_CLIENT_ID: CLIENT_ID_FACTICE, CONF_CLIENT_SECRET: CLIENT_SECRET_FACTICE},
    )
    assert resultat["type"] is FlowResultType.EXTERNAL_STEP
    return resultat


async def _retour_de_google(
    hass: HomeAssistant, resultat: dict[str, Any], **reponse: Any
) -> dict[str, Any]:
    """Simule le retour d'autorisation, comme le ferait la vue du fork."""
    url = URL(resultat["url"])
    resultat = await hass.config_entries.options.async_configure(
        resultat["flow_id"],
        {"state": {"redirect_uri": url.query["redirect_uri"]}, **reponse},
    )
    assert resultat["type"] is FlowResultType.EXTERNAL_STEP_DONE
    return await hass.config_entries.options.async_configure(resultat["flow_id"])


### Déclaration du fournisseur ###


async def test_le_fournisseur_est_livre_avec_l_integration(
    hass: HomeAssistant, entree: MockConfigEntry
) -> None:
    """Critère : « Google Drive » est proposé sans rien installer de plus."""
    assert PROVIDER_GOOGLE_DRIVE in list_providers()
    assert get_provider(PROVIDER_GOOGLE_DRIVE) is GoogleDriveDestination
    assert GoogleDriveDestination.label == LIBELLE_GOOGLE_DRIVE


def test_la_declaration_oauth_ne_demande_que_la_portee_drive_file() -> None:
    """Critère : accès hors-ligne et portée `drive.file`, rien d'autre."""
    spec = SPEC_OAUTH_GOOGLE_DRIVE

    assert spec.authorize_url == "https://accounts.google.com/o/oauth2/v2/auth"
    assert spec.token_url == "https://oauth2.googleapis.com/token"
    assert spec.scopes == ("https://www.googleapis.com/auth/drive.file",)
    assert spec.donnees_d_autorisation() == {
        "scope": PORTEE_DRIVE_FILE,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "false",
    }


async def test_le_fournisseur_expose_sa_declaration_au_socle(
    hass: HomeAssistant, entree: MockConfigEntry
) -> None:
    """Le flux d'options lit la déclaration par le registre, pas par un import."""
    assert spec_oauth_du_fournisseur(PROVIDER_GOOGLE_DRIVE) is SPEC_OAUTH_GOOGLE_DRIVE


async def test_l_url_d_autorisation_porte_les_parametres_exiges_par_google(
    hass: HomeAssistant, entree: MockConfigEntry
) -> None:
    """Critère : `access_type=offline`, `prompt=consent`, portée unique."""
    implementation = implementation_de_la_destination(
        hass,
        DestinationConfig(
            destination_id="provisoire",
            provider=PROVIDER_GOOGLE_DRIVE,
            name="Autorisation en cours",
        ),
        client_id=CLIENT_ID_FACTICE,
        client_secret=CLIENT_SECRET_FACTICE,
    )

    url = URL(await implementation.async_generate_authorize_url("flux-de-test"))

    assert str(url.with_query(None)) == URL_AUTORISATION
    assert url.query["response_type"] == "code"
    assert url.query["client_id"] == CLIENT_ID_FACTICE
    assert url.query["redirect_uri"] == URL_DE_RETOUR
    assert url.query["access_type"] == "offline"
    assert url.query["prompt"] == "consent"
    assert url.query["include_granted_scopes"] == "false"
    assert url.query["state"]
    # Une seule portée demandée, et c'est bien la plus restreinte.
    assert url.query.getall("scope") == [PORTEE_DRIVE_FILE]


### Parcours complet d'ajout ###


async def test_le_parcours_complet_connecte_un_compte_google(
    hass: HomeAssistant,
    entree: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    ouvrir_les_options: OuvrirLesOptions,
) -> None:
    """Critères 1 à 3 : identifiants, consentement, jeton, compte, nom proposé."""
    _mock_google(aioclient_mock)

    resultat = await _jusqu_a_l_autorisation(hass, entree, ouvrir_les_options)
    assert str(URL(resultat["url"]).with_query(None)) == URL_AUTORISATION

    resultat = await _retour_de_google(hass, resultat, code=CODE_AUTORISATION_FACTICE)

    # Le code a été échangé auprès du point de jeton de Google.
    methode, url_jeton, donnees, _ = aioclient_mock.mock_calls[0]
    assert (methode, str(url_jeton)) == ("POST", URL_JETON)
    assert donnees["grant_type"] == "authorization_code"
    assert donnees["code"] == CODE_AUTORISATION_FACTICE
    assert donnees["redirect_uri"] == URL_DE_RETOUR

    # Le compte autorisé a été identifié, et sert de nom par défaut.
    assert resultat["step_id"] == "destination"
    assert _valeur_suggeree(resultat, CONF_NAME) == NOM_PAR_DEFAUT_ATTENDU
    appels_about = _appels(aioclient_mock, URL_ABOUT)
    assert appels_about, "l'ajout doit interroger `about` pour identifier le compte"
    for _, url_about, _, entetes in appels_about:
        assert url_about.query["fields"] == "user(displayName,emailAddress)"
        assert entetes["Authorization"] == "Bearer acces-google-factice-2"

    resultat = await hass.config_entries.options.async_configure(
        resultat["flow_id"],
        {CONF_NAME: NOM_PAR_DEFAUT_ATTENDU, CONF_FOLDER: "Sauvegardes/HA"},
    )
    await hass.async_block_till_done()
    assert resultat["type"] is FlowResultType.CREATE_ENTRY

    persistee = _destination_persistee(entree)
    assert persistee[CONF_PROVIDER] == PROVIDER_GOOGLE_DRIVE
    assert persistee[CONF_NAME] == NOM_PAR_DEFAUT_ATTENDU
    assert persistee[CONF_TOKEN]["access_token"] == "acces-google-factice-2"
    assert persistee[CONF_TOKEN]["refresh_token"] == "rafraichissement-google-factice-2"
    # Critère : l'adresse du compte est conservée avec la destination.
    assert persistee[CONF_PROVIDER_DATA] == {CLE_EMAIL_DU_COMPTE: EMAIL_DU_COMPTE}

    destination = _gestionnaire(hass).async_get(persistee[CONF_DESTINATION_ID])
    assert isinstance(destination, GoogleDriveDestination)
    assert destination.account_email == EMAIL_DU_COMPTE


async def test_un_compte_sans_nom_garde_un_nom_par_defaut_lisible(
    hass: HomeAssistant,
    entree: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    ouvrir_les_options: OuvrirLesOptions,
) -> None:
    """Google ne renvoie pas toujours `displayName` : le flux ne doit pas caler."""
    _mock_google(aioclient_mock, about={"user": {"emailAddress": EMAIL_DU_COMPTE}})

    resultat = await _jusqu_a_l_autorisation(hass, entree, ouvrir_les_options)
    resultat = await _retour_de_google(hass, resultat, code=CODE_AUTORISATION_FACTICE)

    assert _valeur_suggeree(resultat, CONF_NAME) == LIBELLE_GOOGLE_DRIVE


async def test_un_about_sans_utilisateur_n_empeche_pas_l_ajout(
    hass: HomeAssistant,
    entree: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    ouvrir_les_options: OuvrirLesOptions,
) -> None:
    """Une réponse inattendue laisse la destination se créer, sans compte connu."""
    _mock_google(aioclient_mock, about={})

    resultat = await _jusqu_a_l_autorisation(hass, entree, ouvrir_les_options)
    resultat = await _retour_de_google(hass, resultat, code=CODE_AUTORISATION_FACTICE)
    resultat = await hass.config_entries.options.async_configure(
        resultat["flow_id"], {CONF_NAME: "Mon Drive", CONF_FOLDER: "Sauvegardes"}
    )
    await hass.async_block_till_done()

    assert resultat["type"] is FlowResultType.CREATE_ENTRY
    assert CONF_PROVIDER_DATA not in _destination_persistee(entree)


### Échecs caractéristiques, et reprise du flux ###


async def test_des_identifiants_invalides_sont_expliques_et_le_flux_relancable(
    hass: HomeAssistant,
    entree: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    ouvrir_les_options: OuvrirLesOptions,
) -> None:
    """Critère 4 : `invalid_client` -> message explicite, flux relançable."""
    aioclient_mock.post(
        URL_JETON,
        status=401,
        json={"error": "invalid_client", "error_description": "Unauthorized"},
    )

    resultat = await _jusqu_a_l_autorisation(hass, entree, ouvrir_les_options)
    resultat = await _retour_de_google(hass, resultat, code=CODE_AUTORISATION_FACTICE)

    assert resultat["type"] is FlowResultType.ABORT
    assert resultat["reason"] == "autorisation_non_accordee"
    assert not entree.options.get(CONF_DESTINATIONS)

    # Le flux repart sans redémarrer Home Assistant, une fois les identifiants
    # corrigés chez Google.
    aioclient_mock.clear_requests()
    _mock_google(aioclient_mock)
    resultat = await _jusqu_a_l_autorisation(hass, entree, ouvrir_les_options)
    resultat = await _retour_de_google(hass, resultat, code=CODE_AUTORISATION_FACTICE)
    assert resultat["step_id"] == "destination"


async def test_un_consentement_refuse_est_explique_et_le_flux_relancable(
    hass: HomeAssistant,
    entree: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    ouvrir_les_options: OuvrirLesOptions,
) -> None:
    """Critère 4 : l'utilisateur ferme l'écran Google (`access_denied`)."""
    _mock_google(aioclient_mock)

    resultat = await _jusqu_a_l_autorisation(hass, entree, ouvrir_les_options)
    resultat = await _retour_de_google(hass, resultat, error="access_denied")

    assert resultat["type"] is FlowResultType.ABORT
    assert resultat["reason"] == "autorisation_refusee"
    assert resultat["description_placeholders"] == {"erreur": "access_denied"}
    assert not entree.options.get(CONF_DESTINATIONS)

    resultat = await _jusqu_a_l_autorisation(hass, entree, ouvrir_les_options)
    resultat = await _retour_de_google(hass, resultat, code=CODE_AUTORISATION_FACTICE)
    assert resultat["step_id"] == "destination"


async def test_une_api_drive_desactivee_est_expliquee_et_le_flux_relancable(
    hass: HomeAssistant,
    entree: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    ouvrir_les_options: OuvrirLesOptions,
) -> None:
    """Critère 4 : 403 `accessNotConfigured` -> la cause probable est nommée."""
    aioclient_mock.post(URL_JETON, json=reponse_de_jeton())
    aioclient_mock.get(
        URL_ABOUT,
        status=403,
        json=erreur_google(403, "accessNotConfigured", "Drive API has not been used"),
    )

    resultat = await _jusqu_a_l_autorisation(hass, entree, ouvrir_les_options)
    resultat = await _retour_de_google(hass, resultat, code=CODE_AUTORISATION_FACTICE)

    assert resultat["type"] is FlowResultType.ABORT
    assert resultat["reason"] == "echec_fournisseur"
    detail = resultat["description_placeholders"]["detail"]
    assert "API Google Drive n'est pas activée" in detail
    assert "Bibliothèque" in detail
    assert not entree.options.get(CONF_DESTINATIONS)

    # Une fois l'API activée dans la console Google Cloud, le flux aboutit.
    aioclient_mock.clear_requests()
    _mock_google(aioclient_mock)
    resultat = await _jusqu_a_l_autorisation(hass, entree, ouvrir_les_options)
    resultat = await _retour_de_google(hass, resultat, code=CODE_AUTORISATION_FACTICE)
    assert resultat["step_id"] == "destination"


### Rafraîchissement et révocation du jeton ###


async def test_un_jeton_expire_est_rafraichi_avant_l_appel(
    hass: HomeAssistant,
    entree_google: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Critère 5 : la session générique renouvelle le jeton, sans rien demander."""
    _expirer_le_jeton(hass, entree_google)
    aioclient_mock.post(URL_JETON, json=reponse_de_jeton())
    aioclient_mock.get(URL_ABOUT, json=reponse_about())

    await _destination(hass).async_check_connection()
    await hass.async_block_till_done()

    (_, _, donnees, _) = _appels(aioclient_mock, URL_JETON)[0]
    assert donnees["grant_type"] == "refresh_token"
    assert donnees["refresh_token"] == "rafraichissement-google-factice-1"

    # L'appel à Drive a bien utilisé le jeton renouvelé, et celui-ci est persisté.
    (_, _, _, entetes) = _appels(aioclient_mock, URL_ABOUT)[0]
    assert entetes["Authorization"] == "Bearer acces-google-factice-2"
    persiste = _destination_persistee(entree_google)[CONF_TOKEN]
    assert persiste["access_token"] == "acces-google-factice-2"
    assert persiste["expires_at"] > time.time()


async def test_un_acces_revoque_demande_une_reautorisation(
    hass: HomeAssistant,
    entree_google: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Critère 5 : `invalid_grant` -> `DestinationAuthError`, marquage et problème."""
    _expirer_le_jeton(hass, entree_google)
    aioclient_mock.post(
        URL_JETON,
        status=400,
        json={"error": "invalid_grant", "error_description": "Token has been expired"},
    )

    with pytest.raises(DestinationAuthError):
        await _destination(hass).async_check_connection()

    gestionnaire = _gestionnaire(hass)
    assert gestionnaire.reauthentification_requise(IDENTIFIANT_DESTINATION) is True

    probleme = ir.async_get(hass).async_get_issue(
        DOMAIN, identifiant_du_probleme(IDENTIFIANT_DESTINATION)
    )
    assert probleme is not None
    assert probleme.translation_placeholders == {
        "nom": "Mon Drive",
        "fournisseur": PROVIDER_GOOGLE_DRIVE,
    }


async def test_la_reautorisation_met_a_jour_le_jeton_et_le_compte(
    hass: HomeAssistant,
    entree_google: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    ouvrir_les_options: OuvrirLesOptions,
) -> None:
    """Ré-autoriser rafraîchit aussi l'adresse du compte, sans toucher au reste."""
    _mock_google(
        aioclient_mock,
        about={
            "user": {"displayName": "Dominique Roy", "emailAddress": "d@exemple.test"}
        },
    )

    resultat = await ouvrir_les_options(
        entree_google.entry_id, "reautoriser_destination"
    )
    resultat = await hass.config_entries.options.async_configure(
        resultat["flow_id"], {CONF_DESTINATION_ID: IDENTIFIANT_DESTINATION}
    )
    resultat = await _retour_de_google(hass, resultat, code=CODE_AUTORISATION_FACTICE)
    await hass.async_block_till_done()

    assert resultat["type"] is FlowResultType.CREATE_ENTRY
    persistee = _destination_persistee(entree_google)
    assert persistee[CONF_TOKEN]["access_token"] == "acces-google-factice-2"
    assert persistee[CONF_PROVIDER_DATA] == {CLE_EMAIL_DU_COMPTE: "d@exemple.test"}
    assert persistee[CONF_NAME] == "Mon Drive"
    assert persistee[CONF_FOLDER] == "Sauvegardes/HA"


### Vérification de l'accès et qualification des erreurs ###


async def test_la_verification_de_connexion_interroge_drive(
    hass: HomeAssistant,
    entree_google: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """`async_check_connection` appelle `about` avec le jeton en en-tête."""
    aioclient_mock.get(URL_ABOUT, json=reponse_about())

    await _destination(hass).async_check_connection()

    (methode, url, _, entetes) = _appels(aioclient_mock, URL_ABOUT)[0]
    assert methode == "GET"
    assert url.query["fields"] == "user"
    assert entetes["Authorization"] == "Bearer acces-google-factice-1"


@pytest.mark.parametrize(
    ("statut", "corps", "erreur_attendue", "extrait"),
    [
        (401, {"error": {"code": 401}}, DestinationAuthError, "révoquée"),
        (
            403,
            None,
            DestinationError,
            "refusé l'accès",
        ),
        (
            403,
            "accessNotConfigured",
            DestinationError,
            "API Google Drive n'est pas activée",
        ),
        (
            403,
            "storageQuotaExceeded",
            DestinationQuotaError,
            "espace de stockage",
        ),
        (404, "notFound", DestinationNotFoundError, "n'existe pas"),
        (500, "backendError", DestinationError, "HTTP 500"),
    ],
)
async def test_les_echecs_de_drive_sont_qualifies(
    hass: HomeAssistant,
    entree_google: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    statut: int,
    corps: Any,
    erreur_attendue: type[DestinationError],
    extrait: str,
) -> None:
    """Chaque échec devient l'erreur typée que le socle sait traiter."""
    if isinstance(corps, str):
        corps = erreur_google(statut, corps)
    aioclient_mock.get(URL_ABOUT, status=statut, json=corps or {})

    with pytest.raises(erreur_attendue) as erreur:
        await _destination(hass).async_check_connection()

    assert extrait in str(erreur.value)


@pytest.mark.parametrize(
    ("exception", "extrait"),
    [
        (TimeoutError(), "n'a pas répondu"),
        (ClientError("panne réseau"), "injoignable"),
    ],
)
async def test_une_panne_reseau_devient_une_erreur_de_destination(
    hass: HomeAssistant,
    entree_google: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    exception: Exception,
    extrait: str,
) -> None:
    """Ni délai dépassé ni coupure réseau ne remontent bruts aux appelants."""
    aioclient_mock.get(URL_ABOUT, exc=exception)

    with pytest.raises(DestinationError) as erreur:
        await _destination(hass).async_check_connection()

    assert extrait in str(erreur.value)
    assert not isinstance(erreur.value, DestinationAuthError)


async def test_une_reponse_illisible_est_signalee_sans_planter(
    hass: HomeAssistant,
    entree_google: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Un corps non JSON ne doit pas masquer le statut d'erreur."""
    aioclient_mock.get(URL_ABOUT, status=502, text="<html>Bad gateway</html>")

    with pytest.raises(DestinationError) as erreur:
        await _destination(hass).async_check_connection()

    assert "HTTP 502" in str(erreur.value)


async def test_le_compte_est_lu_meme_quand_drive_repond_partiellement(
    hass: HomeAssistant,
    entree_google: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """`async_lire_le_compte` tolère les champs absents ou d'un autre type."""
    aioclient_mock.get(URL_ABOUT, json={"user": {"displayName": "  ", "id": 12}})

    compte = await async_lire_le_compte(_destination(hass).session)

    assert compte == CompteGoogle(nom=None, email=None)


### Opérations différées ###


@pytest.mark.parametrize(
    ("operation", "arguments", "issue"),
    [
        ("async_upload", ("/backup/ha.tar",), "#14"),
        ("async_list_backups", (), "#15"),
        ("async_delete_backup", ("identifiant-distant",), "#15"),
    ],
)
async def test_les_operations_de_sauvegarde_viennent_ensuite(
    hass: HomeAssistant,
    entree_google: MockConfigEntry,
    operation: str,
    arguments: tuple,
    issue: str,
) -> None:
    """Hors périmètre de l'issue #13 : le contrat est déclaré, pas encore tenu."""
    destination = _destination(hass)
    appel = getattr(destination, operation)
    nommes = {"name": "ha"} if operation == "async_upload" else {}

    with pytest.raises(NotImplementedError) as erreur:
        await appel(*arguments, **nommes)

    assert issue in str(erreur.value)


### Journaux ###


async def test_aucun_secret_n_est_journalise_meme_en_debug(
    hass: HomeAssistant,
    entree: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    ouvrir_les_options: OuvrirLesOptions,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Critère 7 : rien de sensible dans les journaux, niveau `debug` compris."""
    _mock_google(aioclient_mock)
    caplog.set_level(logging.DEBUG)

    resultat = await _jusqu_a_l_autorisation(hass, entree, ouvrir_les_options)
    resultat = await _retour_de_google(hass, resultat, code=CODE_AUTORISATION_FACTICE)
    resultat = await hass.config_entries.options.async_configure(
        resultat["flow_id"], {CONF_NAME: "Mon Drive", CONF_FOLDER: "Sauvegardes"}
    )
    await hass.async_block_till_done()
    assert resultat["type"] is FlowResultType.CREATE_ENTRY

    sensibles = [
        CLIENT_SECRET_FACTICE,
        CODE_AUTORISATION_FACTICE,
        reponse_de_jeton()["access_token"],
        reponse_de_jeton()["refresh_token"],
        EMAIL_DU_COMPTE,
    ]
    fuites = [valeur for valeur in sensibles if valeur in caplog.text]
    assert not fuites, f"valeurs sensibles présentes dans les journaux : {fuites}"
