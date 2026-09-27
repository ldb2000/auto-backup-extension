# auto-backup-extension

[![CI](https://github.com/ldb2000/auto-backup-extension/actions/workflows/ci.yml/badge.svg)](https://github.com/ldb2000/auto-backup-extension/actions/workflows/ci.yml)

Objectif : reprendre le projet https://github.com/jcwillox/hass-auto-backup et rajouter la possibilité de sauvegarder directement sur Dropbox et Google Drive. 

## Fork et licence

Ce projet est un **fork de [jcwillox/hass-auto-backup](https://github.com/jcwillox/hass-auto-backup)**,
distribué sous **licence MIT**. Le code de l'intégration Home Assistant `auto_backup` présent dans
`custom_components/auto_backup/` a été importé à l'identique depuis une révision upstream figée ;
le copyright de l'auteur d'origine, Joshua Cowie-Willox, est conservé dans le fichier `LICENSE`
aux côtés de celui du fork. Le domaine `auto_backup` et le nom « Auto Backup » sont inchangés,
afin de rester compatible avec les configurations existantes.

La révision upstream importée, la date d'import et la procédure de resynchronisation sont
documentées dans [`docs/UPSTREAM.md`](docs/UPSTREAM.md).

### Ce que ce fork ajoute

En plus des fonctionnalités de l'upstream (sauvegardes complètes ou partielles, rétention locale,
capteurs d'état), ce fork vise l'envoi automatique des sauvegardes vers le cloud.

**Actuellement disponible : connexion des comptes cloud, mécanique de téléversement, rétention
distante et dépôt effectif des sauvegardes chez Dropbox comme sur Google Drive.** L'option
`upload_to` des services de sauvegarde envoie la sauvegarde créée vers les destinations
configurées (voir « Téléversement des sauvegardes » ci-dessous), et chaque destination applique
sa propre rétention aux sauvegardes qu'elle a reçues (voir « Rétention distante »).

- **Dropbox** : la **connexion du compte et le dépôt des sauvegardes sont disponibles** — voir le
  guide [Connecter un compte Dropbox](docs/destinations/dropbox.md) ; le listage et la purge
  distante arrivent avec l'issue #12. Tant qu'ils manquent, la rétention distante ne peut pas
  s'appliquer à une destination Dropbox : elle est configurable, mais aucune sauvegarde n'y est
  encore supprimée.
- **Google Drive** : la **connexion du compte et le téléversement sont disponibles** — voir le
  guide [Connecter Google Drive](docs/destinations/google-drive.md) ; le listage et la suppression
  chez Google arrivent avec l'issue #15. Tant qu'ils manquent, la rétention distante ne peut pas
  s'appliquer à une destination Google Drive : elle est configurable, mais aucune sauvegarde n'y
  est encore supprimée.

La configuration des destinations se fait depuis l'interface de Home Assistant, avec les
identifiants d'application OAuth de l'utilisateur : aucun secret n'est stocké dans ce dépôt.

Les options de l'intégration s'ouvrent sur un menu : **Ajouter une destination**,
**Ré-autoriser une destination**, **Supprimer une destination**, **Réglages du téléversement**
(délai maximum accordé à l'envoi d'une sauvegarde, 1800 secondes par défaut), **Réglages des
notifications** (voir « Notifications » ci-dessous), et les **réglages des sauvegardes**
d'origine. L'ajout d'une destination cloud demande l'identifiant et le secret
d'une application OAuth2 créée par vos soins chez le fournisseur, dans laquelle vous déclarez
l'URL de redirection affichée par le formulaire — de la forme
`https://votre-instance/auth/auto_backup/callback`. 

Votre instance doit donc avoir une **URL externe configurée** (Paramètres > Système > Réseau)
pour que le fournisseur puisse vous y ramener. Cette URL doit être :
- **HTTPS** (pas HTTP) : requis par les fournisseurs pour des raisons de sécurité ;
- **publiquement accessible** : elle ne peut pas être locale (`.local`) ou basée sur une adresse
  IP nue (par exemple, `192.168.1.10`). Certains fournisseurs comme Google Drive refusent les
  URI locales ou privées.

Si l'accès à une destination est révoqué, Home Assistant crée un **problème** nommant cette
destination et invitant à la ré-autoriser, et une **notification persistante** le rappelle à
l'écran d'accueil ; les autres destinations continuent de fonctionner. Les deux disparaissent
ensemble une fois la destination ré-autorisée ou supprimée.

Si la ré-autorisation porte sur un **autre compte** que celui enregistré, l'interface le dit
avant d'enregistrer quoi que ce soit : elle nomme le compte précédent et le nouveau, et prévient
que les sauvegardes déjà déposées sur l'ancien compte ne seront plus ni listées ni purgées par
Auto Backup. Sans confirmation, rien n'est modifié.

### Téléversement des sauvegardes

Une sauvegarde créée par les services `auto_backup.backup`, `backup_full` ou `backup_partial` peut
être téléversée automatiquement vers une ou plusieurs destinations configurées, en ajoutant
l'option `upload_to` à l'appel de service :

```yaml
service: auto_backup.backup
data:
  upload_to:
    - destination-dropbox-perso
    - nom-autre-destination
```

L'option `upload_to` accepte une liste d'identifiants ou de noms de destinations. La sauvegarde
est créée localement en premier, puis envoyée en tâche de fond, destination après destination.
Un échec de téléversement n'empêche pas les autres destinations d'être traitées et ne supprime
jamais la sauvegarde locale.

Le **délai maximum d'un téléversement** est configurable par l'entrée « Réglages du
téléversement » du menu d'options de l'intégration (délai par défaut : 1800 secondes, soit
30 minutes). Cette valeur se relit à chaque envoi et s'applique donc sans redémarrage.

**Google Drive.** Le dépôt utilise l'envoi « resumable » de l'API Drive : la sauvegarde part par
fragments de 8 Mio, sans jamais être chargée entière en mémoire ni recopiée sur le disque, et une
erreur passagère (limitation de débit, erreur serveur) est réessayée avec un délai croissant. Le
dossier distant est créé par l'intégration au premier envoi, puis réutilisé ; chaque fichier est
nommé `<nom de la sauvegarde> [<slug>].tar` et porte un marqueur d'origine Auto Backup.

**Événements** : trois événements sont émis pendant le téléversement :
- `auto_backup.upload_start` : le téléversement vers une destination commence ;
- `auto_backup.upload_successful` : le téléversement a réussi (champs : `name`, `slug`,
  `destination`, `destination_name`, `size`, `remote_id`) ;
- `auto_backup.upload_failed` : le téléversement a échoué (champs : `name`, `slug`,
  `destination`, `destination_name`, `error`).

Chez **Dropbox**, la sauvegarde est déposée dans le dossier de la destination sous le nom
`<nom de la sauvegarde> [<slug>].tar`. Un fichier de même nom n'est **jamais** remplacé : le
téléversement échoue en le disant. Le détail (dossier, fragmentation des grosses sauvegardes,
limites) est dans [le guide Dropbox](docs/destinations/dropbox.md).

### Rétention distante

Chaque destination a **sa propre rétention**, réglée à son ajout : une durée de conservation en
jours, un nombre maximum de sauvegardes, ou les deux. Les deux se combinent : les sauvegardes
trop anciennes partent d'abord, puis, s'il en reste plus que le nombre autorisé, les plus
anciennes du lot restant sont supprimées jusqu'à revenir sous la limite. Une destination sans
aucune rétention n'est jamais purgée.

> **Aucun fournisseur ne sait encore supprimer.** La mécanique décrite ci-dessous est en place,
> mais supprimer suppose de lister d'abord, et aucun fournisseur livré ne le fait : la purge
> d'une destination Dropbox (issue #12) comme d'une destination Google Drive (issue #15) s'arrête
> au listage, avec un message de journal explicite qui nomme la destination. La rétention que
> vous réglez aujourd'hui est enregistrée et s'appliquera sans rien reconfigurer.

**Rien de ce que vous avez déposé vous-même n'est supprimé.** Auto Backup tient un registre
persistant des sauvegardes qu'il a lui-même téléversées (dans le stockage de Home Assistant,
`auto_backup.remote_backups`) et ne purge que celles-là — ou celles qui portent son marqueur
dans les métadonnées du fournisseur. Tout autre fichier présent dans le dossier distant est
ignoré par la purge, quels que soient son âge et son nom.

La purge distante se déclenche :

- **après chaque téléversement réussi**, si l'option **purge automatique** (`auto_purge`) est
  active dans les réglages des sauvegardes — c'est la même option qui commande la purge locale ;
  désactivée, aucune suppression distante automatique n'a lieu ;
- **à chaque appel du service `auto_backup.purge`**, qui purge d'abord les sauvegardes locales
  expirées, comme auparavant, puis chaque destination distante configurée.

Une destination en attente de ré-autorisation n'est pas contactée : elle est sautée, avec un
avertissement dans le journal, et les autres destinations sont purgées normalement. Un fichier
déjà disparu chez le fournisseur est traité comme purgé (avertissement, entrée retirée du
registre) et une suppression en échec n'interrompt jamais la purge des suivantes.

**Événement** : `auto_backup.remote_purge` est émis **après chaque série de suppressions qui a
supprimé au moins un fichier**, avec les champs `destination` (identifiant), `destination_name`
(nom lisible) et `remote_ids` (liste des identifiants distants supprimés). Aucun événement n'est
émis pour une destination où rien n'a été supprimé.

```yaml
automation:
  triggers:
    - trigger: event
      event_type: auto_backup.remote_purge
  actions:
    - action: persistent_notification.create
      data:
        message: >-
          {{ trigger.event.data.remote_ids | length }} sauvegarde(s) supprimée(s)
          de {{ trigger.event.data.destination_name }}.
```

### Notifications

Quand l'envoi d'une sauvegarde vers une destination distante échoue **définitivement** (après
les nouvelles tentatives), Auto Backup affiche une **notification persistante** en français qui
nomme la destination, la sauvegarde et la cause de l'échec.

- **Une notification par destination**, jamais une par sauvegarde : les échecs suivants mettent
  la même notification à jour et affichent le nombre d'échecs consécutifs.
- **Retrait automatique** : dès qu'un envoi vers cette destination aboutit, la notification
  disparaît.
- **Accès révoqué** : la notification invite à relancer « Ré-autoriser une destination » et
  accompagne le problème affiché dans l'interface des intégrations, sans le doubler.
- **Aucun secret affiché** : jetons, valeurs de `access_token` / `refresh_token` et chemins de
  fichiers absolus sont masqués (`***`) avant affichage.

L'entrée de menu **Réglages des notifications** permet de les désactiver (option
`notify_on_failure`, activée par défaut). Désactivées, l'événement
`auto_backup.upload_failed`, le journal d'erreur et le problème signalant une destination à
ré-autoriser restent émis : seules les notifications persistantes s'arrêtent.
### Entités d'état des destinations

Aux capteurs upstream, qui décrivent les sauvegardes **locales**, s'ajoutent **trois entités par
destination configurée**. Elles sont rattachées au même appareil « Auto Backup » que les entités
d'origine et portent le nom de leur destination, ce qui permet de suivre plusieurs destinations
côte à côte dans un tableau de bord.

| Entité | Type | Ce qu'elle montre | Attributs |
| --- | --- | --- | --- |
| « *Destination* : dernier téléversement réussi » | `sensor`, horodatage | date et heure du dernier envoi réussi vers cette destination | — |
| « *Destination* : sauvegardes distantes » | `sensor`, mesure | nombre de sauvegardes présentes chez le fournisseur | — |
| « *Destination* : problème de téléversement » | `binary_sensor`, `problem` | actif tant qu'aucun envoi n'a réussi depuis le dernier échec | `last_error`, `last_failed_slug`, `last_failed_at` |

Un téléversement réussi horodate le capteur de succès, met à jour le compte et éteint le
capteur de problème ; un échec l'allume et renseigne `last_error` avec un message lisible, dont
les jetons, les secrets et les adresses électroniques sont masqués avant tout affichage. Le capteur de problème se prête
directement à une automatisation — l'identifiant d'entité exact est construit à partir du nom de
la destination, relevez-le dans les outils de développement :

```yaml
trigger:
  - platform: state
    entity_id: binary_sensor.auto_backup_dropbox_perso_probleme_de_televersement
    to: "on"
    for: "01:00:00"
action:
  - service: notify.persistent_notification
    data:
      message: >-
        Sauvegarde distante en échec :
        {{ state_attr(trigger.entity_id, 'last_error') }}
```

L'identifiant unique d'une entité vaut `<entrée>_<destination>_<type>` : ajouter une destination
crée ses entités sans redémarrage, en supprimer une retire les siennes. Le dernier succès, le
nombre de sauvegardes distantes et la dernière erreur sont retrouvés après un redémarrage de
Home Assistant — et une erreur résolue juste avant le redémarrage ne réapparaît pas.

Le nombre de sauvegardes distantes est lu dans l'inventaire tenu par la rétention distante
(issue #9), qui connaît les sauvegardes réellement déposées chez le fournisseur. Tant que cette
fonction n'est pas livrée, le capteur suit les téléversements réussis et les purges annoncées
par l'événement `auto_backup.remote_purge`.

## Développement

Le projet utilise [`uv`](https://docs.astral.sh/uv/) et Python 3.14 (version épinglée dans
`.python-version`). Les dépendances, la configuration de `pytest` et celle de `ruff` sont
déclarées dans `pyproject.toml` ; `uv.lock` fige les versions installées.

| Action | Commande |
| --- | --- |
| Installer | `uv sync --group dev` |
| Tests unitaires | `uv run pytest` |
| Tests e2e | aucun |
| Lint | `uv run ruff check . && uv run ruff format --check .` |
| Build | aucun |

La version de Home Assistant utilisée pour les tests est celle qu'épingle
`pytest-homeassistant-custom-component` : les tests s'exécutent aujourd'hui contre
**Home Assistant 2026.9.0** (via `pytest-homeassistant-custom-component` 0.13.363, versions
figées dans `uv.lock`). C'est ce qui fixe le plancher de développement à Python 3.14.2,
exigé par Home Assistant >= 2026.3.

La version minimale de Home Assistant annoncée aux utilisateurs de l'intégration est
**2026.3.0**, déclarée dans `hacs.json` : les destinations distantes importent des exceptions
OAuth2 apparues dans cette version. Home Assistant 2026.3 exigeant lui aussi Python 3.14.2, le
plancher de développement et celui des utilisateurs coïncident aujourd'hui. Ils restent deux
planchers distincts : le dépôt suit la dernière version de Home Assistant, alors que la version
annoncée ne bouge que sur décision explicite.

Le répertoire `custom_components/auto_backup/` est exclu du reformatage `ruff` pour rester
identique à l'upstream (voir [`docs/UPSTREAM.md`](docs/UPSTREAM.md)).

`uv run pytest` démarre l'intégration dans une instance Home Assistant de test et affiche la
couverture de `custom_components/auto_backup`. Les fixtures disponibles, la structure des
tests et les tests réseau (`--tests-reseau`) sont décrits dans
[`docs/tests.md`](docs/tests.md).

### Intégration continue

Chaque pull request vers `main` déclenche le workflow [`ci.yml`](.github/workflows/ci.yml) :
lint (`ruff`), tests (`pytest`) et validation Home Assistant (`hassfest`) / HACS. Les jobs,
les versions d'actions épinglées et la façon de rejouer la CI en local sont décrits dans
[`docs/ci.md`](docs/ci.md). Les tests marqués `network` n'y sont pas exécutés.

## Travailler avec l'équipe d'agents

Ce projet est piloté par des issues GitHub traitées par des agents Claude Code (voir `docs/guide-agents.md` et `CLAUDE.md`).

1. `claude-perso` puis `/decoupe <objectif>` : l'agent métier propose des issues, tu valides, elles sont créées (label `ready`).
2. `/traite-issue <num>` : chaîne codeur → sécurité + tests → validation métier → PR. Tu merges toi-même.
3. En parallèle : `./scripts/agents-run.sh` traite jusqu'à 2 issues `ready` dans des worktrees séparés.
4. Après merge : `./scripts/agents-cleanup.sh <num>` nettoie le worktree et réactive les issues débloquées.

Les labels (`ready`, `in-progress`, `review`, `needs-human`, `epic`) sont la machine à états ; la branche `main` n'accepte que des PR.
