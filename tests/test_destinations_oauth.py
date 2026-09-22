"""Autorisation OAuth2 des destinations distantes (issue #7).

Ces tests couvrent le socle : déclaration d'un fournisseur OAuth2, masquage des
secrets, états d'autorisation, rafraîchissement automatique du jeton avant une
opération, bascule en « ré-authentification requise » quand l'accès est révoqué,
et vue de retour qui reprend le flux d'options.

Aucun appel réseau n'est fait : le point de jeton du fournisseur factice est
simulé par `aioclient_mock`, et toutes les valeurs sont inventées
(cf. `tests/destinations_factices.py`).
"""

from __future__ import annotations

import time
from unittest.mock import Mock, patch

import pytest
from homeassistant.const import CONF_CLIENT_ID, CONF_CLIENT_SECRET, CONF_TOKEN
from homeassistant.core import HomeAssistant
from homeassistant.core_config import async_process_ha_core_config
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.config_entry_oauth2_flow import HEADER_FRONTEND_BASE
from homeassistant.helpers.http import current_request
from homeassistant.helpers.network import NoURLAvailableError
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker
from yarl import URL

from custom_components.auto_backup.const import (
    CONF_AUTO_PURGE,
    CONF_BACKUP_TIMEOUT,
    CONF_DESTINATIONS,
    DATA_DESTINATIONS,
    DATA_OAUTH_STATES,
    DATA_OAUTH_VIEW,
    DOMAIN,
    OAUTH_CALLBACK_PATH,
)
from custom_components.auto_backup.destinations import (
    VALEUR_MASQUEE,
    DestinationAuthError,
    DestinationConfig,
    DestinationConfigError,
    DestinationError,
    DestinationManager,
    DestinationNotFoundError,
    DestinationOAuth2Session,
    async_effacer_la_reauthentification,
    async_enregistrer_la_vue_de_retour,
    async_entree_auto_backup,
    async_persist_token,
    identifiant_du_probleme,
    jeton_persiste,
    jeton_valide,
    normaliser_le_jeton,
    reauthentification_requise,
    spec_oauth_du_fournisseur,
    url_de_retour,
)
from custom_components.auto_backup.destinations.oauth import (
    EtatOAuth,
    consommer_un_etat,
    enregistrer_un_etat,
    implementation_de_la_destination,
    oublier_les_etats_du_flux,
)
from destinations_factices import (
    CLIENT_ID_FACTICE,
    CLIENT_SECRET_FACTICE,
    PORTEE_FACTICE,
    PROVIDER_FACTICE,
    PROVIDER_OAUTH_FACTICE,
    SPEC_OAUTH_FACTICE,
    URL_AUTORISATION_FACTICE,
    URL_JETON_FACTICE,
    DestinationOAuthEnMemoire,
    config_oauth_factice,
    jeton_factice,
    reponse_de_jeton_factice,
)

URL_EXTERNE = "https://auto-backup.exemple.test"
URL_DE_RETOUR_ATTENDUE = f"{URL_EXTERNE}{OAUTH_CALLBACK_PATH}"


@pytest.fixture
async def url_externe(hass: HomeAssistant) -> str:
    """Donne une URL externe à l'instance : elle sert d'URI de redirection."""
    await async_process_ha_core_config(hass, {"external_url": URL_EXTERNE})
    return URL_EXTERNE


@pytest.fixture
async def entree_oauth(
    hass: HomeAssistant,
    integration_backup: None,
    fournisseur_oauth_factice: str,
    fournisseur_factice: str,
) -> MockConfigEntry:
    """Entrée portant une destination OAuth2 et une destination sans OAuth2.

    Les deux coexistent volontairement : les tests vérifient qu'un accès révoqué
    n'empêche jamais les autres destinations de fonctionner.
    """
    entree = MockConfigEntry(
        domain=DOMAIN,
        title="Auto Backup",
        data={},
        options={
            # Les options upstream sont présentes : les écritures directes de ces
            # tests ne passent pas par `options_avec_destinations()`, qui les
            # compléterait. Leur complétion a son propre test.
            CONF_AUTO_PURGE: True,
            CONF_BACKUP_TIMEOUT: 20,
            CONF_DESTINATIONS: [
                config_oauth_factice(),
                config_oauth_factice(
                    destination_id="destination_oauth_2", name="Seconde destination"
                ),
            ],
        },
    )
    entree.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entree.entry_id)
    await hass.async_block_till_done()
    return entree


def _gestionnaire(hass: HomeAssistant) -> DestinationManager:
    """Gestionnaire de destinations de l'entrée chargée."""
    return hass.data[DATA_DESTINATIONS]


### Déclaration d'un fournisseur OAuth2 ###


def test_un_fournisseur_declare_son_usage_d_oauth(
    fournisseur_oauth_factice: str, fournisseur_factice: str
) -> None:
    """Le registre expose la déclaration OAuth2 d'un fournisseur, ou `None`."""
    assert spec_oauth_du_fournisseur(fournisseur_oauth_factice) is SPEC_OAUTH_FACTICE
    assert spec_oauth_du_fournisseur(fournisseur_factice) is None


def test_les_portees_alimentent_l_url_d_autorisation() -> None:
    """Les portées déclarées deviennent le paramètre `scope`."""
    assert SPEC_OAUTH_FACTICE.donnees_d_autorisation() == {"scope": PORTEE_FACTICE}


### Masquage des secrets ###


def test_la_configuration_masque_ses_secrets_dans_sa_representation() -> None:
    """`repr()` ne laisse filtrer ni identifiants d'application ni jeton."""
    config = DestinationConfig.from_dict(config_oauth_factice())

    representation = repr(config)

    assert CLIENT_ID_FACTICE not in representation
    assert CLIENT_SECRET_FACTICE not in representation
    assert config.token is not None
    assert config.token["access_token"] not in representation
    assert config.token["refresh_token"] not in representation
    assert VALEUR_MASQUEE in representation
    # La structure reste lisible : le nom et le fournisseur aident au diagnostic.
    assert "Destination OAuth" in representation


def test_as_dict_masque_les_secrets_sur_demande_seulement() -> None:
    """Seul `as_dict(masquer=True)` assainit ; l'écriture garde les valeurs."""
    config = DestinationConfig.from_dict(config_oauth_factice())

    persiste = config.as_dict()
    masque = config.as_dict(masquer=True)

    assert persiste[CONF_CLIENT_SECRET] == CLIENT_SECRET_FACTICE
    assert persiste[CONF_TOKEN]["access_token"] == "acces-factice-1"
    assert masque[CONF_CLIENT_ID] == VALEUR_MASQUEE
    assert masque[CONF_CLIENT_SECRET] == VALEUR_MASQUEE
    assert masque[CONF_TOKEN] == dict.fromkeys(persiste[CONF_TOKEN], VALEUR_MASQUEE)


def test_une_destination_sans_oauth_est_persistee_comme_avant() -> None:
    """Les trois champs d'autorisation absents n'apparaissent pas dans l'entrée."""
    config = DestinationConfig.from_dict(config_oauth_factice())
    sans_oauth = DestinationConfig(
        destination_id=config.destination_id,
        provider=PROVIDER_FACTICE,
        name=config.name,
    )

    assert sans_oauth.utilise_oauth is False
    assert CONF_CLIENT_ID not in sans_oauth.as_dict()
    assert CONF_CLIENT_SECRET not in sans_oauth.as_dict()
    assert CONF_TOKEN not in sans_oauth.as_dict()


@pytest.mark.parametrize(
    "surcharges",
    [
        {"client_id": ""},
        {"client_secret": "   "},
        {"client_secret": 42},
        {"token": "pas-un-dictionnaire"},
        {"token": {"refresh_token": "sans-acces", "expires_at": 1}},
        {"token": {"access_token": "sans-expiration"}},
        {"token": {"access_token": "date-invalide", "expires_at": "bientôt"}},
    ],
)
def test_les_champs_d_autorisation_invalides_sont_refuses(
    surcharges: dict[str, object],
) -> None:
    """Des options éditées à la main ne peuvent pas produire une destination."""
    with pytest.raises(DestinationConfigError):
        DestinationConfig.from_dict(config_oauth_factice(**surcharges))


def test_le_message_d_erreur_d_un_secret_ne_cite_pas_sa_valeur() -> None:
    """Un secret mal saisi ne doit pas se retrouver dans une trace."""
    with pytest.raises(DestinationConfigError) as erreur:
        DestinationConfig(
            destination_id="d",
            provider=PROVIDER_OAUTH_FACTICE,
            name="n",
            client_secret="  ",
        )

    assert "client_secret" in str(erreur.value)
    assert "  " not in str(erreur.value).replace("client_secret doit", "")


### Normalisation et validité d'un jeton ###


def test_un_jeton_brut_recoit_sa_date_d_expiration() -> None:
    """`expires_in` relatif devient `expires_at` absolu."""
    avant = time.time()

    jeton = normaliser_le_jeton(reponse_de_jeton_factice())

    assert jeton["expires_at"] >= avant + jeton["expires_in"] - 1
    assert jeton_valide(jeton) is True


def test_un_jeton_sans_duree_est_refuse() -> None:
    """Un jeton sans `expires_in` ni `expires_at` est inexploitable."""
    with pytest.raises(DestinationConfigError):
        normaliser_le_jeton({"access_token": "acces-factice"})


@pytest.mark.parametrize(
    ("jeton", "attendu"),
    [
        (None, False),
        ({}, False),
        ({"access_token": "a"}, False),
        ({"access_token": "a", "expires_at": "hier"}, False),
        ({"access_token": "a", "expires_at": time.time() - 1}, False),
        ({"access_token": "a", "expires_at": time.time() + 3600}, True),
    ],
)
def test_la_validite_d_un_jeton_tient_compte_de_son_expiration(
    jeton: dict | None, attendu: bool
) -> None:
    """Un jeton expiré, illisible ou absent n'est jamais réputé valide."""
    assert jeton_valide(jeton) is attendu


### États d'autorisation ###


async def test_un_etat_est_a_usage_unique(hass: HomeAssistant) -> None:
    """Un état consommé ne peut pas resservir : rejouer le retour est vain."""
    etat = enregistrer_un_etat(hass, "flux-1", URL_DE_RETOUR_ATTENDUE)

    memorise = consommer_un_etat(hass, etat)

    assert memorise is not None
    assert memorise.flow_id == "flux-1"
    assert memorise.redirect_uri == URL_DE_RETOUR_ATTENDUE
    assert consommer_un_etat(hass, etat) is None


async def test_un_etat_inconnu_ou_expire_est_refuse(hass: HomeAssistant) -> None:
    """Un état inventé ou périmé n'ouvre aucun flux."""
    assert consommer_un_etat(hass, "etat-invente") is None

    etat = enregistrer_un_etat(hass, "flux-2", URL_DE_RETOUR_ATTENDUE)
    hass.data[DATA_OAUTH_STATES][etat] = EtatOAuth(
        flow_id="flux-2",
        redirect_uri=URL_DE_RETOUR_ATTENDUE,
        expire_a=time.monotonic() - 1,
    )

    assert consommer_un_etat(hass, etat) is None


async def test_les_etats_d_un_flux_abandonne_sont_oublies(hass: HomeAssistant) -> None:
    """Relancer le menu nettoie les autorisations laissées en attente."""
    etat = enregistrer_un_etat(hass, "flux-3", URL_DE_RETOUR_ATTENDUE)
    autre = enregistrer_un_etat(hass, "flux-4", URL_DE_RETOUR_ATTENDUE)

    oublier_les_etats_du_flux(hass, "flux-3")

    assert consommer_un_etat(hass, etat) is None
    assert consommer_un_etat(hass, autre) is not None


### URL de retour ###


async def test_l_url_de_retour_derive_de_l_url_externe(
    hass: HomeAssistant, url_externe: str
) -> None:
    """Hors requête, l'URL externe configurée sert de base."""
    assert url_de_retour(hass) == URL_DE_RETOUR_ATTENDUE


async def test_sans_url_externe_l_autorisation_est_impossible(
    hass: HomeAssistant,
) -> None:
    """Sans URL joignable, aucune URI de redirection n'est inventée."""
    with pytest.raises(NoURLAvailableError):
        url_de_retour(hass)


async def test_l_url_d_autorisation_porte_l_etat_et_les_portees(
    hass: HomeAssistant, url_externe: str, fournisseur_oauth_factice: str
) -> None:
    """L'URL envoyée au fournisseur contient tout ce qu'il attend."""
    config = DestinationConfig.from_dict(config_oauth_factice())
    implementation = implementation_de_la_destination(hass, config)

    url = URL(await implementation.async_generate_authorize_url("flux-5"))

    assert str(url.with_query(None)) == URL_AUTORISATION_FACTICE
    assert url.query["client_id"] == CLIENT_ID_FACTICE
    assert url.query["redirect_uri"] == URL_DE_RETOUR_ATTENDUE
    assert url.query["response_type"] == "code"
    assert url.query["scope"] == PORTEE_FACTICE
    assert consommer_un_etat(hass, url.query["state"]) is not None
    # Le secret de l'application ne transite jamais par l'URL d'autorisation.
    assert CLIENT_SECRET_FACTICE not in str(url)


async def test_l_implementation_exige_des_identifiants(
    hass: HomeAssistant, fournisseur_oauth_factice: str, fournisseur_factice: str
) -> None:
    """Sans identifiants, ou sans OAuth2, aucune implémentation n'est fabriquée."""
    sans_identifiants = DestinationConfig.from_dict(
        config_oauth_factice(client_id=None, client_secret=None, token=None)
    )
    with pytest.raises(DestinationConfigError):
        implementation_de_la_destination(hass, sans_identifiants)

    sans_oauth = DestinationConfig(
        destination_id="d", provider=PROVIDER_FACTICE, name="n"
    )
    with pytest.raises(DestinationConfigError):
        implementation_de_la_destination(hass, sans_oauth)


### Rafraîchissement automatique avant une opération ###


async def test_le_jeton_expire_est_rafraichi_avant_l_operation(
    hass: HomeAssistant,
    entree_oauth: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Critère : une opération sur un jeton expiré le renouvelle toute seule."""
    _expirer_le_jeton(hass, entree_oauth, "destination_oauth")
    aioclient_mock.post(URL_JETON_FACTICE, json=reponse_de_jeton_factice())

    destination: DestinationOAuthEnMemoire = _gestionnaire(hass).async_get(
        "destination_oauth"
    )
    await destination.async_check_connection()
    await hass.async_block_till_done()

    # Le fournisseur a reçu une demande de rafraîchissement, et l'opération a
    # utilisé le nouveau jeton d'accès.
    methode, url, donnees, _ = aioclient_mock.mock_calls[-1]
    assert methode == "POST"
    assert str(url) == URL_JETON_FACTICE
    assert donnees["grant_type"] == "refresh_token"
    assert donnees["refresh_token"] == "rafraichissement-factice-1"
    assert destination.jetons_utilises == ["acces-factice-2"]
    assert destination.connexions_verifiees == 1

    # Le nouveau jeton est persisté : un redémarrage le retrouve.
    persiste = _destination_persistee(entree_oauth, "destination_oauth")[CONF_TOKEN]
    assert persiste["access_token"] == "acces-factice-2"
    assert persiste["expires_at"] > time.time()


async def test_un_jeton_valide_n_est_pas_rafraichi(
    hass: HomeAssistant,
    entree_oauth: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Aucun appel inutile : un jeton encore valide est utilisé tel quel."""
    destination: DestinationOAuthEnMemoire = _gestionnaire(hass).async_get(
        "destination_oauth"
    )

    await destination.async_check_connection()

    assert aioclient_mock.mock_calls == []
    assert destination.jetons_utilises == ["acces-factice-1"]


async def test_le_jeton_persiste_prime_sur_celui_de_la_configuration(
    hass: HomeAssistant, entree_oauth: MockConfigEntry
) -> None:
    """La session relit le jeton dans l'entrée : un rafraîchissement fait foi."""
    config = DestinationConfig.from_dict(config_oauth_factice())
    session = DestinationOAuth2Session(hass, config)

    _remplacer_le_jeton(
        hass,
        entree_oauth,
        "destination_oauth",
        jeton_factice(access_token="acces-factice-plus-recent"),
    )

    assert await session.async_get_access_token() == "acces-factice-plus-recent"


### Accès révoqué : ré-authentification requise ###


async def test_un_acces_revoque_demande_une_reautorisation(
    hass: HomeAssistant,
    entree_oauth: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Critère : `invalid_grant` -> `DestinationAuthError`, problème et marquage."""
    _expirer_le_jeton(hass, entree_oauth, "destination_oauth")
    aioclient_mock.post(
        URL_JETON_FACTICE,
        status=400,
        json={"error": "invalid_grant", "error_description": "accès révoqué"},
    )

    gestionnaire = _gestionnaire(hass)
    destination = gestionnaire.async_get("destination_oauth")

    with pytest.raises(DestinationAuthError):
        await destination.async_check_connection()

    assert gestionnaire.reauthentification_requise("destination_oauth") is True
    assert gestionnaire.reauthentifications_requises == frozenset({"destination_oauth"})

    probleme = ir.async_get(hass).async_get_issue(
        DOMAIN, identifiant_du_probleme("destination_oauth")
    )
    assert probleme is not None
    assert probleme.translation_key == "reauthentification_requise"
    assert probleme.translation_placeholders == {
        "nom": "Destination OAuth",
        "fournisseur": PROVIDER_OAUTH_FACTICE,
    }


async def test_les_autres_destinations_continuent_de_fonctionner(
    hass: HomeAssistant,
    entree_oauth: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Critère : une destination révoquée n'entraîne pas les autres."""
    _expirer_le_jeton(hass, entree_oauth, "destination_oauth")
    aioclient_mock.post(URL_JETON_FACTICE, status=400, json={"error": "invalid_grant"})

    gestionnaire = _gestionnaire(hass)
    with pytest.raises(DestinationAuthError):
        await gestionnaire.async_get("destination_oauth").async_check_connection()

    seconde: DestinationOAuthEnMemoire = gestionnaire.async_get("destination_oauth_2")
    await seconde.async_check_connection()

    assert seconde.connexions_verifiees == 1
    assert gestionnaire.reauthentification_requise("destination_oauth_2") is False
    assert (
        ir.async_get(hass).async_get_issue(
            DOMAIN, identifiant_du_probleme("destination_oauth_2")
        )
        is None
    )


async def test_un_echec_temporaire_ne_demande_pas_de_reautorisation(
    hass: HomeAssistant,
    entree_oauth: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Une panne du fournisseur (500) n'est pas une révocation d'accès."""
    _expirer_le_jeton(hass, entree_oauth, "destination_oauth")
    aioclient_mock.post(URL_JETON_FACTICE, status=503)

    gestionnaire = _gestionnaire(hass)
    with pytest.raises(DestinationError) as erreur:
        await gestionnaire.async_get("destination_oauth").async_check_connection()

    assert not isinstance(erreur.value, DestinationAuthError)
    assert gestionnaire.reauthentification_requise("destination_oauth") is False


async def test_un_jeton_sans_rafraichissement_demande_une_reautorisation(
    hass: HomeAssistant,
    entree_oauth: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Sans `refresh_token`, rien ne peut être renouvelé : il faut ré-autoriser."""
    _remplacer_le_jeton(
        hass,
        entree_oauth,
        "destination_oauth",
        jeton_factice(refresh_token=None, expires_at=time.time() - 10),
    )

    gestionnaire = _gestionnaire(hass)
    with pytest.raises(DestinationAuthError):
        await gestionnaire.async_get("destination_oauth").async_check_connection()

    assert aioclient_mock.mock_calls == []
    assert gestionnaire.reauthentification_requise("destination_oauth") is True


async def test_une_destination_sans_jeton_demande_une_autorisation(
    hass: HomeAssistant, entree_oauth: MockConfigEntry
) -> None:
    """Une destination jamais autorisée lève `DestinationAuthError`."""
    config = DestinationConfig.from_dict(config_oauth_factice(token=None))
    session = DestinationOAuth2Session(hass, config)

    _supprimer_le_jeton(hass, entree_oauth, "destination_oauth")

    with pytest.raises(DestinationAuthError):
        await session.async_ensure_token_valid()


async def test_le_marquage_disparait_avec_la_destination(
    hass: HomeAssistant, entree_oauth: MockConfigEntry
) -> None:
    """Supprimer la destination efface son marquage et son problème."""
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

    async_effacer_la_reauthentification(hass, "destination_oauth")

    assert gestionnaire.reauthentification_requise("destination_oauth") is False
    assert (
        ir.async_get(hass).async_get_issue(
            DOMAIN, identifiant_du_probleme("destination_oauth")
        )
        is None
    )


async def test_le_rechargement_des_options_oublie_les_destinations_disparues(
    hass: HomeAssistant, entree_oauth: MockConfigEntry
) -> None:
    """Un marquage ne survit pas à la destination qu'il décrit."""
    gestionnaire = _gestionnaire(hass)
    gestionnaire.async_marquer_la_reauthentification("destination_oauth")

    gestionnaire.async_load([config_oauth_factice(destination_id="autre_destination")])

    assert gestionnaire.reauthentifications_requises == frozenset()


### Outils ###


def _destination_persistee(entree: MockConfigEntry, destination_id: str) -> dict:
    """Configuration brute d'une destination, telle qu'écrite dans l'entrée."""
    return next(
        brute
        for brute in entree.options[CONF_DESTINATIONS]
        if brute["destination_id"] == destination_id
    )


def _ecrire_les_destinations(
    hass: HomeAssistant, entree: MockConfigEntry, destinations: list[dict]
) -> None:
    """Réécrit la liste des destinations de l'entrée."""
    hass.config_entries.async_update_entry(
        entree, options={**entree.options, CONF_DESTINATIONS: destinations}
    )


def _remplacer_le_jeton(
    hass: HomeAssistant,
    entree: MockConfigEntry,
    destination_id: str,
    jeton: dict | None,
) -> None:
    """Remplace (ou retire) le jeton d'une destination dans l'entrée."""
    destinations = []
    for brute in entree.options[CONF_DESTINATIONS]:
        copie = dict(brute)
        if copie["destination_id"] == destination_id:
            if jeton is None:
                copie.pop(CONF_TOKEN, None)
            else:
                copie[CONF_TOKEN] = jeton
        destinations.append(copie)
    _ecrire_les_destinations(hass, entree, destinations)


def _expirer_le_jeton(
    hass: HomeAssistant, entree: MockConfigEntry, destination_id: str
) -> None:
    """Rend le jeton d'une destination périmé depuis une minute."""
    _remplacer_le_jeton(
        hass, entree, destination_id, jeton_factice(expires_at=time.time() - 60)
    )


def _supprimer_le_jeton(
    hass: HomeAssistant, entree: MockConfigEntry, destination_id: str
) -> None:
    """Retire complètement le jeton d'une destination."""
    _remplacer_le_jeton(hass, entree, destination_id, None)


### Détails de l'implémentation et de la session ###


async def test_l_url_de_retour_suit_l_en_tete_du_frontal(hass: HomeAssistant) -> None:
    """Dans une requête, l'adresse réellement utilisée par l'utilisateur prime.

    C'est la règle du cœur de Home Assistant : l'instance peut être jointe par
    plusieurs adresses, et la redirection doit revenir sur celle-là.
    """
    requete = Mock(headers={HEADER_FRONTEND_BASE: "https://maison.exemple.test"})
    jeton = current_request.set(requete)
    try:
        assert url_de_retour(hass) == (
            f"https://maison.exemple.test{OAUTH_CALLBACK_PATH}"
        )
    finally:
        current_request.reset(jeton)


async def test_les_representations_ne_divulguent_rien(
    hass: HomeAssistant, entree_oauth: MockConfigEntry
) -> None:
    """Implémentation et session sont journalisables telles quelles."""
    config = DestinationConfig.from_dict(config_oauth_factice())
    implementation = implementation_de_la_destination(hass, config)
    session = DestinationOAuth2Session(hass, config)

    assert implementation.name == "Auto Backup"
    assert CLIENT_ID_FACTICE not in repr(implementation)
    assert CLIENT_SECRET_FACTICE not in repr(implementation)
    assert URL_AUTORISATION_FACTICE in repr(implementation)

    assert session.config is config
    assert session.valid_token is True
    assert "acces-factice-1" not in repr(session)
    assert "destination_oauth" in repr(session)


def test_un_jeton_mal_forme_est_refuse() -> None:
    """Un jeton dont l'accès est vide ne passe pas le schéma."""
    with pytest.raises(DestinationConfigError):
        normaliser_le_jeton({"access_token": "", "expires_in": 60})


async def test_une_reponse_de_rafraichissement_incomplete_est_une_erreur(
    hass: HomeAssistant,
    entree_oauth: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Un fournisseur qui renvoie une durée illisible ne révoque pas l'accès."""
    _expirer_le_jeton(hass, entree_oauth, "destination_oauth")
    aioclient_mock.post(
        URL_JETON_FACTICE,
        json={"access_token": "acces-factice-3", "expires_in": "bientôt"},
    )

    gestionnaire = _gestionnaire(hass)
    with pytest.raises(DestinationError) as erreur:
        await gestionnaire.async_get("destination_oauth").async_check_connection()

    assert not isinstance(erreur.value, DestinationAuthError)
    assert gestionnaire.reauthentification_requise("destination_oauth") is False


async def test_le_jeton_d_une_destination_disparue_n_est_pas_persiste(
    hass: HomeAssistant, entree_oauth: MockConfigEntry
) -> None:
    """Une destination supprimée pendant l'appel réseau ne ressuscite pas."""
    with pytest.raises(DestinationNotFoundError):
        async_persist_token(hass, "destination_inexistante", jeton_factice())


### Enregistrement de la vue de retour ###


async def test_la_vue_de_retour_n_est_enregistree_qu_une_fois(
    hass: HomeAssistant, entree_oauth: MockConfigEntry
) -> None:
    """Deux flux d'autorisation n'ajoutent pas deux fois la même route."""
    serveur = Mock()
    hass.data.pop(DATA_OAUTH_VIEW, None)
    with patch.object(hass, "http", serveur, create=True):
        assert async_enregistrer_la_vue_de_retour(hass) is True
        assert async_enregistrer_la_vue_de_retour(hass) is True

    assert serveur.register_view.call_count == 1


async def test_sans_serveur_http_la_vue_de_retour_est_signalee(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """Sans serveur HTTP, l'absence de retour possible est dite, pas devinée."""
    hass.data.pop(DATA_OAUTH_VIEW, None)
    with patch.object(hass, "http", None, create=True):
        assert async_enregistrer_la_vue_de_retour(hass) is False

    assert "Serveur HTTP indisponible" in caplog.text


### Robustesse des lectures et écritures dans l'entrée ###


def test_un_jeton_construit_en_python_est_valide_lui_aussi() -> None:
    """La dataclass revalide le jeton, même hors du schéma voluptuous."""
    with pytest.raises(DestinationConfigError):
        DestinationConfig(
            destination_id="d",
            provider=PROVIDER_OAUTH_FACTICE,
            name="n",
            token="pas-un-dictionnaire",
        )

    with pytest.raises(DestinationConfigError):
        DestinationConfig(
            destination_id="d",
            provider=PROVIDER_OAUTH_FACTICE,
            name="n",
            token={"access_token": "acces-factice"},
        )


def test_une_configuration_sans_jeton_se_represente_sans_masque() -> None:
    """Rien à masquer n'affiche pas un masque trompeur."""
    config = DestinationConfig(
        destination_id="d", provider=PROVIDER_OAUTH_FACTICE, name="n"
    )

    assert "token=None" in repr(config)
    assert "client_secret=None" in repr(config)


async def test_le_jeton_est_introuvable_sans_entree_ni_destination(
    hass: HomeAssistant, entree_oauth: MockConfigEntry
) -> None:
    """Chercher le jeton d'une destination absente ne lève pas, ne devine pas."""
    assert jeton_persiste(hass, "destination_inexistante") is None


async def test_sans_entree_auto_backup_aucun_jeton_n_est_lu_ni_ecrit(
    hass: HomeAssistant,
) -> None:
    """Hors intégration configurée, la lecture est vide et l'écriture refusée."""
    assert async_entree_auto_backup(hass) is None
    assert jeton_persiste(hass, "destination_oauth") is None

    with pytest.raises(DestinationNotFoundError):
        async_persist_token(hass, "destination_oauth", jeton_factice())


async def test_l_etat_de_reauthentification_est_lisible_sans_gestionnaire(
    hass: HomeAssistant,
) -> None:
    """Interroger l'état avant le chargement des destinations renvoie « non »."""
    assert reauthentification_requise(hass, "destination_oauth") is False


async def test_l_etat_de_reauthentification_suit_le_gestionnaire(
    hass: HomeAssistant, entree_oauth: MockConfigEntry
) -> None:
    """L'aide de haut niveau reflète le marquage porté par le gestionnaire."""
    assert reauthentification_requise(hass, "destination_oauth") is False

    _gestionnaire(hass).async_marquer_la_reauthentification("destination_oauth")

    assert reauthentification_requise(hass, "destination_oauth") is True
