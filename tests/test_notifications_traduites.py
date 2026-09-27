"""Traduction des notifications persistantes (issue #45).

Les notifications d'échec de téléversement et de ré-autorisation s'affichent
dans la langue de Home Assistant : en anglais sur une instance anglaise, en
français sur une instance française, et en anglais pour toute langue que le
fork ne traduit pas. Le comptage des échecs, l'identifiant stable et le masquage
ne changent pas : ils sont couverts par `tests/test_notifications.py`, que ce
module complète sans le répéter.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from homeassistant.components import persistent_notification
from homeassistant.const import ATTR_NAME
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.auto_backup.const import (
    ATTR_DESTINATION,
    ATTR_DESTINATION_NAME,
    ATTR_ERROR,
    ATTR_SLUG,
    CONF_DESTINATIONS,
    DOMAIN,
    EVENT_UPLOAD_FAILED,
    EVENT_UPLOAD_SUCCESSFUL,
)
from custom_components.auto_backup.destinations import (
    DestinationConfig,
    async_effacer_la_reauthentification,
    async_signaler_la_reauthentification,
    notifications,
    traductions,
)
from custom_components.auto_backup.destinations.models import VALEUR_MASQUEE
from custom_components.auto_backup.destinations.notifications import (
    CLE_MESSAGE_ECHEC,
    CLE_TITRE_ECHEC,
    _chemin,
    _texte,
    identifiant_de_notification_d_echec,
    identifiant_de_notification_de_reauthentification,
)
from destinations_factices import config_factice, config_oauth_factice

DESTINATION = "destination_test"
NOM_DE_LA_DESTINATION = "Destination de test"
NOM_DE_LA_SAUVEGARDE = "Sauvegarde du 25/09/2026"

TITRE_ANGLAIS = "Auto Backup: upload to “Destination de test” failed"
TITRE_FRANCAIS = "Auto Backup : échec d'envoi vers « Destination de test »"


async def _charger(hass: HomeAssistant, langue: str, destination: dict) -> None:
    """Règle la langue de l'instance, puis charge une entrée `auto_backup`."""
    hass.config.language = langue
    entree = MockConfigEntry(
        domain=DOMAIN,
        title="Auto Backup",
        data={},
        options={CONF_DESTINATIONS: [destination]},
    )
    entree.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entree.entry_id)
    await hass.async_block_till_done()


async def _echec(hass: HomeAssistant, **donnees: Any) -> None:
    """Émet l'événement d'échec définitif d'un téléversement."""
    hass.bus.async_fire(
        EVENT_UPLOAD_FAILED,
        {
            ATTR_NAME: NOM_DE_LA_SAUVEGARDE,
            ATTR_SLUG: "abcd1234",
            ATTR_DESTINATION: DESTINATION,
            ATTR_DESTINATION_NAME: NOM_DE_LA_DESTINATION,
            ATTR_ERROR: "quota exceeded",
            **donnees,
        },
    )
    await hass.async_block_till_done()


def _notification(hass: HomeAssistant, notification_id: str) -> dict[str, Any]:
    notifications = persistent_notification._async_get_or_create_notifications(hass)
    notification = notifications.get(notification_id)
    assert notification is not None, f"notification {notification_id} absente"
    return notification


def _notification_d_echec(hass: HomeAssistant) -> dict[str, Any]:
    return _notification(hass, identifiant_de_notification_d_echec(DESTINATION))


### Échec de téléversement ###


async def test_une_instance_anglaise_recoit_une_notification_en_anglais(
    hass: HomeAssistant, integration_backup: None, fournisseur_factice: str
) -> None:
    """Critère : titre et message en anglais sur une instance en anglais."""
    await _charger(hass, "en", config_factice())

    await _echec(hass)
    await _echec(hass)

    notification = _notification_d_echec(hass)
    assert notification["title"] == TITRE_ANGLAIS
    message = notification["message"]
    assert message.startswith(
        f"Backup “{NOM_DE_LA_SAUVEGARDE}” could not be uploaded to destination "
        f"“{NOM_DE_LA_DESTINATION}”."
    )
    assert "**Cause**: quota exceeded" in message
    assert "Consecutive failures for this destination: 2." in message


async def test_une_instance_francaise_recoit_une_notification_en_francais(
    hass: HomeAssistant, integration_backup: None, fournisseur_factice: str
) -> None:
    """Critère : la même situation, sur une instance en français."""
    await _charger(hass, "fr", config_factice())

    await _echec(hass)

    notification = _notification_d_echec(hass)
    assert notification["title"] == TITRE_FRANCAIS
    assert notification["message"].startswith(
        f"La sauvegarde « {NOM_DE_LA_SAUVEGARDE} » n'a pas pu être envoyée"
    )
    assert "Échecs consécutifs vers cette destination : 1." in notification["message"]


@pytest.mark.parametrize("langue", ["es", "ja", "de"])
async def test_une_langue_sans_traduction_retombe_sur_l_anglais(
    hass: HomeAssistant,
    integration_backup: None,
    fournisseur_factice: str,
    langue: str,
) -> None:
    """Critère : l'anglais sert de repli.

    `de` est une langue héritée de l'upstream, sans les clés du fork : ses
    textes manquants sont complétés par l'anglais, comme ceux d'une langue que
    l'intégration ne connaît pas du tout.
    """
    await _charger(hass, langue, config_factice())

    await _echec(hass)

    assert _notification_d_echec(hass)["title"] == TITRE_ANGLAIS


async def test_les_valeurs_par_defaut_sont_traduites(
    hass: HomeAssistant, integration_backup: None, fournisseur_factice: str
) -> None:
    """Sauvegarde sans nom ni slug, cause absente : textes de la langue courante."""
    await _charger(hass, "en", config_factice())

    await _echec(hass, **{ATTR_NAME: None, ATTR_SLUG: None, ATTR_ERROR: None})

    message = _notification_d_echec(hass)["message"]
    assert "Backup “unnamed”" in message
    assert "**Cause**: unknown cause" in message


async def test_le_masquage_porte_sur_les_valeurs_pas_sur_le_texte_traduit(
    hass: HomeAssistant, integration_backup: None, fournisseur_factice: str
) -> None:
    """Critère : la cause est masquée, le texte anglais reste intact."""
    await _charger(hass, "en", config_factice())

    await _echec(hass, **{ATTR_ERROR: "refused: access_token=sl.JETONINVENTE123456"})

    message = _notification_d_echec(hass)["message"]
    assert "JETONINVENTE" not in message
    assert VALEUR_MASQUEE in message
    assert "The local backup itself is intact." in message


async def test_un_changement_de_langue_s_applique_sans_redemarrage(
    hass: HomeAssistant, integration_backup: None, fournisseur_factice: str
) -> None:
    """Passer l'instance en français traduit la notification suivante.

    Le cache de traductions n'est pas encore chargé pour la nouvelle langue :
    la notification le charge une fois, puis s'affiche traduite, sans perdre le
    compte des échecs consécutifs.
    """
    await _charger(hass, "en", config_factice())
    await _echec(hass)
    assert _notification_d_echec(hass)["title"] == TITRE_ANGLAIS

    hass.config.language = "fr"
    await _echec(hass)

    notification = _notification_d_echec(hass)
    assert notification["title"] == TITRE_FRANCAIS
    assert "Échecs consécutifs vers cette destination : 2." in notification["message"]


async def test_le_chargement_asynchrone_des_traductions_est_bien_emprunte(
    hass: HomeAssistant,
    integration_backup: None,
    fournisseur_factice: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Preuve directe, indépendante de l'ordre des tests, que
    `charger_puis_creer` (notifications.py ~180-186) s'exécute et traduit.

    `pytest-cov` rapporte ces lignes comme non couvertes sur `uv run pytest`
    (suite complète). La cause n'est pas les tâches `eager_start` de Python
    3.14 : c'est `pytest_homeassistant_custom_component`, dont la fixture
    `translations_once` (portée session) partage un seul cache de traductions
    entre **tous** les tests, pour ne lire les fichiers qu'une fois.
    `auto_backup` étant une intégration réelle (non simulée par un test), ce
    cache n'est jamais purgé pour elle entre modules : dès qu'un test,
    n'importe où dans la suite, a demandé une langue pour ce domaine, elle
    reste chaude pour tous les tests suivants, qui empruntent alors le chemin
    synchrone. Un test qui compterait sur un cache réellement froid serait
    donc fragile face à l'ordre d'exécution — on le vérifie en le faisant
    passer seul, où il passe, puis juste après
    `test_un_changement_de_langue_s_applique_sans_redemarrage`, où le cache
    déjà chaud le fait échouer.

    Ce test contourne le problème en forçant l'absence de cache
    (`_textes_en_cache` retourne toujours `None`) plutôt qu'en essayant de la
    provoquer : le chemin asynchrone est pris de façon déterministe, quel que
    soit l'état du cache partagé, et un espion sur `async_get_translations`
    (son seul point d'entrée) en apporte la preuve observable.
    """
    monkeypatch.setattr(notifications, "_textes_en_cache", lambda hass: None)

    appels: list[tuple[Any, ...]] = []
    original = traductions.async_get_translations

    async def espion(*args: Any, **kwargs: Any) -> dict[str, str]:
        appels.append(args)
        return await original(*args, **kwargs)

    monkeypatch.setattr(traductions, "async_get_translations", espion)

    await _charger(hass, "fr", config_factice())
    await _echec(hass)

    assert appels == [(hass, "fr", traductions.CATEGORIE_DE_TRADUCTION, {DOMAIN})], (
        "async_get_translations n'a pas été appelé : le chemin asynchrone n'a pas couru"
    )
    assert _notification_d_echec(hass)["title"] == TITRE_FRANCAIS


### Course pendant le chargement asynchrone ###


def _chargement_retarde(monkeypatch: pytest.MonkeyPatch) -> asyncio.Event:
    """Force le chemin asynchrone et retient le chargement jusqu'au signal.

    `_textes_en_cache` est forcé à `None` : le cache de traductions est partagé
    entre tous les tests par `pytest_homeassistant_custom_component`, et le
    provoquer froid serait fragile face à l'ordre d'exécution.
    """
    monkeypatch.setattr(notifications, "_textes_en_cache", lambda hass: None)
    attente = asyncio.Event()
    original = traductions.async_get_translations

    async def chargement_retarde(*args: Any, **kwargs: Any) -> dict[str, str]:
        await attente.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(traductions, "async_get_translations", chargement_retarde)
    return attente


def _notifications_affichees(hass: HomeAssistant) -> dict[str, Any]:
    return persistent_notification._async_get_or_create_notifications(hass)


async def test_un_succes_pendant_le_chargement_ne_laisse_aucune_notification(
    hass: HomeAssistant,
    integration_backup: None,
    fournisseur_factice: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Un succès survenu pendant le chargement des traductions l'emporte.

    L'échec lance le chargement des traductions ; le téléversement suivant vers
    la même destination réussit avant la fin de ce chargement. Une fois les
    traductions chargées, la notification n'est pas créée : elle décrirait une
    panne terminée, et plus aucun succès ne viendrait la retirer.
    """
    attente = _chargement_retarde(monkeypatch)
    await _charger(hass, "fr", config_factice())
    identifiant = identifiant_de_notification_d_echec(DESTINATION)

    hass.bus.async_fire(
        EVENT_UPLOAD_FAILED,
        {
            ATTR_NAME: NOM_DE_LA_SAUVEGARDE,
            ATTR_SLUG: "abcd1234",
            ATTR_DESTINATION: DESTINATION,
            ATTR_DESTINATION_NAME: NOM_DE_LA_DESTINATION,
            ATTR_ERROR: "quota exceeded",
        },
    )
    assert identifiant not in _notifications_affichees(hass)

    hass.bus.async_fire(EVENT_UPLOAD_SUCCESSFUL, {ATTR_DESTINATION: DESTINATION})

    attente.set()
    await hass.async_block_till_done()

    assert identifiant not in _notifications_affichees(hass)


async def test_un_echec_plus_recent_affiche_son_propre_compteur(
    hass: HomeAssistant,
    integration_backup: None,
    fournisseur_factice: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deux échecs pendant le chargement : seule la notification du second reste."""
    attente = _chargement_retarde(monkeypatch)
    await _charger(hass, "fr", config_factice())

    hass.bus.async_fire(
        EVENT_UPLOAD_FAILED,
        {ATTR_DESTINATION: DESTINATION, ATTR_DESTINATION_NAME: NOM_DE_LA_DESTINATION},
    )
    hass.bus.async_fire(
        EVENT_UPLOAD_FAILED,
        {ATTR_DESTINATION: DESTINATION, ATTR_DESTINATION_NAME: NOM_DE_LA_DESTINATION},
    )
    attente.set()
    await hass.async_block_till_done()

    message = _notification_d_echec(hass)["message"]
    assert "Échecs consécutifs vers cette destination : 2." in message


async def test_une_reautorisation_pendant_le_chargement_ne_laisse_aucune_notification(
    hass: HomeAssistant,
    integration_backup: None,
    fournisseur_oauth_factice: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cas symétrique : la ré-autorisation aboutit avant la fin du chargement."""
    attente = _chargement_retarde(monkeypatch)
    await _charger(hass, "fr", config_oauth_factice())
    identifiant = identifiant_de_notification_de_reauthentification("destination_oauth")

    async_signaler_la_reauthentification(
        hass, DestinationConfig.from_dict(config_oauth_factice())
    )
    assert identifiant not in _notifications_affichees(hass)

    async_effacer_la_reauthentification(hass, "destination_oauth")

    attente.set()
    await hass.async_block_till_done()

    assert identifiant not in _notifications_affichees(hass)


async def test_une_reautorisation_toujours_requise_est_notifiee_apres_chargement(
    hass: HomeAssistant,
    integration_backup: None,
    fournisseur_oauth_factice: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Témoin : sans résolution entre-temps, la notification est bien créée."""
    attente = _chargement_retarde(monkeypatch)
    await _charger(hass, "fr", config_oauth_factice())

    async_signaler_la_reauthentification(
        hass, DestinationConfig.from_dict(config_oauth_factice())
    )
    attente.set()
    await hass.async_block_till_done()

    notification = _notification(
        hass, identifiant_de_notification_de_reauthentification("destination_oauth")
    )
    assert "doit être ré-autorisée" in notification["title"]


### Chargement des traductions en échec ###


async def test_un_chargement_en_echec_cree_quand_meme_la_notification(
    hass: HomeAssistant,
    integration_backup: None,
    fournisseur_factice: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Sans traduction chargeable ni cache, la notification existe malgré tout.

    Le repli affiche les clés : c'est laid, mais la panne reste signalée, sous
    son identifiant stable. Le journal ne cite que le type de l'exception.
    """
    await _charger(hass, "fr", config_factice())
    monkeypatch.setattr(notifications, "_textes_en_cache", lambda hass: None)
    monkeypatch.setattr(traductions, "async_get_cached_translations", lambda *args: {})

    async def chargement_en_echec(*args: Any, **kwargs: Any) -> dict[str, str]:
        raise OSError("lecture impossible de /config/secret_token=abc")

    monkeypatch.setattr(traductions, "async_get_translations", chargement_en_echec)

    await _echec(hass)

    notification = _notification_d_echec(hass)
    assert notification["notification_id"] == "auto_backup_upload_destination_test"
    assert notification["title"] == notifications.CLE_TITRE_ECHEC
    assert "OSError" in caplog.text
    assert "secret_token" not in caplog.text


async def test_un_chargement_en_echec_retombe_sur_l_anglais_en_cache(
    hass: HomeAssistant,
    integration_backup: None,
    fournisseur_factice: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Si l'anglais est en cache, le repli l'affiche plutôt que les clés."""
    await _charger(hass, "en", config_factice())
    hass.config.language = "fr"
    monkeypatch.setattr(notifications, "_textes_en_cache", lambda hass: None)

    async def chargement_en_echec(*args: Any, **kwargs: Any) -> dict[str, str]:
        raise OSError

    monkeypatch.setattr(traductions, "async_get_translations", chargement_en_echec)

    await _echec(hass)

    assert _notification_d_echec(hass)["title"] == TITRE_ANGLAIS


### Ré-autorisation ###


@pytest.mark.parametrize(
    ("langue", "titre", "consigne", "ecran"),
    [
        (
            "en",
            "Auto Backup: destination “Destination OAuth” must be re-authorized",
            "choose “Re-authorize a destination”, then “Destination OAuth”",
            "Settings → System → Repairs",
        ),
        (
            "fr",
            "Auto Backup : la destination « Destination OAuth » doit être ré-autorisée",
            "choisissez « Ré-autoriser une destination », puis « Destination OAuth »",
            "Paramètres → Système → Réparations",
        ),
        (
            "es",
            "Auto Backup: destination “Destination OAuth” must be re-authorized",
            "choose “Re-authorize a destination”, then “Destination OAuth”",
            "Settings → System → Repairs",
        ),
    ],
)
async def test_la_notification_de_reautorisation_est_traduite(
    hass: HomeAssistant,
    integration_backup: None,
    fournisseur_oauth_factice: str,
    langue: str,
    titre: str,
    consigne: str,
    ecran: str,
) -> None:
    """Critères : anglais, français et repli anglais pour la ré-autorisation.

    Le message désigne l'écran où Home Assistant affiche le problème (#55).
    """
    await _charger(hass, langue, config_oauth_factice())

    async_signaler_la_reauthentification(
        hass, DestinationConfig.from_dict(config_oauth_factice())
    )
    await hass.async_block_till_done()

    notification = _notification(
        hass, identifiant_de_notification_de_reauthentification("destination_oauth")
    )
    assert notification["title"] == titre
    assert consigne in notification["message"]
    assert ecran in notification["message"]


### Composition des textes ###


def test_un_texte_introuvable_affiche_sa_cle() -> None:
    """Un fichier de traduction abîmé ne fait pas échouer la notification."""
    assert _texte({}, CLE_TITRE_ECHEC, destination="x") == CLE_TITRE_ECHEC


def test_un_placeholder_inconnu_laisse_le_modele_tel_quel() -> None:
    """Un placeholder que le code ne fournit pas n'interrompt rien."""
    textes = {_chemin(CLE_MESSAGE_ECHEC): "Backup {inconnu}"}

    assert _texte(textes, CLE_MESSAGE_ECHEC, cause="x") == "Backup {inconnu}"
