"""Non-régression sur l'import de l'upstream (critères de l'issue #2).

Ces vérifications proviennent du script ad hoc `tests/check_issue_2.py`, écrit
avant l'arrivée de pytest (issue #4) et remplacé par ce module. Les contrôles
hors ligne s'exécutent toujours ; les deux comparaisons avec le dépôt upstream
passent par le CLI `gh` et sont marquées `network` : elles ne tournent qu'avec
`uv run pytest --tests-reseau`.
"""

from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

RACINE_DEPOT = Path(__file__).resolve().parent.parent
REPERTOIRE_INTEGRATION = RACINE_DEPOT / "custom_components" / "auto_backup"
DOC_UPSTREAM = RACINE_DEPOT / "docs" / "UPSTREAM.md"
DEPOT_UPSTREAM = "jcwillox/hass-auto-backup"

FICHIERS_UPSTREAM_REQUIS = (
    "__init__.py",
    "manager.py",
    "handlers.py",
    "config_flow.py",
    "const.py",
    "helpers.py",
    "sensor.py",
    "binary_sensor.py",
    "button.py",
    "services.yaml",
    "manifest.json",
)


def _sha_upstream() -> str:
    """SHA de la révision upstream importée, lu dans `docs/UPSTREAM.md`."""
    texte = DOC_UPSTREAM.read_text(encoding="utf-8")
    correspondance = re.search(r"SHA import\S*\s*\|\s*`([0-9a-f]{40})`", texte)
    assert correspondance, "SHA upstream introuvable dans docs/UPSTREAM.md"
    return correspondance.group(1)


def _lignes_de_copyright(texte: str) -> list[str]:
    return [ligne.strip() for ligne in re.findall(r"^Copyright \(c\).*$", texte, re.M)]


@pytest.fixture
def gh_disponible() -> str:
    """Chemin du CLI `gh`, ou ignore le test s'il n'est pas installé."""
    chemin = shutil.which("gh")
    if chemin is None:
        pytest.skip("CLI `gh` introuvable : comparaison avec l'upstream impossible")
    return chemin


def test_les_fichiers_upstream_sont_presents() -> None:
    """Tous les modules importés de l'upstream sont livrés, traductions comprises."""
    manquants = [
        nom
        for nom in FICHIERS_UPSTREAM_REQUIS
        if not (REPERTOIRE_INTEGRATION / nom).is_file()
    ]
    assert not manquants, f"fichiers upstream manquants : {manquants}"

    traductions = REPERTOIRE_INTEGRATION / "translations"
    assert traductions.is_dir()
    assert list(traductions.glob("*.json")), "aucune traduction livrée"


def test_la_licence_mit_porte_les_deux_copyrights() -> None:
    """`LICENSE` reste la licence MIT, avec le copyright upstream et celui du fork."""
    texte = (RACINE_DEPOT / "LICENSE").read_text(encoding="utf-8")
    assert "MIT License" in texte
    assert "Permission is hereby granted" in texte
    assert len(_lignes_de_copyright(texte)) >= 2, (
        "LICENSE doit conserver le copyright upstream et ajouter celui du fork"
    )


def test_le_readme_documente_le_fork() -> None:
    """Le README annonce le fork, sa licence et l'objectif Dropbox / Google Drive."""
    texte = (RACINE_DEPOT / "README.md").read_text(encoding="utf-8")
    attendus = {
        "mention du fork": re.search(r"\bfork\b", texte, re.IGNORECASE) is not None,
        "lien upstream": f"https://github.com/{DEPOT_UPSTREAM}" in texte,
        "licence MIT": "MIT" in texte,
        "Dropbox": re.search(r"\bDropbox\b", texte) is not None,
        "Google Drive": re.search(r"\bGoogle Drive\b", texte) is not None,
    }
    manquants = [libelle for libelle, present in attendus.items() if not present]
    assert not manquants, f"éléments absents du README.md : {manquants}"


def test_la_doc_upstream_trace_la_revision_et_la_procedure() -> None:
    """`docs/UPSTREAM.md` fixe la révision importée et décrit la resynchronisation."""
    texte = DOC_UPSTREAM.read_text(encoding="utf-8")
    assert _sha_upstream()
    assert re.search(r"Date de l'import.*?\|\s*(\d{4}-\d{2}-\d{2})", texte), (
        "date d'import introuvable (format AAAA-MM-JJ attendu)"
    )
    assert "Procédure de resynchronisation" in texte
    procedure = texte.split("Procédure de resynchronisation", 1)[1].strip()
    assert len(procedure) > 50, "la procédure de resynchronisation semble vide"


def test_le_manifeste_porte_l_identite_du_fork() -> None:
    """Le manifeste garde le domaine upstream et pointe vers le dépôt du fork."""
    manifeste = json.loads(
        (REPERTOIRE_INTEGRATION / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifeste["domain"] == "auto_backup"
    assert "ldb2000/auto-backup-extension" in manifeste["documentation"]
    assert "ldb2000/auto-backup-extension" in manifeste["issue_tracker"]
    assert any(
        "ldb2000" in str(proprietaire) for proprietaire in manifeste["codeowners"]
    )


def test_hacs_json_est_exploitable() -> None:
    """`hacs.json` déclare un nom et une version minimale de Home Assistant."""
    hacs = json.loads((RACINE_DEPOT / "hacs.json").read_text(encoding="utf-8"))
    assert hacs.get("name")
    assert re.match(r"^\d+\.\d+(\.\d+)?$", str(hacs.get("homeassistant", "")))


@pytest.mark.network
def test_le_code_importe_est_identique_a_l_upstream(
    gh_disponible: str, tmp_path: Path
) -> None:
    """Le répertoire importé est identique à la révision upstream, hors manifeste.

    Seul `manifest.json` diverge volontairement (identité du fork), cf.
    `docs/UPSTREAM.md`.
    """
    sha = _sha_upstream()
    archive = tmp_path / "upstream.tar.gz"
    with archive.open("wb") as fichier:
        telechargement = subprocess.run(
            [gh_disponible, "api", f"repos/{DEPOT_UPSTREAM}/tarball/{sha}"],
            stdout=fichier,
            stderr=subprocess.PIPE,
            check=False,
        )
    assert telechargement.returncode == 0 and archive.stat().st_size > 0, (
        f"téléchargement du tarball upstream ({sha}) impossible : "
        f"{telechargement.stderr.decode(errors='replace')}"
    )

    extraction = tmp_path / "upstream"
    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(extraction, filter="data")

    candidats = list(extraction.glob("*/custom_components/auto_backup"))
    assert candidats, "custom_components/auto_backup absent du tarball upstream"

    difference = subprocess.run(
        [
            "diff",
            "-rq",
            "--exclude=manifest.json",
            "--exclude=__pycache__",
            str(candidats[0]),
            str(REPERTOIRE_INTEGRATION),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert difference.returncode == 0, (
        f"différences avec l'upstream ({sha}) :\n{difference.stdout}{difference.stderr}"
    )


@pytest.mark.network
def test_le_copyright_upstream_est_conserve(gh_disponible: str) -> None:
    """La ligne de copyright de la LICENSE upstream est toujours présente."""
    sha = _sha_upstream()
    recuperation = subprocess.run(
        [
            gh_disponible,
            "api",
            f"repos/{DEPOT_UPSTREAM}/contents/LICENSE?ref={sha}",
            "--jq",
            ".content",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert recuperation.returncode == 0 and recuperation.stdout.strip(), (
        f"LICENSE upstream ({sha}) illisible : {recuperation.stderr}"
    )

    licence_upstream = base64.b64decode(recuperation.stdout.strip()).decode(
        "utf-8", errors="replace"
    )
    copyrights_upstream = _lignes_de_copyright(licence_upstream)
    assert copyrights_upstream, "aucun copyright dans la LICENSE upstream"

    copyrights_fork = _lignes_de_copyright(
        (RACINE_DEPOT / "LICENSE").read_text(encoding="utf-8")
    )
    assert copyrights_upstream[0] in copyrights_fork, (
        f"copyright upstream absent de LICENSE : attendu {copyrights_upstream[0]!r}, "
        f"trouvé {copyrights_fork}"
    )
    assert [ligne for ligne in copyrights_fork if ligne != copyrights_upstream[0]], (
        "LICENSE ne porte aucun copyright propre au fork"
    )
