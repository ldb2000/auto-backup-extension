"""Listage et suppression des sauvegardes Google Drive (issue #15).

Ces tests suivent la rétention distante jusqu'au bout sur une destination Google
Drive : lister ce qui a été déposé — sur plusieurs pages —, écarter tout ce qui
n'est pas une sauvegarde d'Auto Backup, supprimer les sauvegardes expirées et
émettre l'événement de purge distante.

**Aucun appel réseau n'est fait et aucune valeur réelle n'est employée** : l'API
Drive est simulée par `aioclient_mock`, avec un simulateur (`_FauxDrive`) qui
pagine ses réponses comme le fait Google — `nextPageToken` tant qu'il reste des
fichiers.

Le simulateur **n'applique pas** la requête `q` qu'il reçoit : il renvoie tout ce
qu'on lui a donné, fichiers étrangers et fichiers à la corbeille compris. C'est
délibéré. Le filtre envoyé à Google est une optimisation, pas la barrière de
sûreté ; les tests de filtrage éprouvent donc la vérification que le fournisseur
refait sur chaque fichier reçu, et un test séparé vérifie que la requête envoyée
porte bien les quatre conditions attendues.

Les fixtures de configuration sont réutilisées depuis
`test_provider_google_drive_upload` plutôt que dupliquées : même destination
factice, mêmes jetons, mêmes corps d'erreur Google.
"""

from __future__ import annotations

import logging
import re
from datetime import timedelta
from typing import Any

import pytest
from homeassistant.components import persistent_notification
from homeassistant.const import ATTR_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)
from yarl import URL

from custom_components.auto_backup.const import (
    ATTR_DESTINATION,
    ATTR_REMOTE_ID,
    ATTR_REMOTE_IDS,
    ATTR_SLUG,
    CONF_AUTO_PURGE,
    CONF_BACKUP_TIMEOUT,
    CONF_DESTINATIONS,
    CONF_PROVIDER_DATA,
    CONF_RETENTION_COUNT,
    CONF_RETENTION_DAYS,
    DATA_DESTINATIONS,
    DATA_REMOTE_BACKUPS,
    DOMAIN,
    EVENT_REMOTE_PURGE,
    EVENT_UPLOAD_SUCCESSFUL,
    SERVICE_PURGE,
)
from custom_components.auto_backup.destinations import (
    DestinationAuthError,
    DestinationError,
    DestinationNotFoundError,
    identifiant_du_probleme,
)
from custom_components.auto_backup.destinations.notifications import (
    identifiant_de_notification_de_reauthentification,
)
from custom_components.auto_backup.destinations.providers.google_drive import (
    CLE_ID_DU_DOSSIER,
    GoogleDriveDestination,
)
from custom_components.auto_backup.destinations.providers.google_drive_listage import (
    CHAMPS_LISTAGE,
    PAGES_MAX,
    TAILLE_DE_PAGE,
    porte_le_marqueur_drive,
    requete_des_sauvegardes,
)
from custom_components.auto_backup.destinations.providers.google_drive_upload import (
    MARQUEUR_AUTO_BACKUP,
    MIME_DOSSIER,
    MIME_SAUVEGARDE,
    PROPRIETE_NOM,
    PROPRIETE_SLUG,
    URL_FICHIERS,
    VRAI_DRIVE,
)
from custom_components.auto_backup.destinations.retention import porte_le_marqueur
from test_provider_google_drive_upload import (  # noqa: F401 - fixtures réutilisées
    ACCES_FACTICE,
    CLIENT_SECRET_FACTICE,
    DOSSIER,
    EMAIL_DU_COMPTE,
    IDENTIFIANT_DESTINATION,
    MD5_FACTICE,
    config_google,
    delais,
    erreur_google,
    jeton_google,
)

### Valeurs de test : toutes inventées. ###

ID_DOSSIER = "dossier-drive-factice"
ID_AUTRE_DOSSIER = "sous-dossier-d-une-autre-destination"

# Requêtes `q` : la recherche de dossier du téléversement filtre sur `mimeType =
# <dossier>`, le listage sur `appProperties has`. C'est ce qui les distingue.
_MARQUE_DE_LISTAGE = "appProperties has"


def fichier_drive(
    identifiant: str,
    *,
    nom: str | None = None,
    slug: str | None = None,
    taille: int | None = 4096,
    jours: float = 0,
    cree_le: Any = ...,
    marqueur: Any = VRAI_DRIVE,
    corbeille: bool = False,
    mime: str = MIME_SAUVEGARDE,
    md5: str | None = MD5_FACTICE,
) -> dict[str, Any]:
    """Fichier tel que `files.list` le renvoie, avec ses propriétés privées.

    `marqueur=None` simule un fichier que l'utilisateur aurait déposé lui-même
    dans le dossier : il ne porte aucune propriété d'Auto Backup.
    """
    proprietes: dict[str, Any] = {}
    if marqueur is not None:
        proprietes[MARQUEUR_AUTO_BACKUP] = marqueur
    if slug is not None:
        proprietes[PROPRIETE_SLUG] = slug
    if nom is not None:
        proprietes[PROPRIETE_NOM] = nom

    fichier: dict[str, Any] = {
        "id": identifiant,
        "name": f"{nom or identifiant} [{slug or identifiant}].tar",
        "mimeType": mime,
        "trashed": corbeille,
        "appProperties": proprietes,
    }
    if taille is not None:
        fichier["size"] = str(taille)
    if cree_le is ...:
        date = dt_util.utcnow() - timedelta(days=jours)
        fichier["createdTime"] = date.isoformat().replace("+00:00", "Z")
    elif cree_le is not None:
        fichier["createdTime"] = cree_le
    if md5 is not None:
        fichier["md5Checksum"] = md5
    return fichier


### Simulateur de l'API Drive ###


class _FauxDrive:
    """API Drive simulée : recherche de dossier, listage paginé, suppression.

    Le listage renvoie `fichiers` par tranches de `par_page`, avec un
    `nextPageToken` tant qu'il en reste — sans jamais appliquer la requête `q`
    reçue (cf. l'en-tête du module).
    """

    def __init__(
        self,
        aioclient_mock: AiohttpClientMocker,
        *,
        fichiers: list[dict[str, Any]] | None = None,
        dossiers: dict[tuple[str, str], str] | None = None,
        par_page: int = 2,
        pannes_listage: list[Any] | None = None,
        pannes_suppression: dict[str, Any] | None = None,
        jeton_sans_fin: bool = False,
    ) -> None:
        """Branche le simulateur sur les trois points d'accès utilisés."""
        self.mock = aioclient_mock
        self.fichiers = list(fichiers or [])
        self.dossiers = (
            {("root", DOSSIER): ID_DOSSIER} if dossiers is None else dossiers
        )
        self.par_page = par_page
        self.pannes_listage = list(pannes_listage or [])
        self.pannes_suppression = dict(pannes_suppression or {})
        self.jeton_sans_fin = jeton_sans_fin
        self.requetes: list[str] = []
        self.jetons_recus: list[str | None] = []
        self.recherches: list[tuple[str, str]] = []
        self.creations: list[dict[str, Any]] = []
        self.supprimes: list[str] = []

        aioclient_mock.get(URL_FICHIERS, side_effect=self._get)
        aioclient_mock.post(URL_FICHIERS, side_effect=self._creer)
        aioclient_mock.delete(
            re.compile(rf"^{re.escape(URL_FICHIERS)}/"), side_effect=self._supprimer
        )

    ### Réponses ###

    def _reponse(
        self, methode: str, url: URL, statut: int, corps: Any = None
    ) -> AiohttpClientMockResponse:
        """Construit une réponse simulée."""
        return AiohttpClientMockResponse(methode, url, status=statut, json=corps)

    def _panne(self, methode: str, url: URL, panne: Any) -> AiohttpClientMockResponse:
        """Rejoue une panne programmée : réponse en erreur ou exception levée."""
        if isinstance(panne, BaseException):
            raise panne
        statut, corps = panne
        return self._reponse(methode, url, statut, corps)

    ### Points d'accès ###

    async def _get(
        self, methode: str, url: URL, donnees: Any
    ) -> AiohttpClientMockResponse:
        """`files.list` : recherche de dossier, ou listage des sauvegardes."""
        requete = url.query["q"]
        if _MARQUE_DE_LISTAGE not in requete:
            return await self._rechercher(methode, url, requete)
        return await self._lister(methode, url, requete)

    async def _rechercher(
        self, methode: str, url: URL, requete: str
    ) -> AiohttpClientMockResponse:
        """Recherche d'un sous-dossier, comme au téléversement."""
        nom = re.search(r"name = '([^']*)'", requete).group(1)
        parent = re.search(r"'([^']*)' in parents", requete).group(1)
        self.recherches.append((parent, nom))
        identifiant = self.dossiers.get((parent, nom))
        trouves = [{"id": identifiant, "name": nom}] if identifiant else []
        return self._reponse(methode, url, 200, {"files": trouves})

    async def _lister(
        self, methode: str, url: URL, requete: str
    ) -> AiohttpClientMockResponse:
        """Une page de `files.list`, avec son `nextPageToken` s'il en reste."""
        if self.pannes_listage:
            return self._panne(methode, url, self.pannes_listage.pop(0))
        self.requetes.append(requete)
        jeton = url.query.get("pageToken")
        self.jetons_recus.append(jeton)
        debut = 0 if jeton is None else int(jeton)
        tranche = self.fichiers[debut : debut + self.par_page]
        charge: dict[str, Any] = {"files": tranche}
        fin = debut + len(tranche)
        if self.jeton_sans_fin or fin < len(self.fichiers):
            charge["nextPageToken"] = str(fin if not self.jeton_sans_fin else 0)
        return self._reponse(methode, url, 200, charge)

    async def _creer(
        self, methode: str, url: URL, donnees: Any
    ) -> AiohttpClientMockResponse:
        """`files.create` : crée un dossier et lui attribue un identifiant."""
        self.creations.append(dict(donnees))
        parent = (donnees.get("parents") or ["root"])[0]
        nom = donnees["name"]
        identifiant = f"dossier-cree-{len(self.creations)}"
        self.dossiers[(parent, nom)] = identifiant
        return self._reponse(methode, url, 200, {"id": identifiant, "name": nom})

    async def _supprimer(
        self, methode: str, url: URL, donnees: Any
    ) -> AiohttpClientMockResponse:
        """`files.delete` : réponse vide (204), ou panne programmée."""
        identifiant = url.path.rsplit("/", 1)[-1]
        if (panne := self.pannes_suppression.pop(identifiant, None)) is not None:
            return self._panne(methode, url, panne)
        self.supprimes.append(identifiant)
        self.fichiers = [
            fichier for fichier in self.fichiers if fichier.get("id") != identifiant
        ]
        return AiohttpClientMockResponse(methode, url, status=204, text="")


### Fixtures et aides ###


async def _entree(
    hass: HomeAssistant, *, options: dict[str, Any] | None = None, **surcharges: Any
) -> MockConfigEntry:
    """Initialise l'intégration avec une destination Google Drive.

    Le dossier cible est déjà mémorisé par défaut (`folder_id`) : c'est l'état
    normal d'une destination qui a déjà déposé une sauvegarde.
    """
    surcharges.setdefault(
        CONF_PROVIDER_DATA,
        {**config_google()[CONF_PROVIDER_DATA], CLE_ID_DU_DOSSIER: ID_DOSSIER},
    )
    entree = MockConfigEntry(
        domain=DOMAIN,
        title="Auto Backup",
        data={},
        options={
            CONF_AUTO_PURGE: True,
            CONF_BACKUP_TIMEOUT: 20,
            CONF_DESTINATIONS: [config_google(**surcharges)],
            **(options or {}),
        },
    )
    entree.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entree.entry_id)
    await hass.async_block_till_done()
    return entree


@pytest.fixture
async def entree_drive(
    hass: HomeAssistant, integration_backup: None
) -> MockConfigEntry:
    """Entrée portant une destination Google Drive dont le dossier est connu."""
    return await _entree(hass)


def _destination(hass: HomeAssistant) -> GoogleDriveDestination:
    """Destination Google Drive chargée par le gestionnaire."""
    return hass.data[DATA_DESTINATIONS].async_get(IDENTIFIANT_DESTINATION)


def _appels(aioclient_mock: AiohttpClientMocker, methode: str) -> list[URL]:
    """URL des appels reçus pour cette méthode, dans l'ordre."""
    return [
        url
        for appelee, url, _, _ in aioclient_mock.mock_calls
        if appelee.lower() == methode.lower()
    ]


def _appels_de_listage(aioclient_mock: AiohttpClientMocker) -> list[URL]:
    """URL des appels de listage reçus, dans l'ordre."""
    return [
        url
        for url in _appels(aioclient_mock, "get")
        if _MARQUE_DE_LISTAGE in (url.query.get("q") or "")
    ]


async def _inscrire(
    hass: HomeAssistant, remote_id: str, nom: str = "Sauvegarde"
) -> None:
    """Inscrit une sauvegarde au registre distant, comme le fait un dépôt réussi."""
    hass.bus.async_fire(
        EVENT_UPLOAD_SUCCESSFUL,
        {
            ATTR_DESTINATION: IDENTIFIANT_DESTINATION,
            ATTR_REMOTE_ID: remote_id,
            ATTR_NAME: nom,
            ATTR_SLUG: None,
        },
    )
    await hass.async_block_till_done()


### La requête envoyée à Google ###


def test_la_requete_filtre_le_dossier_la_corbeille_et_le_marqueur() -> None:
    """Critères 1 et 2 : les quatre conditions sont dans la requête `q`."""
    requete = requete_des_sauvegardes(ID_DOSSIER)

    assert requete == (
        f"'{ID_DOSSIER}' in parents and trashed = false "
        "and appProperties has { key='auto_backup' and value='true' } "
        f"and mimeType != '{MIME_DOSSIER}'"
    )


def test_la_requete_echappe_les_valeurs_litterales() -> None:
    """Une apostrophe dans l'identifiant ne rompt pas la chaîne littérale."""
    requete = requete_des_sauvegardes("dossier d'automne")

    assert requete.startswith("'dossier d\\'automne' in parents")


@pytest.mark.parametrize(
    ("valeur", "attendu"),
    [
        (VRAI_DRIVE, True),
        ("True", True),
        ("1", True),
        (True, True),
        ("oui", True),
        (None, False),
        ("false", False),
        ("", False),
        (0, False),
        (False, False),
    ],
)
def test_le_marqueur_est_reconnu_sous_ses_formes_textuelles(
    valeur: Any, attendu: bool
) -> None:
    """L'API Drive ne conserve les propriétés privées qu'en texte."""
    proprietes = {} if valeur is None else {MARQUEUR_AUTO_BACKUP: valeur}

    assert porte_le_marqueur_drive(proprietes) is attendu


### Listage ###


async def test_le_listage_suit_les_pages_jusqu_a_epuisement(
    hass: HomeAssistant,
    entree_drive: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Critère 1 : cinq sauvegardes réparties sur trois pages sont toutes lues."""
    faux = _FauxDrive(
        aioclient_mock,
        fichiers=[fichier_drive(f"sauvegarde-{rang}") for rang in range(5)],
        par_page=2,
    )

    distantes = await _destination(hass).async_list_backups()

    # Trois pages : 2 + 2 + 1, et le jeton de chaque page est celui renvoyé.
    assert faux.jetons_recus == [None, "2", "4"]
    assert [distante.remote_id for distante in distantes] == [
        "sauvegarde-0",
        "sauvegarde-1",
        "sauvegarde-2",
        "sauvegarde-3",
        "sauvegarde-4",
    ]

    # Chaque page est demandée avec les mêmes projections et le même ordre.
    urls = _appels_de_listage(aioclient_mock)
    assert len(urls) == 3
    for url in urls:
        assert url.query["fields"] == CHAMPS_LISTAGE
        assert url.query["pageSize"] == str(TAILLE_DE_PAGE)
        assert url.query["spaces"] == "drive"
        assert url.query["orderBy"] == "createdTime"
    assert "pageToken" not in urls[0].query


async def test_le_listage_rapporte_identifiant_nom_taille_et_date(
    hass: HomeAssistant,
    entree_drive: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Critère 1 : la sauvegarde distante porte tout ce que la rétention exige."""
    _FauxDrive(
        aioclient_mock,
        fichiers=[
            fichier_drive(
                "sauvegarde-1",
                nom="Sauvegarde du 25",
                slug="abc123",
                taille=4096,
                cree_le="2026-09-25T10:11:12.000Z",
            )
        ],
    )

    (distante,) = await _destination(hass).async_list_backups()

    assert distante.remote_id == "sauvegarde-1"
    assert distante.name == "Sauvegarde du 25 [abc123].tar"
    assert distante.slug == "abc123"
    assert distante.size == 4096
    assert distante.created_at is not None
    assert distante.created_at.isoformat() == "2026-09-25T10:11:12+00:00"
    assert distante.path == f"{DOSSIER}/Sauvegarde du 25 [abc123].tar"
    assert distante.metadata["md5Checksum"] == MD5_FACTICE
    # Critère du commentaire de l'issue : la rétention (#9) doit reconnaître le
    # marqueur relu chez Google, même si le registre du fork a été perdu.
    assert porte_le_marqueur(distante) is True


async def test_le_listage_ecarte_ce_qui_n_est_pas_une_sauvegarde(
    hass: HomeAssistant,
    entree_drive: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Critère 2 : fichier étranger, corbeille et dossier ne remontent jamais.

    Le simulateur ignore volontairement la requête `q` : c'est la vérification
    refaite par le fournisseur sur chaque fichier reçu qui est éprouvée ici.
    """
    _FauxDrive(
        aioclient_mock,
        fichiers=[
            fichier_drive("photos-de-vacances", marqueur=None),
            fichier_drive("sauvegarde-a-la-corbeille", corbeille=True),
            fichier_drive("autre-destination", mime=MIME_DOSSIER),
            fichier_drive("sauvegarde-valide"),
        ],
        par_page=10,
    )
    caplog.set_level(logging.DEBUG)

    distantes = await _destination(hass).async_list_backups()

    assert [distante.remote_id for distante in distantes] == ["sauvegarde-valide"]
    assert "ne porte pas le marqueur" in caplog.text
    assert "à la corbeille" in caplog.text
    assert "c'est un dossier" in caplog.text


async def test_un_dossier_marque_n_est_jamais_pris_pour_une_sauvegarde(
    hass: HomeAssistant,
    entree_drive: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Un sous-dossier porte le même marqueur qu'un fichier, et n'est pas listé.

    Le cas n'est pas théorique : deux destinations réglées l'une sur
    `Sauvegardes` et l'autre sur `Sauvegardes/Home Assistant` font du dossier de
    la seconde un enfant marqué du dossier de la première. Le lister le rendrait
    purgeable, avec tout son contenu.
    """
    _FauxDrive(
        aioclient_mock,
        fichiers=[
            fichier_drive(ID_AUTRE_DOSSIER, mime=MIME_DOSSIER, taille=None, md5=None)
        ],
        par_page=10,
    )

    assert await _destination(hass).async_list_backups() == []


async def test_un_fichier_marque_d_un_type_different_du_tar_reste_une_sauvegarde(
    hass: HomeAssistant,
    entree_drive: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Seul le type dossier est exclu, pas un autre type que celui du tar.

    `_est_une_sauvegarde()` n'exige pas `mimeType == MIME_SAUVEGARDE` : elle
    exclut uniquement `MIME_DOSSIER` (cf. son docstring, « il ne remonte jamais
    un dossier »). Un fichier marqué Auto Backup d'un type inhabituel — jamais un
    document de l'utilisateur, la portée `drive.file` ne rendant visible que ce
    qu'Auto Backup a lui-même créé — reste donc purgeable. Ce test fige ce
    comportement pour qu'un resserrement du filtre sur `MIME_SAUVEGARDE` soit un
    choix délibéré, et non une régression silencieuse.
    """
    _FauxDrive(
        aioclient_mock,
        fichiers=[fichier_drive("type-inhabituel", mime="application/octet-stream")],
    )

    distantes = await _destination(hass).async_list_backups()

    assert [distante.remote_id for distante in distantes] == ["type-inhabituel"]


async def test_un_dossier_vide_ne_remonte_rien(
    hass: HomeAssistant,
    entree_drive: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Une réponse sans clé `files` est un dossier vide, pas une erreur."""
    faux = _FauxDrive(aioclient_mock, fichiers=[])

    assert await _destination(hass).async_list_backups() == []
    assert faux.jetons_recus == [None]


async def test_un_fichier_sans_identifiant_est_ignore_sans_abandon(
    hass: HomeAssistant,
    entree_drive: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Un fichier illisible ne fait pas perdre les autres : il n'est pas purgé."""
    sans_id = fichier_drive("orphelin")
    del sans_id["id"]
    _FauxDrive(aioclient_mock, fichiers=[sans_id, fichier_drive("bonne")], par_page=10)

    with caplog.at_level(logging.WARNING):
        distantes = await _destination(hass).async_list_backups()

    assert [distante.remote_id for distante in distantes] == ["bonne"]
    assert "ignoré par le listage" in caplog.text


async def test_une_sauvegarde_sans_taille_ni_date_reste_listee(
    hass: HomeAssistant,
    entree_drive: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Drive peut taire la taille ou la date : la sauvegarde existe quand même."""
    _FauxDrive(
        aioclient_mock,
        fichiers=[fichier_drive("frugale", taille=None, cree_le=None, md5=None)],
    )

    (distante,) = await _destination(hass).async_list_backups()

    assert distante.size is None
    assert distante.created_at is None


async def test_une_sauvegarde_vue_sur_deux_pages_n_est_comptee_qu_une_fois(
    hass: HomeAssistant,
    integration_backup: None,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """La pagination de Drive n'est pas un instantané, et ça compte.

    Un dépôt concurrent ou un simple réordonnancement côté Google peut faire
    apparaître le même fichier sur deux pages. Le compter deux fois ferait croire
    à `retention_count` qu'il y a une sauvegarde de trop — et lui ferait supprimer
    une sauvegarde qui devait rester.
    """
    await _entree(hass, **{CONF_RETENTION_COUNT: 2})
    doublon = fichier_drive("revenante", jours=5)
    faux = _FauxDrive(
        aioclient_mock,
        fichiers=[doublon, fichier_drive("autre", jours=1), doublon],
        par_page=2,
    )
    caplog.set_level(logging.DEBUG)

    distantes = await _destination(hass).async_list_backups()

    assert [distante.remote_id for distante in distantes] == ["revenante", "autre"]
    assert "déjà vue sur une page précédente" in caplog.text

    # Et la conséquence : la rétention en garde deux, donc ne supprime rien.
    await hass.services.async_call(DOMAIN, SERVICE_PURGE, blocking=True)
    await hass.async_block_till_done()

    assert faux.supprimes == []


async def test_le_listage_borne_le_nombre_de_pages_parcourues(
    hass: HomeAssistant,
    entree_drive: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Un `nextPageToken` sans fin ne fait pas tourner le listage indéfiniment.

    C'est la borne que le contrat de `RemoteDestination` réclame au fournisseur :
    le coordinateur de purge ne pose qu'un filet de sécurité grossier.
    """
    faux = _FauxDrive(
        aioclient_mock, fichiers=[fichier_drive("unique")], jeton_sans_fin=True
    )

    with caplog.at_level(logging.WARNING):
        distantes = await _destination(hass).async_list_backups()

    assert len(faux.jetons_recus) == PAGES_MAX
    # Le même fichier revient à chaque page : il n'est compté qu'une fois.
    assert [distante.remote_id for distante in distantes] == ["unique"]
    assert f"après {PAGES_MAX} pages" in caplog.text


async def test_le_dossier_memorise_evite_toute_recherche(
    hass: HomeAssistant,
    entree_drive: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Le listage part du dossier déjà mémorisé par le téléversement (#14)."""
    faux = _FauxDrive(aioclient_mock, fichiers=[fichier_drive("une")])

    await _destination(hass).async_list_backups()

    assert faux.recherches == []
    assert faux.creations == []
    assert _appels_de_listage(aioclient_mock)[0].query["q"] == requete_des_sauvegardes(
        ID_DOSSIER
    )


async def test_un_dossier_inconnu_est_resolu_puis_memorise(
    hass: HomeAssistant,
    integration_backup: None,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Sans identifiant mémorisé, le listage emprunte le chemin du téléversement.

    C'est le même `async_dossier_cible()` : le dossier retrouvé — ou créé, vide —
    est celui où le prochain téléversement déposera, et son identifiant est
    persisté au passage.
    """
    entree = await _entree(
        hass, **{CONF_PROVIDER_DATA: {"account_email": EMAIL_DU_COMPTE}}
    )
    faux = _FauxDrive(aioclient_mock, fichiers=[], dossiers={})

    assert await _destination(hass).async_list_backups() == []
    await hass.async_block_till_done()

    assert faux.recherches == [("root", DOSSIER)]
    assert [creation["name"] for creation in faux.creations] == [DOSSIER]
    (persistee,) = entree.options[CONF_DESTINATIONS]
    assert persistee[CONF_PROVIDER_DATA][CLE_ID_DU_DOSSIER] == "dossier-cree-1"


async def test_une_destination_sans_depot_prealable_ne_produit_aucun_effet_de_bord(
    hass: HomeAssistant,
    integration_backup: None,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Une destination qui n'a jamais rien déposé peut être purgée sans risque.

    Le cas précédent (`test_un_dossier_inconnu_est_resolu_puis_memorise`) prouve
    que le listage crée le dossier, vide, plutôt que d'échouer. Ce test va plus
    loin et passe par le **service `purge`**, avec une rétention configurée : il
    vérifie que ce chemin ne supprime rien, n'émet aucun événement, ne journalise
    rien en `ERROR`, et surtout ne recrée pas le dossier une seconde fois — la
    mémorisation du premier appel doit être effective avant le second.
    """
    entree = await _entree(
        hass,
        **{
            CONF_PROVIDER_DATA: {"account_email": EMAIL_DU_COMPTE},
            CONF_RETENTION_COUNT: 1,
        },
    )
    faux = _FauxDrive(aioclient_mock, fichiers=[], dossiers={})
    evenements = async_capture_events(hass, EVENT_REMOTE_PURGE)

    with caplog.at_level(logging.ERROR):
        await hass.services.async_call(DOMAIN, SERVICE_PURGE, blocking=True)
        await hass.async_block_till_done()

    assert faux.recherches == [("root", DOSSIER)]
    assert [creation["name"] for creation in faux.creations] == [DOSSIER]
    assert faux.supprimes == []
    assert evenements == []
    assert [
        enregistrement.message
        for enregistrement in caplog.records
        if enregistrement.levelno >= logging.ERROR
    ] == []
    (persistee,) = entree.options[CONF_DESTINATIONS]
    assert persistee[CONF_PROVIDER_DATA][CLE_ID_DU_DOSSIER] == "dossier-cree-1"

    # Un second appel réutilise le dossier mémorisé par le premier : ni nouvelle
    # recherche, ni nouvelle création.
    await hass.services.async_call(DOMAIN, SERVICE_PURGE, blocking=True)
    await hass.async_block_till_done()

    assert faux.recherches == [("root", DOSSIER)]
    assert len(faux.creations) == 1


async def test_un_echec_passager_du_listage_est_reessaye(
    hass: HomeAssistant,
    entree_drive: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    delais: list[float],  # noqa: F811 - fixture importée, requise sous ce nom
) -> None:
    """Le listage hérite des nouvelles tentatives du téléversement (#14)."""
    _FauxDrive(
        aioclient_mock,
        fichiers=[fichier_drive("une")],
        pannes_listage=[(429, erreur_google(429, "rateLimitExceeded"))],
    )

    distantes = await _destination(hass).async_list_backups()

    assert [distante.remote_id for distante in distantes] == ["une"]
    assert delais == [1.0]


async def test_une_erreur_d_authentification_au_listage_demande_une_reautorisation(
    hass: HomeAssistant,
    entree_drive: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Critère 5 : un 401 devient `DestinationAuthError` et crée le problème."""
    _FauxDrive(
        aioclient_mock,
        fichiers=[fichier_drive("une")],
        pannes_listage=[(401, erreur_google(401, "authError"))],
    )

    with pytest.raises(DestinationAuthError) as erreur:
        await _destination(hass).async_list_backups()

    assert "Ré-autorisez" in str(erreur.value)
    registre = ir.async_get(hass)
    assert (
        registre.async_get_issue(
            DOMAIN, identifiant_du_probleme(IDENTIFIANT_DESTINATION)
        )
        is not None
    )


### Suppression ###


async def test_la_suppression_retire_le_fichier_du_drive(
    hass: HomeAssistant,
    entree_drive: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Critère 3 : `files.delete` est appelé sur l'identifiant demandé."""
    faux = _FauxDrive(aioclient_mock, fichiers=[fichier_drive("a-supprimer")])

    await _destination(hass).async_delete_backup("a-supprimer")

    assert faux.supprimes == ["a-supprimer"]
    (url,) = _appels(aioclient_mock, "delete")
    assert str(url) == f"{URL_FICHIERS}/a-supprimer"
    assert await _destination(hass).async_list_backups() == []


async def test_un_fichier_deja_absent_n_est_pas_une_erreur_bloquante(
    hass: HomeAssistant,
    entree_drive: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Critère 3 : un 404 lève `DestinationNotFoundError`, traité comme purgé.

    C'est aussi ce qui rend sûre une nouvelle tentative après une réponse
    perdue : la première a pu aboutir, la seconde répond 404.
    """
    _FauxDrive(
        aioclient_mock,
        pannes_suppression={"envolee": (404, erreur_google(404, "notFound"))},
    )

    with pytest.raises(DestinationNotFoundError):
        await _destination(hass).async_delete_backup("envolee")


async def test_une_erreur_d_authentification_a_la_suppression_est_typee(
    hass: HomeAssistant,
    entree_drive: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Critère 5 : un 401 à la suppression déclenche aussi la ré-authentification."""
    _FauxDrive(
        aioclient_mock,
        pannes_suppression={"refusee": (401, erreur_google(401, "authError"))},
    )

    with pytest.raises(DestinationAuthError):
        await _destination(hass).async_delete_backup("refusee")

    registre = ir.async_get(hass)
    assert (
        registre.async_get_issue(
            DOMAIN, identifiant_du_probleme(IDENTIFIANT_DESTINATION)
        )
        is not None
    )


async def test_un_identifiant_vide_est_refuse_sans_appel_reseau(
    hass: HomeAssistant,
    entree_drive: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Un identifiant vide ne doit pas produire une requête sur la collection."""
    _FauxDrive(aioclient_mock)

    with pytest.raises(DestinationError) as erreur:
        await _destination(hass).async_delete_backup("   ")

    assert "identifiant" in str(erreur.value)
    assert aioclient_mock.mock_calls == []


async def test_un_identifiant_biscornu_reste_dans_le_chemin_du_fichier(
    hass: HomeAssistant,
    entree_drive: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Un identifiant venu du registre ne doit pas désigner une autre ressource.

    Le registre persistant est un fichier de stockage éditable à la main : sans
    encodage, une barre oblique remonterait d'un segment et changerait le point
    d'accès appelé. Encodé, l'identifiant reste un **segment** du chemin, que
    Google traitera comme un fichier inexistant.
    """
    _FauxDrive(aioclient_mock)

    await _destination(hass).async_delete_backup("../about")

    (url,) = _appels(aioclient_mock, "delete")
    assert url.raw_path == "/drive/v3/files/..%2Fabout"


### Purge de bout en bout ###


async def test_la_purge_supprime_les_sauvegardes_expirees_et_emet_l_evenement(
    hass: HomeAssistant,
    integration_backup: None,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Critère 4 : le service `purge` supprime réellement chez Google.

    La provenance vient ici du **marqueur relu chez Drive**, sans aucune entrée
    au registre : c'est l'exigence ajoutée par la validation métier de #9 — une
    sauvegarde déposée par une instance qui aurait perdu son registre doit rester
    purgeable.
    """
    await _entree(hass, **{CONF_RETENTION_DAYS: 7})
    faux = _FauxDrive(
        aioclient_mock,
        fichiers=[
            fichier_drive("tres-vieille", jours=30),
            fichier_drive("vieille", jours=10),
            fichier_drive("recente", jours=1),
        ],
        par_page=2,
    )
    evenements = async_capture_events(hass, EVENT_REMOTE_PURGE)

    await hass.services.async_call(DOMAIN, SERVICE_PURGE, blocking=True)
    await hass.async_block_till_done()

    assert faux.supprimes == ["tres-vieille", "vieille"]
    assert [fichier["id"] for fichier in faux.fichiers] == ["recente"]
    assert len(evenements) == 1
    assert evenements[0].data[ATTR_DESTINATION] == IDENTIFIANT_DESTINATION
    assert evenements[0].data[ATTR_REMOTE_IDS] == ["tres-vieille", "vieille"]


async def test_la_purge_en_nombre_garde_les_plus_recentes(
    hass: HomeAssistant,
    integration_backup: None,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Critère 4 : `retention_count` supprime de la plus ancienne à la plus jeune."""
    await _entree(hass, **{CONF_RETENTION_COUNT: 1})
    faux = _FauxDrive(
        aioclient_mock,
        fichiers=[
            fichier_drive("ancienne", jours=5),
            fichier_drive("moyenne", jours=3),
            fichier_drive("derniere", jours=0),
        ],
        par_page=2,
    )

    await hass.services.async_call(DOMAIN, SERVICE_PURGE, blocking=True)
    await hass.async_block_till_done()

    assert faux.supprimes == ["ancienne", "moyenne"]


async def test_la_purge_ne_touche_pas_aux_fichiers_de_l_utilisateur(
    hass: HomeAssistant,
    integration_backup: None,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Un fichier sans marqueur et inconnu du registre est intouchable."""
    await _entree(hass, **{CONF_RETENTION_DAYS: 1})
    faux = _FauxDrive(
        aioclient_mock,
        fichiers=[
            fichier_drive("photos-de-2019", jours=2000, marqueur=None),
            fichier_drive("sauvegarde-expiree", jours=30),
        ],
        par_page=10,
    )

    await hass.services.async_call(DOMAIN, SERVICE_PURGE, blocking=True)
    await hass.async_block_till_done()

    assert faux.supprimes == ["sauvegarde-expiree"]
    assert [fichier["id"] for fichier in faux.fichiers] == ["photos-de-2019"]


async def test_une_entree_de_registre_deja_absente_quitte_le_registre(
    hass: HomeAssistant,
    integration_backup: None,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Une sauvegarde disparue entre le listage et la suppression est « purgée ».

    Elle est inscrite au registre et présente au listage, mais Google répond 404
    à sa suppression : son entrée doit quitter le registre pour ne pas être
    retentée à chaque purge.
    """
    await _entree(hass, options={CONF_AUTO_PURGE: False}, **{CONF_RETENTION_DAYS: 1})
    _FauxDrive(
        aioclient_mock,
        fichiers=[fichier_drive("envolee", jours=30)],
        pannes_suppression={"envolee": (404, erreur_google(404, "notFound"))},
    )
    # `auto_purge` est désactivé : le registre est tenu, mais rien n'est purgé
    # avant l'appel explicite du service.
    await _inscrire(hass, "envolee")
    assert [
        entree.remote_id for entree in _registre(hass).entrees(IDENTIFIANT_DESTINATION)
    ] == ["envolee"]

    with caplog.at_level(logging.WARNING):
        await hass.services.async_call(DOMAIN, SERVICE_PURGE, blocking=True)
        await hass.async_block_till_done()

    assert _registre(hass).entrees(IDENTIFIANT_DESTINATION) == []
    assert "déjà absente" in caplog.text


def _registre(hass: HomeAssistant) -> Any:
    """Registre persistant des sauvegardes distantes."""
    return hass.data[DATA_REMOTE_BACKUPS]


def _notification_de_reauth(
    hass: HomeAssistant, destination_id: str = IDENTIFIANT_DESTINATION
) -> Any:
    """Notification persistante de ré-authentification (#17), si elle existe.

    Le problème Home Assistant (`reauth.py`) et la notification (#17,
    `notifications.py`) sont créés dans le même mouvement par
    `async_signaler_la_reauthentification()` : ce n'est donc pas une mécanique
    propre au listage ou à la suppression, mais un effet de bord partagé de
    l'appel Drive commun (`async_appel_drive_json`), qui doit se produire aussi
    bien pour un appel direct que pour un appel fait depuis la purge.
    """
    return persistent_notification._async_get_or_create_notifications(hass).get(
        identifiant_de_notification_de_reauthentification(destination_id)
    )


async def test_une_purge_sans_rien_a_supprimer_reste_silencieuse(
    hass: HomeAssistant,
    integration_backup: None,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Rien d'expiré : aucune suppression, aucun événement, aucune erreur."""
    await _entree(hass, **{CONF_RETENTION_DAYS: 30})
    faux = _FauxDrive(aioclient_mock, fichiers=[fichier_drive("recente", jours=1)])
    evenements = async_capture_events(hass, EVENT_REMOTE_PURGE)

    caplog.clear()
    with caplog.at_level(logging.ERROR):
        await hass.services.async_call(DOMAIN, SERVICE_PURGE, blocking=True)
        await hass.async_block_till_done()

    assert faux.supprimes == []
    assert evenements == []
    assert [
        enregistrement.message
        for enregistrement in caplog.records
        if enregistrement.levelno >= logging.ERROR
    ] == []


async def test_une_purge_qui_echoue_au_listage_ne_trace_pas_d_appel(
    hass: HomeAssistant,
    integration_backup: None,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Un refus de Google reste une erreur typée, journalisée en une ligne."""
    await _entree(hass, **{CONF_RETENTION_DAYS: 1})
    faux = _FauxDrive(
        aioclient_mock,
        fichiers=[fichier_drive("vieille", jours=30)],
        pannes_listage=[(403, erreur_google(403, "accessNotConfigured"))],
    )

    with caplog.at_level(logging.ERROR):
        await hass.services.async_call(DOMAIN, SERVICE_PURGE, blocking=True)
        await hass.async_block_till_done()

    assert faux.supprimes == []
    assert "listage impossible" in caplog.text
    assert "Traceback" not in caplog.text
    assert [
        enregistrement.message
        for enregistrement in caplog.records
        if enregistrement.exc_info is not None
    ] == []


async def test_une_troncature_de_pagination_n_empeche_pas_la_purge_d_aboutir(
    hass: HomeAssistant,
    integration_backup: None,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`PAGES_MAX` atteint ne met jamais la rétention en erreur.

    Le listage ne lève rien quand il tronque à `PAGES_MAX` pages : il
    journalise un avertissement et renvoie ce qu'il a lu (cf.
    `test_le_listage_borne_le_nombre_de_pages_parcourues`). La purge doit donc
    réussir sans exception ni bruit en `ERROR`, et supprimer les sauvegardes
    expirées **parmi celles qu'elle a effectivement vues** — jamais sur une
    présomption sur les sauvegardes situées au-delà de la troncature.
    """
    await _entree(hass, **{CONF_RETENTION_DAYS: 1})
    fichiers = [
        fichier_drive(f"tres-vieille-{rang}", jours=30)
        for rang in range(PAGES_MAX + 10)
    ]
    faux = _FauxDrive(aioclient_mock, fichiers=fichiers, par_page=1)
    evenements = async_capture_events(hass, EVENT_REMOTE_PURGE)

    with caplog.at_level(logging.ERROR):
        await hass.services.async_call(DOMAIN, SERVICE_PURGE, blocking=True)
        await hass.async_block_till_done()

    # Seules les PAGES_MAX premières sauvegardes ont été vues et supprimées ;
    # les 10 restantes, jamais lues, n'ont pas été touchées.
    assert len(faux.jetons_recus) == PAGES_MAX
    assert len(faux.supprimes) == PAGES_MAX
    assert len(faux.fichiers) == 10
    assert len(evenements) == 1
    assert len(evenements[0].data[ATTR_REMOTE_IDS]) == PAGES_MAX
    assert [
        enregistrement.message
        for enregistrement in caplog.records
        if enregistrement.levelno >= logging.ERROR
    ] == []


### Ré-authentification pendant une purge (issue #17) ###


async def test_un_401_au_listage_d_une_purge_notifie_la_reautorisation(
    hass: HomeAssistant,
    integration_backup: None,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Critère 5 : un 401 au listage d'une purge crée aussi la notification.

    `test_une_erreur_d_authentification_au_listage_demande_une_reautorisation`
    éprouve déjà le problème Home Assistant sur un appel direct à
    `async_list_backups()`. Ici, l'appel vient du coordinateur de purge
    (déclenché par le service `auto_backup.purge`), qui attrape lui-même la
    `DestinationAuthError` pour journaliser et passer à la destination
    suivante : le signalement de ré-authentification — et la notification
    persistante de l'issue #17 qui l'accompagne — doit donc avoir eu lieu
    *avant* que cette exception ne soit rattrapée, sans quoi une destination
    purgée en tâche de fond resterait invisible dans le panneau Notifications.
    """
    await _entree(hass, **{CONF_RETENTION_DAYS: 1})
    _FauxDrive(
        aioclient_mock,
        fichiers=[fichier_drive("vieille", jours=30)],
        pannes_listage=[(401, erreur_google(401, "authError"))],
    )

    await hass.services.async_call(DOMAIN, SERVICE_PURGE, blocking=True)
    await hass.async_block_till_done()

    registre = ir.async_get(hass)
    assert (
        registre.async_get_issue(
            DOMAIN, identifiant_du_probleme(IDENTIFIANT_DESTINATION)
        )
        is not None
    )
    assert _notification_de_reauth(hass) is not None


async def test_un_401_a_la_suppression_d_une_purge_notifie_la_reautorisation(
    hass: HomeAssistant,
    integration_backup: None,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Critère 5 : un 401 à la suppression d'une purge crée aussi la notification.

    Même raisonnement que pour le listage, mais l'échec survient cette fois à
    `files.delete`, une fois le fichier expiré retenu par la rétention : la
    purge le rattrape en `DestinationError` (`_async_supprimer`) et continue,
    mais la ré-authentification a déjà été signalée par l'appel Drive partagé.
    """
    await _entree(hass, **{CONF_RETENTION_DAYS: 1})
    _FauxDrive(
        aioclient_mock,
        fichiers=[fichier_drive("vieille", jours=30)],
        pannes_suppression={"vieille": (401, erreur_google(401, "authError"))},
    )

    await hass.services.async_call(DOMAIN, SERVICE_PURGE, blocking=True)
    await hass.async_block_till_done()

    registre = ir.async_get(hass)
    assert (
        registre.async_get_issue(
            DOMAIN, identifiant_du_probleme(IDENTIFIANT_DESTINATION)
        )
        is not None
    )
    assert _notification_de_reauth(hass) is not None


### Journaux ###


async def test_aucun_secret_n_est_journalise_meme_en_debug(
    hass: HomeAssistant,
    integration_backup: None,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Critère 7 : ni jeton, ni en-tête d'autorisation, ni adresse de compte."""
    await _entree(hass, **{CONF_RETENTION_COUNT: 1})
    _FauxDrive(
        aioclient_mock,
        fichiers=[
            fichier_drive("vieille", jours=5),
            fichier_drive("recente", jours=0),
        ],
    )
    caplog.set_level(logging.DEBUG)

    await hass.services.async_call(DOMAIN, SERVICE_PURGE, blocking=True)
    await hass.async_block_till_done()

    sensibles = [
        ACCES_FACTICE,
        jeton_google()["refresh_token"],
        CLIENT_SECRET_FACTICE,
        EMAIL_DU_COMPTE,
        "Authorization",
    ]
    fuites = [valeur for valeur in sensibles if valeur in caplog.text]
    assert not fuites, f"valeurs sensibles présentes dans les journaux : {fuites}"


async def test_chaque_appel_porte_le_jeton_d_acces(
    hass: HomeAssistant,
    entree_drive: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Listage comme suppression passent par la session OAuth2 du socle."""
    _FauxDrive(aioclient_mock, fichiers=[fichier_drive("une")])

    await _destination(hass).async_list_backups()
    await _destination(hass).async_delete_backup("une")

    for _, _, _, entetes in aioclient_mock.mock_calls:
        assert entetes["Authorization"] == f"Bearer {ACCES_FACTICE}"


async def test_le_dossier_distant_n_est_pas_cite_hors_du_niveau_debug(
    hass: HomeAssistant,
    entree_drive: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Les noms de sauvegardes de l'utilisateur ne sortent pas en `info`."""
    _FauxDrive(
        aioclient_mock,
        fichiers=[fichier_drive("une", nom="Sauvegarde très personnelle")],
    )

    with caplog.at_level(logging.INFO):
        await _destination(hass).async_list_backups()

    assert "Sauvegarde très personnelle" not in caplog.text
