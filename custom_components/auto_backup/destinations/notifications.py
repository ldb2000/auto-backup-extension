"""Notifications persistantes des échecs de destination (issue #17).

Une sauvegarde cloud silencieusement cassée donne une fausse impression de
sécurité : l'événement `auto_backup.upload_failed` et la ligne d'erreur du
journal ne sont vus que par qui les cherche. Ce module les rend visibles dans
Home Assistant, sans noyer l'utilisateur sous les notifications.

Trois règles gouvernent l'ensemble :

1. **Un identifiant de notification stable par destination**
   (`auto_backup_upload_<destination_id>`, `auto_backup_reauth_<destination_id>`).
   Des échecs successifs mettent donc la même notification à jour — en indiquant
   le nombre d'échecs consécutifs — au lieu d'en empiler une par sauvegarde, et
   le premier succès vers cette destination la retire.
2. **Jamais deux signalements pour la même cause.** Un accès révoqué est déjà
   signalé par un problème Home Assistant (« repair issue »,
   `destinations/reauth.py`) ; la notification qui l'accompagne le complète sans
   le doubler, et l'échec de téléversement qui en découle ne crée **pas** de
   seconde notification. Les deux disparaissent ensemble, à la ré-autorisation
   comme à la suppression de la destination.
3. **Aucun secret dans une notification.** Tout ce qui vient d'un fournisseur —
   la cause d'un échec, au premier chef — traverse `masquer_les_secrets()` avant
   d'être affiché : jeton porteur, valeur de `access_token` / `refresh_token` et
   chemins de fichiers absolus sont remplacés par `***`.

L'option `notify_on_failure` (vraie par défaut, réglable dans les options de
l'intégration) coupe les notifications persistantes, et elles seules : les
événements, les journaux d'erreur et le problème Home Assistant restent émis.

Les textes sont écrits en français directement ici : une notification
persistante n'a pas de clé de traduction côté Home Assistant, contrairement aux
problèmes et aux étapes du flux d'options.
"""

from __future__ import annotations

import logging
import re

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_NAME
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback

from ..const import (
    ATTR_DESTINATION,
    ATTR_DESTINATION_NAME,
    ATTR_ERROR,
    ATTR_SLUG,
    CONF_NOTIFY_ON_FAILURE,
    DATA_DESTINATIONS,
    DATA_NOTIFICATIONS,
    DEFAULT_NOTIFY_ON_FAILURE,
    EVENT_UPLOAD_FAILED,
    EVENT_UPLOAD_SUCCESSFUL,
    IDENTIFIANT_PROVISOIRE,
    NOTIFICATION_REAUTH_PREFIX,
    NOTIFICATION_UPLOAD_PREFIX,
)
from .errors import UnknownProviderError
from .models import VALEUR_MASQUEE, DestinationConfig
from .registry import provider_label

_LOGGER = logging.getLogger(__name__)

### Masquage défensif ###

# Jeton porteur d'un en-tête `Authorization`, tel qu'un fournisseur peut le
# recopier dans son message d'erreur.
_MOTIF_PORTEUR = re.compile(r"\bBearer\s+\S+", re.IGNORECASE)

# `access_token=...`, `"refresh_token": "..."`, `client_secret=...`, `api_key=...` :
# la clé est conservée (elle aide à comprendre ce qui a échoué), la valeur non.
_MOTIF_SECRET = re.compile(
    r"\b([\w.-]*(?:token|secret|password|api[_-]?key)[\w.-]*)[\"']?\s*[:=]\s*"
    r"[\"']?[^\s,;\"'}\])]+",
    re.IGNORECASE,
)

# Chemins de fichiers absolus : `/config/...` d'une instance Home Assistant, mais
# aussi les autres racines usuelles d'un conteneur ou d'un Supervisor. Le
# préfixe négatif évite de mutiler le chemin d'une URL (`https://hôte/config`),
# qui n'est pas un chemin local.
_MOTIF_CHEMIN = re.compile(
    r"(?<![\w.~/-])/(?:config|backup|backups|share|media|ssl|addons|addon_configs"
    r"|data|root|home|tmp|var|usr|etc|opt|srv|mnt|Users)(?:/[^\s,;\"'()<>»]*)*"
)


def masquer_les_secrets(texte: str) -> str:
    """Remplace par `***` ce qu'une notification ne doit jamais montrer.

    Le masquage est **défensif** : il porte sur le message d'un fournisseur, que
    le fork ne contrôle pas. Les messages du fork, eux, ne citent déjà aucun
    secret (cf. `docs/adr/0001-destinations-distantes.md`). Mieux vaut masquer
    un mot de trop qu'afficher un jeton dans une notification que l'utilisateur
    recopiera dans un ticket d'assistance.
    """
    masque = _MOTIF_PORTEUR.sub(f"Bearer {VALEUR_MASQUEE}", texte)
    masque = _MOTIF_SECRET.sub(rf"\1={VALEUR_MASQUEE}", masque)
    return _MOTIF_CHEMIN.sub(VALEUR_MASQUEE, masque)


### Identifiants de notification ###


@callback
def identifiant_de_notification_d_echec(destination_id: str) -> str:
    """Identifiant de la notification d'échec de téléversement d'une destination."""
    return f"{NOTIFICATION_UPLOAD_PREFIX}{destination_id}"


@callback
def identifiant_de_notification_de_reauthentification(destination_id: str) -> str:
    """Identifiant de la notification de ré-authentification d'une destination."""
    return f"{NOTIFICATION_REAUTH_PREFIX}{destination_id}"


### Textes ###

TITRE_ECHEC = "Auto Backup : échec d'envoi vers « {destination} »"

MESSAGE_ECHEC = (
    "La sauvegarde « {sauvegarde} » n'a pas pu être envoyée vers la destination "
    "« {destination} ».\n\n"
    "**Cause** : {cause}\n\n"
    "Échecs consécutifs vers cette destination : {echecs}.\n\n"
    "La sauvegarde locale, elle, est intacte. Cette notification disparaîtra "
    "d'elle-même dès qu'un envoi vers cette destination aboutira."
)

TITRE_REAUTH = "Auto Backup : la destination « {destination} » doit être ré-autorisée"

MESSAGE_REAUTH = (
    "Le fournisseur « {fournisseur} » a refusé de renouveler l'accès de la "
    "destination « {destination} » : l'autorisation a expiré ou a été révoquée. "
    "Plus aucune sauvegarde ne lui sera envoyée ; les autres destinations "
    "continuent de fonctionner.\n\n"
    "Pour la remettre en service, ouvrez les options de l'intégration Auto Backup, "
    "choisissez « Ré-autoriser une destination », puis « {destination} ».\n\n"
    "Cette notification et le problème signalé dans l'interface des intégrations "
    "disparaîtront ensemble, à la ré-autorisation comme à la suppression de la "
    "destination."
)


def _libelle_du_fournisseur(provider: str) -> str:
    """Libellé lisible du fournisseur, ou son identifiant technique à défaut."""
    try:
        return provider_label(provider)
    except UnknownProviderError:
        return provider


### Gestionnaire ###


class GestionnaireDeNotifications:
    """Traduit les événements de téléversement en notifications persistantes.

    Le compteur d'échecs consécutifs vit ici, et non dans la notification :
    c'est lui qui distingue une panne passagère d'une destination durablement
    cassée. Il est remis à zéro par un succès, par une ré-autorisation et par la
    suppression de la destination.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Mémorise l'instance et l'entrée dont les options sont relues."""
        self._hass = hass
        self._entry = entry
        self._echecs: dict[str, int] = {}

    @property
    def notifications_actives(self) -> bool:
        """Indique si l'option `notify_on_failure` autorise les notifications.

        L'option est relue dans l'entrée à chaque usage : la changer s'applique
        sans redémarrage, comme le délai de téléversement (#8).
        """
        return bool(
            self._entry.options.get(CONF_NOTIFY_ON_FAILURE, DEFAULT_NOTIFY_ON_FAILURE)
        )

    @callback
    def echecs_consecutifs(self, destination_id: str) -> int:
        """Nombre d'échecs consécutifs comptés pour cette destination."""
        return self._echecs.get(destination_id, 0)

    @callback
    def async_oublier(self, destination_id: str) -> None:
        """Remet le compteur d'échecs de cette destination à zéro."""
        self._echecs.pop(destination_id, None)

    @callback
    def async_echec_de_televersement(self, event: Event) -> None:
        """Signale un téléversement définitivement en échec.

        L'événement n'est émis qu'une fois les tentatives épuisées (#8) : une
        notification ici décrit donc bien un échec définitif, pas une tentative.
        """
        destination_id = event.data.get(ATTR_DESTINATION)
        if not destination_id:
            return

        echecs = self._echecs.get(destination_id, 0) + 1
        self._echecs[destination_id] = echecs

        if self._reauthentification_requise(destination_id):
            # L'accès a été révoqué : la notification de ré-authentification dit
            # déjà quoi faire, et elle seule est montrée. Une seconde
            # notification répéterait la même panne sans rien apprendre.
            _LOGGER.debug(
                "Échec de téléversement vers « %s » non notifié : la destination "
                "attend déjà une ré-autorisation",
                destination_id,
            )
            return

        if not self.notifications_actives:
            _LOGGER.debug(
                "Notifications désactivées (%s) : l'échec vers « %s » reste "
                "journalisé et signalé par son événement",
                CONF_NOTIFY_ON_FAILURE,
                destination_id,
            )
            return

        destination = masquer_les_secrets(
            str(event.data.get(ATTR_DESTINATION_NAME) or destination_id)
        )
        sauvegarde = masquer_les_secrets(
            str(event.data.get(ATTR_NAME) or event.data.get(ATTR_SLUG) or "sans nom")
        )
        cause = masquer_les_secrets(str(event.data.get(ATTR_ERROR) or "cause inconnue"))

        persistent_notification.async_create(
            self._hass,
            MESSAGE_ECHEC.format(
                sauvegarde=sauvegarde,
                destination=destination,
                cause=cause,
                echecs=echecs,
            ),
            title=TITRE_ECHEC.format(destination=destination),
            notification_id=identifiant_de_notification_d_echec(destination_id),
        )

    @callback
    def async_televersement_reussi(self, event: Event) -> None:
        """Retire la notification d'échec d'une destination qui refonctionne.

        Le retrait a lieu même si l'option a été désactivée entre-temps : une
        notification déjà affichée ne doit pas survivre à la panne qu'elle
        décrit.
        """
        destination_id = event.data.get(ATTR_DESTINATION)
        if not destination_id:
            return
        if self._echecs.pop(destination_id, 0):
            _LOGGER.debug(
                "La destination « %s » refonctionne : notification d'échec retirée",
                destination_id,
            )
        persistent_notification.async_dismiss(
            self._hass, identifiant_de_notification_d_echec(destination_id)
        )

    def _reauthentification_requise(self, destination_id: str) -> bool:
        """Indique si cette destination attend déjà une nouvelle autorisation."""
        gestionnaire = self._hass.data.get(DATA_DESTINATIONS)
        return gestionnaire is not None and gestionnaire.reauthentification_requise(
            destination_id
        )


### Branchement sur l'entrée de configuration ###


@callback
def async_setup_notifications(
    hass: HomeAssistant, entry: ConfigEntry
) -> GestionnaireDeNotifications:
    """Installe les notifications persistantes pour une entrée de configuration.

    Les notifications déjà affichées **survivent** au déchargement de l'entrée :
    un simple rechargement de l'intégration ne doit pas effacer l'alerte d'une
    sauvegarde qui n'est jamais partie.
    """
    gestionnaire = GestionnaireDeNotifications(hass, entry)
    hass.data[DATA_NOTIFICATIONS] = gestionnaire

    ecouteurs: list[CALLBACK_TYPE] = [
        hass.bus.async_listen(
            EVENT_UPLOAD_FAILED, gestionnaire.async_echec_de_televersement
        ),
        hass.bus.async_listen(
            EVENT_UPLOAD_SUCCESSFUL, gestionnaire.async_televersement_reussi
        ),
    ]
    for ecouteur in ecouteurs:
        entry.async_on_unload(ecouteur)

    @callback
    def retirer_le_gestionnaire() -> None:
        """Retire le gestionnaire de `hass.data` au déchargement de l'entrée."""
        hass.data.pop(DATA_NOTIFICATIONS, None)

    entry.async_on_unload(retirer_le_gestionnaire)
    return gestionnaire


### Ré-authentification (appelé par `destinations/reauth.py`) ###


@callback
def async_notifier_la_reauthentification(
    hass: HomeAssistant, config: DestinationConfig
) -> None:
    """Invite l'utilisateur à ré-autoriser une destination dont l'accès est perdu.

    La notification **complète** le problème Home Assistant créé au même moment
    (`destinations/reauth.py`) : le problème signale la destination dans
    l'interface des intégrations, la notification la porte à l'écran d'accueil.
    Elle remplace du même coup une éventuelle notification d'échec de
    téléversement pour cette destination : la cause est connue, et la marche à
    suivre est unique.

    La destination fictive du flux d'ajout (`IDENTIFIANT_PROVISOIRE`) est
    ignorée, comme pour l'avertissement du journal : l'utilisateur est
    justement en train d'autoriser.
    """
    if config.destination_id == IDENTIFIANT_PROVISOIRE:
        return

    persistent_notification.async_dismiss(
        hass, identifiant_de_notification_d_echec(config.destination_id)
    )

    gestionnaire = hass.data.get(DATA_NOTIFICATIONS)
    if gestionnaire is None:
        # L'entrée n'est pas (ou plus) chargée : le problème Home Assistant, lui,
        # a été créé et suffit à signaler la destination.
        return
    gestionnaire.async_oublier(config.destination_id)
    if not gestionnaire.notifications_actives:
        return

    destination = masquer_les_secrets(config.name)
    persistent_notification.async_create(
        hass,
        MESSAGE_REAUTH.format(
            destination=destination,
            fournisseur=_libelle_du_fournisseur(config.provider),
        ),
        title=TITRE_REAUTH.format(destination=destination),
        notification_id=identifiant_de_notification_de_reauthentification(
            config.destination_id
        ),
    )


@callback
def async_effacer_les_notifications(hass: HomeAssistant, destination_id: str) -> None:
    """Retire les notifications d'une destination ré-autorisée ou supprimée.

    Les deux notifications partent ensemble, et avec le problème Home Assistant
    que `destinations/reauth.py` supprime au même moment : une destination
    remise en service ne doit rien laisser derrière elle, et une destination
    supprimée encore moins — l'utilisateur n'aurait plus aucun moyen de faire
    disparaître un signalement qui la nomme.
    """
    persistent_notification.async_dismiss(
        hass, identifiant_de_notification_d_echec(destination_id)
    )
    persistent_notification.async_dismiss(
        hass, identifiant_de_notification_de_reauthentification(destination_id)
    )
    gestionnaire = hass.data.get(DATA_NOTIFICATIONS)
    if gestionnaire is not None:
        gestionnaire.async_oublier(destination_id)
