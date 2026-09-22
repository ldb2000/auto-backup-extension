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
     -> jeton -> [description du compte] -> destination -> options enregistrées
```

La description du compte (issue #10) est facultative : le fournisseur peut
proposer un nom par défaut (« Dropbox - Jean Dupont ») et des données à
persister (identifiant de compte). Un fournisseur qui ne le fait pas, ou dont
l'appel échoue, laisse simplement le formulaire de nommage vide.

L'étape externe est celle de Home Assistant (`async_external_step`) ; c'est la
vue du fork (`destinations/oauth.py`) qui reprend le flux, la vue standard ne
sachant reprendre qu'un config flow.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Iterable, Mapping
from dataclasses import replace
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
    async_enregistrer_la_vue_de_retour,
    implementation_de_la_destination,
    normaliser_le_jeton,
    oublier_les_etats_du_flux,
    spec_oauth_du_fournisseur,
    url_de_retour,
)
from .reauth import async_effacer_la_reauthentification
from .registry import create_destination, list_providers, provider_label
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

# Codes d'erreur OAuth2 (RFC 6749 §4.1.2.1) auxquels le fork sait répondre par un
# message compréhensible plutôt que par le code brut. `access_denied` est celui
# que renvoient Dropbox (#10) comme Google Drive (#13) quand l'utilisateur ferme
# la page d'autorisation ou refuse l'accès : le lui montrer tel quel n'apprend
# rien. Tout autre code reste rendu par le message générique, qui le cite.
MOTIFS_DE_REFUS = {"access_denied": "autorisation_annulee"}


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
    _nom_propose: str | None = None
    _provider_data: dict[str, Any] | None = None

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
            self._nom_propose = None
            self._provider_data = None
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
                                label=provider_label(identifiant),
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
                "fournisseur": self._libelle_du_fournisseur(),
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
        erreur = str((self._donnees_externes or {}).get("error", "inconnue"))
        if (motif := MOTIFS_DE_REFUS.get(erreur)) is not None:
            return self.async_abort(reason=motif)
        return self.async_abort(
            reason="autorisation_refusee",
            description_placeholders={"erreur": erreur},
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

        if self._destination_id is not None:
            return self._terminer_la_reautorisation()

        await self._async_decrire_le_compte()
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
        elif self._nom_propose:
            schema = self.add_suggested_values_to_schema(
                schema, {CONF_NAME: self._nom_propose}
            )
        return self.async_show_form(
            step_id="destination",
            data_schema=schema,
            errors=erreurs,
            description_placeholders={"fournisseur": self._libelle_du_fournisseur()},
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

    ### Description du compte autorisé (issue #10) ###

    async def _async_decrire_le_compte(self) -> None:
        """Demande au fournisseur un nom par défaut et les données du compte.

        Les deux crochets sont facultatifs (`RemoteDestination`) : un fournisseur
        qui ne les surcharge pas ne provoque aucun appel réseau. Un échec — compte
        injoignable, réponse inattendue, accès déjà refusé — n'interrompt pas
        l'ajout : l'utilisateur nomme alors sa destination lui-même, plutôt que de
        perdre une autorisation qu'il vient d'accorder.
        """
        self._nom_propose = None
        self._provider_data = None

        try:
            destination = create_destination(self.hass, self._config_provisoire())
        except DestinationConfigError as err:
            _LOGGER.debug("Compte du fournisseur non décrit : %s", err)
            return

        try:
            nom = await destination.async_nom_par_defaut()
            donnees = await destination.async_donnees_du_fournisseur()
        except (DestinationError, ClientError, TimeoutError) as err:
            _LOGGER.warning(
                "Le compte du fournisseur « %s » n'a pas pu être identifié : %s",
                self._provider,
                err,
            )
            return
        finally:
            # Un accès refusé ici (portée oubliée, jeton déjà révoqué) signalerait
            # la destination **provisoire** à ré-autoriser : le problème créé
            # nommerait une destination qui n'existe pas, que l'utilisateur ne
            # pourrait donc ni ré-autoriser ni supprimer. Il est effacé aussitôt.
            async_effacer_la_reauthentification(self.hass, IDENTIFIANT_PROVISOIRE)

        propose = nom.strip() if isinstance(nom, str) else ""
        self._nom_propose = propose or None
        self._provider_data = dict(donnees) if donnees else None

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
        """Remplace le jeton de la destination ré-autorisée et clôt le flux.

        Seuls les trois champs d'autorisation changent : la destination est
        recopiée par `dataclasses.replace()` plutôt que reconstruite champ par
        champ, pour que rien d'autre ne puisse être perdu en chemin. Une
        reconstruction manuelle avait déjà effacé `provider_data` (le compte
        rattaché à la destination), et aurait effacé de la même façon tout champ
        ajouté plus tard à `DestinationConfig`.
        """
        configurations = self._configurations()
        identifiant = str(self._destination_id)
        if not any(config.destination_id == identifiant for config in configurations):
            return self.async_abort(reason="destination_inconnue")

        mises_a_jour = [
            replace(
                config,
                client_id=self._client_id or config.client_id,
                client_secret=self._client_secret or config.client_secret,
                token=self._token,
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
                label=f"{config.name} ({config.provider})",
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

    def _libelle_du_fournisseur(self) -> str:
        """Nom lisible du fournisseur en cours (« Dropbox »), pour l'affichage."""
        if not self._provider:
            return ""
        try:
            return provider_label(self._provider)
        except UnknownProviderError:
            return self._provider

    def _config_provisoire(self) -> DestinationConfig:
        """Configuration de travail de la destination en cours d'ajout.

        Elle porte le jeton fraîchement obtenu mais n'est pas persistée : elle
        permet d'instancier le fournisseur avant que l'utilisateur n'ait nommé sa
        destination. La session OAuth2 ne trouvant aucun jeton persisté sous cet
        identifiant, elle utilise celui-ci.
        """
        return DestinationConfig(
            destination_id=IDENTIFIANT_PROVISOIRE,
            provider=str(self._provider),
            name="Autorisation en cours",
            client_id=self._client_id,
            client_secret=self._client_secret,
            token=self._token,
        )

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
            config = self._config_provisoire()
        return implementation_de_la_destination(
            self.hass,
            config,
            client_id=self._client_id,
            client_secret=self._client_secret,
        )


def etendre_le_flux_d_options(base: type) -> type:
    """Renvoie le flux d'options upstream augmenté de la gestion des destinations.

    La sous-classe est construite ici plutôt que déclarée statiquement pour que
    ce module n'importe jamais `config_flow.py` : l'import inverse
    (`config_flow.py` -> ce module) reste donc le seul, et une resynchronisation
    upstream n'a rien à reporter ici.
    """
    return type(base.__name__, (GestionDesDestinationsMixin, base), {})
