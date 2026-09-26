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

**Actuellement disponible : connexion des comptes cloud, mécanique de téléversement, dépôt
effectif sur Google Drive et rétention distante.** L'option `upload_to` des services de sauvegarde
envoie la sauvegarde créée vers les destinations configurées (voir « Téléversement des
sauvegardes » ci-dessous), et chaque destination applique sa propre rétention aux sauvegardes
qu'elle a reçues (voir « Rétention distante »).

- **Dropbox** : la **connexion du compte est disponible** — voir le guide
  [Connecter un compte Dropbox](docs/destinations/dropbox.md) ; l'envoi effectif du fichier et sa
  suppression chez Dropbox arrivent avec les issues #11 et #12.
- **Google Drive** : la **connexion du compte et le téléversement sont disponibles** — voir le
  guide [Connecter Google Drive](docs/destinations/google-drive.md) ; le listage et la suppression
  chez Google arrivent avec l'issue #15. Tant qu'ils manquent, la rétention distante ne peut pas
  s'appliquer à une destination Google Drive : elle est configurable, mais aucune sauvegarde n'y
  est encore supprimée.

La configuration des destinations se fait depuis l'interface de Home Assistant, avec les
identifiants d'application OAuth de l'utilisateur : aucun secret n'est stocké dans ce dépôt.

Les options de l'intégration s'ouvrent sur un menu : **Ajouter une destination**,
**Ré-autoriser une destination**, **Supprimer une destination**, **Réglages du téléversement**
(délai maximum accordé à l'envoi d'une sauvegarde, 1800 secondes par défaut), et les **réglages
des sauvegardes** d'origine. L'ajout d'une destination cloud demande l'identifiant et le secret
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
destination et invitant à la ré-autoriser ; les autres destinations continuent de fonctionner.

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

### Rétention distante

Chaque destination a **sa propre rétention**, réglée à son ajout : une durée de conservation en
jours, un nombre maximum de sauvegardes, ou les deux. Les deux se combinent : les sauvegardes
trop anciennes partent d'abord, puis, s'il en reste plus que le nombre autorisé, les plus
anciennes du lot restant sont supprimées jusqu'à revenir sous la limite. Une destination sans
aucune rétention n'est jamais purgée.

> **Aucun fournisseur ne sait encore supprimer.** La mécanique décrite ci-dessous est en place,
> mais supprimer suppose de lister d'abord, et aucun fournisseur livré ne le fait : la purge d'une
> destination Google Drive s'arrête au listage avec un message de journal explicite (issue #15),
> et Dropbox ne téléverse pas encore (issues #11 et #12). La rétention que vous réglez aujourd'hui
> est enregistrée et s'appliquera sans rien reconfigurer.

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
