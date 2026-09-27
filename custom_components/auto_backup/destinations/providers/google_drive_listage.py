"""Listage et suppression des sauvegardes Google Drive (issue #15).

Ce module donne à la rétention distante (#9) les deux opérations qui lui
manquaient pour agir réellement sur un Drive :

1. **le listage** : `files.list` filtré sur le dossier dédié, sur `trashed =
   false` et sur la propriété privée `auto_backup`, paginé jusqu'à épuisement des
   `nextPageToken` ;
2. **la suppression** : `files.delete`, idempotente — un fichier déjà absent est
   signalé par `DestinationNotFoundError`, que la purge traite comme « déjà
   purgé ».

## Rien n'est cherché par un second chemin

Toutes les primitives viennent de `google_drive_upload.py`, la couche
`drive/v3/files` du fournisseur : point d'accès (`URL_FICHIERS`), projections
(`CHAMPS_FICHIER`), appel avec nouvelles tentatives (`async_appel_drive_json()`),
échappement d'une requête (`echapper()`), dossier cible (`async_dossier_cible()`)
et construction d'une sauvegarde distante (`sauvegarde_distante_du_fichier()`).
Le listage voit donc **exactement** le dossier où le téléversement dépose, et lit
les fichiers comme lui.

## Ce que le listage ne fait jamais

- **Il n'apparie jamais sur le nom du fichier.** Le nom est borné à 120
  caractères par le fork *et* les propriétés privées sont bornées à 124 octets
  par Google : `appProperties.name` peut donc être tronqué, et deux sauvegardes
  peuvent porter le même nom tronqué. Le nom ne sert qu'à l'affichage ; la
  provenance vient du marqueur, et le lien avec la sauvegarde locale du `slug`.
- **Il ne remonte jamais un dossier.** Les dossiers créés par l'intégration
  portent le même marqueur que les fichiers : sans exclusion explicite du type
  MIME, une destination réglée sur `Sauvegardes` listerait le dossier
  `Sauvegardes/Home Assistant` d'une autre destination comme une sauvegarde — et
  la purge pourrait le supprimer avec tout son contenu.
- **Il ne fait pas confiance au seul filtre envoyé à Google.** Marqueur, corbeille
  et type MIME sont revérifiés sur chaque fichier reçu : une requête `q` mal
  composée ne doit pas pouvoir rendre purgeable un fichier étranger.
- **Il ne compte jamais deux fois la même sauvegarde.** La pagination de Drive
  n'est pas un instantané : un dépôt concurrent ou un réordonnancement peut faire
  apparaître un fichier sur deux pages. Le compter deux fois ferait croire à la
  rétention en nombre qu'il y a une sauvegarde de trop, et lui ferait supprimer
  une sauvegarde qui devait rester.

## Bornes

Le contrat de `RemoteDestination` demande au fournisseur de borner lui-même ses
appels, le coordinateur de purge ne posant qu'un filet de sécurité grossier
(`DEFAULT_PURGE_TIMEOUT`). Trois bornes s'appliquent ici : le délai d'une requête
(`DELAI_REQUETE`, 30 s), le nombre de tentatives (`TENTATIVES_MAX`, 3) et le
nombre de pages parcourues (`PAGES_MAX`), qui plafonne le listage même si Google
renvoie indéfiniment un `nextPageToken`.

## La suppression est définitive

`files.delete` supprime **sans passer par la corbeille**. C'est une décision
argumentée dans `docs/adr/0001-destinations-distantes.md` (section « Lister et
supprimer sur Google Drive ») et annoncée à l'utilisateur dans
`docs/destinations/google-drive.md` : un fichier à la corbeille continue de
consommer le quota du compte Google pendant trente jours, ce qui priverait d'effet
la rétention de l'utilisateur qui l'a réglée précisément parce que son Drive se
remplit.

## Journaux

Ni le jeton ni l'en-tête `Authorization` n'y figurent, à aucun niveau. La requête
`q` et les noms de fichiers ne sont écrits qu'en `debug` : ils portent les noms
des sauvegardes de l'utilisateur. Les niveaux supérieurs ne citent que des
compteurs, un statut HTTP et l'identifiant opaque d'un fichier.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import quote

from ..errors import DestinationError
from ..models import RemoteBackup
from ..oauth import DestinationOAuth2Session
from ..retention import VALEURS_DU_MARQUEUR
from .google_drive import texte_optionnel
from .google_drive_upload import (
    CHAMPS_FICHIER,
    MARQUEUR_AUTO_BACKUP,
    MIME_DOSSIER,
    URL_FICHIERS,
    VRAI_DRIVE,
    async_appel_drive_json,
    async_dossier_cible,
    echapper,
    sauvegarde_distante_du_fichier,
)

_LOGGER = logging.getLogger(__name__)

# Nombre de fichiers demandés par page. Google plafonne `pageSize` à 1000 et
# retombe à 100 par défaut ; 100 garde une réponse de taille modeste tout en
# n'exigeant qu'une page pour les dossiers réels, où quelques dizaines de
# sauvegardes s'accumulent au plus.
TAILLE_DE_PAGE = 100

# Plafond du nombre de pages parcourues, et donc du nombre d'allers-retours d'un
# listage : 50 pages de 100 fichiers couvrent très largement tout dossier de
# sauvegardes réel. Ce n'est pas un réglage mais une borne de sûreté, celle que le
# contrat de `RemoteDestination` réclame : un `nextPageToken` que Google
# renverrait indéfiniment — incident, jeu de résultats instable — ferait sinon
# tourner le listage sans fin, sous le seul garde-fou grossier du coordinateur de
# purge. Le dépassement est signalé et la purge travaille sur ce qui a été lu :
# elle ne supprime que ce qu'elle a vu, jamais sur une présomption.
PAGES_MAX = 50

# Projections demandées à `files.list`. Elles reprennent `CHAMPS_FICHIER` — les
# mêmes champs que le téléversement, pour que les deux construisent la même
# sauvegarde distante — et y ajoutent `mimeType` et `trashed`, dont le listage a
# besoin pour revérifier lui-même ce que la requête `q` a déjà filtré.
CHAMPS_LISTAGE = f"nextPageToken,files({CHAMPS_FICHIER},mimeType,trashed)"

# Ordre demandé à Google : de la plus ancienne à la plus récente. La rétention
# retrie de son côté, mais un ordre stable rend le départage de deux sauvegardes
# de même date déterministe — c'est la première listée qui part — et les journaux
# lisibles.
ORDRE_LISTAGE = "createdTime"


def requete_des_sauvegardes(dossier_id: str) -> str:
    """Requête `q` retrouvant les sauvegardes déposées dans un dossier.

    Quatre conditions, toutes nécessaires :

    - `'<dossier>' in parents` : le dossier dédié de la destination, et lui seul ;
    - `trashed = false` : un fichier mis à la corbeille par l'utilisateur est déjà
      supprimé de son point de vue, le remonter le ferait « purger » une seconde
      fois et fausserait le compte de la rétention ;
    - `appProperties has { key='auto_backup' and value='true' }` : la preuve de
      provenance que la purge exige. C'est la seule condition qui garantit qu'un
      fichier déposé par l'utilisateur dans ce dossier ne peut pas être touché ;
    - `mimeType != '<dossier>'` : les dossiers créés par l'intégration portent le
      même marqueur que les fichiers. Sans cette exclusion, une destination réglée
      sur `Sauvegardes` remonterait le dossier `Sauvegardes/Home Assistant` d'une
      autre destination comme une sauvegarde, que la purge pourrait supprimer avec
      tout son contenu.

    Chaque valeur littérale est échappée par `echapper()`, la fonction du
    téléversement : un identifiant de dossier vient de Google, mais rien
    n'empêche une donnée persistée abîmée d'y glisser une apostrophe.
    """
    return (
        f"'{echapper(dossier_id)}' in parents and trashed = false "
        f"and appProperties has {{ key='{echapper(MARQUEUR_AUTO_BACKUP)}' "
        f"and value='{echapper(VRAI_DRIVE)}' }} "
        f"and mimeType != '{echapper(MIME_DOSSIER)}'"
    )


def porte_le_marqueur_drive(proprietes: Mapping[str, Any]) -> bool:
    """Indique si les propriétés privées d'un fichier portent le marqueur.

    Vérification **redondante** avec le filtre `q` envoyé à Google, et c'est
    voulu : la requête est une optimisation — elle évite de rapatrier tout le
    dossier —, pas la barrière de sûreté. Un filtre mal composé, une évolution de
    l'API ou une réponse inattendue ne doivent pas pouvoir rendre purgeable un
    fichier qu'Auto Backup n'a pas déposé.

    Les valeurs acceptées sont celles de la rétention (`VALEURS_DU_MARQUEUR`) :
    l'API Drive ne conserve les propriétés privées qu'en texte, et la liste des
    formes reconnues n'a, là aussi, qu'une définition.
    """
    valeur = proprietes.get(MARQUEUR_AUTO_BACKUP)
    if isinstance(valeur, bool):
        return valeur
    if isinstance(valeur, str):
        return valeur.strip().casefold() in VALEURS_DU_MARQUEUR
    return False


def _est_une_sauvegarde(fichier: Mapping[str, Any]) -> bool:
    """Indique si ce fichier est une sauvegarde déposée par Auto Backup.

    Les trois conditions de la requête `q` sont rejouées sur la réponse. Un
    fichier écarté est journalisé en `debug` avec son seul identifiant : le nom
    est celui d'un document de l'utilisateur, il n'a pas à figurer dans un
    journal au-delà.
    """
    identifiant = texte_optionnel(fichier.get("id")) or "sans identifiant"
    if fichier.get("trashed") is True:
        _LOGGER.debug(
            "Fichier Drive « %s » ignoré par le listage : il est à la corbeille",
            identifiant,
        )
        return False
    if texte_optionnel(fichier.get("mimeType")) == MIME_DOSSIER:
        _LOGGER.debug(
            "Fichier Drive « %s » ignoré par le listage : c'est un dossier",
            identifiant,
        )
        return False
    proprietes = fichier.get("appProperties")
    proprietes = proprietes if isinstance(proprietes, Mapping) else {}
    if not porte_le_marqueur_drive(proprietes):
        _LOGGER.debug(
            "Fichier Drive « %s » ignoré par le listage : il ne porte pas le "
            "marqueur d'Auto Backup",
            identifiant,
        )
        return False
    return True


async def _async_page(
    session: DestinationOAuth2Session,
    dossier_id: str,
    jeton_de_page: str | None,
) -> Mapping[str, Any]:
    """Demande une page de `files.list` et renvoie sa réponse décodée."""
    params = {
        "q": requete_des_sauvegardes(dossier_id),
        "fields": CHAMPS_LISTAGE,
        "spaces": "drive",
        "pageSize": str(TAILLE_DE_PAGE),
        "orderBy": ORDRE_LISTAGE,
    }
    if jeton_de_page is not None:
        params["pageToken"] = jeton_de_page
    reponse = await async_appel_drive_json(
        session,
        "GET",
        URL_FICHIERS,
        params=params,
        etiquette="listage des sauvegardes du dossier distant",
    )
    return reponse.charge if isinstance(reponse.charge, Mapping) else {}


async def async_lister_les_sauvegardes(
    session: DestinationOAuth2Session,
    *,
    dossier: str,
    dossier_id: str | None = None,
    memoriser: Callable[[str], None] | None = None,
) -> list[RemoteBackup]:
    """Liste les sauvegardes déposées par Auto Backup dans le dossier distant.

    Le dossier cible est celui du téléversement, obtenu par
    `async_dossier_cible()` : déjà mémorisé le plus souvent, sinon retrouvé — ou
    créé, vide — segment par segment. Créer un dossier pour le lister peut
    surprendre ; c'est le prix d'un **chemin unique** vers le dossier cible, et le
    dossier ainsi créé est exactement celui que le prochain téléversement
    utilisera. Le cas ne se produit que sur une destination qui n'a encore rien
    déposé, et le listage renvoie alors une liste vide, ce qui est la vérité.

    Les pages sont suivies jusqu'à épuisement des `nextPageToken`, dans la limite
    de `PAGES_MAX`. Un fichier sans identifiant exploitable est ignoré plutôt que
    de faire échouer tout le listage : la purge des autres sauvegardes reste plus
    utile qu'un abandon.

    Une sauvegarde déjà vue n'est comptée qu'une fois. La pagination de Drive ne
    garantit pas un instantané : un téléversement concurrent, ou un simple
    réordonnancement côté Google, peut faire apparaître le même fichier sur deux
    pages. Le laisser passer deux fois ferait croire à la rétention en nombre
    qu'il y a une sauvegarde de plus que la réalité, et lui ferait supprimer une
    sauvegarde qui devait être conservée.
    """
    identifiant_du_dossier = await async_dossier_cible(
        session, dossier, dossier_id=dossier_id, memoriser=memoriser
    )

    sauvegardes: list[RemoteBackup] = []
    vues: set[str] = set()
    jeton_de_page: str | None = None
    pages = 0
    while True:
        pages += 1
        charge = await _async_page(session, identifiant_du_dossier, jeton_de_page)
        fichiers = charge.get("files")
        fichiers = fichiers if isinstance(fichiers, list) else []
        for fichier in fichiers:
            if not isinstance(fichier, Mapping) or not _est_une_sauvegarde(fichier):
                continue
            try:
                distante = sauvegarde_distante_du_fichier(fichier, dossier=dossier)
            except DestinationError as err:
                # Un fichier illisible (identifiant absent) n'est pas une raison
                # d'abandonner les autres : il ne sera simplement pas purgé.
                _LOGGER.warning(
                    "Fichier Drive ignoré par le listage du dossier « %s » : %s",
                    dossier,
                    err,
                )
                continue
            if distante.remote_id in vues:
                _LOGGER.debug(
                    "Sauvegarde distante « %s » déjà vue sur une page précédente : "
                    "elle n'est comptée qu'une fois",
                    distante.remote_id,
                )
                continue
            vues.add(distante.remote_id)
            sauvegardes.append(distante)

        jeton_de_page = texte_optionnel(charge.get("nextPageToken"))
        if jeton_de_page is None:
            break
        if pages >= PAGES_MAX:
            _LOGGER.warning(
                "Listage des sauvegardes de « %s » interrompu après %s pages "
                "(%s sauvegardes lues) : Google en annonce d'autres. La rétention "
                "ne s'appliquera qu'à ce qui a été lu",
                dossier,
                pages,
                len(sauvegardes),
            )
            break

    _LOGGER.debug(
        "%s sauvegarde(s) Auto Backup listée(s) dans « %s » en %s page(s)",
        len(sauvegardes),
        dossier,
        pages,
    )
    return sauvegardes


async def async_supprimer_la_sauvegarde(
    session: DestinationOAuth2Session, remote_id: str
) -> None:
    """Supprime définitivement un fichier de Drive, par son identifiant.

    L'appel est **idempotent du point de vue de l'appelant** : un fichier déjà
    absent fait répondre `404` à Google, que `erreur_de_la_reponse()` traduit en
    `DestinationNotFoundError` — la purge distante (#9) y voit une sauvegarde
    déjà purgée, retire son entrée du registre et continue. C'est aussi ce qui
    rend sûre une nouvelle tentative après une réponse perdue : la première a pu
    aboutir, la seconde répond `404`, et le résultat est le même.

    L'identifiant est encodé avant d'être placé dans le chemin de l'URL. Un
    identifiant Drive n'a jamais besoin de l'être — Google n'en produit qu'en
    lettres, chiffres, tiret et souligné — mais celui-ci peut venir du registre
    persistant, donc d'un fichier de stockage éditable à la main : sans encodage,
    une barre oblique désignerait une autre ressource de l'API.

    **La suppression ne revérifie pas le marqueur et fait confiance à l'appelant.**
    Seule la purge distante (retention.py, qui ne transmet que des identifiants du
    registre ou d'un listage marqué) doit appeler cette fonction.
    """
    identifiant = texte_optionnel(remote_id)
    if identifiant is None:
        raise DestinationError(
            "la suppression d'une sauvegarde Google Drive exige un identifiant "
            "de fichier non vide"
        )

    await async_appel_drive_json(
        session,
        "DELETE",
        f"{URL_FICHIERS}/{quote(identifiant, safe='')}",
        etiquette=f"suppression de la sauvegarde distante « {identifiant} »",
    )
    _LOGGER.debug(
        "Sauvegarde distante « %s » supprimée définitivement de Google Drive",
        identifiant,
    )


__all__ = [
    "CHAMPS_LISTAGE",
    "ORDRE_LISTAGE",
    "PAGES_MAX",
    "TAILLE_DE_PAGE",
    "async_lister_les_sauvegardes",
    "async_supprimer_la_sauvegarde",
    "porte_le_marqueur_drive",
    "requete_des_sauvegardes",
]
