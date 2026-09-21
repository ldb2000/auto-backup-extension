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
  # Slug ASCII : translittération via iconv si disponible (sortie capturée, code de retour ignoré),
  # repli sur le titre brut si vide ; les ' ` " ^ ~ produits par la translittération BSD sont retirés. Jamais fatal.
  ASCII=$(printf '%s' "$TITLE" | iconv -f UTF-8 -t ASCII//TRANSLIT 2>/dev/null | tr -d "\047\140\042^~") || true
  [ -n "$ASCII" ] || ASCII="$TITLE"
  SLUG=$(printf '%s' "$ASCII" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9' '-' | cut -c1-40 | sed 's/-$//; s/^-//') || SLUG=""
  [ -n "$SLUG" ] || SLUG="sans-titre"
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
  ) </dev/null &
  STARTED=$((STARTED + 1))
done <<EOF_ISSUES
$ISSUES
EOF_ISSUES

if [ "$DRY_RUN" -eq 1 ]; then echo "Mode --dry-run : rien lancé."; exit 0; fi
echo "$STARTED session(s) lancée(s), attente de la fin…"
wait
echo "Toutes les sessions sont terminées. Journaux dans $LOG_DIR/."
