"""Types de données partagés par toutes les destinations distantes."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
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
from .errors import DestinationConfigError
from .schema import DESTINATION_SCHEMA, chemin_de_dossier


def _valide_texte(nom_champ: str, valeur: Any) -> str:
    """Renvoie `valeur` nettoyée si c'est une chaîne non vide, sinon lève."""
    if not isinstance(valeur, str) or not valeur.strip():
        raise DestinationConfigError(
            f"{nom_champ} doit être une chaîne non vide, reçu {valeur!r}"
        )
    return valeur.strip()


def _valide_dossier(nom_champ: str, valeur: Any) -> str:
    """Renvoie le dossier normalisé en chemin relatif POSIX, sinon lève.

    La règle vit dans `chemin_de_dossier()`, partagée avec le schéma : une
    destination construite directement en Python ne doit pas pouvoir échapper à
    la protection contre la traversée de répertoires.
    """
    try:
        return chemin_de_dossier(valeur)
    except vol.Invalid as err:
        raise DestinationConfigError(f"{nom_champ} invalide : {err}") from err


def _valide_retention(nom_champ: str, valeur: Any) -> int | None:
    """Renvoie une rétention valide : `None` ou un entier strictement positif."""
    if valeur is None:
        return None
    if isinstance(valeur, bool) or not isinstance(valeur, int):
        raise DestinationConfigError(
            f"{nom_champ} doit être un entier ou être absent, reçu {valeur!r}"
        )
    if valeur < 1:
        raise DestinationConfigError(
            f"{nom_champ} doit être strictement positif, reçu {valeur}"
        )
    return valeur


@dataclass(frozen=True, slots=True)
class DestinationConfig:
    """Configuration persistée d'une destination distante.

    Cet objet ne contient aucun secret : les jetons d'authentification restent
    gérés par Home Assistant (issue #7). Il est immuable, ce qui garantit qu'une
    destination instanciée ne voit pas sa configuration changer sous ses pieds :
    une modification passe par un rechargement des destinations.

    `folder` est un chemin relatif POSIX (`Sauvegardes/HA`) : la traversée de
    répertoires, les chemins absolus, les séparateurs Windows, les segments
    vides, les espaces de bordure et les caractères de contrôle sont refusés,
    car les fournisseurs le reprennent tel quel pour bâtir un chemin distant.
    """

    destination_id: str
    provider: str
    name: str
    folder: str = DEFAULT_DESTINATION_FOLDER
    retention_days: int | None = None
    retention_count: int | None = None

    def __post_init__(self) -> None:
        """Revalide les invariants, y compris hors du schéma voluptuous."""
        for nom_champ in ("destination_id", "provider", "name"):
            object.__setattr__(
                self, nom_champ, _valide_texte(nom_champ, getattr(self, nom_champ))
            )
        object.__setattr__(self, "folder", _valide_dossier("folder", self.folder))
        for nom_champ in ("retention_days", "retention_count"):
            object.__setattr__(
                self, nom_champ, _valide_retention(nom_champ, getattr(self, nom_champ))
            )

    @classmethod
    def from_dict(cls, donnees: Mapping[str, Any]) -> DestinationConfig:
        """Construit une configuration à partir de données persistées.

        Lève `DestinationConfigError` si les données ne respectent pas
        `DESTINATION_SCHEMA`, ou si elles ne sont même pas un dictionnaire :
        les options d'une entrée restent modifiables à la main.
        """
        try:
            valide = DESTINATION_SCHEMA(dict(donnees))
        except (vol.Invalid, TypeError, ValueError) as err:
            raise DestinationConfigError(
                f"configuration de destination invalide : {err}"
            ) from err
        return cls(
            destination_id=valide[CONF_DESTINATION_ID],
            provider=valide[CONF_PROVIDER],
            name=valide[CONF_NAME],
            folder=valide[CONF_FOLDER],
            retention_days=valide[CONF_RETENTION_DAYS],
            retention_count=valide[CONF_RETENTION_COUNT],
        )

    def as_dict(self) -> dict[str, Any]:
        """Représentation sérialisable, telle qu'écrite dans l'entrée."""
        return {
            CONF_DESTINATION_ID: self.destination_id,
            CONF_PROVIDER: self.provider,
            CONF_NAME: self.name,
            CONF_FOLDER: self.folder,
            CONF_RETENTION_DAYS: self.retention_days,
            CONF_RETENTION_COUNT: self.retention_count,
        }


@dataclass(frozen=True, slots=True)
class RemoteBackup:
    """Sauvegarde telle que vue chez le fournisseur distant.

    `remote_id` est l'identifiant opaque du fournisseur (identifiant de fichier
    Google Drive, chemin Dropbox...) : c'est la seule clé utilisable pour
    supprimer une sauvegarde. `slug` est celui de Home Assistant, quand il est
    connu ; il permet de faire le lien avec une sauvegarde locale.
    """

    remote_id: str
    name: str
    slug: str | None = None
    size: int | None = None
    created_at: datetime | None = None
    path: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Valide l'identifiant distant, le nom et la taille."""
        object.__setattr__(
            self, "remote_id", _valide_texte("remote_id", self.remote_id)
        )
        object.__setattr__(self, "name", _valide_texte("name", self.name))
        if self.size is not None and (
            isinstance(self.size, bool) or not isinstance(self.size, int)
        ):
            raise DestinationConfigError(
                f"size doit être un entier ou être absent, reçu {self.size!r}"
            )
        if self.size is not None and self.size < 0:
            raise DestinationConfigError(
                f"size ne peut pas être négative, reçu {self.size}"
            )

    def as_dict(self) -> dict[str, Any]:
        """Représentation sérialisable, utilisable dans un événement."""
        return {
            "remote_id": self.remote_id,
            "name": self.name,
            "slug": self.slug,
            "size": self.size,
            "created_at": (
                self.created_at.isoformat() if self.created_at is not None else None
            ),
            "path": self.path,
            "metadata": dict(self.metadata),
        }
