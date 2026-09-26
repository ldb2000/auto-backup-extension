"""Téléversement d'une sauvegarde sur Google Drive (issue #14).

Ces tests suivent une sauvegarde depuis le flux du coordinateur jusqu'au fichier
déposé dans le Drive de l'utilisateur : création du dossier cible, mémorisation
de son identifiant, envoi resumable par fragments, reprise après un incident,
erreurs typées et absence de secret dans les journaux.

**Aucun appel réseau n'est fait et aucune valeur réelle n'est employée** : l'API
Drive est simulée par `aioclient_mock`, avec un simulateur (`_FauxDrive`) qui
respecte le protocole resumable de Google — il n'accepte un fragment que s'il
commence exactement là où le précédent s'est arrêté, et répond `308 Resume
Incomplete` avec l'en-tête `Range` tant que l'envoi n'est pas terminé.

La taille d'un fragment est ramenée à quelques centaines d'octets par la fixture
`fragments_courts` : c'est le **découpage** qui est éprouvé, pas la capacité de
la machine de test à brasser 8 Mio.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import ClientError
from homeassistant.const import (
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_NAME,
    CONF_TOKEN,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
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
    ATTR_REMOTE_ID,
    ATTR_SIZE,
    ATTR_UPLOAD_TO,
    CONF_AUTO_PURGE,
    CONF_BACKUP_TIMEOUT,
    CONF_DESTINATION_ID,
    CONF_DESTINATIONS,
    CONF_FOLDER,
    CONF_PROVIDER,
    CONF_PROVIDER_DATA,
    DATA_AUTO_BACKUP,
    DATA_DESTINATIONS,
    DOMAIN,
    EVENT_UPLOAD_FAILED,
    EVENT_UPLOAD_SUCCESSFUL,
    SERVICE_BACKUP,
)
from custom_components.auto_backup.destinations import (
    DestinationAuthError,
    DestinationError,
    DestinationManager,
    DestinationQuotaError,
    identifiant_du_probleme,
)
from custom_components.auto_backup.destinations.providers import google_drive_upload
from custom_components.auto_backup.destinations.providers.google_drive import (
    CLE_EMAIL_DU_COMPTE,
    CLE_ID_DU_DOSSIER,
    PORTEE_DRIVE_FILE,
    PROVIDER_GOOGLE_DRIVE,
    GoogleDriveDestination,
)
from custom_components.auto_backup.destinations.providers.google_drive_upload import (
    MARQUEUR_AUTO_BACKUP,
    MIME_DOSSIER,
    OCTETS_MAX_PROPRIETE,
    PROPRIETE_NOM,
    PROPRIETE_SLUG,
    TAILLE_FRAGMENT,
    UNITE_FRAGMENT,
    URL_ENVOI,
    URL_FICHIERS,
    entete_content_range,
    nom_du_fichier,
    proprietes_du_fichier,
    requete_de_dossier,
)

### Valeurs de test : toutes inventées, aucune ne désigne un compte réel. ###

CLIENT_ID_FACTICE = "identifiant-application-google-factice"
CLIENT_SECRET_FACTICE = "secret-application-google-factice"
ACCES_FACTICE = "acces-google-factice-1"
EMAIL_DU_COMPTE = "camille.martin@exemple.test"
DUREE_JETON = 3600

IDENTIFIANT_DESTINATION = "mon_drive"
DOSSIER = "Sauvegardes"
DOSSIER_IMBRIQUE = "Sauvegardes/Home Assistant"

# L'URL de session porte un identifiant d'envoi : quiconque l'obtient peut écrire
# dans le fichier en cours de dépôt. Elle ne doit apparaître dans aucun journal.
IDENTIFIANT_ENVOI = "identifiant-envoi-sensible-factice"
URL_SESSION = f"{URL_ENVOI}?uploadType=resumable&upload_id={IDENTIFIANT_ENVOI}"

ID_FICHIER = "fichier-drive-factice"
ID_DOSSIER_DISPARU = "dossier-drive-supprime"
MD5_FACTICE = "d41d8cd98f00b204e9800998ecf8427e"
CREE_LE = "2026-09-25T10:11:12.000Z"

NOM_SAUVEGARDE = "Sauvegarde du 25"
SLUG = "abc123"
NOM_ATTENDU = f"{NOM_SAUVEGARDE} [{SLUG}].tar"

# Contenu de test : trois fragments exactement avec `TAILLE_FRAGMENT_TEST`.
TAILLE_FRAGMENT_TEST = 512
CONTENU = bytes(range(256)) * 6
TAILLE_MORCEAU_FLUX = 100

_NOM_RECHERCHE = re.compile(r"name = '([^']*)'")
_PARENT_RECHERCHE = re.compile(r"'([^']*)' in parents")


def jeton_google(**surcharges: Any) -> dict[str, Any]:
    """Jeton déjà normalisé (`expires_at` renseigné), tel que persisté."""
    return {
        "access_token": ACCES_FACTICE,
        "refresh_token": "rafraichissement-google-factice-1",
        "token_type": "Bearer",
        "expires_in": DUREE_JETON,
        "expires_at": time.time() + DUREE_JETON,
        "scope": PORTEE_DRIVE_FILE,
    } | surcharges


def erreur_google(statut: int, raison: str, message: str = "erreur simulée") -> dict:
    """Corps d'erreur de l'API Google, dans sa forme habituelle."""
    return {
        "error": {
            "code": statut,
            "message": message,
            "errors": [{"domain": "global", "reason": raison, "message": message}],
        }
    }


def config_google(**surcharges: Any) -> dict[str, Any]:
    """Configuration brute d'une destination Google Drive, telle que persistée."""
    return {
        CONF_DESTINATION_ID: IDENTIFIANT_DESTINATION,
        CONF_PROVIDER: PROVIDER_GOOGLE_DRIVE,
        CONF_NAME: "Mon Drive",
        CONF_FOLDER: DOSSIER,
        CONF_CLIENT_ID: CLIENT_ID_FACTICE,
        CONF_CLIENT_SECRET: CLIENT_SECRET_FACTICE,
        CONF_TOKEN: jeton_google(),
        CONF_PROVIDER_DATA: {CLE_EMAIL_DU_COMPTE: EMAIL_DU_COMPTE},
    } | surcharges


### Simulateur de l'API Drive ###


def _analyser_content_range(plage: str) -> tuple[int | None, int | None]:
    """Décompose un en-tête `Content-Range` en (début, taille totale).

    Le début vaut `None` pour une demande d'état (`bytes */<taille>`), la taille
    totale vaut `None` quand elle n'est pas encore connue (`.../*`).
    """
    octets, _, taille = plage.removeprefix("bytes ").partition("/")
    total = None if taille == "*" else int(taille)
    if octets == "*":
        return None, total
    return int(octets.split("-")[0]), total


class _FauxDrive:
    """API Google Drive simulée : dossiers, session d'envoi et fragments reçus.

    Le simulateur applique les règles du protocole resumable : un fragment qui ne
    commence pas là où le précédent s'est arrêté est refusé, l'état de la session
    est toujours cohérent avec les octets réellement reçus, et l'envoi ne se
    termine que lorsque la taille totale est atteinte.
    """

    def __init__(
        self,
        aioclient_mock: AiohttpClientMocker,
        *,
        dossiers: dict[tuple[str, str], str] | None = None,
        pannes: list[Any] | None = None,
        pannes_recherche: list[Any] | None = None,
        pertes: set[int] | None = None,
        taille_declaree: Any = None,
        session_sans_url: bool = False,
        dossier_sans_identifiant: bool = False,
        fichier_sans_identifiant: bool = False,
        jamais_termine: bool = False,
        etat_termine: bool = False,
        n_accepte_rien: bool = False,
    ) -> None:
        """Branche le simulateur sur les quatre points d'accès utilisés.

        Une panne est soit un triplet (statut, en-têtes, corps), soit une
        exception à lever — de quoi simuler aussi bien un refus de Google qu'une
        liaison qui cède au milieu d'un fragment.
        """
        self.mock = aioclient_mock
        self.dossiers = dict(dossiers or {})
        self.pannes = list(pannes or [])
        self.pannes_recherche = list(pannes_recherche or [])
        self.pertes = set(pertes or ())
        self.taille_declaree = taille_declaree
        self.session_sans_url = session_sans_url
        self.dossier_sans_identifiant = dossier_sans_identifiant
        self.fichier_sans_identifiant = fichier_sans_identifiant
        self.jamais_termine = jamais_termine
        self.etat_termine = etat_termine
        self.n_accepte_rien = n_accepte_rien
        self.recherches: list[tuple[str, str]] = []
        self.creations: list[dict[str, Any]] = []
        self.sessions: list[dict[str, Any]] = []
        self.plages: list[str] = []
        self.recu = bytearray()
        self.nom_fichier = NOM_ATTENDU
        self.proprietes: dict[str, str] = {}

        aioclient_mock.get(URL_FICHIERS, side_effect=self._rechercher)
        aioclient_mock.post(URL_FICHIERS, side_effect=self._creer)
        aioclient_mock.post(URL_ENVOI, side_effect=self._ouvrir_la_session)
        aioclient_mock.put(URL_SESSION, side_effect=self._pousser)

    ### Réponses ###

    def _reponse(
        self,
        statut: int,
        *,
        entetes: dict[str, str] | None = None,
        corps: Any = None,
    ) -> AiohttpClientMockResponse:
        """Construit une réponse simulée."""
        return AiohttpClientMockResponse(
            "put", URL(URL_SESSION), status=statut, headers=entetes, json=corps
        )

    def _panne(self, panne: Any) -> AiohttpClientMockResponse:
        """Rejoue une panne programmée : réponse en erreur ou exception levée."""
        if isinstance(panne, BaseException):
            raise panne
        statut, entetes, corps = panne
        return self._reponse(statut, entetes=entetes, corps=corps)

    def _etat_de_la_session(self) -> AiohttpClientMockResponse:
        """`308 Resume Incomplete`, avec la plage réellement reçue."""
        if not self.recu:
            # Convention de Google : pas d'en-tête `Range` signifie « rien reçu ».
            return self._reponse(308)
        return self._reponse(308, entetes={"Range": f"bytes=0-{len(self.recu) - 1}"})

    def metadonnees_du_fichier(self) -> dict[str, Any]:
        """Métadonnées renvoyées par Drive à la fin de l'envoi."""
        taille = (
            len(self.recu) if self.taille_declaree is None else self.taille_declaree
        )
        metadonnees = {
            "id": ID_FICHIER,
            "name": self.nom_fichier,
            "size": str(taille),
            "createdTime": CREE_LE,
            "md5Checksum": MD5_FACTICE,
            "appProperties": self.proprietes,
        }
        if self.fichier_sans_identifiant:
            del metadonnees["id"]
        return metadonnees

    @property
    def entetes_du_dernier_appel(self) -> dict[str, str]:
        """En-têtes de la dernière requête reçue (le mock ne les passe pas)."""
        return self.mock.mock_calls[-1][3]

    ### Points d'accès ###

    async def _rechercher(
        self, methode: str, url: URL, donnees: Any
    ) -> AiohttpClientMockResponse:
        """`files.list` : ne renvoie que les dossiers créés par l'intégration."""
        if self.pannes_recherche:
            return self._panne(self.pannes_recherche.pop(0))
        requete = url.query["q"]
        nom = _NOM_RECHERCHE.search(requete).group(1)
        parent = _PARENT_RECHERCHE.search(requete).group(1)
        self.recherches.append((parent, nom))
        identifiant = self.dossiers.get((parent, nom))
        trouves = [{"id": identifiant, "name": nom}] if identifiant else []
        return self._reponse(200, corps={"files": trouves})

    async def _creer(
        self, methode: str, url: URL, donnees: Any
    ) -> AiohttpClientMockResponse:
        """`files.create` : crée un dossier et lui attribue un identifiant."""
        self.creations.append(dict(donnees))
        parent = (donnees.get("parents") or ["root"])[0]
        nom = donnees["name"]
        identifiant = f"dossier-{len(self.creations)}"
        self.dossiers[(parent, nom)] = identifiant
        if self.dossier_sans_identifiant:
            return self._reponse(200, corps={"name": nom})
        return self._reponse(200, corps={"id": identifiant, "name": nom})

    async def _ouvrir_la_session(
        self, methode: str, url: URL, donnees: Any
    ) -> AiohttpClientMockResponse:
        """Ouverture de la session resumable : renvoie l'URL de session."""
        parents = donnees.get("parents") or []
        if parents and parents[0] not in self.dossiers.values():
            # L'identifiant mémorisé ne désigne plus aucun dossier existant.
            return self._reponse(404, corps=erreur_google(404, "notFound"))
        self.sessions.append(dict(donnees))
        self.nom_fichier = donnees["name"]
        self.proprietes = dict(donnees.get("appProperties") or {})
        if self.session_sans_url:
            return self._reponse(200)
        return self._reponse(200, entetes={"Location": URL_SESSION})

    async def _pousser(
        self, methode: str, url: URL, donnees: Any
    ) -> AiohttpClientMockResponse:
        """Réception d'un fragment, ou demande d'état de la session."""
        plage = self.entetes_du_dernier_appel["Content-Range"]
        self.plages.append(plage)
        debut, total = _analyser_content_range(plage)
        if debut is None:
            if total == 0:
                # Sauvegarde vide : l'envoi se termine sans le moindre octet.
                return self._reponse(200, corps=self.metadonnees_du_fichier())
            if self.etat_termine:
                return self._reponse(200, corps=self.metadonnees_du_fichier())
            # Demande d'état : elle répond toujours l'état réel, même en panne.
            return self._etat_de_la_session()
        if self.pannes:
            return self._panne(self.pannes.pop(0))
        if self.n_accepte_rien:
            # 308 sans `Range` : Google prétend n'avoir rien reçu du tout.
            return self._reponse(308)
        if debut in self.pertes:
            # La session prétend avoir perdu presque tout ce qu'elle a reçu.
            return self._reponse(308, entetes={"Range": "bytes=0-9"})
        if debut != len(self.recu):
            return self._reponse(400, corps=erreur_google(400, "badRequest"))
        self.recu.extend(donnees or b"")
        if total is not None and len(self.recu) >= total and not self.jamais_termine:
            return self._reponse(200, corps=self.metadonnees_du_fichier())
        return self._etat_de_la_session()


### Fixtures ###


@pytest.fixture
def fragments_courts(monkeypatch: pytest.MonkeyPatch) -> int:
    """Ramène la taille d'un fragment à quelques centaines d'octets."""
    monkeypatch.setattr(google_drive_upload, "TAILLE_FRAGMENT", TAILLE_FRAGMENT_TEST)
    return TAILLE_FRAGMENT_TEST


@pytest.fixture
def delais(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Relève les délais d'attente entre deux tentatives, sans les subir."""
    attendus: list[float] = []

    async def _patienter(delai: float) -> None:
        attendus.append(delai)

    monkeypatch.setattr(google_drive_upload, "async_attendre_avant_reprise", _patienter)
    return attendus


@pytest.fixture
async def entree_google(
    hass: HomeAssistant, integration_backup: None
) -> MockConfigEntry:
    """Entrée portant une destination Google Drive déjà autorisée."""
    return await _entree(hass)


async def _entree(hass: HomeAssistant, **surcharges: Any) -> MockConfigEntry:
    """Initialise l'intégration avec une destination Google Drive."""
    entree = MockConfigEntry(
        domain=DOMAIN,
        title="Auto Backup",
        data={},
        options={
            CONF_AUTO_PURGE: True,
            CONF_BACKUP_TIMEOUT: 20,
            CONF_DESTINATIONS: [config_google(**surcharges)],
        },
    )
    entree.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entree.entry_id)
    await hass.async_block_till_done()
    return entree


def _gestionnaire(hass: HomeAssistant) -> DestinationManager:
    """Gestionnaire de destinations de l'entrée chargée."""
    return hass.data[DATA_DESTINATIONS]


def _destination(hass: HomeAssistant) -> GoogleDriveDestination:
    """Destination Google Drive chargée par le gestionnaire."""
    return _gestionnaire(hass).async_get(IDENTIFIANT_DESTINATION)


def _donnees_persistees(entree: MockConfigEntry) -> dict[str, Any]:
    """Données de fournisseur telles qu'écrites dans l'entrée."""
    (persistee,) = entree.options[CONF_DESTINATIONS]
    return persistee.get(CONF_PROVIDER_DATA) or {}


def _appels(aioclient_mock: AiohttpClientMocker, url: str) -> list[tuple]:
    """Appels enregistrés vers un point d'accès, quels que soient ses paramètres."""
    return [
        appel for appel in aioclient_mock.mock_calls if str(appel[1]).startswith(url)
    ]


async def _flux(
    contenu: bytes = CONTENU, morceau: int = TAILLE_MORCEAU_FLUX
) -> AsyncIterator[bytes]:
    """Flux de sauvegarde, rendu morceau par morceau comme le coordinateur."""
    for debut in range(0, len(contenu), morceau):
        yield contenu[debut : debut + morceau]


async def _televerser(
    hass: HomeAssistant, *, taille: int | None = len(CONTENU), contenu: bytes = CONTENU
) -> Any:
    """Téléverse un contenu vers la destination Google Drive chargée."""
    return await _destination(hass).async_upload(
        None,
        name=NOM_SAUVEGARDE,
        slug=SLUG,
        stream=_flux(contenu),
        size=taille,
        filename=f"{SLUG}.tar",
    )


### Constantes d'envoi ###


def test_le_fragment_est_un_multiple_de_l_unite_exigee_par_google() -> None:
    """Critère 2 : Google impose des fragments multiples de 256 Kio."""
    assert UNITE_FRAGMENT == 256 * 1024
    assert TAILLE_FRAGMENT == 8 * 1024 * 1024
    assert TAILLE_FRAGMENT % UNITE_FRAGMENT == 0


@pytest.mark.parametrize(
    ("debut", "longueur", "total", "attendu"),
    [
        (0, 512, None, "bytes 0-511/*"),
        (512, 512, 1536, "bytes 512-1023/1536"),
        (1024, 512, 1536, "bytes 1024-1535/1536"),
        (0, 0, 1536, "bytes */1536"),
        (0, 0, None, "bytes */*"),
    ],
)
def test_l_entete_content_range_suit_la_convention_de_google(
    debut: int, longueur: int, total: int | None, attendu: str
) -> None:
    """La taille totale n'est annoncée que lorsqu'elle est connue."""
    assert entete_content_range(debut, longueur, total) == attendu


@pytest.mark.parametrize(
    ("nom", "slug", "attendu"),
    [
        ("Sauvegarde du 25", "abc123", "Sauvegarde du 25 [abc123].tar"),
        # Les séparateurs de chemin ne peuvent pas rester dans un nom de fichier.
        ("Avant/après", "abc123", "Avant-après [abc123].tar"),
        ("  espaces   multiples  ", "abc123", "espaces multiples [abc123].tar"),
        ("", "abc123", "abc123 [abc123].tar"),
        ("Sauvegarde", None, "Sauvegarde.tar"),
        (".", None, "sauvegarde.tar"),
        # Un nom démesuré est tronqué : le slug reste dans le nom du fichier.
        ("N" * 300, "abc123", f"{'N' * 120} [abc123].tar"),
    ],
)
def test_le_nom_du_fichier_reste_lisible_et_unique(
    nom: str, slug: str | None, attendu: str
) -> None:
    """Critère 2 : « <nom> [<slug>].tar », assaini, jamais vide."""
    assert nom_du_fichier(nom, slug) == attendu


def test_un_nom_multi_octets_tient_dans_la_borne_en_octets_de_google() -> None:
    """Critère 3 : la borne d'une propriété privée est celle de Google, en octets.

    L'API Drive refuse une entrée de `appProperties` dont la clé et la valeur
    dépassent ensemble 124 octets. Un nom riche en caractères multi-octets
    (accents, idéogrammes, emoji) tient sous une borne comptée en caractères tout
    en dépassant celle de Google : c'est le cas construit ici.
    """
    # Nom déjà assaini : pas de caractère réservé, pas d'espace doublé, donc
    # `_assainir()` le laisse tel quel et la valeur produite en est un préfixe.
    nom = "Sauvegarde été 漢字 🗄" * 5
    # Moins de 100 caractères — l'ancienne borne, comptée en caractères, laissait
    # donc ce nom passer intact, et ses 140 octets se faisaient refuser.
    assert len(nom) < 100
    assert len(nom.encode("utf-8")) > OCTETS_MAX_PROPRIETE

    valeur = proprietes_du_fichier(nom, "abc123")[PROPRIETE_NOM]

    budget = OCTETS_MAX_PROPRIETE - len(PROPRIETE_NOM.encode("utf-8"))
    assert len(valeur.encode("utf-8")) <= budget
    # Aucun caractère coupé en deux : la valeur reste un préfixe exact du nom, et
    # ne porte pas de caractère de remplacement.
    assert nom.startswith(valeur)
    assert "�" not in valeur
    # La coupe ne rogne pas plus que nécessaire : le caractère suivant est bien
    # celui qui ne tenait plus dans le budget.
    assert len((valeur + nom[len(valeur)]).encode("utf-8")) > budget


@pytest.mark.parametrize(
    "nom",
    [
        "N" * 300,
        "é" * 300,
        "漢" * 300,
        "🗄" * 300,
        "Sauvegarde été 漢字 🗄" * 20,
        "é漢🗄" * 100,
    ],
)
def test_aucune_propriete_ne_depasse_la_borne_en_octets_de_google(nom: str) -> None:
    """Critère 3 : quelle que soit l'entrée, Google ne peut pas refuser l'appel.

    Le dépassement de la borne fait échouer tout le téléversement, pas seulement
    la propriété fautive : aucune valeur produite ne doit donc pouvoir la
    franchir, clé comprise.
    """
    proprietes = proprietes_du_fichier(nom, "sauvegarde-été-🗄" * 20)

    assert set(proprietes) == {MARQUEUR_AUTO_BACKUP, PROPRIETE_NOM, PROPRIETE_SLUG}
    for cle, valeur in proprietes.items():
        octets = len(cle.encode("utf-8")) + len(valeur.encode("utf-8"))
        assert octets <= OCTETS_MAX_PROPRIETE, f"{cle} pèse {octets} octets"
        assert "�" not in valeur


def test_une_apostrophe_dans_le_dossier_est_echappee_pour_google() -> None:
    """Sans échappement, l'apostrophe romprait la chaîne littérale de la requête.

    `chemin_de_dossier()` interdit aujourd'hui l'apostrophe dans un dossier
    saisi par le flux (issue #6) : ce cas ne peut donc pas survenir avec un
    dossier configuré aujourd'hui. `requete_de_dossier()` reste néanmoins la
    seule barrière pour un dossier persisté avant cette restriction.
    """
    requete = requete_de_dossier("Sauvegarde d'automne", "root")

    assert requete == (
        "name = 'Sauvegarde d\\'automne' and mimeType = "
        "'application/vnd.google-apps.folder' and 'root' in parents "
        "and trashed = false"
    )


### Dossier cible ###


async def test_le_dossier_cible_est_cree_au_premier_televersement(
    hass: HomeAssistant,
    entree_google: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    fragments_courts: int,
) -> None:
    """Critère 1 : le dossier est cherché, créé, puis son identifiant mémorisé."""
    faux = _FauxDrive(aioclient_mock)

    await _televerser(hass)
    await hass.async_block_till_done()

    # La recherche vise bien un dossier non supprimé de la racine, par son nom.
    assert faux.recherches == [("root", DOSSIER)]
    (_, url_recherche, _, _) = _appels(aioclient_mock, URL_FICHIERS)[0]
    requete = url_recherche.query["q"]
    assert f"name = '{DOSSIER}'" in requete
    assert f"mimeType = '{MIME_DOSSIER}'" in requete
    assert "'root' in parents" in requete
    assert "trashed = false" in requete

    # Faute de l'avoir trouvé, l'intégration le crée et le marque comme sien.
    assert faux.creations == [
        {
            "name": DOSSIER,
            "mimeType": MIME_DOSSIER,
            "parents": ["root"],
            "appProperties": {MARQUEUR_AUTO_BACKUP: "true"},
        }
    ]

    # L'identifiant est persisté à côté de l'adresse du compte, sans l'effacer.
    assert _donnees_persistees(entree_google) == {
        CLE_EMAIL_DU_COMPTE: EMAIL_DU_COMPTE,
        CLE_ID_DU_DOSSIER: "dossier-1",
    }


async def test_l_identifiant_du_dossier_memorise_est_reutilise(
    hass: HomeAssistant,
    entree_google: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    fragments_courts: int,
) -> None:
    """Critère 1 : le second téléversement ne recherche ni ne crée le dossier."""
    faux = _FauxDrive(aioclient_mock)

    await _televerser(hass)
    await hass.async_block_till_done()
    appels_dossier = len(_appels(aioclient_mock, URL_FICHIERS))

    # La destination a été recréée par l'écriture des options : c'est bien
    # l'identifiant **persisté** qui est relu, pas une mémoire d'instance.
    faux.recu.clear()
    await _televerser(hass)
    await hass.async_block_till_done()

    assert len(_appels(aioclient_mock, URL_FICHIERS)) == appels_dossier
    assert len(faux.creations) == 1
    assert faux.sessions[1]["parents"] == ["dossier-1"]


async def test_un_dossier_memorise_disparu_est_recree(
    hass: HomeAssistant,
    integration_backup: None,
    aioclient_mock: AiohttpClientMocker,
    fragments_courts: int,
) -> None:
    """Critère 1 : un identifiant devenu invalide (404) est recréé et remémoré."""
    entree = await _entree(
        hass,
        provider_data={
            CLE_EMAIL_DU_COMPTE: EMAIL_DU_COMPTE,
            CLE_ID_DU_DOSSIER: ID_DOSSIER_DISPARU,
        },
    )
    faux = _FauxDrive(aioclient_mock)

    distante = await _televerser(hass)
    await hass.async_block_till_done()

    # Le dossier disparu n'est pas retrouvé par la recherche (portée `drive.file`
    # oblige) : il est recréé, et la session repart sur le nouvel identifiant.
    assert faux.recherches == [("root", DOSSIER)]
    assert len(faux.creations) == 1
    assert faux.sessions[0]["parents"] == ["dossier-1"]
    assert _donnees_persistees(entree)[CLE_ID_DU_DOSSIER] == "dossier-1"
    assert distante.remote_id == ID_FICHIER
    assert bytes(faux.recu) == CONTENU


async def test_une_hierarchie_de_dossiers_est_creee_segment_par_segment(
    hass: HomeAssistant,
    integration_backup: None,
    aioclient_mock: AiohttpClientMocker,
    fragments_courts: int,
) -> None:
    """Critère 1 : « Sauvegardes/Home Assistant » donne deux dossiers imbriqués."""
    entree = await _entree(hass, folder=DOSSIER_IMBRIQUE)
    faux = _FauxDrive(aioclient_mock)

    distante = await _televerser(hass)
    await hass.async_block_till_done()

    assert faux.recherches == [("root", "Sauvegardes"), ("dossier-1", "Home Assistant")]
    assert [creation["parents"] for creation in faux.creations] == [
        ["root"],
        ["dossier-1"],
    ]
    # Seul le dossier **final** est mémorisé : c'est le seul dont l'envoi a besoin.
    assert _donnees_persistees(entree)[CLE_ID_DU_DOSSIER] == "dossier-2"
    assert distante.path == f"{DOSSIER_IMBRIQUE}/{NOM_ATTENDU}"


async def test_un_dossier_deja_cree_est_retrouve_sans_etre_recree(
    hass: HomeAssistant,
    entree_google: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    fragments_courts: int,
) -> None:
    """Un dossier créé lors d'une installation antérieure est réutilisé.

    Le cas se produit après une suppression puis un rajout de la destination :
    l'identifiant n'est plus mémorisé, mais le dossier, lui, existe toujours.
    """
    faux = _FauxDrive(aioclient_mock, dossiers={("root", DOSSIER): "dossier-existant"})

    await _televerser(hass)
    await hass.async_block_till_done()

    assert faux.creations == []
    assert faux.sessions[0]["parents"] == ["dossier-existant"]


async def test_un_segment_de_dossier_avec_une_apostrophe_est_echappe_sur_le_reseau(
    hass: HomeAssistant,
    entree_google: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """La requête `q` envoyée à Google échappe l'apostrophe du nom de dossier.

    `chemin_de_dossier()` interdit aujourd'hui l'apostrophe dans un dossier
    saisi par le flux (issue #6) : ce cas ne peut pas survenir avec un dossier
    configuré aujourd'hui. Il reste couvert ici en appelant directement
    `async_resoudre_le_dossier()`, seule barrière pour un dossier persisté
    avant cette restriction.
    """
    faux = _FauxDrive(aioclient_mock)
    session = _destination(hass).session

    identifiant = await google_drive_upload.async_resoudre_le_dossier(
        session, "Sauvegarde d'automne"
    )

    (_, url_recherche, _, _) = _appels(aioclient_mock, URL_FICHIERS)[0]
    requete = url_recherche.query["q"]
    assert "name = 'Sauvegarde d\\'automne'" in requete
    # Le dossier est bien créé sous son nom réel, apostrophe comprise.
    assert faux.creations[0]["name"] == "Sauvegarde d'automne"
    assert identifiant == "dossier-1"


### Envoi resumable ###


async def test_l_envoi_resumable_decoupe_la_sauvegarde_en_fragments(
    hass: HomeAssistant,
    entree_google: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    fragments_courts: int,
) -> None:
    """Critère 2 : fragments successifs, offsets continus, contenu intact."""
    faux = _FauxDrive(aioclient_mock)

    distante = await _televerser(hass)
    await hass.async_block_till_done()

    assert faux.plages == [
        "bytes 0-511/1536",
        "bytes 512-1023/1536",
        "bytes 1024-1535/1536",
    ]
    assert bytes(faux.recu) == CONTENU
    assert distante.size == len(CONTENU)

    # Aucun fragment n'a dépassé la taille prévue : la mémoire est bornée.
    tailles = [
        len(appel[2] or b"")
        for appel in _appels(aioclient_mock, URL_ENVOI)
        if appel[0].lower() == "put"
    ]
    assert max(tailles) <= TAILLE_FRAGMENT_TEST


async def test_une_taille_inconnue_est_annoncee_par_une_etoile(
    hass: HomeAssistant,
    entree_google: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    fragments_courts: int,
) -> None:
    """Critère 2 : sans `Content-Length`, la taille n'arrive qu'au dernier fragment."""
    faux = _FauxDrive(aioclient_mock)

    distante = await _televerser(hass, taille=None)
    await hass.async_block_till_done()

    assert faux.plages == [
        "bytes 0-511/*",
        "bytes 512-1023/*",
        "bytes 1024-1535/1536",
    ]
    assert bytes(faux.recu) == CONTENU
    assert distante.size == len(CONTENU)
    # Sans taille annoncée, l'ouverture de session n'en déclare pas non plus.
    (_, _, _, entetes) = _appels(aioclient_mock, URL_ENVOI)[0]
    assert "X-Upload-Content-Length" not in entetes


async def test_une_sauvegarde_plus_courte_qu_un_fragment_part_en_une_fois(
    hass: HomeAssistant,
    entree_google: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    fragments_courts: int,
) -> None:
    """Le cas courant : un seul envoi, qui annonce d'emblée la taille totale."""
    faux = _FauxDrive(aioclient_mock)
    contenu = b"petite sauvegarde"

    distante = await _televerser(hass, contenu=contenu, taille=len(contenu))

    assert faux.plages == [f"bytes 0-{len(contenu) - 1}/{len(contenu)}"]
    assert bytes(faux.recu) == contenu
    assert distante.size == len(contenu)


async def test_le_fichier_porte_le_marqueur_auto_backup_et_le_slug(
    hass: HomeAssistant,
    entree_google: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    fragments_courts: int,
) -> None:
    """Critère 3 : le listage (#15) et la purge (#9) sauront le reconnaître."""
    faux = _FauxDrive(aioclient_mock)

    distante = await _televerser(hass)
    await hass.async_block_till_done()

    (session,) = faux.sessions
    assert session["name"] == NOM_ATTENDU
    assert session["appProperties"] == {
        MARQUEUR_AUTO_BACKUP: "true",
        "name": NOM_SAUVEGARDE,
        "slug": SLUG,
    }
    # Les champs demandés à Google sont ceux dont la sauvegarde distante a besoin.
    (_, url_envoi, _, _) = _appels(aioclient_mock, URL_ENVOI)[0]
    assert url_envoi.query["uploadType"] == "resumable"
    assert url_envoi.query["fields"] == (
        "id,name,size,createdTime,md5Checksum,appProperties"
    )

    assert distante.remote_id == ID_FICHIER
    assert distante.name == NOM_ATTENDU
    assert distante.slug == SLUG
    assert distante.path == f"{DOSSIER}/{NOM_ATTENDU}"
    assert distante.created_at is not None
    assert distante.created_at.isoformat() == "2026-09-25T10:11:12+00:00"
    assert distante.metadata == {
        "slug": SLUG,
        "md5Checksum": MD5_FACTICE,
        MARQUEUR_AUTO_BACKUP: True,
    }


async def test_chaque_appel_porte_le_jeton_d_acces(
    hass: HomeAssistant,
    entree_google: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    fragments_courts: int,
) -> None:
    """Tous les appels — recherche, création, session, fragments — sont authentifiés."""
    _FauxDrive(aioclient_mock)

    await _televerser(hass)
    await hass.async_block_till_done()

    for _, _, _, entetes in aioclient_mock.mock_calls:
        assert entetes["Authorization"] == f"Bearer {ACCES_FACTICE}"


async def test_la_taille_finale_est_verifiee(
    hass: HomeAssistant,
    entree_google: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    fragments_courts: int,
) -> None:
    """Critère 2 : un fichier tronqué chez Google n'est jamais déclaré valide."""
    _FauxDrive(aioclient_mock, taille_declaree=12)

    with pytest.raises(DestinationError) as erreur:
        await _televerser(hass)

    assert "incomplet" in str(erreur.value)


### Reprises et erreurs ###


async def test_une_erreur_serveur_reprend_la_session_a_l_offset_annonce(
    hass: HomeAssistant,
    entree_google: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    fragments_courts: int,
    delais: list[float],
) -> None:
    """Critère 4 : 5xx, délai croissant, reprise à l'offset annoncé par Google."""
    faux = _FauxDrive(
        aioclient_mock, pannes=[(500, {}, erreur_google(500, "backendError"))]
    )

    distante = await _televerser(hass)
    await hass.async_block_till_done()

    # Le premier fragment échoue, l'état de la session est redemandé, puis le
    # fragment repart de l'offset annoncé (zéro octet reçu).
    assert faux.plages[:4] == [
        "bytes 0-511/1536",
        "bytes */1536",
        "bytes 0-511/1536",
        "bytes 512-1023/1536",
    ]
    assert delais == [1.0]
    assert bytes(faux.recu) == CONTENU
    assert distante.remote_id == ID_FICHIER


async def test_une_limitation_de_debit_est_reessayee_puis_reussit(
    hass: HomeAssistant,
    entree_google: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    fragments_courts: int,
    delais: list[float],
) -> None:
    """Critère 4 : `rateLimitExceeded` n'est pas un échec, c'est une attente."""
    faux = _FauxDrive(
        aioclient_mock,
        pannes=[
            (429, {"Retry-After": "5"}, erreur_google(429, "rateLimitExceeded")),
            (403, {}, erreur_google(403, "userRateLimitExceeded")),
        ],
    )

    distante = await _televerser(hass)
    await hass.async_block_till_done()

    # Le délai demandé par Google l'emporte sur le calcul exponentiel.
    assert delais == [5.0, 2.0]
    assert bytes(faux.recu) == CONTENU
    assert distante.remote_id == ID_FICHIER


async def test_les_tentatives_sont_bornees(
    hass: HomeAssistant,
    entree_google: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    fragments_courts: int,
    delais: list[float],
) -> None:
    """Critère 4 : trois tentatives, délai plafonné, puis un message clair."""
    _FauxDrive(
        aioclient_mock,
        pannes=[(503, {}, erreur_google(503, "backendError"))] * 3,
    )

    with pytest.raises(DestinationError) as erreur:
        await _televerser(hass)

    assert delais == [1.0, 2.0]
    assert "HTTP 503" in str(erreur.value)
    assert not isinstance(erreur.value, DestinationAuthError)


async def test_une_coupure_reseau_est_reessayee_puis_expliquee(
    hass: HomeAssistant,
    entree_google: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    fragments_courts: int,
    delais: list[float],
) -> None:
    """Une liaison qui cède ne remonte pas brute au coordinateur."""
    aioclient_mock.get(URL_FICHIERS, exc=ClientError("panne réseau"))

    with pytest.raises(DestinationError) as erreur:
        await _televerser(hass)

    assert "injoignable" in str(erreur.value)
    assert delais == [1.0, 2.0]


async def test_le_quota_de_stockage_depasse_devient_une_erreur_typee(
    hass: HomeAssistant,
    entree_google: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    fragments_courts: int,
    delais: list[float],
) -> None:
    """Critère 4 : `storageQuotaExceeded` est définitif, aucune reprise."""
    _FauxDrive(
        aioclient_mock,
        pannes=[(403, {}, erreur_google(403, "storageQuotaExceeded"))],
    )

    with pytest.raises(DestinationQuotaError) as erreur:
        await _televerser(hass)

    assert "espace de stockage" in str(erreur.value)
    assert delais == []


async def test_un_acces_revoque_pendant_l_envoi_demande_une_reautorisation(
    hass: HomeAssistant,
    entree_google: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    fragments_courts: int,
    delais: list[float],
) -> None:
    """Critère 4 : un 401 lève `DestinationAuthError` et crée le problème HA."""
    _FauxDrive(aioclient_mock, pannes=[(401, {}, erreur_google(401, "authError"))])

    with pytest.raises(DestinationAuthError):
        await _televerser(hass)
    await hass.async_block_till_done()

    assert delais == []
    assert _gestionnaire(hass).reauthentification_requise(IDENTIFIANT_DESTINATION)
    assert (
        ir.async_get(hass).async_get_issue(
            DOMAIN, identifiant_du_probleme(IDENTIFIANT_DESTINATION)
        )
        is not None
    )


async def test_une_session_qui_perd_des_octets_est_abandonnee(
    hass: HomeAssistant,
    entree_google: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    fragments_courts: int,
) -> None:
    """Un fragment déjà libéré ne peut pas être renvoyé : mieux vaut échouer.

    Google annonce moins d'octets reçus que ce qui lui avait déjà été confirmé :
    poursuivre écrirait une archive trouée, l'envoi s'arrête donc net.
    """
    _FauxDrive(aioclient_mock, pertes={TAILLE_FRAGMENT_TEST})

    with pytest.raises(DestinationError) as erreur:
        await _televerser(hass)

    assert "perdu des octets" in str(erreur.value)


async def test_une_coupure_au_milieu_d_un_fragment_est_reprise(
    hass: HomeAssistant,
    entree_google: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    fragments_courts: int,
    delais: list[float],
) -> None:
    """Critère 4 : un fragment interrompu repart de l'offset annoncé par Google."""
    faux = _FauxDrive(aioclient_mock, pannes=[TimeoutError()])

    distante = await _televerser(hass)
    await hass.async_block_till_done()

    assert delais == [1.0]
    assert bytes(faux.recu) == CONTENU
    assert distante.remote_id == ID_FICHIER


async def test_un_delai_demande_illisible_retombe_sur_le_calcul(
    hass: HomeAssistant,
    entree_google: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    fragments_courts: int,
    delais: list[float],
) -> None:
    """Un `Retry-After` au format date n'empêche pas de patienter."""
    _FauxDrive(
        aioclient_mock,
        pannes=[
            (
                429,
                {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"},
                erreur_google(429, "rateLimitExceeded"),
            )
        ],
    )

    await _televerser(hass)
    await hass.async_block_till_done()

    assert delais == [1.0]


async def test_un_echec_passager_de_la_recherche_de_dossier_est_reessaye(
    hass: HomeAssistant,
    entree_google: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    fragments_courts: int,
    delais: list[float],
) -> None:
    """Critère 4 : la reprise vaut aussi pour les appels au dossier cible."""
    faux = _FauxDrive(
        aioclient_mock,
        pannes_recherche=[(500, {}, erreur_google(500, "backendError"))],
    )

    distante = await _televerser(hass)
    await hass.async_block_till_done()

    assert delais == [1.0]
    assert len(faux.recherches) == 1
    assert distante.remote_id == ID_FICHIER


async def test_une_session_d_envoi_sans_url_est_signalee(
    hass: HomeAssistant,
    entree_google: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    fragments_courts: int,
) -> None:
    """Sans en-tête « Location », il n'y a nulle part où pousser les fragments."""
    _FauxDrive(aioclient_mock, session_sans_url=True)

    with pytest.raises(DestinationError) as erreur:
        await _televerser(hass)

    assert "Location" in str(erreur.value)


async def test_une_taille_annoncee_trop_petite_interrompt_l_envoi(
    hass: HomeAssistant,
    entree_google: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    fragments_courts: int,
) -> None:
    """Google clôt la session dès la taille annoncée atteinte : l'envoi échoue.

    Plutôt que de laisser une archive tronquée passer pour une sauvegarde
    valide, l'erreur est explicite — la rétention ne doit jamais s'appuyer
    dessus pour supprimer la copie locale.
    """
    _FauxDrive(aioclient_mock)

    with pytest.raises(DestinationError) as erreur:
        await _televerser(hass, taille=TAILLE_FRAGMENT_TEST)

    assert "taille annoncée" in str(erreur.value)


async def test_une_taille_annoncee_trop_grande_est_corrigee_au_dernier_fragment(
    hass: HomeAssistant,
    entree_google: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    fragments_courts: int,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """C'est la taille réellement lue qui fait foi, et elle est signalée."""
    faux = _FauxDrive(aioclient_mock)

    distante = await _televerser(hass, taille=len(CONTENU) + 500)
    await hass.async_block_till_done()

    assert faux.plages[-1] == f"bytes 1024-1535/{len(CONTENU)}"
    assert bytes(faux.recu) == CONTENU
    assert distante.size == len(CONTENU)
    assert "octets alors que" in caplog.text


async def test_une_taille_illisible_n_empeche_pas_de_conclure(
    hass: HomeAssistant,
    entree_google: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    fragments_courts: int,
) -> None:
    """Une taille non numérique renvoyée par Drive est ignorée, pas fatale."""
    _FauxDrive(aioclient_mock, taille_declaree="inconnue")

    distante = await _televerser(hass)

    assert distante.size == len(CONTENU)


async def test_un_fichier_sans_identifiant_est_refuse(
    hass: HomeAssistant,
    entree_google: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    fragments_courts: int,
) -> None:
    """Sans identifiant distant, la sauvegarde ne serait ni listable ni purgeable."""
    _FauxDrive(aioclient_mock, fichier_sans_identifiant=True)

    with pytest.raises(DestinationError) as erreur:
        await _televerser(hass)

    assert "identifiant" in str(erreur.value)


async def test_une_session_qui_n_accepte_plus_rien_est_abandonnee(
    hass: HomeAssistant,
    entree_google: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    fragments_courts: int,
    delais: list[float],
) -> None:
    """Une session qui n'avance plus ne doit pas faire boucler le téléversement."""
    _FauxDrive(aioclient_mock, n_accepte_rien=True)

    with pytest.raises(DestinationError) as erreur:
        await _televerser(hass)

    assert "n'accepte plus d'octets" in str(erreur.value)
    assert delais == [1.0, 2.0]


async def test_une_session_declaree_terminee_trop_tot_est_signalee(
    hass: HomeAssistant,
    entree_google: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    fragments_courts: int,
    delais: list[float],
) -> None:
    """La reprise refuse de conclure sur une session incohérente."""
    _FauxDrive(
        aioclient_mock,
        pannes=[(500, {}, erreur_google(500, "backendError"))],
        etat_termine=True,
    )

    with pytest.raises(DestinationError) as erreur:
        await _televerser(hass)

    assert "considère l'envoi terminé" in str(erreur.value)


async def test_l_attente_entre_deux_tentatives_est_reelle() -> None:
    """Le point d'observation des tests attend bien, sans rien faire d'autre."""
    await google_drive_upload.async_attendre_avant_reprise(0)


async def test_un_dossier_cree_sans_identifiant_est_signale(
    hass: HomeAssistant,
    entree_google: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    fragments_courts: int,
) -> None:
    """Sans identifiant de dossier, il n'y a nulle part où déposer le fichier."""
    _FauxDrive(aioclient_mock, dossier_sans_identifiant=True)

    with pytest.raises(DestinationError) as erreur:
        await _televerser(hass)

    assert "dossier" in str(erreur.value)


async def test_un_envoi_jamais_confirme_est_signale(
    hass: HomeAssistant,
    entree_google: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    fragments_courts: int,
) -> None:
    """Sans métadonnées finales, la sauvegarde distante serait inconnaissable."""
    _FauxDrive(aioclient_mock, jamais_termine=True)

    with pytest.raises(DestinationError) as erreur:
        await _televerser(hass)

    assert "confirmé la fin" in str(erreur.value)


async def test_des_coupures_repetees_finissent_par_echouer(
    hass: HomeAssistant,
    entree_google: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    fragments_courts: int,
    delais: list[float],
) -> None:
    """Critère 4 : les reprises sont bornées, y compris pour le réseau."""
    _FauxDrive(aioclient_mock, pannes=[TimeoutError()] * 3)

    with pytest.raises(DestinationError) as erreur:
        await _televerser(hass)

    assert "n'a pas répondu" in str(erreur.value)
    assert delais == [1.0, 2.0]


async def test_deux_envois_de_suite_ne_resolvent_le_dossier_qu_une_fois(
    hass: HomeAssistant,
    entree_google: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    fragments_courts: int,
) -> None:
    """La destination garde l'identifiant du dossier pour ses envois suivants."""
    faux = _FauxDrive(aioclient_mock)
    destination = _destination(hass)

    await destination.async_upload(
        None, name=NOM_SAUVEGARDE, slug=SLUG, stream=_flux(), size=len(CONTENU)
    )
    faux.recu.clear()
    await destination.async_upload(
        None, name=NOM_SAUVEGARDE, slug="def456", stream=_flux(), size=len(CONTENU)
    )
    await hass.async_block_till_done()

    assert len(faux.recherches) == 1
    assert len(faux.creations) == 1
    assert destination.folder_id == "dossier-1"
    assert bytes(faux.recu) == CONTENU


async def test_un_dossier_non_persistable_n_interrompt_pas_l_envoi(
    hass: HomeAssistant,
    entree_google: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    fragments_courts: int,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Une destination supprimée pendant l'envoi : la sauvegarde part quand même.

    L'identifiant du dossier n'est qu'une optimisation : ne pas pouvoir l'écrire
    ne justifie pas de perdre la sauvegarde en cours de transfert.
    """
    faux = _FauxDrive(aioclient_mock)
    destination = _destination(hass)
    hass.config_entries.async_update_entry(
        entree_google, options={**entree_google.options, CONF_DESTINATIONS: []}
    )
    await hass.async_block_till_done()

    distante = await destination.async_upload(
        None, name=NOM_SAUVEGARDE, slug=SLUG, stream=_flux(), size=len(CONTENU)
    )

    assert distante.remote_id == ID_FICHIER
    assert bytes(faux.recu) == CONTENU
    assert "non persisté" in caplog.text


async def test_un_televersement_sans_flux_est_refuse(
    hass: HomeAssistant, entree_google: MockConfigEntry
) -> None:
    """Le fournisseur consomme un flux : sans lui, il le dit clairement."""
    with pytest.raises(DestinationError) as erreur:
        await _destination(hass).async_upload(Path("/backup/ha.tar"), name="ha")

    assert "flux" in str(erreur.value)


### Journaux ###


async def test_aucun_secret_n_est_journalise_meme_en_debug(
    hass: HomeAssistant,
    entree_google: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    fragments_courts: int,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Critère 7 : ni jeton, ni en-tête d'autorisation, ni URL de session."""
    _FauxDrive(aioclient_mock)
    caplog.set_level(logging.DEBUG)

    await _televerser(hass)
    await hass.async_block_till_done()

    sensibles = [
        ACCES_FACTICE,
        jeton_google()["refresh_token"],
        CLIENT_SECRET_FACTICE,
        IDENTIFIANT_ENVOI,
        URL_SESSION,
        EMAIL_DU_COMPTE,
        "Authorization",
    ]
    fuites = [valeur for valeur in sensibles if valeur in caplog.text]
    assert not fuites, f"valeurs sensibles présentes dans les journaux : {fuites}"


### Intégration : du service à l'événement ###


def _faux_backup_manager(chemin: Path) -> MagicMock:
    """`BackupManager` minimal : une sauvegarde locale et son agent."""
    sauvegarde = MagicMock()
    sauvegarde.backup_id = SLUG
    agent = MagicMock()
    agent.get_backup_path = MagicMock(return_value=chemin)
    manager = MagicMock()
    manager.async_get_backup = AsyncMock(return_value=(sauvegarde, {}))
    manager.local_backup_agents = {"backup.local": agent}
    return manager


@pytest.fixture
def sauvegarde_locale(tmp_path: Path) -> Path:
    """Fichier `.tar` local tenant lieu de sauvegarde Home Assistant."""
    chemin = tmp_path / f"{SLUG}.tar"
    chemin.write_bytes(CONTENU)
    return chemin


@pytest.fixture
def integration_prete(
    hass: HomeAssistant, entree_google: MockConfigEntry, sauvegarde_locale: Path
) -> Iterator[None]:
    """Remplace la création de sauvegarde par une création simulée."""
    handler = hass.data[DATA_AUTO_BACKUP]._handler
    handler._manager = _faux_backup_manager(sauvegarde_locale)
    handler.create_backup = AsyncMock(return_value={"slug": SLUG})
    yield None


async def test_le_service_de_sauvegarde_televerse_vers_google_drive(
    hass: HomeAssistant,
    entree_google: MockConfigEntry,
    integration_prete: None,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Critères 2 et 5 : `upload_to` dépose la sauvegarde et publie son identifiant.

    C'est le parcours réel : appel de service, création de la sauvegarde,
    téléversement en tâche de fond, puis événement `auto_backup.upload_successful`.
    """
    faux = _FauxDrive(aioclient_mock)
    succes = async_capture_events(hass, EVENT_UPLOAD_SUCCESSFUL)
    echecs = async_capture_events(hass, EVENT_UPLOAD_FAILED)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_BACKUP,
        {"name": NOM_SAUVEGARDE, ATTR_UPLOAD_TO: IDENTIFIANT_DESTINATION},
        blocking=True,
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    assert not echecs, [evenement.data for evenement in echecs]
    assert len(succes) == 1
    assert succes[0].data[ATTR_REMOTE_ID] == ID_FICHIER
    assert succes[0].data[ATTR_SIZE] == len(CONTENU)
    assert bytes(faux.recu) == CONTENU
    assert faux.sessions[0]["appProperties"]["slug"] == SLUG
