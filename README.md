# auto-backup-extension

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
capteurs d'état), ce fork vise l'envoi automatique des sauvegardes vers le cloud :

- **Dropbox** : téléversement de la sauvegarde créée et rétention distante dédiée.
- **Google Drive** : téléversement de la sauvegarde créée et rétention distante dédiée.

La configuration des destinations se fait depuis l'interface de Home Assistant, avec les
identifiants d'application OAuth de l'utilisateur : aucun secret n'est stocké dans ce dépôt.

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

## Travailler avec l'équipe d'agents

Ce projet est piloté par des issues GitHub traitées par des agents Claude Code (voir `docs/guide-agents.md` et `CLAUDE.md`).

1. `claude-perso` puis `/decoupe <objectif>` : l'agent métier propose des issues, tu valides, elles sont créées (label `ready`).
2. `/traite-issue <num>` : chaîne codeur → sécurité + tests → validation métier → PR. Tu merges toi-même.
3. En parallèle : `./scripts/agents-run.sh` traite jusqu'à 2 issues `ready` dans des worktrees séparés.
4. Après merge : `./scripts/agents-cleanup.sh <num>` nettoie le worktree et réactive les issues débloquées.

Les labels (`ready`, `in-progress`, `review`, `needs-human`, `epic`) sont la machine à états ; la branche `main` n'accepte que des PR.
