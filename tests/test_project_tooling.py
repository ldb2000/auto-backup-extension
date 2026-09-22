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


def test_pyproject_est_parsable_avec_tomllib() -> None:
    """`pyproject.toml` est un TOML valide et déclare un nom de projet."""
    donnees = _pyproject()
    assert donnees["project"]["name"]


def test_requires_python_vise_au_moins_3_13() -> None:
    donnees = _pyproject()
    requires_python = donnees["project"]["requires-python"]
    assert requires_python.startswith(">=3.13"), (
        f"requires-python doit commencer par '>=3.13', trouvé {requires_python!r}"
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
