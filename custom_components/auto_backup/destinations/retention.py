"""Rétention et purge des sauvegardes distantes (issue #9).

Ce module applique, **destination par destination**, la rétention configurée par
l'utilisateur (`retention_days`, `retention_count`) aux sauvegardes déposées chez
le fournisseur. Il est le pendant distant de la rétention locale de l'upstream
(`keep_days` et son registre d'expiration `snapshots_expiry`), dont il ne touche
pas une ligne : `manager.py` n'est pas modifié.

Deux garanties structurent tout le module :

1. **On ne supprime que ce que l'on a soi-même déposé.** Une sauvegarde distante
   n'est candidate à la purge que si elle est inscrite au registre persistant du
   fork (`auto_backup.remote_backups`, alimenté à chaque
   `auto_backup.upload_successful`) **ou** si elle porte le marqueur
   `auto_backup` dans ses métadonnées — marqueur que les fournisseurs réels
   posent au téléversement (issues #12 et #15). Tout le reste du dossier distant
   — un fichier que l'utilisateur y a déposé lui-même, une sauvegarde d'un autre
   outil — est invisible pour la purge, quelles que soient sa date et sa taille.
   Les deux voies se complètent : le registre survit à un dossier partagé par
   plusieurs instances Home Assistant, le marqueur survit à la perte du registre.

2. **Un échec ne fait jamais dérailler le cycle.** Une destination en attente de
   ré-autorisation est sautée sans appel réseau (cf. `docs/adr/0001`), un listage
   impossible n'empêche pas les autres destinations d'être purgées, et une
   suppression qui échoue est journalisée puis dépassée. Une sauvegarde déjà
   absente chez le fournisseur (`DestinationNotFoundError`) est traitée comme
   purgée : l'entrée correspondante quitte le registre.

La purge se déclenche à deux moments :

- après chaque téléversement réussi, si l'option upstream `auto_purge` est active
  — c'est la même option qui commande la purge locale après une création ;
- à chaque appel du service `auto_backup.purge`, pour **toutes** les destinations
  configurées. Le service upstream est enveloppé, pas remplacé : voir
  `async_setup_remote_purge()`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_NAME
from homeassistant.core import Event, HomeAssistant, ServiceCall, callback
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from ..const import (
    ATTR_CREATED_AT,
    ATTR_DESTINATION,
    ATTR_DESTINATION_NAME,
    ATTR_REMOTE_ID,
    ATTR_REMOTE_IDS,
    ATTR_SIZE,
    ATTR_SLUG,
    CONF_AUTO_PURGE,
    DATA_DESTINATIONS,
    DATA_REMOTE_BACKUPS,
    DATA_REMOTE_PURGE,
    DOMAIN,
    EVENT_REMOTE_PURGE,
    EVENT_UPLOAD_SUCCESSFUL,
    SERVICE_PURGE,
    STORAGE_KEY_REMOTE_BACKUPS,
    STORAGE_VERSION_REMOTE_BACKUPS,
)
from .destination import RemoteDestination
from .errors import DestinationError, DestinationNotFoundError
from .models import RemoteBackup

_LOGGER = logging.getLogger(__name__)

# Clé du marqueur déposé dans les métadonnées d'une sauvegarde distante par les
# fournisseurs réels (issues #12 et #15). Elle vaut le domaine de l'intégration :
# un fournisseur qui range les métadonnées à plat chez lui (propriétés Dropbox,
# `appProperties` Google Drive) reste ainsi reconnaissable d'un coup d'œil.
CLE_MARQUEUR = DOMAIN

# Les API de stockage ne conservent souvent que des **chaînes** en métadonnées :
# le marqueur doit donc être reconnu aussi bien en booléen qu'en texte.
VALEURS_DU_MARQUEUR = frozenset({"true", "1", "oui", "yes", DOMAIN})

# Date de repli pour ordonner une sauvegarde dont la date est inconnue : elle
# passe pour la plus ancienne, donc la première supprimée quand la rétention en
# nombre est dépassée. Ce cas ne se produit que si le fournisseur ne date pas ses
# fichiers *et* que l'entrée a disparu du registre ; le contraire — la considérer
# comme la plus récente — ferait survivre indéfiniment une sauvegarde de trop.
DATE_INCONNUE = datetime.min.replace(tzinfo=UTC)


def marqueur_auto_backup() -> dict[str, Any]:
    """Métadonnées à joindre à un téléversement pour marquer sa provenance.

    Les fournisseurs réels (#12, #15) la passent à `async_upload()` : une
    sauvegarde ainsi marquée reste purgeable même si le registre du fork a été
    perdu (réinstallation, `.storage` effacé).
    """
    return {CLE_MARQUEUR: True}


def porte_le_marqueur(sauvegarde: RemoteBackup) -> bool:
    """Indique si cette sauvegarde distante a été déposée par Auto Backup."""
    valeur = sauvegarde.metadata.get(CLE_MARQUEUR)
    if isinstance(valeur, bool):
        return valeur
    if isinstance(valeur, str):
        return valeur.strip().casefold() in VALEURS_DU_MARQUEUR
    return False


def _date_utc(valeur: Any) -> datetime | None:
    """Renvoie une date en UTC, ou `None` si la valeur est inexploitable.

    Une date sans fuseau est réputée UTC : c'est ce que produisent les API des
    fournisseurs qui datent en « Zulu » sans le dire, et se tromper de fuseau
    n'a ici qu'un effet de quelques heures sur une rétention comptée en jours.
    """
    if isinstance(valeur, datetime):
        return valeur.replace(tzinfo=UTC) if valeur.tzinfo is None else valeur
    if isinstance(valeur, str):
        try:
            return _date_utc(datetime.fromisoformat(valeur))
        except ValueError:
            return None
    return None


def _taille(valeur: Any) -> int | None:
    """Renvoie une taille en octets exploitable, ou `None`."""
    if isinstance(valeur, bool) or not isinstance(valeur, int) or valeur < 0:
        return None
    return valeur


@dataclass(frozen=True, slots=True)
class EntreeRegistre:
    """Trace d'une sauvegarde déposée par le fork chez un fournisseur.

    C'est la preuve de provenance qui autorise la purge à supprimer le fichier
    correspondant : ce qui n'est pas ici (et ne porte pas le marqueur) n'est
    jamais touché.
    """

    remote_id: str
    name: str
    slug: str | None = None
    created_at: datetime | None = None
    size: int | None = None

    def as_dict(self) -> dict[str, Any]:
        """Représentation JSON, telle qu'elle est persistée par le `Store`."""
        return {
            ATTR_REMOTE_ID: self.remote_id,
            ATTR_NAME: self.name,
            ATTR_SLUG: self.slug,
            ATTR_CREATED_AT: (
                self.created_at.isoformat() if self.created_at is not None else None
            ),
            ATTR_SIZE: self.size,
        }

    @classmethod
    def from_dict(cls, donnees: Mapping[str, Any]) -> EntreeRegistre | None:
        """Relit une entrée persistée, ou renvoie `None` si elle est illisible.

        Le fichier de stockage reste éditable à la main : une entrée abîmée est
        ignorée plutôt que de faire échouer le chargement de l'intégration.
        """
        remote_id = donnees.get(ATTR_REMOTE_ID)
        if not isinstance(remote_id, str) or not remote_id.strip():
            return None
        nom = donnees.get(ATTR_NAME)
        slug = donnees.get(ATTR_SLUG)
        return cls(
            remote_id=remote_id,
            name=nom if isinstance(nom, str) and nom.strip() else remote_id,
            slug=slug if isinstance(slug, str) and slug.strip() else None,
            created_at=_date_utc(donnees.get(ATTR_CREATED_AT)),
            size=_taille(donnees.get(ATTR_SIZE)),
        )


class RegistreSauvegardesDistantes:
    """Registre persistant des sauvegardes déposées, par destination.

    Il est rangé dans `.storage/auto_backup.remote_backups`, à côté du registre
    d'expiration de l'upstream (`auto_backup.snapshots_expiry`), qu'il ne
    remplace pas : l'un suit les sauvegardes **locales**, l'autre les copies
    **distantes**.

    Le contenu est volontairement pauvre — identifiant distant, nom, slug, date,
    taille — et ne porte aucun secret : ni jeton, ni identifiant de compte.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        """Prépare un registre vide adossé au stockage de Home Assistant."""
        self._store: Store[dict[str, list[dict[str, Any]]]] = Store(
            hass,
            STORAGE_VERSION_REMOTE_BACKUPS,
            f"{DOMAIN}.{STORAGE_KEY_REMOTE_BACKUPS}",
        )
        self._entrees: dict[str, dict[str, EntreeRegistre]] = {}

    async def async_load(self) -> None:
        """Relit le registre persisté ; un contenu illisible repart à vide."""
        donnees = await self._store.async_load()
        self._entrees = {}
        if not isinstance(donnees, Mapping):
            if donnees is not None:
                _LOGGER.warning(
                    "Registre des sauvegardes distantes illisible : il est ignoré"
                )
            return

        for destination_id, brutes in donnees.items():
            if not isinstance(destination_id, str) or not isinstance(brutes, list):
                continue
            entrees: dict[str, EntreeRegistre] = {}
            for brute in brutes:
                if not isinstance(brute, Mapping):
                    continue
                entree = EntreeRegistre.from_dict(brute)
                if entree is not None:
                    entrees[entree.remote_id] = entree
            if entrees:
                self._entrees[destination_id] = entrees

    async def _async_save(self) -> None:
        """Écrit le registre dans le stockage de Home Assistant."""
        await self._store.async_save(
            {
                destination_id: [entree.as_dict() for entree in entrees.values()]
                for destination_id, entrees in self._entrees.items()
            }
        )

    def entrees(self, destination_id: str) -> list[EntreeRegistre]:
        """Entrées connues pour cette destination, dans l'ordre d'ajout."""
        return list(self._entrees.get(destination_id, {}).values())

    def entree(self, destination_id: str, remote_id: str) -> EntreeRegistre | None:
        """Entrée d'une sauvegarde distante, ou `None` si elle est inconnue."""
        return self._entrees.get(destination_id, {}).get(remote_id)

    async def async_enregistrer(
        self, destination_id: str, entree: EntreeRegistre
    ) -> None:
        """Inscrit (ou remplace) une sauvegarde déposée, puis persiste."""
        self._entrees.setdefault(destination_id, {})[entree.remote_id] = entree
        await self._async_save()

    async def async_retirer(
        self, destination_id: str, remote_ids: Iterable[str]
    ) -> bool:
        """Retire ces sauvegardes du registre ; renvoie `True` s'il a changé."""
        entrees = self._entrees.get(destination_id)
        if not entrees:
            return False

        retires = [remote_id for remote_id in remote_ids if remote_id in entrees]
        if not retires:
            return False

        for remote_id in retires:
            del entrees[remote_id]
        if not entrees:
            del self._entrees[destination_id]
        await self._async_save()
        return True


@dataclass(frozen=True, slots=True)
class CandidatPurge:
    """Sauvegarde distante reconnue comme déposée par Auto Backup.

    `date` est la date retenue pour la rétention : celle annoncée par le
    fournisseur quand il en donne une, sinon celle du téléversement telle que le
    registre l'a notée.
    """

    remote_id: str
    nom: str
    date: datetime | None


class CoordinateurPurgeDistante:
    """Applique la rétention distante et tient le registre à jour.

    Une instance par entrée de configuration, exposée dans
    `hass.data[DATA_REMOTE_PURGE]`. Les purges sont sérialisées par un verrou :
    un téléversement terminé pendant un `auto_backup.purge` ne fait pas lister
    ni supprimer deux fois la même destination.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        registre: RegistreSauvegardesDistantes,
    ) -> None:
        """Prépare un coordinateur rattaché à une entrée et à son registre."""
        self._hass = hass
        self._entry = entry
        self._registre = registre
        self._verrou = asyncio.Lock()

    @callback
    def async_setup(self) -> None:
        """Écoute les téléversements réussis jusqu'au déchargement de l'entrée."""
        self._entry.async_on_unload(
            self._hass.bus.async_listen(
                EVENT_UPLOAD_SUCCESSFUL, self._async_televersement_reussi
            )
        )

    @property
    def purge_automatique(self) -> bool:
        """Indique si l'option upstream `auto_purge` est active.

        C'est **la même** option que celle qui commande la purge locale après
        une création : l'utilisateur qui la désactive ne veut aucune suppression
        automatique, ni ici, ni là. L'option est relue à chaque événement, elle
        s'applique donc sans redémarrage.
        """
        return bool(self._entry.options.get(CONF_AUTO_PURGE, True))

    async def _async_televersement_reussi(self, event: Event) -> None:
        """Inscrit la sauvegarde téléversée au registre, puis purge si demandé.

        Le modèle de confiance est celui de l'issue #8 : l'événement est émis
        par le coordinateur de téléversement. Le forger sur le bus — ce qui
        suppose déjà un accès authentifié à Home Assistant — inscrirait au
        registre un identifiant distant arbitraire, qui deviendrait purgeable ;
        c'est exactement la confiance que le téléversement accorde aux
        événements upstream `auto_backup.backup_*`.
        """
        donnees = event.data
        destination_id = donnees.get(ATTR_DESTINATION)
        remote_id = donnees.get(ATTR_REMOTE_ID)
        if not isinstance(destination_id, str) or not isinstance(remote_id, str):
            return

        nom = donnees.get(ATTR_NAME)
        slug = donnees.get(ATTR_SLUG)
        await self._registre.async_enregistrer(
            destination_id,
            EntreeRegistre(
                remote_id=remote_id,
                name=nom if isinstance(nom, str) and nom.strip() else remote_id,
                slug=slug if isinstance(slug, str) and slug.strip() else None,
                # L'événement ne porte pas la date du fournisseur : celle du
                # téléversement en tient lieu, et ne sert de toute façon que si
                # le fournisseur ne date pas ses fichiers.
                created_at=dt_util.utcnow(),
                size=_taille(donnees.get(ATTR_SIZE)),
            ),
        )

        if not self.purge_automatique:
            _LOGGER.debug(
                "Purge distante de « %s » non déclenchée : l'option « %s » est "
                "désactivée",
                destination_id,
                CONF_AUTO_PURGE,
            )
            return

        await self.async_purger_destination(destination_id)

    async def async_purger_toutes(self) -> dict[str, list[str]]:
        """Purge chaque destination configurée, l'une après l'autre.

        Renvoie, par identifiant de destination, les identifiants distants
        supprimés. L'échec d'une destination n'empêche jamais les suivantes
        d'être traitées.
        """
        destinations = self._destinations()
        if not destinations:
            _LOGGER.debug("Aucune destination distante configurée : rien à purger")
            return {}

        resultats: dict[str, list[str]] = {}
        async with self._verrou:
            for destination in destinations:
                supprimes = await self._async_purger(destination)
                if supprimes:
                    resultats[destination.destination_id] = supprimes
        return resultats

    async def async_purger_destination(self, destination_id: str) -> list[str]:
        """Purge une destination désignée par son identifiant."""
        destination = self._destination(destination_id)
        if destination is None:
            _LOGGER.debug(
                "Purge distante ignorée : destination « %s » introuvable, elle a "
                "pu être supprimée depuis",
                destination_id,
            )
            return []

        async with self._verrou:
            return await self._async_purger(destination)

    async def _async_purger(self, destination: RemoteDestination) -> list[str]:
        """Applique la rétention d'une destination ; renvoie les suppressions."""
        destination_id = destination.destination_id
        if self._reauthentification_requise(destination_id):
            _LOGGER.warning(
                "Purge distante de « %s » ignorée : ré-authentification requise, "
                "autorisez de nouveau la destination depuis les options",
                destination.name,
            )
            return []

        if destination.retention_days is None and destination.retention_count is None:
            _LOGGER.debug(
                "Aucune rétention configurée pour « %s » : rien n'est supprimé",
                destination.name,
            )
            return []

        distantes = await self._async_lister(destination)
        if distantes is None:
            return []

        candidats = self._candidats(destination_id, distantes)
        a_supprimer = self._a_supprimer(destination, candidats)
        if not a_supprimer:
            _LOGGER.debug(
                "Rétention respectée sur « %s » : %s sauvegarde(s) conservée(s)",
                destination.name,
                len(candidats),
            )
            return []

        supprimes = await self._async_supprimer(destination, a_supprimer)
        await self._registre.async_retirer(destination_id, supprimes)
        if not supprimes:
            return []

        _LOGGER.info(
            "Purge distante de « %s » : %s sauvegarde(s) supprimée(s) (%s)",
            destination.name,
            len(supprimes),
            ", ".join(supprimes),
        )
        self._hass.bus.async_fire(
            EVENT_REMOTE_PURGE,
            {
                ATTR_DESTINATION: destination_id,
                ATTR_DESTINATION_NAME: destination.name,
                # Copie : l'événement ne doit pas partager sa liste avec la
                # valeur de retour, qu'un appelant peut trier ou vider.
                ATTR_REMOTE_IDS: list(supprimes),
            },
        )
        return supprimes

    async def _async_lister(
        self, destination: RemoteDestination
    ) -> list[RemoteBackup] | None:
        """Liste les sauvegardes distantes, ou `None` si le listage a échoué."""
        try:
            return list(await destination.async_list_backups())
        except DestinationError as err:
            _LOGGER.error(
                "Purge distante de « %s » abandonnée : listage impossible (%s)",
                destination.name,
                err,
            )
        except Exception:  # l'échec d'une destination n'en bloque aucune autre
            _LOGGER.exception(
                "Purge distante de « %s » abandonnée : erreur inattendue au listage",
                destination.name,
            )
        return None

    def _candidats(
        self, destination_id: str, distantes: Sequence[RemoteBackup]
    ) -> list[CandidatPurge]:
        """Ne retient que les sauvegardes dont le fork est l'auteur.

        Une sauvegarde est retenue si elle figure au registre **ou** si elle
        porte le marqueur du fork. Toute autre — un fichier déposé par
        l'utilisateur, la sauvegarde d'un autre outil — est ignorée : elle ne
        peut donc jamais être supprimée, quelle que soit la rétention.
        """
        candidats: list[CandidatPurge] = []
        for distante in distantes:
            entree = self._registre.entree(destination_id, distante.remote_id)
            if entree is None and not porte_le_marqueur(distante):
                _LOGGER.debug(
                    "Sauvegarde distante « %s » ignorée par la purge : elle n'a pas "
                    "été déposée par Auto Backup",
                    distante.remote_id,
                )
                continue
            candidats.append(
                CandidatPurge(
                    remote_id=distante.remote_id,
                    nom=distante.name,
                    date=_date_utc(distante.created_at)
                    or (entree.created_at if entree is not None else None),
                )
            )
        return candidats

    def _a_supprimer(
        self, destination: RemoteDestination, candidats: Sequence[CandidatPurge]
    ) -> list[CandidatPurge]:
        """Applique les deux rétentions, l'âge d'abord, le nombre ensuite.

        Les deux se combinent : ce que l'âge a déjà condamné ne compte plus dans
        le nombre de sauvegardes conservées, de sorte que `retention_count`
        borne bien ce qui **reste** chez le fournisseur.
        """
        expirees: list[CandidatPurge] = []
        survivants: list[CandidatPurge] = []

        if destination.retention_days is not None:
            limite = dt_util.utcnow() - timedelta(days=destination.retention_days)
            for candidat in candidats:
                # Une sauvegarde sans date connue n'est jamais réputée expirée :
                # on ne supprime pas sur une présomption d'ancienneté.
                if candidat.date is not None and candidat.date < limite:
                    expirees.append(candidat)
                else:
                    survivants.append(candidat)
        else:
            survivants = list(candidats)

        en_trop: list[CandidatPurge] = []
        if (
            destination.retention_count is not None
            and len(survivants) > destination.retention_count
        ):
            # Tri de la plus ancienne à la plus récente : les sauvegardes en trop
            # sont prises en tête, donc supprimées de la plus ancienne à la plus
            # jeune. Le tri est stable : deux sauvegardes de même date gardent
            # l'ordre du fournisseur, et c'est la première listée qui part.
            par_anciennete = sorted(
                survivants, key=lambda candidat: candidat.date or DATE_INCONNUE
            )
            en_trop = par_anciennete[: len(survivants) - destination.retention_count]

        return expirees + en_trop

    async def _async_supprimer(
        self, destination: RemoteDestination, a_supprimer: Sequence[CandidatPurge]
    ) -> list[str]:
        """Supprime les sauvegardes retenues ; renvoie celles qui ont disparu.

        Une sauvegarde déjà absente compte comme supprimée : le but est atteint,
        et son entrée doit quitter le registre pour ne pas être retentée à
        chaque purge. Une suppression qui échoue pour une autre raison laisse
        l'entrée en place — le fichier, lui, est toujours là — et n'interrompt
        pas la purge des suivantes.
        """
        supprimes: list[str] = []
        for candidat in a_supprimer:
            try:
                await destination.async_delete_backup(candidat.remote_id)
            except DestinationNotFoundError:
                _LOGGER.warning(
                    "Sauvegarde distante « %s » déjà absente de « %s » : son entrée "
                    "est retirée du registre et la purge continue",
                    candidat.remote_id,
                    destination.name,
                )
                supprimes.append(candidat.remote_id)
            except DestinationError as err:
                _LOGGER.error(
                    "Suppression de la sauvegarde distante « %s » sur « %s » "
                    "impossible : %s",
                    candidat.remote_id,
                    destination.name,
                    err,
                )
            except Exception:  # un échec isolé ne doit pas arrêter la purge
                _LOGGER.exception(
                    "Erreur inattendue à la suppression de la sauvegarde distante "
                    "« %s » sur « %s »",
                    candidat.remote_id,
                    destination.name,
                )
            else:
                _LOGGER.debug(
                    "Sauvegarde distante « %s » (%s) supprimée de « %s »",
                    candidat.nom,
                    candidat.remote_id,
                    destination.name,
                )
                supprimes.append(candidat.remote_id)
        return supprimes

    def _destinations(self) -> list[RemoteDestination]:
        """Destinations configurées, ou une liste vide si aucune n'est chargée."""
        gestionnaire = self._hass.data.get(DATA_DESTINATIONS)
        return [] if gestionnaire is None else gestionnaire.destinations

    def _destination(self, destination_id: str) -> RemoteDestination | None:
        """Destination portant cet identifiant, ou `None` si elle a disparu."""
        gestionnaire = self._hass.data.get(DATA_DESTINATIONS)
        if gestionnaire is None:
            return None
        try:
            return gestionnaire.async_get(destination_id)
        except DestinationError:
            return None

    def _reauthentification_requise(self, destination_id: str) -> bool:
        """Indique si cette destination attend une nouvelle autorisation (#7)."""
        gestionnaire = self._hass.data.get(DATA_DESTINATIONS)
        return gestionnaire is not None and gestionnaire.reauthentification_requise(
            destination_id
        )


async def async_setup_remote_purge(
    hass: HomeAssistant,
    entry: ConfigEntry,
    gestionnaire_upstream: Callable[[ServiceCall], Coroutine[Any, Any, None]],
) -> CoordinateurPurgeDistante:
    """Installe la purge distante et l'ajoute au service `auto_backup.purge`.

    Le service upstream n'est ni modifié ni remplacé : il est **enveloppé**. La
    fonction ré-inscrit `auto_backup.purge` avec un gestionnaire qui appelle
    d'abord celui de l'upstream — la purge locale s'exécute donc exactement comme
    avant, aux mêmes conditions et avec les mêmes journaux — puis purge chaque
    destination distante. Home Assistant remplace silencieusement une inscription
    de service par la dernière reçue, et `async_unload_entry` upstream retire le
    service par son nom : le déchargement reste celui de l'upstream.

    Les deux autres branchements possibles ont été écartés (cf. l'ADR) :
    l'événement `auto_backup.purged_backups` n'est émis **que** si une sauvegarde
    locale a réellement été supprimée — appeler le service sans rien à purger
    localement n'aurait alors purgé aucune destination —, et modifier
    `manager.py` aurait touché du code upstream, ce que le fork s'interdit.
    """
    registre = RegistreSauvegardesDistantes(hass)
    await registre.async_load()
    hass.data[DATA_REMOTE_BACKUPS] = registre

    coordinateur = CoordinateurPurgeDistante(hass, entry, registre)
    coordinateur.async_setup()
    hass.data[DATA_REMOTE_PURGE] = coordinateur

    @callback
    def retirer_le_coordinateur() -> None:
        """Retire le coordinateur et le registre au déchargement de l'entrée."""
        hass.data.pop(DATA_REMOTE_PURGE, None)
        hass.data.pop(DATA_REMOTE_BACKUPS, None)

    entry.async_on_unload(retirer_le_coordinateur)

    async def async_purger(call: ServiceCall) -> None:
        """Purge locale upstream, puis purge distante de chaque destination."""
        await gestionnaire_upstream(call)
        await coordinateur.async_purger_toutes()

    hass.services.async_register(DOMAIN, SERVICE_PURGE, async_purger, None)
    return coordinateur
