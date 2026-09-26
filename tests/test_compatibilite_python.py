"""Compatibilité Python du code livré aux utilisateurs.

Le dépôt se développe et se teste sur l'interpréteur exigé par la dernière version
de Home Assistant (`requires-python` dans `pyproject.toml`, aujourd'hui 3.14), alors
que l'intégration est **installée** sur les instances des utilisateurs, dont le
plancher annoncé est celui de `hacs.json` : Home Assistant 2026.3, qui exige
Python 3.14.2. Une syntaxe apparue après ce plancher ne provoquerait aucune erreur
ici, alors qu'elle empêcherait l'import de l'intégration chez ces utilisateurs — une
`SyntaxError` au chargement, avant même la moindre ligne de logique.

Ces tests analysent donc chaque module de `custom_components/auto_backup/` avec
`ast.parse(..., feature_version=PLANCHER_UTILISATEUR)`, qui refuse les
constructions plus récentes que le plancher : c'est la seule vérification possible
sans installer un second interpréteur.

Les deux planchers coïncident depuis l'alignement de la version annoncée sur 2026.3
(issue #28) : aucune syntaxe connue ne sépare aujourd'hui l'interpréteur de
développement de celui des utilisateurs, et le garde-fou n'a donc rien à rejeter.
Il reste en place parce que les deux planchers sont indépendants — le dépôt suit la
dernière version de Home Assistant, alors que `hacs.json` ne bouge que sur décision
explicite — et qu'ils divergeront de nouveau dès la prochaine version de Python.
`test_le_mecanisme_refuse_une_syntaxe_posterieure_au_plancher` le démontre en
exerçant le mécanisme sur une version antérieure au plancher.

Limite assumée : `feature_version` ne couvre pas *toute* l'évolution de la syntaxe
(la documentation CPython la donne pour « best effort ») et CPython la borne à la
version de l'interpréteur courant. Ce garde-fou ne remplace pas une exécution réelle
sur le plancher.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

RACINE_DEPOT = Path(__file__).resolve().parent.parent
REPERTOIRE_INTEGRATION = RACINE_DEPOT / "custom_components" / "auto_backup"

# Version de Home Assistant annoncée aux utilisateurs dans `hacs.json`, et version
# de Python qu'elle impose. Home Assistant 2026.3 exige Python 3.14.2 : le code
# livré doit donc rester analysable par un interpréteur 3.14.
VERSION_HA_MINIMALE_ANNONCEE = "2026.3.0"
PLANCHER_UTILISATEUR = (3, 14)

# Témoin du mécanisme : extrait volontairement écrit dans la syntaxe de la PEP 758
# (`except` multiple sans parenthèses), acceptée à partir de Python 3.14 et refusée
# avant. `PLANCHER_UTILISATEUR` ne le refuse donc plus — d'où la version témoin
# ci-dessous, choisie *antérieure* au plancher pour éprouver le mécanisme lui-même.
SOURCE_PEP_758 = "try:\n    pass\nexcept ValueError, TypeError:\n    pass\n"
VERSION_ANTERIEURE_AU_PLANCHER = (3, 13)


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


def test_le_mecanisme_refuse_une_syntaxe_posterieure_au_plancher() -> None:
    """`ast.parse(..., feature_version=...)` rejette bien une syntaxe trop récente.

    Le témoin historique était la PEP 758 (`except A, B:` sans parenthèses) opposée
    au plancher annoncé. Il est devenu caduc tel quel : la PEP 758 est valide en
    3.14, qui est désormais le plancher. Aucune syntaxe ne peut le remplacer —
    CPython borne `feature_version` à la version de l'interpréteur courant, donc rien
    n'est « postérieur » au plancher tant que dépôt et utilisateurs partagent le même
    Python.

    Le garde-fou reste pourtant utile pour les versions futures : `hacs.json` ne bouge
    que sur décision explicite alors que le dépôt suit la dernière version de Home
    Assistant, et les deux planchers divergeront de nouveau. Ce test prouve donc que le
    mécanisme mord toujours, en l'exerçant une version en arrière — sans prétendre que
    3.13 soit le plancher contrôlé, qui reste `PLANCHER_UTILISATEUR`.
    """
    with pytest.raises(SyntaxError):
        ast.parse(SOURCE_PEP_758, feature_version=VERSION_ANTERIEURE_AU_PLANCHER)

    # Le témoin est bien du Python valide par ailleurs : c'est la version visée,
    # et elle seule, qui le refuse.
    ast.parse(SOURCE_PEP_758, feature_version=PLANCHER_UTILISATEUR)


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
