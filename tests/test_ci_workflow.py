"""Garde-fous sur le workflow d'intégration continue (issue #5).

Ces tests lisent `.github/workflows/ci.yml` et vérifient les propriétés exigées par
les critères d'acceptation : déclencheurs, jobs `lint`/`tests`/`validate`, absence de
secret, permissions minimales, actions épinglées, cache des dépendances et annulation
des runs obsolètes. Ils ne remplacent pas l'exécution réelle du workflow sur GitHub
(`hassfest` et la validation HACS ne sont pas rejouables en local).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

RACINE_DEPOT = Path(__file__).resolve().parent.parent
CHEMIN_WORKFLOW = RACINE_DEPOT / ".github" / "workflows" / "ci.yml"

# Une action est considérée comme épinglée si la référence après « @ » est un tag de
# version (`v7.0.1`, `22.5.0`) ou un SHA de commit complet (40 caractères hexadécimaux).
REFERENCE_EPINGLEE = re.compile(r"^(v?\d+(?:\.\d+)*|[0-9a-f]{40})$")


def _workflow() -> dict[str, Any]:
    return yaml.safe_load(CHEMIN_WORKFLOW.read_text(encoding="utf-8"))


def _declencheurs(workflow: dict[str, Any]) -> dict[str, Any]:
    """Renvoie la section « on » du workflow.

    En YAML 1.1, la clé nue `on` est interprétée par PyYAML comme le booléen `True` :
    il faut donc accepter les deux formes pour lire les déclencheurs.
    """
    return workflow.get("on", workflow.get(True))


def _jobs(workflow: dict[str, Any]) -> dict[str, Any]:
    return workflow["jobs"]


def _commandes(job: dict[str, Any]) -> list[str]:
    return [etape["run"] for etape in job["steps"] if "run" in etape]


def _actions(job: dict[str, Any]) -> list[str]:
    return [etape["uses"] for etape in job["steps"] if "uses" in etape]


def _toutes_les_actions(workflow: dict[str, Any]) -> list[str]:
    return [action for job in _jobs(workflow).values() for action in _actions(job)]


def test_le_workflow_est_un_yaml_valide() -> None:
    """Le fichier de workflow existe et se parse."""
    assert CHEMIN_WORKFLOW.is_file(), f"{CHEMIN_WORKFLOW} est absent"
    assert isinstance(_workflow(), dict)


def test_le_workflow_se_declenche_sur_les_pr_et_les_push_vers_main() -> None:
    """Critère : toute PR vers `main` (et tout push sur `main`) déclenche la CI."""
    declencheurs = _declencheurs(_workflow())
    assert declencheurs["pull_request"]["branches"] == ["main"]
    assert declencheurs["push"]["branches"] == ["main"]


def test_les_trois_jobs_attendus_existent() -> None:
    assert {"lint", "tests", "validate"} <= set(_jobs(_workflow()))


def test_le_job_lint_execute_ruff_check_et_ruff_format() -> None:
    """Critère : le job « lint » exécute `ruff check` et `ruff format --check`."""
    commandes = _commandes(_jobs(_workflow())["lint"])
    assert "uv run ruff check ." in commandes
    assert "uv run ruff format --check ." in commandes


def test_le_job_lint_installe_les_dependances_de_dev() -> None:
    commandes = _commandes(_jobs(_workflow())["lint"])
    assert any(commande.startswith("uv sync --group dev") for commande in commandes)


def test_le_job_tests_execute_pytest_sur_la_version_python_du_projet() -> None:
    """Critère : le job « tests » exécute `pytest` sur la version Python du projet.

    La version vient de `.python-version`, qu'`uv python install` lit sans argument.
    """
    commandes = _commandes(_jobs(_workflow())["tests"])
    assert "uv run pytest" in commandes
    assert "uv python install" in commandes
    assert (RACINE_DEPOT / ".python-version").is_file()


def test_le_job_tests_ne_lance_pas_les_tests_reseau() -> None:
    """Décision documentée (docs/ci.md) : pas de `--tests-reseau` en CI."""
    commandes = _commandes(_jobs(_workflow())["tests"])
    assert not any("--tests-reseau" in commande for commande in commandes)


def test_le_job_validate_lance_hassfest_et_la_validation_hacs() -> None:
    """Critère : validation `hassfest` et HACS en catégorie `integration`."""
    job = _jobs(_workflow())["validate"]
    actions = _actions(job)
    prefixe_hassfest = "home-assistant/actions/hassfest@"
    assert any(action.startswith(prefixe_hassfest) for action in actions)
    assert any(action.startswith("hacs/action@") for action in actions)

    etape_hacs = next(
        etape
        for etape in job["steps"]
        if etape.get("uses", "").startswith("hacs/action@")
    )
    assert etape_hacs["with"]["category"] == "integration"


def test_les_permissions_sont_minimales_et_en_lecture_seule() -> None:
    """Critère : `contents: read` au niveau workflow, sans élargissement par job."""
    workflow = _workflow()
    assert workflow["permissions"] == {"contents": "read"}
    for nom, job in _jobs(workflow).items():
        assert "permissions" not in job, f"le job {nom} redéfinit les permissions"


def test_le_workflow_n_utilise_aucun_secret() -> None:
    """Critère : aucun secret n'est requis par la CI."""
    contenu = CHEMIN_WORKFLOW.read_text(encoding="utf-8")
    assert "secrets." not in contenu


def test_toutes_les_actions_sont_epinglees_a_une_version() -> None:
    """Critère : aucune action flottante (`@main`, `@master`, branche)."""
    for action in _toutes_les_actions(_workflow()):
        assert "@" in action, f"action sans référence : {action}"
        reference = action.rsplit("@", 1)[1]
        assert REFERENCE_EPINGLEE.match(reference), (
            f"action non épinglée à une version ou un SHA : {action}"
        )


def test_le_cache_des_dependances_python_est_actif() -> None:
    """Critère : les dépendances Python sont mises en cache."""
    jobs = _jobs(_workflow())
    for nom in ("lint", "tests"):
        etapes_uv = [
            etape
            for etape in jobs[nom]["steps"]
            if etape.get("uses", "").startswith("astral-sh/setup-uv@")
        ]
        assert etapes_uv, f"le job {nom} n'installe pas uv"
        assert all(etape["with"]["enable-cache"] is True for etape in etapes_uv), (
            f"le cache uv n'est pas activé dans le job {nom}"
        )


def test_les_runs_obsoletes_d_une_meme_pr_sont_annules() -> None:
    """Critère : `concurrency` annule les runs devenus obsolètes sur une PR."""
    concurrency = _workflow()["concurrency"]
    assert "github.ref" in concurrency["group"]
    annulation = concurrency["cancel-in-progress"]
    # Soit l'annulation est inconditionnelle, soit elle est conditionnée aux PR.
    assert annulation is True or "pull_request" in str(annulation)


def test_le_workflow_des_agents_n_est_pas_impacte() -> None:
    """Le workflow `claude-agents.yml` (désactivé) reste présent et distinct."""
    agents = RACINE_DEPOT / ".github" / "workflows" / "claude-agents.yml"
    assert agents.is_file()


def test_le_readme_affiche_le_badge_ci() -> None:
    contenu = (RACINE_DEPOT / "README.md").read_text(encoding="utf-8")
    assert "actions/workflows/ci.yml/badge.svg" in contenu


def test_la_page_de_documentation_ci_existe() -> None:
    assert (RACINE_DEPOT / "docs" / "ci.md").is_file()
