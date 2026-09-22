"""Documentation de la connexion d'un compte Dropbox (issue #10, critère 6).

Critère : « GIVEN `docs/destinations/dropbox.md` WHEN je le lis THEN la création
de l'application Dropbox, les portées à cocher et l'URI de redirection à
renseigner y sont décrites en français. »

Ce test est indépendant de `tests/test_provider_dropbox.py` : il ne réutilise
aucune de ses assertions et relit le fichier de documentation lui-même, pour
vérifier que la page réellement livrée décrit les trois étapes attendues — et
que les portées et l'URI qu'elle cite correspondent bien à celles que le code
utilise, plutôt que de les recopier à la main (ce qui masquerait une dérive
entre la doc et le code).
"""

from __future__ import annotations

import re
from pathlib import Path

from custom_components.auto_backup.const import DOMAIN, OAUTH_CALLBACK_PATH
from custom_components.auto_backup.destinations.providers.dropbox import (
    PORTEES,
    URL_AUTORISATION,
)

RACINE_DEPOT = Path(__file__).resolve().parent.parent
DOC_DROPBOX = RACINE_DEPOT / "docs" / "destinations" / "dropbox.md"

# Quelques marqueurs simples et peu ambigus d'un texte rédigé en français :
# mots grammaticaux qui n'apparaissent normalement pas dans une page en anglais.
MARQUEURS_FRANCAIS = ("vous", "votre", "compte", "application", "autorisation")

# Ligne du tableau des portées : une cellule qui ne contient qu'un nom de portée
# entre accents graves (`files.content.write`). Les autres tableaux de la page ne
# sont pas concernés, leur première cellule n'étant jamais de cette forme.
LIGNE_DE_PORTEE = re.compile(r"^\| `([a-z_]+(?:\.[a-z_]+)+)` \|", re.MULTILINE)


def _texte_de_la_doc() -> str:
    assert DOC_DROPBOX.is_file(), f"documentation Dropbox introuvable : {DOC_DROPBOX}"
    return DOC_DROPBOX.read_text(encoding="utf-8")


def _texte_aplati() -> str:
    """Texte de la doc, retours à la ligne effacés : robuste au habillage Markdown."""
    return " ".join(_texte_de_la_doc().split())


def test_la_doc_dropbox_existe_et_est_ecrite_en_francais() -> None:
    """Le fichier existe et son contenu est bien en français."""
    texte = _texte_de_la_doc().casefold()

    manquants = [mot for mot in MARQUEURS_FRANCAIS if mot not in texte]
    assert not manquants, f"marqueurs de français absents : {manquants}"

    # Des mots-clés d'une doc rédigée en anglais ne doivent pas s'y substituer.
    for mot_anglais in ("you must", "please", "click here"):
        assert mot_anglais not in texte


def test_la_doc_decrit_la_creation_de_l_application_dropbox() -> None:
    """Étape 1 du critère : créer sa propre application Dropbox.

    L'utilisateur doit être renvoyé vers la console développeur Dropbox et
    savoir qu'il doit y créer une application — Auto Backup n'en fournit pas.
    """
    texte = _texte_de_la_doc()

    assert "dropbox.com/developers/apps" in texte
    assert "Create app" in texte
    # Le fait qu'aucun identifiant n'est fourni par le dépôt doit être explicite.
    assert "propre application" in _texte_aplati().casefold()


def test_la_doc_decrit_exactement_les_portees_utilisees_par_le_code() -> None:
    """Étape 2 du critère : les portées à cocher, alignées avec `dropbox.py`.

    Comparer aux `PORTEES` réellement demandées par le fournisseur — plutôt
    qu'à une liste recopiée à la main — détecte une doc qui aurait cessé de
    correspondre au code après une évolution des portées.
    """
    texte = _texte_de_la_doc()

    assert PORTEES, "aucune portée déclarée par le fournisseur Dropbox"
    manquantes = [portee for portee in PORTEES if portee not in texte]
    assert not manquantes, f"portées absentes de la doc : {manquantes}"

    # Section qui introduit la liste des portées à cocher.
    assert "Permissions" in texte
    assert "Ne cochez rien d'autre" in texte


def test_la_doc_ne_fait_cocher_aucune_portee_que_le_code_ne_demande_pas() -> None:
    """Le tableau des portées ne liste **que** celles que le code demande.

    Le test précédent vérifie que rien ne manque ; celui-ci vérifie que rien n'est
    en trop. Une portée retirée du code mais laissée dans la doc ferait accorder à
    l'application un accès dont Auto Backup n'a pas l'usage — exactement ce que le
    principe de moindre privilège cherche à éviter.
    """
    listees = set(LIGNE_DE_PORTEE.findall(_texte_de_la_doc()))

    en_trop = listees - set(PORTEES)
    assert not en_trop, f"portées listées par la doc mais non demandées : {en_trop}"
    assert listees == set(PORTEES)
    assert "files.content.read" not in listees


def test_la_doc_decrit_l_uri_de_redirection_a_declarer() -> None:
    """Étape 3 du critère : l'URI de redirection, alignée avec `OAUTH_CALLBACK_PATH`.

    La doc doit citer le même chemin que celui que la vue de retour du fork
    écoute réellement (`destinations/oauth.py`), faute de quoi suivre la doc
    à la lettre échouerait chez Dropbox (« invalid redirect_uri »).
    """
    texte = _texte_de_la_doc()

    assert f"/auth/{DOMAIN}/callback" == OAUTH_CALLBACK_PATH
    assert OAUTH_CALLBACK_PATH in texte
    assert "Redirect URIs" in texte
    # Le protocole exigé par Dropbox (HTTPS, sauf localhost) est mentionné.
    assert "https://" in texte
    assert "localhost" in texte


def test_la_doc_cite_l_url_d_autorisation_reelle_du_fournisseur() -> None:
    """La page d'autorisation nommée dans la doc est celle que le code appelle."""
    texte = _texte_de_la_doc()

    domaine_autorisation = URL_AUTORISATION.removeprefix("https://").split("/")[0]
    assert domaine_autorisation in texte
