"""Documentation de connexion à Google Drive (critère 6 de l'issue #13).

Critère : « GIVEN `docs/destinations/google-drive.md` WHEN je le lis THEN la
création du projet Google Cloud, l'activation de l'API Drive, l'écran de
consentement, le type d'identifiants et l'URI de redirection à renseigner y
sont décrits en français. »

Ces tests lisent le guide comme le ferait un utilisateur et vérifient que
chaque étape exigée par le critère y figure, en français, et que la portée
OAuth2 et l'URI de redirection qu'il documente restent cohérentes avec ce que
le fournisseur `google_drive` déclare réellement dans le code (aucune valeur
divergente ou réelle n'est employée).
"""

from __future__ import annotations

from pathlib import Path

from custom_components.auto_backup.const import DOMAIN, OAUTH_CALLBACK_PATH
from custom_components.auto_backup.destinations.providers.google_drive import (
    PORTEE_DRIVE_FILE,
)

RACINE_DEPOT = Path(__file__).resolve().parent.parent
CHEMIN_DOC = RACINE_DEPOT / "docs" / "destinations" / "google-drive.md"


def _doc() -> str:
    return CHEMIN_DOC.read_text(encoding="utf-8")


def test_le_guide_google_drive_existe() -> None:
    """Le guide décrit par le critère est bien livré avec l'issue."""
    assert CHEMIN_DOC.is_file()


def test_le_guide_decrit_la_creation_du_projet_google_cloud() -> None:
    """Étape 1 du critère : création du projet Google Cloud, en français."""
    texte = _doc()

    assert "Créer un projet Google Cloud" in texte
    assert "console Google Cloud" in texte
    assert "Nouveau projet" in texte


def test_le_guide_decrit_l_activation_de_l_api_drive() -> None:
    """Étape 2 du critère : activation de l'API Google Drive, en français."""
    texte = _doc()

    assert "Activer l'API Google Drive" in texte
    assert "Google Drive API" in texte
    assert "Activer" in texte


def test_le_guide_decrit_l_ecran_de_consentement() -> None:
    """Étape 3 du critère : configuration de l'écran de consentement OAuth."""
    texte = _doc()

    assert "Configurer l'écran de consentement" in texte
    assert "Écran de consentement OAuth" in texte
    # Le type « Externe » et les utilisateurs test sont des points de blocage
    # fréquents tant que l'application n'est pas publiée : le guide les couvre.
    assert "Externe" in texte
    assert "Utilisateurs test" in texte


def test_le_guide_decrit_le_type_d_identifiants_a_creer() -> None:
    """Étape 4 du critère : type d'identifiants OAuth attendu par Google."""
    texte = _doc()

    assert "Créer les identifiants OAuth" in texte
    assert "Application Web" in texte
    # Les types qui ne conviennent pas sont explicitement écartés.
    assert "Ordinateur" in texte


def test_le_guide_decrit_l_uri_de_redirection_a_renseigner() -> None:
    """Étape 5 du critère : URI de redirection, cohérente avec le code du fork.

    Le fork n'utilise pas la vue OAuth2 standard de Home Assistant : sa propre
    route (`OAUTH_CALLBACK_PATH`) doit donc être celle documentée, sinon
    l'utilisateur déclarerait une adresse que Google refuserait au retour.
    """
    texte = _doc()

    assert "URI de redirection" in texte
    assert OAUTH_CALLBACK_PATH in texte
    assert f"/auth/{DOMAIN}/callback" in texte
    # Google n'accepte qu'une adresse HTTPS publique : le prérequis est nommé.
    assert "HTTPS" in texte
    assert "domaine public" in texte


def test_la_portee_documentee_correspond_exactement_a_celle_du_code() -> None:
    """La portée `drive.file` promise à l'utilisateur est celle réellement
    demandée par `SPEC_OAUTH_GOOGLE_DRIVE` : aucune divergence entre le guide
    et le code.
    """
    texte = _doc()

    assert PORTEE_DRIVE_FILE in texte
    assert PORTEE_DRIVE_FILE == "https://www.googleapis.com/auth/drive.file"


def test_le_guide_ne_contient_aucun_identifiant_ni_jeton_reel() -> None:
    """Critère 7 (non-régression) : le guide n'emploie que des exemples factices,
    jamais un identifiant Google réel.
    """
    texte = _doc()

    assert "apps.googleusercontent.com" not in texte
    assert "GOCSPX-" not in texte


def test_le_guide_est_redige_en_francais() -> None:
    """L'ensemble du texte destiné aux utilisateurs est en français (CLAUDE.md)."""
    texte = _doc()

    # Quelques tournures qui n'apparaîtraient pas dans une page anglaise.
    for tournure in (
        "votre instance Home Assistant",
        "identifiants OAuth",
        "vous créez votre propre application Google",
    ):
        assert tournure in texte
