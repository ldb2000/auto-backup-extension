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


### Traductions étendues par le fork (issue #7) ###

# Clés d'interface ajoutées par le fork au flux d'options. Elles doivent exister
# dans **toutes** les langues que le fork étend, faute de quoi Home Assistant
# afficherait l'identifiant technique de l'étape à l'utilisateur.
ETAPES_D_OPTIONS_DU_FORK = (
    "menu",
    "ajouter_destination",
    "identifiants",
    "autorisation",
    "destination",
    "reautoriser_destination",
    "supprimer_destination",
    "reglages_televersement",
)
LANGUES_ETENDUES = ("fr", "en")


def _traduction(langue: str) -> dict:
    fichier = INTEGRATION_DIR / "translations" / f"{langue}.json"
    return json.loads(fichier.read_text(encoding="utf-8"))


@pytest.mark.parametrize("langue", LANGUES_ETENDUES)
def test_les_traductions_conservent_les_cles_upstream(langue: str) -> None:
    """Les clés importées de l'upstream restent présentes et renseignées."""
    traduction = _traduction(langue)

    assert traduction["title"]
    assert traduction["config"]["step"]["user"]["title"]
    assert set(traduction["config"]["abort"]) >= {"single_instance", "missing_service"}
    assert set(traduction["options"]["step"]["init"]["data"]) == {
        "auto_purge",
        "backup_timeout",
    }


@pytest.mark.parametrize("langue", LANGUES_ETENDUES)
def test_les_traductions_couvrent_les_etapes_du_fork(langue: str) -> None:
    """Chaque étape ajoutée par le fork est traduite, titre et description."""
    etapes = _traduction(langue)["options"]["step"]

    manquantes = [nom for nom in ETAPES_D_OPTIONS_DU_FORK if nom not in etapes]
    assert not manquantes, f"étapes non traduites en {langue} : {manquantes}"

    for nom in ETAPES_D_OPTIONS_DU_FORK:
        assert etapes[nom].get("title"), f"{langue}/{nom} sans titre"
        assert etapes[nom].get("description"), f"{langue}/{nom} sans description"

    # Le menu nomme chacune de ses entrées, y compris le formulaire upstream.
    assert set(etapes["menu"]["menu_options"]) == {
        "ajouter_destination",
        "reautoriser_destination",
        "supprimer_destination",
        "reglages_televersement",
        "init",
    }


@pytest.mark.parametrize("langue", LANGUES_ETENDUES)
def test_le_probleme_de_reautorisation_est_traduit(langue: str) -> None:
    """Le problème créé quand un accès est révoqué est lisible par l'utilisateur."""
    probleme = _traduction(langue)["issues"]["reauthentification_requise"]

    assert "{nom}" in probleme["title"]
    assert "{nom}" in probleme["description"]
    assert "{fournisseur}" in probleme["description"]


def test_les_deux_langues_etendues_declarent_les_memes_cles() -> None:
    """Une clé ajoutée dans une langue doit l'être dans l'autre."""

    def _chemins(valeur: object, prefixe: str = "") -> set[str]:
        if not isinstance(valeur, dict):
            return {prefixe}
        return {
            chemin
            for cle, sous_valeur in valeur.items()
            for chemin in _chemins(sous_valeur, f"{prefixe}.{cle}")
        }

    assert _chemins(_traduction("fr")) == _chemins(_traduction("en"))
