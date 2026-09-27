"""Documentation de la modification d'une destination existante (issue #51).

Critère 7 : « GIVEN la documentation WHEN je la lis THEN les guides et la FAQ
décrivent cette modification au lieu de « supprimer puis rajouter ». »

Ces tests lisent les mêmes pages qu'un utilisateur cherchant à changer le nom,
le dossier distant ou la rétention d'une destination déjà configurée :
`docs/faq.md`, les deux guides de fournisseur et `docs/services.md`. Ils
vérifient que chacune renvoie désormais vers **Modifier une destination** — et
non plus vers « supprimer puis rajouter » — pour ces trois réglages, et que
l'avertissement sur le sort des sauvegardes de l'ancien dossier y figure.

La marche « supprimer puis ajouter de nouveau » reste légitimement documentée
ailleurs dans la FAQ : elle vaut encore quand les **identifiants de
l'application** ont changé (secret régénéré, application recréée) ou que le
**compte** ou le **fournisseur** changent — des cas hors du périmètre de cette
issue (la ré-autorisation, #17). Ces tests ne l'interdisent donc pas partout :
ils vérifient seulement qu'elle n'est plus la réponse donnée, dans la section
qui répond au changement de nom, de dossier ou de rétention, ni dans les
sections « Modifier la destination » des deux guides.
"""

from __future__ import annotations

from pathlib import Path

RACINE_DEPOT = Path(__file__).resolve().parent.parent
DOC_FAQ = RACINE_DEPOT / "docs" / "faq.md"
DOC_SERVICES = RACINE_DEPOT / "docs" / "services.md"
DOC_DROPBOX = RACINE_DEPOT / "docs" / "destinations" / "dropbox.md"
DOC_GOOGLE_DRIVE = RACINE_DEPOT / "docs" / "destinations" / "google-drive.md"
DOC_ADR = RACINE_DEPOT / "docs" / "adr" / "0001-destinations-distantes.md"


def _lire(chemin: Path) -> str:
    assert chemin.is_file(), f"page introuvable : {chemin}"
    return chemin.read_text(encoding="utf-8")


def _aplati(chemin: Path) -> str:
    """Texte sans retours à la ligne, pour une phrase répartie sur plusieurs lignes."""
    return " ".join(_lire(chemin).split())


def _section(texte: str, debut: str, fin: str) -> str:
    """Extrait le texte compris entre deux titres, celui de fin exclu."""
    indice_debut = texte.index(debut)
    indice_fin = texte.index(fin, indice_debut)
    return texte[indice_debut:indice_fin]


### FAQ : « Modifier une destination » ###


def test_la_faq_a_une_section_dediee_a_la_modification() -> None:
    """La FAQ répond, dans sa propre section, à ce que critère 7 demande."""
    texte = _lire(DOC_FAQ)

    assert "## Modifier une destination" in texte
    assert (
        "Comment changer le nom, le dossier ou la rétention d'une destination" in texte
    )


def test_la_faq_renvoie_vers_modifier_une_destination_sans_supprimer() -> None:
    """La marche à suivre passe par **Modifier une destination**, sans suppression."""
    aplati = _aplati(DOC_FAQ)

    assert "Configurer → Modifier une destination" in aplati
    assert "aucune nouvelle autorisation" in aplati
    assert "le jeton et les identifiants d'application sont conservés" in aplati


def test_la_faq_avertit_du_sort_des_sauvegardes_de_l_ancien_dossier() -> None:
    """Un changement de dossier laisse les anciennes sauvegardes hors de portée."""
    texte = _lire(DOC_FAQ)
    aplati = _aplati(DOC_FAQ)

    assert "Que deviennent les sauvegardes si je change de dossier distant" in texte
    assert "plus ni listées, ni purgées" in aplati
    assert "confirmation explicite" in aplati


def test_la_faq_decrit_le_capteur_apres_un_changement_de_dossier() -> None:
    """#58 : le capteur ne compte plus l'ancien dossier, un retour le recompte."""
    aplati = _aplati(DOC_FAQ)

    assert "ne compte que les sauvegardes du dossier configuré" in aplati
    assert "il repasse à 0 dès l'enregistrement" in aplati
    assert "si vous y revenez, elles sont de nouveau comptées" in aplati
    assert "y restent comptées" not in aplati


def test_l_adr_ne_dit_plus_que_le_capteur_compte_l_ancien_dossier() -> None:
    """#58 : la conséquence assumée de #51 est levée dans l'ADR."""
    aplati = _aplati(DOC_ADR)

    assert "continue de compter les entrées de l'ancien dossier" not in aplati
    assert "Registre rangé par dossier (issue #58)" in _lire(DOC_ADR)


def test_la_section_faq_de_modification_ne_dit_plus_de_supprimer() -> None:
    """La section « Modifier une destination » ne renvoie plus à une suppression.

    « Supprimer la destination » (secret régénéré, changement de compte ou de
    fournisseur) reste documenté ailleurs dans cette même FAQ, un peu plus
    haut : c'est un cas hors périmètre de #51 (cf. #17), que ce test ne touche
    pas. Seule la section qui répond au nom, au dossier et à la rétention ne
    doit plus s'y résoudre.
    """
    section = _section(
        _lire(DOC_FAQ),
        "## Modifier une destination",
        "## Plusieurs instances Home Assistant",
    ).casefold()

    assert "supprim" not in section


### Guide Dropbox : « Modifier la destination » ###


def test_le_guide_dropbox_a_une_section_modifier_la_destination() -> None:
    """Le guide Dropbox décrit la modification, sans repasser par Dropbox."""
    texte = _lire(DOC_DROPBOX)
    aplati = _aplati(DOC_DROPBOX)

    assert "## Modifier la destination" in texte
    assert "Modifier une destination" in aplati
    assert "sans repasser par Dropbox" in aplati
    assert "la clé, le secret et le jeton sont conservés" in aplati


def test_la_section_dropbox_de_modification_ne_dit_pas_de_supprimer() -> None:
    """Elle ne renvoie à une suppression que pour la clé ou le secret d'application.

    Ce cas-là (identifiants d'application changés) reste hors du périmètre de
    #51 ; il est traité ailleurs (« Ce qui se passe ensuite »), jamais dans
    cette section.
    """
    section = _section(
        _lire(DOC_DROPBOX), "## Modifier la destination", "## Limites connues"
    ).casefold()

    assert "supprim" not in section


### Guide Google Drive : « Modifier la destination » ###


def test_le_guide_google_drive_a_une_section_modifier_la_destination() -> None:
    """Le guide Google Drive décrit la modification, sans nouvelle autorisation."""
    texte = _lire(DOC_GOOGLE_DRIVE)
    aplati = _aplati(DOC_GOOGLE_DRIVE)

    assert "## Modifier la destination" in texte
    assert "Modifier une destination" in aplati
    assert "sans nouvelle autorisation" in aplati
    assert "les identifiants et le jeton sont conservés" in aplati


def test_la_section_google_drive_de_modification_invalide_le_dossier_memorise() -> None:
    """Elle annonce que l'ancien identifiant de dossier mémorisé est oublié.

    C'est ce que `folder_id` (`CLES_LIEES_AU_DOSSIER`, #51) fait réellement :
    la doc doit le dire, faute de quoi l'utilisateur pourrait croire qu'un
    changement de dossier ne fait rien de plus qu'un renommage.
    """
    aplati = _aplati(DOC_GOOGLE_DRIVE)

    assert "oublie alors l'identifiant" in aplati
    assert "cherche" in aplati and "crée" in aplati


def test_la_section_google_drive_de_modification_ne_dit_pas_de_supprimer() -> None:
    """Elle ne renvoie à une suppression que pour les identifiants d'application.

    Ce cas-là (application recréée dans un autre projet, secret régénéré)
    reste hors du périmètre de #51 ; il est traité ailleurs (« Durée de vie
    de l'autorisation »), jamais dans cette section.
    """
    section = _section(
        _lire(DOC_GOOGLE_DRIVE),
        "## Modifier la destination",
        "## Supprimer la destination",
    ).casefold()

    assert "supprim" not in section


### Guide des services : rétention d'une destination existante ###


def test_le_guide_des_services_renvoie_vers_modifier_une_destination() -> None:
    """Changer la rétention d'une destination déjà créée passe par la modification."""
    aplati = _aplati(DOC_SERVICES)

    assert "Configurer → Modifier une destination" in aplati
    assert "sans nouvelle autorisation" in aplati
