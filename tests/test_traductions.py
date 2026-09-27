"""Conformité des traductions du fork (issue #18).

Home Assistant affiche l'identifiant technique d'une étape, d'un motif
d'abandon ou d'une erreur dès que sa clé de traduction manque, et `hassfest`
refuse en CI un fichier de traduction mal structuré. Faute de pouvoir rejouer
`hassfest` en local, ce module vérifie ce qui en dépend :

- chaque clé **référencée par le code** (étape, entrée de menu, motif
  d'abandon, erreur de formulaire, champ, entité, problème, action) existe en
  français et en anglais, et aucune clé traduite n'est **orpheline** ;
- les placeholders (`{fournisseur}`) sont identiques d'une langue à l'autre et
  fournis par le code qui affiche le texte ;
- les textes des notifications persistantes (section `exceptions`, #45) sont
  tous référencés par `destinations/notifications.py`, avec les placeholders
  que le code fournit ;
- les actions de `services.yaml` sont intégralement traduites — `hassfest`
  exige un nom et une description pour chaque action et chaque champ déclarés
  dans la section `services` ;
- les langues héritées de l'upstream, que le fork ne complète pas, ne
  contiennent aucune clé que l'anglais ne connaîtrait plus ;
- le code des flux n'écrit aucun texte en dur dans ses placeholders.

Les références sont lues par analyse syntaxique (`ast`) des modules, sans
exécuter le flux : un appel ajouté plus tard est couvert sans retoucher ce test.
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from custom_components.auto_backup import config_flow, const
from custom_components.auto_backup.destinations import flow, notifications, reauth

RACINE_DEPOT = Path(__file__).resolve().parent.parent
INTEGRATION = RACINE_DEPOT / "custom_components" / "auto_backup"
TRADUCTIONS = INTEGRATION / "translations"

LANGUES_ETENDUES = ("fr", "en")
LANGUES_HERITEES = tuple(
    sorted(
        fichier.stem
        for fichier in TRADUCTIONS.glob("*.json")
        if fichier.stem not in LANGUES_ETENDUES
    )
)

MODULE_FLUX = INTEGRATION / "destinations" / "flow.py"
MODULE_CONFIG_FLOW = INTEGRATION / "config_flow.py"
MODULE_ENTITES = INTEGRATION / "destinations" / "entities.py"
MODULE_REAUTH = INTEGRATION / "destinations" / "reauth.py"
MODULE_NOTIFICATIONS = INTEGRATION / "destinations" / "notifications.py"

PLACEHOLDER = re.compile(r"\{(\w+)\}")
CLE_DE_TRADUCTION = re.compile(r"^[a-z0-9_-]+$")


def _traduction(langue: str) -> dict[str, Any]:
    return json.loads((TRADUCTIONS / f"{langue}.json").read_text(encoding="utf-8"))


def _feuilles(valeur: Any, chemin: tuple[str, ...] = ()) -> Iterator[tuple]:
    """Parcourt les chaînes d'une traduction avec leur chemin de clés."""
    if isinstance(valeur, dict):
        for cle, sous_valeur in valeur.items():
            yield from _feuilles(sous_valeur, (*chemin, cle))
    else:
        yield chemin, valeur


def _placeholders(texte: str) -> set[str]:
    return set(PLACEHOLDER.findall(texte))


### Références lues dans le code ###


def _arbre(module: Path) -> ast.Module:
    return ast.parse(module.read_text(encoding="utf-8"))


def _mot_cle(appel: ast.Call, nom: str) -> ast.expr | None:
    return next((kw.value for kw in appel.keywords if kw.arg == nom), None)


def _constante(noeud: ast.expr | None) -> str | None:
    if isinstance(noeud, ast.Constant) and isinstance(noeud.value, str):
        return noeud.value
    return None


def _cles_du_dict(noeud: ast.expr | None) -> set[str]:
    if not isinstance(noeud, ast.Dict):
        return set()
    return {cle for cle in map(_constante, noeud.keys) if cle is not None}


class _References:
    """Clés de traduction référencées par un module de flux."""

    def __init__(self) -> None:
        self.etapes: dict[str, set[str]] = {}
        self.abandons: dict[str, set[str]] = {}
        self.erreurs: set[str] = set()
        self.champs: dict[str, set[str]] = {}
        self.menu: set[str] = set()
        self.placeholders_en_dur: list[str] = []


def _champs_du_formulaire(fonction: ast.AST, module: object) -> set[str]:
    """Clés des `vol.Required` / `vol.Optional` déclarés dans une étape."""
    champs: set[str] = set()
    for noeud in ast.walk(fonction):
        if (
            isinstance(noeud, ast.Call)
            and isinstance(noeud.func, ast.Attribute)
            and noeud.func.attr in {"Required", "Optional"}
            and noeud.args
        ):
            premier = noeud.args[0]
            if (cle := _constante(premier)) is not None:
                champs.add(cle)
            elif isinstance(premier, ast.Name):
                champs.add(getattr(module, premier.id))
    return champs


def _references(arbre: ast.AST, module: object, refs: _References) -> _References:
    for noeud in ast.walk(arbre):
        if isinstance(noeud, ast.AsyncFunctionDef) and noeud.name.startswith(
            "async_step_"
        ):
            etapes_affichees = {
                etape
                for appel in ast.walk(noeud)
                if isinstance(appel, ast.Call)
                and (etape := _constante(_mot_cle(appel, "step_id"))) is not None
            }
            champs = _champs_du_formulaire(noeud, module)
            for etape in etapes_affichees:
                refs.champs.setdefault(etape, set()).update(champs)
            if noeud.name == "async_step_menu":
                refs.menu |= {
                    option
                    for constante in ast.walk(noeud)
                    if isinstance(constante, ast.List)
                    for option in map(_constante, constante.elts)
                    if option is not None
                }

        if isinstance(noeud, ast.Call):
            placeholders = _mot_cle(noeud, "description_placeholders")
            if isinstance(placeholders, ast.Dict):
                refs.placeholders_en_dur += [
                    ast.unparse(valeur)
                    for valeur in placeholders.values
                    if isinstance(valeur, ast.Constant | ast.JoinedStr)
                ]
            if (etape := _constante(_mot_cle(noeud, "step_id"))) is not None:
                refs.etapes.setdefault(etape, set()).update(_cles_du_dict(placeholders))
            if (motif := _constante(_mot_cle(noeud, "reason"))) is not None:
                refs.abandons.setdefault(motif, set()).update(
                    _cles_du_dict(placeholders)
                )

        if (
            isinstance(noeud, ast.Assign)
            and (erreur := _constante(noeud.value)) is not None
        ):
            for cible in noeud.targets:
                if (
                    isinstance(cible, ast.Subscript)
                    and isinstance(cible.value, ast.Name)
                    and cible.value.id in {"erreurs", "errors"}
                ):
                    refs.erreurs.add(erreur)
    return refs


def _classe(arbre: ast.Module, nom: str) -> ast.ClassDef:
    return next(
        noeud
        for noeud in arbre.body
        if isinstance(noeud, ast.ClassDef) and noeud.name == nom
    )


@pytest.fixture(scope="module")
def references_config() -> _References:
    """Clés du flux de configuration upstream (section `config`)."""
    arbre = _arbre(MODULE_CONFIG_FLOW)
    return _references(_classe(arbre, "ConfigFlow"), config_flow, _References())


@pytest.fixture(scope="module")
def references_options() -> _References:
    """Clés du flux d'options : formulaire upstream et étapes du fork."""
    refs = _references(
        _classe(_arbre(MODULE_CONFIG_FLOW), "OptionsFlowHandler"),
        config_flow,
        _References(),
    )
    refs.champs.setdefault("init", set()).update(
        str(cle) for cle in config_flow.OPTIONS_SCHEMA.schema
    )
    refs = _references(_arbre(MODULE_FLUX), flow, refs)
    # Motifs choisis par table plutôt qu'écrits dans l'appel (`reason=motif`).
    for motif in flow.MOTIFS_DE_REFUS.values():
        refs.abandons.setdefault(motif, set())
    return refs


### Structure commune aux deux langues ###


def test_le_francais_et_l_anglais_ont_les_memes_placeholders() -> None:
    """Un placeholder oublié dans une langue s'afficherait tel quel."""
    anglais = dict(_feuilles(_traduction("en")))
    ecarts = [
        ".".join(chemin)
        for chemin, texte in _feuilles(_traduction("fr"))
        if _placeholders(texte) != _placeholders(anglais.get(chemin, ""))
    ]
    assert not ecarts, f"placeholders divergents entre fr et en : {ecarts}"


@pytest.mark.parametrize("langue", LANGUES_ETENDUES)
def test_les_textes_sont_renseignes_et_conformes(langue: str) -> None:
    """Pas de texte vide, de HTML ni de placeholder entre apostrophes.

    `hassfest` refuse les deux derniers ; un texte vide afficherait une étiquette
    blanche dans l'interface.
    """
    fautifs = [
        ".".join(chemin)
        for chemin, texte in _feuilles(_traduction(langue))
        if not isinstance(texte, str)
        or not texte.strip()
        or re.search(r"<[a-z/][^>]*>", texte)
        or re.search(r"'\{\w+\}'", texte)
    ]
    assert not fautifs, f"textes non conformes en {langue} : {fautifs}"


### Flux de configuration et d'options ###


@pytest.mark.parametrize("langue", LANGUES_ETENDUES)
def test_le_flux_de_configuration_est_traduit_sans_orphelin(
    langue: str, references_config: _References
) -> None:
    """Étapes et motifs d'abandon du flux upstream : ni manquant, ni orphelin."""
    config = _traduction(langue)["config"]

    assert set(config["step"]) == set(references_config.etapes)
    assert set(config["abort"]) == set(references_config.abandons)
    assert set(config.get("error", {})) == references_config.erreurs


@pytest.mark.parametrize("langue", LANGUES_ETENDUES)
def test_chaque_etape_du_flux_d_options_est_traduite_sans_orphelin(
    langue: str, references_options: _References
) -> None:
    """Toute étape affichée a sa traduction, et toute traduction son étape."""
    etapes = _traduction(langue)["options"]["step"]

    assert set(etapes) == set(references_options.etapes)
    for nom, etape in etapes.items():
        assert etape.get("title"), f"{langue} : étape {nom} sans titre"


@pytest.mark.parametrize("langue", LANGUES_ETENDUES)
def test_le_menu_nomme_chacune_de_ses_entrees(
    langue: str, references_options: _References
) -> None:
    """Chaque entrée proposée par `async_step_menu` a un libellé, et seulement elles."""
    libelles = _traduction(langue)["options"]["step"]["menu"]["menu_options"]

    assert set(libelles) == references_options.menu


@pytest.mark.parametrize("langue", LANGUES_ETENDUES)
def test_chaque_motif_d_abandon_est_traduit_sans_orphelin(
    langue: str, references_options: _References
) -> None:
    """Un abandon non traduit afficherait son identifiant (`echec_jeton`)."""
    abandons = _traduction(langue)["options"]["abort"]

    assert set(abandons) == set(references_options.abandons)


@pytest.mark.parametrize("langue", LANGUES_ETENDUES)
def test_chaque_erreur_de_formulaire_est_traduite_sans_orphelin(
    langue: str, references_options: _References
) -> None:
    """Les erreurs de saisie (`nom_deja_utilise`…) sont toutes traduites."""
    erreurs = _traduction(langue)["options"]["error"]

    assert set(erreurs) == references_options.erreurs


@pytest.mark.parametrize("langue", LANGUES_ETENDUES)
def test_chaque_champ_a_un_libelle_et_une_description(
    langue: str, references_options: _References
) -> None:
    """Libellé de chaque champ ; description pour tous les champs du fork.

    Le formulaire upstream (`init`) n'a pas de description : le fork ne le
    complète pas au-delà du nécessaire.
    """
    etapes = _traduction(langue)["options"]["step"]

    for nom, champs in references_options.champs.items():
        etape = etapes[nom]
        assert set(etape.get("data", {})) == champs, f"{langue}/{nom} : libellés"
        descriptions = set(etape.get("data_description", {}))
        attendues = set() if nom == "init" else champs
        assert descriptions == attendues, f"{langue}/{nom} : descriptions"


@pytest.mark.parametrize("langue", LANGUES_ETENDUES)
def test_les_placeholders_sont_fournis_par_le_code(
    langue: str, references_options: _References
) -> None:
    """Un `{placeholder}` que le code ne fournit pas s'afficherait brut."""
    options = _traduction(langue)["options"]
    manquants = []
    for nom, fournis in references_options.etapes.items():
        texte = options["step"][nom].get("description", "")
        if not _placeholders(texte) <= fournis:
            manquants.append(f"step.{nom}")
    for motif, fournis in references_options.abandons.items():
        if not _placeholders(options["abort"][motif]) <= fournis:
            manquants.append(f"abort.{motif}")
    for erreur in references_options.erreurs:
        if _placeholders(options["error"][erreur]):
            manquants.append(f"error.{erreur}")
    assert not manquants, f"placeholders sans valeur en {langue} : {manquants}"


def test_le_flux_n_ecrit_aucun_texte_en_dur_dans_ses_placeholders(
    references_options: _References,
) -> None:
    """Les placeholders ne portent que des données, jamais un texte rédigé.

    Un texte écrit en dur (« inconnue ») s'afficherait dans une seule langue au
    milieu d'un message traduit : c'est une clé de traduction qu'il faut.
    """
    assert not references_options.placeholders_en_dur


### Entités et problèmes ###


def _cles_d_entites() -> dict[str, set[str]]:
    """`translation_key` des descriptions d'entités, par plateforme."""
    plateformes = {
        "SensorEntityDescription": "sensor",
        "BinarySensorEntityDescription": "binary_sensor",
    }
    cles: dict[str, set[str]] = {}
    for noeud in ast.walk(_arbre(MODULE_ENTITES)):
        if (
            isinstance(noeud, ast.Call)
            and isinstance(noeud.func, ast.Name)
            and noeud.func.id in plateformes
            and (cle := _constante(_mot_cle(noeud, "translation_key")))
        ):
            cles.setdefault(plateformes[noeud.func.id], set()).add(cle)
    return cles


@pytest.mark.parametrize("langue", LANGUES_ETENDUES)
def test_chaque_entite_du_fork_est_traduite_sans_orphelin(langue: str) -> None:
    """Les entités d'une destination portent un nom traduit, et lui seul."""
    entites = _traduction(langue)["entity"]
    attendues = _cles_d_entites()

    assert attendues, "aucune translation_key trouvée dans destinations/entities.py"
    assert {plateforme: set(cles) for plateforme, cles in entites.items()} == (
        attendues
    )
    for plateforme, cles in entites.items():
        for cle, entite in cles.items():
            assert _placeholders(entite["name"]) == {"destination"}, (
                f"{langue} : {plateforme}.{cle}"
            )


@pytest.mark.parametrize("langue", LANGUES_ETENDUES)
def test_les_attributs_du_capteur_de_probleme_sont_traduits(langue: str) -> None:
    """Chaque attribut exposé par le capteur binaire a un nom traduit."""
    attributs = _traduction(langue)["entity"]["binary_sensor"]["destination_probleme"][
        "state_attributes"
    ]

    assert set(attributs) == {
        const.ATTR_LAST_ERROR,
        const.ATTR_LAST_FAILED_SLUG,
        const.ATTR_LAST_FAILED_AT,
    }
    assert all(attribut["name"] for attribut in attributs.values())


@pytest.mark.parametrize("langue", LANGUES_ETENDUES)
def test_le_probleme_de_reautorisation_est_traduit_sans_orphelin(
    langue: str,
) -> None:
    """Seul le problème créé par `reauth.py` est traduit, placeholders fournis."""
    problemes = _traduction(langue)["issues"]
    fournis = {
        cle
        for noeud in ast.walk(_arbre(MODULE_REAUTH))
        if isinstance(noeud, ast.Call)
        for cle in _cles_du_dict(_mot_cle(noeud, "translation_placeholders"))
    }

    assert set(problemes) == {reauth.CLE_TRADUCTION_REAUTH}
    for texte in problemes[reauth.CLE_TRADUCTION_REAUTH].values():
        assert _placeholders(texte) <= fournis


### Notifications persistantes (section `exceptions`) ###


def _placeholders_des_notifications() -> dict[str, set[str]]:
    """Placeholders fournis par le code, par clé de notification.

    Chaque texte est lu par `_texte(textes, CLE_…, placeholder=valeur, …)` :
    l'analyse de ces appels donne, sans exécuter le module, la clé demandée et
    les placeholders que le code lui passe.
    """
    fournis: dict[str, set[str]] = {}
    for noeud in ast.walk(_arbre(MODULE_NOTIFICATIONS)):
        if (
            isinstance(noeud, ast.Call)
            and isinstance(noeud.func, ast.Name)
            and noeud.func.id == "_texte"
            and len(noeud.args) >= 2
            and isinstance(noeud.args[1], ast.Name)
        ):
            cle = getattr(notifications, noeud.args[1].id)
            fournis.setdefault(cle, set()).update(
                mot.arg for mot in noeud.keywords if mot.arg is not None
            )
    return fournis


def test_chaque_cle_de_notification_est_lue_par_le_code() -> None:
    """Les clés déclarées par le module sont exactement celles qu'il lit."""
    assert set(_placeholders_des_notifications()) == set(
        notifications.CLES_DE_TRADUCTION
    )


@pytest.mark.parametrize("langue", LANGUES_ETENDUES)
def test_les_notifications_sont_traduites_sans_orphelin(langue: str) -> None:
    """Chaque texte de notification existe, sous la seule clé `message`.

    `hassfest` n'accepte que `message` dans une entrée de la section
    `exceptions` ; une clé que le code ne lit pas serait orpheline.
    """
    exceptions = _traduction(langue)["exceptions"]

    assert set(exceptions) == set(notifications.CLES_DE_TRADUCTION)
    for cle, entree in exceptions.items():
        assert set(entree) == {"message"}, f"{langue} : exceptions.{cle}"


@pytest.mark.parametrize("langue", LANGUES_ETENDUES)
def test_les_placeholders_des_notifications_sont_fournis_par_le_code(
    langue: str,
) -> None:
    """Chaque placeholder d'une notification reçoit une valeur, et réciproquement.

    Un placeholder sans valeur s'afficherait brut ; une valeur sans placeholder
    trahirait une information perdue à la traduction (le compteur d'échecs,
    par exemple).
    """
    exceptions = _traduction(langue)["exceptions"]
    fournis = _placeholders_des_notifications()

    ecarts = [
        cle
        for cle, placeholders in fournis.items()
        if _placeholders(exceptions[cle]["message"]) != placeholders
    ]
    assert not ecarts, f"placeholders divergents en {langue} : {ecarts}"


def test_les_notifications_different_entre_les_langues() -> None:
    """Le français n'est pas une copie de l'anglais pour les notifications."""
    francais = _traduction("fr")["exceptions"]
    anglais = _traduction("en")["exceptions"]

    assert all(francais[cle] != anglais[cle] for cle in francais)


### Actions (`services.yaml`) ###


def _services_yaml() -> dict[str, Any]:
    return yaml.safe_load((INTEGRATION / "services.yaml").read_text(encoding="utf-8"))


@pytest.mark.parametrize("langue", LANGUES_ETENDUES)
def test_chaque_action_et_chaque_champ_sont_traduits(langue: str) -> None:
    """`hassfest` exige nom et description pour chaque action traduite.

    Les champs ajoutés par le fork — `upload_to` en tête — le sont donc aussi,
    dans les deux langues.
    """
    services = _traduction(langue)["services"]
    declares = _services_yaml()

    assert set(services) == set(declares)
    for nom, service in declares.items():
        traduction = services[nom]
        assert traduction["name"] and traduction["description"], f"{langue}/{nom}"
        champs = set((service or {}).get("fields") or {})
        assert set(traduction.get("fields", {})) == champs, f"{langue}/{nom}"
        for champ, texte in traduction.get("fields", {}).items():
            assert texte["name"] and texte["description"], f"{langue}/{nom}.{champ}"


@pytest.mark.parametrize("langue", LANGUES_ETENDUES)
def test_le_champ_upload_to_est_traduit_dans_chaque_action_de_sauvegarde(
    langue: str,
) -> None:
    """Le champ ajouté par le fork (#8) est nommé et décrit partout où il existe."""
    services = _traduction(langue)["services"]

    for action in ("backup", "backup_full", "backup_partial"):
        champ = services[action]["fields"]["upload_to"]
        assert champ["name"] and champ["description"]


def test_les_traductions_de_upload_to_different_entre_les_langues() -> None:
    """Le français n'est pas une copie de l'anglais pour le champ du fork."""
    francais = _traduction("fr")["services"]["backup"]["fields"]["upload_to"]
    anglais = _traduction("en")["services"]["backup"]["fields"]["upload_to"]

    assert francais != anglais


### Clés de traduction et langues héritées ###


@pytest.mark.parametrize("langue", LANGUES_ETENDUES)
def test_les_cles_de_traduction_sont_des_identifiants_valides(langue: str) -> None:
    """`hassfest` n'accepte que minuscules, chiffres, `_` et `-` comme clés."""
    traduction = _traduction(langue)
    cles = [
        *traduction["options"]["step"],
        *traduction["options"]["abort"],
        *traduction["options"]["error"],
        *traduction["issues"],
        *traduction["exceptions"],
        *traduction["services"],
        *(cle for plateforme in traduction["entity"].values() for cle in plateforme),
    ]
    invalides = [cle for cle in cles if not CLE_DE_TRADUCTION.match(cle)]
    assert not invalides


@pytest.mark.parametrize("langue", LANGUES_HERITEES)
def test_les_langues_heritees_restent_valides(langue: str) -> None:
    """Les langues de l'upstream n'ont aucune clé que l'anglais ignorerait.

    Home Assistant complète une langue partielle par l'anglais : les nouvelles
    chaînes du fork y apparaissent donc en anglais, sans rien casser. Une clé
    présente dans une langue héritée mais absente de l'anglais serait, elle,
    orpheline.
    """
    anglais = dict(_feuilles(_traduction("en")))
    herite = dict(_feuilles(_traduction(langue)))

    orphelines = sorted(".".join(chemin) for chemin in herite.keys() - anglais.keys())
    assert not orphelines, f"clés orphelines en {langue} : {orphelines}"
    divergents = [
        ".".join(chemin)
        for chemin, texte in herite.items()
        if _placeholders(texte) != _placeholders(anglais[chemin])
    ]
    assert not divergents, f"placeholders divergents en {langue} : {divergents}"
