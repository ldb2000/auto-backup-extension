"""Codes stables et messages traduits des échecs de destination (issue #46).

Ce module couvre le socle : le code porté par chaque `DestinationError`, et la
lecture de son message traduit par `destinations/traductions.py`. Le parcours
complet — événement, notification, `last_error`, abandon du flux, refus
d'`upload_to` — est éprouvé par `tests/test_televersement.py` et par les tests
des fournisseurs ; la cohérence des fichiers de traduction, par
`tests/test_traductions.py`.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.auto_backup.const import DOMAIN
from custom_components.auto_backup.destinations import traductions
from custom_components.auto_backup.destinations.errors import (
    CLES_DES_ERREURS,
    CodeErreur,
    DestinationAuthError,
    DestinationConfigError,
    DestinationError,
    DestinationNotFoundError,
    DestinationQuotaError,
    DuplicateProviderError,
    UnknownProviderError,
    cle_de_traduction_de_l_erreur,
)
from custom_components.auto_backup.destinations.traductions import (
    async_message_d_erreur,
)
from messages_attendus import message_d_erreur

### Codes portés par les erreurs ###


@pytest.mark.parametrize(
    ("classe", "code"),
    [
        (DestinationError, CodeErreur.INCONNUE),
        (DestinationAuthError, CodeErreur.ACCES_REVOQUE),
        (DestinationQuotaError, CodeErreur.QUOTA_DEPASSE),
        (DestinationNotFoundError, CodeErreur.INTROUVABLE),
        (DestinationConfigError, CodeErreur.CONFIGURATION_INVALIDE),
        (UnknownProviderError, CodeErreur.FOURNISSEUR_INCONNU),
        (DuplicateProviderError, CodeErreur.INCONNUE),
    ],
)
def test_chaque_classe_porte_le_code_de_sa_cause(
    classe: type[DestinationError], code: CodeErreur
) -> None:
    """Sans précision au site de levée, la classe fixe le code."""
    erreur = classe("détail technique")

    assert erreur.code is code
    assert isinstance(erreur, HomeAssistantError)


def test_le_site_de_levee_peut_preciser_le_code() -> None:
    """Le fournisseur qualifie l'échec là où il le reconnaît."""
    erreur = DestinationAuthError("portée absente", code=CodeErreur.PORTEE_MANQUANTE)

    assert erreur.code is CodeErreur.PORTEE_MANQUANTE
    assert isinstance(erreur, DestinationAuthError)


def test_le_detail_reste_le_texte_de_l_exception() -> None:
    """Le texte de l'exception est le détail, destiné au journal masqué."""
    erreur = DestinationQuotaError("espace saturé (path/insufficient_space/..)")

    assert str(erreur) == "espace saturé (path/insufficient_space/..)"


def test_l_erreur_porte_sa_cle_de_traduction() -> None:
    """Remontée d'un appel de service, l'interface l'afficherait traduite."""
    erreur = DestinationQuotaError("espace saturé")

    assert erreur.translation_domain == DOMAIN
    assert erreur.translation_key == "erreur_quota_exceeded"
    assert erreur.translation_placeholders is None


def test_chaque_code_a_sa_cle_de_traduction() -> None:
    """Une clé par code, préfixée, dans l'ordre des codes."""
    assert tuple(f"erreur_{code.value}" for code in CodeErreur) == CLES_DES_ERREURS
    assert cle_de_traduction_de_l_erreur("timeout") == "erreur_timeout"


### Lecture du message traduit ###


@pytest.mark.parametrize("langue", ["fr", "en"])
@pytest.mark.parametrize("code", list(CodeErreur))
async def test_le_message_suit_la_langue_de_l_instance(
    hass: HomeAssistant, langue: str, code: CodeErreur
) -> None:
    """Critère 1 de #46 : chaque code se lit en français et en anglais."""
    hass.config.language = langue

    assert await async_message_d_erreur(hass, code) == message_d_erreur(code, langue)


async def test_une_langue_non_traduite_retombe_sur_l_anglais(
    hass: HomeAssistant,
) -> None:
    """Le fork ne traduit que le français et l'anglais (hors périmètre)."""
    hass.config.language = "de"

    assert await async_message_d_erreur(
        hass, CodeErreur.DELAI_DEPASSE
    ) == message_d_erreur(CodeErreur.DELAI_DEPASSE, "en")


async def test_un_code_inconnu_du_fork_donne_le_message_generique(
    hass: HomeAssistant,
) -> None:
    """Une valeur venue d'ailleurs n'est jamais affichée telle quelle."""
    hass.config.language = "fr"

    message = await async_message_d_erreur(hass, "code_invente")

    assert message == message_d_erreur(CodeErreur.INCONNUE, "fr")


async def test_le_chargement_asynchrone_est_emprunte_sans_cache(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sans cache pour la langue courante, les traductions sont chargées."""
    hass.config.language = "fr"
    monkeypatch.setattr(traductions, "textes_en_cache", lambda hass, cles: None)
    appels: list[tuple[Any, ...]] = []
    original = traductions.async_get_translations

    async def espion(*args: Any, **kwargs: Any) -> dict[str, str]:
        appels.append(args)
        return await original(*args, **kwargs)

    monkeypatch.setattr(traductions, "async_get_translations", espion)

    message = await async_message_d_erreur(hass, CodeErreur.QUOTA_DEPASSE)

    assert appels == [(hass, "fr", traductions.CATEGORIE_DE_TRADUCTION, {DOMAIN})]
    assert message == message_d_erreur(CodeErreur.QUOTA_DEPASSE, "fr")


async def test_un_chargement_en_echec_retombe_sur_l_anglais_en_cache(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Le message ne fait jamais échouer l'échec qu'il décrit."""
    hass.config.language = "en"
    await async_message_d_erreur(hass, CodeErreur.QUOTA_DEPASSE)  # cache anglais
    hass.config.language = "fr"
    monkeypatch.setattr(traductions, "textes_en_cache", lambda hass, cles: None)

    async def chargement_en_echec(*args: Any, **kwargs: Any) -> dict[str, str]:
        raise OSError("lecture impossible de /config/secret_token=abc")

    monkeypatch.setattr(traductions, "async_get_translations", chargement_en_echec)

    message = await async_message_d_erreur(hass, CodeErreur.QUOTA_DEPASSE)

    assert message == message_d_erreur(CodeErreur.QUOTA_DEPASSE, "en")
    assert "OSError" in caplog.text
    assert "secret_token" not in caplog.text


async def test_sans_aucune_traduction_la_cle_est_rendue(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dernier repli : la clé, lisible dans un rapport d'anomalie."""
    monkeypatch.setattr(traductions, "textes_en_cache", lambda hass, cles: None)
    monkeypatch.setattr(traductions, "async_get_cached_translations", lambda *args: {})

    async def chargement_en_echec(*args: Any, **kwargs: Any) -> dict[str, str]:
        raise OSError

    monkeypatch.setattr(traductions, "async_get_translations", chargement_en_echec)

    assert await async_message_d_erreur(hass, CodeErreur.DELAI_DEPASSE) == (
        "erreur_timeout"
    )
