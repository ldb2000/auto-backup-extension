"""Fournisseurs de destinations distantes livrés par le fork.

Un fournisseur s'enregistre auprès du registre (`destinations/registry.py`) au
moment où son module est importé, par le décorateur `@provider(...)`. Ce paquet
regroupe ces modules et expose l'unique point d'entrée qui les charge :
`enregistrer_les_fournisseurs()`, appelée au démarrage de l'intégration
(`destinations/config_entry.py`).

L'import est **différé dans la fonction** et non fait au chargement de ce
module : `config_entry` importe ce paquet, et un fournisseur importe à son tour
`destinations/oauth.py`, qui dépend de `config_entry`. Importer au niveau du
module refermerait ce cycle sur un paquet à moitié initialisé.

Appeler la fonction plusieurs fois est sans effet : Python ne rejoue pas
l'import d'un module déjà chargé, donc aucun fournisseur n'est enregistré deux
fois (ce qui lèverait `DuplicateProviderError`).
"""

from __future__ import annotations

from homeassistant.core import callback


@callback
def enregistrer_les_fournisseurs() -> None:
    """Charge les modules de fournisseurs, ce qui les enregistre au registre."""
    from . import google_drive  # noqa: F401  (l'import fait l'enregistrement)


__all__ = ["enregistrer_les_fournisseurs"]
