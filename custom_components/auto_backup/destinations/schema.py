"""Schémas voluptuous des destinations distantes.

Le schéma est la porte d'entrée de toute donnée venant de l'extérieur du code :
options de l'entrée de configuration relues au démarrage, formulaires de
l'interface (issue #7) ou appels de service. Il normalise (valeurs par défaut)
et refuse les valeurs aberrantes ; `DestinationConfig` revalide ensuite les
mêmes invariants, y compris quand elle est construite directement en Python.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.const import CONF_NAME

from ..const import (
    CONF_DESTINATION_ID,
    CONF_FOLDER,
    CONF_PROVIDER,
    CONF_RETENTION_COUNT,
    CONF_RETENTION_DAYS,
    DEFAULT_DESTINATION_FOLDER,
)


def texte_non_vide(valeur: Any) -> str:
    """Valide une chaîne non vide (espaces de bordure retirés)."""
    if not isinstance(valeur, str):
        raise vol.Invalid(f"une chaîne de caractères est attendue, reçu {valeur!r}")
    nettoye = valeur.strip()
    if not nettoye:
        raise vol.Invalid("la valeur ne peut pas être vide")
    return nettoye


def entier_strictement_positif(valeur: Any) -> int:
    """Valide un entier strictement positif.

    Les booléens sont refusés bien qu'ils soient des entiers en Python : une
    rétention `True` n'a aucun sens et masquerait une erreur de saisie.
    """
    if isinstance(valeur, bool) or not isinstance(valeur, int):
        raise vol.Invalid(f"un entier est attendu, reçu {valeur!r}")
    if valeur < 1:
        raise vol.Invalid(f"un entier strictement positif est attendu, reçu {valeur}")
    return valeur


RETENTION_SCHEMA = vol.Any(None, entier_strictement_positif)

DESTINATION_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_DESTINATION_ID): texte_non_vide,
        vol.Required(CONF_PROVIDER): texte_non_vide,
        vol.Required(CONF_NAME): texte_non_vide,
        vol.Optional(CONF_FOLDER, default=DEFAULT_DESTINATION_FOLDER): texte_non_vide,
        vol.Optional(CONF_RETENTION_DAYS, default=None): RETENTION_SCHEMA,
        vol.Optional(CONF_RETENTION_COUNT, default=None): RETENTION_SCHEMA,
    }
)

DESTINATIONS_SCHEMA = vol.Schema([DESTINATION_SCHEMA])
