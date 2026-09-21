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
  [ "$(sed -n '2,$p' "$a" | grep -c '^---$')" -ge 1 ] || fail "$a : frontmatter non fermé"
  fm=$(sed -n '2,/^---$/p' "$a" | sed '$d')
  printf '%s\n' "$fm" | grep -q "^name: $base\$" || fail "$a : 'name: $base' attendu"
  printf '%s\n' "$fm" | grep -q '^description: .' || fail "$a : description absente"
done
ok "agents : frontmatter"

# 4. Skills : SKILL.md avec name et description
for s in .claude/skills/*/SKILL.md; do
  [ -f "$s" ] || fail "aucun skill dans .claude/skills/"
  dir=$(basename "$(dirname "$s")")
  head -1 "$s" | grep -q '^---$' || fail "$s : frontmatter absent"
  [ "$(sed -n '2,$p' "$s" | grep -c '^---$')" -ge 1 ] || fail "$s : frontmatter non fermé"
  fm=$(sed -n '2,/^---$/p' "$s" | sed '$d')
  printf '%s\n' "$fm" | grep -q "^name: $dir\$" || fail "$s : 'name: $dir' attendu"
  printf '%s\n' "$fm" | grep -q '^description: .' || fail "$s : description absente"
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
