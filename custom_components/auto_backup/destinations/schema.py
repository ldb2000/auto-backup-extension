"""Schémas voluptuous des destinations distantes.

Le schéma est la porte d'entrée de toute donnée venant de l'extérieur du code :
options de l'entrée de configuration relues au démarrage, formulaires de
l'interface (issue #7) ou appels de service. Il normalise (valeurs par défaut)
et refuse les valeurs aberrantes ; `DestinationConfig` revalide ensuite les
mêmes invariants, y compris quand elle est construite directement en Python.
"""

from __future__ import annotations

import re
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


SEPARATEUR_DOSSIER = "/"

SEGMENTS_DOSSIER_INTERDITS = frozenset({".", ".."})

# Un chemin Windows absolu, qu'il soit écrit « C:\\... » ou « C:/... ».
_LETTRE_DE_LECTEUR = re.compile(r"^[A-Za-z]:")


def chemin_de_dossier(valeur: Any) -> str:
    """Valide un dossier distant et le renvoie normalisé en chemin relatif POSIX.

    Ce champ n'est pas un texte libre : les fournisseurs le reprennent tel quel
    pour construire le chemin distant d'une sauvegarde. Une traversée (`..`), un
    chemin absolu, un séparateur Windows ou un caractère de contrôle doivent donc
    être refusés ici, dans le socle commun, et non chez chaque fournisseur.

    Seul `/` sépare les segments ; `Sauvegardes/HA` est accepté, `../x`, `/abs`,
    `a\\b`, `a/../b` et `a//b` sont refusés.
    """
    if not isinstance(valeur, str):
        raise vol.Invalid(f"le dossier doit être une chaîne, reçu {valeur!r}")
    if not valeur:
        raise vol.Invalid("le dossier ne peut pas être vide")
    if "\\" in valeur:
        raise vol.Invalid(f"le dossier ne peut pas contenir de « \\ », reçu {valeur!r}")
    if any(not caractere.isprintable() for caractere in valeur):
        raise vol.Invalid(
            f"le dossier ne peut pas contenir de caractère de contrôle, reçu {valeur!r}"
        )
    if valeur.startswith(SEPARATEUR_DOSSIER) or _LETTRE_DE_LECTEUR.match(valeur):
        raise vol.Invalid(f"le dossier doit être un chemin relatif, reçu {valeur!r}")

    segments = valeur.split(SEPARATEUR_DOSSIER)
    for segment in segments:
        if not segment:
            raise vol.Invalid(
                f"le dossier ne peut pas contenir de segment vide, reçu {valeur!r}"
            )
        if segment != segment.strip():
            raise vol.Invalid(
                "le dossier ne peut pas contenir d'espace en tête ou en fin de "
                f"segment, reçu {valeur!r}"
            )
        if segment in SEGMENTS_DOSSIER_INTERDITS:
            raise vol.Invalid(
                f"le dossier ne peut pas contenir de segment « {segment} », "
                f"reçu {valeur!r}"
            )
    return SEPARATEUR_DOSSIER.join(segments)


RETENTION_SCHEMA = vol.Any(None, entier_strictement_positif)

DESTINATION_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_DESTINATION_ID): texte_non_vide,
        vol.Required(CONF_PROVIDER): texte_non_vide,
        vol.Required(CONF_NAME): texte_non_vide,
        vol.Optional(
            CONF_FOLDER, default=DEFAULT_DESTINATION_FOLDER
        ): chemin_de_dossier,
        vol.Optional(CONF_RETENTION_DAYS, default=None): RETENTION_SCHEMA,
        vol.Optional(CONF_RETENTION_COUNT, default=None): RETENTION_SCHEMA,
    }
)

DESTINATIONS_SCHEMA = vol.Schema([DESTINATION_SCHEMA])
