"""Compatibilité Python du code livré aux utilisateurs.

Le dépôt se développe et se teste sur l'interpréteur exigé par la dernière version
de Home Assistant (`requires-python` dans `pyproject.toml`, aujourd'hui 3.14), mais
l'intégration est **installée** sur les instances des utilisateurs, dont le
plancher annoncé est celui de `hacs.json` : Home Assistant 2025.1, qui tourne sous
Python 3.12. Une syntaxe apparue après 3.12 ne provoque aucune erreur ici, alors
qu'elle empêcherait l'import de l'intégration chez ces utilisateurs — une
`SyntaxError` au chargement, avant même la moindre ligne de logique.

Ces tests analysent donc chaque module de `custom_components/auto_backup/` avec
`ast.parse(..., feature_version=PLANCHER_UTILISATEUR)`, qui refuse les
constructions plus récentes que le plancher : c'est la seule vérification possible
sans installer un second interpréteur.

Limite assumée : `feature_version` ne couvre pas *toute* l'évolution de la syntaxe
(la documentation CPython la donne pour « best effort »). Elle attrape les pièges
réellement rencontrés — dont l'`except A, B:` sans parenthèses de la PEP 758,
valide à partir de 3.14 seulement — et `test_le_garde_fou_refuse_la_syntaxe_pep_758`
le démontre. Elle ne remplace pas une exécution réelle sur le plancher.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

RACINE_DEPOT = Path(__file__).resolve().parent.parent
REPERTOIRE_INTEGRATION = RACINE_DEPOT / "custom_components" / "auto_backup"

# Version de Home Assistant annoncée aux utilisateurs dans `hacs.json`, et version
# de Python qu'elle impose. Home Assistant 2025.1 tourne sous Python 3.12 : le code
# livré doit donc rester analysable par un interpréteur 3.12.
VERSION_HA_MINIMALE_ANNONCEE = "2025.1.0"
PLANCHER_UTILISATEUR = (3, 12)

# Extrait volontairement écrit dans la syntaxe de la PEP 758 (`except` multiple sans
# parenthèses), acceptée par Python 3.14 et refusée avant : il sert de témoin au
# garde-fou lui-même.
SOURCE_PEP_758 = "try:\n    pass\nexcept ValueError, TypeError:\n    pass\n"


def _modules_de_l_integration() -> list[Path]:
    return sorted(REPERTOIRE_INTEGRATION.rglob("*.py"))


def _plancher_lisible() -> str:
    return ".".join(str(partie) for partie in PLANCHER_UTILISATEUR)


@pytest.mark.parametrize(
    "module",
    _modules_de_l_integration(),
    ids=lambda chemin: str(chemin.relative_to(REPERTOIRE_INTEGRATION)),
)
def test_le_module_reste_analysable_par_le_plancher_utilisateur(module: Path) -> None:
    """Aucun module livré n'emploie une syntaxe postérieure au plancher annoncé."""
    try:
        ast.parse(
            module.read_text(encoding="utf-8"),
            filename=str(module),
            feature_version=PLANCHER_UTILISATEUR,
        )
    except SyntaxError as err:
        pytest.fail(
            f"{module.relative_to(RACINE_DEPOT)}:{err.lineno} emploie une syntaxe "
            f"indisponible en Python {_plancher_lisible()} ({err.msg}). "
            "L'intégration ne se chargerait pas chez les utilisateurs de Home "
            f"Assistant {VERSION_HA_MINIMALE_ANNONCEE} : voir docs/tests.md, "
            "section « Compatibilité Python »."
        )


def test_le_garde_fou_refuse_la_syntaxe_pep_758() -> None:
    """Le garde-fou détecte bien un `except A, B:` sans parenthèses.

    Sans ce test, une `feature_version` mal choisie — ou trop permissive — laisserait
    passer la régression que le garde-fou est censé empêcher.
    """
    with pytest.raises(SyntaxError):
        ast.parse(SOURCE_PEP_758, feature_version=PLANCHER_UTILISATEUR)

    # Le témoin est bien du Python valide par ailleurs : c'est la version visée,
    # et elle seule, qui le refuse.
    ast.parse(SOURCE_PEP_758, feature_version=(3, 14))


def test_le_plancher_teste_correspond_a_la_version_annoncee_dans_hacs() -> None:
    """`hacs.json` reste la source du plancher utilisateur contrôlé ici.

    Monter la version minimale de Home Assistant annoncée aux utilisateurs permet
    d'employer une syntaxe plus récente : ce test force alors à revoir
    `PLANCHER_UTILISATEUR` plutôt qu'à laisser les deux diverger en silence.
    """
    hacs = json.loads((RACINE_DEPOT / "hacs.json").read_text(encoding="utf-8"))

    assert hacs["homeassistant"] == VERSION_HA_MINIMALE_ANNONCEE, (
        "la version minimale de Home Assistant annoncée dans hacs.json a changé "
        f"({hacs['homeassistant']!r}) : vérifier la version de Python qu'elle "
        f"impose et mettre à jour PLANCHER_UTILISATEUR (aujourd'hui "
        f"{_plancher_lisible()}) ainsi que docs/tests.md."
    )
