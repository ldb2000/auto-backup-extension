"""Signalement d'une destination à ré-autoriser (issue #7).

Quand le fournisseur refuse de rafraîchir un jeton (accès révoqué, application
supprimée, `invalid_grant`), la destination concernée devient inutilisable tant
que l'utilisateur ne l'a pas ré-autorisée. Deux choses se produisent alors, et
elles sont **propres à cette destination** : les autres continuent de
fonctionner.

1. Le gestionnaire (`DestinationManager`) la marque « ré-authentification
   requise ». Le téléversement (#8) et la purge distante (#9) pourront ainsi
   l'ignorer sans tenter d'appel voué à l'échec.
2. Un problème (« repair issue ») est créé dans Home Assistant, avec le nom de
   la destination. Il est volontairement **non réparable automatiquement**
   (`is_fixable=False`) : la réparation demande de repasser par le navigateur du
   fournisseur, ce qui se fait par « Ré-autoriser une destination » dans les
   options de l'intégration. Le choix est justifié dans
   `docs/adr/0001-destinations-distantes.md`.

Le problème disparaît dès que la destination est ré-autorisée ou supprimée.
"""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from ..const import DATA_DESTINATIONS, DOMAIN, ISSUE_REAUTH_PREFIX
from .models import DestinationConfig

_LOGGER = logging.getLogger(__name__)

# Clé de traduction du problème, commune à toutes les destinations : le nom de la
# destination et son fournisseur sont passés en paramètres.
CLE_TRADUCTION_REAUTH = "reauthentification_requise"


@callback
def identifiant_du_probleme(destination_id: str) -> str:
    """Identifiant du problème Home Assistant lié à une destination."""
    return f"{ISSUE_REAUTH_PREFIX}{destination_id}"


@callback
def async_signaler_la_reauthentification(
    hass: HomeAssistant, config: DestinationConfig
) -> None:
    """Marque la destination à ré-autoriser et crée le problème correspondant."""
    gestionnaire = hass.data.get(DATA_DESTINATIONS)
    if gestionnaire is not None:
        gestionnaire.async_marquer_la_reauthentification(config.destination_id)

    _LOGGER.warning(
        "La destination « %s » (%s) doit être ré-autorisée : le fournisseur a "
        "refusé de renouveler l'accès",
        config.name,
        config.provider,
    )
    ir.async_create_issue(
        hass,
        DOMAIN,
        identifiant_du_probleme(config.destination_id),
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=CLE_TRADUCTION_REAUTH,
        translation_placeholders={
            "nom": config.name,
            "fournisseur": config.provider,
        },
    )


@callback
def async_effacer_la_reauthentification(
    hass: HomeAssistant, destination_id: str
) -> None:
    """Lève le marquage et supprime le problème, s'ils existent.

    Appelée après une ré-autorisation réussie et après une suppression : un
    problème qui survivrait à la destination qu'il décrit serait impossible à
    faire disparaître pour l'utilisateur.
    """
    gestionnaire = hass.data.get(DATA_DESTINATIONS)
    if gestionnaire is not None:
        gestionnaire.async_effacer_la_reauthentification(destination_id)
    ir.async_delete_issue(hass, DOMAIN, identifiant_du_probleme(destination_id))


@callback
def reauthentification_requise(hass: HomeAssistant, destination_id: str) -> bool:
    """Indique si la destination attend une nouvelle autorisation."""
    gestionnaire = hass.data.get(DATA_DESTINATIONS)
    if gestionnaire is None:
        return False
    return gestionnaire.reauthentification_requise(destination_id)
