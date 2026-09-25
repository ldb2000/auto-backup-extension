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

**Actuellement disponible : connexion des comptes cloud, mécanique de téléversement, et envoi
réel des sauvegardes vers Dropbox.** L'option `upload_to` des services de sauvegarde envoie la
sauvegarde créée vers les destinations configurées (voir « Téléversement des sauvegardes »
ci-dessous) ; la rétention distante arrive dans une version suivante.

- **Dropbox** : la **connexion du compte et le dépôt des sauvegardes sont disponibles** — voir le
  guide [Connecter un compte Dropbox](docs/destinations/dropbox.md) ; la purge distante arrive
  avec l'issue #12.
- **Google Drive** : la **connexion du compte est disponible** — voir le guide
  [Connecter Google Drive](docs/destinations/google-drive.md) ; le téléversement et la purge
  distante arrivent avec les issues #14 et #15.

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

Ce plancher ne concerne que l'environnement de développement et de test. La version minimale
de Home Assistant annoncée aux utilisateurs de l'intégration reste **2025.1.0**, déclarée dans
`hacs.json`.

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
