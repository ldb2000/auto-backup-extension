"""Téléversement d'une sauvegarde vers ses destinations distantes (issue #8).

Ce module orchestre tout ce qui se passe **après** la création d'une sauvegarde
locale, sans rien changer à la façon dont l'upstream la crée :

1. l'appel de service (`auto_backup.backup`, `backup_full`, `backup_partial`)
   passe par `async_prepare_upload()`, qui valide l'option `upload_to` **avant**
   la création et enregistre une demande de téléversement ;
2. la création reste intégralement celle de l'upstream ; elle émet
   `auto_backup.backup_start`, qui **confirme** la demande, puis, à son succès,
   `auto_backup.backup_successful` ;
3. `CoordinateurTeleversement` écoute ces deux événements, retrouve la demande
   correspondante et lance le téléversement en **tâche de fond**.

Pourquoi passer par l'événement plutôt que par un appel direct : brancher un
crochet dans `AutoBackup._async_create_backup()` exigerait de modifier des
lignes du code importé de l'upstream, ce que le fork s'interdit
(cf. `docs/UPSTREAM.md`). L'événement upstream porte déjà le nom et le slug de
la sauvegarde créée : il suffit de corréler.

La corrélation se fait **par le nom de la sauvegarde** : quand l'appel de
service n'en fournit pas, `async_prepare_upload()` calcule celui que l'upstream
aurait généré (`AutoBackup.generate_backup_name()`) et le pose dans les données
de l'appel. `validate_backup_config()` ne le remplace alors plus : le nom est
connu des deux côtés, sans changement de comportement. Les deux événements
upstream portent ce même nom — `backup_start` le lit dans les données de
l'appel, `backup_successful` le reprend du résultat ou, à défaut, de ces mêmes
données.

Le contenu de la sauvegarde est lu **en flux** : rien n'est chargé en mémoire,
ni copié sur le disque au passage. Les deux handlers upstream sont couverts,
par `isinstance` et sans modifier `handlers.py` :

- `SupervisorHandler` : téléchargement HTTP en streaming depuis l'API Supervisor
  (`/backups/<slug>/download`), morceau par morceau ;
- `BackupHandler` : ouverture du fichier de l'agent de sauvegarde local avec
  `aiofiles`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from time import monotonic
from typing import Any

import aiofiles
import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_NAME
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from slugify import slugify

from ..const import (
    ATTR_DESTINATION,
    ATTR_DESTINATION_NAME,
    ATTR_ERROR,
    ATTR_REMOTE_ID,
    ATTR_SIZE,
    ATTR_SLUG,
    ATTR_UPLOAD_TO,
    CONF_UPLOAD_TIMEOUT,
    DATA_AUTO_BACKUP,
    DATA_DESTINATIONS,
    DATA_UPLOADS,
    DEFAULT_UPLOAD_TIMEOUT,
    EVENT_BACKUP_START,
    EVENT_BACKUP_SUCCESSFUL,
    EVENT_UPLOAD_FAILED,
    EVENT_UPLOAD_START,
    EVENT_UPLOAD_SUCCESSFUL,
)
from ..handlers import BackupHandler, HandlerBase, SupervisorHandler
from .destination import RemoteDestination
from .errors import DestinationError

_LOGGER = logging.getLogger(__name__)

# Même taille de morceau que `handlers.CHUNK_SIZE` : la sauvegarde traverse le
# processus par tranches de 64 Kio, jamais d'un bloc.
TAILLE_MORCEAU = 64 * 1024

# Délai laissé à `auto_backup.backup_start` pour confirmer une demande. Il est
# émis dans le même appel de service, au plus un aller-retour `get_addons()`
# après l'enregistrement : quelques secondes suffisent largement. Passé ce
# délai, une demande que la création n'a jamais réclamée est oubliée, au lieu
# de rester à attendre une sauvegarde homonyme qui n'a rien à voir.
DELAI_CONFIRMATION_DEMANDE = 30.0


class ErreurLectureSauvegarde(HomeAssistantError):
    """La sauvegarde locale n'a pas pu être lue pour être téléversée.

    Distincte de `DestinationError` : l'échec vient d'ici (Supervisor
    injoignable, fichier absent), pas du fournisseur distant. La sauvegarde
    locale reste intacte dans les deux cas.
    """


@dataclass(frozen=True, slots=True)
class ContenuSauvegarde:
    """Contenu d'une sauvegarde locale, prêt à partir en flux.

    - `flux` : itérateur asynchrone de morceaux d'octets, à consommer une seule
      fois et uniquement pendant la durée du contexte qui l'a ouvert ;
    - `taille` : taille totale en octets, ou `None` si la source ne l'annonce
      pas (en-tête `Content-Length` absent côté Supervisor) ;
    - `nom_fichier` : nom d'archive proposé à la destination ;
    - `chemin` : chemin local du fichier quand il en existe un (installation
      Core), `None` quand la sauvegarde n'existe que derrière l'API Supervisor.
    """

    flux: AsyncIterator[bytes]
    taille: int | None
    nom_fichier: str
    chemin: Path | None = None


def nom_de_fichier_sauvegarde(nom: str | None, slug: str) -> str:
    """Nom d'archive d'une sauvegarde, selon la convention upstream.

    Identique à celle de `AutoBackup.async_download_backup()` : le nom est
    transformé en nom de fichier sûr, avec `.tar` en suffixe. Une sauvegarde
    déposée chez un fournisseur porte ainsi le même nom que si elle avait été
    copiée dans un `download_path`.
    """
    fichier = slugify(nom, lowercase=False, separator="_") if nom else ""
    if not fichier:
        fichier = slug
    if not fichier.endswith(".tar"):
        fichier += ".tar"
    return fichier


def _taille_annoncee(reponse: aiohttp.ClientResponse) -> int | None:
    """Taille annoncée par l'en-tête `Content-Length`, si elle est exploitable."""
    brut = reponse.headers.get(aiohttp.hdrs.CONTENT_LENGTH)
    if brut is None:
        return None
    try:
        taille = int(brut)
    except (TypeError, ValueError) as err:
        _LOGGER.debug("En-tête Content-Length inexploitable (%r) : %s", brut, err)
        return None
    return taille if taille >= 0 else None


async def _morceaux_du_fichier(fichier: Any) -> AsyncIterator[bytes]:
    """Itère un fichier ouvert par `aiofiles`, morceau par morceau."""
    while True:
        morceau = await fichier.read(TAILLE_MORCEAU)
        if not morceau:
            return
        yield morceau


@asynccontextmanager
async def _async_ouvrir_via_supervisor(
    handler: SupervisorHandler, slug: str, *, nom: str | None
) -> AsyncIterator[ContenuSauvegarde]:
    """Ouvre le téléchargement en streaming de l'API Supervisor.

    Les attributs du handler sont lus directement : `handlers.py` vient de
    l'upstream et n'est pas modifié par le fork (cf. `docs/UPSTREAM.md`), et sa
    méthode `download_backup()` écrit obligatoirement dans un fichier — ce que
    l'on veut justement éviter ici.
    """
    commande = f"/backups/{slug}/download"
    url = f"http://{handler._ip}{commande}"
    try:
        reponse = await handler._session.get(
            url,
            headers=handler._headers,
            # Le délai global est tenu par `asyncio.timeout()` côté appelant :
            # le délai total par défaut d'aiohttp couperait un gros transfert.
            timeout=aiohttp.ClientTimeout(total=None),
        )
    except aiohttp.ClientError as err:
        raise ErreurLectureSauvegarde(
            f"téléchargement de la sauvegarde « {slug} » impossible : {err}"
        ) from err

    try:
        if reponse.status != HTTPStatus.OK:
            raise ErreurLectureSauvegarde(
                f"le Supervisor a répondu {reponse.status} au téléchargement de "
                f"la sauvegarde « {slug} »"
            )
        yield ContenuSauvegarde(
            flux=reponse.content.iter_chunked(TAILLE_MORCEAU),
            taille=_taille_annoncee(reponse),
            nom_fichier=nom_de_fichier_sauvegarde(nom, slug),
        )
    finally:
        reponse.release()


@asynccontextmanager
async def _async_ouvrir_via_backup_manager(
    hass: HomeAssistant, handler: BackupHandler, slug: str, *, nom: str | None
) -> AsyncIterator[ContenuSauvegarde]:
    """Ouvre le fichier de l'agent de sauvegarde local d'une instance Core."""
    manager = handler._manager
    sauvegarde, _erreurs = await manager.async_get_backup(slug)
    if sauvegarde is None:
        raise ErreurLectureSauvegarde(f"sauvegarde locale « {slug} » introuvable")

    agents = manager.local_backup_agents
    if not agents:
        raise ErreurLectureSauvegarde(
            "aucun agent de sauvegarde local : le fichier de la sauvegarde "
            f"« {slug} » est inaccessible"
        )
    agent = agents[next(iter(agents))]
    chemin = Path(agent.get_backup_path(sauvegarde.backup_id))

    try:
        taille = await hass.async_add_executor_job(lambda: chemin.stat().st_size)
        fichier = await aiofiles.open(chemin, "rb")
    except OSError as err:
        raise ErreurLectureSauvegarde(
            f"lecture du fichier de la sauvegarde « {slug} » impossible : {err}"
        ) from err

    try:
        yield ContenuSauvegarde(
            flux=_morceaux_du_fichier(fichier),
            taille=taille,
            nom_fichier=nom_de_fichier_sauvegarde(nom, slug),
            chemin=chemin,
        )
    finally:
        await fichier.close()


@asynccontextmanager
async def async_ouvrir_sauvegarde(
    hass: HomeAssistant, handler: HandlerBase, slug: str, *, nom: str | None = None
) -> AsyncIterator[ContenuSauvegarde]:
    """Ouvre le contenu d'une sauvegarde locale, quel que soit le handler.

    Le flux obtenu n'est valable que dans le contexte : à la sortie, la réponse
    HTTP est relâchée ou le fichier refermé. Lève `ErreurLectureSauvegarde` si
    la sauvegarde est introuvable ou illisible.
    """
    if isinstance(handler, SupervisorHandler):
        async with _async_ouvrir_via_supervisor(handler, slug, nom=nom) as contenu:
            yield contenu
    elif isinstance(handler, BackupHandler):
        async with _async_ouvrir_via_backup_manager(
            hass, handler, slug, nom=nom
        ) as contenu:
            yield contenu
    else:
        raise ErreurLectureSauvegarde(
            f"handler de sauvegarde inconnu : {type(handler).__name__}"
        )


@dataclass(eq=False, slots=True)
class DemandeTeleversement:
    """Téléversement demandé par un appel de service, en attente de sa sauvegarde.

    `nom` est le nom de la sauvegarde attendue : c'est la clé de corrélation
    avec les événements `auto_backup.backup_start` puis `backup_successful`.

    `confirmee` passe à `True` quand `auto_backup.backup_start` annonce une
    sauvegarde portant ce nom, c'est-à-dire quand la création a réellement
    commencé. Seule une demande confirmée peut donner lieu à un téléversement :
    une demande qu'aucune création n'a réclamée (configuration refusée par
    `validate_backup_config()`, par exemple) expire sans jamais pouvoir être
    récupérée par une sauvegarde homonyme ultérieure.
    """

    nom: str
    destinations: tuple[str, ...]
    expire_a: float
    confirmee: bool = False


class CoordinateurTeleversement:
    """Fait le lien entre les appels de service et les téléversements réels.

    Une instance par entrée de configuration, exposée dans
    `hass.data[DATA_UPLOADS]`. Elle ne détient aucun état persistant : une
    demande vit le temps d'une création de sauvegarde.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Prépare un coordinateur sans demande en attente."""
        self._hass = hass
        self._entry = entry
        self._demandes: list[DemandeTeleversement] = []

    @callback
    def async_setup(self) -> None:
        """Écoute les créations de sauvegarde jusqu'au déchargement de l'entrée."""
        for evenement, ecouteur in (
            (EVENT_BACKUP_START, self._async_sauvegarde_demarree),
            (EVENT_BACKUP_SUCCESSFUL, self._async_sauvegarde_creee),
        ):
            self._entry.async_on_unload(
                self._hass.bus.async_listen(evenement, ecouteur)
            )

    @property
    def demandes_en_attente(self) -> list[DemandeTeleversement]:
        """Demandes enregistrées et pas encore consommées (diagnostic, tests)."""
        return list(self._demandes)

    @callback
    def async_enregistrer(
        self, nom: str, destinations: Sequence[str]
    ) -> DemandeTeleversement:
        """Enregistre une demande pour la prochaine sauvegarde nommée `nom`."""
        self._purger_les_demandes_expirees()
        demande = DemandeTeleversement(
            nom=nom,
            destinations=tuple(destinations),
            expire_a=monotonic() + DELAI_CONFIRMATION_DEMANDE,
        )
        self._demandes.append(demande)
        return demande

    @callback
    def async_oublier(self, demande: DemandeTeleversement) -> None:
        """Retire une demande restée en attente (création échouée ou refusée)."""
        for index, candidate in enumerate(self._demandes):
            if candidate is demande:
                del self._demandes[index]
                return

    def _purger_les_demandes_expirees(self) -> None:
        """Oublie les demandes qu'aucune création n'est venue confirmer.

        Une demande confirmée n'expire pas : la création qu'elle accompagne a
        son propre délai (`backup_timeout`) et se terminera, en succès comme en
        échec.
        """
        maintenant = monotonic()
        self._demandes = [
            demande
            for demande in self._demandes
            if demande.confirmee or demande.expire_a > maintenant
        ]

    @callback
    def _async_sauvegarde_demarree(self, event: Event) -> None:
        """Confirme la demande correspondant à la sauvegarde qui démarre."""
        self._purger_les_demandes_expirees()
        nom = event.data.get(ATTR_NAME)
        if nom is None:
            return
        for demande in self._demandes:
            if demande.nom == nom and not demande.confirmee:
                demande.confirmee = True
                return

    @callback
    def _async_reclamer(self, nom: str | None) -> DemandeTeleversement | None:
        """Retire et renvoie la demande confirmée portant ce nom de sauvegarde."""
        self._purger_les_demandes_expirees()
        if nom is None:
            return None
        for index, demande in enumerate(self._demandes):
            if demande.nom == nom and demande.confirmee:
                del self._demandes[index]
                return demande
        return None

    @callback
    def _async_sauvegarde_creee(self, event: Event) -> None:
        """Lance le téléversement de fond quand une sauvegarde attendue est prête."""
        nom = event.data.get(ATTR_NAME)
        demande = self._async_reclamer(nom)
        if demande is None:
            # Aucune destination n'a été demandée : comportement upstream strict.
            return

        slug = event.data.get(ATTR_SLUG)
        if not slug:
            _LOGGER.error(
                "Téléversement impossible : la sauvegarde « %s » n'a pas de slug",
                nom,
            )
            return

        self._entry.async_create_background_task(
            self._hass,
            self._async_televerser(demande, nom, slug),
            name=f"auto_backup téléversement {slug}",
        )

    @property
    def _delai(self) -> float:
        """Délai maximum d'un téléversement, en secondes (option de l'entrée)."""
        valeur = self._entry.options.get(CONF_UPLOAD_TIMEOUT, DEFAULT_UPLOAD_TIMEOUT)
        try:
            delai = float(valeur)
        except (TypeError, ValueError) as err:
            _LOGGER.warning(
                "Option « %s » inexploitable (%s) : délai par défaut de %s s retenu",
                CONF_UPLOAD_TIMEOUT,
                err,
                DEFAULT_UPLOAD_TIMEOUT,
            )
            return float(DEFAULT_UPLOAD_TIMEOUT)
        return delai if delai > 0 else float(DEFAULT_UPLOAD_TIMEOUT)

    async def _async_televerser(
        self, demande: DemandeTeleversement, nom: str, slug: str
    ) -> None:
        """Téléverse la sauvegarde vers chaque destination, l'une après l'autre.

        Le traitement est séquentiel : la sauvegarde est relue pour chaque
        destination, ce qui évite de dupliquer le flux en mémoire. L'échec de
        l'une n'interrompt jamais les suivantes.
        """
        for destination_id in demande.destinations:
            await self._async_televerser_vers(destination_id, nom, slug)

    async def _async_televerser_vers(
        self, destination_id: str, nom: str, slug: str
    ) -> None:
        """Téléverse la sauvegarde vers une destination et émet ses événements."""
        destination = self._destination(destination_id)
        if destination is None:
            self._async_signaler_echec(
                destination_id,
                destination_id,
                nom,
                slug,
                f"destination « {destination_id} » introuvable, elle a pu être "
                "supprimée depuis l'appel du service",
            )
            return

        delai = self._delai
        base = {
            ATTR_NAME: nom,
            ATTR_SLUG: slug,
            ATTR_DESTINATION: destination_id,
            ATTR_DESTINATION_NAME: destination.name,
        }
        self._hass.bus.async_fire(EVENT_UPLOAD_START, dict(base))
        _LOGGER.info(
            "Téléversement de la sauvegarde « %s » (%s) vers « %s »",
            nom,
            slug,
            destination.name,
        )

        try:
            async with asyncio.timeout(delai):
                distante, taille = await self._async_envoyer(destination, nom, slug)
        except TimeoutError:
            self._async_signaler_echec(
                destination_id,
                destination.name,
                nom,
                slug,
                f"délai de téléversement dépassé ({delai:g} s)",
            )
        except (ErreurLectureSauvegarde, DestinationError) as err:
            self._async_signaler_echec(
                destination_id, destination.name, nom, slug, str(err) or repr(err)
            )
        except Exception as err:  # l'échec d'une destination n'en bloque aucune autre
            _LOGGER.exception(
                "Erreur inattendue pendant le téléversement de « %s » vers « %s »",
                nom,
                destination.name,
            )
            self._async_signaler_echec(
                destination_id, destination.name, nom, slug, str(err) or repr(err)
            )
        else:
            taille_envoyee = distante.size if distante.size is not None else taille
            _LOGGER.info(
                "Sauvegarde « %s » (%s) téléversée vers « %s »",
                nom,
                slug,
                destination.name,
            )
            self._hass.bus.async_fire(
                EVENT_UPLOAD_SUCCESSFUL,
                {
                    **base,
                    ATTR_SIZE: taille_envoyee,
                    ATTR_REMOTE_ID: distante.remote_id,
                },
            )

    async def _async_envoyer(
        self, destination: RemoteDestination, nom: str, slug: str
    ) -> tuple[Any, int | None]:
        """Ouvre le contenu de la sauvegarde et le confie à la destination."""
        handler = self._handler()
        async with async_ouvrir_sauvegarde(
            self._hass, handler, slug, nom=nom
        ) as contenu:
            distante = await destination.async_upload(
                contenu.chemin,
                name=nom,
                slug=slug,
                stream=contenu.flux,
                size=contenu.taille,
                filename=contenu.nom_fichier,
            )
        return distante, contenu.taille

    def _handler(self) -> HandlerBase:
        """Handler de sauvegarde en service, choisi par l'upstream au démarrage."""
        auto_backup = self._hass.data.get(DATA_AUTO_BACKUP)
        if auto_backup is None:
            raise ErreurLectureSauvegarde("l'intégration Auto Backup n'est pas chargée")
        return auto_backup._handler

    def _destination(self, destination_id: str) -> RemoteDestination | None:
        """Destination portant cet identifiant, ou `None` si elle a disparu."""
        gestionnaire = self._hass.data.get(DATA_DESTINATIONS)
        if gestionnaire is None:
            return None
        try:
            return gestionnaire.async_get(destination_id)
        except DestinationError:
            return None

    @callback
    def _async_signaler_echec(
        self,
        destination_id: str,
        destination_nom: str,
        nom: str,
        slug: str,
        message: str,
    ) -> None:
        """Journalise l'échec d'un téléversement et émet l'événement dédié."""
        _LOGGER.error(
            "Échec du téléversement de la sauvegarde « %s » (%s) vers « %s » : %s",
            nom,
            slug,
            destination_nom,
            message,
        )
        self._hass.bus.async_fire(
            EVENT_UPLOAD_FAILED,
            {
                ATTR_NAME: nom,
                ATTR_SLUG: slug,
                ATTR_DESTINATION: destination_id,
                ATTR_DESTINATION_NAME: destination_nom,
                ATTR_ERROR: message,
            },
        )


@callback
def async_setup_upload(
    hass: HomeAssistant, entry: ConfigEntry
) -> CoordinateurTeleversement:
    """Installe le coordinateur de téléversement pour une entrée de configuration."""
    coordinateur = CoordinateurTeleversement(hass, entry)
    coordinateur.async_setup()
    hass.data[DATA_UPLOADS] = coordinateur

    @callback
    def retirer_le_coordinateur() -> None:
        """Retire le coordinateur de `hass.data` au déchargement de l'entrée."""
        hass.data.pop(DATA_UPLOADS, None)

    entry.async_on_unload(retirer_le_coordinateur)
    return coordinateur


@callback
def async_resoudre_destinations(
    hass: HomeAssistant, demandees: Iterable[str]
) -> tuple[str, ...]:
    """Traduit les valeurs d'`upload_to` en identifiants de destination.

    Chaque valeur est un identifiant ou un nom de destination. Lève
    `ServiceValidationError` — message en français, présenté tel quel à
    l'utilisateur — si une valeur ne désigne aucune destination, ou si un nom
    est porté par plusieurs d'entre elles.
    """
    gestionnaire = hass.data.get(DATA_DESTINATIONS)
    disponibles: list[RemoteDestination] = (
        list(gestionnaire) if gestionnaire is not None else []
    )
    identifiants = [
        _resoudre_une_destination(demandee, disponibles) for demandee in demandees
    ]
    # `dict.fromkeys` dédoublonne sans perdre l'ordre demandé : téléverser deux
    # fois vers la même destination n'aurait aucun sens.
    return tuple(dict.fromkeys(identifiants))


def _resoudre_une_destination(
    demandee: str, disponibles: list[RemoteDestination]
) -> str:
    """Renvoie l'identifiant de la destination désignée par `demandee`."""
    recherche = demandee.strip()
    for destination in disponibles:
        if destination.destination_id == recherche:
            return destination.destination_id

    homonymes = [
        destination
        for destination in disponibles
        if destination.name.casefold() == recherche.casefold()
    ]
    if len(homonymes) == 1:
        return homonymes[0].destination_id
    if len(homonymes) > 1:
        identifiants = ", ".join(
            sorted(destination.destination_id for destination in homonymes)
        )
        raise ServiceValidationError(
            f"Plusieurs destinations portent le nom « {recherche} » : précisez "
            f"son identifiant ({identifiants})."
        )

    raise ServiceValidationError(
        f"Destination inconnue : « {demandee} ». {_libelle_disponibles(disponibles)}"
    )


def _libelle_disponibles(disponibles: list[RemoteDestination]) -> str:
    """Phrase listant les destinations configurées, pour un message d'erreur."""
    if not disponibles:
        return "Aucune destination distante n'est configurée."
    listage = ", ".join(
        f"« {destination.name} » ({destination.destination_id})"
        for destination in disponibles
    )
    return f"Destinations configurées : {listage}."


@callback
def async_prepare_upload(
    hass: HomeAssistant, data: dict[str, Any]
) -> DemandeTeleversement | None:
    """Valide `upload_to` et enregistre la demande, avant toute création.

    Retire toujours `upload_to` des données de l'appel : l'upstream transmet
    l'intégralité de ce dictionnaire au Supervisor ou au `BackupManager`.
    Renvoie `None` quand aucune destination n'est demandée — le déroulement est
    alors exactement celui de l'upstream.
    """
    demandees = data.pop(ATTR_UPLOAD_TO, None)
    if not demandees:
        return None
    if isinstance(demandees, str):
        # Le schéma du service applique `cv.ensure_list` ; ce repli protège les
        # appels directs, pour qu'une chaîne ne soit pas itérée caractère
        # par caractère.
        demandees = [demandees]

    coordinateur = hass.data.get(DATA_UPLOADS)
    if coordinateur is None:
        raise ServiceValidationError(
            "Le téléversement distant est indisponible : l'entrée Auto Backup "
            "n'est pas chargée."
        )

    identifiants = async_resoudre_destinations(hass, demandees)

    nom = data.get(ATTR_NAME)
    if not nom:
        # L'upstream nommerait la sauvegarde au même endroit et de la même
        # façon (`validate_backup_config`) ; le faire ici rend le nom connu dès
        # maintenant, donc la corrélation possible, sans rien changer d'autre.
        nom = _nom_de_sauvegarde_par_defaut(hass)
        data[ATTR_NAME] = nom

    return coordinateur.async_enregistrer(nom, identifiants)


@callback
def async_release_upload(
    hass: HomeAssistant, demande: DemandeTeleversement | None
) -> None:
    """Oublie une demande que la création de sauvegarde n'a pas honorée."""
    if demande is None:
        return
    coordinateur = hass.data.get(DATA_UPLOADS)
    if coordinateur is not None:
        coordinateur.async_oublier(demande)


def _nom_de_sauvegarde_par_defaut(hass: HomeAssistant) -> str:
    """Nom que l'upstream donnerait à une sauvegarde sans nom explicite."""
    auto_backup = hass.data.get(DATA_AUTO_BACKUP)
    if auto_backup is None:
        raise ServiceValidationError(
            "Le téléversement distant est indisponible : l'entrée Auto Backup "
            "n'est pas chargée."
        )
    return auto_backup.generate_backup_name()
