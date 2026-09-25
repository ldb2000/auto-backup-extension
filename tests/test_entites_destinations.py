"""Entités d'état des destinations distantes (issue #16).

Ces tests couvrent le cycle de vie complet des entités créées par
`destinations/entities.py` : leur création pour chaque destination configurée,
leur mise à jour sur succès et sur échec de téléversement, la restauration de
l'état après un redémarrage, l'ajout et la suppression d'une destination à
chaud, et le masquage des secrets dans le message d'erreur exposé.

Aucun accès réseau : les événements `auto_backup.upload_*` et
`auto_backup.remote_purge` sont émis directement sur le bus, exactement comme
le feraient le coordinateur de téléversement (#8) et la rétention distante (#9).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

import pytest
from homeassistant.const import (
    ATTR_NAME,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.auto_backup.const import (
    ATTR_DELETED,
    ATTR_DESTINATION,
    ATTR_DESTINATION_NAME,
    ATTR_ERROR,
    ATTR_LAST_ERROR,
    ATTR_LAST_FAILED_AT,
    ATTR_LAST_FAILED_SLUG,
    ATTR_REMAINING,
    ATTR_REMOTE_ID,
    ATTR_SIZE,
    ATTR_SLUG,
    CONF_DESTINATIONS,
    DATA_DESTINATION_ENTITIES,
    DOMAIN,
    EVENT_REMOTE_PURGE,
    EVENT_UPLOAD_FAILED,
    EVENT_UPLOAD_SUCCESSFUL,
)
from custom_components.auto_backup.destinations import (
    DestinationConfig,
    async_persist_destinations,
)
from custom_components.auto_backup.destinations.entities import (
    ERREUR_INCONNUE,
    LONGUEUR_MAX_ERREUR,
    SUFFIXE_DERNIER_TELEVERSEMENT,
    SUFFIXE_PROBLEME,
    SUFFIXE_SAUVEGARDES_DISTANTES,
    assainir_le_message,
    async_enregistrer_source_des_comptes,
    identifiant_unique,
)
from destinations_factices import config_factice

# Deuxième destination, pour éprouver l'indépendance des entités.
DESTINATION_DEUX = "destination_deux"
NOM_DESTINATION_DEUX = "Destination secondaire"

# Jeton inventé : il ne doit jamais ressortir dans un attribut d'entité.
FAUX_JETON = "sl.FAUX-JETON-a1b2c3d4e5f6"


def config_deux(**surcharges: Any) -> dict[str, Any]:
    """Configuration brute de la seconde destination factice."""
    return config_factice(
        destination_id=DESTINATION_DEUX, name=NOM_DESTINATION_DEUX, **surcharges
    )


@pytest.fixture
async def entree_avec_destination(
    hass: HomeAssistant, integration_backup: None, fournisseur_factice: str
) -> AsyncIterator[MockConfigEntry]:
    """Entrée `auto_backup` initialisée avec une destination factice."""
    entree = MockConfigEntry(
        domain=DOMAIN,
        title="Auto Backup",
        data={},
        options={CONF_DESTINATIONS: [config_factice()]},
    )
    entree.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entree.entry_id)
    await hass.async_block_till_done()

    yield entree


@pytest.fixture
async def entree_avec_deux_destinations(
    hass: HomeAssistant, integration_backup: None, fournisseur_factice: str
) -> AsyncIterator[MockConfigEntry]:
    """Entrée `auto_backup` initialisée avec deux destinations factices."""
    entree = MockConfigEntry(
        domain=DOMAIN,
        title="Auto Backup",
        data={},
        options={CONF_DESTINATIONS: [config_factice(), config_deux()]},
    )
    entree.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entree.entry_id)
    await hass.async_block_till_done()

    yield entree


def _entity_id(
    hass: HomeAssistant,
    entree: MockConfigEntry,
    domaine: str,
    suffixe: str,
    destination_id: str = "destination_test",
) -> str | None:
    """Entity_id d'une entité de destination, par son identifiant unique."""
    return er.async_get(hass).async_get_entity_id(
        domaine, DOMAIN, identifiant_unique(entree.entry_id, destination_id, suffixe)
    )


def _etat(
    hass: HomeAssistant,
    entree: MockConfigEntry,
    domaine: str,
    suffixe: str,
    destination_id: str = "destination_test",
):
    """État Home Assistant d'une entité de destination."""
    entity_id = _entity_id(hass, entree, domaine, suffixe, destination_id)
    assert entity_id is not None, f"{suffixe} absente du registre"
    etat = hass.states.get(entity_id)
    assert etat is not None, f"{entity_id} sans état"
    return etat


def _emettre_succes(
    hass: HomeAssistant,
    destination_id: str = "destination_test",
    *,
    slug: str = "abc12345",
    nom: str = "Sauvegarde du soir",
) -> None:
    """Émet `auto_backup.upload_successful` comme le fait le coordinateur #8."""
    hass.bus.async_fire(
        EVENT_UPLOAD_SUCCESSFUL,
        {
            ATTR_NAME: nom,
            ATTR_SLUG: slug,
            ATTR_DESTINATION: destination_id,
            ATTR_DESTINATION_NAME: "Destination de test",
            ATTR_SIZE: 1234,
            ATTR_REMOTE_ID: "distant-1",
        },
    )


def _emettre_echec(
    hass: HomeAssistant,
    destination_id: str = "destination_test",
    *,
    slug: str = "abc12345",
    erreur: str = "quota dépassé",
) -> None:
    """Émet `auto_backup.upload_failed` comme le fait le coordinateur #8."""
    hass.bus.async_fire(
        EVENT_UPLOAD_FAILED,
        {
            ATTR_NAME: "Sauvegarde du soir",
            ATTR_SLUG: slug,
            ATTR_DESTINATION: destination_id,
            ATTR_DESTINATION_NAME: "Destination de test",
            ATTR_ERROR: erreur,
        },
    )


### CRÉATION DES ENTITÉS ###


async def test_une_destination_expose_ses_trois_entites(
    hass: HomeAssistant, entree_avec_destination: MockConfigEntry
) -> None:
    """Une destination configurée expose deux capteurs et un capteur binaire."""
    for domaine, suffixe in (
        (Platform.SENSOR, SUFFIXE_DERNIER_TELEVERSEMENT),
        (Platform.SENSOR, SUFFIXE_SAUVEGARDES_DISTANTES),
        (Platform.BINARY_SENSOR, SUFFIXE_PROBLEME),
    ):
        assert _entity_id(hass, entree_avec_destination, domaine, suffixe) is not None

    # États initiaux : aucun téléversement, aucune sauvegarde, aucun problème.
    horodatage = _etat(
        hass, entree_avec_destination, Platform.SENSOR, SUFFIXE_DERNIER_TELEVERSEMENT
    )
    assert horodatage.state == STATE_UNKNOWN
    assert horodatage.attributes["device_class"] == "timestamp"

    nombre = _etat(
        hass, entree_avec_destination, Platform.SENSOR, SUFFIXE_SAUVEGARDES_DISTANTES
    )
    assert nombre.state == "0"

    probleme = _etat(
        hass, entree_avec_destination, Platform.BINARY_SENSOR, SUFFIXE_PROBLEME
    )
    assert probleme.state == STATE_OFF
    assert probleme.attributes["device_class"] == "problem"
    assert probleme.attributes[ATTR_LAST_ERROR] is None


async def test_aucune_entite_sans_destination_configuree(
    hass: HomeAssistant, entree_auto_backup: MockConfigEntry
) -> None:
    """Sans destination, l'intégration n'ajoute aucune entité au registre."""
    registre = er.async_get(hass)
    entites = er.async_entries_for_config_entry(registre, entree_auto_backup.entry_id)

    assert not [
        entite
        for entite in entites
        if entite.unique_id.startswith(entree_auto_backup.entry_id)
    ]


async def test_les_entites_portent_le_nom_de_la_destination(
    hass: HomeAssistant, entree_avec_deux_destinations: MockConfigEntry
) -> None:
    """Chaque entité est nommée avec le nom de sa destination (critère 4)."""
    premier = _etat(
        hass,
        entree_avec_deux_destinations,
        Platform.SENSOR,
        SUFFIXE_DERNIER_TELEVERSEMENT,
    )
    second = _etat(
        hass,
        entree_avec_deux_destinations,
        Platform.SENSOR,
        SUFFIXE_DERNIER_TELEVERSEMENT,
        DESTINATION_DEUX,
    )

    assert "Destination de test" in premier.attributes["friendly_name"]
    assert NOM_DESTINATION_DEUX in second.attributes["friendly_name"]
    assert premier.entity_id != second.entity_id


async def test_les_entites_sont_rattachees_au_device_de_service(
    hass: HomeAssistant, entree_avec_destination: MockConfigEntry
) -> None:
    """Les entités rejoignent le device « Auto Backup » upstream (critère 4)."""
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, entree_avec_destination.entry_id), entree_avec_destination.entry_id
    )
    assert device is not None

    registre = er.async_get(hass)
    for domaine, suffixe in (
        (Platform.SENSOR, SUFFIXE_DERNIER_TELEVERSEMENT),
        (Platform.SENSOR, SUFFIXE_SAUVEGARDES_DISTANTES),
        (Platform.BINARY_SENSOR, SUFFIXE_PROBLEME),
    ):
        entity_id = _entity_id(hass, entree_avec_destination, domaine, suffixe)
        assert entity_id is not None
        assert registre.async_get(entity_id).device_id == device.id


### MISE À JOUR SUR SUCCÈS ET SUR ÉCHEC ###


async def test_un_televersement_reussi_met_a_jour_l_horodatage(
    hass: HomeAssistant, entree_avec_destination: MockConfigEntry
) -> None:
    """`upload_successful` horodate le succès et incrémente le compteur."""
    avant = dt_util.utcnow()
    _emettre_succes(hass)
    await hass.async_block_till_done()

    horodatage = _etat(
        hass, entree_avec_destination, Platform.SENSOR, SUFFIXE_DERNIER_TELEVERSEMENT
    )
    # L'état d'un capteur d'horodatage est tronqué à la seconde par Home Assistant.
    date = dt_util.parse_datetime(horodatage.state)
    assert date is not None and date >= avant.replace(microsecond=0)

    nombre = _etat(
        hass, entree_avec_destination, Platform.SENSOR, SUFFIXE_SAUVEGARDES_DISTANTES
    )
    assert nombre.state == "1"


async def test_un_echec_leve_le_probleme_et_expose_l_erreur(
    hass: HomeAssistant, entree_avec_destination: MockConfigEntry
) -> None:
    """`upload_failed` passe le capteur binaire à « problème » (critère 3)."""
    avant = dt_util.utcnow()
    _emettre_echec(hass, slug="slug-echoue", erreur="quota dépassé")
    await hass.async_block_till_done()

    probleme = _etat(
        hass, entree_avec_destination, Platform.BINARY_SENSOR, SUFFIXE_PROBLEME
    )
    assert probleme.state == STATE_ON
    assert probleme.attributes[ATTR_LAST_ERROR] == "quota dépassé"
    assert probleme.attributes[ATTR_LAST_FAILED_SLUG] == "slug-echoue"

    horodatage = dt_util.parse_datetime(probleme.attributes[ATTR_LAST_FAILED_AT])
    assert horodatage is not None and horodatage >= avant


async def test_un_succes_efface_l_erreur_et_retombe_le_probleme(
    hass: HomeAssistant, entree_avec_destination: MockConfigEntry
) -> None:
    """Après un échec, un succès éteint le problème et efface `last_error`."""
    _emettre_echec(hass, slug="slug-echoue")
    await hass.async_block_till_done()
    assert (
        _etat(
            hass, entree_avec_destination, Platform.BINARY_SENSOR, SUFFIXE_PROBLEME
        ).state
        == STATE_ON
    )

    _emettre_succes(hass)
    await hass.async_block_till_done()

    probleme = _etat(
        hass, entree_avec_destination, Platform.BINARY_SENSOR, SUFFIXE_PROBLEME
    )
    assert probleme.state == STATE_OFF
    assert probleme.attributes[ATTR_LAST_ERROR] is None
    # La trace du dernier échec connu survit au succès : elle documente ce qui
    # avait échoué, elle ne signale plus un problème en cours.
    assert probleme.attributes[ATTR_LAST_FAILED_SLUG] == "slug-echoue"
    assert probleme.attributes[ATTR_LAST_FAILED_AT] is not None


async def test_un_echec_sans_message_reste_lisible(
    hass: HomeAssistant, entree_avec_destination: MockConfigEntry
) -> None:
    """Un échec sans cause exploitable expose un message de repli."""
    hass.bus.async_fire(
        EVENT_UPLOAD_FAILED,
        {ATTR_DESTINATION: "destination_test", ATTR_SLUG: None, ATTR_ERROR: None},
    )
    await hass.async_block_till_done()

    probleme = _etat(
        hass, entree_avec_destination, Platform.BINARY_SENSOR, SUFFIXE_PROBLEME
    )
    assert probleme.state == STATE_ON
    assert probleme.attributes[ATTR_LAST_ERROR] == ERREUR_INCONNUE
    assert probleme.attributes[ATTR_LAST_FAILED_SLUG] is None


async def test_un_evenement_sur_une_destination_inconnue_est_ignore(
    hass: HomeAssistant, entree_avec_destination: MockConfigEntry
) -> None:
    """Un événement visant une destination supprimée ne crée rien."""
    _emettre_echec(hass, "destination_jamais_configuree")
    _emettre_succes(hass, "destination_jamais_configuree")
    hass.bus.async_fire(
        EVENT_REMOTE_PURGE,
        {ATTR_DESTINATION: "destination_jamais_configuree", ATTR_DELETED: 3},
    )
    # Un événement sans champ `destination` exploitable est ignoré de même.
    hass.bus.async_fire(EVENT_UPLOAD_SUCCESSFUL, {ATTR_DESTINATION: None})
    await hass.async_block_till_done()

    assert (
        _etat(
            hass, entree_avec_destination, Platform.BINARY_SENSOR, SUFFIXE_PROBLEME
        ).state
        == STATE_OFF
    )
    assert (
        _etat(
            hass,
            entree_avec_destination,
            Platform.SENSOR,
            SUFFIXE_SAUVEGARDES_DISTANTES,
        ).state
        == "0"
    )


### MASQUAGE DES SECRETS ###


async def test_le_message_d_erreur_ne_laisse_pas_fuir_de_jeton(
    hass: HomeAssistant, entree_avec_destination: MockConfigEntry
) -> None:
    """Un jeton présent dans le message du fournisseur est masqué (critère 3)."""
    _emettre_echec(
        hass,
        erreur=(
            "HTTP 401 sur https://api.fournisseur.test/upload?"
            f"access_token={FAUX_JETON} — en-tête "
            f"« Authorization: Bearer {FAUX_JETON} »"
        ),
    )
    await hass.async_block_till_done()

    message = _etat(
        hass, entree_avec_destination, Platform.BINARY_SENSOR, SUFFIXE_PROBLEME
    ).attributes[ATTR_LAST_ERROR]

    assert FAUX_JETON not in message
    assert "***" in message
    # Le message reste diagnosticable : le code HTTP et l'hôte sont conservés.
    assert "HTTP 401" in message
    assert "api.fournisseur.test" in message


@pytest.mark.parametrize(
    "message",
    [
        f"access_token={FAUX_JETON}",
        f'{{"refresh_token": "{FAUX_JETON}"}}',
        f"Authorization: Bearer {FAUX_JETON}",
        f"client_secret='{FAUX_JETON}'",
        f"token: {FAUX_JETON}",
        f"Autorisation refusée (code={FAUX_JETON})",
    ],
)
def test_assainir_le_message_masque_toutes_les_formes(message: str) -> None:
    """Le masquage couvre les écritures usuelles d'un secret dans un message."""
    assaini = assainir_le_message(message)

    assert FAUX_JETON not in assaini
    assert "***" in assaini


def test_assainir_le_message_borne_la_longueur() -> None:
    """Un message verbeux est tronqué : il est recopié dans chaque état."""
    assaini = assainir_le_message("x" * (LONGUEUR_MAX_ERREUR * 3))

    assert len(assaini) == LONGUEUR_MAX_ERREUR


def test_assainir_le_message_conserve_un_message_ordinaire() -> None:
    """Un message sans secret traverse le filtre sans être abîmé."""
    assert assainir_le_message("  quota dépassé  ") == "quota dépassé"
    assert assainir_le_message("") == ERREUR_INCONNUE
    assert assainir_le_message(None) == ERREUR_INCONNUE


### NOMBRE DE SAUVEGARDES DISTANTES ###


async def test_le_compteur_suit_les_succes_et_les_purges(
    hass: HomeAssistant, entree_avec_destination: MockConfigEntry
) -> None:
    """Le compteur monte sur `upload_successful`, descend sur `remote_purge`."""
    for _ in range(3):
        _emettre_succes(hass)
    await hass.async_block_till_done()
    assert (
        _etat(
            hass,
            entree_avec_destination,
            Platform.SENSOR,
            SUFFIXE_SAUVEGARDES_DISTANTES,
        ).state
        == "3"
    )

    hass.bus.async_fire(
        EVENT_REMOTE_PURGE,
        {ATTR_DESTINATION: "destination_test", ATTR_DELETED: 2},
    )
    await hass.async_block_till_done()
    assert (
        _etat(
            hass,
            entree_avec_destination,
            Platform.SENSOR,
            SUFFIXE_SAUVEGARDES_DISTANTES,
        ).state
        == "1"
    )

    # Une purge qui annonce le nombre restant fait autorité sur le compteur.
    hass.bus.async_fire(
        EVENT_REMOTE_PURGE,
        {ATTR_DESTINATION: "destination_test", ATTR_DELETED: 1, ATTR_REMAINING: 7},
    )
    await hass.async_block_till_done()
    assert (
        _etat(
            hass,
            entree_avec_destination,
            Platform.SENSOR,
            SUFFIXE_SAUVEGARDES_DISTANTES,
        ).state
        == "7"
    )


async def test_le_compteur_ne_descend_jamais_sous_zero(
    hass: HomeAssistant, entree_avec_destination: MockConfigEntry
) -> None:
    """Une purge plus grande que le compteur le ramène à zéro, pas en négatif."""
    _emettre_succes(hass)
    hass.bus.async_fire(
        EVENT_REMOTE_PURGE,
        {ATTR_DESTINATION: "destination_test", ATTR_DELETED: 9},
    )
    await hass.async_block_till_done()

    assert (
        _etat(
            hass,
            entree_avec_destination,
            Platform.SENSOR,
            SUFFIXE_SAUVEGARDES_DISTANTES,
        ).state
        == "0"
    )


async def test_une_purge_sans_decompte_laisse_le_compteur_intact(
    hass: HomeAssistant, entree_avec_destination: MockConfigEntry
) -> None:
    """Une purge qui n'annonce rien d'exploitable ne change pas le compteur."""
    _emettre_succes(hass)
    _emettre_succes(hass)
    await hass.async_block_till_done()

    for donnees in (
        {},
        {ATTR_DELETED: 0},
        {ATTR_DELETED: True},
        {ATTR_DELETED: "beaucoup"},
        {ATTR_DELETED: []},
    ):
        hass.bus.async_fire(
            EVENT_REMOTE_PURGE, {ATTR_DESTINATION: "destination_test", **donnees}
        )
    await hass.async_block_till_done()

    assert (
        _etat(
            hass,
            entree_avec_destination,
            Platform.SENSOR,
            SUFFIXE_SAUVEGARDES_DISTANTES,
        ).state
        == "2"
    )


@pytest.mark.parametrize(
    ("supprimees", "attendu"),
    [(1, "2"), ("1", "2"), (["slug-a", "slug-b"], "1")],
)
async def test_une_purge_accepte_plusieurs_formes_de_decompte(
    hass: HomeAssistant,
    entree_avec_destination: MockConfigEntry,
    supprimees: Any,
    attendu: str,
) -> None:
    """Le décompte d'une purge peut être un nombre, un texte ou une liste."""
    for _ in range(3):
        _emettre_succes(hass)
    await hass.async_block_till_done()

    hass.bus.async_fire(
        EVENT_REMOTE_PURGE,
        {ATTR_DESTINATION: "destination_test", ATTR_DELETED: supprimees},
    )
    await hass.async_block_till_done()

    assert (
        _etat(
            hass,
            entree_avec_destination,
            Platform.SENSOR,
            SUFFIXE_SAUVEGARDES_DISTANTES,
        ).state
        == attendu
    )


async def test_une_source_de_comptes_enregistree_fait_autorite(
    hass: HomeAssistant, entree_avec_destination: MockConfigEntry
) -> None:
    """La rétention distante (#9) peut brancher un compteur faisant autorité."""
    _emettre_succes(hass)
    await hass.async_block_till_done()

    debrancher = async_enregistrer_source_des_comptes(
        hass,
        lambda destination_id: 42 if destination_id == "destination_test" else None,
    )
    await hass.async_block_till_done()
    assert (
        _etat(
            hass,
            entree_avec_destination,
            Platform.SENSOR,
            SUFFIXE_SAUVEGARDES_DISTANTES,
        ).state
        == "42"
    )

    debrancher()
    await hass.async_block_till_done()
    assert (
        _etat(
            hass,
            entree_avec_destination,
            Platform.SENSOR,
            SUFFIXE_SAUVEGARDES_DISTANTES,
        ).state
        == "1"
    )


async def test_une_source_de_comptes_defaillante_ne_casse_pas_le_capteur(
    hass: HomeAssistant, entree_avec_destination: MockConfigEntry
) -> None:
    """Si la source lève, le capteur retombe sur son compteur interne."""
    _emettre_succes(hass)
    await hass.async_block_till_done()

    def _source(_: str) -> int | None:
        raise RuntimeError("registre indisponible")

    async_enregistrer_source_des_comptes(hass, _source)
    await hass.async_block_till_done()

    assert (
        _etat(
            hass,
            entree_avec_destination,
            Platform.SENSOR,
            SUFFIXE_SAUVEGARDES_DISTANTES,
        ).state
        == "1"
    )


async def test_la_source_des_comptes_exige_des_entites_montees(
    hass: HomeAssistant, integration_backup: None
) -> None:
    """Enregistrer une source sans entités montées est une erreur explicite."""
    with pytest.raises(RuntimeError):
        async_enregistrer_source_des_comptes(hass, lambda _: 1)


### PLUSIEURS DESTINATIONS ###


async def test_les_destinations_ont_des_entites_independantes(
    hass: HomeAssistant, entree_avec_deux_destinations: MockConfigEntry
) -> None:
    """Un échec sur une destination n'affecte pas l'autre (critère 4)."""
    _emettre_echec(hass, DESTINATION_DEUX)
    _emettre_succes(hass, "destination_test")
    await hass.async_block_till_done()

    assert (
        _etat(
            hass,
            entree_avec_deux_destinations,
            Platform.BINARY_SENSOR,
            SUFFIXE_PROBLEME,
            DESTINATION_DEUX,
        ).state
        == STATE_ON
    )
    assert (
        _etat(
            hass,
            entree_avec_deux_destinations,
            Platform.BINARY_SENSOR,
            SUFFIXE_PROBLEME,
        ).state
        == STATE_OFF
    )
    assert (
        _etat(
            hass,
            entree_avec_deux_destinations,
            Platform.SENSOR,
            SUFFIXE_SAUVEGARDES_DISTANTES,
            DESTINATION_DEUX,
        ).state
        == "0"
    )
    assert (
        _etat(
            hass,
            entree_avec_deux_destinations,
            Platform.SENSOR,
            SUFFIXE_SAUVEGARDES_DISTANTES,
        ).state
        == "1"
    )


async def test_les_identifiants_uniques_portent_l_entree_et_la_destination(
    hass: HomeAssistant, entree_avec_deux_destinations: MockConfigEntry
) -> None:
    """`unique_id` = `<entry_id>_<destination_id>_<type>` (critère 4)."""
    registre = er.async_get(hass)
    entity_id = _entity_id(
        hass,
        entree_avec_deux_destinations,
        Platform.BINARY_SENSOR,
        SUFFIXE_PROBLEME,
        DESTINATION_DEUX,
    )

    assert entity_id is not None
    attendu = (
        f"{entree_avec_deux_destinations.entry_id}"
        f"_{DESTINATION_DEUX}_{SUFFIXE_PROBLEME}"
    )
    assert registre.async_get(entity_id).unique_id == attendu


### REDÉMARRAGE ###


async def test_le_dernier_succes_et_l_erreur_survivent_au_redemarrage(
    hass: HomeAssistant, entree_avec_destination: MockConfigEntry
) -> None:
    """Après rechargement, horodatage, compteur et erreur sont restaurés."""
    _emettre_succes(hass)
    _emettre_succes(hass)
    _emettre_echec(hass, slug="slug-echoue", erreur="quota dépassé")
    await hass.async_block_till_done()

    horodatage_avant = _etat(
        hass, entree_avec_destination, Platform.SENSOR, SUFFIXE_DERNIER_TELEVERSEMENT
    ).state
    assert isinstance(dt_util.parse_datetime(horodatage_avant), datetime)

    # Redémarrage simulé : l'entrée est déchargée puis rechargée.
    assert await hass.config_entries.async_reload(entree_avec_destination.entry_id)
    await hass.async_block_till_done()

    horodatage = _etat(
        hass, entree_avec_destination, Platform.SENSOR, SUFFIXE_DERNIER_TELEVERSEMENT
    )
    assert horodatage.state == horodatage_avant

    nombre = _etat(
        hass, entree_avec_destination, Platform.SENSOR, SUFFIXE_SAUVEGARDES_DISTANTES
    )
    assert nombre.state == "2"

    probleme = _etat(
        hass, entree_avec_destination, Platform.BINARY_SENSOR, SUFFIXE_PROBLEME
    )
    assert probleme.state == STATE_ON
    assert probleme.attributes[ATTR_LAST_ERROR] == "quota dépassé"
    assert probleme.attributes[ATTR_LAST_FAILED_SLUG] == "slug-echoue"
    assert probleme.attributes[ATTR_LAST_FAILED_AT] is not None


### AJOUT ET SUPPRESSION À CHAUD ###


async def test_une_destination_ajoutee_a_chaud_recoit_ses_entites(
    hass: HomeAssistant, entree_avec_destination: MockConfigEntry
) -> None:
    """Une destination ajoutée en options est équipée sans redémarrage."""
    assert (
        _entity_id(
            hass,
            entree_avec_destination,
            Platform.BINARY_SENSOR,
            SUFFIXE_PROBLEME,
            DESTINATION_DEUX,
        )
        is None
    )

    async_persist_destinations(
        hass,
        entree_avec_destination,
        [
            DestinationConfig.from_dict(config_factice()),
            DestinationConfig.from_dict(config_deux()),
        ],
    )
    await hass.async_block_till_done()

    for domaine, suffixe in (
        (Platform.SENSOR, SUFFIXE_DERNIER_TELEVERSEMENT),
        (Platform.SENSOR, SUFFIXE_SAUVEGARDES_DISTANTES),
        (Platform.BINARY_SENSOR, SUFFIXE_PROBLEME),
    ):
        etat = _etat(hass, entree_avec_destination, domaine, suffixe, DESTINATION_DEUX)
        assert etat.state != STATE_UNAVAILABLE

    # La nouvelle destination est vivante : elle réagit à ses propres événements.
    _emettre_echec(hass, DESTINATION_DEUX)
    await hass.async_block_till_done()
    assert (
        _etat(
            hass,
            entree_avec_destination,
            Platform.BINARY_SENSOR,
            SUFFIXE_PROBLEME,
            DESTINATION_DEUX,
        ).state
        == STATE_ON
    )


async def test_une_destination_supprimee_perd_ses_entites(
    hass: HomeAssistant, entree_avec_deux_destinations: MockConfigEntry
) -> None:
    """Les entités d'une destination supprimée quittent le registre (critère 6)."""
    assert (
        _entity_id(
            hass,
            entree_avec_deux_destinations,
            Platform.BINARY_SENSOR,
            SUFFIXE_PROBLEME,
            DESTINATION_DEUX,
        )
        is not None
    )

    async_persist_destinations(
        hass,
        entree_avec_deux_destinations,
        [DestinationConfig.from_dict(config_factice())],
    )
    await hass.async_block_till_done()

    for domaine, suffixe in (
        (Platform.SENSOR, SUFFIXE_DERNIER_TELEVERSEMENT),
        (Platform.SENSOR, SUFFIXE_SAUVEGARDES_DISTANTES),
        (Platform.BINARY_SENSOR, SUFFIXE_PROBLEME),
    ):
        assert (
            _entity_id(
                hass, entree_avec_deux_destinations, domaine, suffixe, DESTINATION_DEUX
            )
            is None
        ), f"{suffixe} de {DESTINATION_DEUX} toujours dans le registre"

    # La destination conservée n'est pas touchée.
    assert (
        _entity_id(
            hass,
            entree_avec_deux_destinations,
            Platform.BINARY_SENSOR,
            SUFFIXE_PROBLEME,
        )
        is not None
    )


### CYCLE DE VIE DE L'ENTRÉE ###


async def test_le_coordinateur_disparait_au_dechargement(
    hass: HomeAssistant, entree_avec_destination: MockConfigEntry
) -> None:
    """Le déchargement de l'entrée nettoie `hass.data` et rend les entités KO."""
    assert DATA_DESTINATION_ENTITIES in hass.data
    entity_id = _entity_id(
        hass, entree_avec_destination, Platform.BINARY_SENSOR, SUFFIXE_PROBLEME
    )

    assert await hass.config_entries.async_unload(entree_avec_destination.entry_id)
    await hass.async_block_till_done()

    assert DATA_DESTINATION_ENTITIES not in hass.data
    assert hass.states.get(entity_id).state == STATE_UNAVAILABLE


async def test_un_evenement_recu_pendant_l_ajout_n_est_pas_ecrase(
    hass: HomeAssistant, entree_avec_destination: MockConfigEntry
) -> None:
    """L'état acquis avant le montage des entités survit à leur restauration.

    L'écouteur d'options est exécuté sans attente : le coordinateur connaît la
    nouvelle destination — et traite donc ses événements — avant que Home
    Assistant n'ait fini d'ajouter ses entités. Ce que ces entités restaurent
    ne doit pas écraser ce que le coordinateur a déjà enregistré.
    """
    async_persist_destinations(
        hass,
        entree_avec_destination,
        [
            DestinationConfig.from_dict(config_factice()),
            DestinationConfig.from_dict(config_deux()),
        ],
    )
    coordinateur = hass.data[DATA_DESTINATION_ENTITIES]
    assert {config.destination_id for config in coordinateur.destinations} == {
        "destination_test",
        DESTINATION_DEUX,
    }

    _emettre_succes(hass, DESTINATION_DEUX)
    await hass.async_block_till_done()

    assert (
        _etat(
            hass,
            entree_avec_destination,
            Platform.SENSOR,
            SUFFIXE_SAUVEGARDES_DISTANTES,
            DESTINATION_DEUX,
        ).state
        == "1"
    )
    assert (
        _etat(
            hass,
            entree_avec_destination,
            Platform.SENSOR,
            SUFFIXE_DERNIER_TELEVERSEMENT,
            DESTINATION_DEUX,
        ).state
        != STATE_UNKNOWN
    )
