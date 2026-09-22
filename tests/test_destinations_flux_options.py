"""Ajout, ré-autorisation et suppression d'une destination depuis l'UI (issue #7).

Ces tests parcourent le flux d'options de bout en bout : menu, choix du
fournisseur, saisie des identifiants d'application, étape externe d'autorisation,
retour simulé (y compris par un vrai appel HTTP sur la vue du fork), nommage,
puis persistance. Ils couvrent aussi la ré-autorisation d'une destination dont
l'accès a été révoqué et la suppression.

Aucun secret réel n'est employé : le fournisseur, ses URL et ses jetons sont
factices (cf. `tests/destinations_factices.py`), et le point de jeton est simulé
par `aioclient_mock`.
"""

from __future__ import annotations

import logging
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
from homeassistant.helpers.network import NoURLAvailableError
from homeassistant.setup import async_setup_component
from homeassistant.util import slugify
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
    CONF_RETENTION_COUNT,
    CONF_RETENTION_DAYS,
    DATA_DESTINATIONS,
    DATA_OAUTH_VIEW,
    DEFAULT_BACKUP_TIMEOUT,
    DOMAIN,
    OAUTH_CALLBACK_PATH,
)
from custom_components.auto_backup.destinations import (
    DestinationConfigError,
    DestinationManager,
    DestinationOAuth2Implementation,
    identifiant_du_probleme,
    list_providers,
    register_provider,
    unregister_provider,
)
from custom_components.auto_backup.destinations.flow import (
    IDENTIFIANT_PROVISOIRE,
    GestionDesDestinationsMixin,
    _identifiant_disponible,
    _libelle_du_fournisseur,
    _retention,
)
from custom_components.auto_backup.destinations.providers.dropbox import (
    LIBELLE_DROPBOX,
    PROVIDER_DROPBOX,
)
from custom_components.auto_backup.destinations.providers.google_drive import (
    LIBELLE_GOOGLE_DRIVE,
    PROVIDER_GOOGLE_DRIVE,
)
from destinations_factices import (
    CLIENT_ID_FACTICE,
    CLIENT_SECRET_FACTICE,
    CODE_AUTORISATION_FACTICE,
    PROVIDER_FACTICE,
    PROVIDER_OAUTH_FACTICE,
    URL_AUTORISATION_FACTICE,
    URL_JETON_FACTICE,
    DestinationOAuthEnMemoire,
    config_oauth_factice,
    jeton_factice,
    reponse_de_jeton_factice,
)

URL_EXTERNE = "https://auto-backup.exemple.test"

# Données de compte telles qu'un fournisseur les renvoie à l'autorisation
# (issue #10) : elles décrivent le compte rattaché à la destination, sans secret.
DONNEES_DU_COMPTE = {"account_id": "compte-factice-0000"}

type OuvrirLesOptions = Callable[[str, str], Awaitable[dict[str, Any]]]


@pytest.fixture
async def instance_joignable(hass: HomeAssistant) -> None:
    """Donne une URL externe à l'instance : sans elle, aucune redirection."""
    await async_process_ha_core_config(hass, {"external_url": URL_EXTERNE})


@pytest.fixture
async def entree(
    hass: HomeAssistant,
    integration_backup: None,
    instance_joignable: None,
    fournisseur_oauth_factice: str,
) -> MockConfigEntry:
    """Entrée `auto_backup` initialisée, sans aucune destination."""
    entree = MockConfigEntry(domain=DOMAIN, title="Auto Backup", data={})
    entree.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entree.entry_id)
    await hass.async_block_till_done()
    return entree


@pytest.fixture
async def entree_avec_destination(
    hass: HomeAssistant,
    integration_backup: None,
    instance_joignable: None,
    fournisseur_oauth_factice: str,
) -> MockConfigEntry:
    """Entrée portant déjà une destination autorisée en OAuth2."""
    entree = MockConfigEntry(
        domain=DOMAIN,
        title="Auto Backup",
        data={},
        options={CONF_DESTINATIONS: [config_oauth_factice()]},
    )
    entree.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entree.entry_id)
    await hass.async_block_till_done()
    return entree


def _gestionnaire(hass: HomeAssistant) -> DestinationManager:
    """Gestionnaire de destinations de l'entrée chargée."""
    return hass.data[DATA_DESTINATIONS]


async def _jusqu_a_l_autorisation(
    hass: HomeAssistant,
    entree: MockConfigEntry,
    ouvrir_les_options: OuvrirLesOptions,
) -> dict[str, Any]:
    """Déroule le flux d'ajout jusqu'à l'étape externe d'autorisation."""
    resultat = await ouvrir_les_options(entree.entry_id, "ajouter_destination")
    assert resultat["step_id"] == "ajouter_destination"

    resultat = await hass.config_entries.options.async_configure(
        resultat["flow_id"], {CONF_PROVIDER: PROVIDER_OAUTH_FACTICE}
    )
    assert resultat["step_id"] == "identifiants"
    assert resultat["description_placeholders"]["url_de_retour"] == (
        f"{URL_EXTERNE}{OAUTH_CALLBACK_PATH}"
    )

    resultat = await hass.config_entries.options.async_configure(
        resultat["flow_id"],
        {CONF_CLIENT_ID: CLIENT_ID_FACTICE, CONF_CLIENT_SECRET: CLIENT_SECRET_FACTICE},
    )
    assert resultat["type"] is FlowResultType.EXTERNAL_STEP
    assert resultat["step_id"] == "autorisation"
    return resultat


async def _retour_du_fournisseur(
    hass: HomeAssistant, resultat: dict[str, Any], **reponse: Any
) -> dict[str, Any]:
    """Simule le retour d'autorisation, comme le ferait la vue du fork."""
    url = URL(resultat["url"])
    donnees: dict[str, Any] = {
        "state": {"redirect_uri": url.query["redirect_uri"]},
        **reponse,
    }
    resultat = await hass.config_entries.options.async_configure(
        resultat["flow_id"], donnees
    )
    assert resultat["type"] is FlowResultType.EXTERNAL_STEP_DONE
    return await hass.config_entries.options.async_configure(resultat["flow_id"])


### Menu ###


async def test_le_menu_s_adapte_aux_destinations_existantes(
    hass: HomeAssistant, entree_avec_destination: MockConfigEntry
) -> None:
    """Avec une destination, le menu propose aussi ré-autoriser et supprimer."""
    resultat = await hass.config_entries.options.async_init(
        entree_avec_destination.entry_id
    )

    assert resultat["type"] is FlowResultType.MENU
    assert list(resultat["menu_options"]) == [
        "ajouter_destination",
        "reautoriser_destination",
        "supprimer_destination",
        "reglages_televersement",
        "init",
    ]


async def test_le_formulaire_upstream_conserve_une_destination_oauth(
    hass: HomeAssistant,
    integration_backup: None,
    instance_joignable: None,
    fournisseur_oauth_factice: str,
    ouvrir_les_options: OuvrirLesOptions,
) -> None:
    """Enregistrer le formulaire upstream ne perd pas une destination OAuth2.

    `test_le_flux_d_options_conserve_les_destinations` (issue #6, module
    `test_destinations_persistance.py`) le prouve déjà pour une destination
    sans authentification. Celle-ci porte en plus `client_secret` et `token` :
    exactement ce que `preserve_fork_options()` (issue #8, critère 6) doit
    reporter sans y toucher, pas seulement l'identifiant et le nom.
    """
    destination = config_oauth_factice()
    entree = MockConfigEntry(
        domain=DOMAIN,
        title="Auto Backup",
        data={},
        options={CONF_DESTINATIONS: [destination]},
    )
    entree.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entree.entry_id)
    await hass.async_block_till_done()

    resultat = await ouvrir_les_options(entree.entry_id, "init")
    resultat = await hass.config_entries.options.async_configure(
        resultat["flow_id"],
        user_input={CONF_AUTO_PURGE: False, CONF_BACKUP_TIMEOUT: 45},
    )
    await hass.async_block_till_done()

    assert resultat["type"] is FlowResultType.CREATE_ENTRY
    assert entree.options[CONF_AUTO_PURGE] is False
    # La destination — et ses secrets — sont reportés à l'identique.
    assert entree.options[CONF_DESTINATIONS] == [destination]
    (persistee,) = entree.options[CONF_DESTINATIONS]
    assert persistee[CONF_CLIENT_SECRET] == CLIENT_SECRET_FACTICE
    assert (
        persistee[CONF_TOKEN]["access_token"] == destination[CONF_TOKEN]["access_token"]
    )


async def test_sans_fournisseur_enregistre_l_ajout_est_impossible(
    hass: HomeAssistant,
    entree_auto_backup: MockConfigEntry,
    ouvrir_les_options: OuvrirLesOptions,
) -> None:
    """Registre vide : le flux le dit au lieu d'afficher un formulaire vide.

    Le cas ne se produit plus en fonctionnement normal depuis que l'intégration
    livre des fournisseurs réels (Dropbox #10, Google Drive #13) : le registre
    est simulé vide pour éprouver la branche, qui reste utile à une installation
    dont un fournisseur aurait été retiré.
    """
    with patch(
        "custom_components.auto_backup.destinations.flow.list_providers",
        return_value=(),
    ):
        resultat = await ouvrir_les_options(
            entree_auto_backup.entry_id, "ajouter_destination"
        )

    assert resultat["type"] is FlowResultType.ABORT
    assert resultat["reason"] == "aucun_fournisseur"


async def test_le_choix_du_fournisseur_liste_le_registre(
    hass: HomeAssistant,
    entree: MockConfigEntry,
    fournisseur_factice: str,
    ouvrir_les_options: OuvrirLesOptions,
) -> None:
    """Critère : les fournisseurs proposés sont ceux du registre.

    Dropbox (#10) et Google Drive (#13), enregistrés par l'intégration
    elle-même, y figurent sous leur libellé lisible ; les fournisseurs factices,
    qui n'en déclarent pas, restent affichés sous leur identifiant technique.
    """
    resultat = await ouvrir_les_options(entree.entry_id, "ajouter_destination")

    selecteur = resultat["data_schema"].schema[CONF_PROVIDER]
    libelles = {
        option["value"]: option["label"] for option in selecteur.config["options"]
    }
    assert list(libelles) == list(list_providers())
    assert {PROVIDER_FACTICE, PROVIDER_OAUTH_FACTICE} <= set(libelles)
    # Un fournisseur sans libellé garde son identifiant technique.
    assert libelles[PROVIDER_FACTICE] == PROVIDER_FACTICE
    assert libelles[PROVIDER_DROPBOX] == LIBELLE_DROPBOX
    assert libelles[PROVIDER_GOOGLE_DRIVE] == LIBELLE_GOOGLE_DRIVE


### Parcours complet d'ajout ###


async def test_le_parcours_complet_ajoute_une_destination_autorisee(
    hass: HomeAssistant,
    entree: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    ouvrir_les_options: OuvrirLesOptions,
) -> None:
    """Critère : fournisseur -> identifiants -> autorisation -> nommage -> options."""
    aioclient_mock.post(URL_JETON_FACTICE, json=reponse_de_jeton_factice())

    resultat = await _jusqu_a_l_autorisation(hass, entree, ouvrir_les_options)

    # L'URL externe est bien celle du fournisseur, avec un état à usage unique.
    url = URL(resultat["url"])
    assert str(url.with_query(None)) == URL_AUTORISATION_FACTICE
    assert url.query["state"]

    resultat = await _retour_du_fournisseur(
        hass, resultat, code=CODE_AUTORISATION_FACTICE
    )
    assert resultat["step_id"] == "destination"

    resultat = await hass.config_entries.options.async_configure(
        resultat["flow_id"],
        {
            CONF_NAME: "Mon nuage",
            CONF_FOLDER: "Sauvegardes/HA",
            CONF_RETENTION_DAYS: 7,
        },
    )
    await hass.async_block_till_done()

    assert resultat["type"] is FlowResultType.CREATE_ENTRY

    # Le code a bien été échangé contre un jeton.
    _, url_jeton, donnees, _ = aioclient_mock.mock_calls[-1]
    assert str(url_jeton) == URL_JETON_FACTICE
    assert donnees["grant_type"] == "authorization_code"
    assert donnees["code"] == CODE_AUTORISATION_FACTICE
    assert donnees["client_id"] == CLIENT_ID_FACTICE
    assert donnees["client_secret"] == CLIENT_SECRET_FACTICE

    # La destination est persistée, jeton compris, et les options upstream sont
    # complétées pour l'écouteur de mise à jour.
    (persistee,) = entree.options[CONF_DESTINATIONS]
    assert persistee[CONF_DESTINATION_ID] == "mon_nuage"
    assert persistee[CONF_PROVIDER] == PROVIDER_OAUTH_FACTICE
    assert persistee[CONF_NAME] == "Mon nuage"
    assert persistee[CONF_FOLDER] == "Sauvegardes/HA"
    assert persistee[CONF_RETENTION_DAYS] == 7
    assert persistee[CONF_RETENTION_COUNT] is None
    assert persistee[CONF_CLIENT_ID] == CLIENT_ID_FACTICE
    assert persistee[CONF_TOKEN]["access_token"] == "acces-factice-2"
    assert persistee[CONF_TOKEN]["expires_at"] > 0
    assert entree.options[CONF_AUTO_PURGE] is True
    assert entree.options[CONF_BACKUP_TIMEOUT] == DEFAULT_BACKUP_TIMEOUT

    # Le gestionnaire a rechargé la nouvelle destination.
    gestionnaire = _gestionnaire(hass)
    assert [destination.name for destination in gestionnaire] == ["Mon nuage"]


async def test_un_fournisseur_sans_oauth_saute_l_autorisation(
    hass: HomeAssistant,
    entree: MockConfigEntry,
    fournisseur_factice: str,
    ouvrir_les_options: OuvrirLesOptions,
) -> None:
    """Un fournisseur qui ne déclare pas OAuth2 n'ouvre aucun navigateur."""
    resultat = await ouvrir_les_options(entree.entry_id, "ajouter_destination")
    resultat = await hass.config_entries.options.async_configure(
        resultat["flow_id"], {CONF_PROVIDER: PROVIDER_FACTICE}
    )

    assert resultat["step_id"] == "destination"

    resultat = await hass.config_entries.options.async_configure(
        resultat["flow_id"], {CONF_NAME: "Disque local", CONF_FOLDER: "Sauvegardes"}
    )
    await hass.async_block_till_done()

    (persistee,) = entree.options[CONF_DESTINATIONS]
    assert CONF_CLIENT_ID not in persistee
    assert CONF_TOKEN not in persistee


async def test_le_nom_d_une_destination_doit_etre_unique(
    hass: HomeAssistant,
    entree_avec_destination: MockConfigEntry,
    fournisseur_factice: str,
    ouvrir_les_options: OuvrirLesOptions,
) -> None:
    """Point resté ouvert en #6 : deux destinations homonymes sont refusées."""
    resultat = await ouvrir_les_options(
        entree_avec_destination.entry_id, "ajouter_destination"
    )
    resultat = await hass.config_entries.options.async_configure(
        resultat["flow_id"], {CONF_PROVIDER: PROVIDER_FACTICE}
    )
    resultat = await hass.config_entries.options.async_configure(
        resultat["flow_id"],
        {CONF_NAME: "  destination oauth  ", CONF_FOLDER: "Sauvegardes"},
    )

    assert resultat["type"] is FlowResultType.FORM
    assert resultat["errors"] == {CONF_NAME: "nom_deja_utilise"}
    assert len(entree_avec_destination.options[CONF_DESTINATIONS]) == 1


@pytest.mark.parametrize(
    ("saisie", "champ", "erreur"),
    [
        ({CONF_NAME: "   ", CONF_FOLDER: "Sauvegardes"}, CONF_NAME, "nom_invalide"),
        (
            {CONF_NAME: "Ailleurs", CONF_FOLDER: "../etc"},
            CONF_FOLDER,
            "dossier_invalide",
        ),
    ],
)
async def test_une_saisie_invalide_est_signalee_sans_perdre_le_flux(
    hass: HomeAssistant,
    entree: MockConfigEntry,
    fournisseur_factice: str,
    ouvrir_les_options: OuvrirLesOptions,
    saisie: dict[str, Any],
    champ: str,
    erreur: str,
) -> None:
    """Un nom vide ou un dossier hors des clous est refusé avec un message."""
    resultat = await ouvrir_les_options(entree.entry_id, "ajouter_destination")
    resultat = await hass.config_entries.options.async_configure(
        resultat["flow_id"], {CONF_PROVIDER: PROVIDER_FACTICE}
    )
    resultat = await hass.config_entries.options.async_configure(
        resultat["flow_id"], saisie
    )

    assert resultat["type"] is FlowResultType.FORM
    assert resultat["errors"] == {champ: erreur}
    assert CONF_DESTINATIONS not in entree.options


async def test_des_identifiants_vides_sont_refuses(
    hass: HomeAssistant,
    entree: MockConfigEntry,
    ouvrir_les_options: OuvrirLesOptions,
) -> None:
    """Un identifiant d'application vide ne part pas chez le fournisseur."""
    resultat = await ouvrir_les_options(entree.entry_id, "ajouter_destination")
    resultat = await hass.config_entries.options.async_configure(
        resultat["flow_id"], {CONF_PROVIDER: PROVIDER_OAUTH_FACTICE}
    )
    resultat = await hass.config_entries.options.async_configure(
        resultat["flow_id"],
        {CONF_CLIENT_ID: "  ", CONF_CLIENT_SECRET: CLIENT_SECRET_FACTICE},
    )

    assert resultat["type"] is FlowResultType.FORM
    assert resultat["errors"] == {"base": "identifiants_invalides"}


async def test_sans_url_externe_l_ajout_s_interrompt(
    hass: HomeAssistant,
    integration_backup: None,
    fournisseur_oauth_factice: str,
    ouvrir_les_options: OuvrirLesOptions,
) -> None:
    """Sans URL joignable, le flux le dit plutôt que d'inventer une redirection."""
    entree = MockConfigEntry(domain=DOMAIN, title="Auto Backup", data={})
    entree.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entree.entry_id)
    await hass.async_block_till_done()

    resultat = await ouvrir_les_options(entree.entry_id, "ajouter_destination")
    resultat = await hass.config_entries.options.async_configure(
        resultat["flow_id"], {CONF_PROVIDER: PROVIDER_OAUTH_FACTICE}
    )

    assert resultat["type"] is FlowResultType.ABORT
    assert resultat["reason"] == "url_indisponible"


async def test_une_autorisation_refusee_interrompt_le_flux(
    hass: HomeAssistant,
    entree: MockConfigEntry,
    ouvrir_les_options: OuvrirLesOptions,
) -> None:
    """L'utilisateur refuse l'accès : rien n'est enregistré, le motif est rendu.

    `access_denied` reçoit un message dédié (issue #10) : montrer le code brut
    n'apprend rien à l'utilisateur qui vient de fermer la page d'autorisation.
    """
    resultat = await _jusqu_a_l_autorisation(hass, entree, ouvrir_les_options)

    resultat = await _retour_du_fournisseur(hass, resultat, error="access_denied")

    assert resultat["type"] is FlowResultType.ABORT
    assert resultat["reason"] == "autorisation_annulee"
    assert CONF_DESTINATIONS not in entree.options


async def test_un_refus_inconnu_est_rendu_tel_quel(
    hass: HomeAssistant,
    entree: MockConfigEntry,
    ouvrir_les_options: OuvrirLesOptions,
) -> None:
    """Un code d'erreur non répertorié reste cité dans le message générique."""
    resultat = await _jusqu_a_l_autorisation(hass, entree, ouvrir_les_options)

    resultat = await _retour_du_fournisseur(hass, resultat, error="server_error")

    assert resultat["type"] is FlowResultType.ABORT
    assert resultat["reason"] == "autorisation_refusee"
    assert resultat["description_placeholders"] == {"erreur": "server_error"}


async def test_un_echec_d_echange_du_code_interrompt_le_flux(
    hass: HomeAssistant,
    entree: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    ouvrir_les_options: OuvrirLesOptions,
) -> None:
    """Le fournisseur refuse le code : aucune destination à moitié créée."""
    aioclient_mock.post(URL_JETON_FACTICE, status=400, json={"error": "invalid_grant"})

    resultat = await _jusqu_a_l_autorisation(hass, entree, ouvrir_les_options)
    resultat = await _retour_du_fournisseur(
        hass, resultat, code=CODE_AUTORISATION_FACTICE
    )

    assert resultat["type"] is FlowResultType.ABORT
    assert resultat["reason"] == "autorisation_non_accordee"
    assert CONF_DESTINATIONS not in entree.options


async def test_un_jeton_inexploitable_interrompt_le_flux(
    hass: HomeAssistant,
    entree: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    ouvrir_les_options: OuvrirLesOptions,
) -> None:
    """Un fournisseur qui renvoie un jeton sans durée ne crée rien."""
    aioclient_mock.post(URL_JETON_FACTICE, json={"access_token": "acces-factice"})

    resultat = await _jusqu_a_l_autorisation(hass, entree, ouvrir_les_options)
    resultat = await _retour_du_fournisseur(
        hass, resultat, code=CODE_AUTORISATION_FACTICE
    )

    assert resultat["type"] is FlowResultType.ABORT
    assert resultat["reason"] == "jeton_invalide"


### Retour d'autorisation par la vue HTTP du fork ###


async def test_la_vue_de_retour_reprend_le_flux_d_options(
    hass: HomeAssistant,
    entree: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    ouvrir_les_options: OuvrirLesOptions,
    hass_client_no_auth: Callable[[], Awaitable[Any]],
) -> None:
    """Critère : le retour du fournisseur relance bien le flux d'options.

    C'est la raison d'être de la vue du fork : celle du cœur de Home Assistant
    ne sait reprendre qu'un config flow.
    """
    assert await async_setup_component(hass, "http", {})
    aioclient_mock.post(URL_JETON_FACTICE, json=reponse_de_jeton_factice())

    resultat = await _jusqu_a_l_autorisation(hass, entree, ouvrir_les_options)
    etat = URL(resultat["url"]).query["state"]

    client = await hass_client_no_auth()
    reponse = await client.get(
        f"{OAUTH_CALLBACK_PATH}?code={CODE_AUTORISATION_FACTICE}&state={etat}"
    )

    assert reponse.status == 200
    assert "window.close()" in await reponse.text()

    suite = await hass.config_entries.options.async_configure(resultat["flow_id"])
    assert suite["step_id"] == "destination"

    # L'état est à usage unique : rejouer le retour ne relance rien.
    rejeu = await client.get(
        f"{OAUTH_CALLBACK_PATH}?code={CODE_AUTORISATION_FACTICE}&state={etat}"
    )
    assert rejeu.status == 400


@pytest.mark.parametrize(
    "requete",
    ["", "?state=etat-invente", "?code=un-code"],
)
async def test_la_vue_de_retour_refuse_les_appels_incoherents(
    hass: HomeAssistant,
    entree: MockConfigEntry,
    ouvrir_les_options: OuvrirLesOptions,
    hass_client_no_auth: Callable[[], Awaitable[Any]],
    requete: str,
) -> None:
    """Un état absent ou inconnu ne reprend aucun flux."""
    assert await async_setup_component(hass, "http", {})
    await _jusqu_a_l_autorisation(hass, entree, ouvrir_les_options)

    client = await hass_client_no_auth()
    reponse = await client.get(f"{OAUTH_CALLBACK_PATH}{requete}")

    assert reponse.status == 400


async def test_la_vue_de_retour_refuse_une_reponse_sans_code_ni_erreur(
    hass: HomeAssistant,
    entree: MockConfigEntry,
    ouvrir_les_options: OuvrirLesOptions,
    hass_client_no_auth: Callable[[], Awaitable[Any]],
) -> None:
    """Un état valide ne suffit pas : le fournisseur doit répondre quelque chose."""
    assert await async_setup_component(hass, "http", {})
    resultat = await _jusqu_a_l_autorisation(hass, entree, ouvrir_les_options)
    etat = URL(resultat["url"]).query["state"]

    client = await hass_client_no_auth()
    reponse = await client.get(f"{OAUTH_CALLBACK_PATH}?state={etat}")

    assert reponse.status == 400


async def test_la_vue_de_retour_supporte_un_flux_deja_termine(
    hass: HomeAssistant,
    entree: MockConfigEntry,
    ouvrir_les_options: OuvrirLesOptions,
    hass_client_no_auth: Callable[[], Awaitable[Any]],
) -> None:
    """Un retour tardif, après abandon du flux, ne provoque pas d'erreur 500."""
    assert await async_setup_component(hass, "http", {})
    resultat = await _jusqu_a_l_autorisation(hass, entree, ouvrir_les_options)
    etat = URL(resultat["url"]).query["state"]

    hass.config_entries.options.async_abort(resultat["flow_id"])

    client = await hass_client_no_auth()
    reponse = await client.get(
        f"{OAUTH_CALLBACK_PATH}?code={CODE_AUTORISATION_FACTICE}&state={etat}"
    )

    assert reponse.status == 400


async def test_la_vue_de_retour_transmet_un_refus(
    hass: HomeAssistant,
    entree: MockConfigEntry,
    ouvrir_les_options: OuvrirLesOptions,
    hass_client_no_auth: Callable[[], Awaitable[Any]],
) -> None:
    """Un refus du fournisseur est transmis au flux, pas avalé par la vue."""
    assert await async_setup_component(hass, "http", {})

    resultat = await _jusqu_a_l_autorisation(hass, entree, ouvrir_les_options)
    etat = URL(resultat["url"]).query["state"]

    client = await hass_client_no_auth()
    reponse = await client.get(
        f"{OAUTH_CALLBACK_PATH}?error=access_denied&state={etat}"
    )
    assert reponse.status == 200

    suite = await hass.config_entries.options.async_configure(resultat["flow_id"])
    assert suite["type"] is FlowResultType.ABORT
    assert suite["reason"] == "autorisation_annulee"


### Ré-autorisation ###


async def test_la_reautorisation_remplace_le_jeton_et_efface_le_probleme(
    hass: HomeAssistant,
    entree_avec_destination: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    ouvrir_les_options: OuvrirLesOptions,
) -> None:
    """Critère : ré-autoriser une destination la remet en service, elle seule."""
    gestionnaire = _gestionnaire(hass)
    gestionnaire.async_marquer_la_reauthentification("destination_oauth")
    ir.async_create_issue(
        hass,
        DOMAIN,
        identifiant_du_probleme("destination_oauth"),
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="reauthentification_requise",
    )
    aioclient_mock.post(URL_JETON_FACTICE, json=reponse_de_jeton_factice())

    resultat = await ouvrir_les_options(
        entree_avec_destination.entry_id, "reautoriser_destination"
    )
    assert resultat["step_id"] == "reautoriser_destination"

    resultat = await hass.config_entries.options.async_configure(
        resultat["flow_id"], {CONF_DESTINATION_ID: "destination_oauth"}
    )
    assert resultat["type"] is FlowResultType.EXTERNAL_STEP

    resultat = await _retour_du_fournisseur(
        hass, resultat, code=CODE_AUTORISATION_FACTICE
    )
    await hass.async_block_till_done()

    assert resultat["type"] is FlowResultType.CREATE_ENTRY

    # Seul le jeton change : le nom, le dossier et la rétention sont conservés.
    (persistee,) = entree_avec_destination.options[CONF_DESTINATIONS]
    attendue = config_oauth_factice()
    assert persistee[CONF_NAME] == attendue[CONF_NAME]
    assert persistee[CONF_FOLDER] == attendue[CONF_FOLDER]
    assert persistee[CONF_RETENTION_DAYS] == attendue[CONF_RETENTION_DAYS]
    assert persistee[CONF_TOKEN]["access_token"] == "acces-factice-2"

    assert _gestionnaire(hass).reauthentification_requise("destination_oauth") is False
    assert (
        ir.async_get(hass).async_get_issue(
            DOMAIN, identifiant_du_probleme("destination_oauth")
        )
        is None
    )


async def test_la_reautorisation_conserve_les_donnees_du_compte(
    hass: HomeAssistant,
    integration_backup: None,
    instance_joignable: None,
    fournisseur_oauth_factice: str,
    aioclient_mock: AiohttpClientMocker,
    ouvrir_les_options: OuvrirLesOptions,
) -> None:
    """Ré-autoriser ne remplace que les champs d'autorisation.

    Les données du compte (`provider_data`, issue #10) décrivent la destination,
    pas son autorisation : les perdre en ré-autorisant reviendrait à oublier à
    quel compte la destination est rattachée, sans rien signaler à l'utilisateur.
    """
    entree = MockConfigEntry(
        domain=DOMAIN,
        title="Auto Backup",
        data={},
        options={
            CONF_DESTINATIONS: [config_oauth_factice(provider_data=DONNEES_DU_COMPTE)]
        },
    )
    entree.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entree.entry_id)
    await hass.async_block_till_done()
    aioclient_mock.post(URL_JETON_FACTICE, json=reponse_de_jeton_factice())

    resultat = await ouvrir_les_options(entree.entry_id, "reautoriser_destination")
    resultat = await hass.config_entries.options.async_configure(
        resultat["flow_id"], {CONF_DESTINATION_ID: "destination_oauth"}
    )
    resultat = await _retour_du_fournisseur(
        hass, resultat, code=CODE_AUTORISATION_FACTICE
    )
    await hass.async_block_till_done()

    assert resultat["type"] is FlowResultType.CREATE_ENTRY

    (persistee,) = entree.options[CONF_DESTINATIONS]
    assert persistee[CONF_TOKEN]["access_token"] == "acces-factice-2"
    assert persistee[CONF_PROVIDER_DATA] == DONNEES_DU_COMPTE
    # Et le reste de la configuration est intact.
    attendue = config_oauth_factice()
    assert persistee[CONF_NAME] == attendue[CONF_NAME]
    assert persistee[CONF_FOLDER] == attendue[CONF_FOLDER]
    assert persistee[CONF_RETENTION_DAYS] == attendue[CONF_RETENTION_DAYS]
    assert persistee[CONF_RETENTION_COUNT] == attendue[CONF_RETENTION_COUNT]


async def test_sans_destination_oauth_la_reautorisation_est_impossible(
    hass: HomeAssistant,
    integration_backup: None,
    instance_joignable: None,
    fournisseur_factice: str,
    ouvrir_les_options: OuvrirLesOptions,
) -> None:
    """Une destination sans OAuth2 n'a rien à ré-autoriser."""
    entree = MockConfigEntry(
        domain=DOMAIN,
        title="Auto Backup",
        data={},
        options={
            CONF_DESTINATIONS: [
                {
                    CONF_DESTINATION_ID: "locale",
                    CONF_PROVIDER: PROVIDER_FACTICE,
                    CONF_NAME: "Locale",
                }
            ]
        },
    )
    entree.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entree.entry_id)
    await hass.async_block_till_done()

    resultat = await ouvrir_les_options(entree.entry_id, "reautoriser_destination")

    assert resultat["type"] is FlowResultType.ABORT
    assert resultat["reason"] == "aucune_destination_oauth"


### Suppression ###


async def test_la_suppression_efface_la_configuration_et_le_jeton(
    hass: HomeAssistant,
    entree_avec_destination: MockConfigEntry,
    ouvrir_les_options: OuvrirLesOptions,
) -> None:
    """Critère : après suppression, plus rien ne référence la destination."""
    gestionnaire = _gestionnaire(hass)
    gestionnaire.async_marquer_la_reauthentification("destination_oauth")
    ir.async_create_issue(
        hass,
        DOMAIN,
        identifiant_du_probleme("destination_oauth"),
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="reauthentification_requise",
    )

    resultat = await ouvrir_les_options(
        entree_avec_destination.entry_id, "supprimer_destination"
    )
    assert resultat["step_id"] == "supprimer_destination"

    resultat = await hass.config_entries.options.async_configure(
        resultat["flow_id"], {CONF_DESTINATIONS: ["destination_oauth"]}
    )
    await hass.async_block_till_done()

    assert resultat["type"] is FlowResultType.CREATE_ENTRY
    assert entree_avec_destination.options[CONF_DESTINATIONS] == []

    # Aucune trace : ni dans les options, ni dans le gestionnaire, ni en problème.
    texte_des_options = repr(entree_avec_destination.options)
    assert jeton_factice()["refresh_token"] not in texte_des_options
    assert CLIENT_SECRET_FACTICE not in texte_des_options
    assert len(_gestionnaire(hass)) == 0
    assert "destination_oauth" not in _gestionnaire(hass)
    assert _gestionnaire(hass).reauthentification_requise("destination_oauth") is False
    assert (
        ir.async_get(hass).async_get_issue(
            DOMAIN, identifiant_du_probleme("destination_oauth")
        )
        is None
    )


async def test_la_suppression_exige_une_selection(
    hass: HomeAssistant,
    entree_avec_destination: MockConfigEntry,
    ouvrir_les_options: OuvrirLesOptions,
) -> None:
    """Valider sans rien cocher ne supprime rien."""
    resultat = await ouvrir_les_options(
        entree_avec_destination.entry_id, "supprimer_destination"
    )
    resultat = await hass.config_entries.options.async_configure(
        resultat["flow_id"], {CONF_DESTINATIONS: []}
    )

    assert resultat["type"] is FlowResultType.FORM
    assert resultat["errors"] == {"base": "aucune_selection"}
    assert len(entree_avec_destination.options[CONF_DESTINATIONS]) == 1


async def test_une_destination_disparue_pendant_le_flux_interrompt_la_suppression(
    hass: HomeAssistant,
    entree_avec_destination: MockConfigEntry,
    ouvrir_les_options: OuvrirLesOptions,
) -> None:
    """Le menu masque l'entrée sans destination ; l'étape reste défensive.

    Le cas se produit quand les options changent pendant que le formulaire est
    ouvert : un second onglet, ou une restauration de sauvegarde.
    """
    resultat = await ouvrir_les_options(
        entree_avec_destination.entry_id, "supprimer_destination"
    )
    assert resultat["step_id"] == "supprimer_destination"

    hass.config_entries.async_update_entry(
        entree_avec_destination,
        options={
            CONF_AUTO_PURGE: True,
            CONF_BACKUP_TIMEOUT: DEFAULT_BACKUP_TIMEOUT,
            CONF_DESTINATIONS: [],
        },
    )
    await hass.async_block_till_done()

    resultat = await hass.config_entries.options.async_configure(
        resultat["flow_id"], {CONF_DESTINATIONS: ["destination_oauth"]}
    )

    assert resultat["type"] is FlowResultType.ABORT
    assert resultat["reason"] == "aucune_destination"


### Journaux ###


async def test_aucun_secret_n_est_journalise_meme_en_debug(
    hass: HomeAssistant,
    entree: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    ouvrir_les_options: OuvrirLesOptions,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Critère : le parcours complet ne laisse aucun secret dans les journaux."""
    aioclient_mock.post(URL_JETON_FACTICE, json=reponse_de_jeton_factice())
    caplog.set_level(logging.DEBUG)

    resultat = await _jusqu_a_l_autorisation(hass, entree, ouvrir_les_options)
    resultat = await _retour_du_fournisseur(
        hass, resultat, code=CODE_AUTORISATION_FACTICE
    )
    resultat = await hass.config_entries.options.async_configure(
        resultat["flow_id"], {CONF_NAME: "Mon nuage", CONF_FOLDER: "Sauvegardes"}
    )
    await hass.async_block_till_done()
    assert resultat["type"] is FlowResultType.CREATE_ENTRY

    journaux = caplog.text
    secrets_attendus = [
        CLIENT_SECRET_FACTICE,
        CODE_AUTORISATION_FACTICE,
        reponse_de_jeton_factice()["access_token"],
        reponse_de_jeton_factice()["refresh_token"],
    ]
    fuites = [secret for secret in secrets_attendus if secret in journaux]
    assert not fuites, f"secrets présents dans les journaux : {fuites}"


### Cas limites du flux ###


@pytest.mark.parametrize(
    ("valeur", "attendu"), [(None, None), ("", None), (7, 7), (7.0, 7), ("7", 7)]
)
def test_une_retention_saisie_devient_un_entier(
    valeur: Any, attendu: int | None
) -> None:
    """Les sélecteurs numériques renvoient des flottants, la config des entiers."""
    assert _retention({CONF_RETENTION_DAYS: valeur}, CONF_RETENTION_DAYS) == attendu


def test_une_retention_illisible_est_refusee() -> None:
    """Une valeur non numérique ne peut pas devenir une rétention."""
    with pytest.raises(DestinationConfigError):
        _retention({CONF_RETENTION_DAYS: "sept"}, CONF_RETENTION_DAYS)


def test_un_fournisseur_sans_libelle_garde_son_identifiant() -> None:
    """Le libellé est facultatif : un fournisseur qui n'en a pas reste lisible.

    Le cas couvre aussi un fournisseur absent du registre : une destination
    peut citer un fournisseur retiré depuis, et le sélecteur de suppression
    doit tout de même l'afficher.
    """
    assert _libelle_du_fournisseur(PROVIDER_FACTICE) == PROVIDER_FACTICE
    assert _libelle_du_fournisseur("fournisseur_inexistant") == (
        "fournisseur_inexistant"
    )


def test_le_libelle_du_fournisseur_retombe_sur_l_identifiant() -> None:
    """Sans fournisseur choisi, ou s'il a disparu, l'affichage reste correct."""
    flux = GestionDesDestinationsMixin()

    assert flux._libelle_du_fournisseur() == ""

    flux._provider = "fournisseur_disparu"
    assert flux._libelle_du_fournisseur() == "fournisseur_disparu"


def test_un_identifiant_deja_pris_recoit_un_suffixe() -> None:
    """Deux destinations de noms voisins gardent des identifiants distincts."""
    premier = _identifiant_disponible("Mon nuage", [])
    second = _identifiant_disponible("Mon nuage", [premier])

    assert premier == "mon_nuage"
    assert second.startswith("mon_nuage_")
    assert second != premier


def test_l_identifiant_de_la_destination_provisoire_n_est_jamais_attribue() -> None:
    """Aucune destination réelle ne peut porter l'identifiant du flux d'ajout.

    « Autorisation en cours » se translittère exactement comme la destination
    fictive portée pendant l'autorisation. Si une destination réelle recevait cet
    identifiant, le nettoyage de fin d'ajout effacerait son signalement de
    ré-authentification : elle resterait muette au lieu d'être réparable.
    """
    assert slugify("Autorisation en cours") == IDENTIFIANT_PROVISOIRE

    identifiant = _identifiant_disponible("Autorisation en cours", [])

    assert identifiant != IDENTIFIANT_PROVISOIRE
    assert identifiant.startswith(f"{IDENTIFIANT_PROVISOIRE}_")


def test_un_nom_sans_caractere_translitterable_reste_identifiable() -> None:
    """Un nom entièrement symbolique produit tout de même un identifiant."""
    identifiant = _identifiant_disponible("♥♥♥", [])

    assert identifiant
    assert _identifiant_disponible("♥♥♥", [identifiant]) != identifiant


async def test_une_destination_illisible_n_empeche_pas_la_gestion_des_autres(
    hass: HomeAssistant,
    integration_backup: None,
    instance_joignable: None,
    fournisseur_oauth_factice: str,
    ouvrir_les_options: OuvrirLesOptions,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Une destination corrompue dans les options est écartée, pas bloquante."""
    entree = MockConfigEntry(
        domain=DOMAIN,
        title="Auto Backup",
        data={},
        options={
            CONF_DESTINATIONS: [
                {CONF_DESTINATION_ID: "cassee"},
                config_oauth_factice(),
            ]
        },
    )
    entree.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entree.entry_id)
    await hass.async_block_till_done()

    resultat = await ouvrir_les_options(entree.entry_id, "supprimer_destination")

    selecteur = resultat["data_schema"].schema[CONF_DESTINATIONS]
    proposees = [option["value"] for option in selecteur.config["options"]]
    assert proposees == ["destination_oauth"]
    assert "configuration invalide" in caplog.text


async def test_sans_serveur_http_l_autorisation_s_interrompt(
    hass: HomeAssistant,
    entree: MockConfigEntry,
    ouvrir_les_options: OuvrirLesOptions,
) -> None:
    """Sans vue de retour enregistrable, mieux vaut ne pas ouvrir le navigateur."""
    resultat = await ouvrir_les_options(entree.entry_id, "ajouter_destination")
    resultat = await hass.config_entries.options.async_configure(
        resultat["flow_id"], {CONF_PROVIDER: PROVIDER_OAUTH_FACTICE}
    )

    hass.data.pop(DATA_OAUTH_VIEW, None)
    with patch.object(hass, "http", None, create=True):
        resultat = await hass.config_entries.options.async_configure(
            resultat["flow_id"],
            {
                CONF_CLIENT_ID: CLIENT_ID_FACTICE,
                CONF_CLIENT_SECRET: CLIENT_SECRET_FACTICE,
            },
        )

    assert resultat["type"] is FlowResultType.ABORT
    assert resultat["reason"] == "vue_indisponible"


async def test_un_fournisseur_retire_pendant_le_flux_interrompt_l_autorisation(
    hass: HomeAssistant,
    entree: MockConfigEntry,
    ouvrir_les_options: OuvrirLesOptions,
) -> None:
    """Le fournisseur peut disparaître du registre (mise à jour, rechargement)."""
    resultat = await ouvrir_les_options(entree.entry_id, "ajouter_destination")
    resultat = await hass.config_entries.options.async_configure(
        resultat["flow_id"], {CONF_PROVIDER: PROVIDER_OAUTH_FACTICE}
    )

    unregister_provider(PROVIDER_OAUTH_FACTICE)
    try:
        resultat = await hass.config_entries.options.async_configure(
            resultat["flow_id"],
            {
                CONF_CLIENT_ID: CLIENT_ID_FACTICE,
                CONF_CLIENT_SECRET: CLIENT_SECRET_FACTICE,
            },
        )
    finally:
        register_provider(PROVIDER_OAUTH_FACTICE, DestinationOAuthEnMemoire)

    assert resultat["type"] is FlowResultType.ABORT
    assert resultat["reason"] == "fournisseur_invalide"


async def test_une_panne_reseau_pendant_l_echange_interrompt_le_flux(
    hass: HomeAssistant,
    entree: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    ouvrir_les_options: OuvrirLesOptions,
) -> None:
    """Le fournisseur injoignable : le flux s'arrête sans destination bancale."""
    aioclient_mock.post(URL_JETON_FACTICE, exc=ClientError("réseau indisponible"))

    resultat = await _jusqu_a_l_autorisation(hass, entree, ouvrir_les_options)
    resultat = await _retour_du_fournisseur(
        hass, resultat, code=CODE_AUTORISATION_FACTICE
    )

    assert resultat["type"] is FlowResultType.ABORT
    assert resultat["reason"] == "echec_jeton"
    assert CONF_DESTINATIONS not in entree.options


async def test_une_destination_sans_identifiants_les_redemande(
    hass: HomeAssistant,
    integration_backup: None,
    instance_joignable: None,
    fournisseur_oauth_factice: str,
    ouvrir_les_options: OuvrirLesOptions,
) -> None:
    """Options éditées à la main : la ré-autorisation redemande l'application.

    L'identifiant client déjà connu est resuggéré ; le secret, lui, n'est jamais
    réaffiché et doit être ressaisi.
    """
    entree = MockConfigEntry(
        domain=DOMAIN,
        title="Auto Backup",
        data={},
        options={CONF_DESTINATIONS: [config_oauth_factice(client_secret=None)]},
    )
    entree.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entree.entry_id)
    await hass.async_block_till_done()

    resultat = await ouvrir_les_options(entree.entry_id, "reautoriser_destination")
    resultat = await hass.config_entries.options.async_configure(
        resultat["flow_id"], {CONF_DESTINATION_ID: "destination_oauth"}
    )

    assert resultat["type"] is FlowResultType.FORM
    assert resultat["step_id"] == "identifiants"
    suggestions = [
        cle.description.get("suggested_value")
        for cle in resultat["data_schema"].schema
        if cle == CONF_CLIENT_ID
    ]
    assert suggestions == [CLIENT_ID_FACTICE]


async def test_une_destination_de_fournisseur_inconnu_ne_se_reautorise_pas(
    hass: HomeAssistant,
    integration_backup: None,
    instance_joignable: None,
    ouvrir_les_options: OuvrirLesOptions,
) -> None:
    """Un fournisseur absent du registre n'ouvre aucune ré-autorisation."""
    entree = MockConfigEntry(
        domain=DOMAIN,
        title="Auto Backup",
        data={},
        options={CONF_DESTINATIONS: [config_oauth_factice()]},
    )
    entree.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entree.entry_id)
    await hass.async_block_till_done()

    resultat = await ouvrir_les_options(entree.entry_id, "reautoriser_destination")

    assert resultat["type"] is FlowResultType.ABORT
    assert resultat["reason"] == "aucune_destination_oauth"


async def test_une_destination_disparue_pendant_le_flux_interrompt_la_reautorisation(
    hass: HomeAssistant,
    entree_avec_destination: MockConfigEntry,
    ouvrir_les_options: OuvrirLesOptions,
) -> None:
    """La destination visée peut avoir été supprimée depuis un autre onglet."""
    resultat = await ouvrir_les_options(
        entree_avec_destination.entry_id, "reautoriser_destination"
    )

    # La destination visée disparaît, une autre la remplace : le menu reste
    # proposable, mais l'identifiant choisi n'existe plus.
    hass.config_entries.async_update_entry(
        entree_avec_destination,
        options={
            CONF_AUTO_PURGE: True,
            CONF_BACKUP_TIMEOUT: DEFAULT_BACKUP_TIMEOUT,
            CONF_DESTINATIONS: [
                config_oauth_factice(destination_id="une_autre", name="Une autre")
            ],
        },
    )
    await hass.async_block_till_done()

    resultat = await hass.config_entries.options.async_configure(
        resultat["flow_id"], {CONF_DESTINATION_ID: "destination_oauth"}
    )

    assert resultat["type"] is FlowResultType.ABORT
    assert resultat["reason"] == "destination_inconnue"


@pytest.mark.parametrize(
    ("echec", "motif"),
    [
        (TimeoutError(), "delai_url_autorisation"),
        (NoURLAvailableError(), "url_indisponible"),
    ],
)
async def test_un_echec_de_generation_d_url_interrompt_le_flux(
    hass: HomeAssistant,
    entree: MockConfigEntry,
    ouvrir_les_options: OuvrirLesOptions,
    echec: Exception,
    motif: str,
) -> None:
    """L'URL d'autorisation peut ne jamais venir : le flux le dit."""
    resultat = await ouvrir_les_options(entree.entry_id, "ajouter_destination")
    resultat = await hass.config_entries.options.async_configure(
        resultat["flow_id"], {CONF_PROVIDER: PROVIDER_OAUTH_FACTICE}
    )

    with patch.object(
        DestinationOAuth2Implementation,
        "async_generate_authorize_url",
        side_effect=echec,
    ):
        resultat = await hass.config_entries.options.async_configure(
            resultat["flow_id"],
            {
                CONF_CLIENT_ID: CLIENT_ID_FACTICE,
                CONF_CLIENT_SECRET: CLIENT_SECRET_FACTICE,
            },
        )

    assert resultat["type"] is FlowResultType.ABORT
    assert resultat["reason"] == motif


async def test_un_echange_de_code_qui_n_aboutit_pas_interrompt_le_flux(
    hass: HomeAssistant,
    entree: MockConfigEntry,
    ouvrir_les_options: OuvrirLesOptions,
) -> None:
    """Le fournisseur ne répond jamais : le flux s'arrête au bout du délai."""
    resultat = await _jusqu_a_l_autorisation(hass, entree, ouvrir_les_options)

    with patch.object(
        DestinationOAuth2Implementation,
        "async_resolve_external_data",
        side_effect=TimeoutError,
    ):
        resultat = await _retour_du_fournisseur(
            hass, resultat, code=CODE_AUTORISATION_FACTICE
        )

    assert resultat["type"] is FlowResultType.ABORT
    assert resultat["reason"] == "delai_jeton"


async def test_un_fournisseur_retire_avant_l_echange_interrompt_le_flux(
    hass: HomeAssistant,
    entree: MockConfigEntry,
    ouvrir_les_options: OuvrirLesOptions,
) -> None:
    """Le registre peut changer entre l'autorisation et l'échange du code."""
    resultat = await _jusqu_a_l_autorisation(hass, entree, ouvrir_les_options)

    unregister_provider(PROVIDER_OAUTH_FACTICE)
    try:
        resultat = await _retour_du_fournisseur(
            hass, resultat, code=CODE_AUTORISATION_FACTICE
        )
    finally:
        register_provider(PROVIDER_OAUTH_FACTICE, DestinationOAuthEnMemoire)

    assert resultat["type"] is FlowResultType.ABORT
    assert resultat["reason"] == "fournisseur_invalide"


async def test_une_destination_supprimee_pendant_l_autorisation_n_est_pas_recreee(
    hass: HomeAssistant,
    entree_avec_destination: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    ouvrir_les_options: OuvrirLesOptions,
) -> None:
    """Une ré-autorisation ne doit pas ressusciter une destination supprimée."""
    aioclient_mock.post(URL_JETON_FACTICE, json=reponse_de_jeton_factice())

    resultat = await ouvrir_les_options(
        entree_avec_destination.entry_id, "reautoriser_destination"
    )
    resultat = await hass.config_entries.options.async_configure(
        resultat["flow_id"], {CONF_DESTINATION_ID: "destination_oauth"}
    )

    hass.config_entries.async_update_entry(
        entree_avec_destination,
        options={
            CONF_AUTO_PURGE: True,
            CONF_BACKUP_TIMEOUT: DEFAULT_BACKUP_TIMEOUT,
            CONF_DESTINATIONS: [],
        },
    )
    await hass.async_block_till_done()

    resultat = await _retour_du_fournisseur(
        hass, resultat, code=CODE_AUTORISATION_FACTICE
    )

    assert resultat["type"] is FlowResultType.ABORT
    assert resultat["reason"] == "destination_inconnue"
    assert entree_avec_destination.options[CONF_DESTINATIONS] == []


async def test_un_fournisseur_retire_avant_le_choix_interrompt_le_flux(
    hass: HomeAssistant,
    entree: MockConfigEntry,
    fournisseur_factice: str,
    ouvrir_les_options: OuvrirLesOptions,
) -> None:
    """Le registre peut changer pendant que le formulaire est ouvert."""
    resultat = await ouvrir_les_options(entree.entry_id, "ajouter_destination")

    unregister_provider(PROVIDER_OAUTH_FACTICE)
    try:
        resultat = await hass.config_entries.options.async_configure(
            resultat["flow_id"], {CONF_PROVIDER: PROVIDER_OAUTH_FACTICE}
        )
    finally:
        register_provider(PROVIDER_OAUTH_FACTICE, DestinationOAuthEnMemoire)

    assert resultat["type"] is FlowResultType.ABORT
    assert resultat["reason"] == "fournisseur_invalide"
