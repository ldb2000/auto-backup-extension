"""Fournisseurs de destinations réellement livrés par le fork.

Chaque fournisseur (Dropbox — issue #10, Google Drive — issue #13) vit dans son
propre module et ne connaît que le socle (`destinations/`). Ce paquet n'a qu'un
rôle : **enregistrer** ces fournisseurs auprès du registre, en un point unique,
appelé par `async_setup_destinations()` au chargement de l'entrée.

Aucun module concret n'est importé ici au niveau du fichier, et ce pour deux
raisons :

- l'import est **différé** dans `fournisseurs_livres()` parce qu'un fournisseur
  importe `destinations.oauth`, qui importe `destinations.config_entry`, d'où
  l'appel vient : un import au niveau du module fermerait le cycle ;
- une issue qui ajoute un fournisseur n'a qu'une ligne à ajouter dans la table
  ci-dessous, ce qui limite les conflits entre les issues développées en
  parallèle.

L'enregistrement est **idempotent** : `async_setup_destinations()` est appelé à
chaque chargement de l'entrée, et le registre est un état global du processus.
"""

from __future__ import annotations

from homeassistant.core import callback

from ..registry import DestinationFactory, list_providers, register_provider


@callback
def fournisseurs_livres() -> tuple[tuple[str, DestinationFactory], ...]:
    """Table des fournisseurs livrés : identifiant -> fabrique.

    L'import est fait ici, à l'appel, et non au chargement du module.
    """
    from .dropbox import PROVIDER_DROPBOX, DropboxDestination
    from .google_drive import PROVIDER_GOOGLE_DRIVE, GoogleDriveDestination

    return (
        (PROVIDER_DROPBOX, DropboxDestination),
        (PROVIDER_GOOGLE_DRIVE, GoogleDriveDestination),
    )


@callback
def enregistrer_les_fournisseurs() -> None:
    """Enregistre les fournisseurs livrés qui ne le sont pas déjà."""
    deja_enregistres = set(list_providers())
    for identifiant, fabrique in fournisseurs_livres():
        if identifiant not in deja_enregistres:
            register_provider(identifiant, fabrique)


__all__ = ["enregistrer_les_fournisseurs", "fournisseurs_livres"]
