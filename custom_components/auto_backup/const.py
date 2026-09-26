from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.util.hass_dict import HassKey

if TYPE_CHECKING:
    from .manager import AutoBackup
    from .destinations import DestinationManager
    from .destinations.notifications import GestionnaireDeNotifications
    from .destinations.oauth import EtatOAuth
    from .destinations.upload import CoordinateurTeleversement
    from .destinations.retention import (
        CoordinateurPurgeDistante,
        RegistreSauvegardesDistantes,
    )

DOMAIN = "auto_backup"
DATA_AUTO_BACKUP: HassKey[AutoBackup] = HassKey(DOMAIN)
UNSUB_LISTENER = "unsub_listener"

CONF_AUTO_PURGE = "auto_purge"
CONF_BACKUP_TIMEOUT = "backup_timeout"

DEFAULT_BACKUP_TIMEOUT_SECONDS = 1200
DEFAULT_BACKUP_TIMEOUT = 20

EVENT_BACKUP_SUCCESSFUL = f"{DOMAIN}.backup_successful"
EVENT_BACKUP_START = f"{DOMAIN}.backup_start"
EVENT_BACKUP_FAILED = f"{DOMAIN}.backup_failed"
EVENT_BACKUPS_PURGED = f"{DOMAIN}.purged_backups"

STORAGE_KEY = "snapshots_expiry"
STORAGE_VERSION = 1

ATTR_KEEP_DAYS = "keep_days"
ATTR_INCLUDE = "include"
ATTR_INCLUDE_ADDONS = "include_addons"
ATTR_INCLUDE_FOLDERS = "include_folders"
ATTR_EXCLUDE = "exclude"
ATTR_EXCLUDE_ADDONS = "exclude_addons"
ATTR_EXCLUDE_FOLDERS = "exclude_folders"
ATTR_EXCLUDE_DATABASE = "exclude_database"
ATTR_DOWNLOAD_PATH = "download_path"
ATTR_COMPRESSED = "compressed"
ATTR_ENCRYPTED = "encrypted"
ATTR_LOCATION = "location"

ATTR_LAST_FAILURE = "last_failure"
ATTR_PURGEABLE = "purgeable_backups"
ATTR_MONITORED = "monitored_backups"
ATTR_ERROR = "error"
ATTR_SLUG = "slug"

DEFAULT_BACKUP_FOLDERS = {
    "ssl": "ssl",
    "share": "share",
    "media": "media",
    "addons": "addons/local",
    "config": "homeassistant",
    "local add-ons": "addons/local",
    "home assistant configuration": "homeassistant",
}

SERVICE_PURGE = "purge"
SERVICE_BACKUP = "backup"
SERVICE_BACKUP_FULL = "backup_full"
SERVICE_BACKUP_PARTIAL = "backup_partial"

### DESTINATIONS DISTANTES ###
# Ajouts du fork (cf. docs/UPSTREAM.md) : constantes du sous-paquet `destinations`.
DATA_DESTINATIONS: HassKey[DestinationManager] = HassKey(f"{DOMAIN}_destinations")

CONF_DESTINATIONS = "destinations"
CONF_DESTINATION_ID = "destination_id"
CONF_PROVIDER = "provider"
CONF_FOLDER = "folder"
CONF_RETENTION_DAYS = "retention_days"
CONF_RETENTION_COUNT = "retention_count"

DEFAULT_DESTINATION_FOLDER = "Home Assistant"

# Les noms d'événements suivent la convention upstream `<domaine>.<événement>`
# (cf. EVENT_BACKUP_* ci-dessus) pour rester homogènes dans les automatisations.
EVENT_UPLOAD_START = f"{DOMAIN}.upload_start"
EVENT_UPLOAD_SUCCESSFUL = f"{DOMAIN}.upload_successful"
EVENT_UPLOAD_FAILED = f"{DOMAIN}.upload_failed"
EVENT_REMOTE_PURGE = f"{DOMAIN}.remote_purge"

### TÉLÉVERSEMENT APRÈS CRÉATION (issue #8) ###
# Coordinateur qui corrèle un appel de service avec la sauvegarde créée, puis
# téléverse celle-ci en tâche de fond (cf. `destinations/upload.py`).
DATA_UPLOADS: HassKey[CoordinateurTeleversement] = HassKey(f"{DOMAIN}_uploads")

# Option des services `backup`, `backup_full` et `backup_partial` : la ou les
# destinations distantes vers lesquelles envoyer la sauvegarde créée.
ATTR_UPLOAD_TO = "upload_to"

# Champs des événements `auto_backup.upload_*`, en complément d'ATTR_NAME,
# ATTR_SLUG et ATTR_ERROR ci-dessus.
ATTR_DESTINATION = "destination"
ATTR_DESTINATION_NAME = "destination_name"
ATTR_SIZE = "size"
ATTR_REMOTE_ID = "remote_id"

# Délai maximum d'un téléversement, en secondes. Lu dans `entry.options`, où il
# est réglé par l'étape « Réglages du téléversement » du flux d'options du fork
# (cf. `destinations/flow.py` et docs/UPSTREAM.md).
CONF_UPLOAD_TIMEOUT = "upload_timeout"
DEFAULT_UPLOAD_TIMEOUT = 1800

# Options portées par le fork, et elles seules. Le flux d'options upstream
# remplace l'intégralité des options par le contenu de son formulaire, qui
# ignore ces clés : elles doivent lui être reportées à chaque enregistrement,
# sans quoi elles seraient effacées en silence (cf.
# `destinations/config_entry.py`, `preserve_fork_options()`). Toute option
# ajoutée par le fork doit donc être inscrite ici.
CLES_DU_FORK = (CONF_DESTINATIONS, CONF_UPLOAD_TIMEOUT)

### AUTORISATION OAUTH2 DES DESTINATIONS (issue #7) ###
# Ajouts du fork (cf. docs/UPSTREAM.md) : flux d'autorisation OAuth2 conduit depuis
# le flux d'options. Les identifiants d'application (`client_id`, `client_secret`) et
# le jeton (`token`) sont portés par chaque destination ; leurs clés viennent de
# `homeassistant.const` (CONF_CLIENT_ID, CONF_CLIENT_SECRET, CONF_TOKEN).

# Chemin de la vue qui reçoit le retour d'autorisation du fournisseur. Il est propre
# au fork : la vue standard de Home Assistant (`/auth/external/callback`) ne sait
# reprendre qu'un *config flow*, pas un flux d'options.
OAUTH_CALLBACK_PATH = f"/auth/{DOMAIN}/callback"

# États d'autorisation en attente : jeton aléatoire -> flux d'options à reprendre.
DATA_OAUTH_STATES: HassKey[dict[str, EtatOAuth]] = HassKey(f"{DOMAIN}_oauth_states")
# Vrai une fois la vue de retour enregistrée auprès du serveur HTTP.
DATA_OAUTH_VIEW: HassKey[bool] = HassKey(f"{DOMAIN}_oauth_view")

# Durée de vie d'un état d'autorisation, en secondes : au-delà, le retour du
# fournisseur est refusé et l'utilisateur doit relancer le flux.
OAUTH_STATE_TTL = 900

# Délais des deux appels sortants du flux, en secondes.
OAUTH_AUTHORIZE_URL_TIMEOUT = 30
OAUTH_TOKEN_TIMEOUT = 30

# Préfixe de l'identifiant du problème (« repair issue ») signalant qu'une
# destination doit être ré-autorisée.
ISSUE_REAUTH_PREFIX = "reauthentification_requise_"

# Identifiant de la destination fictive portée par le flux d'ajout : l'autorisation
# précède la création de la destination, et l'implémentation OAuth2 a besoin d'un
# identifiant. Il est partagé par le flux (`destinations/flow.py`), qui le produit,
# et par le signalement de ré-authentification (`destinations/reauth.py`), qui doit
# le reconnaître pour ne pas alerter sur une destination qui n'existe pas. Aucune
# destination réelle ne peut le porter : `_identifiant_disponible()` l'exclut.
IDENTIFIANT_PROVISOIRE = "autorisation_en_cours"

### FOURNISSEURS DE DESTINATION RÉELS (issues #10 et #13) ###
# Ajouts du fork (cf. docs/UPSTREAM.md). Les fournisseurs livrés vivent dans
# `destinations/providers/` et s'enregistrent par `enregistrer_les_fournisseurs()`.

# Données **non secrètes** renvoyées par le fournisseur au moment de l'autorisation et
# conservées avec la destination : identifiant du compte Dropbox (`account_id`) ou
# adresse du compte Google Drive (`account_email`), par exemple. Elles évitent de
# rappeler l'API pour savoir à quel compte une destination est rattachée, et servent à
# détecter qu'une ré-autorisation a changé de compte. Facultatif : une destination qui
# n'en a pas est persistée exactement comme avant.
CONF_PROVIDER_DATA = "provider_data"

### RÉTENTION ET PURGE DISTANTES (issue #9) ###
# Ajouts du fork (cf. docs/UPSTREAM.md). Toute la logique vit dans
# `destinations/retention.py` ; `manager.py` n'est pas touché.

# Registre persistant des sauvegardes déposées par le fork chez un fournisseur :
# destination -> liste d'entrées {remote_id, slug, name, created_at, size}. C'est
# lui qui rend une sauvegarde distante purgeable : un fichier que l'utilisateur a
# déposé lui-même n'y figure pas, donc n'est jamais supprimé (cf. l'ADR).
STORAGE_KEY_REMOTE_BACKUPS = "remote_backups"
STORAGE_VERSION_REMOTE_BACKUPS = 1

DATA_REMOTE_BACKUPS: HassKey[RegistreSauvegardesDistantes] = HassKey(
    f"{DOMAIN}_remote_backups"
)
DATA_REMOTE_PURGE: HassKey[CoordinateurPurgeDistante] = HassKey(
    f"{DOMAIN}_remote_purge"
)

# Champs propres au registre et à l'événement `auto_backup.remote_purge`, en
# complément d'ATTR_DESTINATION, ATTR_DESTINATION_NAME, ATTR_REMOTE_ID,
# ATTR_SIZE, ATTR_SLUG et ATTR_NAME ci-dessus.
ATTR_CREATED_AT = "created_at"
ATTR_REMOTE_IDS = "remote_ids"

# Délai maximum, en secondes, d'un appel réseau du coordinateur de purge : un
# listage de destination, une suppression de sauvegarde. C'est un **filet de
# sécurité**, pas un réglage : un fournisseur dont l'appel pend bloquerait sinon
# la purge de sa destination — et le verrou qui la sérialise — indéfiniment,
# sans erreur ni fin. Le contrat de `RemoteDestination` demande à chaque
# fournisseur de borner lui-même ses appels, bien plus finement ; cette valeur
# n'a donc à se déclencher que si aucun ne l'a fait.
#
# Volontairement **pas** une option de l'interface : la purge n'a aucune étape
# de réglages. Les deux que le fork ajoute au menu d'options portent sur autre
# chose — le téléversement (#8, CONF_UPLOAD_TIMEOUT) et les notifications (#17,
# CONF_NOTIFY_ON_FAILURE) —, et en ouvrir une pour la purge demanderait une
# issue à elle. La constante n'est donc pas inscrite dans CLES_DU_FORK : rien ne
# la persiste dans les options.
DEFAULT_PURGE_TIMEOUT = 300

### NOTIFICATIONS PERSISTANTES (issue #17) ###
# Ajouts du fork (cf. docs/UPSTREAM.md). Une sauvegarde cloud silencieusement cassée
# donne une fausse impression de sécurité : `destinations/notifications.py` transforme
# les événements `auto_backup.upload_*` et le signalement de ré-authentification
# (`destinations/reauth.py`) en notifications persistantes lisibles.

# Gestionnaire de notifications de l'entrée, exposé dans `hass.data`.
DATA_NOTIFICATIONS: HassKey[GestionnaireDeNotifications] = HassKey(
    f"{DOMAIN}_notifications"
)

# Préfixes des identifiants de notification. Ils sont **stables par destination** :
# des échecs successifs mettent la même notification à jour au lieu d'en empiler une
# par sauvegarde, et un succès (ou une ré-autorisation) sait laquelle retirer.
NOTIFICATION_UPLOAD_PREFIX = f"{DOMAIN}_upload_"
NOTIFICATION_REAUTH_PREFIX = f"{DOMAIN}_reauth_"

# Option de l'entrée : les notifications persistantes du fork sont-elles créées ?
# Désactivée, l'intégration continue d'émettre ses événements et ses journaux
# d'erreur, et le problème Home Assistant de ré-authentification reste créé : seule
# la notification disparaît. Elle se règle par l'étape « Réglages des notifications »
# du flux d'options (cf. `destinations/flow.py`).
CONF_NOTIFY_ON_FAILURE = "notify_on_failure"
DEFAULT_NOTIFY_ON_FAILURE = True

# `notify_on_failure` est une option portée par le fork : elle doit être reportée par
# `preserve_fork_options()` comme les autres, sans quoi le premier enregistrement du
# formulaire upstream l'effacerait en silence. La constante étant définie ici, à la
# fin du bloc du fork, la liste des clés du fork est complétée ici aussi plutôt que
# récrite plus haut.
CLES_DU_FORK = (*CLES_DU_FORK, CONF_NOTIFY_ON_FAILURE)
