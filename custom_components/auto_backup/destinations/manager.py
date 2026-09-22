"""Gestionnaire des destinations distantes d'une entrée de configuration."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator, Mapping
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback

from ..const import CONF_DESTINATIONS
from .destination import RemoteDestination
from .errors import DestinationConfigError, DestinationNotFoundError
from .models import DestinationConfig
from .registry import create_destination

_LOGGER = logging.getLogger(__name__)


class DestinationManager:
    """Instancie et tient à jour les destinations configurées.

    Le gestionnaire est volontairement tolérant : une destination dont la
    configuration est invalide ou dont le fournisseur n'est pas (ou plus)
    enregistré est ignorée avec un avertissement, sans empêcher le chargement
    des autres destinations ni celui de l'intégration.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        """Crée un gestionnaire vide rattaché à une instance Home Assistant."""
        self._hass = hass
        self._destinations: dict[str, RemoteDestination] = {}
        self._reauthentifications: set[str] = set()

    @callback
    def async_load(self, raw_configs: Iterable[Mapping[str, Any]]) -> None:
        """Remplace les destinations courantes par celles décrites en options."""
        destinations: dict[str, RemoteDestination] = {}
        for raw_config in raw_configs:
            destination = self._async_create(raw_config)
            if destination is None:
                continue
            if destination.destination_id in destinations:
                _LOGGER.warning(
                    "Destination « %s » ignorée : identifiant déjà utilisé",
                    destination.destination_id,
                )
                continue
            destinations[destination.destination_id] = destination
        self._destinations = destinations
        # Un marquage « à ré-autoriser » ne survit pas à la destination qu'il
        # décrit : une destination supprimée repart d'un état propre (issue #7).
        self._reauthentifications &= set(destinations)

    @callback
    def _async_create(self, raw_config: Mapping[str, Any]) -> RemoteDestination | None:
        """Instancie une destination, ou renvoie `None` si elle est inutilisable."""
        try:
            config = DestinationConfig.from_dict(raw_config)
        except DestinationConfigError as err:
            _LOGGER.warning("Destination ignorée, configuration invalide : %s", err)
            return None
        try:
            return create_destination(self._hass, config)
        except DestinationConfigError as err:
            _LOGGER.warning(
                "Destination « %s » ignorée : %s", config.destination_id, err
            )
            return None

    @property
    def destinations(self) -> list[RemoteDestination]:
        """Destinations chargées, dans l'ordre de la configuration."""
        return list(self._destinations.values())

    @property
    def configs(self) -> list[DestinationConfig]:
        """Configurations des destinations chargées."""
        return [destination.config for destination in self._destinations.values()]

    @callback
    def async_get(self, destination_id: str) -> RemoteDestination:
        """Renvoie une destination par identifiant.

        Lève `DestinationNotFoundError` si aucune destination ne porte cet
        identifiant : c'est le cas quand un service référence une destination
        supprimée entre-temps.
        """
        try:
            return self._destinations[destination_id]
        except KeyError:
            raise DestinationNotFoundError(
                f"destination inconnue : « {destination_id} »"
            ) from None

    @callback
    def async_marquer_la_reauthentification(self, destination_id: str) -> None:
        """Note qu'une destination attend une nouvelle autorisation (issue #7).

        Le marquage est porté par le gestionnaire et non par la destination
        elle-même : les destinations sont recréées à chaque rechargement des
        options, l'information doit leur survivre. Il ne concerne qu'une
        destination : les autres restent utilisables.
        """
        self._reauthentifications.add(destination_id)

    @callback
    def async_effacer_la_reauthentification(self, destination_id: str) -> None:
        """Lève le marquage d'une destination ré-autorisée ou supprimée."""
        self._reauthentifications.discard(destination_id)

    @callback
    def reauthentification_requise(self, destination_id: str) -> bool:
        """Indique si cette destination attend une nouvelle autorisation."""
        return destination_id in self._reauthentifications

    @property
    def reauthentifications_requises(self) -> frozenset[str]:
        """Identifiants des destinations en attente de ré-autorisation."""
        return frozenset(self._reauthentifications)

    async def async_options_updated(
        self, hass: HomeAssistant, entry: ConfigEntry
    ) -> None:
        """Recharge les destinations quand les options de l'entrée changent."""
        self.async_load(entry.options.get(CONF_DESTINATIONS, []))

    def __contains__(self, destination_id: object) -> bool:
        """Indique si une destination porte cet identifiant."""
        return destination_id in self._destinations

    def __iter__(self) -> Iterator[RemoteDestination]:
        """Itère sur les destinations chargées."""
        return iter(self._destinations.values())

    def __len__(self) -> int:
        """Nombre de destinations chargées."""
        return len(self._destinations)
