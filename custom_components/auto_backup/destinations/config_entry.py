"""Lien entre les destinations distantes et l'entrée de configuration.

Les destinations sont persistées dans `entry.options[CONF_DESTINATIONS]`, sous
la forme d'une liste de dictionnaires (cf. `docs/adr/0001-destinations-distantes.md`).
Ce module concentre toutes les lectures et écritures de cette liste.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_TOKEN
from homeassistant.core import HomeAssistant, callback

from ..const import (
    CLES_DU_FORK,
    CONF_AUTO_PURGE,
    CONF_BACKUP_TIMEOUT,
    CONF_DESTINATION_ID,
    CONF_DESTINATIONS,
    CONF_PROVIDER_DATA,
    CONF_UPLOAD_TIMEOUT,
    DATA_DESTINATIONS,
    DEFAULT_BACKUP_TIMEOUT,
    DEFAULT_UPLOAD_TIMEOUT,
    DOMAIN,
)
from .errors import DestinationConfigError, DestinationNotFoundError
from .manager import DestinationManager
from .models import DestinationConfig
from .providers import enregistrer_les_fournisseurs
from .schema import DESTINATIONS_SCHEMA, donnees_de_fournisseur

_LOGGER = logging.getLogger(__name__)


@callback
def async_destination_configs(entry: ConfigEntry) -> list[dict[str, Any]]:
    """Renvoie la liste brute des destinations persistées dans l'entrée."""
    return list(entry.options.get(CONF_DESTINATIONS, []))


@callback
def async_entree_auto_backup(hass: HomeAssistant) -> ConfigEntry | None:
    """Renvoie l'entrée de configuration d'Auto Backup, s'il y en a une.

    L'intégration est mono-entrée (le flux de configuration upstream refuse la
    seconde avec `single_instance`) : chercher l'entrée est donc sans ambiguïté,
    et cela évite de faire circuler l'objet `ConfigEntry` jusqu'aux fournisseurs.
    """
    entrees = hass.config_entries.async_entries(DOMAIN)
    return entrees[0] if entrees else None


@callback
def delai_de_televersement(entry: ConfigEntry) -> float:
    """Délai maximum d'un téléversement, en secondes, tel que l'entrée le règle.

    Point de lecture **unique** de l'option `upload_timeout`, partagé par le
    coordinateur — qui en fait le budget global d'un téléversement — et par les
    fournisseurs, qui en dérivent le garde-fou d'une requête isolée. Sans ce
    partage, un utilisateur relevant le réglage pour une connexion lente verrait
    une requête unique expirer avant que son budget global ne soit épuisé.

    L'option est relue à chaque appel : le réglage s'applique donc sans
    redémarrage. Une valeur inexploitable ou nulle retombe sur la valeur par
    défaut plutôt que de priver le téléversement de toute borne.
    """
    valeur = entry.options.get(CONF_UPLOAD_TIMEOUT, DEFAULT_UPLOAD_TIMEOUT)
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


@callback
def async_setup_destinations(
    hass: HomeAssistant, entry: ConfigEntry
) -> DestinationManager:
    """Charge les destinations de l'entrée et les expose dans `hass.data`.

    C'est **le point unique d'enregistrement des fournisseurs livrés** (Dropbox
    et Google Drive, issues #10 et #13) : le registre est peuplé avant toute
    lecture des destinations persistées, avant que la moindre destination ne
    soit instanciée et avant que le flux d'options ne propose un choix. L'appel
    est idempotent, l'entrée pouvant être rechargée autant de fois que
    nécessaire.

    Le gestionnaire obtenu suit les options de l'entrée : il se recharge quand
    elles changent et disparaît de `hass.data` au déchargement de l'entrée.
    """
    enregistrer_les_fournisseurs()

    manager = DestinationManager(hass)
    manager.async_load(async_destination_configs(entry))
    hass.data[DATA_DESTINATIONS] = manager

    @callback
    def retirer_le_gestionnaire() -> None:
        """Retire le gestionnaire de `hass.data` au déchargement de l'entrée."""
        hass.data.pop(DATA_DESTINATIONS, None)

    entry.async_on_unload(retirer_le_gestionnaire)
    entry.async_on_unload(entry.add_update_listener(manager.async_options_updated))
    return manager


@callback
def _options_completees(entry: ConfigEntry) -> dict[str, Any]:
    """Options de l'entrée, les options upstream manquantes étant complétées.

    L'écouteur de mise à jour upstream lit `entry.options["auto_purge"]` et
    `entry.options["backup_timeout"]` **sans valeur de repli** : toute écriture
    d'options doit donc les porter, y compris sur une entrée qui n'a jamais
    visité le formulaire de réglages.
    """
    return {
        CONF_AUTO_PURGE: entry.options.get(CONF_AUTO_PURGE, True),
        CONF_BACKUP_TIMEOUT: entry.options.get(
            CONF_BACKUP_TIMEOUT, DEFAULT_BACKUP_TIMEOUT
        ),
        **entry.options,
    }


@callback
def options_avec_destinations(
    entry: ConfigEntry, configs: Iterable[DestinationConfig]
) -> dict[str, Any]:
    """Construit les options de l'entrée portant exactement ces destinations.

    Rien n'est écrit ici : le flux d'options a besoin du dictionnaire pour le
    renvoyer à Home Assistant (`async_create_entry`), tandis que les appels
    internes passent par `async_persist_destinations()`.
    """
    configs = list(configs)
    identifiants = [config.destination_id for config in configs]
    doublons = {
        identifiant
        for identifiant in identifiants
        if identifiants.count(identifiant) > 1
    }
    if doublons:
        raise DestinationConfigError(
            f"identifiants de destination en double : {', '.join(sorted(doublons))}"
        )

    return {
        **_options_completees(entry),
        CONF_DESTINATIONS: DESTINATIONS_SCHEMA(
            [config.as_dict() for config in configs]
        ),
    }


@callback
def async_persist_destinations(
    hass: HomeAssistant, entry: ConfigEntry, configs: Iterable[DestinationConfig]
) -> None:
    """Écrit la liste des destinations dans les options de l'entrée."""
    hass.config_entries.async_update_entry(
        entry, options=options_avec_destinations(entry, configs)
    )


@callback
def jeton_persiste(hass: HomeAssistant, destination_id: str) -> dict[str, Any] | None:
    """Jeton OAuth2 actuellement persisté pour une destination, s'il existe.

    La lecture se fait dans l'entrée et non dans la `DestinationConfig` en
    mémoire : un rafraîchissement écrit un nouveau jeton, et c'est celui-là qui
    fait foi, y compris pour une destination instanciée avant l'écriture.
    """
    entry = async_entree_auto_backup(hass)
    if entry is None:
        return None
    for brute in async_destination_configs(entry):
        if brute.get(CONF_DESTINATION_ID) == destination_id:
            jeton = brute.get(CONF_TOKEN)
            return dict(jeton) if isinstance(jeton, Mapping) else None
    return None


@callback
def async_persist_token(
    hass: HomeAssistant, destination_id: str, token: Mapping[str, Any]
) -> None:
    """Remplace le jeton d'une destination dans les options de l'entrée.

    Seule la destination visée est touchée : les autres gardent leur
    configuration et leur jeton, y compris quand l'une d'elles est invalide.
    Lève `DestinationNotFoundError` si la destination n'existe plus — elle a pu
    être supprimée pendant l'opération réseau.
    """
    entry = async_entree_auto_backup(hass)
    if entry is None:
        raise DestinationNotFoundError(
            "aucune entrée de configuration Auto Backup : jeton non persisté"
        )

    destinations = async_destination_configs(entry)
    trouvee = False
    mises_a_jour: list[dict[str, Any]] = []
    for brute in destinations:
        copie = dict(brute)
        if copie.get(CONF_DESTINATION_ID) == destination_id:
            copie[CONF_TOKEN] = dict(token)
            trouvee = True
        mises_a_jour.append(copie)

    if not trouvee:
        raise DestinationNotFoundError(
            f"destination inconnue : « {destination_id} », jeton non persisté"
        )

    hass.config_entries.async_update_entry(
        entry,
        options={**_options_completees(entry), CONF_DESTINATIONS: mises_a_jour},
    )


@callback
def async_persist_provider_data(
    hass: HomeAssistant, destination_id: str, donnees: Mapping[str, Any]
) -> None:
    """Fusionne des données de fournisseur dans celles d'une destination.

    **Fusion et non remplacement** : un fournisseur qui mémorise l'identifiant de
    son dossier cible (issue #14) ne doit pas effacer l'adresse du compte
    autorisé écrite par le flux d'ajout (issues #10 et #13), et réciproquement.
    Les autres destinations ne sont pas touchées, exactement comme pour le jeton.

    La signature est celle d'`async_persist_token()` — l'entrée est retrouvée
    ici, et non passée par l'appelant : un fournisseur n'a que `hass` et sa
    configuration sous la main au moment où il écrit.

    Le résultat est validé par `donnees_de_fournisseur()` avant d'être écrit :
    les bornes du champ (nombre de clés, longueur et type des valeurs) valent
    aussi pour une écriture venue d'un fournisseur.

    Rien n'est écrit si les données sont vides ou déjà présentes à l'identique :
    une écriture d'options recharge les destinations, il n'y a pas lieu de le
    faire à chaque téléversement. Lève `DestinationNotFoundError` si la
    destination n'existe plus — elle a pu être supprimée pendant l'envoi.
    """
    if not donnees:
        return

    entry = async_entree_auto_backup(hass)
    if entry is None:
        raise DestinationNotFoundError(
            "aucune entrée de configuration Auto Backup : données de fournisseur "
            "non persistées"
        )

    destinations = async_destination_configs(entry)
    trouvee = False
    inchangee = False
    mises_a_jour: list[dict[str, Any]] = []
    for brute in destinations:
        copie = dict(brute)
        if copie.get(CONF_DESTINATION_ID) == destination_id:
            trouvee = True
            actuelles = copie.get(CONF_PROVIDER_DATA)
            actuelles = dict(actuelles) if isinstance(actuelles, Mapping) else {}
            fusionnees = {**actuelles, **donnees}
            inchangee = fusionnees == actuelles
            try:
                copie[CONF_PROVIDER_DATA] = donnees_de_fournisseur(fusionnees)
            except (vol.Invalid, TypeError, ValueError) as err:
                raise DestinationConfigError(
                    f"données de fournisseur invalides pour « {destination_id} » : "
                    f"{err}"
                ) from err
        mises_a_jour.append(copie)

    if not trouvee:
        raise DestinationNotFoundError(
            f"destination inconnue : « {destination_id} », données de fournisseur "
            "non persistées"
        )
    if inchangee:
        return

    hass.config_entries.async_update_entry(
        entry,
        options={**_options_completees(entry), CONF_DESTINATIONS: mises_a_jour},
    )


@callback
def preserve_fork_options(
    options: Mapping[str, Any], user_input: dict[str, Any]
) -> dict[str, Any]:
    """Réinjecte les options du fork dans celles soumises par le flux d'options.

    Le flux d'options upstream remplace l'intégralité des options par le contenu
    de son formulaire, qui ne connaît aucune des options du fork : sans ce
    report, les destinations configurées et le délai de téléversement seraient
    perdus au premier enregistrement des réglages de sauvegarde.

    Le report porte sur **toutes** les clés de `CLES_DU_FORK`, et sur elles
    seules : une option ajoutée par une issue suivante est donc protégée du seul
    fait d'y être inscrite, sans nouvelle modification ici ni dans
    `config_flow.py`. Les clés absentes des options ne sont pas inventées.
    """
    return {
        **user_input,
        **{cle: options[cle] for cle in CLES_DU_FORK if cle in options},
    }


@callback
def options_avec_reglage(entry: ConfigEntry, cle: str, valeur: Any) -> dict[str, Any]:
    """Options de l'entrée, une option du fork (`CLES_DU_FORK`) mise à jour.

    Comme `options_avec_destinations()`, rien n'est écrit ici : le flux
    d'options a besoin du dictionnaire pour le renvoyer à Home Assistant. Les
    options upstream manquantes sont complétées, sans quoi l'écouteur de mise à
    jour upstream lèverait sur une entrée qui n'a jamais visité son formulaire.
    """
    return {**_options_completees(entry), cle: valeur}
