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
from collections.abc import Iterable, Iterator
from pathlib import Path

import pytest

from test_conformite_upstream import FICHIERS_UPSTREAM_REQUIS

RACINE_DEPOT = Path(__file__).resolve().parent.parent
REPERTOIRE_INTEGRATION = RACINE_DEPOT / "custom_components" / "auto_backup"
MODULE_DE_MASQUAGE = REPERTOIRE_INTEGRATION / "destinations" / "masquage.py"
DOC_UPSTREAM = RACINE_DEPOT / "docs" / "UPSTREAM.md"

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


def _nom_du_receveur(noeud: ast.AST) -> str | None:
    """Nom d'un receveur d'appel : `_LOGGER`, `LOG`, `_logger` de `self._logger`."""
    if isinstance(noeud, ast.Name):
        return noeud.id
    if isinstance(noeud, ast.Attribute):
        return noeud.attr
    return None


def _est_get_logger(noeud: ast.AST) -> bool:
    """`logging.getLogger(...)` ou `getLogger(...)`."""
    return isinstance(noeud, ast.Call) and (
        (isinstance(noeud.func, ast.Attribute) and noeud.func.attr == "getLogger")
        or (isinstance(noeud.func, ast.Name) and noeud.func.id == "getLogger")
    )


def _journaux_du_module(arbre: ast.AST) -> set[str]:
    """Noms liés à `logging.getLogger(...)` dans le module, quel que soit le nom.

    `LOG = logging.getLogger(__name__)` comme `self._logger = getLogger(...)` :
    c'est l'assignation qui fait le journal, pas la convention `_LOGGER`.
    """
    noms: set[str] = set()
    for noeud in ast.walk(arbre):
        if isinstance(noeud, ast.Assign) and _est_get_logger(noeud.value):
            cibles = noeud.targets
        elif isinstance(noeud, ast.AnnAssign) and noeud.value is not None:
            cibles = [noeud.target] if _est_get_logger(noeud.value) else []
        else:
            continue
        noms.update(
            nom for cible in cibles if (nom := _nom_du_receveur(cible)) is not None
        )
    return noms


def _est_un_appel_de_journal(noeud: ast.AST, journaux: set[str]) -> bool:
    """Appel d'une méthode de journal, **quel que soit le receveur**.

    `debug`, `info`, `warning`, `error`, `critical` et `log` sont reconnus sur
    tout receveur, nom ou attribut : renommer le journal ne doit pas suffire à
    sortir de la garde. `exception` est aussi le nom d'une méthode sans argument
    de `asyncio.Future` : il n'est retenu que sur un journal reconnu (assigné par
    `getLogger()`, ou dont le nom évoque un journal) ou s'il reçoit un message.
    """
    if not isinstance(noeud, ast.Call) or not isinstance(noeud.func, ast.Attribute):
        return False
    methode = noeud.func.attr
    if methode in _NIVEAUX_DE_JOURNAL - {"exception"}:
        return True
    if methode != "exception":
        return False
    receveur = noeud.func.value
    nom = _nom_du_receveur(receveur)
    return (
        bool(noeud.args)
        or _est_get_logger(receveur)
        or (
            nom is not None
            and (nom in journaux or re.search(r"(?i)log|journal", nom) is not None)
        )
    )


def traces_brutes(source: str) -> list[tuple[int, str]]:
    """Appels de journal qui recopieraient une trace brute."""
    arbre = ast.parse(source)
    journaux = _journaux_du_module(arbre)
    trouves = []
    for noeud in ast.walk(arbre):
        if not _est_un_appel_de_journal(noeud, journaux):
            continue
        if noeud.func.attr == "exception":
            trouves.append((noeud.lineno, "exception()"))
        for mot_cle in noeud.keywords:
            if mot_cle.arg in {"exc_info", "stack_info"}:
                trouves.append((noeud.lineno, f"{mot_cle.arg}="))
    return trouves


def _cle(noeud: ast.AST) -> str | None:
    """Clé d'une variable suivie : `err`, `msg`, `self._cause`…"""
    if isinstance(noeud, ast.Name):
        return noeud.id
    if isinstance(noeud, ast.Attribute):
        return ast.unparse(noeud)
    return None


def _cles_assignees(cible: ast.AST) -> set[str]:
    """Variables écrites par une cible d'assignation, dépaquetage compris."""
    if isinstance(cible, ast.Tuple | ast.List):
        return set().union(*(_cles_assignees(element) for element in cible.elts))
    if isinstance(cible, ast.Starred):
        return _cles_assignees(cible.value)
    if isinstance(cible, ast.Subscript):
        # `details["cause"] = str(err)` contamine tout le dictionnaire.
        return _cles_assignees(cible.value)
    cle = _cle(cible)
    return {cle} if cle is not None else set()


def _cite(noeud: ast.AST, contamines: set[str]) -> bool:
    """Vrai si l'expression lit l'exception ou l'un de ses dérivés."""
    return any(
        _cle(sous_noeud) in contamines
        for sous_noeud in ast.walk(noeud)
        if isinstance(sous_noeud, ast.Name | ast.Attribute)
    )


def _est_masque(noeud: ast.AST, contamines: set[str]) -> bool:
    """Expression sûre : elle ne relaie pas le texte de l'exception brut.

    Sont acceptés : une expression qui ne cite pas l'exception ni ses dérivés,
    un appel à `masquer()` (ou `masquer_un_nom()`), et le seul nom de classe
    `type(err).__name__`, qui est du code et non un texte reçu.
    """
    if not _cite(noeud, contamines):
        return True
    if (
        isinstance(noeud, ast.Call)
        and isinstance(noeud.func, ast.Name)
        and noeud.func.id in _FONCTIONS_DE_MASQUAGE
    ):
        return True
    return (
        isinstance(noeud, ast.Attribute)
        and noeud.attr in {"__name__", "__qualname__"}
        and isinstance(noeud.value, ast.Call)
        and isinstance(noeud.value.func, ast.Name)
        and noeud.value.func.id == "type"
    )


def _flux_d_assignation(portee: ast.AST) -> Iterator[tuple[ast.AST, ast.AST]]:
    """Couples (cible, valeur) de toutes les façons d'écrire une variable."""
    for noeud in ast.walk(portee):
        if isinstance(noeud, ast.Assign):
            for cible in noeud.targets:
                yield cible, noeud.value
        elif isinstance(noeud, ast.AnnAssign | ast.AugAssign | ast.NamedExpr):
            if noeud.value is not None:
                yield noeud.target, noeud.value
        elif isinstance(noeud, ast.For | ast.AsyncFor | ast.comprehension):
            yield noeud.target, noeud.iter
        elif isinstance(noeud, ast.withitem) and noeud.optional_vars is not None:
            yield noeud.optional_vars, noeud.context_expr


def _derives(portee: ast.AST, exception: str) -> set[str]:
    """L'exception et tout ce qui en dérive sans masquage, jusqu'au point fixe.

    `msg = str(err)`, `cause = f"… {err}"`, `for a in err.args`, puis
    `texte = msg.upper()` : chacun devient à son tour une variable à masquer.
    """
    contamines = {exception}
    flux = list(_flux_d_assignation(portee))
    change = True
    while change:
        change = False
        for cible, valeur in flux:
            if _est_masque(valeur, contamines):
                continue
            nouvelles = _cles_assignees(cible) - contamines
            if nouvelles:
                contamines |= nouvelles
                change = True
    return contamines


def _portees(arbre: ast.AST) -> Iterator[ast.AST]:
    """Le module et chaque fonction : l'alias d'une exception y reste visible."""
    for noeud in ast.walk(arbre):
        if isinstance(noeud, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef):
            yield noeud


def exceptions_non_masquees(source: str) -> list[tuple[int, str]]:
    """Appels de journal qui relaient une exception rattrapée sans masquage.

    Pour chaque `except … as err`, l'exception est suivie dans toute la
    fonction qui la rattrape, à travers ses alias ; chaque argument de journal
    qui en cite un doit passer par `masquer()`.
    """
    arbre = ast.parse(source)
    journaux = _journaux_du_module(arbre)
    trouves: set[tuple[int, str]] = set()
    for portee in _portees(arbre):
        noeuds = list(ast.walk(portee))
        for gestionnaire in noeuds:
            if not isinstance(gestionnaire, ast.ExceptHandler) or not gestionnaire.name:
                continue
            contamines = _derives(portee, gestionnaire.name)
            for noeud in noeuds:
                if not _est_un_appel_de_journal(noeud, journaux):
                    continue
                arguments = [*noeud.args, *(k.value for k in noeud.keywords)]
                if not all(_est_masque(argument, contamines) for argument in arguments):
                    trouves.add((noeud.lineno, gestionnaire.name))
    return sorted(trouves)


### Périmètre ###


def modules_du_fork() -> Iterator[Path]:
    """Modules Python du fork : tout sauf le code upstream importé."""
    for chemin in sorted(REPERTOIRE_INTEGRATION.rglob("*.py")):
        if chemin.parent == REPERTOIRE_INTEGRATION and chemin.name in MODULES_UPSTREAM:
            continue
        yield chemin


def _relatif(chemin: Path) -> str:
    """Chemin affiché dans un message d'échec, relatif au dépôt si possible."""
    if chemin.is_relative_to(RACINE_DEPOT):
        return chemin.relative_to(RACINE_DEPOT).as_posix()
    return chemin.as_posix()


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


# Contournements relevés par l'audit de sécurité de #35 : chacun passait
# l'ancienne garde, qui ne suivait ni les alias ni un journal autrement nommé.
_CONTOURNEMENTS = {
    "a-alias": """
import logging
_LOGGER = logging.getLogger(__name__)
try:
    pass
except OSError as err:
    msg = str(err)
    _LOGGER.error("échec : %s", msg)
""",
    "a-alias-en-chaine": """
import logging
_LOGGER = logging.getLogger(__name__)
def f():
    try:
        pass
    except OSError as err:
        cause = f"refus : {err.args[0]}"
        texte = cause.upper()
    _LOGGER.warning("échec : %s", texte)
""",
    "a-alias-par-boucle": """
import logging
_LOGGER = logging.getLogger(__name__)
try:
    pass
except OSError as err:
    for argument in err.args:
        _LOGGER.debug("argument : %r", argument)
""",
    "b-journal-renomme": """
import logging
LOG = logging.getLogger(__name__)
try:
    pass
except OSError as err:
    LOG.error("échec : %s", err)
""",
    "c-journal-en-attribut": """
import logging
class Envoi:
    def __init__(self):
        self._logger = logging.getLogger(__name__)
    def envoyer(self):
        try:
            pass
        except OSError as err:
            self._logger.error("échec : %s", repr(err))
""",
    "c-journal-en-attribut-sans-getlogger-visible": """
def envoyer(outil):
    try:
        pass
    except OSError as err:
        outil.sortie.info("échec : %s", err)
""",
}


@pytest.mark.parametrize("source", _CONTOURNEMENTS.values(), ids=_CONTOURNEMENTS)
def test_la_garde_detecte_les_contournements(source: str) -> None:
    """Alias, journal renommé ou porté par un attribut : tous sont vus."""
    assert exceptions_non_masquees(source)


_FORMES_SURES = {
    "alias-masque": """
import logging
_LOGGER = logging.getLogger(__name__)
try:
    pass
except OSError as err:
    msg = masquer(str(err))
    _LOGGER.error("échec : %s", msg)
""",
    "type-seul": """
import logging
LOG = logging.getLogger(__name__)
try:
    pass
except OSError as err:
    nom = type(err).__name__
    LOG.error("échec (%s)", nom)
""",
    "future-exception": """
async def attendre(tache):
    try:
        await tache
    except OSError as err:
        return tache.exception()
""",
}


@pytest.mark.parametrize("source", _FORMES_SURES.values(), ids=_FORMES_SURES)
def test_la_garde_accepte_les_formes_sures(source: str) -> None:
    """Un alias masqué ou le seul type de l'exception ne sont pas des fautes."""
    assert not exceptions_non_masquees(source)
    assert not traces_brutes(source)


@pytest.mark.parametrize(
    "source",
    [
        'import logging\nLOG = logging.getLogger(__name__)\nLOG.exception("x")\n',
        'class A:\n    def f(self):\n        self._logger.error("x", exc_info=True)\n',
        'import logging\nlogging.getLogger(__name__).exception("x")\n',
    ],
    ids=["journal-renomme", "journal-en-attribut", "journal-anonyme"],
)
def test_la_garde_voit_une_trace_brute_quel_que_soit_le_journal(source: str) -> None:
    """`exception()` et `exc_info=` sont vus même hors de la convention `_LOGGER`."""
    assert traces_brutes(source)


### La garde elle-même ###

# Chaque garde est une fonction pure de la liste des modules scannés : le test
# réel lui passe `modules_du_fork()`, la preuve par module synthétique un
# fichier temporaire. Aucun état global n'est modifié, rien n'est remplacé par
# `monkeypatch` — l'ancienne version, qui remplaçait `modules_du_fork` dans
# `sys.modules[__name__]`, échouait de loin en loin selon l'ordre des tests.


def fautes_de_motifs(chemins: Iterable[Path]) -> list[str]:
    """Motifs de masquage trouvés hors de `masquage.py`."""
    return [
        f"{_relatif(chemin)}:{ligne} : {motif!r}"
        for chemin in chemins
        if chemin != MODULE_DE_MASQUAGE
        for ligne, motif in motifs_de_masquage(chemin.read_text(encoding="utf-8"))
    ]


def fautes_de_traces(chemins: Iterable[Path]) -> list[str]:
    """Traces brutes envoyées au journal."""
    return [
        f"{_relatif(chemin)}:{ligne} : {detail}"
        for chemin in chemins
        for ligne, detail in traces_brutes(chemin.read_text(encoding="utf-8"))
    ]


def fautes_d_exceptions(chemins: Iterable[Path]) -> list[str]:
    """Exceptions, ou dérivés, relayées au journal sans `masquer()`."""
    return [
        f"{_relatif(chemin)}:{ligne} : `{nom}` relayée sans masquer()"
        for chemin in chemins
        for ligne, nom in exceptions_non_masquees(chemin.read_text(encoding="utf-8"))
    ]


def test_aucun_masquage_artisanal_hors_du_point_unique() -> None:
    """Critère 3 : aucun motif de masquage ailleurs que dans `masquage.py`."""
    fautes = fautes_de_motifs(modules_du_fork())

    assert not fautes, (
        "motif de masquage hors de destinations/masquage.py — appeler masquer() :\n"
        + "\n".join(fautes)
    )


def test_aucune_trace_brute_dans_le_journal_du_fork() -> None:
    """Critère 1 : une erreur inattendue passe par `journaliser_une_exception()`."""
    fautes = fautes_de_traces(modules_du_fork())

    assert not fautes, (
        "trace brute journalisée — utiliser journaliser_une_exception() :\n"
        + "\n".join(fautes)
    )


def test_toute_exception_relayee_au_journal_est_masquee() -> None:
    """Critère 2 : une exception rattrapée n'atteint le journal que masquée."""
    fautes = fautes_d_exceptions(modules_du_fork())

    assert not fautes, "\n".join(fautes)


def test_la_garde_echoue_vraiment_sur_un_module_synthetique_hors_masquage(
    tmp_path: Path,
) -> None:
    """Critère 3, à la lettre : la garde désigne un vrai fichier fautif.

    Un module synthétique, hors de `masquage.py`, rejoint les modules du fork
    scannés : chacune des trois gardes doit le désigner par son nom, et le
    périmètre réel doit, lui, rester propre.
    """
    module_fautif = tmp_path / "faux_module_du_fork.py"
    module_fautif.write_text(
        "import logging\n"
        "import re\n"
        "\n"
        "LOG = logging.getLogger(__name__)\n"
        'MOTIF_ARTISANAL = re.compile(r"upload_id=[^&]+")\n'
        "\n"
        "\n"
        "def envoyer():\n"
        "    try:\n"
        "        pass\n"
        "    except OSError as err:\n"
        "        msg = str(err)\n"
        '        LOG.error("échec : %s", msg)\n'
        '        LOG.exception("échec")\n',
        encoding="utf-8",
    )
    perimetre = [*modules_du_fork(), module_fautif]

    for garde in (fautes_de_motifs, fautes_de_traces, fautes_d_exceptions):
        fautes = garde(perimetre)
        assert fautes, garde.__name__
        assert all("faux_module_du_fork.py" in faute for faute in fautes), fautes


### Documentation de l'écart assumé avec `sensor.py` (critère 4) ###


def test_la_doc_upstream_documente_le_message_non_masque_de_sensor_py() -> None:
    """Critère 4 : `docs/UPSTREAM.md` dit que `sensor.py` n'est pas masqué.

    `sensor.py` est un module upstream (cf. `FICHIERS_UPSTREAM_ETENDUS` de
    `test_conformite_upstream.py`), hors du périmètre de cette garde : son
    capteur `AutoBackupLastFailureSensor` recopie donc un message d'erreur sans
    passer par `masquer()`. La documentation doit dire noir sur blanc que
    c'est une conséquence assumée de l'import à l'identique, pas un trou
    oublié — `sensor.py` est déjà cité plus haut dans le document à propos des
    entités ajoutées par #16, d'où la nécessité d'isoler le bon passage.
    """
    texte = DOC_UPSTREAM.read_text(encoding="utf-8")

    titre = "Ce que le fork ne masque pas"
    assert titre in texte, f"section dédiée absente de docs/UPSTREAM.md : {titre!r}"

    section = texte.split(titre, 1)[1]
    assert "sensor.py" in section
    assert "AutoBackupLastFailureSensor" in section
    assert "masqu" in section.casefold()
    assert "assum" in section.casefold() or "conséquence" in section.casefold()
