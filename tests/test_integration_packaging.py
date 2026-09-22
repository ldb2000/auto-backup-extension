"""Vérifications de base sur l'intégration livrée et sur l'outillage Python.

Ces tests ne dépendent pas de Home Assistant : ils garantissent que l'environnement
de développement (interpréteur, collecte pytest) est fonctionnel et que les fichiers
livrés dans `custom_components/auto_backup/` restent valides. Les tests fonctionnels
de l'intégration sont traités séparément.
"""

from __future__ import annotations

import json
import py_compile
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INTEGRATION_DIR = REPO_ROOT / "custom_components" / "auto_backup"


def _python_modules() -> list[Path]:
    return sorted(INTEGRATION_DIR.rglob("*.py"))


def _json_files() -> list[Path]:
    return sorted(INTEGRATION_DIR.rglob("*.json"))


def test_python_supporte_la_version_minimale() -> None:
    """L'environnement d'exécution respecte le `requires-python` du projet."""
    assert sys.version_info >= (3, 13, 2)


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
