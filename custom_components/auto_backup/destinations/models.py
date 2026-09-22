"""Types de données partagés par toutes les destinations distantes."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import voluptuous as vol
from homeassistant.const import (
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_NAME,
    CONF_TOKEN,
)

from ..const import (
    CONF_DESTINATION_ID,
    CONF_FOLDER,
    CONF_PROVIDER,
    CONF_RETENTION_COUNT,
    CONF_RETENTION_DAYS,
    DEFAULT_DESTINATION_FOLDER,
)
from .errors import DestinationConfigError
from .schema import DESTINATION_SCHEMA, TOKEN_SCHEMA, chemin_de_dossier

# Remplace toute valeur secrète dans une représentation journalisable. La valeur
# est volontairement constante et sans longueur indicative : un masque du type
# `abcd…wxyz` ou `********` (8 étoiles pour 8 caractères) divulgue encore.
VALEUR_MASQUEE = "***"


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


def _valide_secret(nom_champ: str, valeur: Any) -> str | None:
    """Renvoie un secret nettoyé, ou `None` s'il est absent.

    Le message d'erreur ne cite jamais la valeur reçue, contrairement aux autres
    validateurs : un secret mal saisi ne doit pas se retrouver dans une trace.
    """
    if valeur is None:
        return None
    if not isinstance(valeur, str) or not valeur.strip():
        raise DestinationConfigError(
            f"{nom_champ} doit être une chaîne non vide ou être absent"
        )
    return valeur.strip()


def _valide_jeton(valeur: Any) -> dict[str, Any] | None:
    """Valide un jeton OAuth2 persisté et le renvoie copié, ou `None`.

    La copie est délibérée : la configuration est immuable, elle ne doit pas
    partager son dictionnaire avec les options de l'entrée.
    """
    if valeur is None:
        return None
    if not isinstance(valeur, Mapping):
        raise DestinationConfigError("token doit être un dictionnaire ou être absent")
    try:
        return dict(TOKEN_SCHEMA(dict(valeur)))
    except (vol.Invalid, TypeError, ValueError) as err:
        raise DestinationConfigError(f"token invalide : {err}") from err


def _masque(valeur: Any) -> str | None:
    """Renvoie `None` si la valeur est absente, le masque constant sinon."""
    return None if valeur is None else VALEUR_MASQUEE


def _jeton_masque(jeton: Mapping[str, Any] | None) -> dict[str, str] | None:
    """Réduit un jeton à ses clés : la structure sans aucune valeur.

    Savoir qu'un `refresh_token` existe aide au diagnostic ; sa valeur, jamais.
    """
    if jeton is None:
        return None
    return dict.fromkeys(jeton, VALEUR_MASQUEE)


@dataclass(frozen=True, slots=True, repr=False)
class DestinationConfig:
    """Configuration persistée d'une destination distante.

    Elle porte, depuis l'issue #7, les **secrets d'autorisation** de la
    destination : les identifiants de l'application OAuth2 créée par
    l'utilisateur chez le fournisseur (`client_id`, `client_secret`) et le jeton
    obtenu (`token`). Ces trois champs ne sont jamais journalisés :
    `__repr__()` les masque, et `as_dict(masquer=True)` produit une copie
    assainie destinée aux journaux et aux messages d'erreur. Seul
    `as_dict()` — sans masquage — est écrit dans l'entrée de configuration.

    L'objet est immuable, ce qui garantit qu'une destination instanciée ne voit
    pas sa configuration changer sous ses pieds : une modification passe par un
    rechargement des destinations.

    `folder` est un chemin relatif POSIX (`Sauvegardes/HA`), normalisé en NFKC
    puis restreint à une liste blanche de caractères et borné en longueur : la
    traversée de répertoires, les chemins absolus, les séparateurs Windows, les
    segments vides, les espaces de bordure, les caractères de contrôle et les
    confusables Unicode sont refusés, car les fournisseurs le reprennent tel
    quel pour bâtir un chemin distant. La règle unique vit dans
    `chemin_de_dossier()`.
    """

    destination_id: str
    provider: str
    name: str
    folder: str = DEFAULT_DESTINATION_FOLDER
    retention_days: int | None = None
    retention_count: int | None = None
    client_id: str | None = None
    client_secret: str | None = None
    token: Mapping[str, Any] | None = None

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
        for nom_champ in ("client_id", "client_secret"):
            object.__setattr__(
                self, nom_champ, _valide_secret(nom_champ, getattr(self, nom_champ))
            )
        object.__setattr__(self, "token", _valide_jeton(self.token))

    @property
    def utilise_oauth(self) -> bool:
        """Indique si la destination porte des identifiants d'application OAuth2."""
        return self.client_id is not None and self.client_secret is not None

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
            client_id=valide.get(CONF_CLIENT_ID),
            client_secret=valide.get(CONF_CLIENT_SECRET),
            token=valide.get(CONF_TOKEN),
        )

    def as_dict(self, *, masquer: bool = False) -> dict[str, Any]:
        """Représentation sérialisable, telle qu'écrite dans l'entrée.

        Les champs d'autorisation absents ne sont pas ajoutés : une destination
        sans OAuth2 est persistée exactement comme avant l'issue #7.

        Avec `masquer=True`, les secrets (`client_id`, `client_secret`, jeton)
        sont remplacés par `VALEUR_MASQUEE` et le jeton est réduit à ses clés :
        c'est la seule forme qui peut être journalisée ou affichée.
        """
        donnees: dict[str, Any] = {
            CONF_DESTINATION_ID: self.destination_id,
            CONF_PROVIDER: self.provider,
            CONF_NAME: self.name,
            CONF_FOLDER: self.folder,
            CONF_RETENTION_DAYS: self.retention_days,
            CONF_RETENTION_COUNT: self.retention_count,
        }
        if self.client_id is not None:
            donnees[CONF_CLIENT_ID] = VALEUR_MASQUEE if masquer else self.client_id
        if self.client_secret is not None:
            donnees[CONF_CLIENT_SECRET] = (
                VALEUR_MASQUEE if masquer else self.client_secret
            )
        if self.token is not None:
            donnees[CONF_TOKEN] = (
                _jeton_masque(self.token) if masquer else dict(self.token)
            )
        return donnees

    def __repr__(self) -> str:
        """Représentation journalisable : aucun secret n'y figure."""
        return (
            f"{type(self).__name__}(destination_id={self.destination_id!r}, "
            f"provider={self.provider!r}, name={self.name!r}, "
            f"folder={self.folder!r}, retention_days={self.retention_days!r}, "
            f"retention_count={self.retention_count!r}, "
            f"client_id={_masque(self.client_id)!r}, "
            f"client_secret={_masque(self.client_secret)!r}, "
            f"token={_jeton_masque(self.token)!r})"
        )


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
