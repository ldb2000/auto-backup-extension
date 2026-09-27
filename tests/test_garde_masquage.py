"""Garde du point unique de masquage (issue #35).

`destinations/masquage.py` est le **seul** endroit du fork qui sait reconnaître
un secret. Un masquage artisanal ailleurs serait une faille en soi : il finirait
par couvrir moins que l'original — c'est exactement ce qui était arrivé avant
#17, avec deux masquages aux couvertures différentes. Ce module échoue donc :

- si une expression régulière visant un mot-clé de secret (`token`, `secret`,
  `bearer`, `upload_id`, `password`…) apparaît dans le code du fork ailleurs que
  dans `destinations/masquage.py` ;
- si un appel de journalisation du fork relaie une trace brute
  (`_LOGGER.exception()`, `exc_info=`) plutôt que `journaliser_une_exception()` ;
- si un appel de journalisation relaie une exception rattrapée sans la passer
  par `masquer()`.

**Périmètre : le code du fork seulement.** Les modules à la racine de
`custom_components/auto_backup/` sont le code upstream importé à l'identique
(`docs/UPSTREAM.md`) : le fork ne les modifie pas, et ne peut donc ni y
retirer un masquage ni y en ajouter un. Ils sont exclus **nommément**, par la
liste de `tests/test_conformite_upstream.py`, et non par un motif : un module du
fork glissé à la racine ne serait pas exempté en silence. `tests/` est exclu
aussi : les tests portent des vecteurs de secrets, pas du masquage.

Chaque détecteur est d'abord éprouvé sur un code synthétique qui doit le
déclencher, puis sur `masquage.py` lui-même : une garde qui ne voit rien nulle
part ne prouverait rien.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from test_conformite_upstream import FICHIERS_UPSTREAM_REQUIS

RACINE_DEPOT = Path(__file__).resolve().parent.parent
REPERTOIRE_INTEGRATION = RACINE_DEPOT / "custom_components" / "auto_backup"
MODULE_DE_MASQUAGE = REPERTOIRE_INTEGRATION / "destinations" / "masquage.py"

# Modules upstream, hors d'atteinte du fork (cf. `docs/UPSTREAM.md`).
MODULES_UPSTREAM = frozenset(
    nom for nom in FICHIERS_UPSTREAM_REQUIS if nom.endswith(".py")
)

# Mots-clés d'un secret, tels qu'un masquage artisanal les viserait.
_MOT_CLE_DE_SECRET = re.compile(
    r"(?i)token|secret|bearer|authori[sz]ation|passw(?:or)?d|upload.{0,3}id"
    r"|api.{0,3}key|client.{0,3}id|ya29|sl\\\."
)

# Syntaxe propre à une expression régulière : une phrase ordinaire (message
# d'erreur, docstring) n'en contient pas, un motif de masquage si.
_SYNTAXE_DE_MOTIF = re.compile(
    r"\(\?|\\[sSbBwWdD]|\[\^|\][*+?{]|\.[*+]|\[_-\]|\[A-Za-z|\{\d+,\d*\}"
)

_NIVEAUX_DE_JOURNAL = frozenset(
    {"debug", "info", "warning", "warn", "error", "critical", "exception", "log"}
)
_FONCTIONS_DE_MASQUAGE = frozenset({"masquer", "masquer_un_nom"})


### Détecteurs ###


def _documentation(arbre: ast.AST) -> set[int]:
    """Identifiants des chaînes qui ne sont que de la documentation.

    Une docstring, ou toute chaîne posée seule comme instruction, n'est jamais
    évaluée : elle peut citer un motif pour l'expliquer sans rien masquer.
    """
    return {
        id(noeud.value)
        for noeud in ast.walk(arbre)
        if isinstance(noeud, ast.Expr) and isinstance(noeud.value, ast.Constant)
    }


def motifs_de_masquage(source: str) -> list[tuple[int, str]]:
    """Littéraux qui ont l'allure d'une expression régulière visant un secret."""
    arbre = ast.parse(source)
    documentation = _documentation(arbre)
    trouves = []
    for noeud in ast.walk(arbre):
        if not isinstance(noeud, ast.Constant) or not isinstance(noeud.value, str):
            continue
        if id(noeud) in documentation:
            continue
        texte = noeud.value
        if _MOT_CLE_DE_SECRET.search(texte) and _SYNTAXE_DE_MOTIF.search(texte):
            trouves.append((noeud.lineno, texte))
    return trouves


def _est_un_appel_de_journal(noeud: ast.AST) -> bool:
    """`_LOGGER.error(...)`, `journal.debug(...)`, `logging.exception(...)`…"""
    return (
        isinstance(noeud, ast.Call)
        and isinstance(noeud.func, ast.Attribute)
        and noeud.func.attr in _NIVEAUX_DE_JOURNAL
        and isinstance(noeud.func.value, ast.Name)
        and (
            noeud.func.value.id.upper().endswith("LOGGER")
            or noeud.func.value.id in {"logging", "journal"}
        )
    )


def traces_brutes(source: str) -> list[tuple[int, str]]:
    """Appels de journal qui recopieraient une trace brute."""
    trouves = []
    for noeud in ast.walk(ast.parse(source)):
        if not _est_un_appel_de_journal(noeud):
            continue
        if noeud.func.attr == "exception":
            trouves.append((noeud.lineno, "exception()"))
        for mot_cle in noeud.keywords:
            if mot_cle.arg in {"exc_info", "stack_info"}:
                trouves.append((noeud.lineno, f"{mot_cle.arg}="))
    return trouves


def _cite(noeud: ast.AST, nom: str) -> bool:
    """Vrai si l'expression lit la variable `nom`."""
    return any(
        isinstance(sous_noeud, ast.Name) and sous_noeud.id == nom
        for sous_noeud in ast.walk(noeud)
    )


def _argument_sur(argument: ast.expr, nom: str) -> bool:
    """Un argument de journal est sûr s'il ne relaie pas le texte de `nom` brut.

    Sont acceptés : un argument qui ne cite pas l'exception, un appel à
    `masquer()` (ou `masquer_un_nom()`), et le seul nom de classe
    `type(err).__name__`, qui est du code et non un texte reçu.
    """
    if not _cite(argument, nom):
        return True
    if (
        isinstance(argument, ast.Call)
        and isinstance(argument.func, ast.Name)
        and argument.func.id in _FONCTIONS_DE_MASQUAGE
    ):
        return True
    return (
        isinstance(argument, ast.Attribute)
        and argument.attr in {"__name__", "__qualname__"}
        and isinstance(argument.value, ast.Call)
        and isinstance(argument.value.func, ast.Name)
        and argument.value.func.id == "type"
    )


def exceptions_non_masquees(source: str) -> list[tuple[int, str]]:
    """Appels de journal qui relaient une exception rattrapée sans masquage."""
    trouves = []
    for gestionnaire in ast.walk(ast.parse(source)):
        if not isinstance(gestionnaire, ast.ExceptHandler) or not gestionnaire.name:
            continue
        nom = gestionnaire.name
        for instruction in gestionnaire.body:
            for noeud in ast.walk(instruction):
                if not _est_un_appel_de_journal(noeud):
                    continue
                arguments = [*noeud.args, *(k.value for k in noeud.keywords)]
                if not all(_argument_sur(argument, nom) for argument in arguments):
                    trouves.append((noeud.lineno, nom))
    return trouves


### Périmètre ###


def modules_du_fork() -> Iterator[Path]:
    """Modules Python du fork : tout sauf le code upstream importé."""
    for chemin in sorted(REPERTOIRE_INTEGRATION.rglob("*.py")):
        if chemin.parent == REPERTOIRE_INTEGRATION and chemin.name in MODULES_UPSTREAM:
            continue
        yield chemin


def _relatif(chemin: Path) -> str:
    return chemin.relative_to(RACINE_DEPOT).as_posix()


def test_seuls_les_modules_upstream_sont_exclus_de_la_garde() -> None:
    """La racine de l'intégration ne contient que du code upstream (#2).

    Si un module du fork y apparaissait, il serait scanné quand même : seuls les
    noms listés par `test_conformite_upstream.py` sont exclus.
    """
    racine = {chemin.name for chemin in REPERTOIRE_INTEGRATION.glob("*.py")}

    assert racine == MODULES_UPSTREAM
    scannes = {_relatif(chemin) for chemin in modules_du_fork()}
    assert _relatif(MODULE_DE_MASQUAGE) in scannes
    assert not scannes & {
        f"custom_components/auto_backup/{nom}" for nom in MODULES_UPSTREAM
    }


### Éprouver les détecteurs ###


@pytest.mark.parametrize(
    "motif",
    [
        r're.compile(r"(?i)access[_-]?token=\S+")',
        r're.sub(r"Bearer\s+\S+", "***", texte)',
        r'MOTIF = r"upload_id=[^&]+"',
        r'CLES = (r"client[_-]?secret", r"password")',
        r're.compile(r"sl\.[A-Za-z0-9_-]{15,}")',
    ],
)
def test_la_garde_reconnait_un_masquage_artisanal(motif: str) -> None:
    """Chacune de ces lignes, hors de `masquage.py`, doit faire échouer la garde."""
    assert motifs_de_masquage(motif)


@pytest.mark.parametrize(
    "texte",
    [
        '_LOGGER.debug("Jeton rafraîchi pour la destination « %s »", nom)',
        'raise DestinationAuthError("le jeton (token) a expiré : ré-autorisez")',
        'entetes = {"Authorization": f"Bearer {jeton}"}',
        're.compile(r"bytes=(\\d+)-(\\d+)")',
    ],
)
def test_la_garde_ignore_le_texte_ordinaire(texte: str) -> None:
    """Un message ou un en-tête qui cite un mot-clé n'est pas un motif."""
    assert not motifs_de_masquage(texte)


def test_la_garde_reconnait_les_motifs_de_masquage_py() -> None:
    """Sur la référence elle-même, le détecteur trouve bien des motifs."""
    assert motifs_de_masquage(MODULE_DE_MASQUAGE.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("source", "attendu"),
    [
        ('_LOGGER.exception("échec")', True),
        ('_LOGGER.error("échec", exc_info=True)', True),
        ('_LOGGER.debug("échec", stack_info=True)', True),
        ('_LOGGER.error("échec : %s", masquer(str(err)))', False),
    ],
)
def test_la_garde_reconnait_une_trace_brute(source: str, *, attendu: bool) -> None:
    """`exception()` et `exc_info=` recopient le message brut de l'exception."""
    assert bool(traces_brutes(source)) is attendu


@pytest.mark.parametrize(
    ("appel", "attendu"),
    [
        ('_LOGGER.error("échec : %s", err)', True),
        ('_LOGGER.error("échec : %s", str(err))', True),
        ('_LOGGER.debug("échec : %r", err)', True),
        ('_LOGGER.error(f"échec : {err}")', True),
        ('_LOGGER.error("échec : %s", masquer(str(err)))', False),
        ('_LOGGER.debug("échec (%s)", type(err).__name__)', False),
        ('_LOGGER.error("échec de %s", nom)', False),
    ],
)
def test_la_garde_reconnait_une_exception_relayee_brute(
    appel: str, *, attendu: bool
) -> None:
    """Dans un `except … as err`, `err` ne passe au journal que masqué."""
    source = f"try:\n    pass\nexcept ValueError as err:\n    {appel}\n"

    assert bool(exceptions_non_masquees(source)) is attendu


### La garde elle-même ###


def test_aucun_masquage_artisanal_hors_du_point_unique() -> None:
    """Critère 3 : aucun motif de masquage ailleurs que dans `masquage.py`."""
    fautes = [
        f"{_relatif(chemin)}:{ligne} : {motif!r}"
        for chemin in modules_du_fork()
        if chemin != MODULE_DE_MASQUAGE
        for ligne, motif in motifs_de_masquage(chemin.read_text(encoding="utf-8"))
    ]

    assert not fautes, (
        "motif de masquage hors de destinations/masquage.py — appeler masquer() :\n"
        + "\n".join(fautes)
    )


def test_aucune_trace_brute_dans_le_journal_du_fork() -> None:
    """Critère 1 : une erreur inattendue passe par `journaliser_une_exception()`."""
    fautes = [
        f"{_relatif(chemin)}:{ligne} : {detail}"
        for chemin in modules_du_fork()
        for ligne, detail in traces_brutes(chemin.read_text(encoding="utf-8"))
    ]

    assert not fautes, (
        "trace brute journalisée — utiliser journaliser_une_exception() :\n"
        + "\n".join(fautes)
    )


def test_toute_exception_relayee_au_journal_est_masquee() -> None:
    """Critère 2 : une exception rattrapée n'atteint le journal que masquée."""
    fautes = [
        f"{_relatif(chemin)}:{ligne} : `{nom}` relayée sans masquer()"
        for chemin in modules_du_fork()
        for ligne, nom in exceptions_non_masquees(chemin.read_text(encoding="utf-8"))
    ]

    assert not fautes, "\n".join(fautes)
