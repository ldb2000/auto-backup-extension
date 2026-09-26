"""Cas limites du dépôt d'une sauvegarde chez Dropbox (issue #11).

Complète `tests/test_provider_dropbox_upload.py` avec trois situations que les
critères d'acceptation évoquent mais qu'aucun test existant n'éprouvait
directement :

- une taille annoncée par l'appelant qui ne correspond pas au flux réel, dans
  les deux sens (plus petite, puis plus grande) ;
- deux téléversements successifs vers la même destination, pour s'assurer
  qu'aucun état (session, offset, cache) ne survit d'un dépôt à l'autre ;
- un nom de sauvegarde contenant `/` ou `..`, pour s'assurer que le chemin
  distant reste confiné au dossier configuré.

Les fixtures et utilitaires sont réutilisés depuis `test_provider_dropbox_upload`
plutôt que dupliqués : même configuration factice, même simulateur HTTP.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.auto_backup.destinations import DestinationError
from custom_components.auto_backup.destinations.providers.dropbox import (
    URL_CREATION_DOSSIER,
    URL_ENVOI,
    URL_SESSION_AJOUT,
    URL_SESSION_DEBUT,
    URL_SESSION_FIN,
    DropboxDestination,
)
from test_provider_dropbox_upload import (  # noqa: F401 - fixtures réutilisées
    CONTENU,
    DOSSIER_DISTANT,
    MODULE_DROPBOX,
    appels,
    argument,
    destination,
    entree_dropbox,
    instance_joignable,
    metadonnees_de_fichier,
    reponse,
    servir,
    servir_en_consommant,
    simuler_le_dossier,
    televerser,
)

### Taille annoncée mensongère ###


async def test_une_taille_annoncee_trop_petite_est_detectee(
    destination: DropboxDestination,  # noqa: F811 - fixture importée, requise sous ce nom
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Une taille annoncée plus petite que le flux réel ne passe pas inaperçue.

    L'appelant (l'orchestrateur de l'issue #8) prétend ici que la sauvegarde ne
    fait que 10 octets, alors que `CONTENU` en fait bien davantage. Dropbox, lui,
    rapporte fidèlement le nombre d'octets qu'il a réellement reçus : l'écart
    doit être détecté par `_verifier_la_taille`, pas dissimulé derrière la
    taille (fausse) annoncée par l'appelant.
    """
    simuler_le_dossier(aioclient_mock)
    recu: list[bytes] = []
    aioclient_mock.post(
        URL_ENVOI,
        side_effect=servir_en_consommant(
            reponse(URL_ENVOI, charge=metadonnees_de_fichier(size=len(CONTENU))),
            recu,
        ),
    )

    with pytest.raises(DestinationError, match="incomplet"):
        await televerser(destination, taille=10)

    # Le flux réel a bien été transmis intégralement, malgré l'annonce erronée :
    # ce n'est donc pas une troncature réseau qui explique l'écart.
    assert recu == [CONTENU]


async def test_une_taille_annoncee_trop_grande_est_detectee(
    destination: DropboxDestination,  # noqa: F811 - fixture importée, requise sous ce nom
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Une taille annoncée plus grande que le flux réel ne passe pas non plus.

    Symétrique du test précédent : l'appelant prétend une taille dix fois
    supérieure au contenu réel (tout en restant sous le seuil de la session).
    """
    simuler_le_dossier(aioclient_mock)
    recu: list[bytes] = []
    aioclient_mock.post(
        URL_ENVOI,
        side_effect=servir_en_consommant(
            reponse(URL_ENVOI, charge=metadonnees_de_fichier(size=len(CONTENU))),
            recu,
        ),
    )

    with pytest.raises(DestinationError, match="incomplet"):
        await televerser(destination, taille=len(CONTENU) * 10)

    assert recu == [CONTENU]


### Deux téléversements successifs ###


async def test_deux_televersements_successifs_ne_laissent_aucun_etat_residuel(
    destination: DropboxDestination,  # noqa: F811 - fixture importée, requise sous ce nom
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Deux dépôts vers la même destination sont indépendants l'un de l'autre.

    Chaque appel à `async_upload()` doit ouvrir sa **propre** session Dropbox
    (avec son propre `session_id` et ses propres offsets), et créer le dossier
    cible à nouveau (un dossier déjà présent restant toléré) : rien de l'état
    du premier dépôt ne doit fuiter vers le second, alors que l'instance de
    destination est réutilisée telle quelle par l'orchestrateur.
    """
    simuler_le_dossier(aioclient_mock)
    fragment = 8192
    contenu_1 = b"a" * (fragment * 2 + 10)
    contenu_2 = b"b" * (fragment * 3 + 5)

    aioclient_mock.post(
        URL_SESSION_DEBUT,
        side_effect=servir(
            reponse(URL_SESSION_DEBUT, charge={"session_id": "session-1"}),
            reponse(URL_SESSION_DEBUT, charge={"session_id": "session-2"}),
        ),
    )
    aioclient_mock.post(URL_SESSION_AJOUT, text="")
    aioclient_mock.post(
        URL_SESSION_FIN,
        side_effect=servir(
            reponse(
                URL_SESSION_FIN, charge=metadonnees_de_fichier(size=len(contenu_1))
            ),
            reponse(
                URL_SESSION_FIN, charge=metadonnees_de_fichier(size=len(contenu_2))
            ),
        ),
    )

    with (
        patch(f"{MODULE_DROPBOX}.SEUIL_ENVOI_SIMPLE", fragment),
        patch(f"{MODULE_DROPBOX}.TAILLE_FRAGMENT", fragment),
    ):
        premiere = await televerser(destination, contenu=contenu_1, slug="slug-1")
        seconde = await televerser(destination, contenu=contenu_2, slug="slug-2")

    assert premiere.size == len(contenu_1)
    assert seconde.size == len(contenu_2)

    fins = appels(aioclient_mock, URL_SESSION_FIN)
    assert len(fins) == 2
    # Chaque dépôt valide son propre curseur : le second ne reprend jamais
    # l'identifiant de session ni l'offset du premier.
    assert argument(fins[0])["cursor"] == {
        "session_id": "session-1",
        "offset": len(contenu_1),
    }
    assert argument(fins[1])["cursor"] == {
        "session_id": "session-2",
        "offset": len(contenu_2),
    }

    # Le dossier cible est (re)vérifié à chaque dépôt, jamais mémorisé.
    assert len(appels(aioclient_mock, URL_CREATION_DOSSIER)) == 2


### Nom de sauvegarde hostile ###


@pytest.mark.parametrize(
    ("nom", "slug"),
    [
        ("../../etc/passwd", "a1b2"),
        ("sauvegarde", "a/b/../c"),
        ("../../../etc/shadow", "../../root"),
    ],
)
async def test_un_nom_ou_un_slug_avec_slash_ou_points_reste_dans_le_dossier(
    destination: DropboxDestination,  # noqa: F811 - fixture importée, requise sous ce nom
    aioclient_mock: AiohttpClientMocker,
    nom: str,
    slug: str,
) -> None:
    """Un nom ou un slug hostile ne peut jamais faire sortir le dépôt du dossier.

    Critère : « le dossier est créé si besoin et aucun fichier existant n'est
    écrasé silencieusement » suppose que le chemin déposé reste bien celui du
    dossier configuré — un nom ou un slug contenant `/` ou `..` ne doit donc
    jamais introduire de segment de chemin supplémentaire.
    """
    simuler_le_dossier(aioclient_mock)
    aioclient_mock.post(URL_ENVOI, json=metadonnees_de_fichier())

    await televerser(destination, nom=nom, slug=slug)

    chemin_envoye = argument(appels(aioclient_mock, URL_ENVOI)[0])["path"]

    assert chemin_envoye.startswith(f"{DOSSIER_DISTANT}/")
    # Un seul segment après le dossier configuré : pas de sous-dossier introduit.
    reste = chemin_envoye[len(DOSSIER_DISTANT) + 1 :]
    assert "/" not in reste
    assert chemin_envoye.count(DOSSIER_DISTANT) == 1
