#!/usr/bin/env python3
"""Script de vérification des critères d'acceptation de l'issue #2.

Cette issue n'a pas encore d'outillage de tests dédié (pytest arrive avec
l'issue #4) : ce script Python 3 (bibliothèque standard uniquement) prouve
chaque critère Given-When-Then de l'issue #2 en inspectant le dépôt et, pour
la comparaison à l'identique avec l'upstream, en téléchargeant le tarball du
SHA figé dans docs/UPSTREAM.md via le CLI `gh` (déjà requis par la procédure
de resynchronisation documentée dans ce même fichier).

Usage :
    python3 tests/check_issue_2.py

Code de sortie : 0 si tous les critères sont validés, 1 sinon.
"""

from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
AUTO_BACKUP_DIR = REPO_ROOT / "custom_components" / "auto_backup"
UPSTREAM_DOC = REPO_ROOT / "docs" / "UPSTREAM.md"
LICENSE_FILE = REPO_ROOT / "LICENSE"
README_FILE = REPO_ROOT / "README.md"
MANIFEST_FILE = AUTO_BACKUP_DIR / "manifest.json"
HACS_FILE = REPO_ROOT / "hacs.json"
UPSTREAM_REPO = "jcwillox/hass-auto-backup"

REQUIRED_FILES = [
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
]

results: list[tuple[str, bool, str]] = []


def record(label: str, ok: bool, detail: str = "") -> None:
    results.append((label, ok, detail))
    print(f"[{'OK ' if ok else 'KO '}] {label}")
    for line in detail.splitlines():
        print(f"        {line}")


def run(cmd: list[str]):
    return subprocess.run(cmd, capture_output=True, text=True)


def gh_available() -> bool:
    return shutil.which("gh") is not None


def extract_sha() -> str | None:
    if not UPSTREAM_DOC.is_file():
        return None
    text = UPSTREAM_DOC.read_text(encoding="utf-8")
    m = re.search(r"SHA import\S*\s*\|\s*`([0-9a-f]{40})`", text)
    return m.group(1) if m else None


def check_criterion_1() -> None:
    label = "Critère 1 - modules upstream présents et identiques (hors manifest.json)"
    missing = [f for f in REQUIRED_FILES if not (AUTO_BACKUP_DIR / f).is_file()]
    translations_dir = AUTO_BACKUP_DIR / "translations"
    if not translations_dir.is_dir() or not any(translations_dir.glob("*.json")):
        missing.append("translations/ (répertoire absent ou vide)")
    if missing:
        record(label, False, "Fichiers/répertoires manquants : " + ", ".join(missing))
        return

    if not gh_available():
        record(
            label,
            False,
            "CLI `gh` introuvable : impossible de comparer au tarball upstream",
        )
        return

    sha = extract_sha()
    if not sha:
        record(label, False, "SHA upstream introuvable dans docs/UPSTREAM.md")
        return

    with tempfile.TemporaryDirectory(prefix="upstream-issue2-") as tmp:
        tmp_path = Path(tmp)
        tarball_path = tmp_path / "upstream.tar.gz"
        with open(tarball_path, "wb") as fh:
            proc = subprocess.run(
                ["gh", "api", f"repos/{UPSTREAM_REPO}/tarball/{sha}"],
                stdout=fh,
                stderr=subprocess.PIPE,
            )
        if proc.returncode != 0 or tarball_path.stat().st_size == 0:
            record(
                label,
                False,
                f"Échec du téléchargement du tarball upstream (SHA {sha}) : "
                f"{proc.stderr.decode(errors='replace')}",
            )
            return

        try:
            with tarfile.open(tarball_path, "r:gz") as tf:
                tf.extractall(tmp_path)
        except tarfile.TarError as exc:
            record(label, False, f"Tarball upstream invalide : {exc}")
            return

        candidates = list(tmp_path.glob("*/custom_components/auto_backup"))
        if not candidates:
            record(
                label,
                False,
                "custom_components/auto_backup introuvable dans le tarball upstream",
            )
            return
        upstream_dir = candidates[0]

        diff = run(
            [
                "diff",
                "-rq",
                "--exclude=manifest.json",
                str(upstream_dir),
                str(AUTO_BACKUP_DIR),
            ]
        )
        if diff.returncode != 0:
            record(
                label,
                False,
                f"Différences avec l'upstream (SHA {sha}) :\n"
                f"{diff.stdout}{diff.stderr}",
            )
            return

        record(
            label,
            True,
            f"Tous les fichiers requis sont présents et strictement identiques "
            f"au tarball upstream (SHA {sha}), hors manifest.json.",
        )


def check_criterion_2() -> None:
    label = "Critère 2 - LICENSE (MIT, copyright upstream + fork)"
    if not LICENSE_FILE.is_file():
        record(label, False, "LICENSE introuvable")
        return
    text = LICENSE_FILE.read_text(encoding="utf-8")
    if "MIT License" not in text:
        record(label, False, "Le texte 'MIT License' est absent de LICENSE")
        return
    if "Permission is hereby granted" not in text:
        record(
            label,
            False,
            "La clause 'Permission is hereby granted' est absente de LICENSE",
        )
        return
    copyright_lines = [
        ligne.strip()
        for ligne in re.findall(r"^Copyright \(c\).*$", text, re.MULTILINE)
    ]
    if len(copyright_lines) < 2:
        record(
            label,
            False,
            f"Moins de deux lignes de copyright trouvées : {copyright_lines}",
        )
        return

    if not gh_available():
        record(
            label,
            False,
            "CLI `gh` introuvable : impossible de vérifier "
            "le copyright upstream d'origine",
        )
        return
    sha = extract_sha()
    if not sha:
        record(label, False, "SHA upstream introuvable dans docs/UPSTREAM.md")
        return

    proc = run(
        [
            "gh",
            "api",
            f"repos/{UPSTREAM_REPO}/contents/LICENSE?ref={sha}",
            "--jq",
            ".content",
        ]
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        record(
            label,
            False,
            f"Impossible de récupérer LICENSE upstream (SHA {sha}) : {proc.stderr}",
        )
        return
    upstream_license = base64.b64decode(proc.stdout.strip()).decode(
        "utf-8", errors="replace"
    )
    m = re.search(r"^Copyright \(c\).*$", upstream_license, re.MULTILINE)
    if not m:
        record(
            label, False, "Aucune ligne de copyright trouvée dans la LICENSE upstream"
        )
        return
    upstream_copyright = m.group(0).strip()

    if upstream_copyright not in copyright_lines:
        record(
            label,
            False,
            f"Le copyright upstream d'origine est absent de LICENSE :\n"
            f"  attendu : {upstream_copyright}\n  trouvé  : {copyright_lines}",
        )
        return

    fork_lines = [ligne for ligne in copyright_lines if ligne != upstream_copyright]
    if not fork_lines:
        record(
            label,
            False,
            "Aucune ligne de copyright distincte pour le fork trouvée dans LICENSE",
        )
        return

    record(label, True, "Lignes de copyright : " + " | ".join(copyright_lines))


def check_criterion_3() -> None:
    label = "Critère 3 - section fork dans README.md"
    if not README_FILE.is_file():
        record(label, False, "README.md introuvable")
        return
    text = README_FILE.read_text(encoding="utf-8")
    checks = {
        "mentionne 'fork'": re.search(r"\bfork\b", text, re.IGNORECASE) is not None,
        "lien vers https://github.com/jcwillox/hass-auto-backup": "https://github.com/jcwillox/hass-auto-backup"
        in text,
        "mentionne la licence MIT": "MIT" in text,
        "mentionne Dropbox": re.search(r"\bDropbox\b", text) is not None,
        "mentionne Google Drive": re.search(r"\bGoogle Drive\b", text) is not None,
    }
    missing = [k for k, v in checks.items() if not v]
    if missing:
        record(
            label, False, "Éléments manquants dans README.md : " + ", ".join(missing)
        )
        return
    record(
        label,
        True,
        "Section fork trouvée : lien upstream, licence MIT, "
        "Dropbox et Google Drive mentionnés.",
    )


def check_criterion_4() -> None:
    label = "Critère 4 - docs/UPSTREAM.md (SHA, date, procédure)"
    if not UPSTREAM_DOC.is_file():
        record(label, False, "docs/UPSTREAM.md introuvable")
        return
    text = UPSTREAM_DOC.read_text(encoding="utf-8")
    sha = extract_sha()
    if not sha:
        record(
            label,
            False,
            "SHA upstream introuvable "
            "(attendu un SHA git de 40 caractères hexadécimaux)",
        )
        return
    date_match = re.search(r"Date de l'import.*?\|\s*(\d{4}-\d{2}-\d{2})", text)
    if not date_match:
        record(label, False, "Date d'import introuvable (format AAAA-MM-JJ attendu)")
        return
    if "Procédure de resynchronisation" not in text:
        record(label, False, "Section 'Procédure de resynchronisation' introuvable")
        return
    proc_section = text.split("Procédure de resynchronisation", 1)[1].strip()
    if len(proc_section) < 50:
        record(label, False, "La section de procédure de resynchronisation semble vide")
        return
    record(
        label,
        True,
        f"SHA={sha}, date d'import={date_match.group(1)}, "
        f"section de procédure présente ({len(proc_section)} caractères).",
    )


def check_criterion_5() -> None:
    label = "Critère 5 - champs de manifest.json"
    if not MANIFEST_FILE.is_file():
        record(label, False, "manifest.json introuvable")
        return
    try:
        manifest = json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        record(label, False, f"manifest.json invalide : {exc}")
        return

    problems = []
    if manifest.get("domain") != "auto_backup":
        problems.append(f"domain={manifest.get('domain')!r} (attendu 'auto_backup')")
    if manifest.get("version") != "0.0.0":
        problems.append(f"version={manifest.get('version')!r} (attendu '0.0.0')")
    documentation = manifest.get("documentation", "")
    if "ldb2000/auto-backup-extension" not in documentation:
        problems.append(
            f"documentation={documentation!r} ne pointe pas vers "
            "ldb2000/auto-backup-extension"
        )
    issue_tracker = manifest.get("issue_tracker", "")
    if "ldb2000/auto-backup-extension" not in issue_tracker:
        problems.append(
            f"issue_tracker={issue_tracker!r} ne pointe pas vers "
            "ldb2000/auto-backup-extension"
        )
    codeowners = manifest.get("codeowners", [])
    if not isinstance(codeowners, list) or not any(
        "ldb2000" in str(c) for c in codeowners
    ):
        problems.append(f"codeowners={codeowners!r} ne référence pas ldb2000")

    if problems:
        record(label, False, "; ".join(problems))
        return
    record(
        label,
        True,
        json.dumps(
            {
                k: manifest.get(k)
                for k in (
                    "domain",
                    "version",
                    "documentation",
                    "issue_tracker",
                    "codeowners",
                )
            },
            ensure_ascii=False,
        ),
    )


def check_criterion_6() -> None:
    label = "Critère 6 - hacs.json (nom + version minimale Home Assistant)"
    if not HACS_FILE.is_file():
        record(label, False, "hacs.json introuvable")
        return
    try:
        hacs = json.loads(HACS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        record(label, False, f"hacs.json invalide : {exc}")
        return
    problems = []
    name = hacs.get("name")
    if not name or not isinstance(name, str):
        problems.append("champ 'name' absent ou vide")
    ha_version = hacs.get("homeassistant")
    if not ha_version or not re.match(r"^\d+\.\d+(\.\d+)?$", str(ha_version)):
        problems.append(
            f"champ 'homeassistant' absent ou de format invalide : {ha_version!r}"
        )
    if problems:
        record(label, False, "; ".join(problems))
        return
    record(label, True, f"name={name!r}, homeassistant={ha_version!r}")


def check_criterion_7() -> None:
    label = "Critère 7 - python3 -m compileall custom_components/auto_backup"
    if not AUTO_BACKUP_DIR.is_dir():
        record(label, False, "custom_components/auto_backup introuvable")
        return
    proc = run([sys.executable, "-m", "compileall", "-q", str(AUTO_BACKUP_DIR)])
    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        record(
            label, False, f"compileall a échoué (code {proc.returncode}) :\n{output}"
        )
        return
    record(label, True, "Aucune erreur de compilation (compileall -q).")


def main() -> int:
    print("Vérification des critères d'acceptation de l'issue #2\n")
    check_criterion_1()
    check_criterion_2()
    check_criterion_3()
    check_criterion_4()
    check_criterion_5()
    check_criterion_6()
    check_criterion_7()

    print()
    ok_count = sum(1 for _, ok, _ in results if ok)
    print(f"{ok_count}/{len(results)} critères validés")
    return 0 if ok_count == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
