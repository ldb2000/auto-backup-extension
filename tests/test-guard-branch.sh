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
[ "$(hook_rc 'git push origin issue-1-test:main')" = "2" ] || fail "git push avec refspec vers main devrait être bloqué (2)"
ok "git push avec refspec vers main bloqué"
[ "$(hook_rc 'git push -u origin issue-1-test')" = "0" ] || fail "git push -u origin issue-1-test devrait passer (0)"
ok "git push -u origin issue-1-test autorisé"
[ "$(hook_rc 'git commit -m \"fix:main.go typo\" && git push -u origin issue-1-test')" = "0" ] || fail "« :main » dans un message de commit ne doit pas bloquer un push légitime (0)"
ok "« :main » hors du segment git push ignoré"
[ "$(hook_rc 'git push origin +issue-1-test:refs/heads/main')" = "2" ] || fail "refspec +…:refs/heads/main devrait être bloqué (2)"
ok "refspec vers refs/heads/main bloqué"

git -C "$TMP" checkout -q main
git -C "$TMP" config agents.guardBranch false
[ "$(hook_rc 'git commit -m x')" = "0" ] || fail "opt-out agents.guardBranch=false devrait autoriser"
ok "opt-out par git config respecté"

echo "test-guard-branch : OK"
