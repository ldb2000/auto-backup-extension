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

# Refspec explicite vers main (quelle que soit la branche courante) : toujours refusé.
# La commande est découpée sur ; & | et retours à la ligne : seul le segment « git push » est examiné,
# pour ne pas refuser un commit dont le message contiendrait « :main ».
if printf '%s\n' "$CMD" | tr ';&|' '\n' | grep -E '^ *git +push' | grep -qE ':(refs/heads/)?main([^a-zA-Z0-9_-]|$)'; then
  echo "Push refusé : refspec vers main interdit." >&2
  exit 2
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
