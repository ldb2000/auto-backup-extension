"""Critères d'acceptation de l'issue #55.

Un test par critère Given-When-Then :

1. La notification et le problème `reauthentification_requise` renvoient à
   « Paramètres → Système → Réparations », en plus de l'action « Configurer →
   Ré-autoriser une destination » — de bout en bout, notification et problème
   réellement créés (registre des problèmes de Home Assistant), en français et
   en anglais.
2. Plus aucun texte du fork (traductions, `docs/`, README, code) ne désigne
   « l'interface des intégrations » (« integrations dashboard »).
3. `translations/en.json` suit l'orthographe américaine — au-delà de
   « authoris », d'autres britannismes courants (`-ise`/`-isation`, `colour`,
   `cancelled`, `behaviour`…) ne doivent pas non plus s'y trouver.
4. Le message du code `unknown` ne l'attribue pas au fournisseur.
5. Une valeur d'`upload_to` très longue est tronquée dans le refus.
6. Les fichiers de traduction restent un JSON valide (hassfest est un contrôle
   de CI, hors de portée de cette suite locale ; `tests/test_traductions.py` et
   `tests/test_doc_utilisateur.py` sont exécutés avec le reste de la suite).

Ce module complète — sans les répéter — les tests ajoutés par le codeur dans
`tests/test_traductions.py`, `tests/test_televersement.py` et
`tests/test_notifications_traduites.py`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.auto_backup.const import CONF_DESTINATIONS, DOMAIN
from custom_components.auto_backup.destinations import (
    DestinationConfig,
    async_signaler_la_reauthentification,
    identifiant_du_probleme,
)
from custom_components.auto_backup.destinations.errors import (
    CodeErreur,
    DestinationError,
    cle_de_traduction_de_l_erreur,
)
from custom_components.auto_backup.destinations.notifications import (
    identifiant_de_notification_de_reauthentification,
)
from custom_components.auto_backup.destinations.traductions import (
    async_message_d_erreur,
)
from custom_components.auto_backup.destinations.upload import (
    LONGUEUR_MAX_DESTINATION_AFFICHEE,
    async_resoudre_destinations,
)
from destinations_factices import config_oauth_factice

RACINE_DEPOT = Path(__file__).resolve().parent.parent
INTEGRATION = RACINE_DEPOT / "custom_components" / "auto_backup"
TRADUCTIONS = INTEGRATION / "translations"


### Critère 1 : l'écran exact et l'action, de bout en bout, dans les 2 langues ###

# Home Assistant range ses problèmes dans « Paramètres → Système → Réparations ».
ECRAN_DES_REPARATIONS = {
    "fr": "Paramètres → Système → Réparations",
    "en": "Settings → System → Repairs",
}
ACTION_DE_REAUTORISATION = {
    "fr": "« Ré-autoriser une destination »",
    "en": "“Re-authorize a destination”",
}
MENU_CONFIGURER = {
    "fr": "« Configurer »",
    "en": "“Configure”",
}


def _description_du_probleme(langue: str, placeholders: dict[str, Any]) -> str:
    """Texte que Home Assistant afficherait pour le problème, dans `langue`.

    Le registre des problèmes ne porte que `translation_key` et
    `translation_placeholders` : le rendu affiché à l'utilisateur vient du
    fichier de traduction du fork, la même source que celle que le frontend de
    Home Assistant lirait.
    """
    donnees = json.loads((TRADUCTIONS / f"{langue}.json").read_text(encoding="utf-8"))
    modele = donnees["issues"]["reauthentification_requise"]["description"]
    return modele.format(**placeholders)


@pytest.mark.parametrize("langue", ["fr", "en"])
async def test_critere1_notification_et_probleme_renvoient_aux_reparations(
    hass: HomeAssistant,
    integration_backup: None,
    fournisseur_oauth_factice: str,
    langue: str,
) -> None:
    """Given la notification et le problème, When on les lit.

    Then ils renvoient à Réparations et à l'action qui résout le problème.
    """
    hass.config.language = langue
    entree = MockConfigEntry(
        domain=DOMAIN,
        title="Auto Backup",
        data={},
        options={CONF_DESTINATIONS: [config_oauth_factice()]},
    )
    entree.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entree.entry_id)
    await hass.async_block_till_done()

    async_signaler_la_reauthentification(
        hass, DestinationConfig.from_dict(config_oauth_factice())
    )
    await hass.async_block_till_done()

    # Le problème est réellement créé dans le registre des problèmes de HA.
    probleme = ir.async_get(hass).async_get_issue(
        DOMAIN, identifiant_du_probleme("destination_oauth")
    )
    assert probleme is not None
    assert probleme.translation_key == "reauthentification_requise"
    assert probleme.is_fixable is False
    assert probleme.translation_placeholders is not None
    assert probleme.translation_placeholders["nom"] == "Destination OAuth"

    texte_du_probleme = _description_du_probleme(
        langue, probleme.translation_placeholders
    )
    assert ECRAN_DES_REPARATIONS[langue] in texte_du_probleme
    assert ACTION_DE_REAUTORISATION[langue] in texte_du_probleme
    assert MENU_CONFIGURER[langue] in texte_du_probleme

    # La notification persistante est réellement créée (pas seulement le texte
    # brut du fichier de traduction).
    notifications = persistent_notification._async_get_or_create_notifications(hass)
    notification = notifications.get(
        identifiant_de_notification_de_reauthentification("destination_oauth")
    )
    assert notification is not None
    assert ECRAN_DES_REPARATIONS[langue] in notification["message"]
    assert ACTION_DE_REAUTORISATION[langue] in notification["message"]
    assert MENU_CONFIGURER[langue] in notification["message"]


### Critère 2 : aucun texte du fork ne désigne l'écran disparu ###

LIBELLES_ECRAN_INEXISTANT = ("interface des intégrations", "integrations dashboard")

# CHANGELOG.md est volontairement exclu : il relate, à titre historique, la
# disparition de ce libellé (issue #55) et le cite donc légitimement.
FICHIERS_TEXTES_DU_FORK = [
    TRADUCTIONS / "fr.json",
    TRADUCTIONS / "en.json",
    RACINE_DEPOT / "README.md",
    *sorted((RACINE_DEPOT / "docs").rglob("*.md")),
    *sorted((INTEGRATION).rglob("*.py")),
]


def test_critere2_aucun_texte_du_fork_ne_designe_l_ecran_disparu() -> None:
    """Given les textes du fork, When on cherche l'écran disparu.

    Then rien ne le trouve.
    """
    fautifs = [
        (str(fichier.relative_to(RACINE_DEPOT)), libelle)
        for fichier in FICHIERS_TEXTES_DU_FORK
        for libelle in LIBELLES_ECRAN_INEXISTANT
        if libelle in fichier.read_text(encoding="utf-8").casefold()
    ]
    assert not fautifs, f"écran inexistant encore désigné : {fautifs}"


### Critère 3 : en.json en anglais américain, au-delà de « authoris » ###

# Britannismes courants au-delà de « authoris » (déjà couvert par
# `tests/test_traductions.py`) : `-ise`/`-isation`, mots en `-our`, `-re`, et
# doublement du « l » avant un suffixe (`cancelled`, `travelled`…), propre à
# l'orthographe britannique — l'américain garde un seul « l » (`canceled`).
MOTIFS_BRITANNIQUES = [
    r"authoris",
    r"colour",
    r"behaviour",
    r"favour",
    r"honour",
    r"neighbour",
    r"rumour",
    r"armour",
    r"humour",
    r"endeavour",
    r"\bcentre\b",
    r"\bmetre\b",
    r"\blitre\b",
    r"\btheatre\b",
    r"\bfibre\b",
    r"cancell",
    r"travell",
    r"modell",
    r"labell",
    r"signall",
    r"organis(e|ed|ing|ation)",
    r"recognis(e|ed|ing)",
    r"realis(e|ed|ing|ation)",
    r"customis(e|ed|ing)",
    r"minimis(e|ed|ing)",
    r"maximis(e|ed|ing)",
    r"optimis(e|ed|ing)",
    r"finalis(e|ed|ing)",
    r"initialis(e|ed|ing)",
    r"summaris(e|ed|ing)",
    r"characteris(e|ed|ing)",
    r"categoris(e|ed|ing)",
    r"emphasis(e|ed|ing)",
    r"prioritis(e|ed|ing)",
    r"standardis(e|ed|ing)",
    r"synchronis(e|ed|ing)",
    r"capitalis(e|ed|ing)",
    r"specialis(e|ed|ing)",
    r"utilis(e|ed|ing)",
    r"\bfulfil\b",
    r"\benrol\b",
    r"\bjudgement\b",
    r"\bdefence\b",
    r"\blicence\b",
    r"\bgrey\b",
]
MOTIF_BRITANNIQUE = re.compile("|".join(MOTIFS_BRITANNIQUES), re.IGNORECASE)


def _valeurs_de_texte(valeur: Any):
    """Parcourt les chaînes **valeurs** d'un JSON de traduction (pas les clés)."""
    if isinstance(valeur, dict):
        for sous_valeur in valeur.values():
            yield from _valeurs_de_texte(sous_valeur)
    elif isinstance(valeur, str):
        yield valeur


def test_critere3_en_json_suit_integralement_l_orthographe_americaine() -> None:
    """Given en.json, When on lit ses textes.

    Then aucun britannisme, pas même hors « authoris ».
    """
    donnees = json.loads((TRADUCTIONS / "en.json").read_text(encoding="utf-8"))
    fautifs = [
        (texte, trouve.group(0))
        for texte in _valeurs_de_texte(donnees)
        if (trouve := MOTIF_BRITANNIQUE.search(texte)) is not None
    ]
    assert not fautifs, f"orthographe britannique détectée dans en.json : {fautifs}"


### Critère 4 : le message `unknown` ne l'attribue pas au fournisseur ###


@pytest.mark.parametrize(
    ("langue", "mot_fournisseur"), [("fr", "fournisseur"), ("en", "provider")]
)
async def test_critere4_l_erreur_inconnue_n_accuse_pas_le_fournisseur(
    hass: HomeAssistant, langue: str, mot_fournisseur: str
) -> None:
    """Given une erreur interne non qualifiée, When elle survient.

    Then le fournisseur n'est pas cité.
    """
    hass.config.language = langue

    # Sans précision de `code`, `DestinationError` retombe sur `unknown` : le
    # cas d'une erreur interne ou inattendue, que rien ne rattache à un
    # fournisseur particulier.
    erreur = DestinationError("détail technique interne, jamais montré à l'utilisateur")
    assert erreur.code is CodeErreur.INCONNUE
    assert erreur.translation_key == cle_de_traduction_de_l_erreur(CodeErreur.INCONNUE)

    message = await async_message_d_erreur(hass, erreur.code)

    assert mot_fournisseur not in message.casefold()
    assert "journal" in message or "log" in message


### Critère 5 : troncature d'une valeur d'`upload_to` très longue ###


async def test_critere5_une_valeur_upload_to_tres_longue_est_tronquee_dans_le_refus(
    hass: HomeAssistant,
) -> None:
    """Given un upload_to très long, When la destination est inconnue.

    Then la valeur répétée dans le refus est tronquée.
    """
    demandee = "d" * 5000

    with pytest.raises(ServiceValidationError) as erreur:
        async_resoudre_destinations(hass, [demandee])

    affichee = erreur.value.translation_placeholders["destination"]
    assert len(affichee) == LONGUEUR_MAX_DESTINATION_AFFICHEE
    assert affichee.endswith("…")
    assert demandee not in str(erreur.value)
    assert demandee not in repr(erreur.value.translation_placeholders)


### Critère 6 : hassfest passe, et les tests existants restent verts ###

# `hassfest` est un contrôle de CI (voir `.github/workflows/`) : ce dépôt ne
# fournit pas de suite e2e locale pour le rejouer (CLAUDE.md, section
# « Commandes »). Ce test vérifie ce qui est vérifiable en local — la
# structure JSON des traductions — et le reste de la suite (notamment
# `tests/test_traductions.py` et `tests/test_doc_utilisateur.py`, cités par le
# critère) est exécuté avec ce module par `uv run pytest`.


def test_critere6_les_fichiers_de_traduction_restent_du_json_valide() -> None:
    """Given la CI, When hassfest s'exécute.

    Then les traductions restent un JSON structuré valide.
    """
    for langue in ("fr", "en"):
        donnees = json.loads(
            (TRADUCTIONS / f"{langue}.json").read_text(encoding="utf-8")
        )
        assert isinstance(donnees, dict)
        assert donnees["issues"]["reauthentification_requise"]["description"]
        assert donnees["exceptions"]["erreur_unknown"]["message"]
