"""Vérifications de l'outillage Python du projet (issue #3).

Ces tests contrôlent que la stack Python (`pyproject.toml`, `.gitignore`,
`CLAUDE.md`, `README.md`, `scripts/agents-run.sh`) est cohérente et documentée,
sans dépendre de la sortie de commandes externes (`uv`, `ruff`) pour rester
stables en CI. Ils ne remplacent pas l'exécution de `uv run ruff check .`,
`uv run ruff format --check .` et `uv run pytest`, qui restent la source de
vérité pour le critère « les commandes se terminent en succès ».
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Paquets attendus dans le groupe de dépendances de dev (cf. critère d'acceptation
# de l'issue #3 : pytest, pytest-asyncio, pytest-homeassistant-custom-component,
# ruff, ainsi que pytest-cov ajouté par le codeur pour la couverture de tests).
PAQUETS_DEV_ATTENDUS = {
    "pytest",
    "pytest-asyncio",
    "pytest-cov",
    "pytest-homeassistant-custom-component",
    "ruff",
}

# Plancher minimal exigé par l'issue #3 : `requires-python >= 3.13`. Le projet peut
# viser plus haut (Home Assistant >= 2026.3 exige Python >= 3.14.2) sans casser ce test.
PLANCHER_PYTHON_MINIMAL = (3, 13)

ENTREES_GITIGNORE_ATTENDUES = {
    ".venv/",
    "__pycache__/",
    ".pytest_cache/",
    ".ruff_cache/",
}


def _nom_paquet(specificateur: str) -> str:
    """Extrait le nom d'un paquet d'un specificateur PEP 508 (ignore la version)."""
    correspondance = re.match(r"^[A-Za-z0-9_.-]+", specificateur)
    assert correspondance, f"specificateur de dépendance illisible : {specificateur!r}"
    return correspondance.group(0)


def _pyproject() -> dict:
    contenu = (REPO_ROOT / "pyproject.toml").read_bytes()
    return tomllib.loads(contenu.decode("utf-8"))


def _plancher_requires_python(requires_python: str) -> tuple[int, ...]:
    """Renvoie le plancher `>=X.Y[.Z]` de `requires-python` sous forme de tuple.

    Le contrôle est sémantique (comparaison de tuples) et non textuel : monter la
    cible Python du projet (3.13 -> 3.14, etc.) ne doit pas casser ce test, seule
    une régression sous le plancher minimal soutenu doit le faire échouer.
    """
    correspondance = re.search(r">=\s*(\d+(?:\.\d+)*)", requires_python)
    assert correspondance, (
        f"requires-python doit déclarer un plancher '>=X.Y', trouvé {requires_python!r}"
    )
    return tuple(int(partie) for partie in correspondance.group(1).split("."))


def test_pyproject_est_parsable_avec_tomllib() -> None:
    """`pyproject.toml` est un TOML valide et déclare un nom de projet."""
    donnees = _pyproject()
    assert donnees["project"]["name"]


def test_requires_python_vise_au_moins_3_13() -> None:
    """Le plancher `requires-python` ne descend pas sous Python 3.13.

    Critère d'acceptation de l'issue #3 : `requires-python >= 3.13`. Le projet
    cible aujourd'hui une version plus récente (cf. `.python-version`), ce que ce
    test autorise explicitement.
    """
    requires_python = _pyproject()["project"]["requires-python"]
    plancher = _plancher_requires_python(requires_python)
    minimal = ".".join(str(partie) for partie in PLANCHER_PYTHON_MINIMAL)
    assert plancher >= PLANCHER_PYTHON_MINIMAL, (
        f"requires-python doit valoir au moins >={minimal}, trouvé {requires_python!r}"
    )


def test_les_dependances_runtime_de_l_integration_sont_declarees() -> None:
    """`homeassistant` (dépendance de l'intégration) est en dépendance runtime."""
    donnees = _pyproject()
    dependances = {_nom_paquet(d) for d in donnees["project"]["dependencies"]}
    assert "homeassistant" in dependances


def test_le_groupe_dev_contient_les_paquets_de_test_et_de_lint() -> None:
    donnees = _pyproject()
    groupe_dev = donnees["dependency-groups"]["dev"]
    paquets_dev = {_nom_paquet(d) for d in groupe_dev}
    manquants = PAQUETS_DEV_ATTENDUS - paquets_dev
    assert not manquants, f"paquets dev manquants dans pyproject.toml : {manquants}"


def test_gitignore_ignore_les_artefacts_python() -> None:
    contenu = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    lignes = {ligne.strip() for ligne in contenu.splitlines()}
    manquantes = ENTREES_GITIGNORE_ATTENDUES - lignes
    assert not manquantes, f"entrées manquantes dans .gitignore : {manquantes}"


def test_claude_md_ne_reference_pas_npm() -> None:
    contenu = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    assert "npm" not in contenu.lower()


def test_readme_ne_reference_pas_npm() -> None:
    contenu = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "npm" not in contenu.lower()


def test_agents_run_sh_installe_via_uv_et_pas_npm() -> None:
    contenu = (REPO_ROOT / "scripts" / "agents-run.sh").read_text(encoding="utf-8")
    assert "npm" not in contenu.lower()
    correspondance = re.search(r"CMD_INSTALL=(['\"])(.*?)\1", contenu)
    assert correspondance, "variable CMD_INSTALL introuvable dans scripts/agents-run.sh"
    assert correspondance.group(2) == "uv sync --group dev"
