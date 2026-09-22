"""Garde-fous sur la configuration pytest du dépôt (issue #4)."""

from __future__ import annotations

import shlex
import tomllib
from itertools import pairwise
from pathlib import Path

RACINE_DEPOT = Path(__file__).resolve().parent.parent


def _options_pytest() -> dict:
    contenu = (RACINE_DEPOT / "pyproject.toml").read_bytes()
    return tomllib.loads(contenu.decode("utf-8"))["tool"]["pytest"]["ini_options"]


def test_la_couverture_de_l_integration_est_mesuree_et_affichee() -> None:
    """`addopts` mesure la couverture d'`auto_backup` et l'affiche au terminal."""
    addopts = shlex.split(_options_pytest()["addopts"])
    assert "--cov=custom_components/auto_backup" in addopts
    assert "--cov-report=term-missing" in addopts


def test_le_cache_pytest_reste_desactive() -> None:
    """`-p no:cacheprovider` reste actif : aucun cache pytest écrit dans le dépôt."""
    addopts = shlex.split(_options_pytest()["addopts"])
    assert ("-p", "no:cacheprovider") in pairwise(addopts)


def test_le_marqueur_reseau_est_declare() -> None:
    """Le marqueur `network` est déclaré pour éviter les avertissements pytest."""
    marqueurs = _options_pytest()["markers"]
    assert any(marqueur.startswith("network:") for marqueur in marqueurs)


def test_le_mode_asyncio_automatique_est_actif() -> None:
    """Les tests Home Assistant asynchrones s'écrivent sans décorateur."""
    assert _options_pytest()["asyncio_mode"] == "auto"
