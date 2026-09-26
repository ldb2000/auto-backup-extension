"""Masquage des secrets, point unique du fork (`destinations/masquage.py`).

Ces tests sont des **vecteurs**, pas des lignes de code : la version précédente
du masquage affichait 100 % de couverture de lignes tout en laissant passer un
jeton Dropbox nu, les deux formes de jetons Google, une URL de session, une
adresse électronique et une suite base64 sans mot-clé. Chaque cas relevé par
l'audit de sécurité a donc son entrée ici, à côté des formes déjà couvertes et
des réserves explicitement assumées.

Aucune valeur réelle n'est employée : les jetons, chemins et adresses cités sont
inventés (`example.invalid`, marqueur `FaCtIcE`) et servent justement à vérifier
qu'ils n'apparaissent **pas** en sortie.
"""

from __future__ import annotations

import pytest

from custom_components.auto_backup.destinations.masquage import (
    LONGUEUR_MIN_SUITE_OPAQUE,
    masquer,
    masquer_un_nom,
)
from custom_components.auto_backup.destinations.models import VALEUR_MASQUEE

### Vecteurs relevés par l'audit de sécurité ###

# Chacun de ces messages passait **intégralement** l'ancien masquage, qui ne
# reconnaissait que `Bearer <jeton>`, `clé=valeur` et les chemins absolus.
JETON_DROPBOX = "sl.B1a2C3FaCtIcE-0123456789abcdefghij"
JETON_GOOGLE = "ya29.a0AfH6FaCtIcE-0123456789abcdef"
RAFRAICHISSEMENT_GOOGLE = "1//0gAbCdFaCtIcE-0123456789abcdef"
IDENTIFIANT_DE_SESSION = "AAAAFaCtIcE0123456789abcdefghijkl"
COURRIEL = "jean.dupont@example.invalid"
SUITE_BASE64URL = "QUJDRGVmR2hJaktMbU5vUHFSc1R1Vnd4WXo5ODc2NTQzMjEw"


@pytest.mark.parametrize(
    ("brut", "secret"),
    [
        pytest.param(
            f"invalid access token {JETON_DROPBOX}",
            JETON_DROPBOX,
            id="jeton-dropbox-nu",
        ),
        pytest.param(
            f"le jeton {JETON_GOOGLE} a été refusé",
            JETON_GOOGLE,
            id="jeton-acces-google-nu",
        ),
        pytest.param(
            f"refresh failed for {RAFRAICHISSEMENT_GOOGLE}",
            RAFRAICHISSEMENT_GOOGLE,
            id="jeton-rafraichissement-google-nu",
        ),
        pytest.param(
            "session expirée : https://www.googleapis.invalid/upload/drive/v3/files"
            f"?uploadType=resumable&upload_id={IDENTIFIANT_DE_SESSION}",
            IDENTIFIANT_DE_SESSION,
            id="url-de-session-reprenable",
        ),
        pytest.param(
            f"le compte {COURRIEL} n'a pas autorisé l'application",
            COURRIEL,
            id="adresse-electronique",
        ),
        pytest.param(
            f"réponse inattendue du fournisseur : {SUITE_BASE64URL}",
            SUITE_BASE64URL,
            id="suite-base64-sans-mot-cle",
        ),
    ],
)
def test_les_vecteurs_de_l_audit_ne_fuient_plus(brut: str, secret: str) -> None:
    """Aucun des vecteurs relevés par la sécurité n'apparaît en sortie."""
    masque = masquer(brut)

    assert secret not in masque
    assert VALEUR_MASQUEE in masque


def test_un_message_bavard_est_masque_de_bout_en_bout() -> None:
    """Un fournisseur qui recopie toute la requête refusée ne fuit rien.

    Le cas réunit les six vecteurs : une seule passe ne suffirait pas, et
    l'ordre des passes compte (l'adresse d'abord, la suite opaque en dernier).
    """
    masque = masquer(
        f"POST refusé (Bearer {JETON_DROPBOX}) pour {COURRIEL} : "
        f'{{"refresh_token": "{RAFRAICHISSEMENT_GOOGLE}", '
        f'"access_token": "{JETON_GOOGLE}"}} '
        f"sur https://www.googleapis.invalid/upload?upload_id={IDENTIFIANT_DE_SESSION} "
        f"en lisant /config/backups/nuit.tar, réponse {SUITE_BASE64URL}"
    )

    for secret in (
        JETON_DROPBOX,
        JETON_GOOGLE,
        RAFRAICHISSEMENT_GOOGLE,
        IDENTIFIANT_DE_SESSION,
        COURRIEL,
        SUITE_BASE64URL,
        "/config/backups/nuit.tar",
    ):
        assert secret not in masque
    # Le message reste diagnosticable : le verbe, l'hôte et la clé subsistent.
    assert "POST refusé" in masque
    assert "www.googleapis.invalid" in masque


### Formes d'affectation et d'en-tête ###


@pytest.mark.parametrize(
    ("brut", "attendu"),
    [
        # En-têtes d'autorisation, quelle que soit la casse.
        pytest.param("Bearer jeton-factice-1", "Bearer ***", id="bearer"),
        pytest.param("bearer jeton-factice-1", "bearer ***", id="bearer-minuscule"),
        pytest.param("Basic dXRpbGlzYXRldXI=", "Basic ***", id="basic"),
        # Affectations : la clé est conservée, la forme du message aussi.
        pytest.param(
            "access_token=acces-factice-1", "access_token=***", id="egal-sans-espace"
        ),
        pytest.param(
            '"refresh_token": "rafraichissement-factice-1"',
            '"refresh_token": "***"',
            id="json-guillemets",
        ),
        pytest.param(
            "client_secret = secret-factice",
            "client_secret = ***",
            id="egal-avec-espaces",
        ),
        pytest.param("api_key: cle-factice", "api_key: ***", id="deux-points"),
        pytest.param(
            "authorization_code=code-factice",
            "authorization_code=***",
            id="cle-suffixee-par-mot-cle",
        ),
        pytest.param(
            f"upload_id={IDENTIFIANT_DE_SESSION}",
            "upload_id=***",
            id="identifiant-de-session",
        ),
        # Formes que l'ancien masquage manquait : camelCase, tiret, pluriel.
        pytest.param(
            "accessToken: acces-factice-1", "accessToken: ***", id="camel-case"
        ),
        pytest.param(
            "access-token=acces-factice-1", "access-token=***", id="separateur-tiret"
        ),
        pytest.param(
            "dropboxToken=acces-factice-1", "dropboxToken=***", id="cle-prefixee"
        ),
        pytest.param(
            "tokens=[acces-factice-1,acces-factice-2]",
            "tokens=***",
            id="liste-entiere",
        ),
        pytest.param(
            '"token": {"access_token": "acces-factice-1", "expires_in": 3600}',
            '"token": ***',
            id="objet-json-entier",
        ),
        # Un mot-clé sans valeur n'entraîne aucun masquage.
        pytest.param("secret", "secret", id="mot-cle-seul"),
    ],
)
def test_les_affectations_sensibles_sont_masquees(brut: str, attendu: str) -> None:
    """La valeur disparaît, la clé et la ponctuation restent."""
    assert masquer(brut) == attendu


def test_une_collection_de_jetons_ne_fuit_pas_par_son_second_element() -> None:
    """Le masquage porte sur la collection entière, pas sur son premier élément.

    Une valeur bornée au premier séparateur laisserait fuir tous les éléments
    suivants d'une liste ou d'un objet JSON de jetons.
    """
    masque = masquer(f"tokens=[{JETON_DROPBOX},{JETON_GOOGLE}]")

    assert JETON_DROPBOX not in masque
    assert JETON_GOOGLE not in masque


### Chemins de fichiers absolus ###


@pytest.mark.parametrize(
    ("brut", "attendu"),
    [
        pytest.param(
            "fichier /config/backups/ha.tar illisible",
            "fichier *** illisible",
            id="racine-config",
        ),
        pytest.param(
            "fichier /backup/abcd1234.tar absent",
            "fichier *** absent",
            id="racine-backup",
        ),
        pytest.param(
            "fichier /backups/nuit.tar absent",
            "fichier *** absent",
            id="racine-backups-au-pluriel",
        ),
        pytest.param(
            "dossier /addon_configs/xyz refusé",
            "dossier *** refusé",
            id="racine-la-plus-longue",
        ),
        pytest.param(
            "échec sur /Users/moi/backups/nuit.tar",
            "échec sur ***",
            id="racine-de-poste-de-developpement",
        ),
        pytest.param(
            "échec sur ~/backups/nuit.tar",
            "échec sur ~***",
            id="chemin-relatif-au-foyer",
        ),
        pytest.param(
            'lecture de "/config/secrets.yaml" refusée',
            'lecture de "***" refusée',
            id="chemin-entre-guillemets",
        ),
    ],
)
def test_les_chemins_absolus_sont_masques(brut: str, attendu: str) -> None:
    """Un chemin local révèle l'arborescence de l'hôte et le nom des sauvegardes."""
    assert masquer(brut) == attendu


def test_un_chemin_au_pluriel_ne_fuit_pas_le_nom_de_la_sauvegarde() -> None:
    """`/backups/...` était masqué à moitié : la racine, pas le fichier.

    L'alternative `backup` l'emportait sur `backups`, et le nom de la sauvegarde
    sortait tel quel (`***s/nuit.tar`).
    """
    assert "nuit.tar" not in masquer("écriture de /backups/nuit.tar refusée")


def test_une_url_de_fournisseur_reste_lisible() -> None:
    """Une URL n'est pas un chemin local : le diagnostic en a besoin."""
    url = "https://fournisseur.invalid/config/v3/about"

    assert masquer(f"appel refusé par {url}") == f"appel refusé par {url}"


### Messages légitimes en français ###


@pytest.mark.parametrize(
    "message",
    [
        pytest.param("quota dépassé", id="quota"),
        pytest.param("token expiré", id="token-suivi-d-un-mot"),
        pytest.param("code de la sauvegarde introuvable", id="code-suivi-d-un-article"),
        pytest.param("mot de passe refusé par le fournisseur", id="mot-de-passe"),
        pytest.param("délai de téléversement dépassé (1800 s)", id="delai-et-nombre"),
        pytest.param("erreur 502 au bout de 3 tentatives", id="code-http-nu"),
        pytest.param(
            "la destination « Dropbox perso » a refusé l'envoi", id="nom-de-destination"
        ),
        pytest.param("le nom de la sauvegarde est trop long", id="phrase-longue"),
    ],
)
def test_un_message_legitime_reste_lisible(message: str) -> None:
    """Réserve reprise de #16 : le français ordinaire n'est pas mutilé.

    Le mot qui suit un mot-clé séparé par une simple espace est épargné :
    « token expiré » et « code de la sauvegarde » restent lisibles. Le masquer
    n'aurait rien protégé et aurait rendu les notifications incompréhensibles.
    """
    assert masquer(message) == message


### Noms de la configuration du fork (passes 1 à 5) ###

# Des noms parfaitement ordinaires, mais sans espace : la passe 6 les prenait
# tous pour des suites opaques, et l'utilisateur lisait « échec d'envoi vers
# « *** » » sans pouvoir dire laquelle de ses destinations avait lâché.
NOMS_ORDINAIRES_SANS_ESPACE = (
    "Dropbox-Compte-Familial",
    "Sauvegardes-Maison-Principale",
    "GoogleDriveMaisonPrincipale",
    "sauvegarde-complete-2026-09-26",
    "auto_backup_2026_09_26_03_00",
)


@pytest.mark.parametrize("nom", NOMS_ORDINAIRES_SANS_ESPACE)
def test_un_nom_ordinaire_sans_espace_reste_lisible(nom: str) -> None:
    """Un nom du fork échappe au dernier filet, et lui seul.

    Le masquage complet — celui de la cause d'un échec — masque bien ces noms :
    c'est la démonstration que la distinction est nécessaire, et non un
    embellissement.
    """
    assert len(nom) >= LONGUEUR_MIN_SUITE_OPAQUE
    assert masquer(nom) == VALEUR_MASQUEE

    assert masquer_un_nom(nom) == nom


@pytest.mark.parametrize(
    ("brut", "attendu"),
    [
        pytest.param(
            "Dropbox /config/backups/abcd1234.tar", "Dropbox ***", id="chemin-absolu"
        ),
        pytest.param(f"nuit {JETON_DROPBOX}", "nuit ***", id="jeton-dropbox-nu"),
        pytest.param(f"Compte {COURRIEL}", "Compte ***", id="adresse-electronique"),
        pytest.param(
            f"Compte Bearer {JETON_DROPBOX}", "Compte Bearer ***", id="en-tete"
        ),
        pytest.param(
            f"Dropbox access_token={JETON_GOOGLE}",
            "Dropbox access_token=***",
            id="cle-sensible",
        ),
    ],
)
def test_un_nom_reste_soumis_aux_cinq_premieres_passes(brut: str, attendu: str) -> None:
    """Un secret collé dans un nom est masqué comme ailleurs.

    Le nom d'une destination est saisi par l'utilisateur et celui d'une
    sauvegarde peut venir d'une automatisation : écarter le dernier filet
    n'ouvre pas la porte aux secrets reconnaissables à leur forme.
    """
    assert masquer_un_nom(brut) == attendu


### Réserves assumées ###


def test_reserve_un_code_d_erreur_affecte_est_masque() -> None:
    """`code=500` est masqué : sur-couverture assumée, jamais une fuite.

    La clé `code` couvre `authorization_code`, qu'il faut masquer ; la
    distinguer d'un code d'erreur HTTP demanderait de deviner. Le choix est de
    perdre un élément de diagnostic plutôt que de laisser passer un secret.
    """
    assert masquer("échec avec code=500") == "échec avec code=***"


def test_reserve_un_secret_en_base64_standard_peut_ne_pas_etre_masque() -> None:
    """Angle mort documenté : les tronçons courts d'un base64 standard sortent.

    `/`, `=` et `.` sont exclus du jeu de la dernière passe pour ne pas masquer
    les URL ; ils découpent donc un secret en base64 standard. Si aucun tronçon
    n'atteint `LONGUEUR_MIN_SUITE_OPAQUE`, il n'est pas masqué du tout. Non
    exploitable aujourd'hui : Dropbox et Google émettent du base64url (`-` et
    `_`, jamais `/`), et leurs formes sont reconnues par la passe dédiée.
    """
    troncon = "b64secretchunk1"
    assert len(troncon) < LONGUEUR_MIN_SUITE_OPAQUE

    assert troncon in masquer(f"{troncon}/b64secretchunk2==")


def test_reserve_un_secret_court_hors_de_portee_d_un_mot_cle_sort_intact() -> None:
    """Angle mort documenté : mot-clé non listé, ou mot intercalé.

    Une valeur de moins de vingt caractères voisine d'un mot-clé absent de la
    liste, ou séparée d'un mot-clé listé par un mot intercalé, ne déclenche
    aucune passe. Les motifs ne sont pas étendus pour tolérer des mots
    intercalés : le gain est nul sur les deux fournisseurs intégrés, dont les
    jetons sont longs et reconnus par leur forme, et le coût serait une salve de
    faux positifs sur les phrases françaises.
    """
    assert masquer("session id sess_AbCdEf12 expired") == (
        "session id sess_AbCdEf12 expired"
    )
    assert masquer("secret is court-factice") == "secret is court-factice"


@pytest.mark.parametrize(
    "suite",
    [
        pytest.param("Anticonstitutionnellement", id="mot-francais-a-capitale"),
        pytest.param("storageQuotaExceeded", id="code-google-quota"),
        pytest.param("userRateLimitExceeded", id="code-google-cadence"),
        pytest.param("expired_access_token", id="code-dropbox-jeton-expire"),
        pytest.param("too_many_write_operations", id="code-dropbox-ecritures"),
    ],
)
def test_reserve_un_mot_ou_un_code_technique_interminable_est_masque(
    suite: str,
) -> None:
    """La passe 6 avale aussi les codes d'erreur des fournisseurs.

    Le mot français à capitale initiale est un cas théorique ; la portée réelle
    de la réserve, ce sont les codes techniques de vingt caractères ou plus que
    Dropbox et Google renvoient. C'est acceptable : les fournisseurs du fork
    traduisent la cause principale en français et n'ajoutent le code brut qu'en
    appendice, si bien que la cause reste diagnosticable une fois le code
    masqué. La réserve ne porte que sur la cause : les noms passent par
    `masquer_un_nom()`.
    """
    assert len(suite) >= LONGUEUR_MIN_SUITE_OPAQUE

    assert masquer(suite) == VALEUR_MASQUEE
    assert masquer_un_nom(suite) == suite


def test_un_mot_interminable_en_minuscules_reste_lisible() -> None:
    """Une seule casse et aucun chiffre : c'est un mot, pas un secret."""
    mot = "anticonstitutionnellement"
    assert len(mot) >= LONGUEUR_MIN_SUITE_OPAQUE

    assert masquer(mot) == mot


### Troncature ###


def test_sans_longueur_max_le_texte_n_est_pas_coupe() -> None:
    """Une notification persistante peut accueillir un message entier."""
    message = "a" * 500

    assert masquer(message) == message


def test_avec_longueur_max_le_texte_est_borne_et_termine_par_une_ellipse() -> None:
    """Un attribut d'entité (#16) est recopié dans chaque état historisé."""
    masque = masquer("mot " * 200, longueur_max=64)

    assert len(masque) == 64
    assert masque.endswith("…")


def test_un_texte_plus_court_que_la_borne_est_rendu_tel_quel() -> None:
    """La troncature ne s'applique qu'au-delà de la borne."""
    assert masquer("quota dépassé", longueur_max=255) == "quota dépassé"


def test_un_texte_vide_reste_vide() -> None:
    """Aucune passe ne fabrique de contenu à partir de rien."""
    assert masquer("") == ""
