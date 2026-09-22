# Projet auto-backup-extension

## Contexte métier
Reprendre le projet https://github.com/jcwillox/hass-auto-backup et rajouter la possibilité de sauvegarder directement sur dropbox et google drive

## Stack
- Langage : Python 3.14 (`.python-version`), gestionnaire de projet et d'environnement : `uv`
- Backend : intégration Home Assistant `custom_components/auto_backup` (HACS, domaine `auto_backup`)
- Frontend : aucun (l'UI est fournie par Home Assistant : config flow et entités)
- BDD : aucune (l'état est persisté via le `Store` de Home Assistant)
- Tests : `pytest` + `pytest-asyncio` + `pytest-homeassistant-custom-component`
- Lint et format : `ruff`

## Commandes
- Installer : `uv sync --group dev`
- Tests unitaires : `uv run pytest`
- Tests e2e : aucun (pas de suite e2e sur ce projet)
- Lint : `uv run ruff check . && uv run ruff format --check .`
- Build : aucun (l'intégration est chargée telle quelle depuis `custom_components/`)

## Conventions
- Branches : `issue-<num>-<slug>`
- Commits : Conventional Commits, message en français, avec `Refs #<num>`
- Pas de secret dans le code : variables d'environnement uniquement
- Tout texte destiné aux utilisateurs ou aux issues est en français

## Workflow agents
- Une issue = une branche = un worktree = une PR
- Ne jamais pousser sur main, ne jamais merger
- Critères d'acceptation de l'issue = définition de « fini »
- Doute sur le besoin : commenter l'issue et passer le label `needs-human`
- Labels : `ready` → `in-progress` → `review` (PR ouverte) ; `needs-human` si bloqué ; `epic` pour l'objectif parent

## Équipe d'agents (`.claude/agents/`)
- `metier` : découpe un objectif en issues, valide une implémentation (ne code pas)
- `codeur` : implémente une issue dans le worktree courant
- `securite` : audite le diff de la branche (ne modifie rien)
- `testeur` : écrit et exécute les tests fonctionnels des critères d'acceptation
- `doc` : met à jour README, docs/, CHANGELOG

## Commandes slash
- `/decoupe <objectif>` : découpage en issues, création après validation humaine
- `/traite-issue <num>` : chaîne complète codeur → revues → validation métier → PR

Guide complet : `docs/guide-agents.md`.
