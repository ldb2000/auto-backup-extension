"""Notifications persistantes des échecs de destination (issue #17).

Ces tests partent des **événements publics** du fork
(`auto_backup.upload_failed`, `auto_backup.upload_successful`) et du signalement
de ré-authentification (`destinations/reauth.py`) : ils éprouvent ce que
l'utilisateur voit — une notification par destination, mise à jour et non
dupliquée, retirée d'elle-même — sans dépendre de la mécanique de téléversement,
couverte par `tests/test_televersement.py`.

Aucune valeur réelle n'est employée : les jetons et les chemins cités dans les
causes d'échec sont inventés, et servent justement à vérifier qu'ils
n'apparaissent **pas** dans les notifications.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from homeassistant.components import persistent_notification
from homeassistant.const import ATTR_NAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)

from custom_components.auto_backup.const import (
    ATTR_DESTINATION,
    ATTR_DESTINATION_NAME,
    ATTR_ERROR,
    ATTR_SLUG,
    CONF_AUTO_PURGE,
    CONF_BACKUP_TIMEOUT,
    CONF_DESTINATIONS,
    CONF_NOTIFY_ON_FAILURE,
    DATA_DESTINATIONS,
    DATA_NOTIFICATIONS,
    DATA_UPLOADS,
    DEFAULT_BACKUP_TIMEOUT,
    DOMAIN,
    EVENT_UPLOAD_FAILED,
    EVENT_UPLOAD_SUCCESSFUL,
)
from custom_components.auto_backup.destinations import (
    DestinationConfig,
    async_effacer_la_reauthentification,
    async_signaler_la_reauthentification,
    identifiant_du_probleme,
)
from custom_components.auto_backup.destinations.notifications import (
    identifiant_de_notification_d_echec,
    identifiant_de_notification_de_reauthentification,
    masquer_les_secrets,
)
from destinations_factices import PROVIDER_FACTICE, config_factice, config_oauth_factice

DESTINATION = "destination_test"
NOM_DE_LA_DESTINATION = "Destination de test"
NOM_DE_LA_SAUVEGARDE = "Sauvegarde du 25/09/2026"
SLUG = "abcd1234"

type OuvrirLesOptions = Callable[[str, str], Awaitable[dict[str, Any]]]


@pytest.fixture
async def entree_notifiante(
    hass: HomeAssistant, integration_backup: None, fournisseur_factice: str
) -> MockConfigEntry:
    """Entrée chargée portant une destination factice, notifications actives."""
    entree = MockConfigEntry(
        domain=DOMAIN,
        title="Auto Backup",
        data={},
        options={
            CONF_AUTO_PURGE: True,
            CONF_BACKUP_TIMEOUT: DEFAULT_BACKUP_TIMEOUT,
            CONF_DESTINATIONS: [config_factice()],
        },
    )
    entree.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entree.entry_id)
    await hass.async_block_till_done()
    return entree


def _notifications(hass: HomeAssistant) -> dict[str, Any]:
    """Notifications persistantes actuellement affichées.

    Home Assistant ne les expose qu'au travers de son API WebSocket ; le stock
    lui-même est un simple dictionnaire de `hass.data`, que les tests du cœur
    lisent par cet accesseur.
    """
    return persistent_notification._async_get_or_create_notifications(hass)


def _notification_d_echec(hass: HomeAssistant, destination_id: str = DESTINATION):
    """Notification d'échec de téléversement d'une destination, si elle existe."""
    return _notifications(hass).get(identifiant_de_notification_d_echec(destination_id))


def _notification_de_reauth(hass: HomeAssistant, destination_id: str):
    """Notification de ré-authentification d'une destination, si elle existe."""
    return _notifications(hass).get(
        identifiant_de_notification_de_reauthentification(destination_id)
    )


async def _echec(
    hass: HomeAssistant,
    *,
    cause: str = "le fournisseur a refusé l'envoi",
    nom: str = NOM_DE_LA_SAUVEGARDE,
    destination_id: str = DESTINATION,
    destination_nom: str = NOM_DE_LA_DESTINATION,
) -> None:
    """Émet l'événement d'échec définitif, comme le fait `upload.py` (#8)."""
    hass.bus.async_fire(
        EVENT_UPLOAD_FAILED,
        {
            ATTR_NAME: nom,
            ATTR_SLUG: SLUG,
            ATTR_DESTINATION: destination_id,
            ATTR_DESTINATION_NAME: destination_nom,
            ATTR_ERROR: cause,
        },
    )
    await hass.async_block_till_done()


async def _succes(hass: HomeAssistant, *, destination_id: str = DESTINATION) -> None:
    """Émet l'événement de téléversement réussi vers une destination."""
    hass.bus.async_fire(
        EVENT_UPLOAD_SUCCESSFUL,
        {
            ATTR_NAME: NOM_DE_LA_SAUVEGARDE,
            ATTR_SLUG: SLUG,
            ATTR_DESTINATION: destination_id,
            ATTR_DESTINATION_NAME: NOM_DE_LA_DESTINATION,
        },
    )
    await hass.async_block_till_done()


### Échec de téléversement ###


async def test_un_echec_definitif_cree_une_notification(
    hass: HomeAssistant, entree_notifiante: MockConfigEntry
) -> None:
    """Critère : la notification nomme la destination, la sauvegarde et la cause."""
    await _echec(hass, cause="quota du compte dépassé")

    notification = _notification_d_echec(hass)
    assert notification is not None
    assert notification["notification_id"] == "auto_backup_upload_destination_test"
    assert NOM_DE_LA_DESTINATION in notification["title"]
    assert NOM_DE_LA_SAUVEGARDE in notification["message"]
    assert NOM_DE_LA_DESTINATION in notification["message"]
    assert "quota du compte dépassé" in notification["message"]


async def test_des_echecs_successifs_mettent_la_notification_a_jour(
    hass: HomeAssistant, entree_notifiante: MockConfigEntry
) -> None:
    """Critère : même identifiant, aucune pile de notifications, un compteur."""
    await _echec(hass, cause="première panne")
    await _echec(hass, cause="deuxième panne")
    await _echec(hass, cause="troisième panne")

    affichees = [
        identifiant
        for identifiant in _notifications(hass)
        if identifiant.startswith("auto_backup_upload_")
    ]
    assert affichees == ["auto_backup_upload_destination_test"]

    notification = _notification_d_echec(hass)
    assert notification is not None
    assert "troisième panne" in notification["message"]
    assert "3" in notification["message"]
    assert hass.data[DATA_NOTIFICATIONS].echecs_consecutifs(DESTINATION) == 3


async def test_chaque_destination_a_sa_propre_notification(
    hass: HomeAssistant, entree_notifiante: MockConfigEntry
) -> None:
    """Une panne sur une destination n'écrase pas l'alerte d'une autre."""
    await _echec(hass, cause="panne ici")
    await _echec(
        hass,
        destination_id="autre_destination",
        destination_nom="Autre destination",
        cause="panne ailleurs",
    )

    assert _notification_d_echec(hass) is not None
    assert _notification_d_echec(hass, "autre_destination") is not None


async def test_un_televersement_reussi_retire_la_notification(
    hass: HomeAssistant, entree_notifiante: MockConfigEntry
) -> None:
    """Critère : le premier succès vers la destination efface l'alerte."""
    await _echec(hass)
    assert _notification_d_echec(hass) is not None

    await _succes(hass)

    assert _notification_d_echec(hass) is None
    assert hass.data[DATA_NOTIFICATIONS].echecs_consecutifs(DESTINATION) == 0


async def test_le_compteur_repart_de_un_apres_un_succes(
    hass: HomeAssistant, entree_notifiante: MockConfigEntry
) -> None:
    """« Consécutifs » veut bien dire consécutifs : un succès remet à zéro."""
    await _echec(hass)
    await _echec(hass)
    await _succes(hass)
    await _echec(hass)

    notification = _notification_d_echec(hass)
    assert notification is not None
    assert "cette destination : 1" in notification["message"]


async def test_un_succes_vers_une_autre_destination_ne_retire_rien(
    hass: HomeAssistant, entree_notifiante: MockConfigEntry
) -> None:
    """Le retrait est ciblé : une autre destination n'efface pas l'alerte."""
    await _echec(hass)

    await _succes(hass, destination_id="autre_destination")

    assert _notification_d_echec(hass) is not None


async def test_un_succes_sans_echec_prealable_ne_cree_rien(
    hass: HomeAssistant,
    entree_notifiante: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Un succès isolé, sans échec préalable, ne crée rien et ne plante pas.

    Rien ne distingue cette destination d'une autre qui n'aurait jamais connu
    de panne : son compteur d'échecs est déjà à zéro, et le premier événement
    qu'elle reçoit est un succès.
    """
    assert hass.data[DATA_NOTIFICATIONS].echecs_consecutifs(DESTINATION) == 0

    with caplog.at_level(logging.ERROR):
        await _succes(hass)

    assert _notification_d_echec(hass) is None
    assert hass.data[DATA_NOTIFICATIONS].echecs_consecutifs(DESTINATION) == 0
    assert not caplog.records
    assert not [
        identifiant
        for identifiant in _notifications(hass)
        if identifiant.startswith("auto_backup_")
    ]


async def test_un_evenement_sans_destination_est_ignore(
    hass: HomeAssistant, entree_notifiante: MockConfigEntry
) -> None:
    """Un événement incomplet ne crée pas de notification orpheline."""
    hass.bus.async_fire(EVENT_UPLOAD_FAILED, {ATTR_NAME: NOM_DE_LA_SAUVEGARDE})
    hass.bus.async_fire(EVENT_UPLOAD_SUCCESSFUL, {ATTR_NAME: NOM_DE_LA_SAUVEGARDE})
    await hass.async_block_till_done()

    assert not [
        identifiant
        for identifiant in _notifications(hass)
        if identifiant.startswith("auto_backup_")
    ]


async def test_une_sauvegarde_sans_nom_reste_notifiable(
    hass: HomeAssistant, entree_notifiante: MockConfigEntry
) -> None:
    """Sans nom, le slug identifie la sauvegarde ; la notification est créée."""
    hass.bus.async_fire(
        EVENT_UPLOAD_FAILED,
        {
            ATTR_SLUG: SLUG,
            ATTR_DESTINATION: DESTINATION,
            ATTR_DESTINATION_NAME: NOM_DE_LA_DESTINATION,
            ATTR_ERROR: "panne",
        },
    )
    await hass.async_block_till_done()

    notification = _notification_d_echec(hass)
    assert notification is not None
    assert SLUG in notification["message"]


async def test_un_echec_puis_la_suppression_de_la_destination_retire_l_alerte(
    hass: HomeAssistant,
    entree_notifiante: MockConfigEntry,
    ouvrir_les_options: OuvrirLesOptions,
) -> None:
    """Supprimer une destination en échec retire aussi son alerte.

    À la différence de `test_la_suppression_d_une_destination_efface_sa_notification`
    (destination déjà signalée en ré-authentification, pour laquelle l'échec ne
    crée justement **pas** de notification), cette destination n'a jamais eu de
    problème d'autorisation : sa notification d'échec existe bel et bien avant
    la suppression, et c'est elle que la suppression doit retirer.
    """
    await _echec(hass)
    assert _notification_d_echec(hass) is not None

    resultat = await ouvrir_les_options(
        entree_notifiante.entry_id, "supprimer_destination"
    )
    resultat = await hass.config_entries.options.async_configure(
        resultat["flow_id"], {CONF_DESTINATIONS: [DESTINATION]}
    )
    await hass.async_block_till_done()

    assert resultat["type"] is FlowResultType.CREATE_ENTRY
    assert _notification_d_echec(hass) is None


### Option `notify_on_failure` ###


async def test_l_option_desactivee_supprime_la_notification_pas_l_alerte(
    hass: HomeAssistant,
    integration_backup: None,
    fournisseur_factice: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Critère : sans notification, l'événement et le journal restent émis.

    L'échec est déclenché par le coordinateur de téléversement lui-même, et non
    par un événement forgé : c'est la seule façon de prouver que couper les
    notifications ne coupe ni l'événement ni la ligne d'erreur du journal.
    """
    entree = MockConfigEntry(
        domain=DOMAIN,
        title="Auto Backup",
        data={},
        options={
            CONF_AUTO_PURGE: True,
            CONF_BACKUP_TIMEOUT: DEFAULT_BACKUP_TIMEOUT,
            CONF_DESTINATIONS: [config_factice()],
            CONF_NOTIFY_ON_FAILURE: False,
        },
    )
    entree.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entree.entry_id)
    await hass.async_block_till_done()

    evenements = async_capture_events(hass, EVENT_UPLOAD_FAILED)
    with caplog.at_level(logging.ERROR):
        hass.data[DATA_UPLOADS]._async_signaler_echec(
            DESTINATION,
            NOM_DE_LA_DESTINATION,
            NOM_DE_LA_SAUVEGARDE,
            SLUG,
            "quota du compte dépassé",
        )
        await hass.async_block_till_done()

    assert _notification_d_echec(hass) is None
    assert len(evenements) == 1
    assert evenements[0].data[ATTR_ERROR] == "quota du compte dépassé"
    assert "quota du compte dépassé" in caplog.text


async def test_l_option_reactivee_s_applique_sans_redemarrage(
    hass: HomeAssistant, entree_notifiante: MockConfigEntry
) -> None:
    """L'option est relue dans l'entrée à chaque échec, comme `upload_timeout`."""
    hass.config_entries.async_update_entry(
        entree_notifiante,
        options={**entree_notifiante.options, CONF_NOTIFY_ON_FAILURE: False},
    )
    await hass.async_block_till_done()
    await _echec(hass)
    assert _notification_d_echec(hass) is None

    hass.config_entries.async_update_entry(
        entree_notifiante,
        options={**entree_notifiante.options, CONF_NOTIFY_ON_FAILURE: True},
    )
    await hass.async_block_till_done()
    await _echec(hass)

    assert _notification_d_echec(hass) is not None


async def test_un_succes_retire_la_notification_meme_option_coupee(
    hass: HomeAssistant, entree_notifiante: MockConfigEntry
) -> None:
    """Une alerte affichée ne doit pas survivre à la panne qu'elle décrit."""
    await _echec(hass)
    hass.config_entries.async_update_entry(
        entree_notifiante,
        options={**entree_notifiante.options, CONF_NOTIFY_ON_FAILURE: False},
    )
    await hass.async_block_till_done()

    await _succes(hass)

    assert _notification_d_echec(hass) is None


### Réglage depuis l'interface ###


def _valeur_proposee(resultat: dict[str, Any], cle: str) -> Any:
    """Valeur pré-remplie par le formulaire pour ce champ."""
    for marqueur in resultat["data_schema"].schema:
        if marqueur == cle:
            return (marqueur.description or {}).get("suggested_value")
    raise AssertionError(f"champ « {cle} » absent du formulaire")


async def _regler_les_notifications(
    hass: HomeAssistant,
    entree: MockConfigEntry,
    ouvrir_les_options: OuvrirLesOptions,
    actives: bool,
) -> dict[str, Any]:
    """Ouvre les réglages des notifications et soumet ce choix."""
    resultat = await ouvrir_les_options(entree.entry_id, "reglages_notifications")
    assert resultat["step_id"] == "reglages_notifications"

    resultat = await hass.config_entries.options.async_configure(
        resultat["flow_id"], user_input={CONF_NOTIFY_ON_FAILURE: actives}
    )
    await hass.async_block_till_done()
    return resultat


async def test_le_formulaire_propose_l_etat_en_vigueur(
    hass: HomeAssistant,
    entree_notifiante: MockConfigEntry,
    ouvrir_les_options: OuvrirLesOptions,
) -> None:
    """Le champ est pré-rempli : actif par défaut, puis valeur enregistrée."""
    resultat = await ouvrir_les_options(
        entree_notifiante.entry_id, "reglages_notifications"
    )
    assert resultat["type"] is FlowResultType.FORM
    assert _valeur_proposee(resultat, CONF_NOTIFY_ON_FAILURE) is True

    await _regler_les_notifications(
        hass, entree_notifiante, ouvrir_les_options, actives=False
    )

    resultat = await ouvrir_les_options(
        entree_notifiante.entry_id, "reglages_notifications"
    )
    assert _valeur_proposee(resultat, CONF_NOTIFY_ON_FAILURE) is False


async def test_le_reglage_de_l_interface_coupe_les_notifications(
    hass: HomeAssistant,
    entree_notifiante: MockConfigEntry,
    ouvrir_les_options: OuvrirLesOptions,
) -> None:
    """Critère : désactivée dans les options, plus aucune notification.

    Les destinations configurées, elles, survivent à l'enregistrement : le
    réglage passe par `options_avec_reglage()`, comme le délai de téléversement.
    """
    resultat = await _regler_les_notifications(
        hass, entree_notifiante, ouvrir_les_options, actives=False
    )
    assert resultat["type"] is FlowResultType.CREATE_ENTRY
    assert entree_notifiante.options[CONF_NOTIFY_ON_FAILURE] is False
    assert entree_notifiante.options[CONF_DESTINATIONS] == [config_factice()]

    await _echec(hass)

    assert _notification_d_echec(hass) is None
    assert hass.data[DATA_NOTIFICATIONS].notifications_actives is False


async def test_le_reglage_de_l_interface_les_retablit(
    hass: HomeAssistant,
    entree_notifiante: MockConfigEntry,
    ouvrir_les_options: OuvrirLesOptions,
) -> None:
    """Réactivées, les notifications repartent sans redémarrage."""
    await _regler_les_notifications(
        hass, entree_notifiante, ouvrir_les_options, actives=False
    )
    await _regler_les_notifications(
        hass, entree_notifiante, ouvrir_les_options, actives=True
    )

    await _echec(hass)

    assert _notification_d_echec(hass) is not None


### Ré-authentification ###


@pytest.fixture
async def entree_oauth(
    hass: HomeAssistant, integration_backup: None, fournisseur_oauth_factice: str
) -> MockConfigEntry:
    """Entrée chargée portant une destination autorisée en OAuth2."""
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


def _config_oauth() -> DestinationConfig:
    """Configuration de la destination OAuth2 factice."""
    return DestinationConfig.from_dict(config_oauth_factice())


async def test_un_acces_revoque_signale_le_probleme_et_la_notification(
    hass: HomeAssistant, entree_oauth: MockConfigEntry
) -> None:
    """Critère : le problème reste, et une notification invite à ré-autoriser."""
    async_signaler_la_reauthentification(hass, _config_oauth())
    await hass.async_block_till_done()

    probleme = ir.async_get(hass).async_get_issue(
        DOMAIN, identifiant_du_probleme("destination_oauth")
    )
    assert probleme is not None

    notification = _notification_de_reauth(hass, "destination_oauth")
    assert notification is not None
    assert notification["notification_id"] == "auto_backup_reauth_destination_oauth"
    assert "Destination OAuth" in notification["title"]
    assert "Ré-autoriser une destination" in notification["message"]


async def test_la_reautorisation_efface_le_probleme_et_la_notification(
    hass: HomeAssistant, entree_oauth: MockConfigEntry
) -> None:
    """Critère : les deux signalements disparaissent ensemble."""
    async_signaler_la_reauthentification(hass, _config_oauth())
    await hass.async_block_till_done()

    async_effacer_la_reauthentification(hass, "destination_oauth")
    await hass.async_block_till_done()

    assert (
        ir.async_get(hass).async_get_issue(
            DOMAIN, identifiant_du_probleme("destination_oauth")
        )
        is None
    )
    assert _notification_de_reauth(hass, "destination_oauth") is None


async def test_la_suppression_d_une_destination_efface_sa_notification(
    hass: HomeAssistant,
    entree_oauth: MockConfigEntry,
    ouvrir_les_options: OuvrirLesOptions,
) -> None:
    """Critère : supprimer la destination retire aussi ses signalements.

    Le parcours passe par l'interface, comme l'utilisateur : une notification
    qui survivrait à la destination qu'elle nomme serait impossible à faire
    disparaître.
    """
    async_signaler_la_reauthentification(hass, _config_oauth())
    await _echec(hass, destination_id="destination_oauth")
    assert _notification_de_reauth(hass, "destination_oauth") is not None

    resultat = await ouvrir_les_options(entree_oauth.entry_id, "supprimer_destination")
    resultat = await hass.config_entries.options.async_configure(
        resultat["flow_id"], {CONF_DESTINATIONS: ["destination_oauth"]}
    )
    await hass.async_block_till_done()

    assert resultat["type"] is FlowResultType.CREATE_ENTRY
    assert _notification_de_reauth(hass, "destination_oauth") is None
    assert _notification_d_echec(hass, "destination_oauth") is None


async def test_un_echec_d_authentification_ne_produit_qu_une_notification(
    hass: HomeAssistant, entree_oauth: MockConfigEntry
) -> None:
    """Critère : pas de doublon quand l'échec vient de l'accès révoqué.

    L'ordre est celui de la production : la session signale la
    ré-authentification (#7), puis le téléversement épuisé émet son événement
    d'échec (#8).
    """
    async_signaler_la_reauthentification(hass, _config_oauth())
    await hass.async_block_till_done()

    await _echec(
        hass,
        destination_id="destination_oauth",
        destination_nom="Destination OAuth",
        cause="ré-authentification requise",
    )

    assert _notification_de_reauth(hass, "destination_oauth") is not None
    assert _notification_d_echec(hass, "destination_oauth") is None


async def test_une_notification_d_echec_cede_la_place_a_la_reauthentification(
    hass: HomeAssistant, entree_oauth: MockConfigEntry
) -> None:
    """L'ordre inverse ne laisse pas davantage deux notifications."""
    await _echec(hass, destination_id="destination_oauth")
    assert _notification_d_echec(hass, "destination_oauth") is not None

    async_signaler_la_reauthentification(hass, _config_oauth())
    await hass.async_block_till_done()

    assert _notification_d_echec(hass, "destination_oauth") is None
    assert _notification_de_reauth(hass, "destination_oauth") is not None


async def test_la_destination_provisoire_ne_notifie_jamais(
    hass: HomeAssistant, entree_oauth: MockConfigEntry
) -> None:
    """Autoriser une destination qui n'existe pas encore n'alerte personne."""
    provisoire = DestinationConfig.from_dict(
        config_oauth_factice(
            destination_id="autorisation_en_cours", name="Autorisation en cours"
        )
    )

    async_signaler_la_reauthentification(hass, provisoire)
    await hass.async_block_till_done()

    assert _notification_de_reauth(hass, "autorisation_en_cours") is None


async def test_un_fournisseur_inconnu_garde_son_identifiant_technique(
    hass: HomeAssistant, entree_oauth: MockConfigEntry
) -> None:
    """Un fournisseur retiré du registre reste nommé, faute de mieux.

    C'est le cas d'une destination dont le fournisseur n'est plus livré : la
    notification doit quand même dire de quoi elle parle.
    """
    config = DestinationConfig.from_dict(
        config_oauth_factice(
            destination_id="destination_orpheline",
            name="Destination orpheline",
            provider="jamais_installe",
        )
    )

    async_signaler_la_reauthentification(hass, config)
    await hass.async_block_till_done()

    notification = _notification_de_reauth(hass, "destination_orpheline")
    assert notification is not None
    assert "jamais_installe" in notification["message"]


async def test_l_option_desactivee_laisse_le_probleme_home_assistant(
    hass: HomeAssistant,
    integration_backup: None,
    fournisseur_oauth_factice: str,
) -> None:
    """Critère : l'option ne coupe que les notifications persistantes."""
    entree = MockConfigEntry(
        domain=DOMAIN,
        title="Auto Backup",
        data={},
        options={
            CONF_DESTINATIONS: [config_oauth_factice()],
            CONF_NOTIFY_ON_FAILURE: False,
        },
    )
    entree.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entree.entry_id)
    await hass.async_block_till_done()

    async_signaler_la_reauthentification(hass, _config_oauth())
    await hass.async_block_till_done()

    assert (
        ir.async_get(hass).async_get_issue(
            DOMAIN, identifiant_du_probleme("destination_oauth")
        )
        is not None
    )
    assert hass.data[DATA_DESTINATIONS].reauthentification_requise("destination_oauth")
    assert _notification_de_reauth(hass, "destination_oauth") is None


### Masquage des secrets ###

JETON_FACTICE = "acces-factice-1"
RAFRAICHISSEMENT_FACTICE = "rafraichissement-factice-1"
CHEMIN_FACTICE = "/config/backups/abcd1234.tar"

CAUSE_BAVARDE = (
    f"le fournisseur a refusé l'en-tête « Bearer {JETON_FACTICE} » "
    f'(refresh_token={RAFRAICHISSEMENT_FACTICE}, "access_token": "{JETON_FACTICE}") '
    f"en lisant {CHEMIN_FACTICE}"
)


async def test_aucun_jeton_ni_chemin_dans_une_notification(
    hass: HomeAssistant, entree_notifiante: MockConfigEntry
) -> None:
    """Critère : ni jeton, ni secret, ni chemin de fichier dans l'affichage."""
    await _echec(hass, cause=CAUSE_BAVARDE)

    notification = _notification_d_echec(hass)
    assert notification is not None
    affiche = f"{notification['title']}\n{notification['message']}"
    assert JETON_FACTICE not in affiche
    assert RAFRAICHISSEMENT_FACTICE not in affiche
    assert CHEMIN_FACTICE not in affiche
    assert "***" in affiche
    # La cause reste compréhensible : seules les valeurs sont masquées.
    assert "le fournisseur a refusé l'en-tête" in affiche


@pytest.mark.parametrize(
    ("brut", "attendu"),
    [
        ("Bearer acces-factice-1", "Bearer ***"),
        ("bearer acces-factice-1", "Bearer ***"),
        ("access_token=acces-factice-1", "access_token=***"),
        ('"refresh_token": "rafraichissement-factice-1"', "refresh_token=***"),
        ("client_secret = secret-application-factice", "client_secret=***"),
        ("api_key: cle-factice", "api_key=***"),
        ("fichier /config/backups/ha.tar illisible", "fichier *** illisible"),
        ("fichier /backup/abcd1234.tar absent", "fichier *** absent"),
        ("quota dépassé", "quota dépassé"),
        ("délai de téléversement dépassé (1800 s)", "délai de téléversement dépassé"),
    ],
)
def test_le_masquage_couvre_les_formes_usuelles(brut: str, attendu: str) -> None:
    """Le masquage garde la clé, jamais la valeur, et laisse le reste lisible."""
    assert attendu in masquer_les_secrets(brut)


def test_le_masquage_epargne_les_url_des_fournisseurs() -> None:
    """Une URL n'est pas un chemin local : elle reste lisible dans la cause."""
    masque = masquer_les_secrets(
        "appel refusé par https://fournisseur.test/config/v3/about"
    )

    assert masque == "appel refusé par https://fournisseur.test/config/v3/about"


def test_le_masquage_protege_aussi_le_nom_d_une_destination(
    hass: HomeAssistant,
) -> None:
    """Le masquage est défensif : il porte sur tout ce qui est affiché."""
    assert masquer_les_secrets("Dropbox /config/secret") == "Dropbox ***"


### Cycle de vie ###


async def test_les_notifications_survivent_a_un_rechargement(
    hass: HomeAssistant, entree_notifiante: MockConfigEntry
) -> None:
    """Recharger l'intégration n'efface pas une alerte encore d'actualité."""
    await _echec(hass)

    assert await hass.config_entries.async_reload(entree_notifiante.entry_id)
    await hass.async_block_till_done()

    assert _notification_d_echec(hass) is not None
    # Le gestionnaire est bien reconstruit, et le compteur repart de zéro.
    assert hass.data[DATA_NOTIFICATIONS].echecs_consecutifs(DESTINATION) == 0


async def test_les_evenements_ne_sont_plus_ecoutes_apres_dechargement(
    hass: HomeAssistant, entree_notifiante: MockConfigEntry
) -> None:
    """L'entrée déchargée ne crée plus rien : les écouteurs sont retirés."""
    assert await hass.config_entries.async_unload(entree_notifiante.entry_id)
    await hass.async_block_till_done()

    await _echec(hass)

    assert _notification_d_echec(hass) is None
    assert DATA_NOTIFICATIONS not in hass.data


async def test_sans_entree_chargee_le_signalement_reste_silencieux(
    hass: HomeAssistant, integration_backup: None, fournisseur_factice: str
) -> None:
    """Sans gestionnaire, aucune notification n'est créée (ni aucune erreur)."""
    config = DestinationConfig.from_dict(config_factice(provider=PROVIDER_FACTICE))

    async_signaler_la_reauthentification(hass, config)
    await hass.async_block_till_done()

    assert _notification_de_reauth(hass, DESTINATION) is None
