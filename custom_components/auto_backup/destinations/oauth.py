"""Autorisation OAuth2 des destinations distantes (issue #7).

Ce module fournit tout ce dont le flux d'options (`destinations/flow.py`) et les
fournisseurs ont besoin pour obtenir, conserver et rafraîchir un jeton OAuth2 :

- `OAuth2ProviderSpec` : ce qu'un fournisseur déclare (URL d'autorisation, URL de
  jeton, portées demandées) ;
- `DestinationOAuth2Implementation` : l'implémentation `LocalOAuth2Implementation`
  du cœur de Home Assistant, adaptée au retour d'autorisation du fork ;
- `RetourAutorisationOAuthView` : la vue HTTP qui reçoit ce retour et reprend le
  flux d'options ;
- `DestinationOAuth2Session` : le jeton d'une destination, rafraîchi à la demande
  et persisté, qui lève `DestinationAuthError` quand l'accès est révoqué.

**Pourquoi une vue propre au fork ?** La vue standard
`OAuth2AuthorizeCallbackView` (`/auth/external/callback`) reprend le flux par
`hass.config_entries.flow.async_configure()` : elle ne sait donc reprendre qu'un
*config flow*. Les destinations vivant dans `entry.options`
(cf. `docs/adr/0001-destinations-distantes.md`), l'autorisation est conduite par
un flux d'**options**, que seul `hass.config_entries.options` sait reprendre.
Le fork enregistre donc sa propre vue sur `OAUTH_CALLBACK_PATH`, et l'utilisateur
déclare cette URL comme URI de redirection de son application OAuth2.

Aucun secret n'est journalisé ici, y compris en niveau `debug` : ni les
identifiants d'application, ni le code d'autorisation, ni les jetons.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import voluptuous as vol
from aiohttp import ClientError, web
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import UnknownFlow
from homeassistant.exceptions import (
    OAuth2TokenRequestError,
    OAuth2TokenRequestReauthError,
)
from homeassistant.helpers.config_entry_oauth2_flow import (
    CLOCK_OUT_OF_SYNC_MAX_SEC,
    HEADER_FRONTEND_BASE,
    LocalOAuth2Implementation,
)
from homeassistant.helpers.http import KEY_HASS, HomeAssistantView, current_request
from homeassistant.helpers.network import get_url
from yarl import URL

from ..const import (
    DATA_OAUTH_STATES,
    DATA_OAUTH_VIEW,
    DOMAIN,
    OAUTH_CALLBACK_PATH,
    OAUTH_STATE_TTL,
    OAUTH_TOKEN_TIMEOUT,
)
from .config_entry import async_persist_token, jeton_persiste
from .errors import DestinationAuthError, DestinationConfigError, DestinationError
from .models import DestinationConfig
from .reauth import async_signaler_la_reauthentification
from .registry import get_provider
from .schema import TOKEN_SCHEMA

_LOGGER = logging.getLogger(__name__)

# Réponses de la vue de retour. Elles s'affichent dans le navigateur de
# l'utilisateur, en français comme le reste de l'interface du fork.
_PAGE_DE_FERMETURE = "<script>window.close()</script>"
_ETAT_MANQUANT = "Paramètre « state » absent : autorisation impossible."
_ETAT_INCONNU = (
    "Autorisation inconnue ou expirée. Relancez « Ajouter une destination » "
    "depuis les options d'Auto Backup."
)
_REPONSE_MANQUANTE = "Le fournisseur n'a renvoyé ni code d'autorisation ni erreur."
_FLUX_TERMINE = "Le flux d'autorisation n'est plus en cours dans Home Assistant."


@dataclass(frozen=True, slots=True)
class OAuth2ProviderSpec:
    """Ce qu'un fournisseur déclare pour être autorisé en OAuth2.

    Un fournisseur l'expose par `RemoteDestination.OAUTH2_SPEC`. Rien d'autre
    n'est nécessaire : les identifiants de l'application (`client_id`,
    `client_secret`) sont saisis par l'utilisateur, qui crée lui-même son
    application chez le fournisseur — aucun secret n'est donc livré dans le code
    (règle de `CLAUDE.md`).
    """

    authorize_url: str
    token_url: str
    scopes: tuple[str, ...] = ()
    extra_authorize_data: Mapping[str, str] = field(default_factory=dict)

    def donnees_d_autorisation(self) -> dict[str, str]:
        """Paramètres à ajouter à l'URL d'autorisation, portées comprises."""
        donnees = dict(self.extra_authorize_data)
        if self.scopes:
            donnees.setdefault("scope", " ".join(self.scopes))
        return donnees


@dataclass(frozen=True, slots=True)
class EtatOAuth:
    """Autorisation en attente : à quel flux d'options rendre la main.

    L'URI de redirection est mémorisée avec le flux : le fournisseur exige que
    l'échange du code cite la même valeur que la demande d'autorisation, et
    l'URL externe de Home Assistant peut changer entre les deux.
    """

    flow_id: str
    redirect_uri: str
    expire_a: float


@callback
def spec_oauth_du_fournisseur(provider_id: str) -> OAuth2ProviderSpec | None:
    """Déclaration OAuth2 du fournisseur, ou `None` s'il n'en a pas.

    Lève `UnknownProviderError` si le fournisseur n'est pas enregistré : c'est un
    cas distinct de « ce fournisseur ne demande pas d'autorisation ».
    """
    spec = getattr(get_provider(provider_id), "OAUTH2_SPEC", None)
    return spec if isinstance(spec, OAuth2ProviderSpec) else None


@callback
def url_de_retour(hass: HomeAssistant) -> str:
    """URI de redirection à déclarer dans l'application OAuth2 de l'utilisateur.

    La base est celle utilisée par le cœur de Home Assistant : l'en-tête
    `HA-Frontend-Base` de la requête en cours, c'est-à-dire l'adresse par
    laquelle l'utilisateur accède réellement à son instance. Hors requête (appel
    interne, test), l'URL externe configurée prend le relais ; sans elle,
    `NoURLAvailableError` est levée et le flux s'interrompt avec un message
    explicite plutôt que de fabriquer une URL fausse.
    """
    requete = current_request.get()
    if requete is not None and (base := requete.headers.get(HEADER_FRONTEND_BASE)):
        return f"{base}{OAUTH_CALLBACK_PATH}"
    base = get_url(hass, allow_internal=False, prefer_external=True)
    return f"{base}{OAUTH_CALLBACK_PATH}"


@callback
def _etats(hass: HomeAssistant) -> dict[str, EtatOAuth]:
    """Table des autorisations en attente, créée à la demande."""
    return hass.data.setdefault(DATA_OAUTH_STATES, {})


@callback
def _purger_les_etats(etats: dict[str, EtatOAuth]) -> None:
    """Retire les autorisations expirées : rien ne doit s'accumuler."""
    maintenant = time.monotonic()
    for cle in [cle for cle, etat in etats.items() if etat.expire_a <= maintenant]:
        del etats[cle]


@callback
def enregistrer_un_etat(hass: HomeAssistant, flow_id: str, redirect_uri: str) -> str:
    """Crée l'état opaque qui relie le retour du fournisseur à ce flux.

    L'état est un aléa de 256 bits : il sert à la fois de jeton anti-CSRF (le
    fournisseur le renvoie tel quel) et de clé de reprise du flux d'options. Il
    est à usage unique et expire au bout d'`OAUTH_STATE_TTL` secondes.
    """
    etats = _etats(hass)
    _purger_les_etats(etats)
    etat = secrets.token_urlsafe(32)
    etats[etat] = EtatOAuth(
        flow_id=flow_id,
        redirect_uri=redirect_uri,
        expire_a=time.monotonic() + OAUTH_STATE_TTL,
    )
    return etat


@callback
def consommer_un_etat(hass: HomeAssistant, etat: str) -> EtatOAuth | None:
    """Consomme un état : le renvoie une seule fois, `None` ensuite."""
    etats = _etats(hass)
    _purger_les_etats(etats)
    memorise = etats.pop(etat, None)
    if memorise is None or memorise.expire_a <= time.monotonic():
        return None
    return memorise


@callback
def oublier_les_etats_du_flux(hass: HomeAssistant, flow_id: str) -> None:
    """Oublie les autorisations en attente d'un flux abandonné."""
    etats = _etats(hass)
    for cle in [cle for cle, etat in etats.items() if etat.flow_id == flow_id]:
        del etats[cle]


class DestinationOAuth2Implementation(LocalOAuth2Implementation):
    """Implémentation OAuth2 d'une destination, branchée sur la vue du fork.

    Seule la génération de l'URL d'autorisation est réécrite : elle utilise
    l'état opaque du fork et `OAUTH_CALLBACK_PATH` au lieu du JWT et de la vue du
    cœur, qui ne savent reprendre qu'un config flow. L'échange du code et le
    rafraîchissement du jeton restent ceux de Home Assistant.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        destination_id: str,
        spec: OAuth2ProviderSpec,
        client_id: str,
        client_secret: str,
    ) -> None:
        """Prépare l'implémentation d'une destination donnée."""
        super().__init__(
            hass,
            f"{DOMAIN}_{destination_id}",
            client_id,
            client_secret,
            spec.authorize_url,
            spec.token_url,
        )
        self._spec = spec

    @property
    def name(self) -> str:
        """Nom lisible de l'implémentation."""
        return "Auto Backup"

    @property
    def redirect_uri(self) -> str:
        """URI de redirection déclarée par l'utilisateur chez le fournisseur."""
        return url_de_retour(self.hass)

    @property
    def extra_authorize_data(self) -> dict[str, str]:
        """Portées et paramètres supplémentaires déclarés par le fournisseur."""
        return self._spec.donnees_d_autorisation()

    async def async_generate_authorize_url(self, flow_id: str) -> str:
        """URL vers laquelle envoyer l'utilisateur pour qu'il autorise l'accès."""
        redirect_uri = self.redirect_uri
        return str(
            URL(self.authorize_url)
            .with_query(
                {
                    "response_type": "code",
                    "client_id": self.client_id,
                    "redirect_uri": redirect_uri,
                    "state": enregistrer_un_etat(self.hass, flow_id, redirect_uri),
                }
            )
            .update_query(self.extra_authorize_data)
        )

    def __repr__(self) -> str:
        """Représentation journalisable : ni identifiant ni secret d'application."""
        return f"<{type(self).__name__} {self.domain} authorize={self.authorize_url}>"


@callback
def implementation_de_la_destination(
    hass: HomeAssistant,
    config: DestinationConfig,
    *,
    client_id: str | None = None,
    client_secret: str | None = None,
) -> DestinationOAuth2Implementation:
    """Construit l'implémentation OAuth2 d'une destination.

    `client_id` et `client_secret` permettent d'utiliser des identifiants qui ne
    sont pas encore persistés : c'est le cas pendant l'ajout d'une destination,
    où l'autorisation précède l'écriture dans l'entrée.
    """
    spec = spec_oauth_du_fournisseur(config.provider)
    if spec is None:
        raise DestinationConfigError(
            f"le fournisseur « {config.provider} » ne gère pas OAuth2"
        )
    identifiant = client_id or config.client_id
    secret = client_secret or config.client_secret
    if not identifiant or not secret:
        raise DestinationConfigError(
            f"identifiants d'application absents pour « {config.destination_id} »"
        )
    return DestinationOAuth2Implementation(
        hass, config.destination_id, spec, identifiant, secret
    )


def jeton_valide(
    jeton: Mapping[str, Any] | None, *, marge: float = CLOCK_OUT_OF_SYNC_MAX_SEC
) -> bool:
    """Indique si le jeton est encore utilisable, marge d'horloge comprise."""
    if not jeton:
        return False
    expire_a = jeton.get("expires_at")
    if isinstance(expire_a, bool) or not isinstance(expire_a, int | float):
        return False
    return float(expire_a) > time.time() + marge


def normaliser_le_jeton(jeton: Mapping[str, Any]) -> dict[str, Any]:
    """Complète un jeton brut par `expires_at` et valide sa forme.

    Les fournisseurs renvoient une durée relative (`expires_in`) ; c'est une date
    absolue qu'il faut persister, sans quoi un redémarrage de Home Assistant
    rendrait tout jeton éternellement « valide ».
    """
    complete = dict(jeton)
    if "expires_at" not in complete:
        try:
            duree = int(complete["expires_in"])
        except (KeyError, TypeError, ValueError) as err:
            raise DestinationConfigError(
                "jeton inexploitable : ni « expires_at » ni « expires_in »"
            ) from err
        complete["expires_in"] = duree
        complete["expires_at"] = time.time() + duree
    try:
        return dict(TOKEN_SCHEMA(complete))
    except (vol.Invalid, TypeError, ValueError) as err:
        raise DestinationConfigError(f"jeton invalide : {err}") from err


class DestinationOAuth2Session:
    """Jeton d'accès d'une destination, toujours valide au moment de l'usage.

    Un fournisseur appelle `async_get_access_token()` avant chaque opération : le
    jeton est rafraîchi si nécessaire, puis persisté dans les options de
    l'entrée. Quand le rafraîchissement est refusé par le fournisseur (accès
    révoqué, `invalid_grant`), la session lève `DestinationAuthError` **et**
    signale la destination comme à ré-autoriser, ce qui crée le problème (repair
    issue) correspondant — pour cette destination seulement.
    """

    def __init__(self, hass: HomeAssistant, config: DestinationConfig) -> None:
        """Prépare la session d'une destination configurée."""
        self._hass = hass
        self._config = config
        self._verrou = asyncio.Lock()

    @property
    def config(self) -> DestinationConfig:
        """Configuration de la destination servie par cette session."""
        return self._config

    @property
    def token(self) -> dict[str, Any] | None:
        """Jeton courant, relu dans l'entrée pour suivre les rafraîchissements."""
        persiste = jeton_persiste(self._hass, self._config.destination_id)
        if persiste is not None:
            return persiste
        return dict(self._config.token) if self._config.token else None

    @property
    def valid_token(self) -> bool:
        """Indique si le jeton courant est encore valide."""
        return jeton_valide(self.token)

    async def async_ensure_token_valid(self) -> dict[str, Any]:
        """Renvoie un jeton valide, en le rafraîchissant au besoin."""
        async with self._verrou:
            jeton = self.token
            if jeton is None:
                self._signaler_la_reauthentification()
                raise DestinationAuthError(
                    f"la destination « {self._config.name} » n'est pas autorisée"
                )
            if jeton_valide(jeton):
                return jeton
            return await self._async_rafraichir(jeton)

    async def async_get_access_token(self) -> str:
        """Jeton d'accès utilisable immédiatement dans un en-tête HTTP."""
        jeton = await self.async_ensure_token_valid()
        return str(jeton["access_token"])

    async def _async_rafraichir(self, jeton: dict[str, Any]) -> dict[str, Any]:
        """Rafraîchit le jeton, le persiste et le renvoie."""
        implementation = implementation_de_la_destination(self._hass, self._config)
        if not jeton.get("refresh_token"):
            self._signaler_la_reauthentification()
            raise DestinationAuthError(
                f"jeton expiré et non renouvelable pour « {self._config.name} » : "
                "le fournisseur n'a fourni aucun jeton de rafraîchissement"
            )
        try:
            async with asyncio.timeout(OAUTH_TOKEN_TIMEOUT):
                nouveau = await implementation.async_refresh_token(jeton)
        except OAuth2TokenRequestReauthError as err:
            self._signaler_la_reauthentification()
            raise DestinationAuthError(
                f"accès révoqué pour « {self._config.name} » : "
                "une nouvelle autorisation est nécessaire"
            ) from err
        except (OAuth2TokenRequestError, ClientError, TimeoutError) as err:
            raise DestinationError(
                f"rafraîchissement du jeton impossible pour "
                f"« {self._config.name} » : {err}"
            ) from err
        except (KeyError, TypeError, ValueError) as err:
            raise DestinationError(
                f"réponse de rafraîchissement inexploitable pour "
                f"« {self._config.name} »"
            ) from err

        valide = normaliser_le_jeton(nouveau)
        async_persist_token(self._hass, self._config.destination_id, valide)
        _LOGGER.debug(
            "Jeton rafraîchi pour la destination « %s »", self._config.destination_id
        )
        return valide

    @callback
    def _signaler_la_reauthentification(self) -> None:
        """Marque la destination comme à ré-autoriser et crée le problème HA."""
        async_signaler_la_reauthentification(self._hass, self._config)

    def __repr__(self) -> str:
        """Représentation journalisable : aucun jeton n'y figure."""
        return (
            f"<{type(self).__name__} {self._config.destination_id} "
            f"valide={self.valid_token}>"
        )


class RetourAutorisationOAuthView(HomeAssistantView):
    """Vue qui reçoit le retour d'autorisation et reprend le flux d'options."""

    requires_auth = False
    url = OAUTH_CALLBACK_PATH
    name = f"{DOMAIN}:oauth:callback"

    async def get(self, request: web.Request) -> web.Response:
        """Traite la redirection du fournisseur.

        Ni le code d'autorisation ni l'état ne sont journalisés : le premier
        s'échange contre un jeton, le second reprend un flux en cours.
        """
        hass: HomeAssistant = request.app[KEY_HASS]

        etat = request.query.get("state")
        if not etat:
            return web.Response(text=_ETAT_MANQUANT, status=400)

        memorise = consommer_un_etat(hass, etat)
        if memorise is None:
            return web.Response(text=_ETAT_INCONNU, status=400)

        donnees: dict[str, Any] = {"state": {"redirect_uri": memorise.redirect_uri}}
        if code := request.query.get("code"):
            donnees["code"] = code
        elif erreur := request.query.get("error"):
            donnees["error"] = erreur
        else:
            return web.Response(text=_REPONSE_MANQUANTE, status=400)

        try:
            await hass.config_entries.options.async_configure(
                flow_id=memorise.flow_id, user_input=donnees
            )
        except UnknownFlow:
            return web.Response(text=_FLUX_TERMINE, status=400)

        _LOGGER.debug("Retour d'autorisation reçu, flux d'options repris")
        return web.Response(
            headers={"content-type": "text/html"}, text=_PAGE_DE_FERMETURE
        )


@callback
def async_enregistrer_la_vue_de_retour(hass: HomeAssistant) -> bool:
    """Enregistre la vue de retour, une fois pour toutes.

    L'enregistrement est tardif — au moment où un flux d'autorisation démarre —
    et non au démarrage de l'intégration : le serveur HTTP est alors certainement
    disponible, et une installation qui n'utilise aucune destination OAuth2
    n'expose aucune route supplémentaire. Home Assistant neutralise le gel du
    routeur aiohttp au démarrage, ce qui rend cet ajout possible à chaud.
    """
    if hass.data.get(DATA_OAUTH_VIEW):
        return True
    serveur = getattr(hass, "http", None)
    if serveur is None:
        _LOGGER.warning(
            "Serveur HTTP indisponible : le retour d'autorisation OAuth2 ne peut "
            "pas être reçu"
        )
        return False
    serveur.register_view(RetourAutorisationOAuthView())
    hass.data[DATA_OAUTH_VIEW] = True
    return True


@callback
def async_session_de_la_destination(
    hass: HomeAssistant, config: DestinationConfig
) -> DestinationOAuth2Session:
    """Session OAuth2 d'une destination, à utiliser avant chaque opération."""
    return DestinationOAuth2Session(hass, config)


__all__ = [
    "DestinationOAuth2Implementation",
    "DestinationOAuth2Session",
    "EtatOAuth",
    "OAuth2ProviderSpec",
    "RetourAutorisationOAuthView",
    "async_enregistrer_la_vue_de_retour",
    "async_session_de_la_destination",
    "consommer_un_etat",
    "enregistrer_un_etat",
    "implementation_de_la_destination",
    "jeton_valide",
    "normaliser_le_jeton",
    "oublier_les_etats_du_flux",
    "spec_oauth_du_fournisseur",
    "url_de_retour",
]
