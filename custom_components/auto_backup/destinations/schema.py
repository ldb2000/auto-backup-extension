"""Schémas voluptuous des destinations distantes.

Le schéma est la porte d'entrée de toute donnée venant de l'extérieur du code :
options de l'entrée de configuration relues au démarrage, formulaires de
l'interface (issue #7) ou appels de service. Il normalise (valeurs par défaut)
et refuse les valeurs aberrantes ; `DestinationConfig` revalide ensuite les
mêmes invariants, y compris quand elle est construite directement en Python.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.const import (
    CONF_ACCESS_TOKEN,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_NAME,
    CONF_TOKEN,
)

from ..const import (
    CONF_DESTINATION_ID,
    CONF_FOLDER,
    CONF_PROVIDER,
    CONF_PROVIDER_DATA,
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

# Seul espace admis : l'espace ordinaire U+0020. Toute autre espace Unicode
# (insécable, fine, séparateur de ligne...) est soit ramenée à celle-ci par la
# normalisation NFKC, soit refusée par la liste blanche ci-dessous.
ESPACE_DOSSIER = " "

# Ponctuation sûre : ni séparateur de chemin, ni caractère d'échappement, ni
# joker. Tout le reste (« : », « * », « ? », « ~ », guillemets, symboles...) est
# refusé, faute de savoir comment chaque fournisseur l'interprétera.
PONCTUATION_DOSSIER_AUTORISEE = frozenset("-_.()")

# Bornes volontairement conservatrices : Dropbox et Google Drive tolèrent des
# chemins plus longs, mais un dossier de sauvegarde n'a aucune raison d'aller
# au-delà, et une valeur démesurée relève de l'erreur de saisie ou de l'abus.
LONGUEUR_MAX_DOSSIER = 255
LONGUEUR_MAX_SEGMENT = 100

# Un chemin Windows absolu, qu'il soit écrit « C:\\... » ou « C:/... ».
_LETTRE_DE_LECTEUR = re.compile(r"^[A-Za-z]:")


def _caractere_de_dossier_autorise(caractere: str) -> bool:
    """Indique si un caractère est admis dans un segment de dossier distant.

    Le jeu autorisé est une liste blanche : alphanumérique Unicode (`Été` et
    `Архив` restent valides), espace ordinaire, et la ponctuation sûre de
    `PONCTUATION_DOSSIER_AUTORISEE`. Tout le reste est refusé, en particulier
    les catégories Unicode invisibles ou trompeuses (`Cc`, `Cf`, `Zl`, `Zp`,
    `Zs` hors U+0020) et la ponctuation ou les symboles hors liste blanche
    (`Po`, `Ps`, `Pe`, `Sm`, `So`).
    """
    return (
        caractere.isalnum()
        or caractere == ESPACE_DOSSIER
        or caractere in PONCTUATION_DOSSIER_AUTORISEE
    )


def chemin_de_dossier(valeur: Any) -> str:
    """Valide un dossier distant et le renvoie normalisé en chemin relatif POSIX.

    Ce champ n'est pas un texte libre : les fournisseurs le reprennent tel quel
    pour construire le chemin distant d'une sauvegarde. Une traversée (`..`), un
    chemin absolu, un séparateur Windows ou un caractère de contrôle doivent donc
    être refusés ici, dans le socle commun, et non chez chaque fournisseur.

    La valeur est d'abord normalisée en **NFKC**, *avant* toute vérification :
    sans cela, les confusables Unicode contourneraient les règles, deux points
    pleine chasse (U+FF0E) valant `".."` et une barre oblique pleine chasse
    (U+FF0F) valant `"/"` une fois normalisés par le fournisseur ou par le
    système de fichiers de destination.
    Les règles s'appliquent ensuite à la chaîne normalisée, qui est aussi la
    valeur renvoyée : ce qui est validé est exactement ce qui sera utilisé.

    Les caractères admis forment une liste blanche : alphanumérique Unicode,
    espace ordinaire U+0020 interne, et `-`, `_`, `.`, `(`, `)`. Le chemin est
    borné à `LONGUEUR_MAX_DOSSIER` caractères au total et `LONGUEUR_MAX_SEGMENT`
    par segment.

    Seul `/` sépare les segments ; `Sauvegardes/HA` et `Sauvegardes/Été` sont
    acceptés ; `../x`, `/abs`, `a\\b`, `a/../b` et `a//b` sont refusés, tout
    comme leurs écritures pleine chasse (U+FF0E, U+FF0F) et le point de
    suspension double U+2025, qui valent `..` et `a/..` une fois normalisés.
    """
    if not isinstance(valeur, str):
        raise vol.Invalid(f"le dossier doit être une chaîne, reçu {valeur!r}")
    if not valeur:
        raise vol.Invalid("le dossier ne peut pas être vide")

    normalise = unicodedata.normalize("NFKC", valeur)

    if "\\" in normalise:
        raise vol.Invalid(f"le dossier ne peut pas contenir de « \\ », reçu {valeur!r}")
    if any(not caractere.isprintable() for caractere in normalise):
        raise vol.Invalid(
            f"le dossier ne peut pas contenir de caractère de contrôle, reçu {valeur!r}"
        )
    if normalise.startswith(SEPARATEUR_DOSSIER) or _LETTRE_DE_LECTEUR.match(normalise):
        raise vol.Invalid(f"le dossier doit être un chemin relatif, reçu {valeur!r}")
    if len(normalise) > LONGUEUR_MAX_DOSSIER:
        raise vol.Invalid(
            f"le dossier ne peut pas dépasser {LONGUEUR_MAX_DOSSIER} caractères, "
            f"reçu {len(normalise)}"
        )

    interdits = [
        caractere
        for caractere in normalise
        if caractere != SEPARATEUR_DOSSIER
        and not _caractere_de_dossier_autorise(caractere)
    ]
    if interdits:
        raise vol.Invalid(
            "le dossier ne peut contenir que des lettres, des chiffres, des espaces "
            f"et « -_.() », caractère interdit {interdits[0]!r} dans {valeur!r}"
        )

    segments = normalise.split(SEPARATEUR_DOSSIER)
    for segment in segments:
        if not segment:
            raise vol.Invalid(
                f"le dossier ne peut pas contenir de segment vide, reçu {valeur!r}"
            )
        if len(segment) > LONGUEUR_MAX_SEGMENT:
            raise vol.Invalid(
                f"un segment du dossier ne peut pas dépasser {LONGUEUR_MAX_SEGMENT} "
                f"caractères, reçu {len(segment)}"
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

# Bornes des données de fournisseur (`provider_data`, issues #10 et #13). Ce
# champ accueille le peu qu'un fournisseur a besoin de retenir sur le compte
# autorisé — l'identifiant du compte Dropbox ou l'adresse du compte Google, par
# exemple. Il n'a pas vocation à devenir un fourre-tout : ces bornes l'empêchent
# de grossir sans qu'on le décide.
MAX_CLES_FOURNISSEUR = 20
MAX_LONGUEUR_VALEUR_FOURNISSEUR = 500


def donnees_de_fournisseur(valeur: Any) -> dict[str, Any]:
    """Valide les données propres à un fournisseur et les renvoie copiées.

    Elles sont persistées telles quelles dans l'entrée de configuration : seules
    des valeurs scalaires sérialisables en JSON sont acceptées, sous des clés
    textuelles, en nombre et en longueur bornés. Aucun secret n'a sa place ici :
    les identifiants d'application et le jeton ont leurs propres champs.
    """
    if not isinstance(valeur, Mapping):
        raise vol.Invalid(
            f"les données de fournisseur doivent être un dictionnaire, reçu {valeur!r}"
        )
    if len(valeur) > MAX_CLES_FOURNISSEUR:
        raise vol.Invalid(
            "les données de fournisseur ne peuvent pas dépasser "
            f"{MAX_CLES_FOURNISSEUR} clés, reçu {len(valeur)}"
        )
    valide: dict[str, Any] = {}
    for cle, contenu in valeur.items():
        nom = texte_non_vide(cle)
        if contenu is not None and not isinstance(contenu, str | int | float | bool):
            raise vol.Invalid(
                f"la donnée de fournisseur « {nom} » doit être un texte, un nombre, "
                f"un booléen ou être absente, reçu {type(contenu).__name__}"
            )
        if isinstance(contenu, str) and len(contenu) > MAX_LONGUEUR_VALEUR_FOURNISSEUR:
            raise vol.Invalid(
                f"la donnée de fournisseur « {nom} » ne peut pas dépasser "
                f"{MAX_LONGUEUR_VALEUR_FOURNISSEUR} caractères"
            )
        valide[nom] = contenu
    return valide


def horodatage(valeur: Any) -> float:
    """Valide un horodatage epoch (secondes), tel que renvoyé par `time.time()`."""
    if isinstance(valeur, bool) or not isinstance(valeur, int | float):
        raise vol.Invalid(f"un horodatage numérique est attendu, reçu {valeur!r}")
    return float(valeur)


# Jeton OAuth2 tel que renvoyé par le fournisseur puis persisté (issue #7).
#
# Seuls `access_token` et `expires_at` sont exigés : `expires_at` est ajouté par le
# fork à partir d'`expires_in` (cf. `destinations/oauth.py`), et c'est lui qui décide
# du rafraîchissement. Les clés supplémentaires (`token_type`, `scope`, identifiant de
# compte...) varient d'un fournisseur à l'autre et sont conservées telles quelles :
# elles seront nécessaires à Dropbox (#10) et à Google Drive (#13).
TOKEN_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ACCESS_TOKEN): texte_non_vide,
        vol.Required("expires_at"): horodatage,
        vol.Optional("refresh_token"): vol.Any(None, texte_non_vide),
    },
    extra=vol.ALLOW_EXTRA,
)

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
        # Champs d'autorisation (issue #7), volontairement **sans valeur par
        # défaut** : une destination qui n'utilise pas OAuth2 ne doit pas se voir
        # ajouter trois clés nulles dans les options persistées. `None` reste
        # toutefois accepté, une destination désautorisée à la main écrivant la
        # clé plutôt que de la retirer.
        vol.Optional(CONF_CLIENT_ID): vol.Any(None, texte_non_vide),
        vol.Optional(CONF_CLIENT_SECRET): vol.Any(None, texte_non_vide),
        vol.Optional(CONF_TOKEN): vol.Any(None, TOKEN_SCHEMA),
        # Description du compte autorisé (issues #10 et #13), elle aussi sans
        # valeur par défaut : une destination qui n'en a pas est persistée
        # exactement comme avant.
        vol.Optional(CONF_PROVIDER_DATA): vol.Any(None, donnees_de_fournisseur),
    }
)

DESTINATIONS_SCHEMA = vol.Schema([DESTINATION_SCHEMA])
