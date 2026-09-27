"""Critères d'acceptation de l'issue #58, vérifiés indépendamment du codeur.

Un test par critère Given-When-Then :

1. Après un changement de dossier mené par le **vrai flux d'options**
   (« Modifier une destination », confirmation comprise), le capteur du nombre
   de sauvegardes distantes vaut 0 avant le premier envoi vers le nouveau
   dossier, puis 1 après un envoi réel (`auto_backup.backup`).
2. Une purge déclenchée par le **vrai service** `auto_backup.purge`, après un
   changement de dossier mené par le vrai flux d'options, n'efface jamais les
   entrées de l'ancien dossier chez le fournisseur et ne les compte pas dans
   `retention_count` du nouveau dossier.
3. Un `Store` `auto_backup.remote_backups` en version 1.1 réelle (fixture
   `hass_storage`), avec des entrées sans dossier pour plusieurs destinations
   dont une supprimée, est migré sans perte, sans erreur ni avertissement au
   journal, et l'opération est idempotente à un second démarrage.
4. Un retour au dossier précédent, mené par le vrai flux d'options, recompte
   les sauvegardes de ce dossier et les rend de nouveau purgeables par le vrai
   service `auto_backup.purge`.
5. La FAQ et l'ADR ne mentionnent plus que le capteur continue de compter
   l'ancien dossier ; elles décrivent le nouveau comportement.

Ce module est indépendant de `tests/test_registre_par_dossier.py`, écrit par le
codeur : il rejoue les critères par les points d'entrée réels de l'utilisateur
(flux d'options, service de sauvegarde, service de purge) plutôt que par les
fonctions internes du registre.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.const import ATTR_NAME, CONF_NAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)

from custom_components.auto_backup.const import (
    ATTR_DESTINATION,
    ATTR_FOLDER,
    ATTR_REMOTE_IDS,
    ATTR_UPLOAD_TO,
    CONF_AUTO_PURGE,
    CONF_BACKUP_TIMEOUT,
    CONF_DESTINATION_ID,
    CONF_DESTINATIONS,
    CONF_FOLDER,
    DATA_AUTO_BACKUP,
    DATA_DESTINATIONS,
    DATA_REMOTE_BACKUPS,
    DEFAULT_BACKUP_TIMEOUT,
    DOMAIN,
    EVENT_REMOTE_PURGE,
    EVENT_UPLOAD_SUCCESSFUL,
    SERVICE_BACKUP,
    SERVICE_PURGE,
    STORAGE_KEY_REMOTE_BACKUPS,
    STORAGE_MINOR_VERSION_REMOTE_BACKUPS,
    STORAGE_VERSION_REMOTE_BACKUPS,
)
from custom_components.auto_backup.destinations.entities import (
    SUFFIXE_SAUVEGARDES_DISTANTES,
)
from custom_components.auto_backup.destinations.flow import CONF_CONFIRMER
from custom_components.auto_backup.destinations.retention import (
    EntreeRegistre,
    RegistreSauvegardesDistantes,
)
from destinations_factices import DestinationEnMemoire, config_factice

RACINE_DEPOT = Path(__file__).resolve().parent.parent
DOCS = RACINE_DEPOT / "docs"

DESTINATION = "destination_test"
ANCIEN = "Sauvegardes"
NOUVEAU = "Sauvegardes/Nouveau"
CLE_STOCKAGE = f"{DOMAIN}.{STORAGE_KEY_REMOTE_BACKUPS}"

SLUG = "abc123"
CONTENU_SAUVEGARDE = b"auto-backup"

# Signature de la fixture `ouvrir_les_options` (cf. `tests/conftest.py`).
type OuvrirLesOptions = Callable[[str, str], Awaitable[dict[str, Any]]]


### MONTAGE ###


def _faux_backup_manager(chemin: Path) -> MagicMock:
    """`BackupManager` minimal : une sauvegarde locale et son agent (#8)."""
    sauvegarde = MagicMock()
    sauvegarde.backup_id = SLUG
    agent = MagicMock()
    agent.get_backup_path = MagicMock(return_value=chemin)
    manager = MagicMock()
    manager.async_get_backup = AsyncMock(return_value=(sauvegarde, {}))
    manager.local_backup_agents = {"backup.local": agent}
    return manager


@pytest.fixture
def fichier_de_sauvegarde(tmp_path: Path) -> Path:
    """Fichier `.tar` local tenant lieu de sauvegarde Home Assistant."""
    chemin = tmp_path / f"{SLUG}.tar"
    chemin.write_bytes(CONTENU_SAUVEGARDE)
    return chemin


async def _demarrer(
    hass: HomeAssistant,
    destinations: list[dict[str, Any]] | None = None,
    *,
    fichier: Path | None = None,
    **options: Any,
) -> MockConfigEntry:
    """Initialise l'intégration ; capable de créer une vraie sauvegarde locale
    quand `fichier` est fourni (issue #8), pour un envoi de bout en bout.
    """
    entree = MockConfigEntry(
        domain=DOMAIN,
        title="Auto Backup",
        data={},
        options={
            CONF_AUTO_PURGE: False,
            CONF_BACKUP_TIMEOUT: DEFAULT_BACKUP_TIMEOUT,
            CONF_DESTINATIONS: (
                [config_factice()] if destinations is None else destinations
            ),
            **options,
        },
    )
    entree.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entree.entry_id)
    await hass.async_block_till_done()

    if fichier is not None:
        handler = hass.data[DATA_AUTO_BACKUP]._handler
        handler._manager = _faux_backup_manager(fichier)
        handler.create_backup = AsyncMock(return_value={"slug": SLUG})
    return entree


def _registre(hass: HomeAssistant) -> RegistreSauvegardesDistantes:
    """Registre persistant des sauvegardes distantes."""
    return hass.data[DATA_REMOTE_BACKUPS]


def _destination(
    hass: HomeAssistant, destination_id: str = DESTINATION
) -> DestinationEnMemoire:
    """Destination factice actuellement chargée pour cet identifiant."""
    return hass.data[DATA_DESTINATIONS].async_get(destination_id)


def _compteur(
    hass: HomeAssistant, entree: MockConfigEntry, destination_id: str = DESTINATION
) -> str:
    """État du capteur « sauvegardes distantes » de la destination."""
    entity_id = er.async_get(hass).async_get_entity_id(
        Platform.SENSOR,
        DOMAIN,
        f"{entree.entry_id}_{destination_id}_{SUFFIXE_SAUVEGARDES_DISTANTES}",
    )
    assert entity_id is not None
    etat = hass.states.get(entity_id)
    assert etat is not None
    return etat.state


async def _envoyer_une_sauvegarde(hass: HomeAssistant, nom: str) -> None:
    """Crée puis téléverse une vraie sauvegarde, par le service `auto_backup.backup`.

    C'est le seul chemin emprunté par un utilisateur réel : la création passe
    par le handler upstream simulé, le téléversement par le coordinateur de
    l'issue #8, qui émet ensuite `auto_backup.upload_successful` — l'événement
    qui alimente le registre (issue #58).
    """
    await hass.services.async_call(
        DOMAIN,
        SERVICE_BACKUP,
        {ATTR_NAME: nom, ATTR_UPLOAD_TO: DESTINATION},
        blocking=True,
    )
    await hass.async_block_till_done(wait_background_tasks=True)


async def _changer_de_dossier(
    hass: HomeAssistant,
    entree: MockConfigEntry,
    ouvrir_les_options: OuvrirLesOptions,
    dossier: str,
    *,
    destination_id: str = DESTINATION,
    nom: str = "Destination de test",
    **reste: Any,
) -> dict[str, Any]:
    """Change le dossier distant d'une destination par le **vrai flux d'options**.

    Parcourt « Modifier une destination » exactement comme l'utilisateur : choix
    de la destination, formulaire prérempli, puis confirmation explicite du
    changement de dossier (issue #51) — sans laquelle rien n'est enregistré.
    """
    resultat = await ouvrir_les_options(entree.entry_id, "modifier_destination")
    assert resultat["step_id"] == "modifier_destination"
    resultat = await hass.config_entries.options.async_configure(
        resultat["flow_id"], {CONF_DESTINATION_ID: destination_id}
    )
    assert resultat["step_id"] == "parametres_destination"

    resultat = await hass.config_entries.options.async_configure(
        resultat["flow_id"],
        {CONF_NAME: nom, CONF_FOLDER: dossier, CONF_AUTO_PURGE: False, **reste},
    )
    assert resultat["step_id"] == "confirmer_changement_de_dossier"
    assert resultat["description_placeholders"]["nouveau_dossier"] == dossier

    resultat = await hass.config_entries.options.async_configure(
        resultat["flow_id"], {CONF_CONFIRMER: True}
    )
    assert resultat["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    return resultat


### CRITÈRE 1 : capteur à 0 avant le premier envoi, 1 après (flux d'options réel) ###


async def test_critere1_apres_un_changement_reel_le_capteur_ne_compte_que_le_nouveau(
    hass: HomeAssistant,
    integration_backup: None,
    fournisseur_factice: str,
    fichier_de_sauvegarde: Path,
    ouvrir_les_options: OuvrirLesOptions,
) -> None:
    """Given une destination dont le dossier vient de changer (vrai flux
    d'options), When je consulte le capteur, Then il ne compte que le nouveau
    dossier : 0 avant le premier envoi, 1 après un envoi réel.
    """
    entree = await _demarrer(hass, fichier=fichier_de_sauvegarde)

    # Un envoi réel dans le dossier d'origine : le capteur le compte.
    await _envoyer_une_sauvegarde(hass, "Avant le changement")
    assert _compteur(hass, entree) == "1"

    # WHEN : changement de dossier par le vrai flux d'options, confirmé.
    await _changer_de_dossier(hass, entree, ouvrir_les_options, NOUVEAU)

    # THEN : le capteur retombe à 0 sans redémarrage, avant tout envoi au
    # nouveau dossier — l'historique de l'ancien n'est pas effacé pour autant.
    assert _compteur(hass, entree) == "0"
    assert {e.remote_id for e in _registre(hass).entrees(DESTINATION)} == {
        "destination_test-1"
    }

    succes = async_capture_events(hass, EVENT_UPLOAD_SUCCESSFUL)
    await _envoyer_une_sauvegarde(hass, "Apres le changement")

    assert _compteur(hass, entree) == "1"
    # L'événement porte bien le nouveau dossier (issue #58).
    assert succes[0].data[ATTR_FOLDER] == NOUVEAU


### CRITÈRE 2 : la purge (service réel) épargne l'ancien dossier ###


async def test_critere2_le_service_purge_epargne_l_ancien_dossier(
    hass: HomeAssistant,
    integration_backup: None,
    fournisseur_factice: str,
    ouvrir_les_options: OuvrirLesOptions,
) -> None:
    """Given des entrées du registre de l'ancien dossier, When le **vrai
    service** `auto_backup.purge` s'exécute, Then elles ne sont jamais
    supprimées chez le fournisseur ni comptées dans `retention_count` du
    nouveau dossier.
    """
    entree = await _demarrer(hass, [config_factice(retention_days=None)])

    # WHEN (mise en place) : changement de dossier par le vrai flux d'options,
    # avec une rétention en nombre de 1 sur le nouveau dossier.
    await _changer_de_dossier(
        hass, entree, ouvrir_les_options, NOUVEAU, retention_count=1
    )

    # La destination factice repart vide au rechargement (#51) : les fichiers
    # existent pourtant toujours chez le fournisseur. On les y redépose pour
    # simuler cette persistance réelle — le pire cas où le fournisseur les
    # listerait avec ceux du nouveau dossier (cf. docstring de `retention.py`).
    destination = _destination(hass)
    maintenant = dt_util.utcnow()
    for heures, remote_id, dossier in (
        (4, "ancienne-1", ANCIEN),
        (3, "ancienne-2", ANCIEN),
        (2, "nouvelle-1", NOUVEAU),
        (1, "nouvelle-2", NOUVEAU),
    ):
        date = maintenant - timedelta(hours=heures)
        destination.ajouter_sauvegarde(remote_id, created_at=date)
        await _registre(hass).async_enregistrer(
            DESTINATION,
            EntreeRegistre(
                remote_id=remote_id, name=remote_id, created_at=date, folder=dossier
            ),
        )

    # THEN : le service réel de purge ne supprime que l'excédent du nouveau
    # dossier ; jamais rien de l'ancien.
    purges = async_capture_events(hass, EVENT_REMOTE_PURGE)
    await hass.services.async_call(DOMAIN, SERVICE_PURGE, blocking=True)
    await hass.async_block_till_done()

    assert destination.suppressions == ["nouvelle-1"]
    assert {"ancienne-1", "ancienne-2"} <= set(destination.sauvegardes)
    assert {e.remote_id for e in _registre(hass).entrees(DESTINATION)} == {
        "ancienne-1",
        "ancienne-2",
        "nouvelle-2",
    }
    assert len(purges) == 1
    assert purges[0].data[ATTR_DESTINATION] == DESTINATION
    assert purges[0].data[ATTR_REMOTE_IDS] == ["nouvelle-1"]


### CRITÈRE 3 : migration d'un Store 1.1 réel, sans perte ni erreur, idempotente ###


async def test_critere3_un_registre_1_1_reel_est_migre_sans_perte_ni_erreur(
    hass: HomeAssistant,
    integration_backup: None,
    fournisseur_factice: str,
    hass_storage: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Given un registre écrit avant #58 (`Store` version 1.1, entrées sans
    dossier), pour plusieurs destinations dont une supprimée, When
    l'intégration démarre, Then il est migré sans perte ni erreur, les entrées
    existantes sont rattachées au dossier configuré à ce moment-là, et un
    second démarrage (idempotence) ne change plus rien.
    """
    hass_storage[CLE_STOCKAGE] = {
        "version": 1,
        "minor_version": 1,
        "key": CLE_STOCKAGE,
        "data": {
            DESTINATION: [
                {
                    "remote_id": "a",
                    "name": "Sauvegarde A",
                    "slug": "slug-a",
                    "size": 1024,
                    "created_at": "2026-01-01T00:00:00+00:00",
                },
            ],
            "autre_destination": [
                {"remote_id": "b", "name": "Sauvegarde B"},
            ],
            "destination_disparue": [
                {"remote_id": "c", "name": "Sauvegarde C"},
            ],
        },
    }

    caplog.set_level(logging.DEBUG)
    entree = await _demarrer(
        hass,
        [
            config_factice(folder=NOUVEAU),
            config_factice(
                destination_id="autre_destination",
                name="Autre destination",
                folder="Archives",
            ),
        ],
    )

    # Aucune erreur ni avertissement au démarrage (filtrage explicite par
    # niveau, une trace en `debug` ne compte pas).
    genants = [
        enregistrement
        for enregistrement in caplog.records
        if enregistrement.levelno >= logging.WARNING
    ]
    assert not genants, [enregistrement.message for enregistrement in genants]

    registre = _registre(hass)
    # Rattachées au dossier configuré au moment de la migration.
    entree_a = registre.entree(DESTINATION, "a")
    assert entree_a is not None
    assert entree_a.folder == NOUVEAU
    assert entree_a.slug == "slug-a"  # rien d'autre n'est perdu
    assert entree_a.size == 1024

    entree_b = registre.entree("autre_destination", "b")
    assert entree_b is not None
    assert entree_b.folder == "Archives"

    # Destination supprimée : l'entrée est conservée, dossier inconnu — ni
    # comptée, ni purgeable, mais rien n'est perdu.
    entree_c = registre.entree("destination_disparue", "c")
    assert entree_c is not None
    assert entree_c.folder is None

    stocke = hass_storage[CLE_STOCKAGE]
    assert stocke["version"] == STORAGE_VERSION_REMOTE_BACKUPS
    assert stocke["minor_version"] == STORAGE_MINOR_VERSION_REMOTE_BACKUPS
    brute_a = next(
        brute for brute in stocke["data"][DESTINATION] if brute["remote_id"] == "a"
    )
    assert brute_a[ATTR_FOLDER] == NOUVEAU
    brute_c = next(
        brute
        for brute in stocke["data"]["destination_disparue"]
        if brute["remote_id"] == "c"
    )
    assert brute_c[ATTR_FOLDER] is None

    # THEN (idempotence) : un second démarrage (redémarrage de l'intégration)
    # ne change plus rien.
    caplog.clear()
    assert await hass.config_entries.async_reload(entree.entry_id)
    await hass.async_block_till_done()

    genants_au_redemarrage = [
        enregistrement
        for enregistrement in caplog.records
        if enregistrement.levelno >= logging.WARNING
    ]
    assert not genants_au_redemarrage, [
        enregistrement.message for enregistrement in genants_au_redemarrage
    ]
    registre_redemarre = _registre(hass)
    assert registre_redemarre.entree(DESTINATION, "a").folder == NOUVEAU
    assert registre_redemarre.entree("destination_disparue", "c").folder is None
    assert hass_storage[CLE_STOCKAGE]["data"] == stocke["data"]


### CRITÈRE 4 : retour au dossier précédent -> recompté et purgeable ###


async def test_critere4_un_retour_au_dossier_precedent_recompte_et_repurge(
    hass: HomeAssistant,
    integration_backup: None,
    fournisseur_factice: str,
    fichier_de_sauvegarde: Path,
    ouvrir_les_options: OuvrirLesOptions,
) -> None:
    """Given un retour au dossier précédent, When je modifie la destination
    (vrai flux d'options), Then les sauvegardes de ce dossier, toujours
    présentes au registre, sont de nouveau comptées par le capteur et
    purgeables par le **vrai service** `auto_backup.purge`.
    """
    entree = await _demarrer(
        hass, [config_factice(retention_days=None)], fichier=fichier_de_sauvegarde
    )

    # GIVEN : trois sauvegardes réelles, déposées dans le dossier d'origine —
    # le capteur les compte par le seul jeu des événements réels (issue #8).
    for nom in ("Sauvegarde 1", "Sauvegarde 2", "Sauvegarde 3"):
        await _envoyer_une_sauvegarde(hass, nom)
    assert _compteur(hass, entree) == "3"
    remote_ids = sorted(
        sauvegarde.remote_id for sauvegarde in _destination(hass).sauvegardes.values()
    )
    assert len(remote_ids) == 3

    maintenant = dt_util.utcnow()
    # La date qui compte pour la rétention est celle que le fournisseur annonce
    # au listage (`retention.py::_candidats`) : c'est elle que `_redeposer()`
    # fixe, peu importe celle du registre.
    dates = dict(zip(remote_ids, (3, 2, 1), strict=True))  # heures, plus ancien en tête
    plus_recent = remote_ids[2]

    def _redeposer() -> DestinationEnMemoire:
        """Redépose les fichiers chez le fournisseur, reparti à vide (#51)."""
        destination = _destination(hass)
        for remote_id, heures in dates.items():
            destination.ajouter_sauvegarde(
                remote_id, created_at=maintenant - timedelta(hours=heures)
            )
        return destination

    # WHEN (1) : changement de dossier réel vers un nouveau dossier.
    await _changer_de_dossier(
        hass, entree, ouvrir_les_options, NOUVEAU, retention_count=1
    )
    assert _compteur(hass, entree) == "0"
    _redeposer()
    await hass.services.async_call(DOMAIN, SERVICE_PURGE, blocking=True)
    await hass.async_block_till_done()
    # Rien n'est purgé : ces trois sauvegardes appartiennent à l'ancien dossier.
    assert _destination(hass).suppressions == []

    # WHEN (2) : retour réel au dossier précédent.
    await _changer_de_dossier(
        hass, entree, ouvrir_les_options, ANCIEN, retention_count=1
    )

    # THEN : recomptées par le capteur, sans redémarrage.
    assert _compteur(hass, entree) == "3"

    destination = _redeposer()
    await hass.services.async_call(DOMAIN, SERVICE_PURGE, blocking=True)
    await hass.async_block_till_done()

    # THEN : de nouveau purgeables, selon la rétention du dossier retrouvé.
    assert sorted(destination.suppressions) == sorted(
        remote_id for remote_id in remote_ids if remote_id != plus_recent
    )
    assert [e.remote_id for e in _registre(hass).entrees(DESTINATION)] == [plus_recent]
    assert _compteur(hass, entree) == "1"


### CRITÈRE 5 : FAQ et ADR décrivent le nouveau comportement ###


def _texte_normalise(chemin: Path) -> str:
    """Contenu du fichier, espaces et retours à la ligne réduits à un espace.

    Un habillage Markdown différent (largeur de ligne) ne doit pas faire
    échouer une recherche de phrase qui, à l'affichage, reste continue.
    """
    return " ".join(chemin.read_text(encoding="utf-8").split())


def test_critere5_la_doc_ne_dit_plus_que_le_capteur_compte_l_ancien_dossier() -> None:
    """Given la FAQ et l'ADR, When je les lis, Then la mention « le capteur
    continue de compter l'ancien dossier » a disparu, remplacée par le nouveau
    comportement.
    """
    faq = _texte_normalise(DOCS / "faq.md")
    adr = _texte_normalise(DOCS / "adr" / "0001-destinations-distantes.md")

    # L'ancienne formulation (avant #58) ne doit plus figurer nulle part.
    formulations_perimees = [
        "continue de compter l'ancien dossier",
        "continue de compter les entrées de l'ancien dossier",
        "conséquence assumée",
    ]
    for texte, nom in ((faq, "faq.md"), (adr, "ADR")):
        for formulation in formulations_perimees:
            assert formulation not in texte, f"« {formulation} » encore dans {nom}"

    # Le nouveau comportement est bien décrit, dans les deux documents.
    assert "Que deviennent les sauvegardes si je change de dossier distant" in faq
    assert "ne sont **plus ni listées, ni purgées, ni comptées**" in faq
    assert "ne compte que les sauvegardes du dossier configuré" in faq
    assert "de nouveau comptées et soumises à la rétention" in faq

    assert "Registre rangé par dossier (issue #58)" in adr
    assert "n'est ni candidate, ni comptée dans `retention_count`" in adr
    assert (
        "revenir au dossier précédent les rend de nouveau comptées et purgeables" in adr
    )
    assert "1.1 à la 1.2" in adr
    assert "sans perte" in adr
    assert "idempotente" in adr
