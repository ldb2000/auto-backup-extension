"""Gestion des destinations distantes depuis le flux d'options (issue #7).

Ce module apporte au flux d'options upstream un menu et les étapes d'ajout, de
ré-autorisation et de suppression d'une destination. Il n'existe pas dans
l'upstream et ne modifie aucune de ses lignes : `config_flow.py` se contente
d'envelopper sa classe `OptionsFlowHandler` par `etendre_le_flux_d_options()`,
qui construit une sous-classe portant les étapes ci-dessous
(cf. `docs/UPSTREAM.md`).

Le parcours d'ajout d'une destination OAuth2 :

```text
menu -> ajouter_destination -> identifiants -> autorisation (étape externe)
     -> [navigateur de l'utilisateur, puis retour sur OAUTH_CALLBACK_PATH]
     -> jeton -> destination -> options enregistrées
```

L'étape externe est celle de Home Assistant (`async_external_step`) ; c'est la
vue du fork (`destinations/oauth.py`) qui reprend le flux, la vue standard ne
sachant reprendre qu'un config flow.

Entre `jeton` et `destination`, le flux interroge le fournisseur par deux
crochets **facultatifs** cherchés sur sa fabrique (issue #13) :
`async_donnees_du_fournisseur(session)`, dont le résultat est persisté dans
`DestinationConfig.provider_data`, et `async_nom_par_defaut(session)`, qui
fournit le nom proposé par le formulaire. Un fournisseur qui n'en expose aucun
suit exactement le parcours d'origine, sans appel réseau supplémentaire.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Iterable, Mapping
from typing import Any

import voluptuous as vol
from aiohttp import ClientError
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.const import CONF_CLIENT_ID, CONF_CLIENT_SECRET, CONF_NAME
from homeassistant.exceptions import (
    OAuth2TokenRequestError,
    OAuth2TokenRequestReauthError,
)
from homeassistant.helpers.network import NoURLAvailableError
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.util import slugify

from ..const import (
    CONF_DESTINATION_ID,
    CONF_DESTINATIONS,
    CONF_FOLDER,
    CONF_PROVIDER,
    CONF_RETENTION_COUNT,
    CONF_RETENTION_DAYS,
    DEFAULT_DESTINATION_FOLDER,
    OAUTH_AUTHORIZE_URL_TIMEOUT,
    OAUTH_TOKEN_TIMEOUT,
)
from .config_entry import async_destination_configs, options_avec_destinations
from .errors import DestinationConfigError, DestinationError, UnknownProviderError
from .models import DestinationConfig
from .oauth import (
    DestinationOAuth2Implementation,
    DestinationOAuth2Session,
    async_enregistrer_la_vue_de_retour,
    async_session_de_la_destination,
    implementation_de_la_destination,
    normaliser_le_jeton,
    oublier_les_etats_du_flux,
    spec_oauth_du_fournisseur,
    url_de_retour,
)
from .reauth import async_effacer_la_reauthentification
from .registry import get_provider, list_providers
from .schema import chemin_de_dossier

_LOGGER = logging.getLogger(__name__)

# Bornes des rétentions proposées par le formulaire. Elles ne remplacent pas la
# validation (`DESTINATION_SCHEMA`) : elles évitent seulement à l'utilisateur de
# saisir une valeur que le schéma refusera.
RETENTION_JOURS_MAX = 3650
RETENTION_NOMBRE_MAX = 1000

# Longueur du suffixe aléatoire ajouté à un identifiant de destination déjà pris.
LONGUEUR_SUFFIXE_IDENTIFIANT = 4

# Identifiant de la destination fictive utilisée pendant l'ajout : l'autorisation
# précède la création, et l'implémentation OAuth2 a besoin d'un identifiant.
IDENTIFIANT_PROVISOIRE = "autorisation_en_cours"

# Crochets facultatifs qu'un fournisseur peut exposer (issue #13). Ils sont
# détectés par `getattr` et non déclarés dans `RemoteDestination` : un
# fournisseur qui ne les implémente pas continue de fonctionner à l'identique.
CROCHET_NOM_PAR_DEFAUT = "async_nom_par_defaut"
CROCHET_DONNEES_DU_FOURNISSEUR = "async_donnees_du_fournisseur"


def _retention(user_input: Mapping[str, Any], cle: str) -> int | None:
    """Convertit une rétention saisie en entier, ou `None` si elle est vide.

    Les sélecteurs numériques de Home Assistant renvoient un flottant ; la
    configuration, elle, n'accepte que des entiers strictement positifs.
    """
    valeur = user_input.get(cle)
    if valeur is None or valeur == "":
        return None
    try:
        return int(float(valeur))
    except (TypeError, ValueError) as err:
        raise DestinationConfigError(f"{cle} doit être un nombre entier") from err


def _libelle_du_fournisseur(provider_id: str) -> str:
    """Libellé lisible d'un fournisseur, ou son identifiant technique à défaut.

    Un fournisseur annonce son nom d'affichage par un attribut de classe
    `label` (« Google Drive » pour `google_drive`). L'attribut est facultatif :
    un fournisseur qui ne le déclare pas s'affiche comme avant, sous son
    identifiant.
    """
    try:
        fabrique = get_provider(provider_id)
    except UnknownProviderError:
        return provider_id
    libelle = getattr(fabrique, "label", None)
    return libelle if isinstance(libelle, str) and libelle.strip() else provider_id


def _identifiant_disponible(nom: str, pris: Iterable[str]) -> str:
    """Fabrique un identifiant stable, lisible et unique dans l'entrée.

    L'identifiant dérive du nom choisi (`Mon Dropbox` -> `mon_dropbox`) : il est
    reconnaissable dans les journaux et dans les options. En cas de collision —
    ou de nom sans aucun caractère translittérable — un suffixe aléatoire est
    ajouté, car l'identifiant ne doit jamais changer par la suite.
    """
    deja_pris = set(pris)
    base = slugify(nom) or "destination"
    if base not in deja_pris:
        return base
    while True:
        suffixe = secrets.token_hex(LONGUEUR_SUFFIXE_IDENTIFIANT // 2)
        candidat = f"{base}_{suffixe}"
        if candidat not in deja_pris:
            return candidat


class GestionDesDestinationsMixin:
    """Étapes du flux d'options propres aux destinations distantes.

    Le mélange est appliqué à la classe upstream par
    `etendre_le_flux_d_options()` : l'étape `init` reste **exactement** le
    formulaire upstream (`auto_purge`, `backup_timeout`), simplement atteinte
    depuis le menu au lieu d'être la première étape.
    """

    # Le point d'entrée du flux devient le menu ; `init` reste une étape.
    init_step = "menu"

    # État du parcours en cours. Ces attributs de classe (tous immuables) tiennent
    # lieu de valeurs par défaut : chaque flux écrit les siens sur son instance.
    _provider: str | None = None
    _destination_id: str | None = None
    _client_id: str | None = None
    _client_secret: str | None = None
    _token: dict[str, Any] | None = None
    _donnees_externes: dict[str, Any] | None = None
    _provider_data: dict[str, Any] | None = None
    _nom_suggere: str | None = None

    ### Lecture de l'existant ###

    def _configurations(self) -> list[DestinationConfig]:
        """Destinations actuellement persistées, les invalides mises de côté."""
        configurations: list[DestinationConfig] = []
        for brute in async_destination_configs(self.config_entry):
            try:
                configurations.append(DestinationConfig.from_dict(brute))
            except DestinationConfigError as err:
                _LOGGER.warning(
                    "Destination ignorée par le flux d'options, "
                    "configuration invalide : %s",
                    err,
                )
        return configurations

    def _configuration(self, destination_id: str) -> DestinationConfig | None:
        """Destination persistée portant cet identifiant, si elle existe."""
        return next(
            (
                config
                for config in self._configurations()
                if config.destination_id == destination_id
            ),
            None,
        )

    def _enregistrer(
        self, configurations: Iterable[DestinationConfig]
    ) -> ConfigFlowResult:
        """Clôt le flux en écrivant ces destinations dans les options."""
        try:
            options = options_avec_destinations(self.config_entry, configurations)
        except DestinationConfigError as err:
            _LOGGER.error("Destinations non enregistrées : %s", err)
            return self.async_abort(reason="configuration_invalide")
        return self.async_create_entry(data=options)

    ### Menu ###

    async def async_step_menu(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Propose la gestion des destinations et les réglages upstream."""
        oublier_les_etats_du_flux(self.hass, self.flow_id)
        options = ["ajouter_destination"]
        if self._configurations():
            options += ["reautoriser_destination", "supprimer_destination"]
        options.append("init")
        return self.async_show_menu(step_id="menu", menu_options=options)

    ### Ajout d'une destination ###

    async def async_step_ajouter_destination(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choix du fournisseur parmi ceux enregistrés dans le registre."""
        fournisseurs = list_providers()
        if not fournisseurs:
            return self.async_abort(reason="aucun_fournisseur")

        if user_input is not None:
            self._destination_id = None
            self._token = None
            self._provider_data = None
            self._nom_suggere = None
            self._provider = user_input[CONF_PROVIDER]
            try:
                spec = spec_oauth_du_fournisseur(self._provider)
            except UnknownProviderError:
                # Le registre a changé pendant que le formulaire était ouvert.
                return self.async_abort(reason="fournisseur_invalide")
            if spec is None:
                return await self.async_step_destination()
            return await self.async_step_identifiants()

        schema = vol.Schema(
            {
                vol.Required(CONF_PROVIDER): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            SelectOptionDict(
                                value=identifiant,
                                label=_libelle_du_fournisseur(identifiant),
                            )
                            for identifiant in fournisseurs
                        ],
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                )
            }
        )
        return self.async_show_form(step_id="ajouter_destination", data_schema=schema)

    async def async_step_identifiants(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Saisie des identifiants de l'application OAuth2 de l'utilisateur.

        Le secret est saisi dans un champ masqué et n'est jamais réaffiché : le
        formulaire ne propose que l'identifiant client comme valeur suggérée.
        """
        try:
            retour = url_de_retour(self.hass)
        except NoURLAvailableError:
            return self.async_abort(reason="url_indisponible")

        erreurs: dict[str, str] = {}
        if user_input is not None:
            client_id = str(user_input.get(CONF_CLIENT_ID, "")).strip()
            client_secret = str(user_input.get(CONF_CLIENT_SECRET, "")).strip()
            if not client_id or not client_secret:
                erreurs["base"] = "identifiants_invalides"
            else:
                self._client_id = client_id
                self._client_secret = client_secret
                return await self.async_step_autorisation()

        schema = vol.Schema(
            {
                vol.Required(CONF_CLIENT_ID): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.TEXT)
                ),
                vol.Required(CONF_CLIENT_SECRET): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.PASSWORD)
                ),
            }
        )
        if self._client_id:
            schema = self.add_suggested_values_to_schema(
                schema, {CONF_CLIENT_ID: self._client_id}
            )
        return self.async_show_form(
            step_id="identifiants",
            data_schema=schema,
            errors=erreurs,
            description_placeholders={
                "url_de_retour": retour,
                "fournisseur": self._provider or "",
            },
        )

    async def async_step_autorisation(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Étape externe : l'utilisateur autorise l'accès chez le fournisseur.

        Le second appel vient de la vue de retour (`destinations/oauth.py`), qui
        transmet le code d'autorisation ou l'erreur renvoyée par le fournisseur.
        """
        if user_input is not None:
            self._donnees_externes = dict(user_input)
            suivant = "autorisation_refusee" if "error" in user_input else "jeton"
            return self.async_external_step_done(next_step_id=suivant)

        if not async_enregistrer_la_vue_de_retour(self.hass):
            return self.async_abort(reason="vue_indisponible")

        try:
            implementation = self._implementation()
        except (DestinationConfigError, UnknownProviderError) as err:
            _LOGGER.error("Autorisation impossible : %s", err)
            return self.async_abort(reason="fournisseur_invalide")

        try:
            async with asyncio.timeout(OAUTH_AUTHORIZE_URL_TIMEOUT):
                url = await implementation.async_generate_authorize_url(self.flow_id)
        except TimeoutError:
            return self.async_abort(reason="delai_url_autorisation")
        except NoURLAvailableError:
            return self.async_abort(reason="url_indisponible")

        return self.async_external_step(step_id="autorisation", url=url)

    async def async_step_autorisation_refusee(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Le fournisseur a refusé ou l'utilisateur a annulé l'autorisation."""
        erreur = (self._donnees_externes or {}).get("error", "inconnue")
        return self.async_abort(
            reason="autorisation_refusee",
            description_placeholders={"erreur": str(erreur)},
        )

    async def async_step_jeton(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Échange le code d'autorisation contre un jeton.

        Ni le code ni le jeton ne sont journalisés : seules les causes d'échec
        le sont, et elles ne contiennent aucun secret.
        """
        try:
            implementation = self._implementation()
        except (DestinationConfigError, UnknownProviderError) as err:
            _LOGGER.error("Échange du code impossible : %s", err)
            return self.async_abort(reason="fournisseur_invalide")

        try:
            async with asyncio.timeout(OAUTH_TOKEN_TIMEOUT):
                brut = await implementation.async_resolve_external_data(
                    self._donnees_externes
                )
        except TimeoutError:
            return self.async_abort(reason="delai_jeton")
        except OAuth2TokenRequestReauthError:
            _LOGGER.error("Le fournisseur a refusé le code d'autorisation")
            return self.async_abort(reason="autorisation_non_accordee")
        except (OAuth2TokenRequestError, ClientError) as err:
            _LOGGER.error("Échec de l'obtention du jeton : %s", err)
            return self.async_abort(reason="echec_jeton")

        try:
            self._token = normaliser_le_jeton(brut)
        except DestinationConfigError as err:
            _LOGGER.error("Jeton inexploitable : %s", err)
            return self.async_abort(reason="jeton_invalide")

        try:
            await self._async_interroger_le_fournisseur()
        except DestinationError as err:
            _LOGGER.error("Le fournisseur a refusé la première requête : %s", err)
            return self.async_abort(
                reason="echec_fournisseur",
                description_placeholders={"detail": str(err)},
            )

        if self._destination_id is not None:
            return self._terminer_la_reautorisation()
        return await self.async_step_destination()

    async def async_step_destination(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Nom, dossier distant et rétention de la nouvelle destination."""
        erreurs: dict[str, str] = {}
        configurations = self._configurations()

        if user_input is not None:
            nom = str(user_input.get(CONF_NAME, "")).strip()
            if not nom:
                erreurs[CONF_NAME] = "nom_invalide"
            elif any(
                config.name.casefold() == nom.casefold() for config in configurations
            ):
                erreurs[CONF_NAME] = "nom_deja_utilise"

            dossier = str(user_input.get(CONF_FOLDER, DEFAULT_DESTINATION_FOLDER))
            try:
                chemin_de_dossier(dossier)
            except vol.Invalid as err:
                _LOGGER.debug("Dossier distant refusé : %s", err)
                erreurs[CONF_FOLDER] = "dossier_invalide"

            if not erreurs:
                try:
                    nouvelle = self._construire(nom, user_input, configurations)
                except DestinationConfigError as err:
                    _LOGGER.debug("Destination refusée : %s", err)
                    erreurs["base"] = "destination_invalide"
                else:
                    return self._enregistrer([*configurations, nouvelle])

        schema = vol.Schema(
            {
                vol.Required(CONF_NAME): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.TEXT)
                ),
                vol.Required(
                    CONF_FOLDER, default=DEFAULT_DESTINATION_FOLDER
                ): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
                vol.Optional(CONF_RETENTION_DAYS): NumberSelector(
                    NumberSelectorConfig(
                        min=1,
                        max=RETENTION_JOURS_MAX,
                        step=1,
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Optional(CONF_RETENTION_COUNT): NumberSelector(
                    NumberSelectorConfig(
                        min=1,
                        max=RETENTION_NOMBRE_MAX,
                        step=1,
                        mode=NumberSelectorMode.BOX,
                    )
                ),
            }
        )
        if user_input is not None:
            schema = self.add_suggested_values_to_schema(schema, user_input)
        elif self._nom_suggere:
            # Nom proposé par le fournisseur d'après le compte autorisé : il
            # reste modifiable, et l'unicité est vérifiée à la validation.
            schema = self.add_suggested_values_to_schema(
                schema, {CONF_NAME: self._nom_suggere}
            )
        return self.async_show_form(
            step_id="destination",
            data_schema=schema,
            errors=erreurs,
            description_placeholders={"fournisseur": self._provider or ""},
        )

    def _construire(
        self,
        nom: str,
        user_input: Mapping[str, Any],
        configurations: Iterable[DestinationConfig],
    ) -> DestinationConfig:
        """Assemble la configuration de la destination en cours d'ajout."""
        return DestinationConfig(
            destination_id=_identifiant_disponible(
                nom, [config.destination_id for config in configurations]
            ),
            provider=str(self._provider),
            name=nom,
            folder=str(user_input.get(CONF_FOLDER, DEFAULT_DESTINATION_FOLDER)),
            retention_days=_retention(user_input, CONF_RETENTION_DAYS),
            retention_count=_retention(user_input, CONF_RETENTION_COUNT),
            client_id=self._client_id,
            client_secret=self._client_secret,
            token=self._token,
            provider_data=self._provider_data,
        )

    ### Ré-autorisation d'une destination existante ###

    async def async_step_reautoriser_destination(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Relance l'autorisation d'une destination, elle seule."""
        candidates = [
            config for config in self._configurations() if self._est_oauth(config)
        ]
        if not candidates:
            return self.async_abort(reason="aucune_destination_oauth")

        if user_input is not None:
            config = self._configuration(user_input[CONF_DESTINATION_ID])
            if config is None:
                return self.async_abort(reason="destination_inconnue")
            self._destination_id = config.destination_id
            self._provider = config.provider
            self._client_id = config.client_id
            self._client_secret = config.client_secret
            self._token = None
            self._provider_data = None
            self._nom_suggere = None
            if not config.utilise_oauth:
                # Identifiants d'application perdus (options éditées à la main) :
                # ils sont redemandés avant de repartir chez le fournisseur.
                return await self.async_step_identifiants()
            return await self.async_step_autorisation()

        return self.async_show_form(
            step_id="reautoriser_destination",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_DESTINATION_ID): SelectSelector(
                        SelectSelectorConfig(
                            options=self._choix_de_destinations(candidates),
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
        )

    def _terminer_la_reautorisation(self) -> ConfigFlowResult:
        """Remplace le jeton de la destination ré-autorisée et clôt le flux."""
        configurations = self._configurations()
        identifiant = str(self._destination_id)
        if not any(config.destination_id == identifiant for config in configurations):
            return self.async_abort(reason="destination_inconnue")

        mises_a_jour = [
            DestinationConfig(
                destination_id=config.destination_id,
                provider=config.provider,
                name=config.name,
                folder=config.folder,
                retention_days=config.retention_days,
                retention_count=config.retention_count,
                client_id=self._client_id or config.client_id,
                client_secret=self._client_secret or config.client_secret,
                token=self._token,
                # Le compte peut avoir changé : les données fraîchement lues
                # priment, celles d'origine servent de repli.
                provider_data=self._provider_data or config.provider_data,
            )
            if config.destination_id == identifiant
            else config
            for config in configurations
        ]
        async_effacer_la_reauthentification(self.hass, identifiant)
        return self._enregistrer(mises_a_jour)

    ### Suppression ###

    async def async_step_supprimer_destination(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Supprime une ou plusieurs destinations, jeton compris."""
        configurations = self._configurations()
        if not configurations:
            return self.async_abort(reason="aucune_destination")

        erreurs: dict[str, str] = {}
        if user_input is not None:
            choisies = set(user_input.get(CONF_DESTINATIONS) or [])
            if not choisies:
                erreurs["base"] = "aucune_selection"
            else:
                restantes = [
                    config
                    for config in configurations
                    if config.destination_id not in choisies
                ]
                for identifiant in choisies:
                    async_effacer_la_reauthentification(self.hass, identifiant)
                _LOGGER.info(
                    "Suppression de %s destination(s) depuis les options",
                    len(choisies),
                )
                return self._enregistrer(restantes)

        return self.async_show_form(
            step_id="supprimer_destination",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_DESTINATIONS): SelectSelector(
                        SelectSelectorConfig(
                            options=self._choix_de_destinations(configurations),
                            mode=SelectSelectorMode.LIST,
                            multiple=True,
                        )
                    )
                }
            ),
            errors=erreurs,
        )

    ### Outils communs ###

    @staticmethod
    def _choix_de_destinations(
        configurations: Iterable[DestinationConfig],
    ) -> list[SelectOptionDict]:
        """Options d'un sélecteur : identifiant technique, libellé lisible."""
        return [
            SelectOptionDict(
                value=config.destination_id,
                label=f"{config.name} ({_libelle_du_fournisseur(config.provider)})",
            )
            for config in configurations
        ]

    @staticmethod
    def _est_oauth(config: DestinationConfig) -> bool:
        """Indique si le fournisseur de cette destination utilise OAuth2."""
        try:
            return spec_oauth_du_fournisseur(config.provider) is not None
        except UnknownProviderError:
            return False

    def _implementation(self) -> DestinationOAuth2Implementation:
        """Implémentation OAuth2 du parcours en cours.

        La configuration utilisée est celle de la destination visée quand il
        s'agit d'une ré-autorisation ; sinon une configuration provisoire, car à
        l'ajout la destination n'existe pas encore au moment de l'autorisation.
        """
        config: DestinationConfig | None = None
        if self._destination_id is not None:
            config = self._configuration(self._destination_id)
        if config is None:
            config = DestinationConfig(
                destination_id=IDENTIFIANT_PROVISOIRE,
                provider=str(self._provider),
                name="Autorisation en cours",
            )
        return implementation_de_la_destination(
            self.hass,
            config,
            client_id=self._client_id,
            client_secret=self._client_secret,
        )

    def _session_provisoire(self) -> DestinationOAuth2Session:
        """Session portant le jeton qui vient d'être obtenu, avant persistance.

        L'identifiant provisoire est délibéré : une session construite sur la
        destination visée relirait le jeton **persisté**, c'est-à-dire l'ancien
        lors d'une ré-autorisation. Avec un identifiant qui n'existe dans aucune
        entrée, la session se rabat sur le jeton de la configuration, le neuf.
        """
        config = DestinationConfig(
            destination_id=IDENTIFIANT_PROVISOIRE,
            provider=str(self._provider),
            name="Autorisation en cours",
            client_id=self._client_id,
            client_secret=self._client_secret,
            token=self._token,
        )
        return async_session_de_la_destination(self.hass, config)

    async def _async_interroger_le_fournisseur(self) -> None:
        """Demande au fournisseur ce qu'il sait du compte qui vient d'autoriser.

        Deux crochets facultatifs sont cherchés sur la fabrique du fournisseur :
        `async_donnees_du_fournisseur()`, dont le résultat est persisté avec la
        destination, et `async_nom_par_defaut()`, qui fournit le nom proposé
        dans le formulaire suivant. Un fournisseur qui n'en expose aucun ne
        déclenche aucun appel réseau, et le parcours reste celui de l'issue #7.

        Les deux crochets interrogent le fournisseur chacun de leur côté plutôt
        que de partager une réponse : ils restent ainsi indépendants, et cet
        aller-retour supplémentaire n'a lieu qu'une fois, dans un parcours
        interactif.

        Les erreurs remontent telles quelles : l'appelant interrompt le flux en
        citant la cause, car une destination qu'on ne peut même pas interroger
        ne fonctionnerait pas davantage une fois créée. C'est aussi le cas d'un
        fournisseur disparu du registre entre-temps : `UnknownProviderError` est
        une `DestinationError`, et l'appelant la traite comme les autres.
        """
        fabrique = get_provider(str(self._provider))

        crochet_donnees = getattr(fabrique, CROCHET_DONNEES_DU_FOURNISSEUR, None)
        crochet_nom = getattr(fabrique, CROCHET_NOM_PAR_DEFAUT, None)
        if crochet_donnees is None and crochet_nom is None:
            return

        session = self._session_provisoire()
        if crochet_donnees is not None:
            donnees = await crochet_donnees(session)
            self._provider_data = dict(donnees) if donnees else None
        if crochet_nom is not None:
            self._nom_suggere = await crochet_nom(session)


def etendre_le_flux_d_options(base: type) -> type:
    """Renvoie le flux d'options upstream augmenté de la gestion des destinations.

    La sous-classe est construite ici plutôt que déclarée statiquement pour que
    ce module n'importe jamais `config_flow.py` : l'import inverse
    (`config_flow.py` -> ce module) reste donc le seul, et une resynchronisation
    upstream n'a rien à reporter ici.
    """
    return type(base.__name__, (GestionDesDestinationsMixin, base), {})
