# Spec : template-agents — template de projet piloté par une équipe d'agents Claude Code

Date : 2026-09-19
Statut : validé (design approuvé en brainstorming)

## 1. Objectif

Fournir un dépôt modèle qui, une fois copié et paramétré par un script, donne un projet prêt à
fonctionner avec le workflow décrit dans `docs/guide-agents.md` : un agent métier découpe un
objectif en issues GitHub, un orchestrateur (`/traite-issue`) fait passer chaque issue par un
codeur, des relecteurs (sécurité, tests), une validation métier et une PR. GitHub est la source de
vérité (labels = machine à états).

Le template est **agnostique de la stack** : les commandes (install, test, lint, build) et le
contexte métier sont injectés au paramétrage.

## 2. Deux chemins d'entrée

**Chemin principal (template repository GitHub) :**

```bash
gh repo create mon-projet --template ldb2000/template-agents --private --clone
cd mon-projet && ./setup.sh
```

**Chemin de secours (copie du dossier) :**

```bash
cp -r template-agents mon-projet && cd mon-projet && ./setup.sh
```

`setup.sh` détecte le cas : si un dépôt git avec un remote `origin` existe, il saute `git init`
et `gh repo create`. Sinon il les fait. Tout le reste est identique.

Le dépôt `template-agents` lui-même est publié sur GitHub avec le flag *template repository*
(`gh repo edit --template`). Ce n'est pas fait par `setup.sh` mais documenté dans le README.

## 3. Arborescence du template

```
template-agents/
├── README.md                        # mode d'emploi du template (copie + setup) ; remplacé au setup
├── setup.sh                         # paramétrage (supprimé à la fin du setup)
├── setup.env.example                # fichier de réponses commenté (supprimé au setup)
├── CLAUDE.md                        # placeholders {{...}}
├── .gitignore                       # .agents-logs/, .claude/settings.local.json, .env, .env.*, setup.env
├── .claude/
│   ├── settings.json                # permissions allow/deny + hook PreToolUse
│   ├── agents/                      # metier.md codeur.md securite.md testeur.md doc.md
│   ├── skills/                      # decoupe/SKILL.md traite-issue/SKILL.md (format actuel des commandes slash)
│   └── hooks/guard-branch.sh        # bloque git commit/push hors branche issue-*
├── .github/
│   ├── ISSUE_TEMPLATE/agent-task.md
│   ├── workflows/template-ci.yml    # CI du template seul : lance tests/ (supprimé au setup)
│   └── workflows/claude-agents.yml  # squelette, workflow_dispatch uniquement
├── scripts/
│   ├── agents-run.sh                # traitement parallèle des issues ready (worktrees + headless)
│   └── agents-cleanup.sh            # nettoyage worktree/branche après merge + réactivation des issues débloquées
├── docs/
│   ├── guide-agents.md              # le guide de référence (copié tel quel)
│   └── superpowers/specs/           # cette spec (supprimée au setup, propre au template)
└── tests/
    ├── test-setup.sh                # test bout en bout de setup.sh --no-github
    └── test-guard-branch.sh         # test du hook
```

## 4. Placeholders

Syntaxe : `{{NOM}}`. Présents dans : `CLAUDE.md`, `.claude/settings.json`, `.claude/agents/*.md`,
`.claude/skills/*/SKILL.md`, `scripts/*.sh`
(le README final est un README projet minimal généré par le script, pas par substitution). `settings.json` ne contient aucun placeholder : il est enrichi par `jq`. Jamais dans `setup.sh`, `docs/`, `tests/`, `.github/`.

| Variable | Défaut | Usage |
| --- | --- | --- |
| `PROJECT_NAME` | nom du repo GitHub ou du dossier | CLAUDE.md, README, workflow |
| `PROJECT_CONTEXT` | (obligatoire, 2-3 phrases) | CLAUDE.md « Contexte métier », agent métier |
| `STACK_BACKEND` | `à définir` | CLAUDE.md |
| `STACK_FRONTEND` | `à définir` | CLAUDE.md |
| `STACK_DB` | `à définir` | CLAUDE.md |
| `CMD_INSTALL` | `npm ci` | CLAUDE.md, agents-run.sh |
| `CMD_TEST` | `npm test` | CLAUDE.md, codeur, testeur, permissions |
| `CMD_TEST_E2E` | `npm run test:e2e` | CLAUDE.md, testeur, permissions |
| `CMD_LINT` | `npm run lint` | CLAUDE.md, codeur, permissions |
| `CMD_BUILD` | `npm run build` | CLAUDE.md, permissions |
| `CLAUDE_BIN` | (obligatoire, ex. `claude-perso`) | agents-run.sh |
| `MAX_PARALLEL` | `2` | agents-run.sh |
| `GITHUB_VISIBILITY` | `private` | gh repo create (chemin copie seulement) |

Les permissions `allow` de `settings.json` sont **dérivées** des commandes : pour chaque
`CMD_*`, le script ajoute `Bash(<commande sans arguments finaux>:*)` via `jq` (ex. `npm test`
→ `Bash(npm test:*)`, `pytest -q` → `Bash(pytest:*)`). Le JSON reste valide et dédoublonné.

## 5. Flux de `setup.sh`

Options : `--yes` (accepte tous les défauts, échoue si une variable obligatoire manque),
`--no-github` (aucun appel réseau : pas de `gh repo create`, labels, protection, push),
`--env <fichier>` (défaut `./setup.env`), `--help`.

1. **Prérequis** : `git`, `jq`, `gh` présents ; `gh auth status` OK sauf `--no-github`. Message
   d'erreur explicite et sortie 1 sinon.
2. **Collecte** : charge `setup.env` s'il existe (format `CLE=valeur`, sourcé après validation
   des clés). Pour chaque variable absente, pose la question avec le défaut affiché (`read -r`).
   Avec `--yes`, prend le défaut sans question.
3. **Récapitulatif** et confirmation (sautée avec `--yes`).
4. **Substitution** : pour chaque fichier concerné, `sed` avec délimiteur sûr et échappement des
   valeurs (`&`, `|`, `\`, retours à la ligne interdits sauf `PROJECT_CONTEXT` qui est injecté
   ligne par ligne). `settings.json` : substitution puis enrichissement `allow` via `jq`.
5. **Vérification** : `grep -rn '{{[A-Z_]*}}'` sur les fichiers cibles ; si résultat non vide,
   affiche la liste, sortie 1, **avant** tout appel GitHub.
6. **README** : remplace le README du template par un README projet minimal (nom, contexte,
   commandes, lien vers `docs/guide-agents.md`).
7. **Git** :
   - si pas de `.git` : `git init -b main`, `git add -A`, commit `chore: initialisation depuis template-agents`.
   - si `.git` sans `origin` et pas `--no-github` : `gh repo create "$PROJECT_NAME" --$GITHUB_VISIBILITY --source . --push`.
   - si `origin` existe : rien.
8. **GitHub** (sauf `--no-github`) :
   - labels `ready`, `in-progress`, `review`, `needs-human`, `epic` via `gh label create --force`.
   - protection de `main` via `gh api -X PUT repos/{owner}/{repo}/branches/main/protection`
     (PR obligatoire avec 0 review minimum, `allow_force_pushes: false`, `allow_deletions: false`,
     `enforce_admins: false`). En cas d'échec (plan Free + repo privé), avertissement, pas d'arrêt.
9. **Nettoyage** : `git rm` de `setup.sh`, `setup.env.example`, `tests/`, `docs/superpowers/`, `.github/workflows/template-ci.yml` ; suppression de
   `setup.env` s'il existe (non suivi) ; commit `chore: paramétrage du projet <nom>` ; `git push -u origin main`
   sauf `--no-github`.
10. **Récapitulatif final** : ce qui a été fait, avertissements, et les trois prochaines
    commandes : `$CLAUDE_BIN` puis `/decoupe <objectif>` puis `/traite-issue <num>`.

Le script est `set -euo pipefail`, bash 3.2 compatible (macOS), pas de `mapfile`/tableaux
associatifs. Toute sortie utilisateur en français.

## 6. Contenu des fichiers agents

Les cinq agents, les deux commandes, le template d'issue, le hook et `agents-run.sh` reprennent
le guide (`docs/guide-agents.md`) avec ces ajustements :

- Les commandes de test/lint sont injectées (`{{CMD_TEST}}`, `{{CMD_LINT}}`…) au lieu d'être
  citées en dur.
- `agents-run.sh` : `CLAUDE_BIN="${CLAUDE_BIN:-{{CLAUDE_BIN}}}"`, `MAX_PARALLEL` idem, exécute
  `{{CMD_INSTALL}}` dans chaque worktree avant la session, refuse de démarrer si un worktree
  `issue-<num>` existe déjà, fetch `origin main`.
- `agents-cleanup.sh <num>` : `git worktree remove`, `git branch -d`, puis pour chaque issue
  ouverte dont le corps contient `Bloquée par : #<num>` et dont toutes les dépendances sont
  fermées, remplace un éventuel label bloquant par `ready` et commente. Idempotent.
- `guard-branch.sh` : lit `tool_input.command` sur stdin, bloque (`exit 2`) tout
  `git commit`/`git push` si la branche courante ne commence pas par `issue-`. Pas de blocage
  si la commande n'est pas git.
- Les frontmatters (champs `tools`, `model`), la syntaxe des commandes slash, les flags headless
  et le format des hooks sont alignés sur la doc Claude Code au moment de l'implémentation
  (vérification en cours par un agent dédié ; les écarts éventuels sont corrigés dans les
  fichiers, pas dans cette spec).
- `claude-agents.yml` : `on: workflow_dispatch` avec input `issue`, un job qui utilise
  `anthropics/claude-code-action` et un secret `ANTHROPIC_API_KEY`, entièrement commenté au
  niveau du job avec instructions d'activation. Le setup ne touche pas aux secrets.

## 7. Gestion des erreurs

- Toute étape réseau échouée après la substitution laisse le dépôt local cohérent : les fichiers
  sont déjà paramétrés et commités ; le message final indique la commande à relancer à la main.
- Placeholder restant = arrêt avant GitHub (état : fichiers partiellement substitués, non
  commités ; l'utilisateur relance après correction ou fait `git checkout .`).
- `gh` non authentifié : arrêt immédiat avant toute modification.
- `setup.env` contenant une clé inconnue : avertissement, clé ignorée.

## 8. Tests

`tests/test-setup.sh` (bash pur, sans dépendance) :

1. Copie le template dans un répertoire temporaire, écrit un `setup.env` complet avec des
   valeurs distinctives (ex. `CMD_TEST="pytest -q"`), lance `./setup.sh --yes --no-github`.
2. Vérifie : code de sortie 0 ; aucun `{{` dans les fichiers cibles ; `jq . .claude/settings.json`
   passe ; `allow` contient `Bash(pytest:*)` ; `CLAUDE.md` contient le nom et le contexte ;
   `scripts/agents-run.sh` contient le `CLAUDE_BIN` fourni ; `setup.sh`, `setup.env.example`,
   `tests/` absents ; `.claude/hooks/guard-branch.sh` exécutable ; dépôt git avec au moins un
   commit sur `main`.
3. Cas d'erreur : `setup.env` sans `PROJECT_CONTEXT` + `--yes` → sortie non nulle, rien commité.

`tests/test-guard-branch.sh` : crée un dépôt temporaire, simule le JSON stdin du hook, vérifie
`exit 2` sur `main` pour `git commit -m x`, `exit 0` sur `issue-1-test`, `exit 0` pour une
commande non git sur `main`.

Les tests sont lancés à la main (`./tests/test-setup.sh`) et dans le template via une GitHub
Action `template-ci.yml` **du template uniquement** (supprimée au setup, car elle n'a pas de sens
dans le projet cible).

## 9. Hors périmètre

- Presets de stack (Node, Python) : les défauts sont Node, le reste passe par les questions.
- Gestion des secrets GitHub Actions.
- Agent teams expérimentales.
- Mise à jour d'un projet déjà paramétré depuis une nouvelle version du template.

## 10. Écarts constatés avec le guide (doc Claude Code, septembre 2026)

- Commandes slash : `.claude/skills/<nom>/SKILL.md` avec `disable-model-invocation: true`
  remplace `.claude/commands/*.md`. `$ARGUMENTS` reste valide.
- Permission push : `Bash(git push -u origin issue-*)` (le suffixe `:*` insère un espace et ne
  matcherait pas `issue-42-slug`).
- Ordre du setup : un seul commit après nettoyage, push, puis labels et protection de `main`
  (la protection arrive en dernier pour ne pas bloquer le push initial).
- Le hook `guard-branch.sh` peut être désactivé localement par `git config agents.guardBranch false`
  (non commité) : nécessaire pour maintenir le template lui-même, qui travaille sur `main`.
- Les valeurs de `setup.env` sont mono-ligne (y compris `PROJECT_CONTEXT`).
