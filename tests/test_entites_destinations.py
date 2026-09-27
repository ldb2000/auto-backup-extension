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
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import restore_state
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
    ATTR_REMOTE_IDS,
    ATTR_SIZE,
    ATTR_SLUG,
    CONF_DESTINATIONS,
    DATA_DESTINATION_ENTITIES,
    DATA_REMOTE_BACKUPS,
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
    CapteurBinaireProblemeDestination,
    CapteurSauvegardesDistantes,
    assainir_le_message,
    identifiant_unique,
)
from custom_components.auto_backup.destinations.masquage import masquer
from custom_components.auto_backup.destinations.retention import (
    EntreeRegistre,
    RegistreSauvegardesDistantes,
)
from destinations_factices import config_factice

# Deuxième destination, pour éprouver l'indépendance des entités.
DESTINATION_DEUX = "destination_deux"
NOM_DESTINATION_DEUX = "Destination secondaire"

# Jetons inventés, dans les formes réellement émises par les fournisseurs :
# aucun ne doit ressortir dans un attribut d'entité. Aucune valeur réelle ici.
FAUX_JETON = "sl.FAUX-JETON-a1b2c3d4e5f6"
FAUX_JETON_GOOGLE = "ya29.FAUX-JETON-a1b2c3d4e5f6"
FAUX_RAFRAICHISSEMENT_GOOGLE = "1//FAUX-JETON-a1b2c3d4e5f6"
FAUX_UPLOAD_ID = "AEnB2UoFAUXident1f1antDeSess10n"
FAUX_JETON_BASE64 = "ZXlKaGJHY2lPaUpJVXpJMU5pSjk"


class RegistreFactice:
    """Doublure du registre des sauvegardes distantes de l'issue #9.

    Seule `entrees(destination_id)` est utilisée par les entités : le capteur
    compte ce qu'elle renvoie. Le mode `defaillant` éprouve le repli quand le
    registre lève au lieu de répondre — un cas que le registre réel ne sait pas
    produire, d'où la doublure. Le contrat avec le vrai registre est éprouvé,
    lui, par `test_le_registre_reel_de_la_retention_alimente_le_compteur`.
    """

    def __init__(self, comptes: dict[str, int], *, defaillant: bool = False) -> None:
        """Prépare un registre rendant `comptes[destination_id]` entrées."""
        self.comptes = comptes
        self.defaillant = defaillant

    def entrees(self, destination_id: str) -> list[str]:
        """Sauvegardes distantes connues pour cette destination."""
        if self.defaillant:
            raise RuntimeError("registre indisponible")
        return [f"distant-{index}" for index in range(self.comptes[destination_id])]


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


@pytest.fixture
def entree_sans_registre_distant(
    hass: HomeAssistant, entree_avec_destination: MockConfigEntry
) -> MockConfigEntry:
    """Entrée dont le registre de la rétention distante a été retiré.

    Depuis la fusion de l'issue #9, l'entrée monte **toujours** son registre
    persistant, qui fait alors autorité sur le capteur de comptage : le compteur
    de repli tenu par les événements ne sert plus qu'aux deux cas où ce registre
    ne répond pas — clé absente de `hass.data`, ou lecture en échec. Le retirer
    est donc le seul moyen d'éprouver ce chemin de bout en bout.
    """
    hass.data.pop(DATA_REMOTE_BACKUPS, None)
    return entree_avec_destination


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
    remote_id: str = "distant-1",
) -> None:
    """Émet `auto_backup.upload_successful` comme le fait le coordinateur #8.

    `remote_id` identifie le fichier chez le fournisseur : deux succès qui le
    partagent désignent la **même** sauvegarde, et le registre de la rétention
    distante (#9) n'en retient alors qu'une entrée. Les tests qui veulent voir
    le compteur monter donnent donc des identifiants distincts.
    """
    hass.bus.async_fire(
        EVENT_UPLOAD_SUCCESSFUL,
        {
            ATTR_NAME: nom,
            ATTR_SLUG: slug,
            ATTR_DESTINATION: destination_id,
            ATTR_DESTINATION_NAME: "Destination de test",
            ATTR_SIZE: 1234,
            ATTR_REMOTE_ID: remote_id,
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
    ("erreur", "fuite", "attendu"),
    [
        # Clé sensible en camelCase, séparateur deux-points.
        ("accessToken: abcDEF123456", "abcDEF123456", "accessToken: ***"),
        # Chemin absolu d'une sauvegarde : il révèle l'arborescence de l'hôte.
        (
            "échec lecture /backup/sauvegarde_maison.tar",
            "/backup/sauvegarde_maison.tar",
            "échec lecture ***",
        ),
        (
            "fichier introuvable : /config/secrets.yaml",
            "/config/secrets.yaml",
            "fichier introuvable : ***",
        ),
        # Liste de jetons entre crochets.
        ("tokens=[abc123XYZ, def]", "abc123XYZ", "tokens=***"),
    ],
)
async def test_last_error_suit_le_masquage_commun(
    hass: HomeAssistant,
    entree_avec_destination: MockConfigEntry,
    erreur: str,
    fuite: str,
    attendu: str,
) -> None:
    """`last_error` applique le module commun `masquage.py`, pas une copie.

    Non-régression de la validation métier de l'issue #16 : ces trois formes
    passaient en clair par l'ancienne copie locale du masquage.
    """
    _emettre_echec(hass, erreur=erreur)
    await hass.async_block_till_done()

    message = _etat(
        hass, entree_avec_destination, Platform.BINARY_SENSOR, SUFFIXE_PROBLEME
    ).attributes[ATTR_LAST_ERROR]

    assert fuite not in message
    assert message == attendu
    assert message == masquer(erreur, longueur_max=LONGUEUR_MAX_ERREUR)


@pytest.mark.parametrize(
    ("message", "secret"),
    [
        (f"access_token={FAUX_JETON}", FAUX_JETON),
        (f'{{"refresh_token": "{FAUX_JETON}"}}', FAUX_JETON),
        (f"Authorization: Bearer {FAUX_JETON}", FAUX_JETON),
        (f"client_secret='{FAUX_JETON}'", FAUX_JETON),
        (f"token: {FAUX_JETON}", FAUX_JETON),
        (f"Autorisation refusée (code={FAUX_JETON})", FAUX_JETON),
        # `\b` ne verrait pas cette clé : `_` est un caractère de mot.
        (f"authorization_code={FAUX_JETON}", FAUX_JETON),
    ],
)
def test_assainir_le_message_masque_toutes_les_formes(
    message: str, secret: str
) -> None:
    """Le masquage couvre les écritures usuelles d'un secret dans un message."""
    assaini = assainir_le_message(message)

    assert secret not in assaini
    assert "***" in assaini


@pytest.mark.parametrize(
    ("message", "secret"),
    [
        # Jeton Dropbox nu : aucune clé, aucun en-tête autour de lui.
        (FAUX_JETON, FAUX_JETON),
        # Mot-clé séparé du jeton par une simple espace.
        (f"invalid token {FAUX_JETON}", FAUX_JETON),
        # URL de session reprenable Google : elle vaut jeton de reprise.
        (
            "HTTP 400 sur https://www.googleapis.com/upload/drive/v3/files"
            f"?uploadType=resumable&upload_id={FAUX_UPLOAD_ID}",
            FAUX_UPLOAD_ID,
        ),
        # Jeton encodé, sans mot-clé ni forme reconnaissable.
        (f"le fournisseur a refusé {FAUX_JETON_BASE64}", FAUX_JETON_BASE64),
        # Formes propres à Google, nues elles aussi.
        (f"credentials {FAUX_JETON_GOOGLE} rejected", FAUX_JETON_GOOGLE),
        (
            f"bad refresh token {FAUX_RAFRAICHISSEMENT_GOOGLE}",
            FAUX_RAFRAICHISSEMENT_GOOGLE,
        ),
    ],
)
def test_assainir_le_message_masque_un_jeton_nu(message: str, secret: str) -> None:
    """Un jeton sans clé adjacente est masqué par sa forme (audit sécurité)."""
    assaini = assainir_le_message(message)

    assert secret not in assaini
    assert "***" in assaini


def test_assainir_le_message_masque_une_adresse_electronique() -> None:
    """Une adresse citée par le fournisseur n'entre pas dans l'historique."""
    assaini = assainir_le_message("compte jean.dupont@example.com non autorisé")

    assert "jean.dupont" not in assaini
    assert "example.com" not in assaini
    assert assaini == "compte *** non autorisé"


def test_assainir_le_message_garde_une_url_lisible() -> None:
    """Le filtre générique coupe aux `/` et `=` : l'URL reste diagnosticable."""
    assaini = assainir_le_message(
        "HTTP 400 sur https://www.googleapis.com/upload/drive/v3/files"
        f"?uploadType=resumable&upload_id={FAUX_UPLOAD_ID}"
    )

    assert "www.googleapis.com/upload/drive/v3/files" in assaini
    assert "uploadType=resumable" in assaini


def test_assainir_le_message_borne_la_longueur() -> None:
    """Un message verbeux est tronqué : il est recopié dans chaque état."""
    assaini = assainir_le_message("x" * (LONGUEUR_MAX_ERREUR * 3))

    assert len(assaini) == LONGUEUR_MAX_ERREUR


@pytest.mark.parametrize(
    "message",
    [
        # Un mot ordinaire derrière un mot-clé : le masquer ne protégerait rien
        # et rendrait les messages français illisibles.
        "le token a expiré",
        "le code de la sauvegarde est invalide",
        # Suites longues d'une seule casse et sans chiffre : des mots, pas des
        # secrets — le filtre générique ne doit pas les toucher.
        "anticonstitutionnellement impossible",
        "ERREUR IRRECUPERABLE COTE FOURNISSEUR",
    ],
)
def test_assainir_le_message_ne_masque_pas_le_francais(message: str) -> None:
    """Le masquage large s'arrête aux mots : le message reste lisible."""
    assert assainir_le_message(message) == message


def test_assainir_le_message_conserve_un_message_ordinaire() -> None:
    """Un message sans secret traverse le filtre sans être abîmé."""
    assert assainir_le_message("  quota dépassé  ") == "quota dépassé"
    assert assainir_le_message("") == ERREUR_INCONNUE
    assert assainir_le_message(None) == ERREUR_INCONNUE


### NOMBRE DE SAUVEGARDES DISTANTES ###


async def test_le_capteur_compte_ce_qui_reste_et_non_les_envois(
    hass: HomeAssistant, entree_avec_destination: MockConfigEntry
) -> None:
    """Le capteur compte les sauvegardes présentes, pas les téléversements.

    Le registre de la rétention distante (#9) est monté par l'entrée elle-même :
    il fait autorité dès le premier succès, sans que le test ait rien à
    installer. Réécrire la même sauvegarde ne crée pas un second fichier chez le
    fournisseur, et le capteur n'en compte donc qu'une.
    """
    assert isinstance(hass.data[DATA_REMOTE_BACKUPS], RegistreSauvegardesDistantes)

    for _ in range(3):
        _emettre_succes(hass, remote_id="distant-1")
    _emettre_succes(hass, remote_id="distant-2")
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


async def test_le_compteur_de_repli_suit_les_succes_et_les_purges(
    hass: HomeAssistant, entree_sans_registre_distant: MockConfigEntry
) -> None:
    """Sans registre, le compteur monte sur un succès et descend sur une purge."""
    for _ in range(3):
        _emettre_succes(hass)
    await hass.async_block_till_done()
    assert (
        _etat(
            hass,
            entree_sans_registre_distant,
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
            entree_sans_registre_distant,
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
            entree_sans_registre_distant,
            Platform.SENSOR,
            SUFFIXE_SAUVEGARDES_DISTANTES,
        ).state
        == "7"
    )


async def test_le_compteur_de_repli_ne_descend_jamais_sous_zero(
    hass: HomeAssistant, entree_sans_registre_distant: MockConfigEntry
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
            entree_sans_registre_distant,
            Platform.SENSOR,
            SUFFIXE_SAUVEGARDES_DISTANTES,
        ).state
        == "0"
    )


async def test_une_purge_sans_decompte_laisse_le_compteur_intact(
    hass: HomeAssistant, entree_sans_registre_distant: MockConfigEntry
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
            entree_sans_registre_distant,
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
    entree_sans_registre_distant: MockConfigEntry,
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
            entree_sans_registre_distant,
            Platform.SENSOR,
            SUFFIXE_SAUVEGARDES_DISTANTES,
        ).state
        == attendu
    )


async def test_le_registre_de_la_retention_fait_autorite(
    hass: HomeAssistant, entree_avec_destination: MockConfigEntry
) -> None:
    """Le registre persistant de #9 l'emporte sur le compteur de repli."""
    _emettre_succes(hass)
    await hass.async_block_till_done()

    registre = RegistreFactice({"destination_test": 42})
    hass.data[DATA_REMOTE_BACKUPS] = registre

    # L'événement n'est plus qu'un déclencheur : le compte vient du registre.
    _emettre_succes(hass)
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

    # Une purge relit le registre au lieu de retrancher quoi que ce soit.
    registre.comptes["destination_test"] = 40
    hass.bus.async_fire(
        EVENT_REMOTE_PURGE,
        {
            ATTR_DESTINATION: "destination_test",
            ATTR_REMOTE_IDS: ["distant-1", "distant-2"],
        },
    )
    await hass.async_block_till_done()
    assert (
        _etat(
            hass,
            entree_avec_destination,
            Platform.SENSOR,
            SUFFIXE_SAUVEGARDES_DISTANTES,
        ).state
        == "40"
    )


async def test_le_registre_reel_de_la_retention_alimente_le_compteur(
    hass: HomeAssistant, entree_avec_destination: MockConfigEntry
) -> None:
    """Le capteur compte les entrées du `RegistreSauvegardesDistantes` de #9.

    Les autres tests de cette section passent par une doublure ; celui-ci monte
    le registre réel de la rétention distante pour vérifier qu'il n'y a pas que
    les chaînes qui correspondent, mais bien le contrat : le capteur appelle
    `entrees(destination_id)` et en compte les `EntreeRegistre`.

    Il montre aussi ce que le capteur **ne** compte pas : une sauvegarde
    enregistrée pour une autre destination, et un fichier que l'utilisateur
    aurait déposé lui-même dans le dossier, absent du registre par construction.
    """
    registre = RegistreSauvegardesDistantes(hass)
    hass.data[DATA_REMOTE_BACKUPS] = registre

    for index in range(2):
        await registre.async_enregistrer(
            "destination_test",
            EntreeRegistre(remote_id=f"distant-{index}", name=f"nuit-{index}.tar"),
        )
    # Bruit : cette entrée appartient à une autre destination.
    await registre.async_enregistrer(
        DESTINATION_DEUX, EntreeRegistre(remote_id="ailleurs", name="ailleurs.tar")
    )

    _emettre_succes(hass)
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

    # Une purge réelle retire l'entrée : le capteur relit et redescend.
    assert await registre.async_retirer("destination_test", ["distant-0"])
    hass.bus.async_fire(
        EVENT_REMOTE_PURGE,
        {ATTR_DESTINATION: "destination_test", ATTR_REMOTE_IDS: ["distant-0"]},
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


async def test_sans_registre_le_compteur_de_repli_prend_le_relais(
    hass: HomeAssistant, entree_sans_registre_distant: MockConfigEntry
) -> None:
    """Registre absent de `hass.data` : `remote_ids` alimente le repli."""
    assert DATA_REMOTE_BACKUPS not in hass.data

    for _ in range(3):
        _emettre_succes(hass)
    hass.bus.async_fire(
        EVENT_REMOTE_PURGE,
        {ATTR_DESTINATION: "destination_test", ATTR_REMOTE_IDS: ["distant-1"]},
    )
    await hass.async_block_till_done()

    assert (
        _etat(
            hass,
            entree_sans_registre_distant,
            Platform.SENSOR,
            SUFFIXE_SAUVEGARDES_DISTANTES,
        ).state
        == "2"
    )


async def test_un_registre_qui_ignore_la_destination_laisse_le_repli(
    hass: HomeAssistant, entree_avec_destination: MockConfigEntry
) -> None:
    """Un registre défaillant ne casse pas le capteur : il retombe au repli."""
    _emettre_succes(hass)
    hass.data[DATA_REMOTE_BACKUPS] = RegistreFactice({}, defaillant=True)
    _emettre_succes(hass)
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
    """Après rechargement, horodatage, compteur et erreur sont restaurés.

    Le compteur ne se restaure pas de la même façon que les deux autres états :
    l'horodatage et l'erreur reviennent du cache de `RestoreEntity`, tandis que
    les deux sauvegardes viennent du registre persistant de la rétention
    distante (#9), qui a survécu au rechargement par lui-même. D'où les deux
    identifiants distants **distincts** : c'est le registre qui est interrogé,
    et il ne compte pas deux fois le même fichier.
    """
    _emettre_succes(hass, remote_id="distant-1")
    _emettre_succes(hass, remote_id="distant-2")
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


def _inscrire_au_cache_de_restauration(
    hass: HomeAssistant,
    etat: State,
    donnees: dict[str, Any] | None = None,
) -> None:
    """Inscrit un état à restaurer dans le cache déjà en place.

    `mock_restore_cache()` remplacerait le cache entier, et les entités déjà
    montées ne s'y retrouveraient plus au démontage. `donnees` porte les données
    propres à un `RestoreSensor` (`native_value`), que l'état seul ne contient
    pas.
    """
    restore_state.async_get(hass).last_states[etat.entity_id] = (
        restore_state.StoredState(
            etat,
            restore_state.RestoredExtraData(donnees) if donnees is not None else None,
            dt_util.utcnow(),
        )
    )


def _preparer_une_entite_montee_apres_coup(
    hass: HomeAssistant,
    capteur: Any,
    entity_id: str,
    restaure: State,
    donnees: dict[str, Any] | None = None,
) -> Any:
    """Prépare une entité que le test montera lui-même, après les événements.

    Home Assistant ajoute les entités d'une destination *après* que le
    coordinateur a commencé à traiter ses événements : l'écouteur d'options
    s'exécute sans attente. Monter l'entité à la main rend cet ordre
    déterministe, là où un test de bout en bout laisse la boucle d'événements
    décider lequel des deux passe en premier.
    """
    capteur.hass = hass
    capteur.entity_id = entity_id
    _inscrire_au_cache_de_restauration(hass, restaure, donnees)
    return capteur


async def test_une_erreur_resolue_n_est_pas_reintroduite_par_la_restauration(
    hass: HomeAssistant, entree_avec_destination: MockConfigEntry
) -> None:
    """Un succès reçu avant le montage l'emporte sur l'état « problème » restauré.

    `derniere_erreur is None` ne suffit pas à décider : cette nullité signifie
    aussi bien « aucun événement depuis le démarrage » que « un téléversement
    vient de réussir et a effacé l'erreur ». S'y fier ferait repasser le capteur
    en « problème » sur la foi d'un état périmé (audit sécurité, MAJEUR 1).
    """
    capteur = _preparer_une_entite_montee_apres_coup(
        hass,
        CapteurBinaireProblemeDestination(
            entree_avec_destination,
            hass.data[DATA_DESTINATION_ENTITIES],
            DestinationConfig.from_dict(config_factice()),
        ),
        "binary_sensor.probleme_monte_apres_coup",
        State(
            "binary_sensor.probleme_monte_apres_coup",
            STATE_ON,
            {ATTR_LAST_ERROR: "vieille erreur résolue"},
        ),
    )

    # Le téléversement réussit avant que l'entité n'ait fini d'être montée.
    _emettre_succes(hass)
    await hass.async_block_till_done()

    await capteur.async_added_to_hass()

    assert capteur.is_on is False
    assert capteur.extra_state_attributes[ATTR_LAST_ERROR] is None


async def test_un_compteur_purge_n_est_pas_ecrase_par_la_restauration(
    hass: HomeAssistant, entree_sans_registre_distant: MockConfigEntry
) -> None:
    """Une purge ramenant le compteur à zéro résiste à la valeur restaurée.

    `not sauvegardes_distantes` confondrait « jamais compté » et « purgé » : le
    compte périmé se réinstallerait (audit sécurité, MINEUR 4).
    """
    capteur = _preparer_une_entite_montee_apres_coup(
        hass,
        CapteurSauvegardesDistantes(
            entree_sans_registre_distant,
            hass.data[DATA_DESTINATION_ENTITIES],
            DestinationConfig.from_dict(config_factice()),
        ),
        "sensor.compteur_monte_apres_coup",
        State("sensor.compteur_monte_apres_coup", "5"),
        {"native_value": 5, "native_unit_of_measurement": None},
    )

    # La purge vide la destination avant que le capteur ne soit monté.
    hass.bus.async_fire(
        EVENT_REMOTE_PURGE,
        {ATTR_DESTINATION: "destination_test", ATTR_REMAINING: 0},
    )
    await hass.async_block_till_done()

    await capteur.async_added_to_hass()

    assert capteur.native_value == 0


async def test_un_compteur_vierge_accepte_la_valeur_restauree(
    hass: HomeAssistant, entree_sans_registre_distant: MockConfigEntry
) -> None:
    """Sans événement reçu, la valeur restaurée réarme bien le compteur."""
    capteur = _preparer_une_entite_montee_apres_coup(
        hass,
        CapteurSauvegardesDistantes(
            entree_sans_registre_distant,
            hass.data[DATA_DESTINATION_ENTITIES],
            DestinationConfig.from_dict(config_factice()),
        ),
        "sensor.compteur_vierge",
        State("sensor.compteur_vierge", "5"),
        {"native_value": 5, "native_unit_of_measurement": None},
    )

    await capteur.async_added_to_hass()

    assert capteur.native_value == 5


async def test_l_erreur_restauree_est_reassainie(
    hass: HomeAssistant, entree_avec_destination: MockConfigEntry
) -> None:
    """Un historique écrit par une version au masquage plus étroit est nettoyé."""
    capteur = _preparer_une_entite_montee_apres_coup(
        hass,
        CapteurBinaireProblemeDestination(
            entree_avec_destination,
            hass.data[DATA_DESTINATION_ENTITIES],
            DestinationConfig.from_dict(config_factice()),
        ),
        "binary_sensor.probleme_a_reassainir",
        State(
            "binary_sensor.probleme_a_reassainir",
            STATE_ON,
            {ATTR_LAST_ERROR: f"invalid token {FAUX_JETON}"},
        ),
    )

    await capteur.async_added_to_hass()

    message = capteur.extra_state_attributes[ATTR_LAST_ERROR]
    assert capteur.is_on is True
    assert FAUX_JETON not in message
    assert "***" in message


@pytest.mark.parametrize(
    ("erreur", "fuite"),
    [
        # Chemin absolu : révèle l'arborescence de l'hôte, pas seulement à
        # l'échec — un `last_error` écrit avant l'ajout du masquage des
        # chemins doit être nettoyé lui aussi à la restauration.
        ("fichier introuvable : /config/secrets.yaml", "/config/secrets.yaml"),
        # Clé sensible en camelCase, portant un jeton : même exigence.
        (f"accessToken: {FAUX_JETON}", FAUX_JETON),
    ],
)
async def test_l_erreur_restauree_avec_un_chemin_ou_une_cle_sensible_est_reassainie(
    hass: HomeAssistant,
    entree_avec_destination: MockConfigEntry,
    erreur: str,
    fuite: str,
) -> None:
    """Un `last_error` ancien contenant un chemin ou un jeton par clé est masqué.

    Non-régression du critère d'acceptation de l'issue #16 : un message
    restauré après redémarrage ne doit pas exposer de chemin `/config/…` ni de
    jeton porté par une clé sensible (`accessToken`), au même titre qu'un
    message frais issu d'un échec (`test_last_error_suit_le_masquage_commun`).
    """
    capteur = _preparer_une_entite_montee_apres_coup(
        hass,
        CapteurBinaireProblemeDestination(
            entree_avec_destination,
            hass.data[DATA_DESTINATION_ENTITIES],
            DestinationConfig.from_dict(config_factice()),
        ),
        "binary_sensor.probleme_chemin_ou_cle_a_reassainir",
        State(
            "binary_sensor.probleme_chemin_ou_cle_a_reassainir",
            STATE_ON,
            {ATTR_LAST_ERROR: erreur},
        ),
    )

    await capteur.async_added_to_hass()

    message = capteur.extra_state_attributes[ATTR_LAST_ERROR]
    assert capteur.is_on is True
    assert fuite not in message
    assert "***" in message
    assert message == masquer(erreur, longueur_max=LONGUEUR_MAX_ERREUR)


async def test_un_capteur_binaire_restaure_sans_probleme_reste_au_repos(
    hass: HomeAssistant, entree_avec_destination: MockConfigEntry
) -> None:
    """Un état restauré « pas de problème » n'invente aucune erreur active."""
    capteur = _preparer_une_entite_montee_apres_coup(
        hass,
        CapteurBinaireProblemeDestination(
            entree_avec_destination,
            hass.data[DATA_DESTINATION_ENTITIES],
            DestinationConfig.from_dict(config_factice()),
        ),
        "binary_sensor.probleme_au_repos",
        State(
            "binary_sensor.probleme_au_repos",
            STATE_OFF,
            {ATTR_LAST_FAILED_SLUG: "slug-ancien"},
        ),
    )

    await capteur.async_added_to_hass()

    attributs = capteur.extra_state_attributes
    assert capteur.is_on is False
    assert attributs[ATTR_LAST_ERROR] is None
    assert attributs[ATTR_LAST_FAILED_SLUG] == "slug-ancien"


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
