# Projet {{PROJECT_NAME}}

## Contexte métier
{{PROJECT_CONTEXT}}

## Stack
- Backend : {{STACK_BACKEND}}
- Frontend : {{STACK_FRONTEND}}
- BDD : {{STACK_DB}}

## Commandes
- Installer : `{{CMD_INSTALL}}`
- Tests unitaires : `{{CMD_TEST}}`
- Tests e2e : `{{CMD_TEST_E2E}}`
- Lint : `{{CMD_LINT}}`
- Build : `{{CMD_BUILD}}`

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
