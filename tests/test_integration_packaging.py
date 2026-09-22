"""Vérifications de base sur l'intégration livrée et sur l'outillage Python.

Ces tests ne dépendent pas de Home Assistant : ils garantissent que l'environnement
de développement (interpréteur, collecte pytest) est fonctionnel et que les fichiers
livrés dans `custom_components/auto_backup/` restent valides. Les tests fonctionnels
de l'intégration sont traités séparément.
"""

from __future__ import annotations

import json
import py_compile
import re
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INTEGRATION_DIR = REPO_ROOT / "custom_components" / "auto_backup"


def _python_modules() -> list[Path]:
    return sorted(INTEGRATION_DIR.rglob("*.py"))


def _json_files() -> list[Path]:
    return sorted(INTEGRATION_DIR.rglob("*.json"))


def _plancher_requires_python() -> tuple[int, ...]:
    """Plancher `>=X.Y[.Z]` déclaré par `requires-python` dans `pyproject.toml`.

    Le plancher est lu dans `pyproject.toml` plutôt que recopié ici : monter la
    cible Python du projet ne demande qu'une seule modification, et ce test
    contrôle bien ce qu'il annonce, à savoir la cohérence entre l'interpréteur
    courant et la version déclarée.
    """
    pyproject = tomllib.loads(
        (REPO_ROOT / "pyproject.toml").read_bytes().decode("utf-8")
    )
    requires_python = pyproject["project"]["requires-python"]
    correspondance = re.search(r">=\s*(\d+(?:\.\d+)*)", requires_python)
    assert correspondance, (
        f"requires-python doit déclarer un plancher '>=X.Y', trouvé {requires_python!r}"
    )
    return tuple(int(partie) for partie in correspondance.group(1).split("."))


def test_python_supporte_la_version_minimale() -> None:
    """L'environnement d'exécution respecte le `requires-python` du projet."""
    plancher = _plancher_requires_python()
    assert sys.version_info[: len(plancher)] >= plancher, (
        f"interpréteur {sys.version.split()[0]} sous le plancher déclaré "
        f">={'.'.join(map(str, plancher))}"
    )


def test_l_integration_est_presente() -> None:
    assert INTEGRATION_DIR.is_dir()
    assert (INTEGRATION_DIR / "manifest.json").is_file()
    assert _python_modules(), "aucun module Python trouvé dans l'intégration"


@pytest.mark.parametrize("module", _python_modules(), ids=lambda p: p.name)
def test_les_modules_compilent(module: Path, tmp_path: Path) -> None:
    """Chaque module de l'intégration compile avec l'interpréteur courant."""
    py_compile.compile(
        str(module), cfile=str(tmp_path / f"{module.stem}.pyc"), doraise=True
    )


@pytest.mark.parametrize("fichier", _json_files(), ids=lambda p: p.name)
def test_les_fichiers_json_sont_valides(fichier: Path) -> None:
    """Le manifeste et les traductions sont du JSON valide."""
    json.loads(fichier.read_text(encoding="utf-8"))


def test_le_manifeste_declare_le_domaine_attendu() -> None:
    manifeste = json.loads(
        (INTEGRATION_DIR / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifeste["domain"] == "auto_backup"
    assert manifeste["version"]
    assert manifeste["documentation"]
    assert manifeste["issue_tracker"]
