# auto-backup-extension

 Objectif reprendre le projet https://github.com/jcwillox/hass-auto-backup et rajouter un backup dans dropbox et google drive. 

## Développement

| Action | Commande |
| --- | --- |
| Installer | `—` |
| Tests unitaires | `—` |
| Tests e2e | `—` |
| Lint | `—` |
| Build | `—` |

## Travailler avec l'équipe d'agents

Ce projet est piloté par des issues GitHub traitées par des agents Claude Code (voir `docs/guide-agents.md` et `CLAUDE.md`).

1. `claude-perso` puis `/decoupe <objectif>` : l'agent métier propose des issues, tu valides, elles sont créées (label `ready`).
2. `/traite-issue <num>` : chaîne codeur → sécurité + tests → validation métier → PR. Tu merges toi-même.
3. En parallèle : `./scripts/agents-run.sh` traite jusqu'à 2 issues `ready` dans des worktrees séparés.
4. Après merge : `./scripts/agents-cleanup.sh <num>` nettoie le worktree et réactive les issues débloquées.

Les labels (`ready`, `in-progress`, `review`, `needs-human`, `epic`) sont la machine à états ; la branche `main` n'accepte que des PR.
