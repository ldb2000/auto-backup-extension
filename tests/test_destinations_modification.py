"""Modification d'une destination existante depuis les options (issue #51).

Ces tests parcourent l'entrée « Modifier une destination » du flux d'options :
choix de la destination, formulaire prérempli, validation (nom unique, dossier
normalisé, rétention), confirmation d'un changement de dossier, puis
enregistrement. Ils vérifient surtout ce qui **ne doit pas** changer : le jeton
et les identifiants d'application (aucune ré-autorisation), le registre des
sauvegardes déposées (#9), les entités (#16, mêmes `unique_id`) et le
signalement de ré-autorisation (#17), dont seul le nom affiché suit.

Aucun secret réel : fournisseurs, jetons et comptes sont factices
(cf. `tests/destinations_factices.py`).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from datetime import timedelta
from typing import Any

import pytest
from homeassistant.const import (
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_NAME,
    CONF_TOKEN,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.auto_backup.const import (
    CONF_AUTO_PURGE,
    CONF_BACKUP_TIMEOUT,
    CONF_DESTINATION_ID,
    CONF_DESTINATIONS,
    CONF_FOLDER,
    CONF_PROVIDER_DATA,
    CONF_RETENTION_COUNT,
    CONF_RETENTION_DAYS,
    DATA_DESTINATIONS,
    DATA_REMOTE_BACKUPS,
    DATA_REMOTE_PURGE,
    DEFAULT_BACKUP_TIMEOUT,
    DOMAIN,
)
from custom_components.auto_backup.destinations import (
    DestinationConfig,
    async_persist_provider_data,
    async_signaler_la_reauthentification,
    enregistrer_les_fournisseurs,
    identifiant_du_probleme,
    register_provider,
    unregister_provider,
)
from custom_components.auto_backup.destinations.entities import (
    SUFFIXE_DERNIER_TELEVERSEMENT,
    SUFFIXE_PROBLEME,
    SUFFIXE_SAUVEGARDES_DISTANTES,
    identifiant_unique,
)
from custom_components.auto_backup.destinations.flow import (
    CONF_CONFIRMER,
    _appliquer_la_modification,
)
from custom_components.auto_backup.destinations.providers.google_drive import (
    CLE_ID_DU_DOSSIER,
    PROVIDER_GOOGLE_DRIVE,
)
from custom_components.auto_backup.destinations.registry import (
    cles_liees_au_dossier,
)
from custom_components.auto_backup.destinations.retention import EntreeRegistre
from destinations_factices import (
    CLIENT_SECRET_FACTICE,
    PROVIDER_OAUTH_FACTICE,
    DestinationEnMemoire,
    config_factice,
    config_oauth_factice,
)

type OuvrirLesOptions = Callable[[str, str], Awaitable[dict[str, Any]]]

DESTINATION_OAUTH = "destination_oauth"
DESTINATION_TEST = "destination_test"

# Données de compte telles que la description du compte les a persistées (#10).
DONNEES_DU_COMPTE = {"account_id": "compte-factice-0000"}

# Fournisseur factice qui, comme Google Drive, mémorise une donnée propre au
# dossier distant courant.
PROVIDER_LIE_AU_DOSSIER = "factice_lie_au_dossier"
CLE_LIEE_AU_DOSSIER = "folder_id"


class DestinationLieeAuDossier(DestinationEnMemoire):
    """Destination factice dont une donnée de fournisseur dépend du dossier."""

    CLES_LIEES_AU_DOSSIER = frozenset({CLE_LIEE_AU_DOSSIER})


@pytest.fixture
def fournisseur_lie_au_dossier() -> Iterator[str]:
    """Enregistre le fournisseur factice lié au dossier, puis le retire."""
    register_provider(PROVIDER_LIE_AU_DOSSIER, DestinationLieeAuDossier)
    yield PROVIDER_LIE_AU_DOSSIER
    unregister_provider(PROVIDER_LIE_AU_DOSSIER)


async def _demarrer(
    hass: HomeAssistant,
    destinations: list[dict[str, Any]],
    **options: Any,
) -> MockConfigEntry:
    """Démarre l'intégration avec ces destinations persistées."""
    entree = MockConfigEntry(
        domain=DOMAIN,
        title="Auto Backup",
        data={},
        options={
            CONF_AUTO_PURGE: True,
            CONF_BACKUP_TIMEOUT: DEFAULT_BACKUP_TIMEOUT,
            CONF_DESTINATIONS: destinations,
            **options,
        },
    )
    entree.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entree.entry_id)
    await hass.async_block_till_done()
    return entree


@pytest.fixture
async def entree(
    hass: HomeAssistant,
    integration_backup: None,
    fournisseur_factice: str,
    fournisseur_oauth_factice: str,
) -> MockConfigEntry:
    """Entrée portant une destination OAuth2 (compte décrit) et une autre, simple."""
    return await _demarrer(
        hass,
        [
            config_oauth_factice(provider_data=dict(DONNEES_DU_COMPTE)),
            config_factice(),
        ],
    )


async def _formulaire(
    hass: HomeAssistant,
    entree: MockConfigEntry,
    ouvrir_les_options: OuvrirLesOptions,
    destination_id: str = DESTINATION_OAUTH,
) -> dict[str, Any]:
    """Ouvre « Modifier une destination » et choisit une destination."""
    resultat = await ouvrir_les_options(entree.entry_id, "modifier_destination")
    assert resultat["type"] is FlowResultType.FORM
    assert resultat["step_id"] == "modifier_destination"
    resultat = await hass.config_entries.options.async_configure(
        resultat["flow_id"], {CONF_DESTINATION_ID: destination_id}
    )
    assert resultat["type"] is FlowResultType.FORM
    assert resultat["step_id"] == "parametres_destination"
    return resultat


def _saisie(**surcharges: Any) -> dict[str, Any]:
    """Saisie complète du formulaire, valeurs d'origine de la destination OAuth."""
    return {
        CONF_NAME: "Destination OAuth",
        CONF_FOLDER: "Sauvegardes",
        CONF_RETENTION_DAYS: 7,
        CONF_RETENTION_COUNT: 3,
        CONF_AUTO_PURGE: True,
    } | surcharges


def _persistee(entree: MockConfigEntry, destination_id: str) -> dict[str, Any]:
    """Destination telle qu'écrite dans les options de l'entrée."""
    return next(
        brute
        for brute in entree.options[CONF_DESTINATIONS]
        if brute[CONF_DESTINATION_ID] == destination_id
    )


def _valeurs_suggerees(resultat: dict[str, Any]) -> dict[str, Any]:
    """Valeurs proposées par un formulaire, champ par champ."""
    return {
        str(cle): (cle.description or {}).get("suggested_value")
        for cle in resultat["data_schema"].schema
    }


### Parcours nominal ###


async def test_le_formulaire_est_prerempli_avec_la_destination(
    hass: HomeAssistant, entree: MockConfigEntry, ouvrir_les_options: OuvrirLesOptions
) -> None:
    """Critère 1 : nom, dossier, rétentions et suppression automatique proposés."""
    resultat = await _formulaire(hass, entree, ouvrir_les_options)

    assert _valeurs_suggerees(resultat) == {
        CONF_NAME: "Destination OAuth",
        CONF_FOLDER: "Sauvegardes",
        CONF_RETENTION_DAYS: 7,
        CONF_RETENTION_COUNT: 3,
        CONF_AUTO_PURGE: True,
    }
    assert resultat["description_placeholders"] == {
        "destination": "Destination OAuth",
        "fournisseur": PROVIDER_OAUTH_FACTICE,
    }


async def test_une_retention_absente_n_est_pas_proposee(
    hass: HomeAssistant,
    integration_backup: None,
    fournisseur_factice: str,
    ouvrir_les_options: OuvrirLesOptions,
) -> None:
    """Une rétention non fixée reste un champ vide, et non « None »."""
    entree = await _demarrer(
        hass, [config_factice(retention_days=None, retention_count=None)]
    )

    resultat = await _formulaire(hass, entree, ouvrir_les_options, DESTINATION_TEST)

    suggerees = _valeurs_suggerees(resultat)
    assert suggerees[CONF_RETENTION_DAYS] is None
    assert suggerees[CONF_RETENTION_COUNT] is None


async def test_la_modification_conserve_l_autorisation_et_l_identifiant(
    hass: HomeAssistant, entree: MockConfigEntry, ouvrir_les_options: OuvrirLesOptions
) -> None:
    """Critères 1 et 2 : nom et rétention changent, jeton et compte restent."""
    avant = _persistee(entree, DESTINATION_OAUTH)
    resultat = await _formulaire(hass, entree, ouvrir_les_options)

    resultat = await hass.config_entries.options.async_configure(
        resultat["flow_id"],
        _saisie(
            **{
                CONF_NAME: "Dropbox du salon",
                CONF_RETENTION_DAYS: 30,
                CONF_RETENTION_COUNT: 10,
                CONF_AUTO_PURGE: False,
            }
        ),
    )
    await hass.async_block_till_done()

    assert resultat["type"] is FlowResultType.CREATE_ENTRY
    apres = _persistee(entree, DESTINATION_OAUTH)
    assert apres[CONF_DESTINATION_ID] == DESTINATION_OAUTH
    assert apres[CONF_NAME] == "Dropbox du salon"
    assert apres[CONF_FOLDER] == "Sauvegardes"
    assert apres[CONF_RETENTION_DAYS] == 30
    assert apres[CONF_RETENTION_COUNT] == 10
    # Rien de l'autorisation n'a bougé.
    for cle in (CONF_CLIENT_ID, CONF_CLIENT_SECRET, CONF_TOKEN, CONF_PROVIDER_DATA):
        assert apres[cle] == avant[cle], cle
    assert apres[CONF_CLIENT_SECRET] == CLIENT_SECRET_FACTICE
    # L'autre destination n'est pas touchée, l'option commune est enregistrée.
    assert _persistee(entree, DESTINATION_TEST) == config_factice()
    assert entree.options[CONF_AUTO_PURGE] is False
    assert entree.options[CONF_BACKUP_TIMEOUT] == DEFAULT_BACKUP_TIMEOUT


async def test_le_televersement_suivant_reutilise_le_jeton(
    hass: HomeAssistant, entree: MockConfigEntry, ouvrir_les_options: OuvrirLesOptions
) -> None:
    """Critère 2 : la destination rechargée opère avec le jeton d'origine."""
    jeton = _persistee(entree, DESTINATION_OAUTH)[CONF_TOKEN]["access_token"]
    resultat = await _formulaire(hass, entree, ouvrir_les_options)
    await hass.config_entries.options.async_configure(
        resultat["flow_id"], _saisie(**{CONF_NAME: "Renommée"})
    )
    await hass.async_block_till_done()

    destination = hass.data[DATA_DESTINATIONS].async_get(DESTINATION_OAUTH)
    assert destination.name == "Renommée"
    await destination.async_check_connection()
    assert destination.jetons_utilises == [jeton]


async def test_vider_une_retention_la_supprime(
    hass: HomeAssistant, entree: MockConfigEntry, ouvrir_les_options: OuvrirLesOptions
) -> None:
    """Un champ de rétention laissé vide retire la limite."""
    resultat = await _formulaire(hass, entree, ouvrir_les_options)
    saisie = _saisie()
    del saisie[CONF_RETENTION_DAYS]
    del saisie[CONF_RETENTION_COUNT]

    resultat = await hass.config_entries.options.async_configure(
        resultat["flow_id"], saisie
    )

    assert resultat["type"] is FlowResultType.CREATE_ENTRY
    apres = _persistee(entree, DESTINATION_OAUTH)
    assert apres[CONF_RETENTION_DAYS] is None
    assert apres[CONF_RETENTION_COUNT] is None


async def test_un_jeton_rafraichi_pendant_la_saisie_n_est_pas_ecrase(
    hass: HomeAssistant, entree: MockConfigEntry, ouvrir_les_options: OuvrirLesOptions
) -> None:
    """La modification s'applique à la configuration relue, pas à une copie périmée."""
    resultat = await _formulaire(hass, entree, ouvrir_les_options)

    # Un téléversement rafraîchit le jeton pendant que le formulaire est ouvert.
    destinations = [dict(brute) for brute in entree.options[CONF_DESTINATIONS]]
    frais = dict(destinations[0][CONF_TOKEN], access_token="acces-rafraichi")
    destinations[0][CONF_TOKEN] = frais
    hass.config_entries.async_update_entry(
        entree, options={**entree.options, CONF_DESTINATIONS: destinations}
    )
    await hass.async_block_till_done()

    await hass.config_entries.options.async_configure(
        resultat["flow_id"], _saisie(**{CONF_NAME: "Renommée"})
    )

    apres = _persistee(entree, DESTINATION_OAUTH)
    assert apres[CONF_NAME] == "Renommée"
    assert apres[CONF_TOKEN]["access_token"] == "acces-rafraichi"


### Validation ###


async def test_le_nom_d_une_autre_destination_est_refuse(
    hass: HomeAssistant, entree: MockConfigEntry, ouvrir_les_options: OuvrirLesOptions
) -> None:
    """L'unicité du nom, imposée à l'ajout, vaut aussi pour la modification."""
    resultat = await _formulaire(hass, entree, ouvrir_les_options)

    resultat = await hass.config_entries.options.async_configure(
        resultat["flow_id"], _saisie(**{CONF_NAME: "destination DE TEST"})
    )

    assert resultat["type"] is FlowResultType.FORM
    assert resultat["errors"] == {CONF_NAME: "nom_deja_utilise"}
    assert _persistee(entree, DESTINATION_OAUTH)[CONF_NAME] == "Destination OAuth"


async def test_garder_son_propre_nom_n_est_pas_un_doublon(
    hass: HomeAssistant, entree: MockConfigEntry, ouvrir_les_options: OuvrirLesOptions
) -> None:
    """La destination modifiée ne se compte pas elle-même, casse comprise."""
    resultat = await _formulaire(hass, entree, ouvrir_les_options)

    resultat = await hass.config_entries.options.async_configure(
        resultat["flow_id"], _saisie(**{CONF_NAME: "  DESTINATION oauth  "})
    )

    assert resultat["type"] is FlowResultType.CREATE_ENTRY
    assert _persistee(entree, DESTINATION_OAUTH)[CONF_NAME] == "DESTINATION oauth"


async def test_un_nom_vide_est_refuse(
    hass: HomeAssistant, entree: MockConfigEntry, ouvrir_les_options: OuvrirLesOptions
) -> None:
    """Un nom réduit à des espaces est refusé, la saisie est conservée."""
    resultat = await _formulaire(hass, entree, ouvrir_les_options)

    resultat = await hass.config_entries.options.async_configure(
        resultat["flow_id"], _saisie(**{CONF_NAME: "   ", CONF_RETENTION_DAYS: 12})
    )

    assert resultat["errors"] == {CONF_NAME: "nom_invalide"}
    assert _valeurs_suggerees(resultat)[CONF_RETENTION_DAYS] == 12


@pytest.mark.parametrize(
    "dossier",
    [
        "../ailleurs",
        "/absolu",
        "a\\b",
        "a//b",
        "Sauvegardes/*",
        # Deux points pleine chasse : « .. » une fois normalisés en NFKC.
        "\uff0e\uff0e/x",
    ],
)
async def test_un_dossier_invalide_est_refuse(
    hass: HomeAssistant,
    entree: MockConfigEntry,
    ouvrir_les_options: OuvrirLesOptions,
    dossier: str,
) -> None:
    """La validation du dossier est celle de l'ajout (#6)."""
    resultat = await _formulaire(hass, entree, ouvrir_les_options)

    resultat = await hass.config_entries.options.async_configure(
        resultat["flow_id"], _saisie(**{CONF_FOLDER: dossier})
    )

    assert resultat["type"] is FlowResultType.FORM
    assert resultat["errors"] == {CONF_FOLDER: "dossier_invalide"}
    assert _persistee(entree, DESTINATION_OAUTH)[CONF_FOLDER] == "Sauvegardes"


async def test_un_dossier_normalise_identique_n_est_pas_un_changement(
    hass: HomeAssistant,
    integration_backup: None,
    fournisseur_factice: str,
    ouvrir_les_options: OuvrirLesOptions,
) -> None:
    """Le dossier est comparé une fois normalisé en NFKC : pas de confirmation."""
    entree = await _demarrer(hass, [config_factice(folder="Sauvegardes/HA")])
    resultat = await _formulaire(hass, entree, ouvrir_les_options, DESTINATION_TEST)

    resultat = await hass.config_entries.options.async_configure(
        resultat["flow_id"],
        {
            CONF_NAME: "Destination de test",
            # H et A pleine chasse (U+FF28, U+FF21) : « HA » une fois normalisés.
            CONF_FOLDER: "Sauvegardes/\uff28\uff21",
            CONF_AUTO_PURGE: True,
        },
    )

    assert resultat["type"] is FlowResultType.CREATE_ENTRY
    assert _persistee(entree, DESTINATION_TEST)[CONF_FOLDER] == "Sauvegardes/HA"


### Changement de dossier distant ###


async def test_un_changement_de_dossier_demande_confirmation(
    hass: HomeAssistant, entree: MockConfigEntry, ouvrir_les_options: OuvrirLesOptions
) -> None:
    """Critère 5 : l'interface prévient avant d'abandonner l'ancien dossier."""
    resultat = await _formulaire(hass, entree, ouvrir_les_options)

    resultat = await hass.config_entries.options.async_configure(
        resultat["flow_id"], _saisie(**{CONF_FOLDER: "Nouveau/Dossier"})
    )

    assert resultat["type"] is FlowResultType.FORM
    assert resultat["step_id"] == "confirmer_changement_de_dossier"
    assert resultat["description_placeholders"] == {
        "destination": "Destination OAuth",
        "ancien_dossier": "Sauvegardes",
        "nouveau_dossier": "Nouveau/Dossier",
    }
    # Rien n'est écrit tant que l'utilisateur n'a pas confirmé.
    assert _persistee(entree, DESTINATION_OAUTH)[CONF_FOLDER] == "Sauvegardes"

    resultat = await hass.config_entries.options.async_configure(
        resultat["flow_id"], {CONF_CONFIRMER: True}
    )

    assert resultat["type"] is FlowResultType.CREATE_ENTRY
    apres = _persistee(entree, DESTINATION_OAUTH)
    assert apres[CONF_FOLDER] == "Nouveau/Dossier"
    # Le fournisseur factice ne déclare aucune donnée liée au dossier.
    assert apres[CONF_PROVIDER_DATA] == DONNEES_DU_COMPTE


async def test_un_changement_de_dossier_non_confirme_n_ecrit_rien(
    hass: HomeAssistant, entree: MockConfigEntry, ouvrir_les_options: OuvrirLesOptions
) -> None:
    """Sans confirmation, ni le dossier ni le reste de la saisie n'est enregistré."""
    options_avant = dict(entree.options)
    resultat = await _formulaire(hass, entree, ouvrir_les_options)
    resultat = await hass.config_entries.options.async_configure(
        resultat["flow_id"],
        _saisie(**{CONF_FOLDER: "Ailleurs", CONF_NAME: "Autre nom"}),
    )

    resultat = await hass.config_entries.options.async_configure(
        resultat["flow_id"], {CONF_CONFIRMER: False}
    )

    assert resultat["type"] is FlowResultType.ABORT
    assert resultat["reason"] == "changement_de_dossier_annule"
    assert dict(entree.options) == options_avant


async def test_le_changement_de_dossier_oublie_les_donnees_liees_au_dossier(
    hass: HomeAssistant,
    integration_backup: None,
    fournisseur_lie_au_dossier: str,
    ouvrir_les_options: OuvrirLesOptions,
) -> None:
    """L'identifiant de l'ancien dossier est oublié, le compte est conservé."""
    entree = await _demarrer(
        hass,
        [
            config_factice(
                provider=PROVIDER_LIE_AU_DOSSIER,
                provider_data={"account_email": "a@exemple.test", "folder_id": "ID1"},
            )
        ],
    )
    resultat = await _formulaire(hass, entree, ouvrir_les_options, DESTINATION_TEST)
    resultat = await hass.config_entries.options.async_configure(
        resultat["flow_id"],
        {
            CONF_NAME: "Destination de test",
            CONF_FOLDER: "Ailleurs",
            CONF_AUTO_PURGE: True,
        },
    )
    await hass.config_entries.options.async_configure(
        resultat["flow_id"], {CONF_CONFIRMER: True}
    )

    apres = _persistee(entree, DESTINATION_TEST)
    assert apres[CONF_FOLDER] == "Ailleurs"
    assert apres[CONF_PROVIDER_DATA] == {"account_email": "a@exemple.test"}


async def test_sans_changement_de_dossier_les_donnees_liees_restent(
    hass: HomeAssistant,
    integration_backup: None,
    fournisseur_lie_au_dossier: str,
    ouvrir_les_options: OuvrirLesOptions,
) -> None:
    """Renommer seulement ne fait pas rechercher le dossier chez le fournisseur."""
    donnees = {"folder_id": "ID1"}
    entree = await _demarrer(
        hass,
        [config_factice(provider=PROVIDER_LIE_AU_DOSSIER, provider_data=donnees)],
    )
    resultat = await _formulaire(hass, entree, ouvrir_les_options, DESTINATION_TEST)
    await hass.config_entries.options.async_configure(
        resultat["flow_id"],
        {CONF_NAME: "Renommée", CONF_FOLDER: "Sauvegardes", CONF_AUTO_PURGE: True},
    )

    assert _persistee(entree, DESTINATION_TEST)[CONF_PROVIDER_DATA] == donnees


def test_appliquer_la_modification_retire_toutes_les_donnees_liees() -> None:
    """Une destination dont il ne reste aucune donnée n'en persiste pas de vide."""
    enregistrer_les_fournisseurs()
    config = DestinationConfig(
        destination_id="gd",
        provider=PROVIDER_GOOGLE_DRIVE,
        name="Drive",
        folder="Ancien",
        provider_data={CLE_ID_DU_DOSSIER: "ID1"},
    )

    modifiee = _appliquer_la_modification(
        config,
        {
            CONF_NAME: "Drive",
            CONF_FOLDER: "Nouveau",
            CONF_RETENTION_DAYS: None,
            CONF_RETENTION_COUNT: 5,
        },
    )

    assert modifiee.folder == "Nouveau"
    assert modifiee.provider_data is None
    assert modifiee.retention_count == 5


def test_google_drive_declare_son_identifiant_de_dossier() -> None:
    """Google Drive mémorise `folder_id` : il est lié au dossier configuré."""
    enregistrer_les_fournisseurs()

    assert cles_liees_au_dossier(PROVIDER_GOOGLE_DRIVE) == {CLE_ID_DU_DOSSIER}
    assert cles_liees_au_dossier("dropbox") == frozenset()
    assert cles_liees_au_dossier("fournisseur_inexistant") == frozenset()


async def test_un_ancien_identifiant_de_dossier_n_est_pas_reecrit(
    hass: HomeAssistant,
    integration_backup: None,
    fournisseur_lie_au_dossier: str,
) -> None:
    """Un envoi commencé avant le changement de dossier ne le défait pas."""
    entree = await _demarrer(
        hass,
        [config_factice(provider=PROVIDER_LIE_AU_DOSSIER, folder="Nouveau")],
    )

    # L'envoi en cours avait résolu l'identifiant de l'**ancien** dossier.
    async_persist_provider_data(
        hass, DESTINATION_TEST, {CLE_LIEE_AU_DOSSIER: "ID1"}, dossier="Ancien"
    )
    assert CONF_PROVIDER_DATA not in _persistee(entree, DESTINATION_TEST)

    async_persist_provider_data(
        hass, DESTINATION_TEST, {CLE_LIEE_AU_DOSSIER: "ID2"}, dossier="Nouveau"
    )
    assert _persistee(entree, DESTINATION_TEST)[CONF_PROVIDER_DATA] == {
        CLE_LIEE_AU_DOSSIER: "ID2"
    }


### Destination disparue ###


async def test_une_destination_supprimee_pendant_la_saisie_n_est_pas_recreee(
    hass: HomeAssistant, entree: MockConfigEntry, ouvrir_les_options: OuvrirLesOptions
) -> None:
    """Supprimée ailleurs pendant la saisie, la destination n'est pas ressuscitée."""
    resultat = await _formulaire(hass, entree, ouvrir_les_options)
    hass.config_entries.async_update_entry(
        entree, options={**entree.options, CONF_DESTINATIONS: [config_factice()]}
    )
    await hass.async_block_till_done()

    resultat = await hass.config_entries.options.async_configure(
        resultat["flow_id"], _saisie(**{CONF_NAME: "Fantôme"})
    )

    assert resultat["type"] is FlowResultType.ABORT
    assert resultat["reason"] == "destination_inconnue"
    assert entree.options[CONF_DESTINATIONS] == [config_factice()]


async def test_une_destination_supprimee_avant_la_confirmation_n_est_pas_recreee(
    hass: HomeAssistant, entree: MockConfigEntry, ouvrir_les_options: OuvrirLesOptions
) -> None:
    """Même garantie à l'étape de confirmation du changement de dossier."""
    resultat = await _formulaire(hass, entree, ouvrir_les_options)
    resultat = await hass.config_entries.options.async_configure(
        resultat["flow_id"], _saisie(**{CONF_FOLDER: "Ailleurs"})
    )
    hass.config_entries.async_update_entry(
        entree, options={**entree.options, CONF_DESTINATIONS: [config_factice()]}
    )
    await hass.async_block_till_done()

    resultat = await hass.config_entries.options.async_configure(
        resultat["flow_id"], {CONF_CONFIRMER: True}
    )

    assert resultat["type"] is FlowResultType.ABORT
    assert resultat["reason"] == "destination_inconnue"


async def test_une_destination_inconnue_au_choix_interrompt_le_flux(
    hass: HomeAssistant, entree: MockConfigEntry, ouvrir_les_options: OuvrirLesOptions
) -> None:
    """Une destination retirée entre l'affichage et le choix est signalée."""
    resultat = await ouvrir_les_options(entree.entry_id, "modifier_destination")
    hass.config_entries.async_update_entry(
        entree, options={**entree.options, CONF_DESTINATIONS: [config_factice()]}
    )
    await hass.async_block_till_done()

    resultat = await hass.config_entries.options.async_configure(
        resultat["flow_id"], {CONF_DESTINATION_ID: DESTINATION_OAUTH}
    )

    assert resultat["type"] is FlowResultType.ABORT
    assert resultat["reason"] == "destination_inconnue"


### Registre des sauvegardes déposées et purge (#9) ###


async def test_le_registre_est_conserve_et_la_nouvelle_retention_s_applique(
    hass: HomeAssistant, entree: MockConfigEntry, ouvrir_les_options: OuvrirLesOptions
) -> None:
    """Critère 3 : les sauvegardes inscrites avant la modification restent connues."""
    registre = hass.data[DATA_REMOTE_BACKUPS]
    for index in range(3):
        await registre.async_enregistrer(
            DESTINATION_TEST,
            EntreeRegistre(remote_id=f"s{index}", name=f"s{index}"),
        )

    resultat = await _formulaire(hass, entree, ouvrir_les_options, DESTINATION_TEST)
    await hass.config_entries.options.async_configure(
        resultat["flow_id"],
        {
            CONF_NAME: "Renommée",
            CONF_FOLDER: "Sauvegardes",
            CONF_RETENTION_COUNT: 1,
            CONF_AUTO_PURGE: True,
        },
    )
    await hass.async_block_till_done()

    assert [e.remote_id for e in registre.entrees(DESTINATION_TEST)] == [
        "s0",
        "s1",
        "s2",
    ]
    # Les fichiers sont présents chez le fournisseur, sans marqueur : seul le
    # registre conservé permet à la purge de les reconnaître.
    destination = hass.data[DATA_DESTINATIONS].async_get(DESTINATION_TEST)
    maintenant = dt_util.utcnow()
    for heures, remote_id in ((3, "s0"), (2, "s1"), (1, "s2")):
        destination.ajouter_sauvegarde(
            remote_id, created_at=maintenant - timedelta(hours=heures)
        )

    supprimes = await hass.data[DATA_REMOTE_PURGE].async_purger_destination(
        DESTINATION_TEST
    )

    assert sorted(supprimes) == ["s0", "s1"]
    assert [e.remote_id for e in registre.entrees(DESTINATION_TEST)] == ["s2"]


### Entités (#16) ###


async def test_les_entites_gardent_leur_identifiant_et_suivent_le_nom(
    hass: HomeAssistant, entree: MockConfigEntry, ouvrir_les_options: OuvrirLesOptions
) -> None:
    """Critère 4 : mêmes `unique_id` et `entity_id`, nom affiché mis à jour à chaud."""
    registre = er.async_get(hass)
    entites = {
        (domaine, suffixe): registre.async_get_entity_id(
            domaine,
            DOMAIN,
            identifiant_unique(entree.entry_id, DESTINATION_TEST, suffixe),
        )
        for domaine, suffixe in (
            (Platform.SENSOR, SUFFIXE_DERNIER_TELEVERSEMENT),
            (Platform.SENSOR, SUFFIXE_SAUVEGARDES_DISTANTES),
            (Platform.BINARY_SENSOR, SUFFIXE_PROBLEME),
        )
    }
    assert all(entites.values())
    for entity_id in entites.values():
        assert (
            "Destination de test"
            in hass.states.get(entity_id).attributes["friendly_name"]
        )

    resultat = await _formulaire(hass, entree, ouvrir_les_options, DESTINATION_TEST)
    await hass.config_entries.options.async_configure(
        resultat["flow_id"],
        {
            CONF_NAME: "Coffre du grenier",
            CONF_FOLDER: "Sauvegardes",
            CONF_AUTO_PURGE: True,
        },
    )
    await hass.async_block_till_done()

    for (domaine, suffixe), entity_id in entites.items():
        # Même entité : l'identifiant unique retrouve le même entity_id.
        assert (
            registre.async_get_entity_id(
                domaine,
                DOMAIN,
                identifiant_unique(entree.entry_id, DESTINATION_TEST, suffixe),
            )
            == entity_id
        )
        nom = hass.states.get(entity_id).attributes["friendly_name"]
        assert "Coffre du grenier" in nom, nom
        assert "Destination de test" not in nom, nom
    # L'autre destination garde son nom.
    autre = registre.async_get_entity_id(
        Platform.SENSOR,
        DOMAIN,
        identifiant_unique(
            entree.entry_id, DESTINATION_OAUTH, SUFFIXE_DERNIER_TELEVERSEMENT
        ),
    )
    assert "Destination OAuth" in hass.states.get(autre).attributes["friendly_name"]


### Signalement de ré-autorisation (#17) ###


async def test_le_probleme_de_reautorisation_suit_le_nouveau_nom(
    hass: HomeAssistant, entree: MockConfigEntry, ouvrir_les_options: OuvrirLesOptions
) -> None:
    """Le problème Home Assistant nomme la destination par son nouveau nom."""
    config = DestinationConfig.from_dict(_persistee(entree, DESTINATION_OAUTH))
    async_signaler_la_reauthentification(hass, config)
    await hass.async_block_till_done()

    resultat = await _formulaire(hass, entree, ouvrir_les_options)
    await hass.config_entries.options.async_configure(
        resultat["flow_id"], _saisie(**{CONF_NAME: "Nom tout neuf"})
    )
    await hass.async_block_till_done()

    probleme = ir.async_get(hass).async_get_issue(
        DOMAIN, identifiant_du_probleme(DESTINATION_OAUTH)
    )
    assert probleme is not None
    assert probleme.translation_placeholders["nom"] == "Nom tout neuf"
    # La destination reste à ré-autoriser : renommer ne répare rien.
    assert hass.data[DATA_DESTINATIONS].reauthentification_requise(DESTINATION_OAUTH)


async def test_renommer_une_destination_saine_ne_cree_aucun_probleme(
    hass: HomeAssistant, entree: MockConfigEntry, ouvrir_les_options: OuvrirLesOptions
) -> None:
    """Sans signalement en cours, renommer n'en crée pas."""
    resultat = await _formulaire(hass, entree, ouvrir_les_options)
    await hass.config_entries.options.async_configure(
        resultat["flow_id"], _saisie(**{CONF_NAME: "Nom tout neuf"})
    )
    await hass.async_block_till_done()

    assert (
        ir.async_get(hass).async_get_issue(
            DOMAIN, identifiant_du_probleme(DESTINATION_OAUTH)
        )
        is None
    )
