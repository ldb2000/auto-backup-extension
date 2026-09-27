# Services, options et rétention distante

Cette page décrit les services exposés par Auto Backup, chacune de leurs options, les
événements émis et la façon dont les sauvegardes sont conservées — sur votre instance
(rétention locale) comme chez Dropbox ou Google Drive (rétention distante). Elle se termine
par une automatisation complète, prête à adapter.

Pour connecter un compte cloud, commencez par le guide de votre fournisseur :
[Dropbox](destinations/dropbox.md) ou [Google Drive](destinations/google-drive.md). Les
questions fréquentes (taille, quotas, chiffrement, jetons) sont dans la [FAQ](faq.md).

## Les services

| Service | Rôle |
| --- | --- |
| `auto_backup.backup` | Crée une sauvegarde complète ou partielle, selon les inclusions et exclusions indiquées. |
| `auto_backup.backup_full` | Crée une sauvegarde complète, avec des exclusions facultatives. |
| `auto_backup.backup_partial` | Crée une sauvegarde partielle : seuls les modules complémentaires et dossiers listés. |
| `auto_backup.purge` | Supprime les sauvegardes expirées : d'abord les sauvegardes locales, puis celles de chaque destination distante. |

Les trois services de sauvegarde partagent les **options communes** ci-dessous ; chacun y
ajoute ses options de sélection. `auto_backup.purge` n'a aucune option.

### Options communes aux trois services de sauvegarde

| Option | Type | Par défaut | Effet |
| --- | --- | --- | --- |
| `name` | texte | date et heure courantes | Nom de la sauvegarde. Accepte un modèle (`{{ now().strftime('%Y-%m-%d') }}`) dans une automatisation. |
| `keep_days` | nombre (jours, décimales permises) | aucun : conservée indéfiniment | **Rétention locale** : nombre de jours pendant lesquels la sauvegarde est conservée sur l'instance. |
| `exclude_database` | booléen | `false` | Exclut la base de données intégrée de Home Assistant. |
| `encrypted` | booléen | `false` | Chiffre la sauvegarde avec la clé de chiffrement par défaut définie dans les paramètres de sauvegarde de Home Assistant. |
| `password` | texte | aucun | Mot de passe personnalisé qui chiffre la sauvegarde. Sans `password` et avec `encrypted: false`, la sauvegarde n'est pas chiffrée. |
| `location` | texte | stockage par défaut (`/backup`) | Stockage réseau de sauvegarde où créer la sauvegarde (installation supervisée). |
| `download_path` | liste de dossiers | aucun | Dossiers **locaux** (clé USB, partage monté…) où copier la sauvegarde après sa création. Chaque dossier doit exister. |
| `compressed` | booléen | `true` | Utilise des archives compressées. |
| `upload_to` | liste de destinations | aucune | **Destinations distantes** (Dropbox, Google Drive) vers lesquelles envoyer la sauvegarde après sa création. Voir ci-dessous. |

### Options de sélection

| Service | Option | Effet |
| --- | --- | --- |
| `auto_backup.backup` | `include_addons` | Modules complémentaires à sauvegarder (nom ou slug). Leur présence rend la sauvegarde partielle. |
| `auto_backup.backup` | `include_folders` | Dossiers à sauvegarder : `config`, `share`, `ssl`, `media`, `addons`. |
| `auto_backup.backup` | `exclude_addons` | Modules complémentaires à exclure d'une sauvegarde complète. |
| `auto_backup.backup` | `exclude_folders` | Dossiers à exclure d'une sauvegarde complète. |
| `auto_backup.backup_full` | `exclude` | Dictionnaire `{"addons": [...], "folders": [...]}` des éléments à exclure. |
| `auto_backup.backup_partial` | `addons` | Modules complémentaires à inclure (nom ou slug). |
| `auto_backup.backup_partial` | `folders` | Dossiers à inclure. |

Les modules complémentaires et les dossiers n'existent que sur une installation supervisée
(Home Assistant OS ou Supervised), de même que `location` et `compressed`. Sur Home Assistant
Container ou Core, la sauvegarde contient toujours la configuration de Home Assistant, et les
options de sélection sont sans effet.

## L'option `upload_to`

`upload_to` reçoit une destination ou une liste de destinations, chacune désignée par :

- son **nom**, tel qu'il apparaît dans les options de l'intégration (`Dropbox – Jeanne Dupont`),
  sans tenir compte des majuscules ;
- ou son **identifiant**, dérivé du nom au moment de l'ajout (`dropbox_jeanne_dupont`) et qui ne
  change plus ensuite.

```yaml
action: auto_backup.backup
data:
  upload_to:
    - Dropbox – Jeanne Dupont
    - google_drive_camille_martin
```

Pour retrouver l'identifiant exact, appelez le service avec un nom quelconque : le message
d'erreur liste les destinations configurées, avec leur nom et leur identifiant.

Ce qui se passe à l'appel :

1. Les destinations demandées sont vérifiées **avant** la création de la sauvegarde : une
   destination inconnue, ou un nom porté par deux destinations, fait échouer l'appel avec un
   message affiché dans la langue de l'interface, et aucune sauvegarde n'est créée.
2. La sauvegarde est créée localement, exactement comme sans `upload_to`.
3. Elle est ensuite envoyée **en tâche de fond**, destination après destination. L'appel de
   service rend la main sans attendre la fin des envois.
4. Un envoi en échec n'empêche pas les destinations suivantes d'être traitées, et **ne supprime
   ni n'altère jamais la sauvegarde locale**.

La sauvegarde envoyée est **le fichier `.tar` tel que Home Assistant l'a créé** : s'il est
chiffré (`encrypted` ou `password`), la copie distante l'est aussi. Chaque envoi est limité par
le délai réglé dans **Configurer → Réglages du téléversement** (`upload_timeout`, 1800 secondes
par défaut).

## Rétention locale et rétention distante

Les deux rétentions sont **indépendantes** : l'une ne supprime jamais ce qui relève de l'autre.

| | Rétention locale | Rétention distante |
| --- | --- | --- |
| Porte sur | les sauvegardes présentes sur l'instance | les sauvegardes déposées par Auto Backup dans le dossier d'une destination |
| Se règle | à chaque appel de service, par `keep_days` | une fois pour toutes, **sur la destination**, à son ajout |
| Critère | l'âge de chaque sauvegarde | l'âge (`retention_days`), le nombre (`retention_count`), ou les deux |
| Sans réglage | la sauvegarde est conservée indéfiniment | la destination n'est jamais purgée |

### Régler la rétention d'une destination

À l'ajout d'une destination (**Configurer → Ajouter une destination**), le dernier formulaire
propose deux champs facultatifs :

- **Conserver pendant (jours)** (`retention_days`) : les sauvegardes distantes plus anciennes
  que ce nombre de jours sont supprimées ;
- **Nombre maximum de sauvegardes conservées** (`retention_count`) : au-delà, les plus anciennes
  sont supprimées.

Les deux valeurs sont des entiers strictement positifs. Réglées ensemble, elles se combinent :
les sauvegardes trop anciennes partent d'abord, puis, s'il en reste plus que le nombre autorisé,
les plus anciennes du lot restant, jusqu'à revenir sous la limite. La rétention n'est pas
modifiable après l'ajout : pour la changer, supprimez puis ajoutez de nouveau la destination.

### Quand la purge distante s'exécute

- **Après chaque envoi réussi** vers une destination, si l'option **« Suppression automatique des
  sauvegardes expirées »** (`auto_purge`) est cochée dans **Configurer → Réglages des
  sauvegardes**. C'est la même option qui déclenche la purge locale après chaque sauvegarde.
  Décochée, aucune suppression distante automatique n'a lieu.
- **À chaque appel de `auto_backup.purge`**, qui purge d'abord les sauvegardes locales expirées,
  puis chaque destination distante configurée.

### Ce qui peut être supprimé

Seules les sauvegardes qu'Auto Backup a **lui-même déposées** : il tient un registre de ses
envois dans le stockage de Home Assistant (`auto_backup.remote_backups`) et, à défaut, les
reconnaît au marqueur posé sur le fichier (Google Drive) ou à la forme de leur nom
(`Sauvegarde du soir [a1b2c3d4].tar`, Dropbox). Vos propres fichiers et les sous-dossiers ne
sont jamais touchés.

> **Un dossier distant par instance Home Assistant.** Si plusieurs instances envoient leurs
> sauvegardes dans le même dossier distant, chacune reconnaît aussi les sauvegardes des autres
> comme les siennes : elle les compte dans sa rétention, et peut les supprimer. Donnez à chaque
> instance son propre dossier distant (`Home Assistant/Maison`, `Home Assistant/Chalet`…), chez
> Dropbox comme sur Google Drive.

Les suppressions sont réelles : **définitives chez Google Drive** (sans passer par la corbeille),
et via la corbeille chez Dropbox. Le détail est dans chaque guide :
[Dropbox](destinations/dropbox.md#rétention--ce-qui-est-supprimé-et-ce-qui-ne-lest-jamais),
[Google Drive](destinations/google-drive.md#rétention--ce-qui-est-supprimé-et-comment).

Une destination en attente de ré-autorisation est sautée sans être contactée, et une suppression
en échec n'interrompt pas la purge des suivantes.

## Les événements

Chaque événement se capte dans une automatisation par un déclencheur `event`, et ses champs se
lisent dans `trigger.event.data`.

| Événement | Émis quand | Champs |
| --- | --- | --- |
| `auto_backup.backup_start` | une sauvegarde commence | `name` |
| `auto_backup.backup_successful` | une sauvegarde locale est créée | `name`, `slug` |
| `auto_backup.backup_failed` | la création d'une sauvegarde échoue | `name`, `error` |
| `auto_backup.purged_backups` | des sauvegardes locales ont été supprimées | `backups` (liste des slugs) |
| `auto_backup.upload_start` | l'envoi vers une destination commence | `name`, `slug`, `destination`, `destination_name` |
| `auto_backup.upload_successful` | l'envoi vers une destination a réussi | `name`, `slug`, `destination`, `destination_name`, `size`, `remote_id` |
| `auto_backup.upload_failed` | l'envoi vers une destination a échoué | `name`, `slug`, `destination`, `destination_name`, `error`, `error_code` |
| `auto_backup.remote_purge` | la purge d'une destination a supprimé au moins une sauvegarde | `destination`, `destination_name`, `remote_ids` |

`destination` est l'identifiant de la destination et `destination_name` son nom lisible.
Dans `auto_backup.upload_failed`, deux champs décrivent la cause de l'échec :

- `error_code` est un **code stable**, le même quelle que soit la langue et quel que soit le
  fournisseur (voir [Codes d'erreur](#codes-derreur)) : c'est lui qu'une automatisation compare ;
- `error` est un **message à afficher**, traduit dans la langue de Home Assistant au moment de
  l'échec (français ou anglais, l'anglais pour toute autre langue). Il est écrit par Auto Backup :
  il ne contient ni le texte renvoyé par le fournisseur ni aucun secret. Le détail technique de
  l'échec (statut HTTP, motif du fournisseur, avec ses secrets masqués) est consigné dans le
  journal de Home Assistant, et là seulement.

`auto_backup.remote_purge` n'est pas émis quand rien n'a été supprimé.

Un échec d'envoi déclenche aussi, par défaut, une notification persistante et allume l'entité
« *Destination* : problème de téléversement » (voir le [README](../README.md#notifications)).

## Codes d'erreur

Valeurs possibles du champ `error_code` de `auto_backup.upload_failed`. Elles forment un contrat :
de nouvelles valeurs peuvent apparaître, aucune n'est renommée ni retirée.

| Code | Cause |
| --- | --- |
| `access_revoked` | l'accès a été révoqué ou a expiré : la destination attend une nouvelle autorisation |
| `missing_scope` | l'autorisation accordée n'inclut pas toutes les permissions demandées |
| `quota_exceeded` | l'espace de stockage du compte est plein |
| `rate_limited` | le fournisseur limite temporairement le nombre de requêtes |
| `timeout` | le délai imparti est dépassé (délai de téléversement ou d'une requête) |
| `network_error` | le fournisseur est injoignable (coupure réseau) |
| `provider_unavailable` | le fournisseur est en panne passagère (erreur serveur) |
| `invalid_folder` | le dossier distant est invalide ou refusé par le fournisseur |
| `api_disabled` | l'API Google Drive n'est pas activée sur le projet Google Cloud |
| `not_found` | l'élément demandé n'existe pas (ou plus) chez le fournisseur |
| `unknown_destination` | la destination a été supprimée depuis l'appel du service |
| `local_backup_unreadable` | la sauvegarde locale n'a pas pu être lue pour être envoyée |
| `invalid_config` | la configuration de la destination est invalide ou incomplète |
| `unknown_provider` | le fournisseur de la destination n'est pas pris en charge |
| `unknown` | cause non reconnue : le message est générique, le détail est dans le journal |

Un même code vaut pour Dropbox et Google Drive : un espace plein est `quota_exceeded` chez l'un
comme chez l'autre. Pour réagir à une cause précise, filtrez sur ce code :

```yaml
- id: destination_pleine
  alias: Alerte quand une destination cloud est pleine
  mode: queued
  triggers:
    - trigger: event
      event_type: auto_backup.upload_failed
      event_data:
        error_code: quota_exceeded
  actions:
    - action: persistent_notification.create
      data:
        title: Destination cloud pleine
        message: >-
          {{ trigger.event.data.destination_name }} : {{ trigger.event.data.error }}
```

## Exemple complet : sauvegarde quotidienne envoyée dans le cloud

Objectif : chaque nuit à 3 h, une sauvegarde complète, chiffrée par un mot de passe, gardée
7 jours sur l'instance et envoyée chez Dropbox, où les 14 dernières sont conservées pendant
au plus 30 jours.

**1. Régler la destination (une seule fois).** Dans **Configurer → Ajouter une destination**,
connectez votre compte (voir le guide [Dropbox](destinations/dropbox.md) ou
[Google Drive](destinations/google-drive.md)), puis, sur le dernier formulaire, saisissez :

- Nom : `Dropbox – Jeanne Dupont` (proposé d'après le compte) ;
- Dossier distant : `Home Assistant/Maison` (un dossier par instance) ;
- Conserver pendant (jours) : `30` ;
- Nombre maximum de sauvegardes conservées : `14`.

**2. Activer la purge automatique.** Dans **Configurer → Réglages des sauvegardes**, cochez
« Suppression automatique des sauvegardes expirées » : la sauvegarde locale de plus de 7 jours
est alors supprimée après chaque nouvelle sauvegarde, et la destination est purgée après chaque
envoi réussi.

**3. Ranger le mot de passe dans `secrets.yaml`**, jamais en clair dans l'automatisation :

```yaml
mot_de_passe_sauvegarde: "une-phrase-de-passe-longue-et-unique"
```

**4. Écrire les automatisations** (dans `automations.yaml`, ou dans la configuration sous la clé
`automation:`) :

```yaml
- id: sauvegarde_quotidienne_cloud
  alias: Sauvegarde quotidienne envoyée chez Dropbox
  mode: single
  triggers:
    - trigger: time
      at: "03:00:00"
  actions:
    - action: auto_backup.backup_full
      data:
        name: "Sauvegarde quotidienne {{ now().strftime('%Y-%m-%d') }}"
        # Rétention locale : 7 jours sur l'instance.
        keep_days: 7
        # Chiffrement : la copie distante est chiffrée elle aussi.
        password: !secret mot_de_passe_sauvegarde
        # Envoi vers la destination, qui applique sa propre rétention (30 jours, 14 sauvegardes).
        upload_to:
          - Dropbox – Jeanne Dupont

- id: sauvegarde_cloud_en_echec
  alias: Alerte si l'envoi d'une sauvegarde échoue
  mode: queued
  triggers:
    - trigger: event
      event_type: auto_backup.upload_failed
  actions:
    - action: persistent_notification.create
      data:
        title: Sauvegarde distante en échec
        message: >-
          La sauvegarde « {{ trigger.event.data.name }} » n'a pas pu être envoyée vers
          {{ trigger.event.data.destination_name }} : {{ trigger.event.data.error }}

- id: purge_hebdomadaire_des_sauvegardes
  alias: Purge hebdomadaire des sauvegardes expirées
  mode: single
  triggers:
    - trigger: time
      at: "04:30:00"
  conditions:
    - condition: time
      weekday:
        - sun
  actions:
    - action: auto_backup.purge
```

La troisième automatisation est un filet de sécurité : elle applique les deux rétentions même si
la purge automatique est décochée, ou si une destination était à ré-autoriser au moment de
l'envoi. Elle est inutile si la purge automatique est active et que tout se passe bien.

La deuxième double la notification persistante qu'Auto Backup affiche déjà en cas d'échec :
remplacez `persistent_notification.create` par votre service de notification mobile
(`notify.mobile_app_<appareil>`) pour être prévenu hors de l'interface.

Pour Google Drive, seule la valeur de `upload_to` change (`Google Drive – Camille Martin`, ou
l'identifiant de la destination). Pour envoyer la même sauvegarde vers les deux fournisseurs,
listez les deux destinations sous `upload_to`.
