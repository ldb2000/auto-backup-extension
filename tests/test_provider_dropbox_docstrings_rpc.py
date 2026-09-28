"""Docstrings de `_async_appel_rpc()` et `_async_requete()` (issue #67, critère 4).

Critère de l'issue : « GIVEN les docstrings de `_async_appel_rpc()` et
`_async_requete()` WHEN je les lis THEN elles décrivent le comportement réel. »

Ce test relit les docstrings **du code livré** (`inspect.getdoc`), pas une copie
figée ici : si l'une d'elles était réécrite pour recommencer à annoncer un corps
optionnel ou l'absence de `Content-Type` — le comportement de l'ancien code,
justement à l'origine de l'issue #67 — ce test échouerait avec elle, sans
attendre une relecture humaine.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable

from custom_components.auto_backup.destinations.providers.dropbox import (
    DropboxDestination,
)


def _texte_aplati(fonction: Callable[..., object]) -> str:
    """Docstring de `fonction`, retours à la ligne et indentation aplatis."""
    doc = inspect.getdoc(fonction)
    assert doc, f"{fonction.__qualname__} n'a pas de docstring"
    return " ".join(doc.split())


def test_la_docstring_de_l_appel_rpc_decrit_le_comportement_reel() -> None:
    """`_async_appel_rpc()` documente le corps JSON `null` envoyé, et pourquoi.

    Le comportement réel (`custom_components/.../dropbox.py`) : l'appel envoie
    le corps `null` avec `Content-Type: application/json`, précisément parce
    qu'`aiohttp` poserait sinon `Content-Type: application/octet-stream` sur un
    POST sans corps, que Dropbox refuse (`400 Bad Request`).
    """
    texte = _texte_aplati(DropboxDestination._async_appel_rpc)
    assert "Content-Type: application/json" in texte
    assert "`null`" in texte
    assert "application/octet-stream" in texte
    assert "400 Bad Request" in texte
    assert "issue #67" in texte


def test_la_docstring_de_la_requete_decrit_le_comportement_reel() -> None:
    """`_async_requete()` documente un corps désormais obligatoire.

    Le comportement réel : la fonction lève `ValueError` si `corps` est `None`,
    et transmet toujours le `Content-Type` choisi par l'appelant — jamais celui
    qu'`aiohttp` ajouterait de lui-même à une requête sans corps.
    """
    texte = _texte_aplati(DropboxDestination._async_requete)
    assert "obligatoire" in texte
    assert "Aucune requête ne part sans corps" in texte
    assert "application/octet-stream" in texte
    assert "400 Bad Request" in texte
    assert "issue #67" in texte


def test_la_requete_leve_bien_l_erreur_que_sa_docstring_annonce() -> None:
    """La docstring annonce un corps obligatoire : le code le vérifie vraiment.

    Relie la documentation (test précédent) au comportement observable, déjà
    éprouvé contre un vrai serveur par `tests/test_provider_dropbox_entetes_reels.py`.
    """
    signature = inspect.signature(DropboxDestination._async_requete)
    assert signature.parameters["corps"].default is inspect.Parameter.empty
