"""Hiérarchie d'erreurs typées des destinations distantes.

Toutes les erreurs dérivent de `DestinationError`, elle-même dérivée de
`HomeAssistantError` : un appel de service qui remonte l'une d'elles est
présenté à l'utilisateur par Home Assistant sans traitement particulier.

Les appelants peuvent donc :

- attraper `DestinationError` pour traiter n'importe quel échec de destination ;
- attraper une sous-classe pour réagir spécifiquement (relancer une
  authentification, alerter sur un quota, ignorer une sauvegarde déjà purgée).
"""

from __future__ import annotations

from homeassistant.exceptions import HomeAssistantError


class DestinationError(HomeAssistantError):
    """Erreur générique d'une destination distante."""


class DestinationAuthError(DestinationError):
    """Le fournisseur refuse les identifiants : jeton expiré ou accès révoqué.

    C'est cette erreur qui déclenchera la ré-authentification côté Home Assistant.
    """


class DestinationQuotaError(DestinationError):
    """Le fournisseur refuse l'écriture : quota ou espace de stockage épuisé."""


class DestinationNotFoundError(DestinationError):
    """La sauvegarde distante visée n'existe pas (ou plus).

    Utile à la purge distante : une sauvegarde supprimée entre le listage et la
    suppression ne doit pas faire échouer le cycle de rétention.
    """


class DestinationConfigError(DestinationError):
    """La configuration d'une destination est invalide ou incomplète."""


class UnknownProviderError(DestinationConfigError):
    """Le fournisseur demandé n'est pas enregistré dans le registre."""


class DuplicateProviderError(DestinationError):
    """Un fournisseur est enregistré deux fois sous le même identifiant."""
