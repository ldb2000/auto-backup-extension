"""Registre des fournisseurs de destinations distantes.

Un fournisseur (Dropbox, Google Drive, ...) s'enregistre auprès de ce registre
en associant son identifiant à une fabrique. Le cœur de l'intégration ne connaît
que le registre : ajouter un fournisseur consiste à ajouter un module qui
s'enregistre, sans toucher au reste du code.

```python
@provider("dropbox")
class DropboxDestination(RemoteDestination):
    ...
```
"""

from __future__ import annotations

from collections.abc import Callable

from homeassistant.core import HomeAssistant, callback

from .destination import RemoteDestination
from .errors import DuplicateProviderError, UnknownProviderError
from .models import DestinationConfig

type DestinationFactory = Callable[
    [HomeAssistant, DestinationConfig], RemoteDestination
]

_FABRIQUES: dict[str, DestinationFactory] = {}


@callback
def register_provider(provider_id: str, factory: DestinationFactory) -> None:
    """Enregistre la fabrique du fournisseur `provider_id`.

    Lève `DuplicateProviderError` si l'identifiant est déjà pris : deux
    fournisseurs homonymes se masqueraient silencieusement l'un l'autre.
    """
    if provider_id in _FABRIQUES:
        raise DuplicateProviderError(
            f"le fournisseur « {provider_id} » est déjà enregistré"
        )
    _FABRIQUES[provider_id] = factory


@callback
def provider(provider_id: str) -> Callable[[DestinationFactory], DestinationFactory]:
    """Décorateur équivalent à `register_provider`, à poser sur une classe."""

    def decorateur(factory: DestinationFactory) -> DestinationFactory:
        register_provider(provider_id, factory)
        return factory

    return decorateur


@callback
def unregister_provider(provider_id: str) -> None:
    """Retire un fournisseur du registre (utilisé par les tests)."""
    if provider_id not in _FABRIQUES:
        raise UnknownProviderError(f"fournisseur inconnu : « {provider_id} »")
    del _FABRIQUES[provider_id]


@callback
def get_provider(provider_id: str) -> DestinationFactory:
    """Renvoie la fabrique du fournisseur, ou lève `UnknownProviderError`."""
    try:
        return _FABRIQUES[provider_id]
    except KeyError:
        raise UnknownProviderError(
            f"fournisseur inconnu : « {provider_id} » "
            f"(fournisseurs enregistrés : {', '.join(list_providers()) or 'aucun'})"
        ) from None


@callback
def provider_label(provider_id: str) -> str:
    """Libellé lisible du fournisseur, son identifiant à défaut (issue #10).

    Le libellé est déclaré par la fabrique elle-même (`RemoteDestination.LABEL`)
    : un fournisseur qui n'en déclare pas — c'est le cas des fournisseurs
    factices des tests — reste affiché sous son identifiant technique.
    """
    libelle = getattr(get_provider(provider_id), "LABEL", None)
    if isinstance(libelle, str) and libelle.strip():
        return libelle.strip()
    return provider_id


@callback
def cles_liees_au_dossier(provider_id: str) -> frozenset[str]:
    """Clés de `provider_data` à oublier quand le dossier distant change (#51).

    Elles sont déclarées par la fabrique (`RemoteDestination.CLES_LIEES_AU_DOSSIER`).
    Un fournisseur qui n'en déclare pas — ou qui a disparu du registre — n'en a
    aucune : ses données de compte sont conservées telles quelles.
    """
    try:
        fabrique = get_provider(provider_id)
    except UnknownProviderError:
        return frozenset()
    cles = getattr(fabrique, "CLES_LIEES_AU_DOSSIER", None)
    if not isinstance(cles, frozenset | set | tuple | list):
        return frozenset()
    return frozenset(cle for cle in cles if isinstance(cle, str))


@callback
def list_providers() -> tuple[str, ...]:
    """Identifiants des fournisseurs enregistrés, par ordre alphabétique."""
    return tuple(sorted(_FABRIQUES))


@callback
def create_destination(
    hass: HomeAssistant, config: DestinationConfig
) -> RemoteDestination:
    """Instancie la destination décrite par `config` via son fournisseur."""
    return get_provider(config.provider)(hass, config)
