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
def list_providers() -> tuple[str, ...]:
    """Identifiants des fournisseurs enregistrés, par ordre alphabétique."""
    return tuple(sorted(_FABRIQUES))


@callback
def create_destination(
    hass: HomeAssistant, config: DestinationConfig
) -> RemoteDestination:
    """Instancie la destination décrite par `config` via son fournisseur."""
    return get_provider(config.provider)(hass, config)
