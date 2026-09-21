# template-agents

Template de projet pour développer avec une équipe d'agents Claude Code pilotée par les issues GitHub : un agent métier découpe un objectif en issues, un orchestrateur (`/traite-issue`) fait passer chaque issue par un codeur, des revues sécurité et tests, une validation métier, jusqu'à une PR que tu merges toi-même. Guide complet : [`docs/guide-agents.md`](docs/guide-agents.md).

## Créer un projet

Prérequis : `gh` authentifié (`gh auth status`), `jq`, `git`, Claude Code installé.

```bash
# 1. Nouveau repo depuis le template
gh repo create mon-projet --template ldb2000/template-agents --private --clone
cd mon-projet

# 2. Paramétrage : questions interactives…
./setup.sh
#    …ou fichier de réponses : cp setup.env.example setup.env, complète-le, puis
./setup.sh --yes
```

`setup.sh` remplace les placeholders (nom, contexte métier, stack, commandes), génère le README, commite, pousse, crée les labels GitHub et protège `main`, puis se supprime.

À savoir :
- `CLAUDE_BIN` doit être un exécutable présent dans le PATH (ex. `claude`) : un alias shell comme `claude-perso` ne fonctionne pas dans `scripts/agents-run.sh`. Crée un petit wrapper exécutable si besoin.
- La protection de `main` n'est pas disponible sur un repo **privé** en plan GitHub Free : le script affiche un avertissement et continue. Active-la à la main (Settings → Branches) ou passe le repo en public.

Sans GitHub (essai local) : `./setup.sh --no-github`. Sans passer par le template GitHub : `cp -R template-agents mon-projet && rm -rf mon-projet/.git && cd mon-projet && ./setup.sh` (le repo GitHub est alors créé par le script). Sans `rm -rf .git`, le dossier garde le remote du template et setup.sh refusera de continuer.

## Ce que contient le projet créé

| Élément | Rôle |
| --- | --- |
| `CLAUDE.md` | Contexte commun à tous les agents |
| `.claude/settings.json` | Permissions allow/deny + hook anti-commit sur main |
| `.claude/agents/` | `metier`, `codeur`, `securite`, `testeur`, `doc` |
| `.claude/skills/` | `/decoupe <objectif>`, `/traite-issue <num>` |
| `.claude/hooks/guard-branch.sh` | Refuse `git commit`/`push` hors branche `issue-*` |
| `.github/ISSUE_TEMPLATE/agent-task.md` | Structure d'issue (critères Given-When-Then) |
| `.github/workflows/claude-agents.yml` | Exécution sur runner GitHub (désactivée par défaut) |
| `scripts/agents-run.sh` | Plusieurs issues `ready` en parallèle (worktrees + sessions headless) |
| `scripts/agents-cleanup.sh <num>` | Nettoyage après merge, réactivation des issues débloquées |

## Maintenir le template

```bash
./tests/lint-template.sh      # cohérence statique
./tests/test-guard-branch.sh  # hook
./tests/test-setup.sh         # setup.sh bout en bout (--no-github)
git config agents.guardBranch false   # une fois : le template travaille sur main
gh repo edit ldb2000/template-agents --template   # une fois : rend le repo utilisable avec --template
```

Après chaque projet créé, reporte ici les ajustements de prompts qui ont fait leurs preuves.
