"""Lien entre les destinations distantes et l'entrée de configuration.

Les destinations sont persistées dans `entry.options[CONF_DESTINATIONS]`, sous
la forme d'une liste de dictionnaires (cf. `docs/adr/0001-destinations-distantes.md`).
Ce module concentre toutes les lectures et écritures de cette liste.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback

from ..const import (
    CONF_AUTO_PURGE,
    CONF_BACKUP_TIMEOUT,
    CONF_DESTINATIONS,
    DATA_DESTINATIONS,
    DEFAULT_BACKUP_TIMEOUT,
)
from .errors import DestinationConfigError
from .manager import DestinationManager
from .models import DestinationConfig
from .schema import DESTINATIONS_SCHEMA


@callback
def async_destination_configs(entry: ConfigEntry) -> list[dict[str, Any]]:
    """Renvoie la liste brute des destinations persistées dans l'entrée."""
    return list(entry.options.get(CONF_DESTINATIONS, []))


@callback
def async_setup_destinations(
    hass: HomeAssistant, entry: ConfigEntry
) -> DestinationManager:
    """Charge les destinations de l'entrée et les expose dans `hass.data`.

    Le gestionnaire obtenu suit les options de l'entrée : il se recharge quand
    elles changent et disparaît de `hass.data` au déchargement de l'entrée.
    """
    manager = DestinationManager(hass)
    manager.async_load(async_destination_configs(entry))
    hass.data[DATA_DESTINATIONS] = manager

    @callback
    def retirer_le_gestionnaire() -> None:
        """Retire le gestionnaire de `hass.data` au déchargement de l'entrée."""
        hass.data.pop(DATA_DESTINATIONS, None)

    entry.async_on_unload(retirer_le_gestionnaire)
    entry.async_on_unload(entry.add_update_listener(manager.async_options_updated))
    return manager


@callback
def async_persist_destinations(
    hass: HomeAssistant, entry: ConfigEntry, configs: Iterable[DestinationConfig]
) -> None:
    """Écrit la liste des destinations dans les options de l'entrée.

    Les options upstream (`auto_purge`, `backup_timeout`) sont conservées et,
    si elles sont absentes, complétées par leurs valeurs par défaut : l'écouteur
    de mise à jour upstream les lit sans valeur de repli.
    """
    identifiants = [config.destination_id for config in configs]
    doublons = {
        identifiant
        for identifiant in identifiants
        if identifiants.count(identifiant) > 1
    }
    if doublons:
        raise DestinationConfigError(
            f"identifiants de destination en double : {', '.join(sorted(doublons))}"
        )

    options = {
        CONF_AUTO_PURGE: entry.options.get(CONF_AUTO_PURGE, True),
        CONF_BACKUP_TIMEOUT: entry.options.get(
            CONF_BACKUP_TIMEOUT, DEFAULT_BACKUP_TIMEOUT
        ),
        **entry.options,
        CONF_DESTINATIONS: DESTINATIONS_SCHEMA(
            [config.as_dict() for config in configs]
        ),
    }
    hass.config_entries.async_update_entry(entry, options=options)


@callback
def preserve_destinations(
    options: Mapping[str, Any], user_input: dict[str, Any]
) -> dict[str, Any]:
    """Réinjecte les destinations dans les options soumises par le flux d'options.

    Le flux d'options upstream remplace l'intégralité des options par le contenu
    de son formulaire, qui ne connaît pas les destinations : sans ce report, les
    destinations configurées seraient perdues au premier enregistrement.
    """
    if CONF_DESTINATIONS not in options:
        return dict(user_input)
    return {**user_input, CONF_DESTINATIONS: options[CONF_DESTINATIONS]}
