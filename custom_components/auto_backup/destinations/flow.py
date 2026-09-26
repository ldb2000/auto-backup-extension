"""Gestion des destinations distantes depuis le flux d'options (issue #7).

Ce module apporte au flux d'options upstream un menu et les étapes d'ajout, de
ré-autorisation et de suppression d'une destination, ainsi que — depuis
l'issue #8 — les réglages du téléversement (délai maximum) et — depuis l'issue
#17 — ceux des notifications d'échec. Il n'existe pas dans
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

La description du compte (issues #10 et #13) est facultative : le fournisseur
peut proposer un nom par défaut (« Dropbox - Jean Dupont ») et des données à
persister (identifiant ou adresse du compte). Un fournisseur qui ne surcharge
aucun des deux crochets de `RemoteDestination` suit exactement le parcours
d'origine, sans le moindre appel réseau supplémentaire.

Les deux crochets — `async_nom_par_defaut()` et `async_donnees_du_fournisseur()`
— sont des **méthodes d'instance** appelées sur une seule destination
provisoire, construite par `create_destination()` : un fournisseur qui mémorise
la réponse du service n'a donc qu'un aller-retour réseau à faire pour les deux.

Leur échec **interrompt** l'ajout (abandon `echec_fournisseur`, le détail
citant la cause : API non activée, portée manquante, compte injoignable). Une
destination que le fournisseur refuse déjà d'identifier ne fonctionnerait pas
davantage une fois créée : mieux vaut le dire tout de suite que laisser
l'utilisateur découvrir la panne à la première sauvegarde.

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
    BooleanSelector,
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
    CONF_NOTIFY_ON_FAILURE,
    CONF_PROVIDER,
    CONF_RETENTION_COUNT,
    CONF_RETENTION_DAYS,
    CONF_UPLOAD_TIMEOUT,
    DEFAULT_DESTINATION_FOLDER,
    DEFAULT_NOTIFY_ON_FAILURE,
    DEFAULT_UPLOAD_TIMEOUT,
    IDENTIFIANT_PROVISOIRE,
    OAUTH_AUTHORIZE_URL_TIMEOUT,
    OAUTH_TOKEN_TIMEOUT,
)
from .config_entry import (
    async_destination_configs,
    options_avec_destinations,
    options_avec_reglage,
)
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

# Clés de `provider_data` qui **identifient un compte** chez un fournisseur :
# `account_id` (Dropbox, #10), `account_email` (Google Drive, #13), `email` pour
# un fournisseur qui n'en renverrait que l'adresse. Elles servent à reconnaître
# qu'une ré-autorisation a changé de compte ; les autres données du fournisseur
# (quota, nom d'affichage) changent sans que le compte change.
CLES_IDENTIFIANTES_DU_COMPTE = ("account_id", "account_email", "email")

# Case à cocher de l'étape de confirmation d'un changement de compte. Ce n'est
# pas une option de l'entrée : elle ne vit que le temps du formulaire.
CONF_CONFIRMER = "confirmer"

# Comptes qu'aucune donnée de fournisseur ne décrit : affiché tel quel dans
# l'étape de confirmation, qui doit bien nommer les deux côtés.
COMPTE_INCONNU = "compte non identifié"

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


def _libelle_du_fournisseur(provider_id: str) -> str:
    """Libellé lisible d'un fournisseur, ou son identifiant technique à défaut.

    Le libellé est déclaré par le fournisseur lui-même (`RemoteDestination.LABEL`,
    issue #10) et lu par le registre. Un fournisseur qui n'en déclare pas — ou qui
    a disparu du registre pendant que le formulaire était ouvert — reste affiché
    sous son identifiant, faute de mieux.
    """
    try:
        return provider_label(provider_id)
    except UnknownProviderError:
        return provider_id


def _delai_de_televersement(valeur: Any) -> int:
    """Convertit le délai saisi en secondes entières, ou lève.

    Le sélecteur numérique de Home Assistant renvoie un flottant, et ne borne
    volontairement pas la saisie : la contrainte — un entier **strictement
    positif** — est vérifiée ici, en un seul endroit, pour que l'utilisateur
    reçoive un message explicite plutôt qu'un refus générique du schéma. Un
    délai nul ou négatif couperait tout téléversement avant même qu'il commence.
    """
    try:
        secondes = int(float(valeur))
    except (TypeError, ValueError, OverflowError) as err:
        # OverflowError : un flottant infini ne se convertit pas en entier.
        raise DestinationConfigError(
            f"{CONF_UPLOAD_TIMEOUT} doit être un nombre de secondes"
        ) from err
    if secondes <= 0:
        raise DestinationConfigError(
            f"{CONF_UPLOAD_TIMEOUT} doit être strictement positif"
        )
    return secondes


def _identite_du_compte(donnees: Mapping[str, Any] | None) -> dict[str, str]:
    """Réduit des données de fournisseur aux clés qui identifient le compte.

    Les valeurs vides et les clés non identifiantes sont écartées : deux
    autorisations du même compte doivent donner exactement le même résultat,
    même si le fournisseur enrichit sa réponse entre-temps.
    """
    if not donnees:
        return {}
    identite: dict[str, str] = {}
    for cle in CLES_IDENTIFIANTES_DU_COMPTE:
        valeur = donnees.get(cle)
        if isinstance(valeur, str) and valeur.strip():
            identite[cle] = valeur.strip()
    return identite


def _description_du_compte(identite: Mapping[str, str]) -> str:
    """Décrit un compte pour l'utilisateur, ou le dit non identifié."""
    return " / ".join(identite.values()) or COMPTE_INCONNU


def _memes_comptes(ancienne: Mapping[str, str], nouvelle: Mapping[str, str]) -> bool:
    """Indique si deux identités décrivent le même compte.

    La comparaison porte sur les clés **communes** : un fournisseur qui se met
    à renvoyer une donnée de plus ne doit pas faire croire à un changement de
    compte. Sans aucune clé commune, rien ne permet d'affirmer que c'est le même
    compte : la question est posée à l'utilisateur plutôt que tranchée ici.
    """
    communes = ancienne.keys() & nouvelle.keys()
    if not communes:
        return False
    return all(ancienne[cle] == nouvelle[cle] for cle in communes)


def _identifiant_disponible(nom: str, pris: Iterable[str]) -> str:
    """Fabrique un identifiant stable, lisible et unique dans l'entrée.

    L'identifiant dérive du nom choisi (`Mon Dropbox` -> `mon_dropbox`) : il est
    reconnaissable dans les journaux et dans les options. En cas de collision —
    ou de nom sans aucun caractère translittérable — un suffixe aléatoire est
    ajouté, car l'identifiant ne doit jamais changer par la suite.

    `IDENTIFIANT_PROVISOIRE` est traité comme déjà pris : une destination nommée
    « Autorisation en cours » se translittérerait sinon exactement comme la
    destination fictive du flux d'ajout, et le nettoyage de celle-ci
    (`_async_decrire_le_compte()`) effacerait alors le signalement de
    ré-authentification d'une destination bien réelle.
    """
    deja_pris = {*pris, IDENTIFIANT_PROVISOIRE}
    base = slugify(nom) or "destination"
    if base not in deja_pris:
        return base
    while True:
        suffixe = secrets.token_hex(LONGUEUR_SUFFIXE_IDENTIFIANT // 2)
        candidat = f"{base}_{suffixe}"
        if candidat not in deja_pris:
            return candidat


class GestionDesDestinationsMixin:
    """Étapes du flux d'options propres au fork.

    Le mélange est appliqué à la classe upstream par
    `etendre_le_flux_d_options()` : l'étape `init` reste **exactement** le
    formulaire upstream (`auto_purge`, `backup_timeout`), simplement atteinte
    depuis le menu au lieu d'être la première étape. Les réglages propres au
    fork — aujourd'hui le délai de téléversement — ont leur propre étape, pour
    que le schéma upstream reste intact.
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
        options += ["reglages_televersement", "reglages_notifications", "init"]
        return self.async_show_menu(step_id="menu", menu_options=options)

    ### Réglages du téléversement ###

    async def async_step_reglages_televersement(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Délai maximum accordé à l'envoi d'une sauvegarde, en secondes.

        Le réglage vit dans les options de l'entrée, aux côtés de ceux de
        l'upstream ; `CoordinateurTeleversement` le relit à chaque
        téléversement, il s'applique donc sans redémarrage. Les autres options
        — destinations comprises — sont reconduites telles quelles.
        """
        erreurs: dict[str, str] = {}
        propose: Any = self.config_entry.options.get(
            CONF_UPLOAD_TIMEOUT, DEFAULT_UPLOAD_TIMEOUT
        )

        if user_input is not None:
            try:
                secondes = _delai_de_televersement(user_input.get(CONF_UPLOAD_TIMEOUT))
            except DestinationConfigError as err:
                _LOGGER.debug("Délai de téléversement refusé : %s", err)
                erreurs[CONF_UPLOAD_TIMEOUT] = "delai_invalide"
                propose = user_input.get(CONF_UPLOAD_TIMEOUT, propose)
            else:
                _LOGGER.info("Délai maximum d'un téléversement : %s s", secondes)
                return self.async_create_entry(
                    data=options_avec_reglage(
                        self.config_entry, CONF_UPLOAD_TIMEOUT, secondes
                    )
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_UPLOAD_TIMEOUT): NumberSelector(
                    NumberSelectorConfig(
                        step=1,
                        mode=NumberSelectorMode.BOX,
                        unit_of_measurement="s",
                    )
                )
            }
        )
        return self.async_show_form(
            step_id="reglages_televersement",
            data_schema=self.add_suggested_values_to_schema(
                schema, {CONF_UPLOAD_TIMEOUT: propose}
            ),
            errors=erreurs,
        )

    ### Réglages des notifications (issue #17) ###

    async def async_step_reglages_notifications(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Notifications persistantes des échecs de destination, ou silence.

        Le réglage ne porte que sur les **notifications** : désactivé, un échec
        de téléversement reste journalisé et signalé par l'événement
        `auto_backup.upload_failed`, et une destination dont l'accès est révoqué
        reste signalée par un problème Home Assistant. Les automatisations
        existantes ne changent donc pas de comportement.
        """
        if user_input is not None:
            actives = bool(
                user_input.get(CONF_NOTIFY_ON_FAILURE, DEFAULT_NOTIFY_ON_FAILURE)
            )
            _LOGGER.info(
                "Notifications persistantes des échecs de destination : %s",
                "activées" if actives else "désactivées",
            )
            return self.async_create_entry(
                data=options_avec_reglage(
                    self.config_entry, CONF_NOTIFY_ON_FAILURE, actives
                )
            )

        schema = vol.Schema({vol.Required(CONF_NOTIFY_ON_FAILURE): BooleanSelector()})
        return self.async_show_form(
            step_id="reglages_notifications",
            data_schema=self.add_suggested_values_to_schema(
                schema,
                {
                    CONF_NOTIFY_ON_FAILURE: self.config_entry.options.get(
                        CONF_NOTIFY_ON_FAILURE, DEFAULT_NOTIFY_ON_FAILURE
                    )
                },
            ),
        )

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

        try:
            await self._async_decrire_le_compte()
        except DestinationError as err:
            _LOGGER.error("Le fournisseur a refusé la première requête : %s", err)
            return self.async_abort(
                reason="echec_fournisseur",
                description_placeholders={"detail": str(err)},
            )

        if self._destination_id is not None:
            # La ré-autorisation peut avoir porté sur un autre compte : l'étape
            # de confirmation le vérifie et ne s'affiche que dans ce cas (#17).
            return await self.async_step_confirmer_changement_de_compte()

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
            # Nom proposé par le fournisseur d'après le compte autorisé : il
            # reste modifiable, et l'unicité est vérifiée à la validation.
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

    ### Description du compte autorisé (issues #10 et #13) ###

    async def _async_decrire_le_compte(self) -> None:
        """Demande au fournisseur un nom par défaut et les données du compte.

        Les deux crochets sont déclarés par `RemoteDestination` et facultatifs :
        un fournisseur qui ne les surcharge pas renvoie `None` des deux côtés et
        ne provoque aucun appel réseau. Ils sont appelés sur **une seule**
        destination provisoire : un fournisseur qui mémorise la réponse du
        service n'a qu'un aller-retour à faire pour les deux.

        Un échec — compte injoignable, API non activée, portée manquante, accès
        déjà refusé — est **propagé** : l'appelant interrompt l'ajout en citant
        la cause. Une destination que le fournisseur refuse déjà d'identifier ne
        fonctionnerait pas davantage une fois créée, et l'utilisateur corrige
        immédiatement ce qu'un message d'abandon lui désigne. `UnknownProviderError`
        (fournisseur disparu du registre entre-temps) est une `DestinationError` :
        elle suit le même chemin.

        La méthode est aussi jouée lors d'une ré-autorisation, pour rafraîchir
        les données du compte : elles peuvent désigner un autre compte qu'à
        l'autorisation initiale.
        """
        self._nom_propose = None
        self._provider_data = None
        destination = create_destination(self.hass, self._config_provisoire())

        try:
            nom = await destination.async_nom_par_defaut()
            donnees = await destination.async_donnees_du_fournisseur()
        except (ClientError, TimeoutError) as err:
            raise DestinationError(
                f"le fournisseur « {self._provider} » n'a pas répondu : {err}"
            ) from err
        finally:
            # Un accès refusé ici (portée oubliée, jeton déjà révoqué) signalerait
            # la destination **provisoire** à ré-autoriser : le problème créé
            # nommerait une destination qui n'existe pas, que l'utilisateur ne
            # pourrait donc ni ré-autoriser ni supprimer. Il est effacé aussitôt,
            # que les crochets aient abouti ou non.
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
            self._provider_data = None
            self._nom_propose = None
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

    async def async_step_confirmer_changement_de_compte(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Fait valider un changement de compte avant de l'enregistrer (#17).

        Ré-autoriser sur un autre compte n'est pas une panne : c'est un choix
        légitime (compte professionnel devenu personnel, organisation migrée).
        Mais la destination continuerait de porter le même nom, tandis que les
        sauvegardes déposées sur l'ancien compte deviendraient invisibles pour
        Auto Backup : ni listées, ni purgées par la rétention distante. L'étape
        nomme donc les deux comptes et demande une confirmation explicite.

        Elle ne s'affiche que s'il y a vraiment changement : dans tous les
        autres cas — même compte, fournisseur muet, première donnée de compte —
        la ré-autorisation se termine sans rien demander.
        """
        config = self._configuration(str(self._destination_id))
        if config is None:
            # La destination a disparu pendant le flux : c'est
            # `_terminer_la_reautorisation()` qui porte ce refus, et lui seul,
            # pour que les deux chemins d'arrivée l'énoncent de la même façon.
            return self._terminer_la_reautorisation()

        changement = self._changement_de_compte(config)
        if changement is None:
            return self._terminer_la_reautorisation()

        if user_input is not None:
            if not user_input.get(CONF_CONFIRMER):
                _LOGGER.info(
                    "Ré-autorisation de « %s » abandonnée : le changement de "
                    "compte n'a pas été confirmé, rien n'a été enregistré",
                    config.name,
                )
                return self.async_abort(reason="changement_de_compte_annule")
            _LOGGER.warning(
                "La destination « %s » est désormais rattachée à un autre "
                "compte du fournisseur %s",
                config.name,
                config.provider,
            )
            return self._terminer_la_reautorisation()

        ancien, nouveau = changement
        return self.async_show_form(
            step_id="confirmer_changement_de_compte",
            data_schema=vol.Schema(
                {vol.Required(CONF_CONFIRMER, default=False): BooleanSelector()}
            ),
            description_placeholders={
                "destination": config.name,
                "ancien_compte": ancien,
                "nouveau_compte": nouveau,
            },
        )

    def _changement_de_compte(
        self, config: DestinationConfig
    ) -> tuple[str, str] | None:
        """Comptes (ancien, nouveau) si la ré-autorisation en a changé, sinon `None`.

        Trois situations ne sont **pas** un changement de compte :

        - le fournisseur n'a rien renvoyé (`self._provider_data` vide) : les
          données d'origine sont conservées, il n'y a rien à comparer ;
        - la destination n'avait encore aucune donnée de compte : le
          fournisseur vient de la renseigner, sans qu'on puisse dire qu'elle a
          changé de compte ;
        - les clés identifiantes communes portent les mêmes valeurs.
        """
        ancienne = _identite_du_compte(config.provider_data)
        nouvelle = _identite_du_compte(self._provider_data)
        if not ancienne or not nouvelle or _memes_comptes(ancienne, nouvelle):
            return None
        return (_description_du_compte(ancienne), _description_du_compte(nouvelle))

    def _terminer_la_reautorisation(self) -> ConfigFlowResult:
        """Remplace le jeton de la destination ré-autorisée et clôt le flux.

        Seuls les trois champs d'autorisation changent : la destination est
        recopiée par `dataclasses.replace()` plutôt que reconstruite champ par
        champ, pour que rien d'autre ne puisse être perdu en chemin. Une
        reconstruction manuelle avait déjà effacé `provider_data` (le compte
        rattaché à la destination), et aurait effacé de la même façon tout champ
        ajouté plus tard à `DestinationConfig`.

        Un changement de compte a, lui, déjà été confirmé par
        `async_step_confirmer_changement_de_compte()` (#17) : arrivé ici, le
        nouveau compte s'enregistre sans autre question.
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
                # Le compte peut avoir changé : les données fraîchement lues
                # priment. Un fournisseur qui n'en renvoie aucune laisse celles
                # d'origine en place — c'est un fournisseur muet, pas un compte
                # effacé. Le test est explicite (`is not None`) et non une
                # vérité booléenne : il ne dépend pas du fait qu'un dictionnaire
                # vide soit déjà ramené à `None` par `_async_decrire_le_compte()`.
                provider_data=(
                    self._provider_data
                    if self._provider_data is not None
                    else config.provider_data
                ),
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

    def _libelle_du_fournisseur(self) -> str:
        """Nom lisible du fournisseur en cours (« Dropbox »), pour l'affichage.

        Il alimente les placeholders `{fournisseur}` des étapes `identifiants` et
        `destination` : l'utilisateur n'y lit jamais `google_drive`.
        """
        if not self._provider:
            return ""
        return _libelle_du_fournisseur(self._provider)

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
