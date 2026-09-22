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
from difflib import SequenceMatcher
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

# Écarts volontaires du fork, tous documentés dans `docs/UPSTREAM.md`.
#
# - `FICHIERS_UPSTREAM_REECRITS` : fichiers dont le contenu diverge librement de
#   l'upstream. Ils sortent de la comparaison ligne à ligne, mais restent
#   contrôlés par ailleurs (pour `manifest.json`, par
#   `test_le_manifeste_porte_l_identite_du_fork`).
# - `FICHIERS_UPSTREAM_ETENDUS` : modules upstream que le fork complète. La
#   comparaison exige qu'ils ne contiennent **que des ajouts** : aucune ligne
#   upstream supprimée ni modifiée, ce qui garde les resynchronisations simples.
# - `REPERTOIRES_DU_FORK` : code propre au fork, absent de l'upstream.
FICHIERS_UPSTREAM_REECRITS = ("manifest.json",)
FICHIERS_UPSTREAM_ETENDUS = ("__init__.py", "const.py", "config_flow.py")
REPERTOIRES_DU_FORK = ("destinations",)


def _sha_upstream() -> str:
    """SHA de la révision upstream importée, lu dans `docs/UPSTREAM.md`."""
    texte = DOC_UPSTREAM.read_text(encoding="utf-8")
    correspondance = re.search(r"SHA import\S*\s*\|\s*`([0-9a-f]{40})`", texte)
    assert correspondance, "SHA upstream introuvable dans docs/UPSTREAM.md"
    return correspondance.group(1)


def _section_des_ecarts() -> str:
    """Contenu de la section « Écarts volontaires » de `docs/UPSTREAM.md`."""
    texte = DOC_UPSTREAM.read_text(encoding="utf-8")
    assert "Écarts volontaires" in texte, "section des écarts absente de UPSTREAM.md"
    return texte.split("Écarts volontaires", 1)[1].split("\n## ", 1)[0]


def _suppressions_upstream(upstream: Path, fork: Path) -> list[str]:
    """Décrit les lignes upstream supprimées ou modifiées par le fork."""
    lignes_upstream = upstream.read_text(encoding="utf-8").splitlines()
    lignes_fork = fork.read_text(encoding="utf-8").splitlines()
    comparaison = SequenceMatcher(None, lignes_upstream, lignes_fork, autojunk=False)
    return [
        f"lignes upstream {debut + 1}-{fin} ({operation}) : "
        + " / ".join(lignes_upstream[debut:fin])
        for operation, debut, fin, _, _ in comparaison.get_opcodes()
        if operation in {"delete", "replace"}
    ]


def _lignes_de_copyright(texte: str) -> list[str]:
    return [ligne.strip() for ligne in re.findall(r"^Copyright \(c\).*$", texte, re.M)]


@pytest.fixture
def gh_disponible() -> str:
    """Chemin du CLI `gh`, ou ignore le test s'il n'est pas installé."""
    chemin = shutil.which("gh")
    if chemin is None:
        pytest.skip("CLI `gh` introuvable : comparaison avec l'upstream impossible")
    return chemin


@pytest.fixture(scope="module")
def arborescence_upstream(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Télécharge la révision upstream importée et renvoie son `auto_backup/`."""
    gh = shutil.which("gh")
    if gh is None:
        pytest.skip("CLI `gh` introuvable : comparaison avec l'upstream impossible")

    sha = _sha_upstream()
    racine = tmp_path_factory.mktemp("upstream")
    archive = racine / "upstream.tar.gz"
    with archive.open("wb") as fichier:
        telechargement = subprocess.run(
            [gh, "api", f"repos/{DEPOT_UPSTREAM}/tarball/{sha}"],
            stdout=fichier,
            stderr=subprocess.PIPE,
            check=False,
        )
    assert telechargement.returncode == 0 and archive.stat().st_size > 0, (
        f"téléchargement du tarball upstream ({sha}) impossible : "
        f"{telechargement.stderr.decode(errors='replace')}"
    )

    extraction = racine / "extraction"
    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(extraction, filter="data")

    candidats = list(extraction.glob("*/custom_components/auto_backup"))
    assert candidats, "custom_components/auto_backup absent du tarball upstream"
    return candidats[0]


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
    arborescence_upstream: Path,
) -> None:
    """Le code importé est identique à l'upstream, hors écarts documentés.

    Les écarts exclus ici ne sortent pas du contrôle : `manifest.json` est
    vérifié par `test_le_manifeste_porte_l_identite_du_fork`, les modules
    étendus par `test_les_fichiers_upstream_etendus_ne_sont_que_completes`, et
    le code propre au fork par sa propre suite de tests.
    """
    exclusions = [
        f"--exclude={nom}"
        for nom in (
            "__pycache__",
            *FICHIERS_UPSTREAM_REECRITS,
            *FICHIERS_UPSTREAM_ETENDUS,
            *REPERTOIRES_DU_FORK,
        )
    ]
    difference = subprocess.run(
        [
            "diff",
            "-rq",
            *exclusions,
            str(arborescence_upstream),
            str(REPERTOIRE_INTEGRATION),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert difference.returncode == 0, (
        f"différences avec l'upstream :\n{difference.stdout}{difference.stderr}"
    )


@pytest.mark.network
def test_les_fichiers_upstream_etendus_ne_sont_que_completes(
    arborescence_upstream: Path,
) -> None:
    """Les modules upstream étendus par le fork ne subissent que des ajouts.

    Aucune ligne upstream ne doit être supprimée ni modifiée : la
    resynchronisation reste ainsi un report de diff, jamais un arbitrage.
    """
    for nom in FICHIERS_UPSTREAM_ETENDUS:
        upstream = arborescence_upstream / nom
        assert upstream.is_file(), f"{nom} absent de l'upstream"
        suppressions = _suppressions_upstream(upstream, REPERTOIRE_INTEGRATION / nom)
        assert not suppressions, (
            f"{nom} modifie ou supprime du code upstream :\n" + "\n".join(suppressions)
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


def test_les_ecarts_volontaires_sont_documentes() -> None:
    """Chaque écart avec l'upstream est listé dans `docs/UPSTREAM.md`."""
    ecarts = _section_des_ecarts()
    non_documentes = [
        nom
        for nom in (
            *FICHIERS_UPSTREAM_REECRITS,
            *FICHIERS_UPSTREAM_ETENDUS,
            *REPERTOIRES_DU_FORK,
        )
        if nom not in ecarts
    ]
    assert not non_documentes, (
        f"écarts non documentés dans docs/UPSTREAM.md : {non_documentes}"
    )


def test_le_code_du_fork_est_range_dans_ses_propres_sous_paquets() -> None:
    """Le code propre au fork vit dans des sous-paquets, pas à la racine."""
    for nom in REPERTOIRES_DU_FORK:
        assert (REPERTOIRE_INTEGRATION / nom / "__init__.py").is_file(), (
            f"le sous-paquet du fork « {nom} » est introuvable"
        )

    modules_racine = {fichier.name for fichier in REPERTOIRE_INTEGRATION.glob("*.py")}
    inattendus = modules_racine - set(FICHIERS_UPSTREAM_REQUIS)
    assert not inattendus, (
        "modules du fork à la racine de l'intégration, à déplacer dans un "
        f"sous-paquet dédié : {sorted(inattendus)}"
    )
