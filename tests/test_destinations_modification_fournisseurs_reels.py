"""Changement de dossier distant et fournisseurs réels (issue #51).

`tests/test_destinations_modification.py` prouve, avec un fournisseur factice
qui déclare une donnée liée au dossier, que le flux d'options oublie cette
donnée (`CLES_LIEES_AU_DOSSIER`) quand le dossier distant change. Ces tests
vont un cran plus loin avec les **deux fournisseurs réellement livrés** par le
fork, pour prouver ce que l'avertissement affiché à l'utilisateur (critère 5)
promet concrètement :

- **Google Drive** : le téléversement qui suit un changement de dossier ne
  peut pas réutiliser l'identifiant de l'ancien dossier (`folder_id`, oublié
  par la modification) — il cherche, ou crée, le nouveau, et ne mémorise que
  le sien ;
- **Dropbox**, qui ne mémorise aucun identifiant de dossier, dépose au chemin
  du dossier **configuré** : le dépôt qui suit un changement de dossier part
  donc directement dans le nouveau chemin ;
- une **purge distante** qui suit un changement de dossier ne liste — et ne
  peut donc supprimer — que le nouveau dossier chez Google Drive : l'ancien
  dossier, et tout ce qu'il contient, reste hors de portée d'Auto Backup.

Aucune valeur réelle n'est employée : les appels HTTP sont simulés par
`aioclient_mock`, comme dans `tests/test_provider_google_drive_upload.py` et
`tests/test_provider_dropbox_upload.py`, dont les fixtures et utilitaires
publics sont réutilisés plutôt que dupliqués.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)
from yarl import URL

from custom_components.auto_backup.const import (
    CONF_AUTO_PURGE,
    CONF_BACKUP_TIMEOUT,
    CONF_DESTINATION_ID,
    CONF_DESTINATIONS,
    CONF_FOLDER,
    CONF_PROVIDER_DATA,
    CONF_RETENTION_DAYS,
    DATA_DESTINATIONS,
    DATA_REMOTE_PURGE,
    DEFAULT_BACKUP_TIMEOUT,
    DOMAIN,
)
from custom_components.auto_backup.destinations.flow import CONF_CONFIRMER
from custom_components.auto_backup.destinations.providers.dropbox import (
    URL_ENVOI as URL_ENVOI_DROPBOX,
)
from custom_components.auto_backup.destinations.providers.google_drive import (
    CLE_EMAIL_DU_COMPTE,
    CLE_ID_DU_DOSSIER,
)
from custom_components.auto_backup.destinations.providers.google_drive_upload import (
    MARQUEUR_AUTO_BACKUP,
    MIME_SAUVEGARDE,
    URL_ENVOI,
    URL_FICHIERS,
    VRAI_DRIVE,
)
from test_provider_dropbox_upload import (
    DESTINATION_ID as DESTINATION_ID_DROPBOX,
)
from test_provider_dropbox_upload import (
    DOSSIER as DOSSIER_DROPBOX,
)
from test_provider_dropbox_upload import (  # noqa: F401 - fixtures réutilisées
    NOM_DESTINATION,
    NOM_FICHIER,
    appels,
    argument,
    entree_dropbox,
    instance_joignable,
    simuler_l_envoi,
    simuler_le_dossier,
    televerser,
)
from test_provider_google_drive_upload import (
    CONTENU as CONTENU_GOOGLE,
)
from test_provider_google_drive_upload import (
    DOSSIER as DOSSIER_GOOGLE,
)
from test_provider_google_drive_upload import (
    EMAIL_DU_COMPTE,
    IDENTIFIANT_DESTINATION,
    NOM_ATTENDU,
    NOM_SAUVEGARDE,
    SLUG,
    config_google,
)

type OuvrirLesOptions = Callable[[str, str], Awaitable[dict[str, Any]]]

# Identifiant de la session d'envoi Google Drive simulée. Fixe : les tests ne
# déposent jamais deux fichiers en même temps, un seul suffit — comme dans
# `test_provider_google_drive_upload.py`.
URL_SESSION_GOOGLE = f"{URL_ENVOI}?uploadType=resumable&upload_id=faux-session-51"

_NOM_RECHERCHE = re.compile(r"name = '([^']*)'")
_PARENT_RECHERCHE = re.compile(r"'([^']*)' in parents")


### Simulateur Google Drive « multi-dossiers » ###


class _FauxDriveDossier:
    """API Google Drive simulée, avec des fichiers rattachés à leur dossier.

    Les simulateurs des autres modules ne modélisent qu'un seul dossier cible
    à la fois : suffisant pour prouver qu'un téléversement ou une purge y
    fonctionne, pas pour prouver qu'un changement de dossier (#51) ne les fait
    plus jamais atteindre l'**ancien**. Celui-ci associe donc chaque fichier
    déposé — ou pré-existant — à l'identifiant du dossier qui le contient.
    """

    def __init__(
        self,
        aioclient_mock: AiohttpClientMocker,
        *,
        dossiers: dict[tuple[str, str], str] | None = None,
        fichiers: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self.dossiers = dict(dossiers or {})
        self.fichiers: dict[str, list[dict[str, Any]]] = {
            cle: list(valeur) for cle, valeur in (fichiers or {}).items()
        }
        self.recherches: list[tuple[str, str]] = []
        self.creations: list[dict[str, Any]] = []
        self.listages: list[str] = []
        self.supprimes: list[str] = []
        self._dossier_ouvert: str | None = None
        self._nom_ouvert: str | None = None
        self._depots = 0

        aioclient_mock.get(URL_FICHIERS, side_effect=self._get)
        aioclient_mock.post(URL_FICHIERS, side_effect=self._creer)
        aioclient_mock.post(URL_ENVOI, side_effect=self._ouvrir_la_session)
        aioclient_mock.put(URL_SESSION_GOOGLE, side_effect=self._deposer)
        aioclient_mock.delete(
            re.compile(rf"^{re.escape(URL_FICHIERS)}/"), side_effect=self._supprimer
        )

    ### `files.list` : recherche d'un sous-dossier, ou listage d'un dossier ###

    async def _get(
        self, methode: str, url: URL, donnees: Any
    ) -> AiohttpClientMockResponse:
        requete = url.query["q"]
        if "appProperties has" in requete:
            return await self._lister(methode, url, requete)
        return await self._rechercher(methode, url, requete)

    async def _rechercher(
        self, methode: str, url: URL, requete: str
    ) -> AiohttpClientMockResponse:
        nom = _NOM_RECHERCHE.search(requete).group(1)
        parent = _PARENT_RECHERCHE.search(requete).group(1)
        self.recherches.append((parent, nom))
        identifiant = self.dossiers.get((parent, nom))
        trouves = [{"id": identifiant, "name": nom}] if identifiant else []
        return AiohttpClientMockResponse(
            methode, url, status=200, json={"files": trouves}
        )

    async def _lister(
        self, methode: str, url: URL, requete: str
    ) -> AiohttpClientMockResponse:
        parent = _PARENT_RECHERCHE.search(requete).group(1)
        self.listages.append(parent)
        return AiohttpClientMockResponse(
            methode,
            url,
            status=200,
            json={"files": list(self.fichiers.get(parent, []))},
        )

    ### `files.create` : crée un dossier et lui attribue un identifiant ###

    async def _creer(
        self, methode: str, url: URL, donnees: Any
    ) -> AiohttpClientMockResponse:
        self.creations.append(dict(donnees))
        parent = (donnees.get("parents") or ["root"])[0]
        nom = donnees["name"]
        identifiant = f"dossier-cree-{len(self.creations)}"
        self.dossiers[(parent, nom)] = identifiant
        self.fichiers.setdefault(identifiant, [])
        return AiohttpClientMockResponse(
            methode, url, status=200, json={"id": identifiant, "name": nom}
        )

    ### Session d'envoi : ouverture, puis dépôt en un seul fragment ###

    async def _ouvrir_la_session(
        self, methode: str, url: URL, donnees: Any
    ) -> AiohttpClientMockResponse:
        self._dossier_ouvert = (donnees.get("parents") or [None])[0]
        self._nom_ouvert = donnees["name"]
        return AiohttpClientMockResponse(
            methode, url, status=200, headers={"Location": URL_SESSION_GOOGLE}
        )

    async def _deposer(
        self, methode: str, url: URL, donnees: Any
    ) -> AiohttpClientMockResponse:
        self._depots += 1
        fichier = {
            "id": f"fichier-{self._depots}",
            "name": self._nom_ouvert,
            "size": str(len(donnees or b"")),
            "createdTime": "2026-09-27T10:00:00.000Z",
            "mimeType": MIME_SAUVEGARDE,
            "trashed": False,
            "appProperties": {MARQUEUR_AUTO_BACKUP: VRAI_DRIVE},
        }
        self.fichiers.setdefault(self._dossier_ouvert, []).append(fichier)
        return AiohttpClientMockResponse(methode, url, status=200, json=fichier)

    ### `files.delete` ###

    async def _supprimer(
        self, methode: str, url: URL, donnees: Any
    ) -> AiohttpClientMockResponse:
        identifiant = url.path.rsplit("/", 1)[-1]
        self.supprimes.append(identifiant)
        for fichiers_du_dossier in self.fichiers.values():
            fichiers_du_dossier[:] = [
                fichier
                for fichier in fichiers_du_dossier
                if fichier.get("id") != identifiant
            ]
        return AiohttpClientMockResponse(methode, url, status=204, text="")


### Aides Google Drive ###


async def _entree_google(hass: HomeAssistant, **surcharges: Any) -> MockConfigEntry:
    """Initialise l'intégration avec une destination Google Drive."""
    entree = MockConfigEntry(
        domain=DOMAIN,
        title="Auto Backup",
        data={},
        options={
            CONF_AUTO_PURGE: True,
            CONF_BACKUP_TIMEOUT: DEFAULT_BACKUP_TIMEOUT,
            CONF_DESTINATIONS: [config_google(**surcharges)],
        },
    )
    entree.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entree.entry_id)
    await hass.async_block_till_done()
    return entree


def _destination_google(hass: HomeAssistant) -> Any:
    """Destination Google Drive chargée par le gestionnaire."""
    return hass.data[DATA_DESTINATIONS].async_get(IDENTIFIANT_DESTINATION)


def _donnees_persistees_google(entree: MockConfigEntry) -> dict[str, Any]:
    """Données de fournisseur telles qu'écrites dans l'entrée."""
    (persistee,) = entree.options[CONF_DESTINATIONS]
    return persistee.get(CONF_PROVIDER_DATA) or {}


async def _flux_google(contenu: bytes) -> AsyncIterator[bytes]:
    yield contenu


async def _televerser_google(hass: HomeAssistant) -> Any:
    """Téléverse une sauvegarde vers la destination Google Drive chargée."""
    return await _destination_google(hass).async_upload(
        None,
        name=NOM_SAUVEGARDE,
        slug=SLUG,
        stream=_flux_google(CONTENU_GOOGLE),
        size=len(CONTENU_GOOGLE),
        filename=f"{SLUG}.tar",
    )


async def _modifier_le_dossier(
    hass: HomeAssistant,
    entree: MockConfigEntry,
    ouvrir_les_options: OuvrirLesOptions,
    *,
    destination_id: str,
    nom: str,
    nouveau_dossier: str,
    retention_days: int | None = None,
) -> None:
    """Change le dossier distant d'une destination, confirmation comprise."""
    resultat = await ouvrir_les_options(entree.entry_id, "modifier_destination")
    resultat = await hass.config_entries.options.async_configure(
        resultat["flow_id"], {CONF_DESTINATION_ID: destination_id}
    )
    assert resultat["type"] is FlowResultType.FORM
    saisie: dict[str, Any] = {
        CONF_NAME: nom,
        CONF_FOLDER: nouveau_dossier,
        CONF_AUTO_PURGE: True,
    }
    if retention_days is not None:
        saisie[CONF_RETENTION_DAYS] = retention_days
    resultat = await hass.config_entries.options.async_configure(
        resultat["flow_id"], saisie
    )
    assert resultat["type"] is FlowResultType.FORM
    assert resultat["step_id"] == "confirmer_changement_de_dossier"

    resultat = await hass.config_entries.options.async_configure(
        resultat["flow_id"], {CONF_CONFIRMER: True}
    )
    assert resultat["type"] is FlowResultType.CREATE_ENTRY


### Google Drive : le téléversement suivant cherche, ou crée, le nouveau dossier ###


async def test_google_drive_le_televersement_suivant_utilise_le_nouveau_dossier(
    hass: HomeAssistant,
    integration_backup: None,
    aioclient_mock: AiohttpClientMocker,
    ouvrir_les_options: OuvrirLesOptions,
) -> None:
    """Après un changement de dossier, le dépôt suivant vise le nouveau.

    `folder_id` — la donnée que Google Drive déclare liée au dossier (#51,
    `CLES_LIEES_AU_DOSSIER`) — est oubliée par la modification : le
    téléversement qui suit ne peut donc plus réutiliser l'ancien identifiant.
    Il cherche, puis crée, le nouveau dossier, et ne mémorise que le sien.
    """
    ancien_id = "dossier-ancien"
    entree = await _entree_google(
        hass,
        **{
            CONF_PROVIDER_DATA: {
                CLE_EMAIL_DU_COMPTE: EMAIL_DU_COMPTE,
                CLE_ID_DU_DOSSIER: ancien_id,
            }
        },
    )
    faux = _FauxDriveDossier(
        aioclient_mock, dossiers={("root", DOSSIER_GOOGLE): ancien_id}
    )

    await _modifier_le_dossier(
        hass,
        entree,
        ouvrir_les_options,
        destination_id=IDENTIFIANT_DESTINATION,
        nom="Mon Drive",
        nouveau_dossier="Archives",
    )
    assert CLE_ID_DU_DOSSIER not in _donnees_persistees_google(entree)

    distante = await _televerser_google(hass)
    await hass.async_block_till_done()

    # Le nouveau dossier est cherché, faute d'être trouvé il est créé : jamais
    # l'ancien dossier n'est ni cherché, ni réutilisé.
    assert faux.recherches == [("root", "Archives")]
    assert [creation["name"] for creation in faux.creations] == ["Archives"]
    nouveau_id = faux.dossiers[("root", "Archives")]
    assert nouveau_id != ancien_id

    # Seul l'identifiant du nouveau dossier est mémorisé, à côté du compte.
    assert _donnees_persistees_google(entree) == {
        CLE_EMAIL_DU_COMPTE: EMAIL_DU_COMPTE,
        CLE_ID_DU_DOSSIER: nouveau_id,
    }
    assert distante.path == f"Archives/{NOM_ATTENDU}"


### Google Drive : une purge qui suit ne touche plus l'ancien dossier ###


async def test_google_drive_la_purge_apres_changement_de_dossier_ignore_l_ancien(
    hass: HomeAssistant,
    integration_backup: None,
    aioclient_mock: AiohttpClientMocker,
    ouvrir_les_options: OuvrirLesOptions,
) -> None:
    """Une purge qui suit un changement de dossier ne liste que le nouveau.

    Un fichier expiré, resté dans l'**ancien** dossier, n'est plus jamais listé
    une fois le dossier changé : Auto Backup ne peut donc plus le supprimer,
    faute de le chercher au bon endroit. C'est la contrepartie, côté purge, de
    l'avertissement affiché à l'utilisateur avant de valider (critère 5) : les
    sauvegardes de l'ancien dossier restent hors de sa portée.
    """
    ancien_id = "dossier-ancien"
    fichier_expire = {
        "id": "fichier-expire",
        "name": "Sauvegarde expirée",
        "size": "1024",
        "createdTime": "2020-01-01T00:00:00.000Z",
        "mimeType": MIME_SAUVEGARDE,
        "trashed": False,
        "appProperties": {MARQUEUR_AUTO_BACKUP: VRAI_DRIVE},
    }
    entree = await _entree_google(
        hass,
        **{
            CONF_RETENTION_DAYS: 7,
            CONF_PROVIDER_DATA: {
                CLE_EMAIL_DU_COMPTE: EMAIL_DU_COMPTE,
                CLE_ID_DU_DOSSIER: ancien_id,
            },
        },
    )
    faux = _FauxDriveDossier(
        aioclient_mock,
        dossiers={("root", DOSSIER_GOOGLE): ancien_id},
        fichiers={ancien_id: [fichier_expire]},
    )

    await _modifier_le_dossier(
        hass,
        entree,
        ouvrir_les_options,
        destination_id=IDENTIFIANT_DESTINATION,
        nom="Mon Drive",
        nouveau_dossier="Archives",
        retention_days=7,
    )

    supprimes = await hass.data[DATA_REMOTE_PURGE].async_purger_destination(
        IDENTIFIANT_DESTINATION
    )

    assert supprimes == []
    assert faux.supprimes == []
    # Le fichier expiré est toujours là, intact, dans son dossier d'origine.
    assert faux.fichiers[ancien_id] == [fichier_expire]
    # Seul le nouveau dossier (vide, fraîchement créé) a été listé.
    nouveau_id = faux.dossiers[("root", "Archives")]
    assert faux.listages == [nouveau_id]


### Dropbox : le dépôt suivant part dans le nouveau chemin ###


async def test_dropbox_le_depot_suivant_va_dans_le_nouveau_chemin(
    hass: HomeAssistant,
    entree_dropbox: MockConfigEntry,  # noqa: F811 - fixture importée, requise sous ce nom
    ouvrir_les_options: OuvrirLesOptions,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Le dépôt qui suit un changement de dossier Dropbox vise le nouveau chemin.

    Dropbox ne mémorise aucun identifiant de dossier —
    `cles_liees_au_dossier("dropbox")` renvoie un ensemble vide — le chemin
    distant est recalculé, à chaque dépôt, depuis le dossier **configuré**.
    C'est justement ce que la modification (#51) change : le dépôt suivant
    doit donc viser le nouveau dossier, sans qu'aucun état ne pointe plus vers
    l'ancien.
    """
    simuler_le_dossier(aioclient_mock)
    recu = simuler_l_envoi(aioclient_mock)
    ancienne = hass.data[DATA_DESTINATIONS].async_get(DESTINATION_ID_DROPBOX)
    await televerser(ancienne)

    await _modifier_le_dossier(
        hass,
        entree_dropbox,
        ouvrir_les_options,
        destination_id=DESTINATION_ID_DROPBOX,
        nom=NOM_DESTINATION,
        nouveau_dossier="Archives",
    )

    nouvelle = hass.data[DATA_DESTINATIONS].async_get(DESTINATION_ID_DROPBOX)
    assert nouvelle is not ancienne
    await televerser(nouvelle)

    # C'est le chemin **envoyé à Dropbox** (`path`, dans l'argument de la
    # requête) qui prouve le dépôt réel : la réponse simulée par
    # `simuler_l_envoi()` est fixe, quel que soit le chemin demandé, et ne
    # peut donc pas servir cette preuve.
    envois = appels(aioclient_mock, URL_ENVOI_DROPBOX)
    assert len(envois) == 2
    assert argument(envois[0])["path"] == f"/{DOSSIER_DROPBOX}/{NOM_FICHIER}"
    assert argument(envois[1])["path"] == f"/Archives/{NOM_FICHIER}"
    assert len(recu) == 2
