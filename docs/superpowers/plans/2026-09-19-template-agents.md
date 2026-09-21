# Template-agents — plan d'implémentation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Un dépôt modèle GitHub (`template-agents`) qui, après `./setup.sh`, donne un projet prêt pour le workflow « équipe d'agents Claude Code pilotée par les issues GitHub ».

**Architecture:** Fichiers statiques avec placeholders `{{VAR}}` (CLAUDE.md, agents, skills, scripts) + `setup.sh` en bash qui collecte les valeurs (questions ou `setup.env`), substitue, enrichit `settings.json` via jq, initialise git/GitHub (repo, labels, protection de main) puis se supprime. Tests en bash pur dans `tests/`, exécutés par une CI propre au template.

**Tech Stack:** bash 3.2 (macOS), sed BSD/GNU, jq, git, gh CLI, GitHub Actions. Aucune autre dépendance.

**Spec:** `docs/superpowers/specs/2026-09-19-template-agents-design.md` (lis-la avant de commencer, ainsi que `docs/guide-agents.md` qui est le guide de référence dont les agents et skills sont tirés).

## Global Constraints

- Tout le texte destiné à l'utilisateur (messages des scripts, prompts d'agents, README, commentaires) est en **français**.
- Scripts : `#!/usr/bin/env bash`, `set -euo pipefail`, compatibles **bash 3.2** : pas de `mapfile`, pas de tableaux associatifs, pas de `${var,,}`, pas de `local -n`.
- `sed -i` n'est jamais utilisé (incompatible BSD/GNU) : écrire dans un fichier temporaire puis `cat tmp > fichier` (préserve le mode).
- Placeholders : syntaxe exacte `{{NOM_EN_MAJUSCULES}}` (majuscules, chiffres, underscore ; regex `\{\{[A-Z0-9_]+\}\}`). Liste fermée : `PROJECT_NAME PROJECT_CONTEXT STACK_BACKEND STACK_FRONTEND STACK_DB CMD_INSTALL CMD_TEST CMD_TEST_E2E CMD_LINT CMD_BUILD CLAUDE_BIN MAX_PARALLEL GITHUB_VISIBILITY`.
- Fichiers contenant des placeholders : `CLAUDE.md`, `.claude/agents/*.md`, `.claude/skills/*/SKILL.md`, `scripts/*.sh`. **Jamais** dans `setup.sh`, `.claude/settings.json`, `.github/`, `docs/` (hors `docs/superpowers/`, qui documente les placeholders et est supprimé au setup), `tests/`.
- Labels GitHub : `ready` 0E8A16, `in-progress` FBCA04, `review` 1D76DB, `needs-human` D93F0B, `epic` 5319E7.
- Branches d'issue : `issue-<num>-<slug>`. Commits : Conventional Commits en français, terminés par `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- Le dépôt du template travaille sur `main`. Avant le premier commit qui suit la création de `.claude/settings.json` (Task 3), exécuter une fois `git config agents.guardBranch false` dans le dépôt du template (Task 1 l'installe).
- Tests : `tests/*.sh` retournent 0 en succès, non-zéro sinon, et affichent `✓ <libellé>` par vérification.

---

### Task 1 : hook `guard-branch.sh` et `.gitignore`

**Files:**
- Create: `.gitignore`
- Create: `.claude/hooks/guard-branch.sh` (exécutable)
- Test: `tests/test-guard-branch.sh` (exécutable)

**Interfaces:**
- Produces: `.claude/hooks/guard-branch.sh` lit sur stdin le JSON du hook PreToolUse (`{"tool_name":"Bash","tool_input":{"command":"..."}}`), sort `2` pour bloquer, `0` sinon. Opt-out : `git config agents.guardBranch false`. Task 3 le référence dans `settings.json`.

- [ ] **Step 1 : écrire le test qui échoue**

```bash
cat > tests/test-guard-branch.sh <<'EOF'
#!/usr/bin/env bash
# Vérifie que le hook bloque git commit/push hors branche issue-* et laisse passer le reste.
set -uo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
HOOK="$HERE/.claude/hooks/guard-branch.sh"
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
fail() { echo "ÉCHEC : $*" >&2; exit 1; }
ok() { echo "  ✓ $*"; }

# hook_rc <commande bash simulée> : joue le hook dans $TMP, renvoie son code de sortie
hook_rc() {
  ( cd "$TMP" && printf '{"tool_name":"Bash","tool_input":{"command":"%s"}}' "$1" | "$HOOK" 2>/dev/null )
  echo $?
}

git -C "$TMP" init -q
git -C "$TMP" checkout -q -b main
git -C "$TMP" -c user.email=t@t -c user.name=t commit -q --allow-empty -m init

[ -x "$HOOK" ] || fail "hook absent ou non exécutable : $HOOK"
ok "hook exécutable"

[ "$(hook_rc 'git commit -m x')" = "2" ] || fail "git commit sur main devrait être bloqué (2)"
ok "git commit bloqué sur main"
[ "$(hook_rc 'git push -u origin main')" = "2" ] || fail "git push sur main devrait être bloqué (2)"
ok "git push bloqué sur main"
[ "$(hook_rc 'git add -A && git commit -m x')" = "2" ] || fail "git commit enchaîné après && devrait être bloqué"
ok "git commit après && bloqué sur main"
[ "$(hook_rc 'npm test')" = "0" ] || fail "commande non git devrait passer (0)"
ok "commande non git autorisée"
[ "$(hook_rc 'git status')" = "0" ] || fail "git status devrait passer (0)"
ok "git status autorisé"

git -C "$TMP" checkout -q -b issue-1-test
[ "$(hook_rc 'git commit -m x')" = "0" ] || fail "git commit sur issue-1-test devrait passer (0)"
ok "git commit autorisé sur issue-1-test"

git -C "$TMP" checkout -q main
git -C "$TMP" config agents.guardBranch false
[ "$(hook_rc 'git commit -m x')" = "0" ] || fail "opt-out agents.guardBranch=false devrait autoriser"
ok "opt-out par git config respecté"

echo "test-guard-branch : OK"
EOF
chmod +x tests/test-guard-branch.sh
```

- [ ] **Step 2 : lancer le test, vérifier qu'il échoue**

Run: `./tests/test-guard-branch.sh`
Expected: `ÉCHEC : hook absent ou non exécutable`

- [ ] **Step 3 : écrire le hook et le `.gitignore`**

```bash
mkdir -p .claude/hooks
cat > .claude/hooks/guard-branch.sh <<'EOF'
#!/usr/bin/env bash
# Hook PreToolUse (matcher Bash) : refuse git commit / git push hors d'une branche issue-*.
# Reçoit l'appel d'outil en JSON sur stdin. Code de sortie 2 = action bloquée, 0 = laisser faire.
# Désactivation locale (maintenance du template) : git config agents.guardBranch false
set -uo pipefail

INPUT=$(cat)
CMD=$(printf '%s' "$INPUT" | jq -r '.tool_input.command // ""' 2>/dev/null || echo "")

# Ne concerne que git commit / git push, y compris enchaînés (&&, ;, |).
if ! printf '%s' "$CMD" | grep -qE '(^|[;&|]) *git +(commit|push)( |$)'; then
  exit 0
fi

# Hors dépôt git : rien à protéger.
BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null) || exit 0

if [ "$(git config --get agents.guardBranch 2>/dev/null || echo true)" = "false" ]; then
  exit 0
fi

case "$BRANCH" in
  issue-*) exit 0 ;;
  *)
    echo "Commit/push refusé : la branche '$BRANCH' n'est pas une branche d'issue (issue-<num>-<slug>)." >&2
    exit 2
    ;;
esac
EOF
chmod +x .claude/hooks/guard-branch.sh

cat > .gitignore <<'EOF'
# Journaux des sessions headless
.agents-logs/
# Préférences locales Claude Code (non partagées)
.claude/settings.local.json
# Secrets
.env
.env.*
secrets/
# Réponses du setup (peuvent contenir du contexte privé)
setup.env
# Fichiers temporaires de setup.sh
*.tmp.*
EOF
```

- [ ] **Step 4 : lancer le test, vérifier qu'il passe**

Run: `./tests/test-guard-branch.sh`
Expected: 8 lignes `✓` puis `test-guard-branch : OK`

- [ ] **Step 5 : désactiver le hook pour le dépôt du template et commiter**

```bash
git config agents.guardBranch false
git add .gitignore .claude/hooks/guard-branch.sh tests/test-guard-branch.sh
git commit -m "feat: hook guard-branch et gitignore

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2 : lint du template (`tests/lint-template.sh`)

**Files:**
- Create: `tests/lint-template.sh` (exécutable)

**Interfaces:**
- Produces: un script qui vérifie la cohérence statique du template. Les tâches suivantes doivent le garder vert. Il échoue tant que `.claude/settings.json` n'existe pas : c'est attendu jusqu'à Task 3 (on le commite quand même, en rouge, avec un message explicite).

- [ ] **Step 1 : écrire le lint**

```bash
cat > tests/lint-template.sh <<'EOF'
#!/usr/bin/env bash
# Cohérence statique du template : syntaxe bash, JSON valide, frontmatters, placeholders connus.
set -uo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
cd "$HERE"
fail() { echo "ÉCHEC : $*" >&2; exit 1; }
ok() { echo "  ✓ $*"; }

VARS="PROJECT_NAME PROJECT_CONTEXT STACK_BACKEND STACK_FRONTEND STACK_DB CMD_INSTALL CMD_TEST CMD_TEST_E2E CMD_LINT CMD_BUILD CLAUDE_BIN MAX_PARALLEL GITHUB_VISIBILITY"

# 1. Syntaxe bash de tous les scripts
for s in setup.sh scripts/*.sh .claude/hooks/*.sh tests/*.sh; do
  [ -f "$s" ] || continue
  bash -n "$s" || fail "erreur de syntaxe : $s"
  [ -x "$s" ] || fail "non exécutable : $s"
done
ok "scripts : syntaxe et bit exécutable"

# 2. settings.json valide, sans placeholder, avec le hook
[ -f .claude/settings.json ] || fail ".claude/settings.json absent"
jq . .claude/settings.json >/dev/null || fail ".claude/settings.json invalide"
grep -q '{{' .claude/settings.json && fail "placeholder interdit dans settings.json"
jq -e '.hooks.PreToolUse[0].hooks[0].command | test("guard-branch.sh")' .claude/settings.json >/dev/null \
  || fail "settings.json ne référence pas guard-branch.sh"
jq -e '.permissions.deny | index("Bash(gh pr merge:*)")' .claude/settings.json >/dev/null \
  || fail "settings.json : deny gh pr merge manquant"
ok "settings.json valide"

# 3. Frontmatter des agents : name = nom de fichier, description présente
for a in .claude/agents/*.md; do
  [ -f "$a" ] || fail "aucun agent dans .claude/agents/"
  base=$(basename "$a" .md)
  head -1 "$a" | grep -q '^---$' || fail "$a : frontmatter absent"
  grep -q "^name: $base\$" "$a" || fail "$a : 'name: $base' attendu"
  grep -q '^description: .' "$a" || fail "$a : description absente"
done
ok "agents : frontmatter"

# 4. Skills : SKILL.md avec name et description
for s in .claude/skills/*/SKILL.md; do
  [ -f "$s" ] || fail "aucun skill dans .claude/skills/"
  dir=$(basename "$(dirname "$s")")
  grep -q "^name: $dir\$" "$s" || fail "$s : 'name: $dir' attendu"
  grep -q '^description: .' "$s" || fail "$s : description absente"
done
ok "skills : frontmatter"

# 5. Placeholders : uniquement des noms connus, uniquement dans les fichiers autorisés
found=$(grep -rhoE '\{\{[A-Z0-9_]+\}\}' CLAUDE.md .claude/agents .claude/skills scripts 2>/dev/null | sort -u | tr -d '{}')
for v in $found; do
  case " $VARS " in *" $v "*) ;; *) fail "placeholder inconnu : {{$v}}" ;; esac
done
stray=$(grep -rlE '\{\{[A-Z0-9_]+\}\}' .claude/settings.json .claude/hooks .github tests setup.sh $(find docs -type f -not -path "docs/superpowers/*") 2>/dev/null || true)
[ -z "$stray" ] || fail "placeholders interdits dans : $stray"
ok "placeholders : $(echo "$found" | wc -w | tr -d ' ') noms, tous connus"

echo "lint-template : OK"
EOF
chmod +x tests/lint-template.sh
```

- [ ] **Step 2 : lancer, constater l'échec attendu**

Run: `./tests/lint-template.sh`
Expected: `ÉCHEC : .claude/settings.json absent` (les scripts existants passent l'étape 1).

- [ ] **Step 3 : commiter**

```bash
git add tests/lint-template.sh
git commit -m "test: lint statique du template

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3 : `CLAUDE.md`, `.claude/settings.json`, agents, skills, template d'issue

**Files:**
- Create: `CLAUDE.md`
- Create: `.claude/settings.json`
- Create: `.claude/agents/metier.md`, `codeur.md`, `securite.md`, `testeur.md`, `doc.md`
- Create: `.claude/skills/decoupe/SKILL.md`, `.claude/skills/traite-issue/SKILL.md`
- Create: `.github/ISSUE_TEMPLATE/agent-task.md`
- Test: `tests/lint-template.sh` (existant)

**Interfaces:**
- Consumes: `.claude/hooks/guard-branch.sh` (Task 1).
- Produces: les placeholders `{{PROJECT_NAME}} {{PROJECT_CONTEXT}} {{STACK_*}} {{CMD_*}}` que `setup.sh` (Task 5) substitue ; `settings.json` dont `setup.sh` enrichit `.permissions.allow`.

- [ ] **Step 1 : `CLAUDE.md`**

```bash
cat > CLAUDE.md <<'EOF'
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
EOF
```

- [ ] **Step 2 : `.claude/settings.json`**

```bash
cat > .claude/settings.json <<'EOF'
{
  "permissions": {
    "allow": [
      "Bash(gh issue:*)",
      "Bash(gh pr create:*)",
      "Bash(gh pr view:*)",
      "Bash(gh pr list:*)",
      "Bash(gh label:*)",
      "Bash(gh repo view:*)",
      "Bash(git status:*)",
      "Bash(git diff:*)",
      "Bash(git log:*)",
      "Bash(git show:*)",
      "Bash(git branch:*)",
      "Bash(git add:*)",
      "Bash(git commit:*)",
      "Bash(git checkout:*)",
      "Bash(git switch:*)",
      "Bash(git fetch:*)",
      "Bash(git worktree:*)",
      "Bash(git push -u origin issue-*)",
      "Bash(git push origin issue-*)"
    ],
    "deny": [
      "Bash(git push --force:*)",
      "Bash(git push -f:*)",
      "Bash(git push origin main:*)",
      "Bash(git push -u origin main:*)",
      "Bash(gh pr merge:*)",
      "Bash(gh repo delete:*)",
      "Bash(rm -rf:*)",
      "Read(./.env)",
      "Read(./.env.*)",
      "Read(./secrets/**)"
    ]
  },
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "$CLAUDE_PROJECT_DIR/.claude/hooks/guard-branch.sh"
          }
        ]
      }
    ]
  }
}
EOF
jq . .claude/settings.json >/dev/null && echo "JSON OK"
```

- [ ] **Step 3 : les cinq agents**

```bash
mkdir -p .claude/agents
cat > .claude/agents/metier.md <<'EOF'
---
name: metier
description: Expert métier avec la vue globale du projet. À utiliser pour découper un objectif en issues GitHub, et pour valider qu'une implémentation répond au besoin avant l'ouverture d'une PR. Ne modifie jamais le code.
tools: Read, Grep, Glob, Bash
model: opus
---
Tu es le responsable métier du projet {{PROJECT_NAME}}. Tu as la vue d'ensemble : objectifs, utilisateurs, cohérence fonctionnelle. Lis `CLAUDE.md` en premier : la section « Contexte métier » est ta référence.

MODE DÉCOUPAGE (quand on te donne un objectif) :
- Découpe en issues indépendantes, livrables en moins d'une journée de dev chacune.
- Chaque issue suit le template `.github/ISSUE_TEMPLATE/agent-task.md` : Contexte / Besoin / Critères d'acceptation (Given-When-Then) / Hors périmètre / Dépendances.
- Indique l'ordre et les dépendances entre issues (« Bloquée par : #<num> »).
- Ne crée rien toi-même : renvoie la liste pour validation humaine.

MODE VALIDATION (quand on te donne une issue, son epic et un diff) :
- Relis l'epic pour garder la vue globale.
- Vérifie chaque critère d'acceptation un par un, en t'appuyant sur le diff et les rapports sécurité et tests fournis.
- Vérifie la cohérence avec l'objectif global et les autres fonctionnalités.
- Tu ne modifies jamais le code. Tu peux lire le dépôt et interroger GitHub avec `gh issue view`.

Format de sortie obligatoire en validation :
VERDICT: OK | KO | NEEDS_HUMAN
CRITERES: liste « ✓/✗ critère »
REMARQUES: actions précises pour le codeur si KO ; question précise pour l'humain si NEEDS_HUMAN
EOF

cat > .claude/agents/codeur.md <<'EOF'
---
name: codeur
description: Développeur qui implémente une issue GitHub dans le worktree courant, sur sa branche issue-<num>-<slug>. À utiliser pour tout travail d'implémentation ou de correction suite à une revue.
model: opus
---
Tu implémentes UNE issue, dans le worktree courant, sur sa branche.

- Lis l'issue (`gh issue view <num>`) et `CLAUDE.md` avant de coder.
- Reste strictement dans le périmètre de l'issue ; note le reste en « hors périmètre ».
- Écris ou mets à jour les tests unitaires de ce que tu codes.
- Avant de rendre la main, lance le lint (`{{CMD_LINT}}`) et les tests unitaires (`{{CMD_TEST}}`) ; ils doivent passer.
- Commits atomiques en Conventional Commits, en français, avec « Refs #<num> ».
- Si tu reçois des remarques de revue (sécurité, tests, métier), traite-les toutes et liste ce que tu as changé.
- Tu ne pousses jamais sur main et tu ne merges jamais.
- Si l'issue est ambiguë au point de bloquer, ne devine pas : STATUT BLOQUÉ avec la question précise.

Format de sortie obligatoire :
STATUT: DONE | BLOQUÉ
FICHIERS: liste des fichiers créés ou modifiés
TESTS: commande lancée et résultat
NOTES: points d'attention pour la revue, hors périmètre constaté
EOF

cat > .claude/agents/securite.md <<'EOF'
---
name: securite
description: Auditeur sécurité en lecture seule. À utiliser systématiquement après une implémentation, avant PR, sur le diff de la branche courante.
tools: Read, Grep, Glob, Bash
model: sonnet
---
Tu audites uniquement le diff de la branche courante (`git fetch -q origin main && git diff origin/main...HEAD`). Tu ne modifies jamais de fichier.

Cherche en priorité : secrets en dur, injections (SQL, commande, template, chemin), authentification ou autorisation manquante, validation d'entrées, données personnelles loguées ou exposées, dépendances ajoutées et leur réputation, configuration permissive (CORS, headers, permissions de fichiers), désérialisation non sûre.

Gravités : BLOQUANT (exploitable ou fuite de secret), MAJEUR (à corriger avant PR), MINEUR (à noter).

Format de sortie obligatoire :
VERDICT: OK | A_CORRIGER | BLOQUANT
CONSTATS: une ligne par constat « fichier:ligne — gravité — problème — correction suggérée », ou « aucun »
EOF

cat > .claude/agents/testeur.md <<'EOF'
---
name: testeur
description: Testeur fonctionnel. Écrit et exécute les tests fonctionnels ou e2e qui prouvent les critères d'acceptation d'une issue. N'écrit que dans les dossiers de tests.
tools: Read, Write, Edit, Grep, Glob, Bash
model: sonnet
---
Tu prouves que les critères d'acceptation de l'issue sont remplis.

- Un test fonctionnel (ou e2e) par critère Given-When-Then de l'issue.
- Tu n'écris que dans les dossiers de tests ; tu ne corriges jamais le code applicatif. Si un test échoue à cause du code, tu le signales au codeur.
- Exécute toute la suite : tes tests + l'existant (non-régression). Commandes : `{{CMD_TEST}}` puis `{{CMD_TEST_E2E}}` si des tests e2e existent.
- Commite tes tests sur la branche courante (Conventional Commits, « test: ... Refs #<num> »).

Format de sortie obligatoire :
VERDICT: OK | KO
COUVERTURE: une ligne par critère « critère → fichier de test::nom → ✓/✗ »
ÉCHECS: détail et hypothèse de cause pour le codeur, ou « aucun »
EOF

cat > .claude/agents/doc.md <<'EOF'
---
name: doc
description: Rédacteur technique. Met à jour la documentation impactée par une implémentation validée (README, docs/, CHANGELOG, docstrings). N'écrit que dans la doc et les commentaires.
tools: Read, Write, Edit, Grep, Glob
model: haiku
---
Tu mets à jour la documentation impactée par le diff de la branche : README, `docs/`, CHANGELOG, docstrings des fonctions publiques.

- N'écris que dans les fichiers de doc et les commentaires ; jamais dans le code.
- Pas de doc sur ce qui n'a pas changé.
- Ajoute une entrée CHANGELOG sous « Unreleased » (crée `CHANGELOG.md` s'il n'existe pas), en français, avec « Refs #<num> ».

Format de sortie obligatoire :
FICHIERS_MODIFIÉS: liste
RÉSUMÉ: une phrase
EOF
```

- [ ] **Step 4 : les deux skills**

```bash
mkdir -p .claude/skills/decoupe .claude/skills/traite-issue
cat > .claude/skills/decoupe/SKILL.md <<'EOF'
---
name: decoupe
description: Découpe un objectif en issues GitHub via l'agent métier, puis crée l'epic et les issues après validation humaine. Usage - /decoupe <objectif en une phrase ou chemin vers un brief>
disable-model-invocation: true
argument-hint: <objectif en une phrase ou chemin vers un brief>
---
Objectif : $ARGUMENTS

1. Lis `CLAUDE.md` et explore rapidement le code pour comprendre l'existant. Si `$ARGUMENTS` est un chemin de fichier, lis-le : c'est le brief.
2. Délègue à l'agent `metier` en MODE DÉCOUPAGE, en lui donnant l'objectif et ce que tu as compris de l'existant.
3. Affiche-moi la liste proposée (titre, critères d'acceptation, dépendances, ordre conseillé) et ATTENDS ma validation. Intègre mes corrections et représente la liste si besoin.
4. Après validation explicite :
   - crée une issue epic avec l'objectif complet (label `epic`) : `gh issue create --label epic --title "..." --body "..."` ;
   - crée chaque issue en suivant exactement la structure de `.github/ISSUE_TEMPLATE/agent-task.md`, avec le numéro de l'epic dans « Contexte » et « Bloquée par : #<num> » ou « aucune » dans « Dépendances » ;
   - mets le label `ready` uniquement sur les issues sans dépendance ouverte.
5. Résume : numéros créés, lesquelles sont `ready`, ordre conseillé.
EOF

cat > .claude/skills/traite-issue/SKILL.md <<'EOF'
---
name: traite-issue
description: Fait passer une issue GitHub dans toute la chaîne d'agents (codeur, sécurité, testeur, métier, doc) jusqu'à une PR ouverte. Usage - /traite-issue <numéro d'issue>
disable-model-invocation: true
argument-hint: <numéro d'issue>
---
Issue à traiter : #$ARGUMENTS

## 1. Prise en charge
- `gh issue view $ARGUMENTS --json number,title,body,labels` : lis l'issue, puis son epic (numéro dans « Contexte »).
- Vérifie la présence du label `ready`. Sinon arrête-toi et dis pourquoi (déjà prise, bloquée, ou non validée).
- Remplace `ready` par `in-progress` : `gh issue edit $ARGUMENTS --remove-label ready --add-label in-progress`.
- Vérifie la branche courante (`git rev-parse --abbrev-ref HEAD`). Si elle ne commence pas par `issue-$ARGUMENTS-`, crée `issue-$ARGUMENTS-<slug>` depuis `origin/main` (`git fetch origin main && git checkout -b issue-$ARGUMENTS-<slug> origin/main`). Le slug : titre en minuscules, ASCII, tirets, 40 caractères max.

## 2. Implémentation
- Délègue à l'agent `codeur` avec le numéro d'issue. Si STATUT = BLOQUÉ, passe directement à l'étape 5 avec NEEDS_HUMAN.

## 3. Revue parallèle
- Lance EN PARALLÈLE (un seul message, deux appels) les agents `securite` et `testeur` sur la branche, en donnant à chacun le numéro d'issue.

## 4. Validation métier
- Donne à l'agent `metier` (MODE VALIDATION) : l'issue, l'epic, `git diff origin/main...HEAD --stat` et le diff complet, les rapports sécurité et tests.

## 5. Boucle de correction
- Si sécurité = BLOQUANT ou A_CORRIGER, tests = KO, ou métier = KO : renvoie TOUTES les remarques au `codeur` en un seul message, puis refais les étapes 3 et 4.
- Maximum 3 itérations au total. Au-delà, ou si métier = NEEDS_HUMAN, ou si codeur = BLOQUÉ : commente l'issue avec le blocage précis et les verdicts (`gh issue comment`), remplace `in-progress` par `needs-human`, pousse la branche telle quelle, et arrête-toi.

## 6. Finalisation
- Délègue à l'agent `doc`, puis commite son travail toi-même : `git add -A && git commit -m "docs: mise à jour de la documentation. Refs #$ARGUMENTS"` (rien à commiter = pas de commit).
- Pousse la branche : `git push -u origin issue-$ARGUMENTS-<slug>`.
- Ouvre la PR avec `gh pr create --base main --title "<type>: <titre> (#$ARGUMENTS)" --body-file <fichier>` : corps avec « Closes #$ARGUMENTS », puis une section par agent (verdict + points clés), puis la liste des commits.
- Remplace `in-progress` par `review`.
- Ne merge jamais. Termine par : numéro de PR, URL, verdicts.
EOF
```

- [ ] **Step 5 : template d'issue**

```bash
mkdir -p .github/ISSUE_TEMPLATE
cat > .github/ISSUE_TEMPLATE/agent-task.md <<'EOF'
---
name: Tâche agent
about: Issue destinée à l'équipe d'agents Claude Code
labels: ''
---
## Contexte
Pourquoi cette issue existe. Epic : #<num>

## Besoin
Ce que l'utilisateur doit pouvoir faire.

## Critères d'acceptation
- [ ] GIVEN ... WHEN ... THEN ...
- [ ] GIVEN ... WHEN ... THEN ...

## Hors périmètre
- ...

## Dépendances
Bloquée par : #<num> (ou aucune)
EOF
```

- [ ] **Step 6 : lancer le lint**

Run: `./tests/lint-template.sh`
Expected: `✓ scripts`, `✓ settings.json valide`, `✓ agents`, `✓ skills`, `✓ placeholders : 10 noms, tous connus`, `lint-template : OK`. (10 = tous sauf CLAUDE_BIN et MAX_PARALLEL, qui arrivent en Task 4, et GITHUB_VISIBILITY qui ne sert qu au setup.)

- [ ] **Step 7 : commiter**

```bash
git add CLAUDE.md .claude .github
git commit -m "feat: CLAUDE.md, permissions, agents, skills et template d'issue

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4 : `scripts/agents-run.sh` et `scripts/agents-cleanup.sh`

**Files:**
- Create: `scripts/agents-run.sh` (exécutable)
- Create: `scripts/agents-cleanup.sh` (exécutable)
- Test: `tests/lint-template.sh` (syntaxe) + vérification manuelle des options `--help`

**Interfaces:**
- Consumes: skill `traite-issue` (Task 3), labels GitHub.
- Produces: placeholders `{{CLAUDE_BIN}} {{MAX_PARALLEL}} {{CMD_INSTALL}}` substitués par Task 5.

- [ ] **Step 1 : `agents-run.sh`**

```bash
mkdir -p scripts
cat > scripts/agents-run.sh <<'EOF'
#!/usr/bin/env bash
# Traite les issues « ready » en parallèle : un worktree git et une session Claude Code headless par issue.
# Usage : ./scripts/agents-run.sh [--dry-run]
# Variables (surchargent les valeurs du setup) : CLAUDE_BIN, MAX_PARALLEL, MAX_TURNS, MAX_BUDGET_USD
set -euo pipefail

CLAUDE_BIN="${CLAUDE_BIN:-{{CLAUDE_BIN}}}"
MAX_PARALLEL="${MAX_PARALLEL:-{{MAX_PARALLEL}}}"
MAX_TURNS="${MAX_TURNS:-150}"
MAX_BUDGET_USD="${MAX_BUDGET_USD:-}"
CMD_INSTALL='{{CMD_INSTALL}}'
DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

REPO_ROOT=$(git rev-parse --show-toplevel)
WT_DIR="$(cd "$REPO_ROOT/.." && pwd)/$(basename "$REPO_ROOT")-worktrees"
LOG_DIR="$REPO_ROOT/.agents-logs"
mkdir -p "$WT_DIR" "$LOG_DIR"

command -v "$CLAUDE_BIN" >/dev/null || { echo "Binaire Claude Code introuvable : $CLAUDE_BIN" >&2; exit 1; }
command -v gh >/dev/null || { echo "gh (GitHub CLI) manquant" >&2; exit 1; }

git -C "$REPO_ROOT" fetch -q origin main

ISSUES=$(gh issue list --label ready --state open --limit "$MAX_PARALLEL" \
  --json number,title --jq '.[] | "\(.number)|\(.title)"')

[ -z "$ISSUES" ] && { echo "Aucune issue ready."; exit 0; }

STARTED=0
while IFS='|' read -r NUM TITLE; do
  [ -z "$NUM" ] && continue
  SLUG=$(printf '%s' "$TITLE" | iconv -f UTF-8 -t ASCII//TRANSLIT 2>/dev/null | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9' '-' | cut -c1-40 | sed 's/-$//; s/^-//')
  BRANCH="issue-$NUM-$SLUG"
  WT="$WT_DIR/issue-$NUM"

  if [ -d "$WT" ]; then
    echo "⏭  #$NUM : worktree déjà présent ($WT), issue ignorée. Nettoie avec scripts/agents-cleanup.sh $NUM"
    continue
  fi

  echo "→ #$NUM « $TITLE » : branche $BRANCH, worktree $WT"
  if [ "$DRY_RUN" -eq 1 ]; then continue; fi

  if git -C "$REPO_ROOT" show-ref --verify --quiet "refs/heads/$BRANCH"; then
    git -C "$REPO_ROOT" worktree add "$WT" "$BRANCH"
  else
    git -C "$REPO_ROOT" worktree add -b "$BRANCH" "$WT" origin/main
  fi

  (
    cd "$WT"
    if [ -n "$CMD_INSTALL" ]; then
      echo "   #$NUM : installation ($CMD_INSTALL)"
      sh -c "$CMD_INSTALL" > "$LOG_DIR/issue-$NUM.install.log" 2>&1 || echo "   #$NUM : installation en échec, voir $LOG_DIR/issue-$NUM.install.log"
    fi
    BUDGET_ARGS=""
    [ -n "$MAX_BUDGET_USD" ] && BUDGET_ARGS="--max-budget-usd $MAX_BUDGET_USD"
    # shellcheck disable=SC2086
    "$CLAUDE_BIN" -p "/traite-issue $NUM" \
      --permission-mode acceptEdits \
      --max-turns "$MAX_TURNS" \
      $BUDGET_ARGS \
      --output-format json > "$LOG_DIR/issue-$NUM.json" 2> "$LOG_DIR/issue-$NUM.err"
    echo "✓ #$NUM terminée (voir $LOG_DIR/issue-$NUM.json)"
  ) &
  STARTED=$((STARTED + 1))
done <<EOF_ISSUES
$ISSUES
EOF_ISSUES

if [ "$DRY_RUN" -eq 1 ]; then echo "Mode --dry-run : rien lancé."; exit 0; fi
echo "$STARTED session(s) lancée(s), attente de la fin…"
wait
echo "Toutes les sessions sont terminées. Journaux dans $LOG_DIR/."
EOF
chmod +x scripts/agents-run.sh
```

- [ ] **Step 2 : `agents-cleanup.sh`**

```bash
cat > scripts/agents-cleanup.sh <<'EOF'
#!/usr/bin/env bash
# Après merge de la PR d'une issue : supprime son worktree et sa branche locale,
# puis repasse en « ready » les issues ouvertes qu'elle bloquait et dont toutes les dépendances sont fermées.
# Usage : ./scripts/agents-cleanup.sh <numéro d'issue> [--force]
set -euo pipefail

NUM="${1:-}"
FORCE="${2:-}"
[ -n "$NUM" ] || { echo "Usage : $0 <numéro d'issue> [--force]" >&2; exit 1; }
case "$NUM" in ''|*[!0-9]*) echo "Numéro d'issue invalide : $NUM" >&2; exit 1 ;; esac

REPO_ROOT=$(git rev-parse --show-toplevel)
WT_DIR="$(cd "$REPO_ROOT/.." && pwd)/$(basename "$REPO_ROOT")-worktrees"
WT="$WT_DIR/issue-$NUM"

cd "$REPO_ROOT"
git fetch -q --prune origin

# 1. Worktree
if [ -d "$WT" ]; then
  if [ "$FORCE" = "--force" ]; then git worktree remove --force "$WT"; else
    git worktree remove "$WT" || { echo "Worktree non propre : relance avec --force pour l'écraser." >&2; exit 1; }
  fi
  echo "✓ worktree supprimé : $WT"
else
  echo "· pas de worktree pour #$NUM"
fi
git worktree prune

# 2. Branche locale
BRANCH=$(git branch --list "issue-$NUM-*" | sed 's/^[* ]*//' | head -1)
if [ -n "$BRANCH" ]; then
  git branch -d "$BRANCH" 2>/dev/null || git branch -D "$BRANCH"
  echo "✓ branche supprimée : $BRANCH"
else
  echo "· pas de branche locale issue-$NUM-*"
fi

# 3. Réactivation des issues débloquées
command -v gh >/dev/null || { echo "gh absent : réactivation des issues sautée." >&2; exit 0; }
CANDIDATES=$(gh issue list --state open --search "\"Bloquée par\" \"#$NUM\"" --limit 50 --json number --jq '.[].number')
for C in $CANDIDATES; do
  BODY=$(gh issue view "$C" --json body --jq .body)
  DEPS=$(printf '%s' "$BODY" | sed -n '/^## Dépendances/,$p' | grep -oE '#[0-9]+' | tr -d '#' | sort -u)
  printf '%s\n' "$DEPS" | grep -qx "$NUM" || continue
  ALL_CLOSED=1
  for D in $DEPS; do
    STATE=$(gh issue view "$D" --json state --jq .state 2>/dev/null || echo "UNKNOWN")
    [ "$STATE" = "CLOSED" ] || { ALL_CLOSED=0; break; }
  done
  [ "$ALL_CLOSED" -eq 1 ] || { echo "· #$C reste bloquée (autres dépendances ouvertes)"; continue; }
  LABELS=$(gh issue view "$C" --json labels --jq '.labels[].name' | tr '\n' ' ')
  case " $LABELS " in
    *" ready "*|*" in-progress "*|*" review "*) echo "· #$C déjà en cours ($LABELS)"; continue ;;
  esac
  gh issue edit "$C" --add-label ready --remove-label needs-human >/dev/null 2>&1 || gh issue edit "$C" --add-label ready >/dev/null
  gh issue comment "$C" --body "Débloquée : toutes ses dépendances (dont #$NUM) sont fermées. Label \`ready\` posé automatiquement." >/dev/null
  echo "✓ #$C repassée en ready"
done
echo "Nettoyage de #$NUM terminé."
EOF
chmod +x scripts/agents-cleanup.sh
```

- [ ] **Step 3 : vérifier syntaxe et lint**

Run: `bash -n scripts/agents-run.sh && bash -n scripts/agents-cleanup.sh && ./tests/lint-template.sh`
Expected: `lint-template : OK` avec `12 noms, tous connus`.

Run: `./scripts/agents-cleanup.sh`
Expected: `Usage : ./scripts/agents-cleanup.sh <numéro d'issue> [--force]`, code 1.

- [ ] **Step 4 : commiter**

```bash
git add scripts
git commit -m "feat: scripts agents-run (parallèle) et agents-cleanup

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5 : `setup.sh` — collecte, substitution, vérification, README (mode `--no-github`)

**Files:**
- Create: `setup.sh` (exécutable)
- Create: `setup.env.example`
- Test: `tests/test-setup.sh` (exécutable)

**Interfaces:**
- Consumes: tous les fichiers à placeholders (Tasks 3, 4), `.claude/settings.json`.
- Produces: `setup.sh --yes --no-github --env <fichier>` ; fonctions internes `cmd_prefix`, `substitute_file`, `enrich_settings`, `write_readme`, `main`. Task 6 ajoute `git_and_github` et `cleanup`.

- [ ] **Step 1 : écrire le test (partie locale)**

```bash
cat > tests/test-setup.sh <<'EOF'
#!/usr/bin/env bash
# Test bout en bout de setup.sh en mode --no-github, sur une copie du template.
set -uo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
fail() { echo "ÉCHEC : $*" >&2; [ -f "$TMP/out.log" ] && sed 's/^/    | /' "$TMP/out.log" >&2; exit 1; }
ok() { echo "  ✓ $*"; }

copy_template() { # copy_template <dest>
  mkdir -p "$1"
  ( cd "$HERE" && tar -cf - --exclude .git --exclude .agents-logs --exclude .superpowers . ) | ( cd "$1" && tar -xf - )
}

# ---------- Cas nominal (chemin « copie de dossier », pas de .git)
PROJ="$TMP/mon-projet"
copy_template "$PROJ"
cat > "$PROJ/setup.env" <<'ENV'
# commentaire ignoré
PROJECT_NAME=mon-projet
PROJECT_CONTEXT="Application de suivi de séances pour coachs sportifs & leurs clients."
STACK_BACKEND=FastAPI
CMD_INSTALL=uv sync
CMD_TEST=pytest -q
CMD_TEST_E2E=pytest -q tests/e2e
CMD_LINT=ruff check .
CMD_BUILD=
CLAUDE_BIN=claude-test
MAX_PARALLEL=3
CLE_INCONNUE=oui
ENV
( cd "$PROJ" && ./setup.sh --yes --no-github ) > "$TMP/out.log" 2>&1 || fail "setup.sh a échoué (code $?)"
ok "setup.sh --yes --no-github termine en 0"
grep -q "Clé inconnue ignorée : CLE_INCONNUE" "$TMP/out.log" || fail "clé inconnue non signalée"
ok "clé inconnue signalée"

cd "$PROJ"
! grep -rqE '\{\{[A-Z0-9_]+\}\}' CLAUDE.md .claude scripts README.md || fail "placeholders restants : $(grep -rlE '\{\{[A-Z0-9_]+\}\}' CLAUDE.md .claude scripts README.md)"
ok "aucun placeholder restant"
grep -q '^# Projet mon-projet$' CLAUDE.md || fail "PROJECT_NAME absent de CLAUDE.md"
grep -q 'coachs sportifs & leurs clients' CLAUDE.md || fail "PROJECT_CONTEXT (avec &) mal substitué"
grep -q 'Backend : FastAPI' CLAUDE.md || fail "STACK_BACKEND absent"
grep -q 'Frontend : à définir' CLAUDE.md || fail "défaut STACK_FRONTEND non appliqué"
grep -q 'Tests unitaires : `pytest -q`' CLAUDE.md || fail "CMD_TEST absent de CLAUDE.md"
ok "CLAUDE.md substitué (valeurs, défauts, caractère &)"
grep -q 'CLAUDE_BIN:-claude-test' scripts/agents-run.sh || fail "CLAUDE_BIN absent d'agents-run.sh"
grep -q 'MAX_PARALLEL:-3' scripts/agents-run.sh || fail "MAX_PARALLEL absent d'agents-run.sh"
grep -q "CMD_INSTALL='uv sync'" scripts/agents-run.sh || fail "CMD_INSTALL absent d'agents-run.sh"
ok "agents-run.sh paramétré"
grep -q 'pytest -q' .claude/agents/testeur.md || fail "CMD_TEST absent de testeur.md"
ok "agents paramétrés"

jq . .claude/settings.json >/dev/null || fail "settings.json invalide"
for p in 'Bash(uv sync:*)' 'Bash(pytest:*)' 'Bash(ruff check .:*)' 'Bash(gh pr create:*)'; do
  jq -e --arg p "$p" '.permissions.allow | index($p)' .claude/settings.json >/dev/null || fail "permission manquante : $p"
done
jq -e '.permissions.allow | index("Bash(:*)")' .claude/settings.json >/dev/null && fail "permission vide ajoutée pour CMD_BUILD vide"
[ "$(jq '.permissions.allow | length' .claude/settings.json)" = "$(jq '.permissions.allow | unique | length' .claude/settings.json)" ] || fail "doublons dans allow"
ok "settings.json enrichi (préfixes de commandes, pas de vide, pas de doublon)"

[ -f README.md ] && grep -q '^# mon-projet$' README.md && grep -q 'coachs sportifs' README.md || fail "README projet non généré"
! grep -q 'template-agents' README.md || fail "README encore celui du template"
ok "README projet généré"
[ -x .claude/hooks/guard-branch.sh ] || fail "hook non exécutable après setup"
[ -x scripts/agents-run.sh ] || fail "agents-run.sh non exécutable après setup"
ok "bits exécutables préservés"

# ---------- Suite du test (Task 6) : git, nettoyage, cas d'erreur
if grep -q 'git rm -q -r' "$HERE/setup.sh"; then   # implémentation de Task 6 présente
  [ -d .git ] || fail "dépôt git non initialisé"
  [ "$(git rev-parse --abbrev-ref HEAD)" = "main" ] || fail "branche courante ≠ main"
  [ "$(git rev-list --count HEAD)" -ge 1 ] || fail "aucun commit"
  [ -z "$(git status --porcelain)" ] || fail "arbre de travail non propre : $(git status --porcelain)"
  ok "dépôt git initialisé, 1 commit sur main, propre"
  for f in setup.sh setup.env.example setup.env tests docs/superpowers .github/workflows/template-ci.yml; do
    [ ! -e "$f" ] || fail "fichier de setup non supprimé : $f"
  done
  ok "fichiers de setup supprimés"
  [ -f docs/guide-agents.md ] || fail "docs/guide-agents.md supprimé à tort"
  ok "guide conservé"

  # Cas d'erreur : variable obligatoire manquante → aucun effet
  PROJ2="$TMP/projet-ko"
  copy_template "$PROJ2"
  printf 'PROJECT_NAME=ko\nCLAUDE_BIN=claude-test\n' > "$PROJ2/setup.env"
  if ( cd "$PROJ2" && ./setup.sh --yes --no-github ) > "$TMP/out2.log" 2>&1; then fail "setup.sh aurait dû échouer sans PROJECT_CONTEXT"; fi
  grep -q 'PROJECT_CONTEXT' "$TMP/out2.log" || fail "le message d'erreur ne nomme pas PROJECT_CONTEXT"
  [ ! -d "$PROJ2/.git" ] || fail "git init effectué malgré l'échec"
  grep -q '{{PROJECT_NAME}}' "$PROJ2/CLAUDE.md" || fail "substitution effectuée malgré l'échec"
  ok "variable obligatoire manquante : arrêt avant toute modification"

  # Cas d'erreur : visibilité invalide
  PROJ3="$TMP/projet-vis"
  copy_template "$PROJ3"
  printf 'PROJECT_CONTEXT=x\nCLAUDE_BIN=c\nGITHUB_VISIBILITY=interne\n' > "$PROJ3/setup.env"
  if ( cd "$PROJ3" && ./setup.sh --yes --no-github ) > "$TMP/out3.log" 2>&1; then fail "GITHUB_VISIBILITY invalide accepté"; fi
  ok "GITHUB_VISIBILITY invalide refusé"
fi

echo "test-setup : OK"
EOF
chmod +x tests/test-setup.sh
```

- [ ] **Step 2 : lancer, constater l'échec**

Run: `./tests/test-setup.sh`
Expected: `ÉCHEC : setup.sh a échoué` (fichier absent).

- [ ] **Step 3 : écrire `setup.env.example`**

```bash
cat > setup.env.example <<'EOF'
# Réponses pour ./setup.sh — copie ce fichier en setup.env, complète, puis lance ./setup.sh --yes
# Une valeur par ligne, sans retour à la ligne. Les guillemets englobants sont retirés.
# Les lignes absentes prennent la valeur par défaut (ou sont demandées en interactif).

# Nom du projet (défaut : nom du repo GitHub, sinon nom du dossier)
PROJECT_NAME=

# OBLIGATOIRE — 2 à 3 phrases : à quoi sert l'application, pour qui
PROJECT_CONTEXT=

# Stack (défaut : « à définir »)
STACK_BACKEND=
STACK_FRONTEND=
STACK_DB=

# Commandes (défauts Node : npm ci / npm test / npm run test:e2e / npm run lint / npm run build)
# Laisse vide une commande qui n'existe pas encore ; elle ne créera pas de permission.
CMD_INSTALL=
CMD_TEST=
CMD_TEST_E2E=
CMD_LINT=
CMD_BUILD=

# OBLIGATOIRE — binaire Claude Code utilisé par scripts/agents-run.sh (ex. claude, claude-pro, claude-perso)
CLAUDE_BIN=

# Nombre de sessions parallèles par défaut (défaut : 2)
MAX_PARALLEL=

# Visibilité du repo GitHub si setup.sh doit le créer : private | public (défaut : private)
GITHUB_VISIBILITY=
EOF
```

- [ ] **Step 4 : écrire `setup.sh` (partie locale)**

```bash
cat > setup.sh <<'EOF'
#!/usr/bin/env bash
# setup.sh — paramètre un projet créé depuis template-agents, puis se supprime.
# Usage : ./setup.sh [--yes] [--no-github] [--env <fichier>] [--help]
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

YES=0
NO_GITHUB=0
ENV_FILE="$ROOT/setup.env"

VARS="PROJECT_NAME PROJECT_CONTEXT STACK_BACKEND STACK_FRONTEND STACK_DB CMD_INSTALL CMD_TEST CMD_TEST_E2E CMD_LINT CMD_BUILD CLAUDE_BIN MAX_PARALLEL GITHUB_VISIBILITY"
REQUIRED="PROJECT_CONTEXT CLAUDE_BIN"
# Fichiers de setup supprimés à la fin
SETUP_FILES="setup.sh setup.env.example tests docs/superpowers .github/workflows/template-ci.yml"

# ---------- affichage
info() { printf '\033[1;34m→\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m✓\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m✗\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<USAGE
Usage : ./setup.sh [options]

Paramètre ce projet à partir du template : remplace les placeholders, génère le README,
initialise git et GitHub (repo, labels, protection de main), puis supprime les fichiers de setup.

Options :
  --yes, -y        accepte toutes les valeurs par défaut (échoue si une valeur obligatoire manque)
  --no-github      aucun appel réseau : ni création de repo, ni labels, ni protection, ni push
  --env <fichier>  fichier de réponses (défaut : ./setup.env ; modèle : setup.env.example)
  --help, -h       cette aide
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    --yes|-y) YES=1 ;;
    --no-github) NO_GITHUB=1 ;;
    --env) [ $# -ge 2 ] || die "--env attend un fichier"; ENV_FILE="$2"; shift ;;
    --help|-h) usage; exit 0 ;;
    *) usage >&2; die "Option inconnue : $1" ;;
  esac
  shift
done

# ---------- helpers
# getvar NOM → valeur de la variable (vide si non définie)
getvar() { eval "printf '%s' \"\${$1:-}\""; }
# setvar NOM VALEUR
setvar() { eval "$1=\"\$2\""; }

# target_files : fichiers contenant des placeholders
target_files() {
  find CLAUDE.md .claude/agents .claude/skills scripts -type f 2>/dev/null
}

# cmd_prefix "npm run lint" → "npm run lint" ; "pytest -q tests" → "pytest" (coupe au premier mot commençant par -)
cmd_prefix() {
  out=""
  for w in $1; do
    case "$w" in -*) break ;; esac
    out="${out:+$out }$w"
  done
  printf '%s' "$out"
}

# sed_escape VALEUR : échappe \ | & pour une substitution sed délimitée par |
sed_escape() { printf '%s' "$1" | sed -e 's/[\\|&]/\\&/g'; }

# substitute_file FICHIER : remplace tous les {{VAR}} (mode du fichier préservé)
substitute_file() {
  f="$1"; tmp="$f.tmp.$$"
  cp "$f" "$tmp"
  for v in $VARS; do
    esc=$(sed_escape "$(getvar "$v")")
    sed "s|{{$v}}|$esc|g" "$tmp" > "$tmp.2"
    mv "$tmp.2" "$tmp"
  done
  cat "$tmp" > "$f"
  rm -f "$tmp"
}

# enrich_settings : ajoute Bash(<préfixe>:*) pour chaque commande non vide, sans doublon
enrich_settings() {
  perms=""
  for c in "$CMD_INSTALL" "$CMD_TEST" "$CMD_TEST_E2E" "$CMD_LINT" "$CMD_BUILD"; do
    p=$(cmd_prefix "$c")
    [ -n "$p" ] && perms="$perms
Bash($p:*)"
  done
  jq --arg perms "$perms" \
    '.permissions.allow = ((.permissions.allow + ($perms | split("\n") | map(select(length > 0)))) | unique)' \
    .claude/settings.json > .claude/settings.json.tmp.$$
  mv .claude/settings.json.tmp.$$ .claude/settings.json
}

# write_readme : README minimal du projet (remplace celui du template)
write_readme() {
  cat > README.md <<README
# $PROJECT_NAME

$PROJECT_CONTEXT

## Développement

| Action | Commande |
| --- | --- |
| Installer | \`${CMD_INSTALL:-—}\` |
| Tests unitaires | \`${CMD_TEST:-—}\` |
| Tests e2e | \`${CMD_TEST_E2E:-—}\` |
| Lint | \`${CMD_LINT:-—}\` |
| Build | \`${CMD_BUILD:-—}\` |

## Travailler avec l'équipe d'agents

Ce projet est piloté par des issues GitHub traitées par des agents Claude Code (voir \`docs/guide-agents.md\` et \`CLAUDE.md\`).

1. \`$CLAUDE_BIN\` puis \`/decoupe <objectif>\` : l'agent métier propose des issues, tu valides, elles sont créées (label \`ready\`).
2. \`/traite-issue <num>\` : chaîne codeur → sécurité + tests → validation métier → PR. Tu merges toi-même.
3. En parallèle : \`./scripts/agents-run.sh\` traite jusqu'à $MAX_PARALLEL issues \`ready\` dans des worktrees séparés.
4. Après merge : \`./scripts/agents-cleanup.sh <num>\` nettoie le worktree et réactive les issues débloquées.

Les labels (\`ready\`, \`in-progress\`, \`review\`, \`needs-human\`, \`epic\`) sont la machine à états ; la branche \`main\` n'accepte que des PR.
README
}

# ---------- flux principal
main() {
  info "Paramétrage du projet depuis template-agents"

  # 1. Prérequis
  for t in git jq; do command -v "$t" >/dev/null || die "Outil manquant : $t"; done
  if [ "$NO_GITHUB" -eq 0 ]; then
    command -v gh >/dev/null || die "gh (GitHub CLI) manquant : https://cli.github.com"
    gh auth status >/dev/null 2>&1 || die "gh n'est pas authentifié : lance « gh auth login »"
  fi
  [ -f CLAUDE.md ] && [ -f .claude/settings.json ] || die "Ce dossier ne ressemble pas au template (CLAUDE.md ou .claude/settings.json absent)"

  # 2. Collecte : fichier de réponses
  if [ -f "$ENV_FILE" ]; then
    info "Lecture de $ENV_FILE"
    while IFS= read -r line || [ -n "$line" ]; do
      case "$line" in ''|'#'*) continue ;; esac
      case "$line" in *=*) ;; *) warn "Ligne ignorée (pas de =) : $line"; continue ;; esac
      key="${line%%=*}"; val="${line#*=}"
      val="${val%\"}"; val="${val#\"}"
      case " $VARS " in
        *" $key "*) setvar "$key" "$val" ;;
        *) warn "Clé inconnue ignorée : $key" ;;
      esac
    done < "$ENV_FILE"
  fi

  # 2b. Collecte : questions (ou défauts avec --yes)
  default_name=$(basename "$ROOT")
  if git rev-parse --show-toplevel >/dev/null 2>&1 && git remote get-url origin >/dev/null 2>&1; then
    default_name=$(basename -s .git "$(git remote get-url origin)")
  fi
  ask PROJECT_NAME "Nom du projet" "$default_name"
  ask PROJECT_CONTEXT "Contexte métier (2-3 phrases, obligatoire)" ""
  ask STACK_BACKEND "Backend" "à définir"
  ask STACK_FRONTEND "Frontend" "à définir"
  ask STACK_DB "Base de données" "à définir"
  ask CMD_INSTALL "Commande d'installation" "npm ci"
  ask CMD_TEST "Commande de tests unitaires" "npm test"
  ask CMD_TEST_E2E "Commande de tests e2e" "npm run test:e2e"
  ask CMD_LINT "Commande de lint" "npm run lint"
  ask CMD_BUILD "Commande de build" "npm run build"
  ask CLAUDE_BIN "Binaire Claude Code pour les scripts (ex. claude, claude-pro, claude-perso ; obligatoire)" ""
  ask MAX_PARALLEL "Sessions parallèles par défaut" "2"
  ask GITHUB_VISIBILITY "Visibilité GitHub si le repo doit être créé (private|public)" "private"

  # 3. Validation
  for v in $REQUIRED; do
    [ -n "$(getvar "$v")" ] || die "Variable obligatoire manquante : $v (renseigne-la dans setup.env ou réponds à la question)"
  done
  case "$GITHUB_VISIBILITY" in private|public) ;; *) die "GITHUB_VISIBILITY doit valoir private ou public (reçu : $GITHUB_VISIBILITY)" ;; esac
  case "$MAX_PARALLEL" in ''|*[!0-9]*) die "MAX_PARALLEL doit être un entier (reçu : $MAX_PARALLEL)" ;; esac
  for v in $VARS; do
    case "$(getvar "$v")" in *"
"*) die "$v ne doit pas contenir de retour à la ligne" ;; esac
  done

  echo
  info "Récapitulatif"
  for v in $VARS; do printf '  %-18s %s\n' "$v" "$(getvar "$v")"; done
  [ "$NO_GITHUB" -eq 1 ] && printf '  %-18s %s\n' "(mode)" "--no-github : aucun appel réseau"
  echo
  if [ "$YES" -eq 0 ]; then
    printf 'Continuer ? [O/n] '
    read -r rep </dev/tty || rep=""
    case "$rep" in n|N|non|NON) die "Abandon." ;; esac
  fi

  # 4. Substitution
  info "Substitution des placeholders"
  for f in $(target_files); do substitute_file "$f"; done
  enrich_settings
  ok "fichiers paramétrés"

  # 5. Vérification
  remaining=$(grep -rnE '\{\{[A-Z0-9_]+\}\}' $(target_files) || true)
  if [ -n "$remaining" ]; then
    echo "$remaining" >&2
    die "Des placeholders subsistent (voir ci-dessus). Corrige puis relance, ou restaure avec « git checkout . »."
  fi
  jq . .claude/settings.json >/dev/null || die ".claude/settings.json invalide après enrichissement"
  ok "aucun placeholder restant, settings.json valide"

  # 6. README
  write_readme
  ok "README.md généré"

  # 7-9. Git, GitHub, nettoyage (Task 6)
  git_and_github
  cleanup

  # 10. Récapitulatif final
  echo
  ok "Projet « $PROJECT_NAME » prêt."
  echo "Prochaines étapes :"
  echo "  1. $CLAUDE_BIN                 # ouvre Claude Code dans ce dossier"
  echo "  2. /decoupe <objectif>         # l'agent métier propose des issues, tu valides"
  echo "  3. /traite-issue <num>         # première issue de bout en bout, à la main"
  echo "Guide : docs/guide-agents.md"
}

# ask NOM "question" "défaut" : ne demande que si la variable n'est pas déjà renseignée
ask() {
  var="$1"; prompt="$2"; def="$3"
  [ -n "$(getvar "$var")" ] && return 0
  if [ "$YES" -eq 1 ]; then setvar "$var" "$def"; return 0; fi
  if [ -n "$def" ]; then printf '%s [%s] : ' "$prompt" "$def"; else printf '%s : ' "$prompt"; fi
  read -r answer </dev/tty || answer=""
  [ -z "$answer" ] && answer="$def"
  setvar "$var" "$answer"
}

# Définies en Task 6 ; en attendant, des coquilles vides pour que le mode local fonctionne.
git_and_github() { :; }
cleanup() { :; }

main "$@"
EOF
chmod +x setup.sh
```

- [ ] **Step 5 : lancer le test (partie locale) et le lint**

Run: `./tests/test-setup.sh`
Expected: toutes les lignes `✓` jusqu'à `bits exécutables préservés`, puis `test-setup : OK`. Le bloc « Task 6 » du test est sauté tant que `setup.sh` ne contient pas la chaîne `git rm -q -r` (présente uniquement dans l'implémentation de Task 6).

Run: `./tests/lint-template.sh`
Expected: `lint-template : OK`

- [ ] **Step 6 : essai manuel du mode interactif**

Run (dans une copie temporaire) : `cp -R . /tmp/essai-setup && cd /tmp/essai-setup && rm -rf .git && ./setup.sh --no-github` puis répondre aux questions (Entrée pour les défauts, un contexte, un binaire), et répondre `n` à « Continuer ? ».
Expected: les questions s'affichent avec leurs défauts, le récapitulatif est correct, `✗ Abandon.` et aucun fichier modifié. Supprimer `/tmp/essai-setup` ensuite.

- [ ] **Step 7 : commiter**

```bash
git add setup.sh setup.env.example tests/test-setup.sh
git commit -m "feat: setup.sh — collecte, substitution, README (mode local)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6 : `setup.sh` — git, GitHub, nettoyage

**Files:**
- Modify: `setup.sh` (remplacer les coquilles `git_and_github` et `cleanup`)
- Test: `tests/test-setup.sh` (bloc Task 6 déjà écrit, activé par la présence de `git rm -q -r` dans setup.sh)

**Interfaces:**
- Consumes: variables `PROJECT_NAME GITHUB_VISIBILITY`, `NO_GITHUB`, `SETUP_FILES`, helpers `info ok warn die`.
- Produces: dépôt local commité sur `main` ; si réseau : repo GitHub créé ou poussé, labels, protection.

- [ ] **Step 1 : lancer le test pour voir l'état**

Run: `./tests/test-setup.sh`
Expected: `test-setup : OK` mais sans les lignes du bloc Task 6 (bloc sauté).

- [ ] **Step 2 : remplacer les coquilles par l'implémentation**

Dans `setup.sh`, remplacer les deux lignes
```bash
git_and_github() { :; }
cleanup() { :; }
```
par :

```bash
# git_and_github : commit local puis, sauf --no-github, repo/push, labels, protection de main.
git_and_github() {
  info "Git"
  if [ ! -d .git ]; then
    git init -q
    git checkout -q -b main 2>/dev/null || git symbolic-ref HEAD refs/heads/main
    ok "dépôt git initialisé (main)"
  fi
  cleanup_files
  git add -A
  if git diff --cached --quiet; then
    warn "rien à commiter"
  else
    git -c user.useConfigOnly=false commit -q -m "chore: paramétrage du projet $PROJECT_NAME depuis template-agents" \
      || die "commit impossible : configure git (user.name / user.email) puis relance"
    ok "commit de paramétrage créé"
  fi

  [ "$NO_GITHUB" -eq 1 ] && { info "Mode --no-github : repo, labels et protection non traités"; return 0; }

  info "GitHub"
  if git remote get-url origin >/dev/null 2>&1; then
    git push -q -u origin main || die "push vers origin/main refusé (branche protégée ou droits) : pousse à la main puis relance les labels"
    ok "poussé sur origin/main"
  else
    gh repo create "$PROJECT_NAME" "--$GITHUB_VISIBILITY" --source . --push >/dev/null || die "création du repo GitHub échouée"
    ok "repo GitHub créé ($GITHUB_VISIBILITY) et poussé"
  fi
  REPO=$(gh repo view --json nameWithOwner --jq .nameWithOwner)

  gh label create ready       --repo "$REPO" --color 0E8A16 --description "Prête pour les agents" --force >/dev/null
  gh label create in-progress --repo "$REPO" --color FBCA04 --description "Prise par un agent" --force >/dev/null
  gh label create review      --repo "$REPO" --color 1D76DB --description "PR ouverte, attend ton merge" --force >/dev/null
  gh label create needs-human --repo "$REPO" --color D93F0B --description "Bloquée, décision humaine requise" --force >/dev/null
  gh label create epic        --repo "$REPO" --color 5319E7 --description "Objectif parent" --force >/dev/null
  ok "labels créés ou mis à jour"

  if gh api -X PUT "repos/$REPO/branches/main/protection" --input - >/dev/null 2>&1 <<'PROTECT'
{
  "required_status_checks": null,
  "enforce_admins": false,
  "required_pull_request_reviews": { "required_approving_review_count": 0 },
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false
}
PROTECT
  then
    ok "branche main protégée (PR obligatoire, pas de force push)"
  else
    warn "protection de main impossible (repo privé sur plan Free ?). Active-la à la main : Settings → Branches → Add rule « main »."
  fi
}

# cleanup_files : retire les fichiers propres au template (avant le commit).
cleanup_files() {
  for f in $SETUP_FILES; do
    [ -e "$f" ] || continue
    if git ls-files --error-unmatch "$f" >/dev/null 2>&1; then
      git rm -q -r "$f"
    else
      rm -rf "$f"
    fi
  done
  rm -f setup.env
  ok "fichiers de setup supprimés"
}

# cleanup : plus rien à faire après le commit (conservé pour lisibilité du flux principal).
cleanup() { :; }
```

- [ ] **Step 3 : lancer le test complet**

Run: `./tests/test-setup.sh`
Expected: toutes les lignes `✓` y compris `dépôt git initialisé, 1 commit sur main, propre`, `fichiers de setup supprimés`, `guide conservé`, `variable obligatoire manquante : arrêt avant toute modification`, `GITHUB_VISIBILITY invalide refusé`, puis `test-setup : OK`.

Si le commit échoue dans le test parce que git n'a pas d'identité dans l'environnement CI : ajouter dans `tests/test-setup.sh`, juste avant l'appel de `setup.sh`, `export GIT_AUTHOR_NAME=test GIT_AUTHOR_EMAIL=test@example.com GIT_COMMITTER_NAME=test GIT_COMMITTER_EMAIL=test@example.com`.

- [ ] **Step 4 : lint et test du hook**

Run: `./tests/lint-template.sh && ./tests/test-guard-branch.sh`
Expected: `lint-template : OK` et `test-guard-branch : OK`.

- [ ] **Step 5 : commiter**

```bash
git add setup.sh tests/test-setup.sh
git commit -m "feat: setup.sh — git, GitHub (repo, labels, protection) et nettoyage

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7 : workflows GitHub Actions

**Files:**
- Create: `.github/workflows/claude-agents.yml` (squelette désactivé, conservé dans le projet)
- Create: `.github/workflows/template-ci.yml` (CI du template, supprimé au setup)

**Interfaces:**
- Consumes: skill `/traite-issue`, tests `tests/*.sh`.

- [ ] **Step 1 : `claude-agents.yml`**

```bash
cat > .github/workflows/claude-agents.yml <<'EOF'
# Traite une issue avec l'équipe d'agents directement sur un runner GitHub.
# DÉSACTIVÉ par défaut (if: false). Pour activer :
#   1. Ajoute le secret ANTHROPIC_API_KEY (Settings → Secrets and variables → Actions),
#      ou remplace par claude_code_oauth_token: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}.
#   2. Supprime la ligne « if: false » ci-dessous.
#   3. Lance-le depuis l'onglet Actions (Run workflow) avec un numéro d'issue « ready ».
# Documentation : https://code.claude.com/docs/en/github-actions
name: Agents — traiter une issue

on:
  workflow_dispatch:
    inputs:
      issue:
        description: Numéro de l'issue (label ready)
        required: true
        type: string

jobs:
  traite-issue:
    if: false
    runs-on: ubuntu-latest
    timeout-minutes: 90
    permissions:
      contents: write
      issues: write
      pull-requests: write
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - name: Branche d'issue
        run: |
          git config user.name "claude-agents[bot]"
          git config user.email "claude-agents@users.noreply.github.com"
          git checkout -b "issue-${{ inputs.issue }}-ci"
      - uses: anthropics/claude-code-action@v1
        with:
          anthropic_api_key: ${{ secrets.ANTHROPIC_API_KEY }}
          github_token: ${{ secrets.GITHUB_TOKEN }}
          prompt: "/traite-issue ${{ inputs.issue }}"
          claude_args: "--max-turns 150 --permission-mode acceptEdits"
EOF
```

- [ ] **Step 2 : `template-ci.yml`**

```bash
cat > .github/workflows/template-ci.yml <<'EOF'
# CI du template lui-même (supprimée par setup.sh dans les projets créés).
name: Template CI

on:
  push:
    branches: [main]
  pull_request:

jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Identité git pour les tests
        run: |
          git config --global user.name test
          git config --global user.email test@example.com
      - name: Lint du template
        run: ./tests/lint-template.sh
      - name: Test du hook guard-branch
        run: ./tests/test-guard-branch.sh
      - name: Test de setup.sh (mode --no-github)
        run: ./tests/test-setup.sh
EOF
```

- [ ] **Step 3 : vérifier**

Run: `./tests/lint-template.sh && ./tests/test-setup.sh`
Expected: OK pour les deux ; dans test-setup, la ligne `fichiers de setup supprimés` prouve que `template-ci.yml` est bien retiré et que `claude-agents.yml` reste (vérifier à la main : ajouter dans le test, après `ok "guide conservé"`, la ligne `[ -f .github/workflows/claude-agents.yml ] || fail "claude-agents.yml supprimé à tort"` puis `ok "workflow claude-agents conservé"`).

- [ ] **Step 4 : commiter**

```bash
git add .github/workflows tests/test-setup.sh
git commit -m "ci: workflow agents (désactivé) et CI du template

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8 : README du template et publication comme template repository

**Files:**
- Create: `README.md` (celui du template ; remplacé par `write_readme` au setup)
- Action: création du repo GitHub `ldb2000/template-agents`, flag template.

**Interfaces:**
- Consumes: tout ce qui précède.

- [ ] **Step 1 : README du template**

```bash
cat > README.md <<'EOF'
# template-agents

Template de projet pour développer avec une équipe d'agents Claude Code pilotée par les issues GitHub : un agent métier découpe un objectif en issues, un orchestrateur (`/traite-issue`) fait passer chaque issue par un codeur, des revues sécurité et tests, une validation métier, jusqu'à une PR que tu merges toi-même. Guide complet : [`docs/guide-agents.md`](docs/guide-agents.md).

## Créer un projet

Prérequis : `gh` authentifié (`gh auth status`), `jq`, `git`, Claude Code installé.

```bash
# 1. Nouveau repo depuis le template
gh repo create mon-projet --template ldb2000/template-agents --private --clone
cd mon-projet

# 2. Paramétrage (questions interactives, ou copie setup.env.example en setup.env puis --yes)
./setup.sh
```

`setup.sh` remplace les placeholders (nom, contexte métier, stack, commandes), génère le README, commite, pousse, crée les labels GitHub et protège `main`, puis se supprime.

Sans GitHub (essai local) : `./setup.sh --no-github`. Sans passer par le template GitHub : `cp -R template-agents mon-projet && cd mon-projet && ./setup.sh` (le repo GitHub est alors créé par le script).

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
```

Après chaque projet créé, reporte ici les ajustements de prompts qui ont fait leurs preuves.
EOF
```

- [ ] **Step 2 : test complet et commit**

Run: `./tests/lint-template.sh && ./tests/test-guard-branch.sh && ./tests/test-setup.sh`
Expected: trois `OK`.

```bash
git add README.md
git commit -m "docs: README du template

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

- [ ] **Step 3 : publier sur GitHub comme template repository**

```bash
gh repo create ldb2000/template-agents --private --source . --push --description "Template de projet : équipe d'agents Claude Code pilotée par les issues GitHub"
gh repo edit ldb2000/template-agents --template
gh repo view ldb2000/template-agents --json isTemplate --jq .isTemplate
```
Expected: dernière commande affiche `true`. La CI `Template CI` se lance sur le push : vérifier `gh run list --limit 1` puis `gh run watch` jusqu'au succès.

- [ ] **Step 4 : validation de bout en bout du chemin principal**

```bash
cd "$(mktemp -d)"
gh repo create essai-template-agents --template ldb2000/template-agents --private --clone
cd essai-template-agents
cat > setup.env <<'ENV'
PROJECT_CONTEXT=Projet d'essai du template, à supprimer.
CLAUDE_BIN=claude-perso
ENV
./setup.sh --yes
gh label list
gh api repos/{owner}/essai-template-agents/branches/main/protection --jq .required_pull_request_reviews 2>&1 | head -3
```
Expected: setup termine par `✓ Projet « essai-template-agents » prêt.` ; 5 labels listés ; la protection répond (ou avertissement affiché par setup si plan Free). Puis supprimer le repo d'essai : `gh repo delete essai-template-agents --yes` et le dossier temporaire.

---

## Auto-revue du plan

- **Couverture spec** : §2 chemins d'entrée → Task 6 (détection `.git`/`origin`) + Task 8 ; §3 arborescence → Tasks 1-8 ; §4 placeholders et dérivation des permissions → Task 5 (`cmd_prefix`, `enrich_settings`) ; §5 flux → Tasks 5-6 ; §6 contenu agents/skills/hook/scripts → Tasks 1, 3, 4 ; §7 erreurs → Task 5 (validation avant modification, placeholders restants) et Task 6 (échec réseau non bloquant pour la protection, `die` explicite pour push/create) ; §8 tests → Tasks 1, 2, 5, 6, 7 ; §10 écarts → skills (Task 3), `issue-*` (Task 3), ordre commit/push/protection (Task 6), opt-out git config (Task 1).
- **Cohérence des noms** : `getvar/setvar/ask/cmd_prefix/sed_escape/substitute_file/enrich_settings/write_readme/git_and_github/cleanup_files/cleanup/main` utilisés uniquement tels que définis en Task 5-6. Variable `SETUP_FILES` définie en Task 5, consommée en Task 6. Le test de Task 5 détecte l'implémentation de Task 6 par la chaîne `git rm -q -r`.
- **Point d'attention** : `read -r … </dev/tty` échoue sans terminal ; couvert par `|| answer=""` et par `--yes` dans les tests et la CI.
