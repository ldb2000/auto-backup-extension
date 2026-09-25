"""Entités d'état des destinations distantes (issue #16).

L'upstream expose des capteurs sur les sauvegardes **locales** ; ce module fait
de même pour chaque destination **distante** configurée, afin qu'un
téléversement qui échoue depuis des semaines se voie depuis un tableau de bord
et puisse déclencher une automatisation.

Chaque destination reçoit trois entités, rattachées au device de service
« Auto Backup » de l'upstream (mêmes `identifiers`, cf. `helpers.get_device_info()`) :

| Entité | Domaine | Rôle |
| --- | --- | --- |
| `dernier_televersement` | `sensor` | horodatage du dernier téléversement réussi |
| `sauvegardes_distantes` | `sensor` | nombre de sauvegardes chez le fournisseur |
| `probleme` | `binary_sensor` | PROBLEM : la dernière tentative a échoué |

Trois choix structurants :

1. **Aucun module upstream n'est récrit.** `sensor.py` et `binary_sensor.py`
   se contentent d'appeler `async_setup_destination_sensors()` et
   `async_setup_destination_binary_sensors()` à la fin de leur
   `async_setup_entry()` : deux lignes ajoutées, aucune supprimée
   (cf. `docs/UPSTREAM.md`).

2. **Un état par destination, partagé par ses trois entités.**
   `CoordinateurEntitesDestinations` écoute **une seule fois** les événements
   `auto_backup.upload_successful`, `auto_backup.upload_failed` et
   `auto_backup.remote_purge`, met à jour l'`EtatDestination` concerné, puis
   prévient les entités de cette destination par un signal de dispatcher. Les
   entités restent de simples vues : elles ne s'abonnent pas au bus.

3. **Le nombre de sauvegardes distantes est un compteur, pas un inventaire.**
   Tant que la rétention distante (#9) ne tient pas de registre persistant, le
   compteur s'incrémente sur `upload_successful` et se décrémente sur
   `remote_purge`, et il est restauré au redémarrage par `RestoreSensor`. Dès
   que #9 sait répondre, elle enregistre sa source par
   `async_enregistrer_source_des_comptes()` : le capteur lui donne alors la
   priorité, sans que rien ne change ici.

Les messages d'erreur exposés en attribut passent tous par
`assainir_le_message()`, qui masque les jetons et les secrets susceptibles
d'apparaître dans une réponse de fournisseur : un attribut d'entité est lisible
par toute personne ayant accès à l'instance, et il est journalisé par
l'enregistreur.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_ON, Platform
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from ..const import (
    ATTR_DELETED,
    ATTR_DESTINATION,
    ATTR_ERROR,
    ATTR_LAST_ERROR,
    ATTR_LAST_FAILED_AT,
    ATTR_LAST_FAILED_SLUG,
    ATTR_REMAINING,
    ATTR_SLUG,
    DATA_DESTINATION_ENTITIES,
    DATA_DESTINATIONS,
    DOMAIN,
    EVENT_REMOTE_PURGE,
    EVENT_UPLOAD_FAILED,
    EVENT_UPLOAD_SUCCESSFUL,
)
from ..helpers import get_device_info
from .models import VALEUR_MASQUEE, DestinationConfig

_LOGGER = logging.getLogger(__name__)

# Suffixes d'identifiant unique. La forme complète est
# `<entry_id>_<destination_id>_<suffixe>` : l'`entry_id` évite toute collision
# si une seconde entrée voyait le jour, et le `destination_id` distingue les
# destinations entre elles (critère 4 de l'issue #16).
SUFFIXE_DERNIER_TELEVERSEMENT = "dernier_televersement"
SUFFIXE_SAUVEGARDES_DISTANTES = "sauvegardes_distantes"
SUFFIXE_PROBLEME = "probleme"

# Longueur maximale d'un message d'erreur exposé en attribut : un attribut
# d'entité est recopié dans chaque état historisé par l'enregistreur, et une
# trace de pile complète n'a rien à y faire.
LONGUEUR_MAX_ERREUR = 255

# Message de repli quand un échec est signalé sans cause exploitable.
ERREUR_INCONNUE = "cause inconnue"

# Clés dont la valeur est masquée dans un message d'erreur. Les plus longues
# viennent en premier : l'alternative d'une expression régulière est évaluée de
# gauche à droite, et `token` ne doit pas l'emporter sur `refresh_token`.
_CLES_SENSIBLES = (
    "refresh_token",
    "access_token",
    "id_token",
    "client_secret",
    "client_id",
    "api_key",
    "apikey",
    "authorization",
    "password",
    "secret",
    "token",
    "code",
)

# « Bearer <jeton> », « Basic <identifiants> » : l'en-tête d'autorisation tel
# qu'un fournisseur le renvoie parfois dans son message d'erreur.
_MOTIF_PORTEUR = re.compile(r"(?i)\b(bearer|basic)\s+\S+")

# `access_token=...`, `"client_secret": "..."`, `token: ...` : la valeur est
# masquée, la clé conservée pour que le message reste diagnosticable.
_MOTIF_AFFECTATION = re.compile(
    r"(?i)(?P<cle>\b(?:" + "|".join(_CLES_SENSIBLES) + r")\b)"
    r"(?P<separateur>\"?\s*[:=]\s*)"
    r"(?P<valeur>\"[^\"]*\"|'[^']*'|[^\s,;&)\]}]+)"
)


@callback
def assainir_le_message(message: object) -> str:
    """Rend un message d'erreur publiable en attribut d'entité.

    Deux traitements, dans cet ordre :

    1. **masquage** des jetons et secrets — un fournisseur peut recopier la
       requête refusée, en-tête `Authorization` compris, dans son message
       d'erreur ; l'attribut est visible de toute personne ayant accès à
       l'instance et l'enregistreur le conserve ;
    2. **troncature** à `LONGUEUR_MAX_ERREUR` caractères, pour qu'un message
       verbeux n'alourdisse pas chaque état historisé.

    Le masquage est volontairement large : il vaut mieux masquer un code
    d'erreur HTTP (« code=500 » devient « code=*** ») que laisser fuir un jeton
    de rafraîchissement.
    """
    texte = (str(message).strip() if message is not None else "") or ERREUR_INCONNUE
    texte = _MOTIF_PORTEUR.sub(
        lambda trouve: f"{trouve.group(1)} {VALEUR_MASQUEE}", texte
    )
    texte = _MOTIF_AFFECTATION.sub(_masquer_l_affectation, texte)
    if len(texte) > LONGUEUR_MAX_ERREUR:
        texte = texte[: LONGUEUR_MAX_ERREUR - 1].rstrip() + "…"
    return texte


def _masquer_l_affectation(trouve: re.Match[str]) -> str:
    """Remplace la valeur d'une clé sensible, en gardant la forme du message."""
    valeur = trouve["valeur"]
    guillemet = valeur[0] if valeur[:1] in {'"', "'"} else ""
    return (
        f"{trouve['cle']}{trouve['separateur']}{guillemet}{VALEUR_MASQUEE}{guillemet}"
    )


@dataclass(slots=True)
class EtatDestination:
    """État courant d'une destination, partagé par ses trois entités.

    `derniere_erreur` porte l'erreur **active** : elle est effacée dès qu'un
    téléversement réussit, ce qui fait retomber le capteur binaire « problème ».
    `dernier_slug_echec` et `dernier_echec` gardent en revanche la trace du
    dernier échec connu, même après un succès : ils servent à comprendre *ce
    qui* avait échoué, pas à signaler un problème en cours.
    """

    dernier_succes: datetime | None = None
    sauvegardes_distantes: int = 0
    derniere_erreur: str | None = None
    dernier_slug_echec: str | None = None
    dernier_echec: datetime | None = None


# Source faisant autorité sur le nombre de sauvegardes distantes : elle reçoit
# un identifiant de destination et renvoie un nombre, ou `None` si elle ne sait
# pas répondre pour cette destination. Prévue pour le registre de la rétention
# distante (issue #9).
SourceDesComptes = Callable[[str], int | None]

# Construit une entité pour une destination donnée.
FabriqueEntite = Callable[
    [ConfigEntry, "CoordinateurEntitesDestinations", DestinationConfig], Entity
]


@dataclass(frozen=True, slots=True)
class _Plateforme:
    """Une plateforme d'entités branchée sur le coordinateur."""

    async_add_entities: AddEntitiesCallback
    fabriques: tuple[tuple[str, FabriqueEntite], ...]


@callback
def signal_de_destination(entry_id: str, destination_id: str) -> str:
    """Signal de dispatcher rafraîchissant les entités d'une destination."""
    return f"{DOMAIN}_destination_maj_{entry_id}_{destination_id}"


@callback
def identifiant_unique(entry_id: str, destination_id: str, suffixe: str) -> str:
    """Identifiant unique d'une entité de destination (critère 4 de #16)."""
    return f"{entry_id}_{destination_id}_{suffixe}"


class CoordinateurEntitesDestinations:
    """Tient l'état des destinations et fait vivre leurs entités.

    Un seul coordinateur par entrée de configuration, exposé dans
    `hass.data[DATA_DESTINATION_ENTITIES]`. Il :

    - écoute les trois événements distants et met à jour les `EtatDestination` ;
    - prévient les entités concernées par un signal de dispatcher ;
    - suit les options de l'entrée pour créer les entités d'une destination
      ajoutée à chaud et retirer du registre celles d'une destination supprimée.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Prépare un coordinateur vide pour cette entrée de configuration."""
        self._hass = hass
        self._entry = entry
        self._etats: dict[str, EtatDestination] = {}
        self._connues: dict[str, DestinationConfig] = {}
        self._plateformes: dict[str, _Plateforme] = {}
        self._source_des_comptes: SourceDesComptes | None = None

    @property
    def entry_id(self) -> str:
        """Entrée de configuration suivie par ce coordinateur."""
        return self._entry.entry_id

    @property
    def destinations(self) -> list[DestinationConfig]:
        """Destinations actuellement dotées d'entités."""
        return list(self._connues.values())

    @callback
    def async_demarrer(self) -> None:
        """Branche les écoutes du coordinateur sur la vie de l'entrée."""
        self._recharger_les_destinations()

        for evenement, gestionnaire in (
            (EVENT_UPLOAD_SUCCESSFUL, self._async_televersement_reussi),
            (EVENT_UPLOAD_FAILED, self._async_televersement_echoue),
            (EVENT_REMOTE_PURGE, self._async_purge_distante),
        ):
            self._entry.async_on_unload(
                self._hass.bus.async_listen(evenement, gestionnaire)
            )

        self._entry.async_on_unload(
            self._entry.add_update_listener(self._async_options_mises_a_jour)
        )
        self._entry.async_on_unload(self._async_oublier)

    @callback
    def _async_oublier(self) -> None:
        """Retire le coordinateur de `hass.data` au déchargement de l'entrée."""
        if self._hass.data.get(DATA_DESTINATION_ENTITIES) is self:
            self._hass.data.pop(DATA_DESTINATION_ENTITIES, None)

    @callback
    def async_enregistrer_plateforme(
        self,
        domaine: str,
        async_add_entities: AddEntitiesCallback,
        fabriques: Sequence[tuple[str, FabriqueEntite]],
    ) -> None:
        """Déclare une plateforme et crée ses entités pour l'existant."""
        plateforme = _Plateforme(async_add_entities, tuple(fabriques))
        self._plateformes[domaine] = plateforme
        entites = self._entites(plateforme, self._connues.values())
        if entites:
            async_add_entities(entites)

    @callback
    def _entites(
        self, plateforme: _Plateforme, configs: Iterable[DestinationConfig]
    ) -> list[Entity]:
        """Construit les entités d'une plateforme pour ces destinations."""
        return [
            fabrique(self._entry, self, config)
            for config in configs
            for _, fabrique in plateforme.fabriques
        ]

    @callback
    def etat(self, destination_id: str) -> EtatDestination:
        """État d'une destination, créé à la volée s'il n'existe pas encore."""
        return self._etats.setdefault(destination_id, EtatDestination())

    @callback
    def nombre_de_sauvegardes(self, destination_id: str) -> int:
        """Nombre de sauvegardes distantes connu pour cette destination.

        La source enregistrée par `async_enregistrer_source_des_comptes()` fait
        autorité quand elle sait répondre ; sinon, le compteur tenu par les
        événements — et restauré au redémarrage — est utilisé.
        """
        if self._source_des_comptes is not None:
            try:
                nombre = self._source_des_comptes(destination_id)
            except Exception:  # une source défaillante ne casse pas le capteur
                _LOGGER.exception(
                    "La source du nombre de sauvegardes distantes a échoué pour "
                    "« %s » : repli sur le compteur interne",
                    destination_id,
                )
            else:
                if nombre is not None:
                    return max(int(nombre), 0)
        return self.etat(destination_id).sauvegardes_distantes

    @callback
    def async_definir_la_source_des_comptes(
        self, source: SourceDesComptes | None
    ) -> None:
        """Remplace la source faisant autorité sur le nombre de sauvegardes."""
        self._source_des_comptes = source
        for destination_id in self._connues:
            self._async_notifier(destination_id)

    @callback
    def _async_notifier(self, destination_id: str) -> None:
        """Demande aux entités de cette destination de réécrire leur état."""
        async_dispatcher_send(
            self._hass, signal_de_destination(self.entry_id, destination_id)
        )

    @callback
    def _recharger_les_destinations(self) -> None:
        """Relit les destinations chargées par le gestionnaire.

        La lecture passe par `hass.data[DATA_DESTINATIONS]` plutôt que par les
        options brutes : le gestionnaire est la seule source de vérité de ce qui
        est réellement utilisable (il écarte une configuration invalide ou un
        fournisseur inconnu), et il s'est déjà rechargé quand ce coordinateur
        est prévenu — son écouteur de mise à jour est enregistré par
        `async_setup_destinations()`, donc avant celui d'une plateforme.
        """
        gestionnaire = self._hass.data.get(DATA_DESTINATIONS)
        configs = list(gestionnaire.configs) if gestionnaire is not None else []
        self._connues = {config.destination_id: config for config in configs}

    @callback
    def _etat_suivi(self, event: Event) -> tuple[str, EtatDestination] | None:
        """Destination visée par un événement, si elle a bien des entités."""
        destination_id = event.data.get(ATTR_DESTINATION)
        if not isinstance(destination_id, str) or destination_id not in self._connues:
            return None
        return destination_id, self.etat(destination_id)

    @callback
    def _async_televersement_reussi(self, event: Event) -> None:
        """`upload_successful` : horodatage, compteur, erreur active effacée."""
        suivi = self._etat_suivi(event)
        if suivi is None:
            return
        destination_id, etat = suivi
        etat.dernier_succes = event.time_fired
        etat.sauvegardes_distantes += 1
        etat.derniere_erreur = None
        self._async_notifier(destination_id)

    @callback
    def _async_televersement_echoue(self, event: Event) -> None:
        """`upload_failed` : erreur active, slug et date du dernier échec."""
        suivi = self._etat_suivi(event)
        if suivi is None:
            return
        destination_id, etat = suivi
        etat.derniere_erreur = assainir_le_message(event.data.get(ATTR_ERROR))
        slug = event.data.get(ATTR_SLUG)
        etat.dernier_slug_echec = slug if isinstance(slug, str) else None
        etat.dernier_echec = event.time_fired
        self._async_notifier(destination_id)

    @callback
    def _async_purge_distante(self, event: Event) -> None:
        """`remote_purge` : le compteur suit les sauvegardes supprimées (#9).

        L'événement peut renseigner le nombre restant (`remaining`), qui fait
        alors autorité, ou le nombre de sauvegardes supprimées (`deleted`), qui
        est retranché. Sans l'un ni l'autre, le compteur est laissé tel quel :
        une purge qui n'a rien supprimé ne doit rien changer.
        """
        suivi = self._etat_suivi(event)
        if suivi is None:
            return
        destination_id, etat = suivi

        restantes = _nombre(event.data.get(ATTR_REMAINING))
        supprimees = _nombre(event.data.get(ATTR_DELETED))
        if restantes is not None:
            etat.sauvegardes_distantes = restantes
        elif supprimees:
            etat.sauvegardes_distantes = max(etat.sauvegardes_distantes - supprimees, 0)
        else:
            return
        self._async_notifier(destination_id)

    async def _async_options_mises_a_jour(
        self, hass: HomeAssistant, entry: ConfigEntry
    ) -> None:
        """Suit l'ajout et la suppression de destinations, sans rechargement."""
        anciennes = set(self._connues)
        self._recharger_les_destinations()
        nouvelles = set(self._connues)

        for destination_id in sorted(nouvelles - anciennes):
            self._async_ajouter(destination_id)
        for destination_id in sorted(anciennes - nouvelles):
            self._async_retirer(destination_id)

    @callback
    def _async_ajouter(self, destination_id: str) -> None:
        """Crée les entités d'une destination ajoutée à chaud."""
        config = self._connues[destination_id]
        _LOGGER.debug("Entités créées pour la destination « %s »", destination_id)
        for plateforme in self._plateformes.values():
            entites = self._entites(plateforme, (config,))
            if entites:
                plateforme.async_add_entities(entites)

    @callback
    def _async_retirer(self, destination_id: str) -> None:
        """Retire du registre les entités d'une destination supprimée.

        Le retrait passe par le registre d'entités : Home Assistant en déduit la
        suppression de l'entité elle-même, y compris lorsqu'elle était désactivée
        et n'avait donc jamais été instanciée.
        """
        self._etats.pop(destination_id, None)
        registre = er.async_get(self._hass)
        for domaine, plateforme in self._plateformes.items():
            for suffixe, _ in plateforme.fabriques:
                entity_id = registre.async_get_entity_id(
                    domaine,
                    DOMAIN,
                    identifiant_unique(self.entry_id, destination_id, suffixe),
                )
                if entity_id is not None:
                    registre.async_remove(entity_id)
        _LOGGER.debug("Entités retirées pour la destination « %s »", destination_id)


@callback
def _nombre(valeur: Any) -> int | None:
    """Convertit une donnée d'événement en nombre positif, ou `None`.

    Une liste est acceptée et vaut sa longueur : la rétention distante (#9)
    peut aussi bien annoncer un décompte que la liste des sauvegardes
    supprimées.
    """
    if isinstance(valeur, bool):
        return None
    if isinstance(valeur, list | tuple | set):
        return len(valeur)
    if isinstance(valeur, int | float):
        return max(int(valeur), 0)
    if isinstance(valeur, str) and valeur.strip().lstrip("+-").isdigit():
        return max(int(valeur.strip()), 0)
    return None


@callback
def _texte(valeur: Any) -> str | None:
    """Renvoie une chaîne non vide, ou `None`."""
    return valeur if isinstance(valeur, str) and valeur else None


class _EntiteDestination:
    """Socle commun aux entités d'une destination.

    Cette classe **n'hérite pas** d'`Entity` : les classes concrètes la placent
    en premier parent, devant leur classe Home Assistant (`RestoreSensor`,
    `RestoreEntity`). `async_added_to_hass()` passe donc ici d'abord, puis
    redescend la chaîne d'héritage normale.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        entry: ConfigEntry,
        coordinateur: CoordinateurEntitesDestinations,
        config: DestinationConfig,
        suffixe: str,
    ) -> None:
        """Rattache l'entité à sa destination et au device de service upstream."""
        self._coordinateur = coordinateur
        self._destination_id = config.destination_id
        self._signal = signal_de_destination(entry.entry_id, config.destination_id)
        self._attr_unique_id = identifiant_unique(
            entry.entry_id, config.destination_id, suffixe
        )
        # Même device que les entités upstream : les destinations ne créent pas
        # d'appareil supplémentaire, elles complètent celui de l'intégration.
        self._attr_device_info = get_device_info(entry)
        # Le nom traduit porte le nom de la destination, seul moyen de
        # distinguer les entités de deux destinations sur un device partagé.
        self._attr_translation_placeholders = {"destination": config.name}

    @property
    def _etat(self) -> EtatDestination:
        """État partagé de la destination décrite par cette entité."""
        return self._coordinateur.etat(self._destination_id)

    async def async_added_to_hass(self) -> None:
        """S'abonne au signal de rafraîchissement de sa destination."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(self.hass, self._signal, self._async_rafraichir)
        )

    @callback
    def _async_rafraichir(self) -> None:
        """Réécrit l'état de l'entité après une mise à jour du coordinateur."""
        self.async_write_ha_state()


class CapteurDernierTeleversement(_EntiteDestination, RestoreSensor):
    """Horodatage du dernier téléversement réussi vers cette destination."""

    entity_description = SensorEntityDescription(
        key=SUFFIXE_DERNIER_TELEVERSEMENT,
        translation_key="destination_dernier_televersement",
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:cloud-check-variant",
    )

    def __init__(
        self,
        entry: ConfigEntry,
        coordinateur: CoordinateurEntitesDestinations,
        config: DestinationConfig,
    ) -> None:
        """Construit le capteur d'horodatage d'une destination."""
        super().__init__(entry, coordinateur, config, SUFFIXE_DERNIER_TELEVERSEMENT)

    @property
    def native_value(self) -> datetime | None:
        """Date du dernier succès, ou `None` si aucun n'a encore eu lieu."""
        return self._etat.dernier_succes

    async def async_added_to_hass(self) -> None:
        """Restaure le dernier succès connu avant le redémarrage.

        Un succès enregistré entre le démarrage du coordinateur et l'ajout de
        cette entité fait foi : la valeur restaurée, plus ancienne, ne l'écrase
        pas.
        """
        await super().async_added_to_hass()
        donnees = await self.async_get_last_sensor_data()
        restaure = donnees.native_value if donnees is not None else None
        courant = self._etat.dernier_succes
        if isinstance(restaure, datetime) and (courant is None or restaure > courant):
            self._etat.dernier_succes = restaure


class CapteurSauvegardesDistantes(_EntiteDestination, RestoreSensor):
    """Nombre de sauvegardes présentes chez le fournisseur distant."""

    entity_description = SensorEntityDescription(
        key=SUFFIXE_SAUVEGARDES_DISTANTES,
        translation_key="destination_sauvegardes_distantes",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:cloud-upload-outline",
    )

    def __init__(
        self,
        entry: ConfigEntry,
        coordinateur: CoordinateurEntitesDestinations,
        config: DestinationConfig,
    ) -> None:
        """Construit le capteur de comptage d'une destination."""
        super().__init__(entry, coordinateur, config, SUFFIXE_SAUVEGARDES_DISTANTES)

    @property
    def native_value(self) -> int:
        """Nombre de sauvegardes distantes, source de #9 prioritaire."""
        return self._coordinateur.nombre_de_sauvegardes(self._destination_id)

    async def async_added_to_hass(self) -> None:
        """Restaure le compteur tel qu'il était avant le redémarrage.

        Un téléversement comptabilisé avant l'ajout de cette entité l'emporte :
        la valeur restaurée ne s'applique qu'à un compteur encore vierge.
        """
        await super().async_added_to_hass()
        donnees = await self.async_get_last_sensor_data()
        restaure = _nombre(donnees.native_value) if donnees is not None else None
        if restaure is not None and not self._etat.sauvegardes_distantes:
            self._etat.sauvegardes_distantes = restaure


class CapteurBinaireProblemeDestination(
    _EntiteDestination, RestoreEntity, BinarySensorEntity
):
    """Signale que le dernier téléversement vers cette destination a échoué."""

    entity_description = BinarySensorEntityDescription(
        key=SUFFIXE_PROBLEME,
        translation_key="destination_probleme",
        device_class=BinarySensorDeviceClass.PROBLEM,
    )

    def __init__(
        self,
        entry: ConfigEntry,
        coordinateur: CoordinateurEntitesDestinations,
        config: DestinationConfig,
    ) -> None:
        """Construit le capteur binaire « problème » d'une destination."""
        super().__init__(entry, coordinateur, config, SUFFIXE_PROBLEME)

    @property
    def is_on(self) -> bool:
        """Vrai tant qu'aucun téléversement n'a réussi depuis le dernier échec."""
        return self._etat.derniere_erreur is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Dernière erreur active, et trace du dernier échec connu."""
        etat = self._etat
        return {
            ATTR_LAST_ERROR: etat.derniere_erreur,
            ATTR_LAST_FAILED_SLUG: etat.dernier_slug_echec,
            ATTR_LAST_FAILED_AT: (
                etat.dernier_echec.isoformat() if etat.dernier_echec else None
            ),
        }

    async def async_added_to_hass(self) -> None:
        """Restaure l'erreur active et la trace du dernier échec."""
        await super().async_added_to_hass()
        dernier = await self.async_get_last_state()
        if dernier is None:
            return

        etat = self._etat
        if etat.derniere_erreur is None and dernier.state == STATE_ON:
            etat.derniere_erreur = (
                _texte(dernier.attributes.get(ATTR_LAST_ERROR)) or ERREUR_INCONNUE
            )
        if etat.dernier_slug_echec is None:
            etat.dernier_slug_echec = _texte(
                dernier.attributes.get(ATTR_LAST_FAILED_SLUG)
            )
        if etat.dernier_echec is None:
            horodatage = _texte(dernier.attributes.get(ATTR_LAST_FAILED_AT))
            etat.dernier_echec = (
                dt_util.parse_datetime(horodatage) if horodatage else None
            )


# Entités livrées par chaque plateforme, avec le suffixe d'identifiant unique
# qui permet de les retrouver dans le registre quand une destination disparaît.
FABRIQUES_SENSOR: tuple[tuple[str, FabriqueEntite], ...] = (
    (SUFFIXE_DERNIER_TELEVERSEMENT, CapteurDernierTeleversement),
    (SUFFIXE_SAUVEGARDES_DISTANTES, CapteurSauvegardesDistantes),
)
FABRIQUES_BINARY_SENSOR: tuple[tuple[str, FabriqueEntite], ...] = (
    (SUFFIXE_PROBLEME, CapteurBinaireProblemeDestination),
)


@callback
def async_coordinateur_des_destinations(
    hass: HomeAssistant, entry: ConfigEntry
) -> CoordinateurEntitesDestinations:
    """Renvoie le coordinateur de l'entrée, en le créant au premier appel.

    Les deux plateformes sont montées en parallèle par
    `async_forward_entry_setups()` : la création est donc volontairement
    synchrone, sans `await` entre la lecture et l'écriture de `hass.data`.
    """
    coordinateur = hass.data.get(DATA_DESTINATION_ENTITIES)
    if coordinateur is None or coordinateur.entry_id != entry.entry_id:
        coordinateur = CoordinateurEntitesDestinations(hass, entry)
        hass.data[DATA_DESTINATION_ENTITIES] = coordinateur
        coordinateur.async_demarrer()
    return coordinateur


@callback
def async_enregistrer_source_des_comptes(
    hass: HomeAssistant, source: SourceDesComptes
) -> CALLBACK_TYPE:
    """Branche un compteur faisant autorité sur les sauvegardes distantes.

    Point d'entrée prévu pour la rétention distante (issue #9), qui tiendra un
    registre persistant des sauvegardes déposées chez chaque fournisseur. Tant
    qu'aucune source n'est enregistrée, les capteurs s'appuient sur leur propre
    compteur, alimenté par les événements et restauré au redémarrage.

    Renvoie la fonction qui débranche la source.
    """
    coordinateur = hass.data.get(DATA_DESTINATION_ENTITIES)
    if coordinateur is None:
        raise RuntimeError(
            "les entités de destination ne sont pas encore montées : "
            "aucune source de comptes ne peut être enregistrée"
        )
    coordinateur.async_definir_la_source_des_comptes(source)

    @callback
    def _debrancher() -> None:
        if hass.data.get(DATA_DESTINATION_ENTITIES) is coordinateur:
            coordinateur.async_definir_la_source_des_comptes(None)

    return _debrancher


async def async_setup_destination_sensors(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Ajoute les capteurs d'état des destinations distantes (issue #16).

    Appelée à la fin de l'`async_setup_entry()` de `sensor.py` : les capteurs
    upstream sont créés d'abord, ceux du fork ensuite, et aucune ligne upstream
    n'est touchée (cf. `docs/UPSTREAM.md`).
    """
    async_coordinateur_des_destinations(hass, entry).async_enregistrer_plateforme(
        Platform.SENSOR, async_add_entities, FABRIQUES_SENSOR
    )


async def async_setup_destination_binary_sensors(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Ajoute les capteurs binaires « problème » des destinations (issue #16)."""
    async_coordinateur_des_destinations(hass, entry).async_enregistrer_plateforme(
        Platform.BINARY_SENSOR, async_add_entities, FABRIQUES_BINARY_SENSOR
    )
