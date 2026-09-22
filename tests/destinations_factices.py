"""Fournisseurs de destination factices, entièrement en mémoire (issues #6, #7, #9).

Ce module ne fait aucune entrée-sortie : rien n'est lu sur le disque, rien n'est
envoyé sur le réseau. Il sert à démontrer qu'un fournisseur s'ajoute par le seul
registre, sans modification du cœur de l'intégration, et à éprouver la remontée
des erreurs typées.

Deux fournisseurs y sont définis :

- `DestinationEnMemoire` (`factice`) : aucune authentification ;
- `DestinationOAuthEnMemoire` (`factice_oauth`) : la même, qui déclare en plus une
  `OAUTH2_SPEC` et exige un jeton valide avant chaque opération (issue #7).

Toutes les valeurs sensibles employées ici sont **inventées** : les URL pointent
vers le domaine réservé `.test` (RFC 2606) et les jetons sont des chaînes
reconnaissables (`acces-factice-...`). Aucun identifiant réel n'est présent.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant

from custom_components.auto_backup.destinations import (
    DestinationConfig,
    DestinationNotFoundError,
    OAuth2ProviderSpec,
    RemoteBackup,
    RemoteDestination,
    async_session_de_la_destination,
)

PROVIDER_FACTICE = "factice"
PROVIDER_OAUTH_FACTICE = "factice_oauth"

DATE_FACTICE = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)

# Fournisseur OAuth2 imaginaire : même joint par erreur, `.test` ne peut
# atteindre aucun service réel.
URL_AUTORISATION_FACTICE = "https://fournisseur.test/oauth2/autoriser"
URL_JETON_FACTICE = "https://fournisseur.test/oauth2/jeton"
PORTEE_FACTICE = "sauvegardes.ecriture"

SPEC_OAUTH_FACTICE = OAuth2ProviderSpec(
    authorize_url=URL_AUTORISATION_FACTICE,
    token_url=URL_JETON_FACTICE,
    scopes=(PORTEE_FACTICE,),
)

# Identifiants d'application factices, tels que l'utilisateur les saisirait.
CLIENT_ID_FACTICE = "identifiant-application-factice"
CLIENT_SECRET_FACTICE = "secret-application-factice"
CODE_AUTORISATION_FACTICE = "code-autorisation-factice"

DUREE_JETON_FACTICE = 3600


def jeton_factice(**surcharges: Any) -> dict[str, Any]:
    """Jeton OAuth2 factice déjà normalisé (`expires_at` renseigné)."""
    return {
        "access_token": "acces-factice-1",
        "refresh_token": "rafraichissement-factice-1",
        "token_type": "Bearer",
        "expires_in": DUREE_JETON_FACTICE,
        "expires_at": time.time() + DUREE_JETON_FACTICE,
    } | surcharges


def reponse_de_jeton_factice(**surcharges: Any) -> dict[str, Any]:
    """Réponse brute du fournisseur : durée relative, pas de date absolue."""
    return {
        "access_token": "acces-factice-2",
        "refresh_token": "rafraichissement-factice-2",
        "token_type": "Bearer",
        "expires_in": DUREE_JETON_FACTICE,
    } | surcharges


def config_factice(**surcharges: Any) -> dict[str, Any]:
    """Configuration brute d'une destination factice, telle que persistée."""
    return {
        "destination_id": "destination_test",
        "provider": PROVIDER_FACTICE,
        "name": "Destination de test",
        "folder": "Sauvegardes",
        "retention_days": 7,
        "retention_count": 3,
    } | surcharges


def config_oauth_factice(**surcharges: Any) -> dict[str, Any]:
    """Configuration brute d'une destination factice autorisée en OAuth2."""
    return (
        config_factice(
            destination_id="destination_oauth",
            provider=PROVIDER_OAUTH_FACTICE,
            name="Destination OAuth",
            client_id=CLIENT_ID_FACTICE,
            client_secret=CLIENT_SECRET_FACTICE,
            token=jeton_factice(),
        )
        | surcharges
    )


class DestinationEnMemoire(RemoteDestination):
    """Destination factice qui conserve les sauvegardes dans un dictionnaire.

    Attributs utiles aux tests :

    - `sauvegardes` : les sauvegardes distantes, par identifiant ;
    - `erreur_a_lever` : erreur levée par toutes les opérations quand elle est
      définie, pour simuler un échec du fournisseur ;
    - `connexions_verifiees` : nombre d'appels réussis à `async_check_connection` ;
    - `listages` : nombre d'appels à `async_list_backups` — un zéro prouve
      qu'une destination n'a jamais été jointe (issue #9) ;
    - `suppressions` : identifiants distants dont la suppression a été *tentée*,
      dans l'ordre ;
    - `erreurs_de_suppression` : erreur à lever pour un identifiant distant
      donné, pour éprouver la tolérance aux échecs de la purge ;
    - `attente_secondes` : durée d'attente simulée pendant un téléversement,
      pour éprouver le délai maximum (issue #8) ;
    - `octets_recus` : contenu du dernier flux consommé, `None` si le
      téléversement n'a reçu qu'un chemin ;
    - `taille_recue` : nombre d'octets réellement lus dans le flux ;
    - `taille_annoncee` : valeur du paramètre `size` du dernier téléversement.
    """

    def __init__(self, hass: HomeAssistant, config: DestinationConfig) -> None:
        """Prépare une destination vide."""
        super().__init__(hass, config)
        self.sauvegardes: dict[str, RemoteBackup] = {}
        # Une erreur quelconque est acceptée, pas seulement une `DestinationError` :
        # un fournisseur réel peut aussi laisser filer une erreur inattendue,
        # que l'orchestrateur doit traiter sans compromettre les autres destinations.
        self.erreur_a_lever: Exception | None = None
        self.connexions_verifiees = 0
        self.listages = 0
        self.suppressions: list[str] = []
        self.erreurs_de_suppression: dict[str, Exception] = {}
        self.attente_secondes = 0.0
        self.octets_recus: bytes | None = None
        self.taille_recue: int | None = None
        self.taille_annoncee: int | None = None
        self._compteur = 0

    def _verifier_erreur(self) -> None:
        """Lève l'erreur simulée si le test en a programmé une."""
        if self.erreur_a_lever is not None:
            raise self.erreur_a_lever

    async def async_check_connection(self) -> None:
        """Simule une vérification d'accès au fournisseur."""
        self._verifier_erreur()
        self.connexions_verifiees += 1

    async def async_upload(
        self,
        source: Path | str | None = None,
        *,
        name: str,
        slug: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        stream: AsyncIterator[bytes] | None = None,
        size: int | None = None,
        filename: str | None = None,
    ) -> RemoteBackup:
        """Mémorise une sauvegarde distante et la renvoie.

        Quand un flux est fourni, il est consommé morceau par morceau — comme
        le ferait un vrai fournisseur — et la taille réellement reçue est
        enregistrée. Sans flux, l'ancien comportement (chemin seul) est
        conservé, ce qui garde les tests du socle (#6) valides.
        """
        self._verifier_erreur()
        if self.attente_secondes:
            await asyncio.sleep(self.attente_secondes)

        self.taille_annoncee = size
        if stream is None:
            self.octets_recus = None
            self.taille_recue = None
            taille = len(str(source))
        else:
            morceaux = [morceau async for morceau in stream]
            self.octets_recus = b"".join(morceaux)
            self.taille_recue = len(self.octets_recus)
            taille = self.taille_recue

        nom_fichier = filename or (Path(source).name if source is not None else name)
        self._compteur += 1
        sauvegarde = RemoteBackup(
            remote_id=f"{self.destination_id}-{self._compteur}",
            name=name,
            slug=slug,
            size=taille,
            created_at=DATE_FACTICE,
            path=f"{self.folder}/{nom_fichier}",
            metadata=dict(metadata or {}),
        )
        self.sauvegardes[sauvegarde.remote_id] = sauvegarde
        return sauvegarde

    def ajouter_sauvegarde(
        self,
        remote_id: str,
        *,
        name: str | None = None,
        created_at: datetime | None = DATE_FACTICE,
        slug: str | None = None,
        size: int | None = 1024,
        metadata: Mapping[str, Any] | None = None,
        path: str | None = None,
    ) -> RemoteBackup:
        """Dépose une sauvegarde distante sans passer par un téléversement.

        C'est ainsi qu'un test de purge (issue #9) peuple un dossier distant :
        des sauvegardes datées, et au besoin un fichier « étranger » que
        l'utilisateur y aurait déposé lui-même — un fichier que rien n'inscrit
        au registre du fork et qui ne porte aucun marqueur.
        """
        sauvegarde = RemoteBackup(
            remote_id=remote_id,
            name=name or remote_id,
            slug=slug,
            size=size,
            created_at=created_at,
            path=path or f"{self.folder}/{remote_id}.tar",
            metadata=dict(metadata or {}),
        )
        self.sauvegardes[sauvegarde.remote_id] = sauvegarde
        return sauvegarde

    async def async_list_backups(self) -> list[RemoteBackup]:
        """Renvoie les sauvegardes mémorisées."""
        self.listages += 1
        self._verifier_erreur()
        return list(self.sauvegardes.values())

    async def async_delete_backup(self, remote_id: str) -> None:
        """Supprime une sauvegarde mémorisée.

        La tentative est notée **avant** tout échec : un test peut ainsi
        vérifier que la purge a poursuivi son chemin après une erreur.
        """
        self.suppressions.append(remote_id)
        self._verifier_erreur()
        erreur = self.erreurs_de_suppression.get(remote_id)
        if erreur is not None:
            raise erreur
        if remote_id not in self.sauvegardes:
            raise DestinationNotFoundError(
                f"sauvegarde distante inconnue : « {remote_id} »"
            )
        del self.sauvegardes[remote_id]


class DestinationOAuthEnMemoire(DestinationEnMemoire):
    """Destination factice qui s'authentifie en OAuth2 (issue #7).

    Elle se comporte comme `DestinationEnMemoire`, mais demande un jeton d'accès
    valide **avant chaque opération** : c'est ce que fera un vrai fournisseur.
    Le jeton obtenu est mémorisé dans `jetons_utilises`, ce qui permet à un test
    de vérifier qu'un jeton expiré a bien été rafraîchi avant l'appel.
    """

    OAUTH2_SPEC = SPEC_OAUTH_FACTICE

    def __init__(self, hass: HomeAssistant, config: DestinationConfig) -> None:
        """Prépare la destination et sa session OAuth2."""
        super().__init__(hass, config)
        self.session = async_session_de_la_destination(hass, config)
        self.jetons_utilises: list[str] = []

    async def _async_autoriser(self) -> None:
        """Récupère un jeton valide, en le rafraîchissant si nécessaire."""
        self.jetons_utilises.append(await self.session.async_get_access_token())

    async def async_check_connection(self) -> None:
        """Vérifie l'accès après s'être assuré d'un jeton valide."""
        await self._async_autoriser()
        await super().async_check_connection()

    async def async_upload(
        self,
        source: Path | str | None = None,
        *,
        name: str,
        slug: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        stream: AsyncIterator[bytes] | None = None,
        size: int | None = None,
        filename: str | None = None,
    ) -> RemoteBackup:
        """Téléverse après s'être assuré d'un jeton valide.

        Le jeton est obtenu **avant** de consommer le flux : un rafraîchissement
        ne doit pas s'intercaler au milieu de l'envoi (issues #7 et #8).
        """
        await self._async_autoriser()
        return await super().async_upload(
            source,
            name=name,
            slug=slug,
            metadata=metadata,
            stream=stream,
            size=size,
            filename=filename,
        )

    async def async_list_backups(self) -> list[RemoteBackup]:
        """Liste après s'être assuré d'un jeton valide."""
        await self._async_autoriser()
        return await super().async_list_backups()

    async def async_delete_backup(self, remote_id: str) -> None:
        """Supprime après s'être assuré d'un jeton valide."""
        await self._async_autoriser()
        await super().async_delete_backup(remote_id)
