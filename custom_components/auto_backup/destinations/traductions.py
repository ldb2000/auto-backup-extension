"""Textes traduits que le fork compose lui-même (issues #45 et #46).

Home Assistant traduit seul ce qui a une catégorie à lui — étapes de flux,
problèmes, entités, erreurs d'un appel de service. Deux sortes de textes n'en ont
pas, et c'est le fork qui les compose dans la langue de l'instance
(`hass.config.language`) :

- les notifications persistantes (`destinations/notifications.py`, #45) ;
- les messages des échecs de destination (#46), repris par l'événement
  `auto_backup.upload_failed` (champ `error`), l'attribut `last_error` des
  entités, la cause des notifications et le détail de l'abandon
  `echec_fournisseur` du flux d'options.

Les deux vivent dans la section `exceptions` de `translations/*.json`, chacun
sous `message` : c'est la seule catégorie que `hassfest` accepte pour un texte
libre avec placeholders sans le rattacher à une entité, un flux ou un problème.

Le mécanisme est **unique** : lecture du cache de traductions de Home Assistant
(`async_get_cached_translations`), rempli au chargement de l'intégration et à
chaque changement de langue ; chargement ponctuel (`async_get_translations`)
seulement si ce cache n'est pas encore prêt pour la langue courante ; et, si ce
chargement échoue, repli sur l'anglais déjà en cache, sinon sur la clé
elle-même. Un texte ne fait jamais échouer ce qu'il accompagne.

Le masquage (`destinations/masquage.py`) ne s'applique pas ici : les textes
traduits sont écrits par le fork et ne portent aucun secret. Il s'applique aux
**valeurs** des placeholders venues d'ailleurs, chez l'appelant.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.translation import (
    async_get_cached_translations,
    async_get_translations,
)

from ..const import DOMAIN
from .errors import CodeErreur, cle_de_traduction_de_l_erreur

_LOGGER = logging.getLogger(__name__)

# Catégorie de traduction des textes composés par le fork : voir l'en-tête.
CATEGORIE_DE_TRADUCTION = "exceptions"

# Langue de repli, celle de Home Assistant quand une traduction manque.
LANGUE_DE_REPLI = "en"


def chemin_de_traduction(cle: str) -> str:
    """Clé aplatie d'un texte dans le cache de traductions de Home Assistant."""
    return f"component.{DOMAIN}.{CATEGORIE_DE_TRADUCTION}.{cle}.message"


def textes_complets(textes: dict[str, str], cles: Iterable[str]) -> bool:
    """Indique si toutes les clés demandées sont présentes dans `textes`."""
    return all(chemin_de_traduction(cle) in textes for cle in cles)


@callback
def textes_en_cache(hass: HomeAssistant, cles: Iterable[str]) -> dict[str, str] | None:
    """Textes déjà chargés pour la langue courante, ou `None` s'il en manque.

    Home Assistant charge les traductions d'une intégration à son installation,
    puis à chaque changement de langue : le cache suffit presque toujours, et
    aucun texte ne relit les fichiers.
    """
    textes = async_get_cached_translations(
        hass, hass.config.language, CATEGORIE_DE_TRADUCTION, DOMAIN
    )
    return textes if textes_complets(textes, cles) else None


async def async_charger_les_textes(
    hass: HomeAssistant, cles: Iterable[str]
) -> dict[str, str]:
    """Charge les textes de la langue courante, avec repli si le chargement échoue.

    Un chargement en échec ne doit rien faire perdre : le type de l'exception
    est journalisé (jamais son message), puis les textes anglais déjà en cache
    sont rendus s'ils sont complets, un dictionnaire vide sinon — `texte()`
    affiche alors la clé.
    """
    cles = tuple(cles)
    try:
        return await async_get_translations(
            hass, hass.config.language, CATEGORIE_DE_TRADUCTION, {DOMAIN}
        )
    except Exception as err:  # le texte ne fait jamais échouer son porteur
        _LOGGER.warning(
            "Chargement des traductions impossible (%s) : repli sur l'anglais en cache",
            type(err).__name__,
        )
    textes = async_get_cached_translations(
        hass, LANGUE_DE_REPLI, CATEGORIE_DE_TRADUCTION, DOMAIN
    )
    return textes if textes_complets(textes, cles) else {}


async def async_textes(hass: HomeAssistant, cles: Iterable[str]) -> dict[str, str]:
    """Textes de la langue courante : le cache, sinon un chargement ponctuel."""
    cles = tuple(cles)
    if (textes := textes_en_cache(hass, cles)) is not None:
        return textes
    return await async_charger_les_textes(hass, cles)


def texte(textes: dict[str, str], cle: str, **placeholders: object) -> str:
    """Texte traduit d'une clé, placeholders remplacés.

    Un texte introuvable (fichier de traduction abîmé) rend sa clé plutôt que de
    faire échouer ce qu'il accompagne.
    """
    modele = textes.get(chemin_de_traduction(cle))
    if modele is None:
        _LOGGER.warning("Traduction « %s » introuvable", cle)
        return cle
    try:
        return modele.format(**placeholders)
    except KeyError, IndexError, ValueError:
        _LOGGER.warning("Placeholders invalides dans la traduction « %s »", cle)
        return modele


### Messages des échecs de destination (#46) ###


async def async_message_d_erreur(hass: HomeAssistant, code: CodeErreur | str) -> str:
    """Message traduit, dans la langue de l'instance, d'un code d'erreur stable.

    Un code que le fork ne connaît pas (valeur venue d'ailleurs) est ramené à
    `unknown` : son message générique renvoie au journal, seul endroit où le
    détail de l'échec est consigné, masqué.
    """
    try:
        code = CodeErreur(code)
    except ValueError:
        code = CodeErreur.INCONNUE
    cle = cle_de_traduction_de_l_erreur(code)
    return texte(await async_textes(hass, (cle,)), cle)
