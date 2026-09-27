"""Documentation utilisateur de bout en bout (issue #19).

Critères couverts :

- le `README.md` présente le fork, son installation via HACS, le lien vers la
  documentation détaillée et la mention de l'upstream sous licence MIT ;
- `docs/` contient une page décrivant les options de service et la rétention
  distante (`docs/services.md`) et une FAQ (`docs/faq.md`) ;
- au moins une automatisation YAML complète montre une sauvegarde planifiée
  envoyée vers une destination cloud avec rétention locale et distante ;
- les liens internes et les noms de services, d'options et d'événements cités
  correspondent **exactement** au code livré.

Les noms attendus ne sont jamais recopiés à la main : ils sont relus dans
`services.yaml`, dans `const.py` et dans les schémas voluptuous des services,
pour qu'une dérive entre la documentation et le code fasse échouer ces tests.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import voluptuous as vol
import yaml

from custom_components.auto_backup import MAP_SERVICES
from custom_components.auto_backup.const import (
    ATTR_DESTINATION,
    ATTR_DESTINATION_NAME,
    ATTR_ERROR,
    ATTR_REMOTE_ID,
    ATTR_REMOTE_IDS,
    ATTR_SIZE,
    ATTR_SLUG,
    ATTR_UPLOAD_TO,
    CONF_AUTO_PURGE,
    CONF_NOTIFY_ON_FAILURE,
    CONF_RETENTION_COUNT,
    CONF_RETENTION_DAYS,
    CONF_UPLOAD_TIMEOUT,
    DOMAIN,
    EVENT_BACKUP_FAILED,
    EVENT_BACKUP_START,
    EVENT_BACKUP_SUCCESSFUL,
    EVENT_BACKUPS_PURGED,
    EVENT_REMOTE_PURGE,
    EVENT_UPLOAD_FAILED,
    EVENT_UPLOAD_START,
    EVENT_UPLOAD_SUCCESSFUL,
    STORAGE_KEY_REMOTE_BACKUPS,
)

RACINE_DEPOT = Path(__file__).resolve().parent.parent
README = RACINE_DEPOT / "README.md"
DOCS = RACINE_DEPOT / "docs"
DOC_INDEX = DOCS / "README.md"
DOC_SERVICES = DOCS / "services.md"
DOC_FAQ = DOCS / "faq.md"
GUIDES = (
    DOCS / "destinations" / "dropbox.md",
    DOCS / "destinations" / "google-drive.md",
)
SERVICES_YAML = RACINE_DEPOT / "custom_components" / DOMAIN / "services.yaml"

# Pages destinées aux utilisateurs, dont les noms cités sont vérifiés.
PAGES_UTILISATEUR = (README, DOC_SERVICES, DOC_FAQ, *GUIDES)

# Champs émis par chaque événement (cf. `manager.py`, `destinations/upload.py`
# et `destinations/retention.py`), construits depuis les constantes du code.
CHAMPS_UPLOAD = ("name", ATTR_SLUG, ATTR_DESTINATION, ATTR_DESTINATION_NAME)
CHAMPS_DES_EVENEMENTS: dict[str, tuple[str, ...]] = {
    EVENT_BACKUP_START: ("name",),
    EVENT_BACKUP_SUCCESSFUL: ("name", ATTR_SLUG),
    EVENT_BACKUP_FAILED: ("name", ATTR_ERROR),
    EVENT_BACKUPS_PURGED: ("backups",),
    EVENT_UPLOAD_START: CHAMPS_UPLOAD,
    EVENT_UPLOAD_SUCCESSFUL: (*CHAMPS_UPLOAD, ATTR_SIZE, ATTR_REMOTE_ID),
    EVENT_UPLOAD_FAILED: (*CHAMPS_UPLOAD, ATTR_ERROR),
    EVENT_REMOTE_PURGE: (ATTR_DESTINATION, ATTR_DESTINATION_NAME, ATTR_REMOTE_IDS),
}

BLOC_DE_CODE = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)
BLOC_YAML = re.compile(r"^```yaml\n(.*?)^```", re.MULTILINE | re.DOTALL)
CODE_EN_LIGNE = re.compile(r"`[^`\n]*`")
LIEN = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
TITRE = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$", re.MULTILINE)
# `auto_backup.<nom>` isolé : exclut `custom_components.auto_backup.destinations`.
NOM_QUALIFIE = re.compile(rf"(?<![\w.]){DOMAIN}\.([a-z_]+)")


def _lire(chemin: Path) -> str:
    assert chemin.is_file(), f"page introuvable : {chemin}"
    return chemin.read_text(encoding="utf-8")


def _aplati(chemin: Path) -> str:
    """Texte sans retours à la ligne ni marqueurs de citation."""
    lignes = (re.sub(r"^\s*>\s?", "", ligne) for ligne in _lire(chemin).splitlines())
    return " ".join(" ".join(lignes).split())


def _services_declares() -> dict[str, dict[str, Any]]:
    return yaml.safe_load(_lire(SERVICES_YAML))


def _evenements() -> set[str]:
    return set(CHAMPS_DES_EVENEMENTS)


### README ###


def test_le_readme_presente_le_fork_en_francais() -> None:
    texte = _aplati(README)

    assert "Dropbox et Google Drive" in texte
    assert "Objectif du fork" in texte
    for mot in ("sauvegardes", "votre", "destination"):
        assert mot in texte


def test_le_readme_decrit_l_installation_via_hacs() -> None:
    texte = _lire(README)

    assert "## Installation via HACS" in texte
    assert "Dépôts personnalisés" in texte
    assert "https://github.com/ldb2000/auto-backup-extension" in texte
    # La version minimale annoncée est celle que déclare `hacs.json`.
    hacs = yaml.safe_load((RACINE_DEPOT / "hacs.json").read_text(encoding="utf-8"))
    assert hacs["homeassistant"] in texte


def test_le_readme_mentionne_l_upstream_sous_licence_mit() -> None:
    texte = _aplati(README)

    assert "jcwillox/hass-auto-backup" in texte
    assert "licence MIT" in texte


@pytest.mark.parametrize(
    "cible",
    [
        "docs/services.md",
        "docs/faq.md",
        "docs/destinations/dropbox.md",
        "docs/destinations/google-drive.md",
        "docs/README.md",
    ],
)
def test_le_readme_renvoie_vers_la_documentation_detaillee(cible: str) -> None:
    assert f"]({cible})" in _lire(README)


@pytest.mark.parametrize("cible", ["services.md", "faq.md"])
def test_l_index_de_docs_reference_les_nouvelles_pages(cible: str) -> None:
    assert f"]({cible})" in _lire(DOC_INDEX)


### Liens internes ###


def _ancre_github(titre: str) -> str:
    """Ancre qu'attribue GitHub à un titre Markdown (algorithme de github-slugger)."""
    texte = titre.strip().lower()
    texte = re.sub(r"[^\w\- ]", "", texte)
    return texte.replace(" ", "-")


def _ancres(chemin: Path) -> set[str]:
    sans_code = BLOC_DE_CODE.sub("", _lire(chemin))
    ancres: set[str] = set()
    vues: dict[str, int] = {}
    for titre in TITRE.findall(sans_code):
        base = _ancre_github(titre)
        rang = vues.get(base, 0)
        vues[base] = rang + 1
        ancres.add(base if rang == 0 else f"{base}-{rang}")
    return ancres


def _pages_markdown() -> list[Path]:
    return [README, *sorted(DOCS.rglob("*.md"))]


def _liens_internes(chemin: Path) -> Iterator[str]:
    texte = CODE_EN_LIGNE.sub("", BLOC_DE_CODE.sub("", _lire(chemin)))
    for cible in LIEN.findall(texte):
        if not re.match(r"^[a-z]+:", cible):
            yield cible


@pytest.mark.parametrize(
    "page", _pages_markdown(), ids=lambda p: str(p.relative_to(RACINE_DEPOT))
)
def test_les_liens_internes_menent_a_une_page_et_une_ancre_existantes(
    page: Path,
) -> None:
    casses = []
    for lien in _liens_internes(page):
        fichier, _, ancre = lien.partition("#")
        cible = (page.parent / fichier).resolve() if fichier else page
        if not cible.exists():
            casses.append(f"{lien} : fichier absent")
        elif ancre and cible.suffix == ".md" and ancre not in _ancres(cible):
            casses.append(f"{lien} : ancre absente")
    assert not casses, f"liens cassés dans {page.name} : {casses}"


def test_le_calcul_des_ancres_suit_celui_de_github() -> None:
    """Garde-fou du test précédent, sur des titres réels de la documentation."""
    assert (
        _ancre_github("Rétention : ce qui est supprimé, et ce qui ne l'est jamais")
        == "rétention--ce-qui-est-supprimé-et-ce-qui-ne-lest-jamais"
    )
    assert _ancre_github("Mode « Test » ou mode « Production » ?") == (
        "mode--test--ou-mode--production--"
    )


### Page des services ###


def test_la_page_des_services_cite_chaque_service_declare() -> None:
    texte = _lire(DOC_SERVICES)

    for service in _services_declares():
        assert f"`{DOMAIN}.{service}`" in texte, service
    assert set(_services_declares()) == set(MAP_SERVICES)


def test_la_page_des_services_documente_chaque_option_declaree() -> None:
    texte = _lire(DOC_SERVICES)

    manquantes = {
        f"{service}.{champ}"
        for service, definition in _services_declares().items()
        for champ in (definition.get("fields") or {})
        if f"`{champ}`" not in texte
    }
    assert not manquantes, f"options absentes de docs/services.md : {manquantes}"


def test_la_page_des_services_decrit_upload_to_et_la_retention_distante() -> None:
    texte = _lire(DOC_SERVICES)

    assert f"## L'option `{ATTR_UPLOAD_TO}`" in texte
    for cle in (
        CONF_RETENTION_DAYS,
        CONF_RETENTION_COUNT,
        CONF_AUTO_PURGE,
        CONF_UPLOAD_TIMEOUT,
        STORAGE_KEY_REMOTE_BACKUPS,
    ):
        assert f"`{cle}`" in texte or cle in texte, cle
    assert "Rétention locale et rétention distante" in texte


def test_la_page_des_services_decrit_chaque_evenement_avec_ses_champs() -> None:
    lignes = _lire(DOC_SERVICES).splitlines()

    for evenement, champs in CHAMPS_DES_EVENEMENTS.items():
        ligne = next(
            (ligne for ligne in lignes if ligne.startswith(f"| `{evenement}` |")),
            None,
        )
        assert ligne is not None, f"événement absent du tableau : {evenement}"
        cites = set(re.findall(r"`([a-z_]+)`", ligne.rsplit("|", 2)[1]))
        assert cites == set(champs), f"{evenement} : {cites} au lieu de {champs}"


def test_l_option_des_notifications_citee_par_le_readme_existe() -> None:
    assert f"`{CONF_NOTIFY_ON_FAILURE}`" in _lire(README)


@pytest.mark.parametrize(
    "page", PAGES_UTILISATEUR, ids=lambda p: str(p.relative_to(RACINE_DEPOT))
)
def test_les_noms_qualifies_cites_existent_dans_le_code(page: Path) -> None:
    """Tout `auto_backup.<nom>` cité est un service, un événement ou le registre."""
    connus = (
        {f"{DOMAIN}.{service}" for service in _services_declares()}
        | _evenements()
        | {f"{DOMAIN}.{STORAGE_KEY_REMOTE_BACKUPS}"}
    )
    inconnus = {
        f"{DOMAIN}.{nom}"
        for nom in NOM_QUALIFIE.findall(_lire(page))
        if f"{DOMAIN}.{nom}" not in connus
    }
    assert not inconnus, f"noms inconnus du code dans {page.name} : {inconnus}"


### Exemples YAML ###


class _ChargeurHomeAssistant(yaml.SafeLoader):
    """Chargeur YAML qui accepte la balise `!secret` de Home Assistant."""


_ChargeurHomeAssistant.add_constructor(
    "!secret", lambda chargeur, noeud: f"<secret {chargeur.construct_scalar(noeud)}>"
)


def _blocs_yaml(chemin: Path) -> list[Any]:
    return [
        yaml.load(bloc, Loader=_ChargeurHomeAssistant)
        for bloc in BLOC_YAML.findall(_lire(chemin))
    ]


def _noeuds(valeur: Any) -> Iterator[dict[str, Any]]:
    if isinstance(valeur, dict):
        yield valeur
        for enfant in valeur.values():
            yield from _noeuds(enfant)
    elif isinstance(valeur, list):
        for enfant in valeur:
            yield from _noeuds(enfant)


def _appels_auto_backup(valeur: Any) -> Iterator[tuple[str, dict[str, Any]]]:
    for noeud in _noeuds(valeur):
        action = noeud.get("action", noeud.get("service"))
        if isinstance(action, str) and action.startswith(f"{DOMAIN}."):
            yield action.removeprefix(f"{DOMAIN}."), noeud.get("data") or {}


@pytest.mark.parametrize(
    "page", PAGES_UTILISATEUR, ids=lambda p: str(p.relative_to(RACINE_DEPOT))
)
def test_les_exemples_yaml_n_appellent_que_des_services_et_options_du_code(
    page: Path,
) -> None:
    """Chaque appel d'exemple est accepté par le schéma réel du service."""
    declares = _services_declares()
    for bloc in _blocs_yaml(page):
        for service, donnees in _appels_auto_backup(bloc):
            assert service in declares, f"service inconnu : {service}"
            champs = set((declares[service] or {}).get("fields") or {})
            assert set(donnees) <= champs, f"{service} : {set(donnees) - champs}"
            schema = MAP_SERVICES[service]
            if schema is None:
                assert not donnees
            else:
                try:
                    schema(dict(donnees))
                except vol.Invalid as err:  # pragma: no cover - message d'échec
                    pytest.fail(f"exemple refusé par le schéma de {service} : {err}")


def _automatisations(page: Path) -> list[dict[str, Any]]:
    return [
        noeud
        for bloc in _blocs_yaml(page)
        for noeud in _noeuds(bloc)
        if "triggers" in noeud or ("trigger" in noeud and "action" in noeud)
    ]


@pytest.mark.parametrize(
    "page", PAGES_UTILISATEUR, ids=lambda p: str(p.relative_to(RACINE_DEPOT))
)
def test_les_evenements_des_exemples_existent_et_leurs_champs_aussi(
    page: Path,
) -> None:
    for automatisation in _automatisations(page):
        for noeud in _noeuds(automatisation):
            evenement = noeud.get("event_type")
            if not isinstance(evenement, str) or not evenement.startswith(DOMAIN):
                continue
            assert evenement in CHAMPS_DES_EVENEMENTS, evenement
            lus = set(
                re.findall(
                    r"trigger\.event\.data\.(\w+)", yaml.safe_dump(automatisation)
                )
            )
            assert lus <= set(CHAMPS_DES_EVENEMENTS[evenement]), (evenement, lus)


def test_une_automatisation_planifiee_envoie_la_sauvegarde_dans_le_cloud() -> None:
    """Critère 3 : planification, envoi cloud, rétention locale et distante."""
    completes = []
    for automatisation in _automatisations(DOC_SERVICES):
        declencheurs = automatisation.get("triggers") or []
        planifiee = any(d.get("trigger") == "time" for d in declencheurs)
        for _, donnees in _appels_auto_backup(automatisation.get("actions")):
            if planifiee and donnees.get(ATTR_UPLOAD_TO) and "keep_days" in donnees:
                completes.append(automatisation)
    assert completes, "aucune automatisation planifiée avec upload_to et keep_days"

    # La rétention distante se règle sur la destination : la page le montre.
    texte = _aplati(DOC_SERVICES)
    assert "Conserver pendant (jours)" in texte
    assert "Nombre maximum de sauvegardes conservées" in texte


def test_les_libelles_de_retention_cites_sont_ceux_de_l_interface() -> None:
    traductions = _lire(
        RACINE_DEPOT / "custom_components" / DOMAIN / "translations" / "fr.json"
    )
    for libelle in (
        "Conserver pendant (jours)",
        "Nombre maximum de sauvegardes conservées",
        "Suppression automatique des sauvegardes expirées",
        "Réglages du téléversement",
        "Ré-autoriser une destination",
    ):
        assert libelle in traductions, libelle
        assert libelle in _lire(DOC_SERVICES) + _lire(DOC_FAQ), libelle


### FAQ ###


@pytest.mark.parametrize(
    "sujet",
    [
        "Taille des sauvegardes et quotas cloud",
        "Chiffrement et mot de passe",
        "Portées OAuth et stockage des jetons",
        "Ré-authentification",
        "Plusieurs instances Home Assistant",
    ],
)
def test_la_faq_traite_les_sujets_exiges(sujet: str) -> None:
    assert f"## {sujet}" in _lire(DOC_FAQ)


def test_la_faq_cite_les_portees_reellement_demandees() -> None:
    from custom_components.auto_backup.destinations.providers.dropbox import PORTEES
    from custom_components.auto_backup.destinations.providers.google_drive import (
        PORTEE_DRIVE_FILE,
    )

    texte = _lire(DOC_FAQ)
    for portee in (*PORTEES, PORTEE_DRIVE_FILE):
        assert f"`{portee}`" in texte, portee


@pytest.mark.parametrize(
    "page",
    [DOC_SERVICES, DOC_FAQ, *GUIDES],
    ids=lambda p: str(p.relative_to(RACINE_DEPOT)),
)
def test_un_dossier_distant_par_instance_est_recommande(page: Path) -> None:
    """Commentaire de l'issue : la recommandation vaut pour les deux fournisseurs."""
    texte = _aplati(page).casefold()
    assert "par instance" in texte
    assert "chaque instance" in texte
