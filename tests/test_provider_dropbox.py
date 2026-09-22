"""Connexion d'un compte Dropbox en OAuth2 (issue #10).

Ces tests parcourent l'ajout d'une destination Dropbox de bout en bout — choix du
fournisseur, identifiants d'application, URL d'autorisation, échange du code,
identification du compte, nommage, persistance — puis éprouvent la vie du jeton
(rafraîchissement, révocation) et la vérification d'accès.

**Aucune valeur réelle n'y figure** : les identifiants d'application, le code
d'autorisation, les jetons et l'identifiant de compte sont des chaînes
reconnaissables (« -factice »), l'instance de test vit sur le domaine réservé
`.test` (RFC 2606), et les appels HTTP sont simulés par `aioclient_mock`. Rien ne
sort du processus de test.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import patch

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
    enregistrer_les_fournisseurs,
    get_provider,
    identifiant_du_probleme,
    list_providers,
    provider_label,
    spec_oauth_du_fournisseur,
)
from custom_components.auto_backup.destinations.errors import UnknownProviderError
from custom_components.auto_backup.destinations.flow import IDENTIFIANT_PROVISOIRE
from custom_components.auto_backup.destinations.providers.dropbox import (
    CLE_ACCOUNT_ID,
    LIBELLE_DROPBOX,
    PORTEES,
    PROVIDER_DROPBOX,
    URL_AUTORISATION,
    URL_COMPTE,
    URL_JETON,
    CompteDropbox,
    DropboxDestination,
)

type OuvrirLesOptions = Callable[[str, str], Awaitable[dict[str, Any]]]

URL_EXTERNE = "https://auto-backup.exemple.test"

# Identifiants de l'application Dropbox tels que l'utilisateur les saisirait.
# Inventés : aucune application réelle ne porte ces valeurs.
CLE_APPLICATION = "cle-application-dropbox-factice"
SECRET_APPLICATION = "secret-application-dropbox-factice"
CODE_AUTORISATION = "code-autorisation-dropbox-factice"

ACCES_INITIAL = "acces-dropbox-factice-1"
ACCES_RENOUVELE = "acces-dropbox-factice-2"
RAFRAICHISSEMENT = "rafraichissement-dropbox-factice"

# Compte Dropbox factice renvoyé par `users/get_current_account`.
ACCOUNT_ID = "dbid:compte-factice-0000"
NOM_AFFICHE = "Jeanne Factice"
COURRIEL = "jeanne@exemple.test"

NOM_PAR_DEFAUT_ATTENDU = f"{LIBELLE_DROPBOX} \N{EN DASH} {NOM_AFFICHE}"

DUREE_JETON = 14400

SECRETS_A_NE_PAS_JOURNALISER = (
    CLE_APPLICATION,
    SECRET_APPLICATION,
    CODE_AUTORISATION,
    ACCES_INITIAL,
    ACCES_RENOUVELE,
    RAFRAICHISSEMENT,
    ACCOUNT_ID,
)


def reponse_de_compte(**surcharges: Any) -> dict[str, Any]:
    """Réponse de `users/get_current_account`, réduite aux champs utilisés."""
    return {
        "account_id": ACCOUNT_ID,
        "name": {"display_name": NOM_AFFICHE, "given_name": "Jeanne"},
        "email": COURRIEL,
        "email_verified": True,
    } | surcharges


def reponse_de_jeton(**surcharges: Any) -> dict[str, Any]:
    """Réponse de `oauth2/token` : Dropbox renvoie une durée relative."""
    return {
        "access_token": ACCES_INITIAL,
        "refresh_token": RAFRAICHISSEMENT,
        "token_type": "bearer",
        "expires_in": DUREE_JETON,
        "scope": " ".join(PORTEES),
        "account_id": ACCOUNT_ID,
    } | surcharges


def config_dropbox(**surcharges: Any) -> dict[str, Any]:
    """Configuration brute d'une destination Dropbox, telle que persistée."""
    return {
        CONF_DESTINATION_ID: "dropbox_jeanne",
        CONF_PROVIDER: PROVIDER_DROPBOX,
        CONF_NAME: "Dropbox de Jeanne",
        CONF_FOLDER: "Sauvegardes/HA",
        CONF_CLIENT_ID: CLE_APPLICATION,
        CONF_CLIENT_SECRET: SECRET_APPLICATION,
        CONF_TOKEN: {
            "access_token": ACCES_INITIAL,
            "refresh_token": RAFRAICHISSEMENT,
            "token_type": "bearer",
            "expires_in": DUREE_JETON,
            "expires_at": time.time() + DUREE_JETON,
        },
        CONF_PROVIDER_DATA: {CLE_ACCOUNT_ID: ACCOUNT_ID},
    } | surcharges


def config_au_jeton_expire(**surcharges: Any) -> dict[str, Any]:
    """La même, dont le jeton d'accès a expiré il y a une minute."""
    config = config_dropbox(**surcharges)
    config[CONF_TOKEN] = {**config[CONF_TOKEN], "expires_at": time.time() - 60}
    return config


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
async def entree_dropbox(
    hass: HomeAssistant, integration_backup: None, instance_joignable: None
) -> MockConfigEntry:
    """Entrée portant une destination Dropbox déjà autorisée."""
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


def _destination(hass: HomeAssistant, entree: MockConfigEntry) -> DropboxDestination:
    """Destination Dropbox chargée par le gestionnaire pour cette entrée."""
    gestionnaire: DestinationManager = hass.data[DATA_DESTINATIONS]
    destination = gestionnaire.async_get(config_dropbox()[CONF_DESTINATION_ID])
    assert isinstance(destination, DropboxDestination)
    return destination


def _destination_directe(hass: HomeAssistant, **surcharges: Any) -> DropboxDestination:
    """Destination Dropbox construite à la main, hors gestionnaire."""
    return DropboxDestination(
        hass, DestinationConfig.from_dict(config_dropbox(**surcharges))
    )


async def _jusqu_a_l_autorisation(
    hass: HomeAssistant,
    entree: MockConfigEntry,
    ouvrir_les_options: OuvrirLesOptions,
) -> dict[str, Any]:
    """Déroule le flux d'ajout Dropbox jusqu'à l'étape externe d'autorisation."""
    resultat = await ouvrir_les_options(entree.entry_id, "ajouter_destination")
    assert resultat["step_id"] == "ajouter_destination"

    resultat = await hass.config_entries.options.async_configure(
        resultat["flow_id"], {CONF_PROVIDER: PROVIDER_DROPBOX}
    )
    assert resultat["step_id"] == "identifiants"
    assert resultat["description_placeholders"]["fournisseur"] == LIBELLE_DROPBOX

    resultat = await hass.config_entries.options.async_configure(
        resultat["flow_id"],
        {CONF_CLIENT_ID: CLE_APPLICATION, CONF_CLIENT_SECRET: SECRET_APPLICATION},
    )
    assert resultat["type"] is FlowResultType.EXTERNAL_STEP
    return resultat


async def _retour_du_fournisseur(
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


def _valeur_suggeree(resultat: dict[str, Any], champ: str) -> Any:
    """Valeur pré-remplie d'un champ du formulaire, `None` s'il n'y en a pas."""
    for cle in resultat["data_schema"].schema:
        if cle == champ:
            return (cle.description or {}).get("suggested_value")
    raise AssertionError(f"champ « {champ} » absent du formulaire")


def _appel(aioclient_mock: AiohttpClientMocker, url: str) -> tuple[Any, Any, Any, Any]:
    """Dernier appel simulé vers cette URL."""
    appels = [appel for appel in aioclient_mock.mock_calls if str(appel[1]) == url]
    assert appels, f"aucun appel vers {url}"
    return appels[-1]


### Enregistrement du fournisseur ###


async def test_le_fournisseur_dropbox_est_enregistre(entree: MockConfigEntry) -> None:
    """Critère : « Dropbox » est proposé, sous un libellé lisible."""
    assert PROVIDER_DROPBOX in list_providers()
    assert get_provider(PROVIDER_DROPBOX) is DropboxDestination
    assert provider_label(PROVIDER_DROPBOX) == "Dropbox"


async def test_l_enregistrement_des_fournisseurs_est_idempotent(
    entree: MockConfigEntry,
) -> None:
    """L'entrée peut être rechargée sans faire échouer le registre global."""
    enregistrer_les_fournisseurs()
    enregistrer_les_fournisseurs()

    assert list_providers().count(PROVIDER_DROPBOX) == 1


async def test_dropbox_declare_les_portees_minimales(entree: MockConfigEntry) -> None:
    """Critère : seules les portées nécessaires au cycle de vie sont demandées."""
    spec = spec_oauth_du_fournisseur(PROVIDER_DROPBOX)

    assert spec is not None
    assert spec.authorize_url == "https://www.dropbox.com/oauth2/authorize"
    assert spec.token_url == "https://api.dropboxapi.com/oauth2/token"
    assert set(spec.scopes) == {
        "account_info.read",
        "files.metadata.read",
        "files.content.write",
    }
    # Lire le contenu d'une sauvegarde déposée ne sert à aucune opération du
    # périmètre : l'envoi et la suppression relèvent de `files.content.write`, le
    # listage de `files.metadata.read`, et la restauration depuis le nuage est
    # hors périmètre de l'epic.
    assert "files.content.read" not in spec.scopes
    # Aucune portée de partage, de demande de fichier, de contact ni d'équipe.
    assert not [
        portee
        for portee in spec.scopes
        if portee.startswith(("sharing.", "file_requests.", "contacts.", "team"))
    ]


### Demande d'autorisation ###


async def test_l_url_d_autorisation_demande_un_acces_hors_ligne(
    hass: HomeAssistant,
    entree: MockConfigEntry,
    ouvrir_les_options: OuvrirLesOptions,
) -> None:
    """Critère : accès hors-ligne (jeton de rafraîchissement) et portées justes."""
    resultat = await _jusqu_a_l_autorisation(hass, entree, ouvrir_les_options)

    url = URL(resultat["url"])
    assert str(url.with_query(None)) == URL_AUTORISATION
    assert url.query["response_type"] == "code"
    assert url.query["client_id"] == CLE_APPLICATION
    assert url.query["redirect_uri"] == f"{URL_EXTERNE}{OAUTH_CALLBACK_PATH}"
    assert url.query["state"]
    # Sans ce paramètre, Dropbox ne délivre aucun jeton de rafraîchissement.
    assert url.query["token_access_type"] == "offline"
    assert url.query["scope"].split(" ") == list(PORTEES)
    # Le secret de l'application ne part jamais dans le navigateur.
    assert SECRET_APPLICATION not in str(url)


### Parcours complet ###


async def test_le_parcours_complet_connecte_un_compte_dropbox(
    hass: HomeAssistant,
    entree: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    ouvrir_les_options: OuvrirLesOptions,
) -> None:
    """Critères : jeton obtenu, compte identifié, nom proposé, tout persisté."""
    aioclient_mock.post(URL_JETON, json=reponse_de_jeton())
    aioclient_mock.post(URL_COMPTE, json=reponse_de_compte())

    resultat = await _jusqu_a_l_autorisation(hass, entree, ouvrir_les_options)
    resultat = await _retour_du_fournisseur(hass, resultat, code=CODE_AUTORISATION)

    # Le code a été échangé contre un jeton, avec les identifiants de l'utilisateur.
    _, _, corps_jeton, _ = _appel(aioclient_mock, URL_JETON)
    assert corps_jeton["grant_type"] == "authorization_code"
    assert corps_jeton["code"] == CODE_AUTORISATION
    assert corps_jeton["client_id"] == CLE_APPLICATION
    assert corps_jeton["client_secret"] == SECRET_APPLICATION

    # Le compte a été identifié par `users/get_current_account`.
    methode, _, _, entetes = _appel(aioclient_mock, URL_COMPTE)
    assert methode.lower() == "post"
    assert entetes["Authorization"] == f"Bearer {ACCES_INITIAL}"

    # Critère : le nom du compte est proposé par défaut.
    assert resultat["step_id"] == "destination"
    assert _valeur_suggeree(resultat, CONF_NAME) == NOM_PAR_DEFAUT_ATTENDU
    # Le placeholder `{fournisseur}` de cette étape affiche aussi le libellé
    # lisible, comme celui de l'étape « identifiants ».
    assert resultat["description_placeholders"]["fournisseur"] == LIBELLE_DROPBOX

    resultat = await hass.config_entries.options.async_configure(
        resultat["flow_id"],
        {CONF_NAME: NOM_PAR_DEFAUT_ATTENDU, CONF_FOLDER: "Sauvegardes/HA"},
    )
    await hass.async_block_till_done()
    assert resultat["type"] is FlowResultType.CREATE_ENTRY

    (persistee,) = entree.options[CONF_DESTINATIONS]
    assert persistee[CONF_PROVIDER] == PROVIDER_DROPBOX
    assert persistee[CONF_NAME] == NOM_PAR_DEFAUT_ATTENDU
    assert persistee[CONF_CLIENT_ID] == CLE_APPLICATION
    assert persistee[CONF_TOKEN]["access_token"] == ACCES_INITIAL
    assert persistee[CONF_TOKEN]["refresh_token"] == RAFRAICHISSEMENT
    assert persistee[CONF_TOKEN]["expires_at"] > time.time()
    # Critère : l'identifiant du compte est conservé avec la destination.
    assert persistee[CONF_PROVIDER_DATA] == {CLE_ACCOUNT_ID: ACCOUNT_ID}

    gestionnaire: DestinationManager = hass.data[DATA_DESTINATIONS]
    (destination,) = list(gestionnaire)
    assert isinstance(destination, DropboxDestination)
    assert destination.provider_data == {CLE_ACCOUNT_ID: ACCOUNT_ID}


async def test_un_compte_sans_nom_affiche_propose_le_libelle(
    hass: HomeAssistant,
    entree: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    ouvrir_les_options: OuvrirLesOptions,
) -> None:
    """Un compte sans nom affiché ne laisse pas le formulaire sans proposition."""
    aioclient_mock.post(URL_JETON, json=reponse_de_jeton())
    aioclient_mock.post(URL_COMPTE, json=reponse_de_compte(name={}))

    resultat = await _jusqu_a_l_autorisation(hass, entree, ouvrir_les_options)
    resultat = await _retour_du_fournisseur(hass, resultat, code=CODE_AUTORISATION)

    assert _valeur_suggeree(resultat, CONF_NAME) == LIBELLE_DROPBOX


async def test_un_compte_injoignable_interrompt_l_ajout(
    hass: HomeAssistant,
    entree: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    ouvrir_les_options: OuvrirLesOptions,
) -> None:
    """Un fournisseur qui ne répond pas interrompt l'ajout, en disant pourquoi.

    La convergence des issues #10 et #13 a tranché pour l'interruption : une
    destination que Dropbox refuse déjà d'identifier ne fonctionnerait pas
    davantage une fois créée, et l'utilisateur relance le flux d'un clic une
    fois le service rétabli, sans hériter d'une destination muette.
    """
    aioclient_mock.post(URL_JETON, json=reponse_de_jeton())
    aioclient_mock.post(URL_COMPTE, status=500, text="service indisponible")

    resultat = await _jusqu_a_l_autorisation(hass, entree, ouvrir_les_options)
    resultat = await _retour_du_fournisseur(hass, resultat, code=CODE_AUTORISATION)

    assert resultat["type"] is FlowResultType.ABORT
    assert resultat["reason"] == "echec_fournisseur"
    assert "500" in resultat["description_placeholders"]["detail"]
    assert CONF_DESTINATIONS not in entree.options

    # Le service rétabli, le flux repart sans redémarrer Home Assistant.
    aioclient_mock.clear_requests()
    aioclient_mock.post(URL_JETON, json=reponse_de_jeton())
    aioclient_mock.post(URL_COMPTE, json=reponse_de_compte())
    resultat = await _jusqu_a_l_autorisation(hass, entree, ouvrir_les_options)
    resultat = await _retour_du_fournisseur(hass, resultat, code=CODE_AUTORISATION)

    assert resultat["step_id"] == "destination"
    assert _valeur_suggeree(resultat, CONF_NAME) == NOM_PAR_DEFAUT_ATTENDU


async def test_un_fournisseur_devenu_inutilisable_interrompt_l_ajout(
    hass: HomeAssistant,
    entree: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    ouvrir_les_options: OuvrirLesOptions,
) -> None:
    """Un fournisseur retiré du registre pendant le parcours arrête l'ajout.

    `UnknownProviderError` est une `DestinationError` : elle emprunte le même
    chemin que les échecs réseau, plutôt que de laisser l'utilisateur nommer une
    destination que plus personne ne sait instancier.
    """
    aioclient_mock.post(URL_JETON, json=reponse_de_jeton())

    resultat = await _jusqu_a_l_autorisation(hass, entree, ouvrir_les_options)
    with patch(
        "custom_components.auto_backup.destinations.flow.create_destination",
        side_effect=UnknownProviderError("fournisseur retiré"),
    ):
        resultat = await _retour_du_fournisseur(hass, resultat, code=CODE_AUTORISATION)

    assert resultat["type"] is FlowResultType.ABORT
    assert resultat["reason"] == "echec_fournisseur"
    assert "fournisseur retiré" in resultat["description_placeholders"]["detail"]
    assert CONF_DESTINATIONS not in entree.options


async def test_un_acces_refuse_pendant_l_ajout_ne_laisse_pas_de_probleme_orphelin(
    hass: HomeAssistant,
    entree: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    ouvrir_les_options: OuvrirLesOptions,
) -> None:
    """Une portée oubliée ne crée pas un problème nommant une destination fictive.

    L'ajout s'interrompt (`echec_fournisseur`), mais le signalement porté par la
    destination provisoire est effacé dans tous les cas — c'est le rôle du
    `finally` du flux : un problème survivant nommerait « Autorisation en
    cours », que l'utilisateur ne pourrait ni ré-autoriser ni supprimer.
    """
    aioclient_mock.post(URL_JETON, json=reponse_de_jeton())
    aioclient_mock.post(
        URL_COMPTE, status=401, json={"error_summary": "missing_scope/..."}
    )

    resultat = await _jusqu_a_l_autorisation(hass, entree, ouvrir_les_options)
    resultat = await _retour_du_fournisseur(hass, resultat, code=CODE_AUTORISATION)
    await hass.async_block_till_done()

    assert resultat["type"] is FlowResultType.ABORT
    assert resultat["reason"] == "echec_fournisseur"
    registre = ir.async_get(hass)
    assert not [
        probleme
        for probleme in registre.issues
        if probleme[0] == DOMAIN and IDENTIFIANT_PROVISOIRE in probleme[1]
    ]

    # La portée corrigée chez Dropbox, l'ajout aboutit sans rien nettoyer à la main.
    aioclient_mock.clear_requests()
    aioclient_mock.post(URL_JETON, json=reponse_de_jeton())
    aioclient_mock.post(URL_COMPTE, json=reponse_de_compte())
    resultat = await _jusqu_a_l_autorisation(hass, entree, ouvrir_les_options)
    resultat = await _retour_du_fournisseur(hass, resultat, code=CODE_AUTORISATION)
    resultat = await hass.config_entries.options.async_configure(
        resultat["flow_id"], {CONF_NAME: "Mon Dropbox", CONF_FOLDER: "Sauvegardes"}
    )
    await hass.async_block_till_done()
    assert resultat["type"] is FlowResultType.CREATE_ENTRY


async def test_un_acces_refuse_pendant_l_ajout_n_alerte_pas_sur_la_destination_fictive(
    hass: HomeAssistant,
    entree: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    ouvrir_les_options: OuvrirLesOptions,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Le refus pendant l'ajout ne s'annonce pas comme une ré-autorisation à faire.

    Signaler « la destination "Autorisation en cours" doit être ré-autorisée »
    enverrait l'utilisateur chercher dans ses options une destination qui n'existe
    pas : le flux vient justement de la créer pour le temps de l'autorisation, et
    l'efface aussitôt. Le refus reste tracé en `debug`, où il aide à diagnostiquer
    une portée oubliée.
    """
    aioclient_mock.post(URL_JETON, json=reponse_de_jeton())
    aioclient_mock.post(
        URL_COMPTE, status=403, json={"error_summary": "missing_scope/..."}
    )

    with caplog.at_level(logging.DEBUG):
        resultat = await _jusqu_a_l_autorisation(hass, entree, ouvrir_les_options)
        resultat = await _retour_du_fournisseur(hass, resultat, code=CODE_AUTORISATION)
        await hass.async_block_till_done()

    assert resultat["type"] is FlowResultType.ABORT
    assert resultat["reason"] == "echec_fournisseur"

    # Le signalement de ré-authentification a bien eu lieu, mais en `debug`.
    signalements = [
        enregistrement
        for enregistrement in caplog.records
        if enregistrement.name.endswith("destinations.reauth")
    ]
    assert signalements, "le refus n'a pas été signalé du tout"
    assert all(
        enregistrement.levelno == logging.DEBUG for enregistrement in signalements
    )
    assert any(
        "refusé l'accès pendant l'autorisation" in enregistrement.getMessage()
        for enregistrement in signalements
    )

    avertissements = [
        enregistrement.getMessage()
        for enregistrement in caplog.records
        if enregistrement.levelno >= logging.WARNING
    ]
    # Aucun avertissement n'invite à ré-autoriser quoi que ce soit : le seul qui
    # subsiste dit ce qui s'est réellement passé, et sert le message d'abandon.
    assert not [message for message in avertissements if "ré-autorisée" in message]
    assert [
        message for message in avertissements if "refusé la première requête" in message
    ]


### Échecs d'autorisation, et relance du flux ###


async def test_des_identifiants_invalides_sont_signales_et_le_flux_est_relancable(
    hass: HomeAssistant,
    entree: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    ouvrir_les_options: OuvrirLesOptions,
) -> None:
    """Critère : clé ou secret refusé -> message explicite, flux relançable."""
    aioclient_mock.post(
        URL_JETON,
        status=400,
        json={"error": "invalid_client", "error_description": "Invalid client_id"},
    )

    resultat = await _jusqu_a_l_autorisation(hass, entree, ouvrir_les_options)
    resultat = await _retour_du_fournisseur(hass, resultat, code=CODE_AUTORISATION)

    assert resultat["type"] is FlowResultType.ABORT
    assert resultat["reason"] == "autorisation_non_accordee"
    assert CONF_DESTINATIONS not in entree.options

    # Relance immédiate, sans redémarrage de Home Assistant, avec les bonnes clés.
    aioclient_mock.clear_requests()
    aioclient_mock.post(URL_JETON, json=reponse_de_jeton())
    aioclient_mock.post(URL_COMPTE, json=reponse_de_compte())

    resultat = await _jusqu_a_l_autorisation(hass, entree, ouvrir_les_options)
    resultat = await _retour_du_fournisseur(hass, resultat, code=CODE_AUTORISATION)
    resultat = await hass.config_entries.options.async_configure(
        resultat["flow_id"], {CONF_NAME: "Mon Dropbox", CONF_FOLDER: "Sauvegardes"}
    )
    await hass.async_block_till_done()

    assert resultat["type"] is FlowResultType.CREATE_ENTRY
    assert len(entree.options[CONF_DESTINATIONS]) == 1


async def test_une_autorisation_refusee_est_expliquee_et_le_flux_est_relancable(
    hass: HomeAssistant,
    entree: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    ouvrir_les_options: OuvrirLesOptions,
) -> None:
    """Critère : « access_denied » donne un message clair, puis on peut réessayer."""
    resultat = await _jusqu_a_l_autorisation(hass, entree, ouvrir_les_options)
    resultat = await _retour_du_fournisseur(hass, resultat, error="access_denied")

    assert resultat["type"] is FlowResultType.ABORT
    assert resultat["reason"] == "autorisation_annulee"
    assert CONF_DESTINATIONS not in entree.options

    aioclient_mock.post(URL_JETON, json=reponse_de_jeton())
    aioclient_mock.post(URL_COMPTE, json=reponse_de_compte())

    resultat = await _jusqu_a_l_autorisation(hass, entree, ouvrir_les_options)
    resultat = await _retour_du_fournisseur(hass, resultat, code=CODE_AUTORISATION)

    assert resultat["step_id"] == "destination"


### Vie du jeton ###


async def test_un_jeton_expire_est_rafraichi_avant_l_appel(
    hass: HomeAssistant,
    integration_backup: None,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Critère : un jeton expiré est renouvelé sans intervention de l'utilisateur."""
    entree = MockConfigEntry(
        domain=DOMAIN,
        title="Auto Backup",
        data={},
        options={CONF_DESTINATIONS: [config_au_jeton_expire()]},
    )
    entree.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entree.entry_id)
    await hass.async_block_till_done()

    aioclient_mock.post(
        URL_JETON,
        json={
            "access_token": ACCES_RENOUVELE,
            "token_type": "bearer",
            "expires_in": DUREE_JETON,
        },
    )
    aioclient_mock.post(URL_COMPTE, json=reponse_de_compte())

    await _destination(hass, entree).async_check_connection()
    await hass.async_block_till_done()

    # Le renouvellement a bien utilisé le jeton de rafraîchissement de Dropbox.
    _, _, corps, _ = _appel(aioclient_mock, URL_JETON)
    assert corps["grant_type"] == "refresh_token"
    assert corps["refresh_token"] == RAFRAICHISSEMENT
    assert corps["client_id"] == CLE_APPLICATION
    assert corps["client_secret"] == SECRET_APPLICATION

    # L'appel à l'API a utilisé le nouveau jeton, pas l'ancien.
    _, _, _, entetes = _appel(aioclient_mock, URL_COMPTE)
    assert entetes["Authorization"] == f"Bearer {ACCES_RENOUVELE}"

    # Et le nouveau jeton est persisté, jeton de rafraîchissement conservé.
    (persistee,) = entree.options[CONF_DESTINATIONS]
    assert persistee[CONF_TOKEN]["access_token"] == ACCES_RENOUVELE
    assert persistee[CONF_TOKEN]["refresh_token"] == RAFRAICHISSEMENT


async def test_un_acces_revoque_declenche_la_reauthentification(
    hass: HomeAssistant,
    integration_backup: None,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Critère : accès révoqué -> `DestinationAuthError` et problème Home Assistant."""
    entree = MockConfigEntry(
        domain=DOMAIN,
        title="Auto Backup",
        data={},
        options={CONF_DESTINATIONS: [config_au_jeton_expire()]},
    )
    entree.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entree.entry_id)
    await hass.async_block_till_done()

    aioclient_mock.post(URL_JETON, status=400, json={"error": "invalid_grant"})

    destination = _destination(hass, entree)
    with pytest.raises(DestinationAuthError):
        await destination.async_check_connection()
    await hass.async_block_till_done()

    identifiant = config_dropbox()[CONF_DESTINATION_ID]
    gestionnaire: DestinationManager = hass.data[DATA_DESTINATIONS]
    assert gestionnaire.reauthentification_requise(identifiant)
    registre = ir.async_get(hass)
    assert registre.async_get_issue(DOMAIN, identifiant_du_probleme(identifiant))
    # Aucun appel à l'API n'a été tenté avec un jeton périmé.
    assert not [
        appel for appel in aioclient_mock.mock_calls if str(appel[1]) == URL_COMPTE
    ]


async def test_un_jeton_refuse_par_l_api_declenche_la_reauthentification(
    hass: HomeAssistant,
    entree_dropbox: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Un jeton encore « valide » mais révoqué côté Dropbox mène au même endroit."""
    aioclient_mock.post(
        URL_COMPTE,
        status=401,
        json={"error_summary": "expired_access_token/", "error": {}},
    )

    destination = _destination(hass, entree_dropbox)
    with pytest.raises(DestinationAuthError, match="401"):
        await destination.async_check_connection()
    await hass.async_block_till_done()

    identifiant = config_dropbox()[CONF_DESTINATION_ID]
    gestionnaire: DestinationManager = hass.data[DATA_DESTINATIONS]
    assert gestionnaire.reauthentification_requise(identifiant)
    registre = ir.async_get(hass)
    assert registre.async_get_issue(DOMAIN, identifiant_du_probleme(identifiant))


async def test_une_portee_manquante_demande_une_nouvelle_autorisation(
    hass: HomeAssistant,
    entree_dropbox: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Un 403 vient d'une portée absente : seule une ré-autorisation y remédie."""
    aioclient_mock.post(
        URL_COMPTE, status=403, json={"error_summary": "missing_scope/..."}
    )

    with pytest.raises(DestinationAuthError, match="403"):
        await _destination(hass, entree_dropbox).async_check_connection()
    await hass.async_block_till_done()

    gestionnaire: DestinationManager = hass.data[DATA_DESTINATIONS]
    assert gestionnaire.reauthentification_requise(
        config_dropbox()[CONF_DESTINATION_ID]
    )


### Vérification de l'accès ###


async def test_la_verification_d_acces_interroge_le_compte(
    hass: HomeAssistant,
    entree_dropbox: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """`async_check_connection` appelle réellement Dropbox, à chaque fois."""
    aioclient_mock.post(URL_COMPTE, json=reponse_de_compte())

    destination = _destination(hass, entree_dropbox)
    await destination.async_check_connection()
    await destination.async_check_connection()

    appels = [
        appel for appel in aioclient_mock.mock_calls if str(appel[1]) == URL_COMPTE
    ]
    assert len(appels) == 2
    # Dropbox refuse un `Content-Type` sur un point d'entrée sans argument.
    assert "Content-Type" not in appels[0][3]


@pytest.mark.parametrize(
    ("statut", "corps"),
    [
        (429, {"error_summary": "too_many_requests/"}),
        (500, {"error_summary": "internal_error/"}),
        (400, {"error_summary": "other/"}),
    ],
)
async def test_un_echec_temporaire_reste_une_erreur_de_destination(
    hass: HomeAssistant,
    entree_dropbox: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    statut: int,
    corps: dict[str, Any],
) -> None:
    """Ces échecs ne remettent pas l'autorisation en cause : pas de problème créé."""
    aioclient_mock.post(URL_COMPTE, status=statut, json=corps)

    destination = _destination(hass, entree_dropbox)
    with pytest.raises(DestinationError) as echec:
        await destination.async_check_connection()
    await hass.async_block_till_done()

    assert not isinstance(echec.value, DestinationAuthError)
    gestionnaire: DestinationManager = hass.data[DATA_DESTINATIONS]
    assert not gestionnaire.reauthentification_requise(
        config_dropbox()[CONF_DESTINATION_ID]
    )


async def test_une_reponse_illisible_est_refusee(
    hass: HomeAssistant,
    entree_dropbox: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Une réponse qui n'est pas du JSON ne doit pas passer pour un succès."""
    aioclient_mock.post(URL_COMPTE, text="<html>maintenance</html>")

    with pytest.raises(DestinationError, match="illisible"):
        await _destination(hass, entree_dropbox).async_check_connection()


async def test_une_reponse_json_inattendue_est_refusee(
    hass: HomeAssistant,
    entree_dropbox: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Du JSON valide mais qui n'est pas un objet n'est pas exploitable."""
    aioclient_mock.post(URL_COMPTE, text="[]")

    with pytest.raises(DestinationError, match="inattendue"):
        await _destination(hass, entree_dropbox).async_check_connection()


async def test_un_compte_sans_identifiant_est_refuse(
    hass: HomeAssistant,
    entree_dropbox: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Sans `account_id`, la destination ne saurait pas à quel compte elle parle."""
    aioclient_mock.post(URL_COMPTE, json={"name": {"display_name": NOM_AFFICHE}})

    with pytest.raises(DestinationError, match="identifiant"):
        await _destination(hass, entree_dropbox).async_check_connection()


async def test_dropbox_injoignable_est_une_erreur_de_destination(
    hass: HomeAssistant,
    entree_dropbox: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Une panne réseau est signalée comme telle, sans remettre l'accès en cause."""
    aioclient_mock.post(URL_COMPTE, exc=ClientError("réseau indisponible"))

    with pytest.raises(DestinationError, match="injoignable"):
        await _destination(hass, entree_dropbox).async_check_connection()


async def test_une_absence_de_reponse_est_signalee(
    hass: HomeAssistant,
    entree_dropbox: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Un appel qui n'aboutit pas dans le délai imparti n'attend pas indéfiniment."""
    aioclient_mock.post(URL_COMPTE, exc=TimeoutError())

    with pytest.raises(DestinationError, match="temps imparti"):
        await _destination(hass, entree_dropbox).async_check_connection()


### Hors périmètre de l'issue #10 ###


async def test_le_cycle_de_vie_des_sauvegardes_reste_a_implementer(
    hass: HomeAssistant, entree_dropbox: MockConfigEntry
) -> None:
    """Téléversement (#11), listage et suppression (#12) sont hors périmètre."""
    destination = _destination(hass, entree_dropbox)

    with pytest.raises(NotImplementedError, match="#11"):
        await destination.async_upload("/backup/ha.tar", name="ha")
    with pytest.raises(NotImplementedError, match="#12"):
        await destination.async_list_backups()
    with pytest.raises(NotImplementedError, match="#12"):
        await destination.async_delete_backup("id:factice")


### Secrets ###


async def test_aucun_secret_n_est_journalise_meme_en_debug(
    hass: HomeAssistant,
    entree: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    ouvrir_les_options: OuvrirLesOptions,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Critère : le parcours complet ne laisse fuir ni clé, ni jeton, ni compte."""
    aioclient_mock.post(URL_JETON, json=reponse_de_jeton())
    aioclient_mock.post(URL_COMPTE, json=reponse_de_compte())

    with caplog.at_level(logging.DEBUG):
        resultat = await _jusqu_a_l_autorisation(hass, entree, ouvrir_les_options)
        resultat = await _retour_du_fournisseur(hass, resultat, code=CODE_AUTORISATION)
        resultat = await hass.config_entries.options.async_configure(
            resultat["flow_id"],
            {CONF_NAME: "Mon Dropbox", CONF_FOLDER: "Sauvegardes"},
        )
        await hass.async_block_till_done()
        assert resultat["type"] is FlowResultType.CREATE_ENTRY

        gestionnaire: DestinationManager = hass.data[DATA_DESTINATIONS]
        (destination,) = list(gestionnaire)
        await destination.async_check_connection()

    journaux = caplog.text
    for secret in SECRETS_A_NE_PAS_JOURNALISER:
        assert secret not in journaux, f"valeur sensible journalisée : {secret}"


async def test_un_message_d_erreur_volumineux_est_borne(
    hass: HomeAssistant,
    entree_dropbox: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Une réponse d'erreur bavarde ne se déverse pas entière dans un journal."""
    aioclient_mock.post(URL_COMPTE, status=500, text="erreur " * 500)

    with pytest.raises(DestinationError) as echec:
        await _destination(hass, entree_dropbox).async_check_connection()

    assert str(echec.value).endswith("...")
    assert len(str(echec.value)) < 400


async def test_un_corps_d_erreur_vide_reste_lisible(
    hass: HomeAssistant,
    entree_dropbox: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Un échec sans corps de réponse donne tout de même un message complet."""
    aioclient_mock.post(URL_COMPTE, status=502, text="")

    with pytest.raises(DestinationError, match="réponse vide"):
        await _destination(hass, entree_dropbox).async_check_connection()


def test_le_compte_ne_se_represente_pas() -> None:
    """Le compte connecté se représente sans nom, ni courriel, ni identifiant."""
    representation = repr(
        CompteDropbox(account_id=ACCOUNT_ID, display_name=NOM_AFFICHE, email=COURRIEL)
    )

    assert representation == "<CompteDropbox compte connecté>"
    for valeur in (ACCOUNT_ID, NOM_AFFICHE, COURRIEL):
        assert valeur not in representation


def test_la_representation_d_une_destination_ne_montre_rien(
    hass: HomeAssistant,
) -> None:
    """La destination et le compte se représentent sans donnée personnelle."""
    destination = _destination_directe(hass)

    representation = repr(destination)
    assert "dropbox_jeanne" in representation
    for secret in SECRETS_A_NE_PAS_JOURNALISER:
        assert secret not in representation

    config = DestinationConfig.from_dict(config_dropbox())
    masquee = config.as_dict(masquer=True)
    assert masquee[CONF_PROVIDER_DATA] == {CLE_ACCOUNT_ID: "***"}
    assert ACCOUNT_ID not in repr(config)
