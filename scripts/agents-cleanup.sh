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
# Pas de --search : l'index GitHub ignore # et les accents.
CANDIDATES=$(gh issue list --state open --limit 200 --json number --jq '.[].number')
for C in $CANDIDATES; do
  BODY=$(gh issue view "$C" --json body --jq .body)
  DEPS=$(printf '%s\n' "$BODY" | sed -n '/^## Dépendances/,/^## /p' | grep -v '^## ' | grep -oE '#[0-9]+' | tr -d '#' | sort -u || true)
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
  if ! gh issue edit "$C" --add-label ready --remove-label needs-human >/dev/null 2>&1; then
    gh issue edit "$C" --add-label ready >/dev/null 2>&1 || { echo "! #$C : pose du label ready échouée, à faire à la main" >&2; continue; }
  fi
  gh issue comment "$C" --body "Débloquée : toutes ses dépendances (dont #$NUM) sont fermées. Label \`ready\` posé automatiquement." >/dev/null
  echo "✓ #$C repassée en ready"
done
echo "Nettoyage de #$NUM terminé."
