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

import logging
from pathlib import Path

import pytest

from custom_components.auto_backup.destinations.masquage import (
    CODES_D_ERREUR_CONNUS,
    LONGUEUR_MIN_SUITE_OPAQUE,
    decrire_l_exception,
    journaliser_une_exception,
    masquer,
    masquer_un_nom,
    trace_masquee,
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


def test_reserve_un_mot_interminable_a_capitale_est_masque() -> None:
    """La passe 6 avale un mot français à capitale initiale : cas théorique.

    Il mêle deux casses, comme un jeton : la passe ne sait pas l'en distinguer.
    La réserve ne porte que sur la cause : les noms passent par
    `masquer_un_nom()`.
    """
    mot = "Anticonstitutionnellement"
    assert len(mot) >= LONGUEUR_MIN_SUITE_OPAQUE

    assert masquer(mot) == VALEUR_MASQUEE
    assert masquer_un_nom(mot) == mot


# Homoglyphe cyrillique U+0430 (petite lettre a cyrillique), écrit par son
# échappement : à l'œil, il ne se distingue pas du « a » latin, et c'est
# justement ce qui en fait un séparateur invisible pour la passe 6.
HOMOGLYPHE_CYRILLIQUE = "\u0430"


@pytest.mark.parametrize(
    ("brut", "avant", "apres"),
    [
        pytest.param(
            f"refus du fournisseur : FaCtIcE01234{HOMOGLYPHE_CYRILLIQUE}56789FaCtIcE",
            "FaCtIcE01234",
            "56789FaCtIcE",
            id="homoglyphe-au-milieu",
        ),
        pytest.param(
            f"refus : FaCtIcE0123456789{HOMOGLYPHE_CYRILLIQUE}bcdefghijkl",
            "FaCtIcE0123456789",
            "bcdefghijkl",
            id="homoglyphe-decale",
        ),
    ],
)
def test_reserve_une_suite_coupee_par_un_caractere_non_ascii_sort_intacte(
    brut: str, avant: str, apres: str
) -> None:
    """Angle mort documenté (#54) : un caractère non ASCII coupe la suite opaque.

    La passe 6 ne reconnaît une suite que sur `[A-Za-z0-9_+-]`. Un caractère non
    ASCII — ici un homoglyphe cyrillique — la découpe en deux morceaux de moins
    de `LONGUEUR_MIN_SUITE_OPAQUE` caractères chacun, que le dernier filet ne
    voit plus : la suite entière sort intacte. Accepté : les jetons des
    fournisseurs intégrés sont en ASCII et reconnus par leur forme (passe 4) ou
    par la clé qui les accompagne toujours (passe 3), aucun secret n'est
    fabriqué par un tiers, et élargir la passe à l'Unicode masquerait les mots
    français accentués.
    """
    suite = f"{avant}{HOMOGLYPHE_CYRILLIQUE}{apres}"
    assert suite in brut
    assert len(suite) >= LONGUEUR_MIN_SUITE_OPAQUE
    assert len(avant) < LONGUEUR_MIN_SUITE_OPAQUE
    assert len(apres) < LONGUEUR_MIN_SUITE_OPAQUE
    # Sans l'homoglyphe, la même suite est bien masquée par la passe 6.
    assert masquer(f"{avant}a{apres}") == VALEUR_MASQUEE

    assert masquer(brut) == brut


### Codes d'erreur connus des fournisseurs (#48) ###

# Codes que la passe 6 masquait avant #48, faute de liste blanche : c'est pour
# eux que la liste existe. Ils doivent tous y figurer.
CODES_LONGS_EXIGES = (
    "expired_access_token",
    "userRateLimitExceeded",
    "too_many_write_operations",
    "storageQuotaExceeded",
)


def test_la_liste_blanche_contient_les_codes_exiges_par_l_issue() -> None:
    """Critère 1 : les cinq codes nommés par l'issue sont dans la liste."""
    for code in (*CODES_LONGS_EXIGES, "insufficient_space"):
        assert code in CODES_D_ERREUR_CONNUS


@pytest.mark.parametrize("code", sorted(CODES_D_ERREUR_CONNUS))
def test_chaque_code_est_justifie_par_sa_source(code: str) -> None:
    """Critère 1 : chaque code cite la documentation de son fournisseur."""
    source = CODES_D_ERREUR_CONNUS[code]
    assert source.startswith(("Dropbox API v2", "Google Drive API"))


@pytest.mark.parametrize("code", sorted(CODES_D_ERREUR_CONNUS))
def test_aucun_code_ne_peut_coincider_avec_un_jeton(code: str) -> None:
    """Critère 5 : un code est un identifiant de mots, pas une valeur aléatoire.

    Un jeton réel porte des chiffres ou un préfixe reconnu (`sl.`, `ya29.`,
    `1//`) ; un code de la liste n'est fait que de lettres et de `_`. Et il ne
    doit contenir aucun des caractères qui coupent une suite opaque, sans quoi
    la comparaison exacte ne pourrait jamais le reconnaître.
    """
    assert code.replace("_", "").isalpha()
    assert code.isascii()
    assert not code.startswith(("sl", "ya29", "1//"))


@pytest.mark.parametrize("code", sorted(CODES_D_ERREUR_CONNUS))
def test_un_code_connu_reste_intact_a_cote_d_un_secret_masque(code: str) -> None:
    """Critère 2 : le code reste lisible, le reste est masqué comme avant."""
    brut = (
        f"échec (motif : {code}) avec access_token={IDENTIFIANT_DE_SESSION} "
        f"et {SUITE_BASE64URL}"
    )

    masque = masquer(brut)

    assert masque == (
        f"échec (motif : {code}) avec access_token={VALEUR_MASQUEE} et {VALEUR_MASQUEE}"
    )
    assert masquer(masque) == masque


@pytest.mark.parametrize("code", CODES_LONGS_EXIGES)
def test_un_code_long_seul_reste_intact(code: str) -> None:
    """Critère 2 : ces codes, que la passe 6 masquait avant #48, passent."""
    assert len(code) >= LONGUEUR_MIN_SUITE_OPAQUE

    assert masquer(code) == code


@pytest.mark.parametrize(
    ("brut", "attendu"),
    [
        # `error_summary` Dropbox tel que le fournisseur le recopie : le code
        # est borné par `/`, qui coupe la suite opaque.
        pytest.param(
            "(HTTP 401) : expired_access_token/",
            "(HTTP 401) : expired_access_token/",
            id="dropbox-jeton-expire-barre",
        ),
        pytest.param(
            "(HTTP 401) : expired_access_token/..",
            "(HTTP 401) : expired_access_token/..",
            id="dropbox-jeton-expire-points",
        ),
        pytest.param(
            "(HTTP 409) : path/insufficient_space/..",
            "(HTTP 409) : path/insufficient_space/..",
            id="dropbox-espace-sature",
        ),
        pytest.param(
            "(HTTP 429) : too_many_write_operations/...",
            "(HTTP 429) : too_many_write_operations/...",
            id="dropbox-ecritures",
        ),
        pytest.param(
            "(HTTP 409) : path/too_many_write_operations/.",
            "(HTTP 409) : path/too_many_write_operations/.",
            id="dropbox-ecritures-sous-champ",
        ),
        # Motifs Google, séparés par une virgule par `erreur_de_la_reponse()`.
        pytest.param(
            "(motif : storageQuotaExceeded, userRateLimitExceeded)",
            "(motif : storageQuotaExceeded, userRateLimitExceeded)",
            id="google-deux-motifs",
        ),
    ],
)
def test_un_code_connu_est_reconnu_dans_son_contexte(brut: str, attendu: str) -> None:
    """Les séparateurs réels (`/`, `.`, `,`, `)`) bornent bien la suite."""
    assert masquer(brut) == attendu


@pytest.mark.parametrize(
    "suite",
    [
        pytest.param("expired_access_tokenAbC", id="suffixe"),
        pytest.param("Xexpired_access_token", id="prefixe"),
        pytest.param("expired_access_token_2", id="suffixe-chiffre"),
        pytest.param("expired_access_token-FaCtIcE", id="suffixe-tiret"),
        pytest.param("EXPIRED_ACCESS_TOKEN", id="majuscules"),
        pytest.param("Expired_Access_Token", id="casse-mixte"),
        pytest.param("UserRateLimitExceeded", id="capitale-initiale"),
        pytest.param("userratelimitexceeded1", id="minuscules-chiffre"),
        pytest.param("storageQuotaExceeded+", id="suffixe-plus"),
        pytest.param("too_many_write_operations_", id="suffixe-souligne"),
        pytest.param("tooManyWriteOperationsFaCtIcE", id="code-inconnu"),
    ],
)
def test_une_suite_proche_d_un_code_reste_masquee(suite: str) -> None:
    """Critère 3 : comparaison stricte, jamais un préfixe ni une casse libre."""
    assert len(suite) >= LONGUEUR_MIN_SUITE_OPAQUE
    assert suite not in CODES_D_ERREUR_CONNUS

    assert masquer(suite) == VALEUR_MASQUEE


@pytest.mark.parametrize(
    ("brut", "attendu"),
    [
        # Passe 3 : la valeur d'une clé sensible reste masquée, même égale à
        # un code de la liste.
        pytest.param(
            "access_token=expired_access_token",
            f"access_token={VALEUR_MASQUEE}",
            id="valeur-de-cle-egale-a-un-code",
        ),
        pytest.param(
            '"refresh_token": "userRateLimitExceeded"',
            f'"refresh_token": "{VALEUR_MASQUEE}"',
            id="valeur-json-egale-a-un-code",
        ),
        pytest.param(
            "invalid token storageQuotaExceeded",
            f"invalid token {VALEUR_MASQUEE}",
            id="mot-cle-espace-code",
        ),
        # Passe 4 : un jeton reconnaissable reste masqué, même s'il se termine
        # par un code.
        pytest.param(
            "jeton sl.expired_access_token",
            f"jeton {VALEUR_MASQUEE}",
            id="jeton-dropbox-forme-de-code",
        ),
        # Passe 2 : l'en-tête d'autorisation reste masqué.
        pytest.param(
            "Bearer too_many_write_operations",
            f"Bearer {VALEUR_MASQUEE}",
            id="porteur-egal-a-un-code",
        ),
    ],
)
def test_les_passes_precedentes_l_emportent_sur_la_liste_blanche(
    brut: str, attendu: str
) -> None:
    """La liste blanche n'agit qu'en passe 6 : elle ne rouvre aucune fuite."""
    assert masquer(brut) == attendu


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


### Exceptions et journal (#35) ###

URL_DE_SESSION = (
    "https://www.googleapis.invalid/upload/drive/v3/files"
    f"?uploadType=resumable&upload_id={IDENTIFIANT_DE_SESSION}"
)
JOURNAL_DE_TEST = "custom_components.auto_backup.destinations.test_masquage"


def _leve(erreur: BaseException) -> BaseException:
    """Lève puis rattrape l'erreur, pour qu'elle porte une vraie trace de pile."""
    try:
        raise erreur
    except BaseException as rattrapee:
        return rattrapee


def _chaine_avec_secrets() -> BaseException:
    """Erreur réseau portant l'URI de session, cause d'une erreur de plus haut."""
    try:
        try:
            raise ConnectionError(f"connexion perdue vers {URL_DE_SESSION}")
        except ConnectionError as cause:
            raise RuntimeError(f"envoi interrompu, jeton {JETON_GOOGLE}") from cause
    except RuntimeError as erreur:
        return erreur


def test_la_description_d_une_exception_garde_le_type_et_masque_le_message() -> None:
    """Le type reste lisible, le message passe par `masquer()`."""
    description = decrire_l_exception(
        ConnectionError(f"connexion perdue vers {URL_DE_SESSION}")
    )

    assert description.startswith("ConnectionError: connexion perdue vers ")
    assert IDENTIFIANT_DE_SESSION not in description


def test_une_exception_sans_message_se_reduit_a_son_type() -> None:
    """`TimeoutError()` n'a pas de message : son type suffit."""
    assert decrire_l_exception(TimeoutError()) == "TimeoutError"


def test_un_message_illisible_ne_fait_pas_echouer_la_description() -> None:
    """Un `__str__` défaillant ne doit pas faire échouer la journalisation."""

    class ErreurIllisible(Exception):
        def __str__(self) -> str:
            raise ValueError("illisible")

    assert decrire_l_exception(ErreurIllisible()) == (
        "ErreurIllisible: <message illisible>"
    )


def test_la_trace_masquee_ne_contient_aucun_secret_de_la_chaine() -> None:
    """Cause et conséquence sont masquées, les cadres restent intacts."""
    erreur = _chaine_avec_secrets()
    erreur.add_note(f"note du fournisseur : Bearer {JETON_DROPBOX}")

    trace = trace_masquee(erreur)

    for secret in (IDENTIFIANT_DE_SESSION, JETON_GOOGLE, JETON_DROPBOX):
        assert secret not in trace
    # Le diagnostic reste possible : types, lien de causalité, fonction et fichier.
    assert trace.startswith("Traceback (most recent call last):")
    assert "ConnectionError: connexion perdue vers" in trace
    assert "direct cause of the following exception" in trace
    assert "RuntimeError: envoi interrompu" in trace
    assert "_chaine_avec_secrets" in trace
    assert "test_masquage.py" in trace
    assert "note du fournisseur : Bearer ***" in trace


def _contexte_implicite(*, supprime: bool) -> BaseException:
    """Erreur levée pendant la gestion d'une autre, lien gardé ou supprimé."""
    try:
        try:
            raise KeyError(f"refresh_token={RAFRAICHISSEMENT_GOOGLE}")
        except KeyError:
            if supprime:
                raise ValueError("second échec") from None
            raise ValueError("second échec")  # noqa: B904 — contexte voulu
    except ValueError as erreur:
        return erreur


def test_la_trace_masquee_suit_aussi_le_contexte_implicite() -> None:
    """Une erreur levée pendant la gestion d'une autre garde ce lien, masqué."""
    trace = trace_masquee(_contexte_implicite(supprime=False))

    assert "During handling of the above exception" in trace
    assert "KeyError" in trace
    assert "ValueError: second échec" in trace
    assert RAFRAICHISSEMENT_GOOGLE not in trace


def test_un_contexte_supprime_n_apparait_pas_dans_la_trace() -> None:
    """`raise … from None` masque le contexte, comme la trace standard."""
    trace = trace_masquee(_contexte_implicite(supprime=True))

    assert "KeyError" not in trace
    assert "ValueError: second échec" in trace


def test_une_exception_jamais_levee_se_reduit_a_sa_description() -> None:
    """Sans trace de pile, seule la description masquée subsiste."""
    erreur = RuntimeError(f"jeton {JETON_DROPBOX}")

    assert trace_masquee(erreur) == decrire_l_exception(erreur)


def test_une_chaine_circulaire_ne_boucle_pas() -> None:
    """Une cause qui se désigne elle-même ne fait pas tourner la trace sans fin."""
    erreur = _leve(RuntimeError("boucle"))
    erreur.__cause__ = erreur

    assert trace_masquee(erreur).count("RuntimeError: boucle") == 1


def test_journaliser_une_exception_masque_le_message_au_niveau_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """En `error` : le message du fork, puis le type et le message masqués."""
    journal = logging.getLogger(JOURNAL_DE_TEST)

    with caplog.at_level(logging.INFO, logger=JOURNAL_DE_TEST):
        journaliser_une_exception(
            journal, _chaine_avec_secrets(), "Échec vers « %s »", "Mon Drive"
        )

    (enregistrement,) = caplog.records
    assert enregistrement.levelno == logging.ERROR
    assert enregistrement.exc_info is None
    assert enregistrement.getMessage().startswith(
        "Échec vers « Mon Drive » : RuntimeError: envoi interrompu"
    )
    assert JETON_GOOGLE not in caplog.text


def test_journaliser_une_exception_ne_donne_la_trace_qu_en_debug(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """En `debug` : la trace suit, masquée, sans aucun secret de la chaîne."""
    journal = logging.getLogger(JOURNAL_DE_TEST)

    with caplog.at_level(logging.DEBUG, logger=JOURNAL_DE_TEST):
        journaliser_une_exception(journal, _chaine_avec_secrets(), "Échec")

    niveaux = [enregistrement.levelno for enregistrement in caplog.records]
    assert niveaux == [logging.ERROR, logging.DEBUG]
    assert "Traceback (most recent call last):" in caplog.text
    assert "ConnectionError" in caplog.text
    for secret in (IDENTIFIANT_DE_SESSION, JETON_GOOGLE):
        assert secret not in caplog.text
    assert all(enregistrement.exc_info is None for enregistrement in caplog.records)


### Idempotence (#44) ###

# L'événement `auto_backup.upload_failed` porte désormais une cause déjà masquée
# (#44), que les notifications (#17) et les entités (#16) masquent de nouveau à
# la lecture. Leur affichage ne reste inchangé que si masquer un texte déjà
# masqué ne le modifie plus. Placée en fin de module, cette section rassemble
# **toutes** les entrées des vecteurs paramétrés ci-dessus : un vecteur ajouté
# plus tard est éprouvé ici sans qu'on ait à le recopier.


def _entrees_des_vecteurs() -> list[str]:
    """Premier argument texte de chaque cas paramétré de ce module."""
    entrees: dict[str, None] = {}
    for objet in list(globals().values()):
        for marque in getattr(objet, "pytestmark", ()):
            if marque.name != "parametrize":
                continue
            for cas in marque.args[1]:
                valeurs = getattr(cas, "values", cas)
                premier = valeurs[0] if isinstance(valeurs, tuple) else valeurs
                if isinstance(premier, str):
                    entrees[premier] = None
    return list(entrees)


ENTREES_DES_VECTEURS = _entrees_des_vecteurs()


def test_les_vecteurs_sont_bien_rassembles() -> None:
    """Garde-fou : une collecte vide rendrait le test suivant trivialement vert."""
    assert len(ENTREES_DES_VECTEURS) >= 40
    assert f"invalid access token {JETON_DROPBOX}" in ENTREES_DES_VECTEURS
    # Réserve de #54 : une entrée non ASCII est éprouvée elle aussi.
    assert any(HOMOGLYPHE_CYRILLIQUE in entree for entree in ENTREES_DES_VECTEURS)


@pytest.mark.parametrize("brut", ENTREES_DES_VECTEURS)
def test_masquer_un_texte_deja_masque_ne_le_change_plus(brut: str) -> None:
    """`masquer(masquer(x)) == masquer(x)`, avec ou sans troncature.

    La composition éprouvée est aussi celle des entités : l'événement porte
    `masquer(x)`, l'attribut d'entité applique `masquer(…, longueur_max=255)`.
    """
    masque = masquer(brut)

    assert masquer(masque) == masque
    for borne in (255, 32):
        assert masquer(masque, longueur_max=borne) == masquer(brut, longueur_max=borne)
    assert masquer_un_nom(masquer_un_nom(brut)) == masquer_un_nom(brut)


### Documentation du masquage à l'émission de l'événement (critère 5 de #44) ###

RACINE_DEPOT = Path(__file__).resolve().parent.parent
README = RACINE_DEPOT / "README.md"
CHANGELOG = RACINE_DEPOT / "CHANGELOG.md"
DOC_UPSTREAM = RACINE_DEPOT / "docs" / "UPSTREAM.md"


def test_le_readme_documente_le_masquage_du_champ_error() -> None:
    """Critère 5 : la section des événements dit que `error` est masqué.

    Le README décrit le schéma de `auto_backup.upload_failed` juste avant :
    c'est là, et non ailleurs dans le document, que la précision sur le
    masquage doit se trouver pour qu'un lecteur qui découvre l'événement la
    voie immédiatement.
    """
    texte = README.read_text(encoding="utf-8")

    marqueur = "**Événements**"
    assert marqueur in texte, f"section des événements absente du README : {marqueur!r}"

    section = texte.split(marqueur, 1)[1]
    assert "auto_backup.upload_failed" in section
    assert "masqu" in section.casefold()
    # L'exemple qui rassure sur la stabilité d'une cause sans secret, cité mot
    # pour mot, pour qu'une automatisation sache qu'elle continue de fonctionner.
    assert "quota" in section.casefold()


def test_le_changelog_signale_le_changement_visible_pour_les_automatisations() -> None:
    """Critère 5 : le CHANGELOG signale, pour #44, le texte désormais masqué.

    Le critère demande explicitement que « le changement visible pour une
    automatisation qui comparerait le texte exact d'une erreur contenant un
    secret » soit signalé au CHANGELOG — pas seulement que `error` est masqué.
    """
    texte = CHANGELOG.read_text(encoding="utf-8")

    entrees_44 = [ligne for ligne in texte.splitlines() if "Refs #44" in ligne]
    assert entrees_44, "aucune entrée du CHANGELOG ne référence #44"

    entree_automatisations = next(
        (ligne for ligne in entrees_44 if "automatisation" in ligne.casefold()), None
    )
    assert entree_automatisations is not None, (
        "aucune entrée #44 ne signale le changement visible pour les automatisations"
    )
    assert "upload_failed" in entree_automatisations
    assert "***" in entree_automatisations


def test_upstream_ne_decrit_plus_une_cause_transportee_brute() -> None:
    """Critère 5 : `docs/UPSTREAM.md` reflète le masquage à l'émission.

    Avant #44, ce document disait explicitement que l'événement « transporte
    la cause brute » : cette affirmation, devenue fausse, ne doit plus s'y
    trouver.
    """
    texte = DOC_UPSTREAM.read_text(encoding="utf-8")

    assert "auto_backup.upload_failed" in texte
    assert "transporte la cause brute" not in texte
    assert "#44" in texte
    section = texte.split("auto_backup.upload_failed", 1)[1]
    assert "masqu" in section.casefold()


### ADR : le champ structuré `error_code` (critère 6 de #48) ###

ADR_0001 = RACINE_DEPOT / "docs" / "adr" / "0001-destinations-distantes.md"


def test_l_adr_tranche_l_ajout_d_un_champ_error_code() -> None:
    """Critère 6 : l'ADR tranche l'ajout éventuel d'un champ `error_code`.

    Un tel champ serait un ajout au contrat public de `auto_backup.upload_failed`
    sans rupture (`error` resterait présent et inchangé) : l'issue #48 exige que
    l'ADR le dise explicitement, faute de quoi une resynchronisation future
    pourrait rouvrir le débat sans connaître les raisons déjà pesées. Il ne
    suffit pas que « error_code » apparaisse quelque part dans le document :
    la décision doit être prise, et justifiée, à son sujet.
    """
    texte = ADR_0001.read_text(encoding="utf-8")

    assert "error_code" in texte, "l'ADR ne mentionne pas le champ `error_code`"
    section = texte.split("error_code", 1)[1]

    # La décision est prise : ce n'est pas retenu maintenant, et c'est reporté
    # à une issue précise plutôt que laissé en suspens.
    assert "#46" in texte
    assert "reporté" in texte.casefold() or "report" in texte.casefold()

    # Elle est justifiée comme un ajout au contrat public, exactement dans les
    # termes du critère d'acceptation de #48.
    assert "contrat public" in texte
    assert "sans rupture" in texte

    # Et la liste blanche de #48 est présentée comme la réponse en attendant.
    assert "CODES_D_ERREUR_CONNUS" in texte
    assert "#48" in section or "#48" in texte


### Documentation de l'angle mort Unicode (critère 1 de #54) ###

MASQUAGE_PY = (
    RACINE_DEPOT / "custom_components" / "auto_backup" / "destinations" / "masquage.py"
)


def _fenetre_autour_de(
    texte: str, marqueur: str, avant: int = 400, apres: int = 1400
) -> str:
    """Fenêtre de texte centrée sur `marqueur`, espaces normalisés.

    Les deux documents ne placent pas `#54` au même endroit de leur
    paragraphe (avant ou après la référence à l'alphabet ASCII, selon le
    document) : une fenêtre plutôt qu'un simple découpage en deux évite de
    dépendre de cet ordre. Les espaces sont normalisés pour qu'un retour à la
    ligne au milieu d'une phrase ne casse pas une recherche de sous-chaîne.
    """
    aplati = " ".join(texte.split())
    index = aplati.find(marqueur)
    assert index != -1, f"{marqueur!r} introuvable"
    return aplati[max(0, index - avant) : index + apres]


def test_la_docstring_et_l_adr_documentent_l_angle_mort_non_ascii() -> None:
    """Critère 1 de #54 : la docstring de `masquage.py` et l'ADR décrivent l'angle mort.

    Il ne suffit pas que « #54 » ou « ASCII » apparaisse quelque part dans
    chaque texte : la même référence doit porter, dans le même paragraphe,
    l'angle mort (un caractère non ASCII coupe une suite opaque), sa cause
    (la passe 6 ne reconnaît qu'un alphabet ASCII) et la raison de son
    acceptation (les jetons des fournisseurs intégrés sont en ASCII et
    repérés en passe 4, et aucun secret n'est fabriqué par un tiers) —
    exactement les termes du critère d'acceptation de #54.
    """
    docstring = MASQUAGE_PY.read_text(encoding="utf-8")
    adr = ADR_0001.read_text(encoding="utf-8")

    for texte, origine in ((docstring, "la docstring de masquage.py"), (adr, "l'ADR")):
        section = _fenetre_autour_de(texte, "#54")

        # L'angle mort et sa cause : la passe 6 ne reconnaît qu'un alphabet
        # ASCII, qu'un caractère non ASCII vient couper.
        assert "non ASCII" in section, f"{origine} ne décrit pas l'angle mort"
        assert "[A-Za-z0-9_+-]" in section, (
            f"{origine} ne cite pas l'alphabet reconnu par la passe 6 (la cause)"
        )

        # La justification de l'acceptation : jetons ASCII des fournisseurs
        # intégrés, reconnus en passe 4, et aucun secret fabriqué par un tiers.
        assert "passe 4" in section, (
            f"{origine} ne justifie pas l'acceptation par la reconnaissance en passe 4"
        )
        assert "aucun secret n'est fabriqué par un tiers" in section, (
            f"{origine} ne justifie pas l'acceptation par l'absence de secret fabriqué "
            "par un tiers"
        )

        # La condition de réouverture, cohérente avec le hors périmètre de
        # l'issue : le motif n'est pas élargi tant qu'aucun fournisseur
        # n'émet de jetons hors ASCII.
        assert "fournisseur dont les jetons sortent de l'ASCII" in section, (
            f"{origine} ne pose pas la condition de réouverture"
        )
