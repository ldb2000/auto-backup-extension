"""Tests du socle des destinations distantes (issue #6).

Couvre le contrat abstrait, les types de données, la hiérarchie d'erreurs, le
registre de fournisseurs et le gestionnaire de destinations. Le fournisseur
factice de `tests/destinations_factices.py` tient lieu de fournisseur réel :
aucun accès réseau n'est réalisé.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from pathlib import Path

import pytest
import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.auto_backup.const import (
    CONF_FOLDER,
    CONF_PROVIDER_DATA,
    DEFAULT_DESTINATION_FOLDER,
    EVENT_REMOTE_PURGE,
    EVENT_UPLOAD_FAILED,
    EVENT_UPLOAD_START,
    EVENT_UPLOAD_SUCCESSFUL,
)
from custom_components.auto_backup.destinations import (
    DESTINATION_SCHEMA,
    VALEUR_MASQUEE,
    DestinationAuthError,
    DestinationConfig,
    DestinationConfigError,
    DestinationError,
    DestinationManager,
    DestinationNotFoundError,
    DestinationQuotaError,
    DuplicateProviderError,
    RemoteBackup,
    RemoteDestination,
    UnknownProviderError,
    create_destination,
    get_provider,
    list_providers,
    provider,
    register_provider,
    unregister_provider,
)
from destinations_factices import (
    PROVIDER_FACTICE,
    DestinationEnMemoire,
    config_factice,
)

OPERATIONS_ATTENDUES = {
    "async_upload",
    "async_list_backups",
    "async_delete_backup",
    "async_check_connection",
}

PROPRIETES_ATTENDUES = {"destination_id", "provider", "name"}

# Dossiers distants refusés : traversée, chemin absolu, séparateur Windows,
# segment vide, espace de bordure, caractère de contrôle ou non imprimable,
# confusable Unicode dont la forme NFKC est une traversée, caractère hors liste
# blanche, et longueur excessive (au total ou par segment).
DOSSIERS_REFUSES = [
    "..",
    ".",
    "../x",
    "../../etc/passwd",
    "a/../b",
    "a/..",
    "./a",
    "/",
    "/abs",
    "/etc/passwd",
    "C:/Sauvegardes",
    "c:\\Sauvegardes",
    "a\\b",
    "\\\\serveur\\partage",
    "a//b",
    "a/b/",
    " Sauvegardes",
    "Sauvegardes ",
    "a/ b",
    "a/b ",
    "Sauvegardes\n",
    "Sauvegardes\x00HA",
    "Sauvegardes\u202eHA",
    # Confusables Unicode : leur forme NFKC vaut « .. », « a/../b » et « a/.. ».
    "\uff0e\uff0e",
    "a\uff0f..\uff0fb",
    "a/\u2025",
    # Espace sans chasse (U+200B), invisible dans un formulaire.
    "a\u200bb",
    # Caractères hors liste blanche.
    "Sauvegardes*",
    "Sauvegardes/HA?",
    # Longueurs : plus de 255 caractères au total, plus de 100 par segment.
    "S" * 256,
    "S" * 101,
]

# Dossiers distants acceptés : chemins relatifs POSIX, accents compris, sous la
# forme (entrée, valeur normalisée attendue en sortie).
DOSSIERS_ACCEPTES = [
    ("Sauvegardes", "Sauvegardes"),
    ("Sauvegardes/HA", "Sauvegardes/HA"),
    (DEFAULT_DESTINATION_FOLDER, DEFAULT_DESTINATION_FOLDER),
    ("Home Assistant/2026-09", "Home Assistant/2026-09"),
    ("a.b/c_d-e", "a.b/c_d-e"),
    ("Sauvegardes (2026)", "Sauvegardes (2026)"),
    ("Sauvegardes/mes..archives", "Sauvegardes/mes..archives"),
    ("Sauvegardes/Été", "Sauvegardes/Été"),
    # Les bornes de longueur sont inclusives : 100 caractères par segment.
    ("S" * 100 + "/" + "S" * 100, "S" * 100 + "/" + "S" * 100),
    # Normalisation NFKC : la valeur renvoyée est la forme normalisée, qui est
    # aussi celle que les règles ont vérifiée.
    ("Sauvegardes/\uff28", "Sauvegardes/H"),
]


def _destination(hass: HomeAssistant, **surcharges) -> DestinationEnMemoire:
    """Instancie une destination factice à partir d'une configuration brute."""
    return DestinationEnMemoire(
        hass, DestinationConfig.from_dict(config_factice(**surcharges))
    )


### CONTRAT ABSTRAIT ###


def test_le_contrat_declare_les_operations_et_les_proprietes() -> None:
    """La classe abstraite impose les quatre opérations et les propriétés."""
    assert RemoteDestination.__abstractmethods__ >= OPERATIONS_ATTENDUES
    for nom in OPERATIONS_ATTENDUES:
        assert inspect.iscoroutinefunction(getattr(RemoteDestination, nom))
    for nom in PROPRIETES_ATTENDUES:
        assert isinstance(getattr(RemoteDestination, nom), property)


def test_une_destination_incomplete_n_est_pas_instanciable(
    hass: HomeAssistant,
) -> None:
    """Un fournisseur qui n'implémente pas tout le contrat est refusé."""

    class DestinationIncomplete(RemoteDestination):
        """Fournisseur volontairement incomplet."""

        async def async_check_connection(self) -> None:
            """Ne fait rien."""

    with pytest.raises(TypeError):
        DestinationIncomplete(hass, DestinationConfig.from_dict(config_factice()))


def test_les_proprietes_exposent_la_configuration(hass: HomeAssistant) -> None:
    """Les propriétés de la destination reflètent sa configuration."""
    destination = _destination(hass)

    assert destination.destination_id == "destination_test"
    assert destination.provider == PROVIDER_FACTICE
    assert destination.name == "Destination de test"
    assert destination.folder == "Sauvegardes"
    assert destination.retention_days == 7
    assert destination.retention_count == 3
    assert destination.config == DestinationConfig.from_dict(config_factice())


def test_la_representation_ne_divulgue_que_l_identite(hass: HomeAssistant) -> None:
    """`repr()` reste utilisable dans les journaux, sans secret."""
    representation = repr(_destination(hass))

    assert "destination_test" in representation
    assert PROVIDER_FACTICE in representation


### CONFIGURATION ET RÉTENTION ###


def test_la_configuration_applique_les_valeurs_par_defaut() -> None:
    """Dossier et rétentions sont facultatifs et ont des valeurs par défaut."""
    config = DestinationConfig.from_dict(
        {
            "destination_id": "d1",
            "provider": PROVIDER_FACTICE,
            "name": "Sans dossier",
        }
    )

    assert config.folder == DEFAULT_DESTINATION_FOLDER
    assert config.retention_days is None
    assert config.retention_count is None


def test_la_configuration_se_serialise_et_se_relit() -> None:
    """`as_dict()` puis `from_dict()` redonnent une configuration identique."""
    config = DestinationConfig.from_dict(config_factice())

    assert DestinationConfig.from_dict(config.as_dict()) == config
    assert config.as_dict() == config_factice()


def test_la_configuration_est_immuable() -> None:
    """Une configuration chargée ne peut pas être modifiée en place."""
    config = DestinationConfig.from_dict(config_factice())

    with pytest.raises(AttributeError):
        config.name = "Autre nom"


@pytest.mark.parametrize("champ", ["retention_days", "retention_count"])
@pytest.mark.parametrize("valeur", [0, -1, -30, True, "7", 1.5])
def test_les_retentions_refusent_les_valeurs_non_entieres_ou_nulles(
    champ: str, valeur: object
) -> None:
    """Zéro, valeurs négatives, booléens et non-entiers sont refusés."""
    with pytest.raises(DestinationConfigError):
        DestinationConfig.from_dict(config_factice(**{champ: valeur}))

    with pytest.raises(DestinationConfigError):
        DestinationConfig(
            destination_id="d1",
            provider=PROVIDER_FACTICE,
            name="Destination",
            **{champ: valeur},
        )


@pytest.mark.parametrize("champ", ["retention_days", "retention_count"])
def test_les_retentions_acceptent_un_entier_positif_ou_rien(champ: str) -> None:
    """Un entier strictement positif et l'absence de valeur sont acceptés."""
    avec_valeur = DestinationConfig.from_dict(config_factice(**{champ: 1}))
    sans_valeur = DestinationConfig.from_dict(config_factice(**{champ: None}))

    assert getattr(avec_valeur, champ) == 1
    assert getattr(sans_valeur, champ) is None


@pytest.mark.parametrize("champ", ["destination_id", "provider", "name", "folder"])
@pytest.mark.parametrize("valeur", ["", "   ", None, 12])
def test_les_champs_texte_refusent_le_vide(champ: str, valeur: object) -> None:
    """Identifiant, fournisseur, nom et dossier doivent être non vides."""
    with pytest.raises(DestinationConfigError):
        DestinationConfig.from_dict(config_factice(**{champ: valeur}))


@pytest.mark.parametrize("dossier", DOSSIERS_REFUSES)
def test_le_dossier_refuse_la_traversee_et_les_chemins_absolus(dossier: str) -> None:
    """Le dossier distant ne doit pas pouvoir désigner autre chose que lui-même.

    Il est repris tel quel par les fournisseurs pour bâtir le chemin distant
    d'une sauvegarde : `folder = "../../etc/passwd"` doit être refusé par le
    schéma comme par la dataclass, y compris construite directement en Python,
    sans quoi un téléversement sortirait du dossier configuré.
    """
    with pytest.raises(vol.Invalid):
        DESTINATION_SCHEMA(config_factice(folder=dossier))

    with pytest.raises(DestinationConfigError):
        DestinationConfig.from_dict(config_factice(folder=dossier))

    with pytest.raises(DestinationConfigError):
        DestinationConfig(
            destination_id="d1",
            provider=PROVIDER_FACTICE,
            name="Destination",
            folder=dossier,
        )


@pytest.mark.parametrize(("dossier", "attendu"), DOSSIERS_ACCEPTES)
def test_le_dossier_accepte_un_chemin_relatif_posix(dossier: str, attendu: str) -> None:
    """Un chemin relatif POSIX est accepté et renvoyé sous sa forme NFKC.

    Couvre les trois points d'entrée, comme le test symétrique sur les
    dossiers refusés : le schéma voluptuous appelé seul (tel qu'utilisé pour
    relire les options de l'entrée), `from_dict()` et la construction directe
    de la dataclass doivent tous renvoyer la même valeur normalisée, sans
    qu'aucun n'altère autrement une valeur pourtant acceptée.

    La sortie attendue est comparée à la forme NFKC et non à l'entrée brute :
    c'est cette forme qui a été validée, et c'est elle qui sera persistée puis
    transmise au fournisseur.
    """
    valide = DESTINATION_SCHEMA(config_factice(folder=dossier))
    depuis_dict = DestinationConfig.from_dict(config_factice(folder=dossier))
    direct = DestinationConfig(
        destination_id="d1",
        provider=PROVIDER_FACTICE,
        name="Destination",
        folder=dossier,
    )

    assert valide[CONF_FOLDER] == attendu
    assert depuis_dict.folder == attendu
    assert direct.folder == attendu


@pytest.mark.parametrize("donnees", ["pas-un-dictionnaire", 12, ["a", "b"]])
def test_la_configuration_refuse_des_donnees_qui_ne_sont_pas_un_dictionnaire(
    donnees: object,
) -> None:
    """Des options modifiées à la main ne doivent pas provoquer d'erreur brute."""
    with pytest.raises(DestinationConfigError):
        DestinationConfig.from_dict(donnees)


def test_la_configuration_refuse_un_champ_inconnu_ou_manquant() -> None:
    """Le schéma refuse une clé inconnue comme une clé obligatoire absente."""
    with pytest.raises(DestinationConfigError):
        DestinationConfig.from_dict(config_factice(inconnu="valeur"))

    incomplete = config_factice()
    del incomplete["provider"]
    with pytest.raises(DestinationConfigError):
        DestinationConfig.from_dict(incomplete)


### SAUVEGARDE DISTANTE ###


def test_la_sauvegarde_distante_se_serialise() -> None:
    """`RemoteBackup.as_dict()` produit une charge utile sérialisable."""
    sauvegarde = RemoteBackup(
        remote_id="abc",
        name="Sauvegarde",
        slug="sauvegarde",
        size=42,
        created_at=datetime(2026, 9, 22, 12, 0, tzinfo=UTC),
        path="Sauvegardes/sauvegarde.tar",
        metadata={"etag": "1"},
    )

    assert sauvegarde.as_dict() == {
        "remote_id": "abc",
        "name": "Sauvegarde",
        "slug": "sauvegarde",
        "size": 42,
        "created_at": "2026-09-22T12:00:00+00:00",
        "path": "Sauvegardes/sauvegarde.tar",
        "metadata": {"etag": "1"},
    }


def test_la_sauvegarde_distante_tolere_les_champs_optionnels() -> None:
    """Seuls l'identifiant distant et le nom sont obligatoires."""
    sauvegarde = RemoteBackup(remote_id="abc", name="Sauvegarde")

    assert sauvegarde.as_dict() == {
        "remote_id": "abc",
        "name": "Sauvegarde",
        "slug": None,
        "size": None,
        "created_at": None,
        "path": None,
        "metadata": {},
    }


@pytest.mark.parametrize(
    "champs",
    [
        {"remote_id": ""},
        {"name": "  "},
        {"size": -1},
        {"size": "42"},
        {"size": True},
    ],
)
def test_la_sauvegarde_distante_refuse_les_champs_invalides(champs: dict) -> None:
    """Identifiant, nom et taille sont validés à la construction."""
    with pytest.raises(DestinationConfigError):
        RemoteBackup(**{"remote_id": "abc", "name": "Sauvegarde"} | champs)


### ERREURS TYPÉES ###


def test_les_erreurs_derivent_de_l_erreur_de_destination() -> None:
    """Toute erreur de destination est attrapable via `DestinationError`."""
    assert issubclass(DestinationError, HomeAssistantError)
    for erreur in (
        DestinationAuthError,
        DestinationQuotaError,
        DestinationNotFoundError,
        DestinationConfigError,
        UnknownProviderError,
        DuplicateProviderError,
    ):
        assert issubclass(erreur, DestinationError)
    assert issubclass(UnknownProviderError, DestinationConfigError)


### REGISTRE DE FOURNISSEURS ###


def test_le_registre_restitue_le_fournisseur_enregistre(
    fournisseur_factice: str,
) -> None:
    """Un fournisseur enregistré est retrouvé et listé."""
    assert get_provider(fournisseur_factice) is DestinationEnMemoire
    assert fournisseur_factice in list_providers()


def test_le_registre_refuse_un_doublon(fournisseur_factice: str) -> None:
    """Enregistrer deux fois le même identifiant est une erreur explicite."""
    with pytest.raises(DuplicateProviderError):
        register_provider(fournisseur_factice, DestinationEnMemoire)


def test_le_registre_signale_un_fournisseur_inconnu() -> None:
    """Demander un fournisseur non enregistré lève `UnknownProviderError`."""
    assert "inexistant" not in list_providers()

    with pytest.raises(UnknownProviderError):
        get_provider("inexistant")

    with pytest.raises(UnknownProviderError):
        unregister_provider("inexistant")


def test_un_fournisseur_s_ajoute_par_decorateur(hass: HomeAssistant) -> None:
    """Le décorateur `provider` enregistre une classe sans toucher au cœur."""

    @provider("factice_decore")
    class AutreDestination(DestinationEnMemoire):
        """Second fournisseur factice, enregistré par décoration."""

    try:
        assert "factice_decore" in list_providers()
        destination = create_destination(
            hass,
            DestinationConfig.from_dict(config_factice(provider="factice_decore")),
        )
        assert isinstance(destination, AutreDestination)
    finally:
        unregister_provider("factice_decore")

    assert "factice_decore" not in list_providers()


def test_la_fabrique_refuse_un_fournisseur_inconnu(hass: HomeAssistant) -> None:
    """`create_destination` remonte l'erreur du registre."""
    config = DestinationConfig.from_dict(config_factice(provider="inexistant"))

    with pytest.raises(UnknownProviderError):
        create_destination(hass, config)


### CYCLE DE VIE D'UNE SAUVEGARDE DISTANTE ###


async def test_la_verification_de_connexion_reussit(hass: HomeAssistant) -> None:
    """`async_check_connection` ne lève rien quand l'accès est valide."""
    destination = _destination(hass)

    await destination.async_check_connection()

    assert destination.connexions_verifiees == 1


async def test_le_televersement_cree_une_sauvegarde_distante(
    hass: HomeAssistant,
) -> None:
    """Le téléversement renvoie la sauvegarde distante créée."""
    destination = _destination(hass)

    sauvegarde = await destination.async_upload(
        "/backup/ha.tar", name="Sauvegarde du 22", slug="abc123"
    )

    assert sauvegarde.remote_id == "destination_test-1"
    assert sauvegarde.name == "Sauvegarde du 22"
    assert sauvegarde.slug == "abc123"
    assert sauvegarde.path == "Sauvegardes/ha.tar"


async def test_le_listage_renvoie_les_sauvegardes_televersees(
    hass: HomeAssistant,
) -> None:
    """Les sauvegardes téléversées sont listées par la destination."""
    destination = _destination(hass)
    assert await destination.async_list_backups() == []

    premiere = await destination.async_upload("/backup/1.tar", name="1")
    seconde = await destination.async_upload("/backup/2.tar", name="2")

    assert await destination.async_list_backups() == [premiere, seconde]


async def test_la_suppression_retire_la_sauvegarde(hass: HomeAssistant) -> None:
    """Une sauvegarde supprimée disparaît du listage."""
    destination = _destination(hass)
    sauvegarde = await destination.async_upload("/backup/1.tar", name="1")

    await destination.async_delete_backup(sauvegarde.remote_id)

    assert await destination.async_list_backups() == []


async def test_la_suppression_d_une_sauvegarde_absente_est_signalee(
    hass: HomeAssistant,
) -> None:
    """Supprimer une sauvegarde inconnue lève `DestinationNotFoundError`."""
    destination = _destination(hass)

    with pytest.raises(DestinationNotFoundError):
        await destination.async_delete_backup("inexistant")


@pytest.mark.parametrize(
    "erreur",
    [
        DestinationError("échec générique"),
        DestinationAuthError("jeton expiré"),
        DestinationQuotaError("quota dépassé"),
    ],
)
async def test_les_erreurs_typees_remontent_a_l_appelant(
    hass: HomeAssistant, erreur: DestinationError
) -> None:
    """Chaque opération propage l'erreur typée levée par le fournisseur."""
    destination = _destination(hass)
    destination.erreur_a_lever = erreur

    with pytest.raises(type(erreur)):
        await destination.async_check_connection()
    with pytest.raises(type(erreur)):
        await destination.async_upload("/backup/1.tar", name="1")
    with pytest.raises(type(erreur)):
        await destination.async_list_backups()
    with pytest.raises(type(erreur)):
        await destination.async_delete_backup("destination_test-1")

    # Un appelant qui ne veut pas distinguer les cas attrape la classe mère.
    with pytest.raises(DestinationError):
        await destination.async_check_connection()


### GESTIONNAIRE DE DESTINATIONS ###


async def test_le_gestionnaire_charge_les_destinations_configurees(
    hass: HomeAssistant, fournisseur_factice: str
) -> None:
    """Le gestionnaire instancie une destination par configuration."""
    gestionnaire = DestinationManager(hass)
    gestionnaire.async_load([config_factice(), config_factice(destination_id="d2")])

    assert len(gestionnaire) == 2
    assert "destination_test" in gestionnaire
    assert [destination.destination_id for destination in gestionnaire] == [
        "destination_test",
        "d2",
    ]
    assert gestionnaire.async_get("d2").name == "Destination de test"
    assert gestionnaire.configs == [
        destination.config for destination in gestionnaire.destinations
    ]


async def test_le_gestionnaire_signale_une_destination_inconnue(
    hass: HomeAssistant, fournisseur_factice: str
) -> None:
    """Demander une destination absente lève `DestinationNotFoundError`."""
    gestionnaire = DestinationManager(hass)
    gestionnaire.async_load([config_factice()])

    with pytest.raises(DestinationNotFoundError):
        gestionnaire.async_get("inexistante")


@pytest.mark.parametrize(
    ("configs", "attendu_dans_le_journal"),
    [
        ([config_factice(provider="inexistant")], "fournisseur inconnu"),
        ([config_factice(retention_days=0)], "configuration invalide"),
        ([config_factice(), config_factice()], "identifiant déjà utilisé"),
    ],
)
async def test_le_gestionnaire_ignore_les_destinations_inutilisables(
    hass: HomeAssistant,
    fournisseur_factice: str,
    caplog: pytest.LogCaptureFixture,
    configs: list[dict],
    attendu_dans_le_journal: str,
) -> None:
    """Une destination inutilisable est ignorée avec un avertissement."""
    gestionnaire = DestinationManager(hass)
    gestionnaire.async_load(configs)

    assert len(gestionnaire) == len(configs) - 1
    assert attendu_dans_le_journal in caplog.text


async def test_le_gestionnaire_remplace_les_destinations_au_rechargement(
    hass: HomeAssistant, fournisseur_factice: str
) -> None:
    """Un second chargement remplace intégralement les destinations."""
    gestionnaire = DestinationManager(hass)
    gestionnaire.async_load([config_factice()])
    gestionnaire.async_load([config_factice(destination_id="d2")])

    assert [destination.destination_id for destination in gestionnaire] == ["d2"]


### CONSTANTES D'ÉVÉNEMENTS ###


def test_les_evenements_du_fork_sont_prefixes_et_distincts() -> None:
    """Les événements de téléversement et de purge distante sont définis."""
    evenements = (
        EVENT_UPLOAD_START,
        EVENT_UPLOAD_SUCCESSFUL,
        EVENT_UPLOAD_FAILED,
        EVENT_REMOTE_PURGE,
    )

    assert len(set(evenements)) == len(evenements)
    for evenement in evenements:
        assert evenement.startswith("auto_backup")


### Données du compte du fournisseur (issues #10 et #13) ###


def test_les_donnees_de_fournisseur_sont_absentes_par_defaut() -> None:
    """Une destination qui n'en a pas est persistée exactement comme avant."""
    config = DestinationConfig.from_dict(config_factice())

    assert config.provider_data is None
    assert CONF_PROVIDER_DATA not in config.as_dict()


def test_des_donnees_de_fournisseur_vides_ne_sont_pas_persistees() -> None:
    """Un fournisseur qui ne décrit pas le compte n'ajoute aucune clé creuse."""
    config = DestinationConfig(
        destination_id="d1",
        provider=PROVIDER_FACTICE,
        name="Destination",
        provider_data={},
    )

    assert config.provider_data is None
    assert CONF_PROVIDER_DATA not in config.as_dict()


def test_les_donnees_du_fournisseur_sont_validees() -> None:
    """`provider_data` n'accepte que des scalaires JSON sous des clés textuelles."""
    config = DestinationConfig(
        destination_id="d1",
        provider=PROVIDER_FACTICE,
        name="Destination",
        provider_data={"account_id": "dbid:factice", "verifie": True, "quota": 2},
    )

    assert config.provider_data == {
        "account_id": "dbid:factice",
        "verifie": True,
        "quota": 2,
    }
    assert config.as_dict()[CONF_PROVIDER_DATA] == config.provider_data


def test_les_donnees_de_fournisseur_sont_relues_et_copiees() -> None:
    """Elles font l'aller-retour, sans partager leur dictionnaire d'origine."""
    origine = {"account_email": "camille@exemple.test", "quota": 42, "actif": True}
    brute = config_factice(provider_data=origine)

    config = DestinationConfig.from_dict(brute)

    assert config.provider_data == origine
    assert config.as_dict()[CONF_PROVIDER_DATA] == origine
    origine["account_email"] = "autre@exemple.test"
    assert config.provider_data["account_email"] == "camille@exemple.test"


def test_les_donnees_de_fournisseur_sont_masquees_dans_les_journaux() -> None:
    """Ce ne sont pas des secrets, mais elles identifient une personne."""
    config = DestinationConfig.from_dict(
        config_factice(provider_data={"account_email": "camille@exemple.test"})
    )

    assert "camille@exemple.test" not in repr(config)
    assert config.as_dict(masquer=True)[CONF_PROVIDER_DATA] == {
        "account_email": VALEUR_MASQUEE
    }
    # Sans masquage, c'est bien la valeur qui est écrite dans l'entrée.
    assert config.as_dict()[CONF_PROVIDER_DATA]["account_email"] == (
        "camille@exemple.test"
    )


@pytest.mark.parametrize(
    "donnees",
    [
        pytest.param("pas un dictionnaire", id="chaine"),
        pytest.param("dbid:factice", id="scalaire_nu"),
        pytest.param(["dbid:factice"], id="liste"),
        pytest.param({"compte": {"email": "a@exemple.test"}}, id="valeur_imbriquee"),
        pytest.param({"account_id": {"imbrique": "interdit"}}, id="compte_imbrique"),
        pytest.param({"compte": ["a@exemple.test"]}, id="valeur_liste"),
        pytest.param({"": "clé vide"}, id="cle_vraiment_vide"),
        pytest.param({"  ": "vide"}, id="cle_vide"),
        pytest.param({1: "cle_non_textuelle"}, id="cle_non_textuelle"),
        pytest.param({f"cle_{i}": i for i in range(21)}, id="trop_de_cles"),
        pytest.param({"compte": "a" * 501}, id="valeur_trop_longue"),
    ],
)
def test_des_donnees_de_fournisseur_aberrantes_sont_refusees(donnees: object) -> None:
    """Le schéma et la dataclass refusent les mêmes valeurs, comme ailleurs."""
    with pytest.raises(vol.Invalid):
        DESTINATION_SCHEMA(config_factice(provider_data=donnees))

    with pytest.raises(DestinationConfigError):
        DestinationConfig.from_dict(config_factice(provider_data=donnees))

    with pytest.raises(DestinationConfigError):
        DestinationConfig(
            destination_id="destination_test",
            provider=PROVIDER_FACTICE,
            name="Destination de test",
            provider_data=donnees,
        )


### ADR ###


def test_l_adr_justifie_la_persistance_et_la_hierarchie_d_erreurs() -> None:
    """L'ADR #6 justifie le support de persistance et la hiérarchie d'erreurs.

    Il ne suffit pas que le fichier existe : il doit trancher explicitement
    entre les options de persistance étudiées et documenter chaque erreur
    typée, faute de quoi une future resynchronisation upstream ne saurait pas
    pourquoi `entry.options` a été préféré aux sous-entrées de configuration.
    """
    adr = (
        Path(__file__).resolve().parent.parent
        / "docs"
        / "adr"
        / "0001-destinations-distantes.md"
    )
    assert adr.is_file(), "l'ADR des destinations distantes est introuvable"

    texte = adr.read_text(encoding="utf-8")

    # Justifie le choix du support de persistance face à l'alternative écartée.
    assert "Décision" in texte
    assert "entry.options" in texte
    assert "ConfigSubentry" in texte or "sous-entrées de configuration" in texte

    # Justifie la hiérarchie d'erreurs, classe par classe.
    for nom_erreur in (
        "DestinationError",
        "DestinationAuthError",
        "DestinationQuotaError",
        "DestinationNotFoundError",
        "DestinationConfigError",
        "UnknownProviderError",
        "DuplicateProviderError",
    ):
        assert nom_erreur in texte, f"{nom_erreur} absente de l'ADR"
