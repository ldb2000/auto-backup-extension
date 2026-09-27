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
   la cause d'un échec, au premier chef — traverse `masquer()` avant d'être
   affiché. Les deux **noms** affichés, celui de la destination et celui de la
   sauvegarde, passent par `masquer_un_nom()` : mêmes passes, sauf le dernier
   filet des suites opaques, qui réduisait à `***` tout nom de vingt caractères
   ou plus sans espace (« Dropbox-Compte-Familial »). Un nom est de la
   configuration du fork, déjà affichée en clair par le problème Home Assistant
   et par le menu des options : le masquer ne protégeait rien et empêchait
   l'utilisateur de savoir laquelle de ses destinations avait lâché. Le masquage
   ne vit pas ici : `destinations/masquage.py` est le point unique du fork, et
   son en-tête décrit les passes comme les réserves assumées.

L'option `notify_on_failure` (vraie par défaut, réglable dans les options de
l'intégration) coupe les notifications persistantes, et elles seules : les
événements, les journaux d'erreur et le problème Home Assistant restent émis.

Les textes sont traduits (#45) : titres et messages vivent dans la section
`exceptions` de `translations/*.json`, sous les clés `notification_*`, et sont
lus dans la langue de Home Assistant au moment de la notification. Une
notification persistante n'a pas de catégorie de traduction propre ; la section
`exceptions` est la seule que `hassfest` accepte pour un texte libre avec
placeholders (`{destination}`), et le cache de traductions de Home Assistant y
donne accès sans relire les fichiers. Une langue sans traduction retombe sur
l'anglais, comme partout ailleurs dans Home Assistant. Le masquage s'applique
aux **valeurs** des placeholders, jamais au texte traduit.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_NAME
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers.translation import (
    async_get_cached_translations,
    async_get_translations,
)

from ..const import (
    ATTR_DESTINATION,
    ATTR_DESTINATION_NAME,
    ATTR_ERROR,
    ATTR_SLUG,
    CONF_NOTIFY_ON_FAILURE,
    DATA_DESTINATIONS,
    DATA_NOTIFICATIONS,
    DEFAULT_NOTIFY_ON_FAILURE,
    DOMAIN,
    EVENT_UPLOAD_FAILED,
    EVENT_UPLOAD_SUCCESSFUL,
    IDENTIFIANT_PROVISOIRE,
    NOTIFICATION_REAUTH_PREFIX,
    NOTIFICATION_UPLOAD_PREFIX,
)
from .errors import UnknownProviderError
from .masquage import masquer, masquer_un_nom
from .models import DestinationConfig
from .registry import provider_label

_LOGGER = logging.getLogger(__name__)

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

# Catégorie de traduction des textes de notification : voir l'en-tête du module.
CATEGORIE_DE_TRADUCTION = "exceptions"

CLE_TITRE_ECHEC = "notification_echec_titre"
CLE_MESSAGE_ECHEC = "notification_echec_message"
CLE_TITRE_REAUTH = "notification_reauth_titre"
CLE_MESSAGE_REAUTH = "notification_reauth_message"
CLE_SAUVEGARDE_SANS_NOM = "notification_sauvegarde_sans_nom"
CLE_CAUSE_INCONNUE = "notification_cause_inconnue"

CLES_DE_TRADUCTION = (
    CLE_TITRE_ECHEC,
    CLE_MESSAGE_ECHEC,
    CLE_TITRE_REAUTH,
    CLE_MESSAGE_REAUTH,
    CLE_SAUVEGARDE_SANS_NOM,
    CLE_CAUSE_INCONNUE,
)


def _chemin(cle: str) -> str:
    """Clé aplatie d'un texte dans le cache de traductions de Home Assistant."""
    return f"component.{DOMAIN}.{CATEGORIE_DE_TRADUCTION}.{cle}.message"


def _textes_complets(textes: dict[str, str]) -> bool:
    """Indique si toutes les clés de notification sont présentes."""
    return all(_chemin(cle) in textes for cle in CLES_DE_TRADUCTION)


@callback
def _textes_en_cache(hass: HomeAssistant) -> dict[str, str] | None:
    """Textes déjà chargés pour la langue courante, ou `None` s'ils manquent.

    Home Assistant charge les traductions d'une intégration à son installation,
    puis à chaque changement de langue : le cache suffit presque toujours, et
    aucune notification ne relit les fichiers.
    """
    textes = async_get_cached_translations(
        hass, hass.config.language, CATEGORIE_DE_TRADUCTION, DOMAIN
    )
    return textes if _textes_complets(textes) else None


def _texte(textes: dict[str, str], cle: str, **placeholders: object) -> str:
    """Texte traduit d'une clé, placeholders remplacés.

    Un texte introuvable (fichier de traduction abîmé) affiche sa clé plutôt
    que de faire échouer la notification.
    """
    modele = textes.get(_chemin(cle))
    if modele is None:
        _LOGGER.warning("Traduction « %s » introuvable", cle)
        return cle
    try:
        return modele.format(**placeholders)
    except KeyError, IndexError, ValueError:
        _LOGGER.warning("Placeholders invalides dans la traduction « %s »", cle)
        return modele


@callback
def _async_creer_la_notification(
    hass: HomeAssistant,
    notification_id: str,
    composer: Callable[[dict[str, str]], tuple[str, str]],
    toujours_d_actualite: Callable[[], bool],
) -> None:
    """Crée une notification dont `composer` écrit le titre et le message.

    Cas courant : les textes sont en cache, la notification est créée
    immédiatement. Sinon (langue changée à l'instant, cache pas encore
    rechargé), les traductions sont chargées une fois — avec repli anglais —
    puis la notification est créée. Les valeurs affichées (compteur, noms
    masqués) sont figées avant ce chargement.

    Deux garanties encadrent ce chargement :

    - `toujours_d_actualite` est réévalué **après** l'attente : si la panne a
      été résolue entre-temps (succès du téléversement, ré-autorisation), la
      notification n'est pas créée — elle décrirait une panne terminée, que plus
      rien ne viendrait retirer ;
    - un chargement en échec ne fait pas perdre la notification : elle est
      créée avec les textes anglais déjà en cache, ou à défaut avec les clés.
    """
    if (textes := _textes_en_cache(hass)) is not None:
        _creer(hass, notification_id, composer(textes))
        return

    async def charger_puis_creer() -> None:
        try:
            textes = await async_get_translations(
                hass, hass.config.language, CATEGORIE_DE_TRADUCTION, {DOMAIN}
            )
        except Exception as err:  # la notification prime sur sa traduction
            _LOGGER.warning(
                "Chargement des traductions des notifications impossible (%s) : "
                "repli sur l'anglais en cache",
                type(err).__name__,
            )
            textes = async_get_cached_translations(
                hass, "en", CATEGORIE_DE_TRADUCTION, DOMAIN
            )
            if not _textes_complets(textes):
                textes = {}
        if not toujours_d_actualite():
            _LOGGER.debug(
                "Notification « %s » abandonnée : la panne a été résolue pendant "
                "le chargement des traductions",
                notification_id,
            )
            return
        _creer(hass, notification_id, composer(textes))

    hass.async_create_task(charger_puis_creer(), eager_start=True)


def _creer(hass: HomeAssistant, notification_id: str, textes: tuple[str, str]) -> None:
    """Crée ou met à jour la notification persistante d'identifiant donné."""
    titre, message = textes
    persistent_notification.async_create(
        hass, message, title=titre, notification_id=notification_id
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

        # Les noms gardent leurs passes 1 à 5 mais pas le dernier filet : ce
        # sont des noms du fork, et l'utilisateur doit pouvoir les lire pour
        # savoir quelle destination a lâché. Seule la cause, texte du
        # fournisseur, traverse le masquage entier. Le masquage porte sur les
        # valeurs des placeholders, jamais sur le texte traduit.
        destination = masquer_un_nom(
            str(event.data.get(ATTR_DESTINATION_NAME) or destination_id)
        )
        nom_de_sauvegarde = event.data.get(ATTR_NAME) or event.data.get(ATTR_SLUG)
        erreur = event.data.get(ATTR_ERROR)

        def composer(textes: dict[str, str]) -> tuple[str, str]:
            sauvegarde = masquer_un_nom(
                str(nom_de_sauvegarde or _texte(textes, CLE_SAUVEGARDE_SANS_NOM))
            )
            cause = masquer(str(erreur or _texte(textes, CLE_CAUSE_INCONNUE)))
            return (
                _texte(textes, CLE_TITRE_ECHEC, destination=destination),
                _texte(
                    textes,
                    CLE_MESSAGE_ECHEC,
                    sauvegarde=sauvegarde,
                    destination=destination,
                    cause=cause,
                    echecs=echecs,
                ),
            )

        _async_creer_la_notification(
            self._hass,
            identifiant_de_notification_d_echec(destination_id),
            composer,
            # Un succès remet le compteur à zéro ; un échec plus récent crée sa
            # propre notification, avec son propre compteur.
            lambda: self._echecs.get(destination_id) == echecs,
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

    # Le problème Home Assistant créé au même moment affiche ce nom en clair
    # (`destinations/reauth.py`) : la notification ne gagnerait rien à le réduire
    # à `***`, elle perdrait seulement de l'information.
    destination = masquer_un_nom(config.name)
    fournisseur = _libelle_du_fournisseur(config.provider)

    def composer(textes: dict[str, str]) -> tuple[str, str]:
        return (
            _texte(textes, CLE_TITRE_REAUTH, destination=destination),
            _texte(
                textes,
                CLE_MESSAGE_REAUTH,
                destination=destination,
                fournisseur=fournisseur,
            ),
        )

    def toujours_a_reautoriser() -> bool:
        """La destination attend-elle encore une nouvelle autorisation ?"""
        destinations = hass.data.get(DATA_DESTINATIONS)
        return destinations is not None and destinations.reauthentification_requise(
            config.destination_id
        )

    _async_creer_la_notification(
        hass,
        identifiant_de_notification_de_reauthentification(config.destination_id),
        composer,
        toujours_a_reautoriser,
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
