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
cat > "$PROJ/reponses.env" <<'ENV'
# commentaire ignoré
PROJECT_NAME=mon-projet
PROJECT_CONTEXT="Application de suivi de séances pour coachs sportifs & leurs clients."
STACK_BACKEND=FastAPI
STACK_DB=
CMD_INSTALL=uv sync
CMD_TEST=pytest -q
CMD_TEST_E2E=pytest -q tests/e2e
CMD_LINT=ruff check .
CMD_BUILD=
CLAUDE_BIN=claude-test
MAX_PARALLEL=3
GITHUB_VISIBILITY=
CLE_INCONNUE=oui
ENV
( cd "$PROJ" && ./setup.sh --yes --no-github --env "$PROJ/reponses.env" ) > "$TMP/out.log" 2>&1 || fail "setup.sh a échoué (code $?)"
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
grep -q 'Build : (aucune)' CLAUDE.md || fail "CMD_BUILD vide dans setup.env a été écrasé par le défaut"
ok "valeur vide de setup.env conservée"
grep -q 'BDD : à définir' CLAUDE.md || fail "STACK_DB vide dans setup.env aurait dû prendre le défaut"
ok "clé non-CMD vide dans setup.env = défaut"
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

grep -q "n'est pas trouvé dans le PATH" "$TMP/out.log" || fail "avertissement CLAUDE_BIN absent du PATH manquant"
ok "CLAUDE_BIN absent du PATH : avertissement"

[ ! -f reponses.env ] || fail "fichier --env non supprimé"
! git ls-files | grep -q reponses.env || fail "fichier --env commité"
ok "fichier --env supprimé et non commité"

# ---------- Sans terminal et sans --yes : arrêt net
PROJ4="$TMP/projet-notty"
copy_template "$PROJ4"
if ! ( : </dev/tty ) 2>/dev/null || command -v setsid >/dev/null 2>&1; then
  if command -v setsid >/dev/null 2>&1; then RUN4="setsid ./setup.sh --no-github"; else RUN4="./setup.sh --no-github"; fi
  if ( cd "$PROJ4" && $RUN4 </dev/null ) > "$TMP/out4.log" 2>&1; then
    if [ -r /dev/tty ] && ( : </dev/tty ) 2>/dev/null; then echo "  · sans terminal : non testable ici (terminal présent, pas de setsid)"; else fail "setup.sh sans --yes ni terminal aurait dû s'arrêter"; fi
  else
    grep -q 'Aucun terminal interactif' "$TMP/out4.log" || fail "message « Aucun terminal interactif » absent"
    grep -q '{{'"PROJECT_NAME"'}}' "$PROJ4/CLAUDE.md" || fail "substitution effectuée malgré l'absence de terminal"
    ok "sans terminal ni --yes : arrêt avant toute modification"
  fi
else
  echo "  · sans terminal : non testable ici"
fi

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
  [ -f .github/workflows/claude-agents.yml ] || fail "claude-agents.yml supprimé à tort"
  ok "workflow claude-agents conservé"

  # Cas d'erreur : variable obligatoire manquante → aucun effet
  PROJ2="$TMP/projet-ko"
  copy_template "$PROJ2"
  printf 'PROJECT_NAME=ko\nCLAUDE_BIN=claude-test\n' > "$PROJ2/setup.env"
  if ( cd "$PROJ2" && ./setup.sh --yes --no-github ) > "$TMP/out2.log" 2>&1; then fail "setup.sh aurait dû échouer sans PROJECT_CONTEXT"; fi
  grep -q 'PROJECT_CONTEXT' "$TMP/out2.log" || fail "le message d'erreur ne nomme pas PROJECT_CONTEXT"
  [ ! -d "$PROJ2/.git" ] || fail "git init effectué malgré l'échec"
  grep -q '{{'"PROJECT_NAME"'}}' "$PROJ2/CLAUDE.md" || fail "substitution effectuée malgré l'échec"
  ok "variable obligatoire manquante : arrêt avant toute modification"

  # Cas d'erreur : visibilité invalide
  PROJ3="$TMP/projet-vis"
  copy_template "$PROJ3"
  printf 'PROJECT_CONTEXT=x\nCLAUDE_BIN=c\nGITHUB_VISIBILITY=interne\n' > "$PROJ3/setup.env"
  if ( cd "$PROJ3" && ./setup.sh --yes --no-github ) > "$TMP/out3.log" 2>&1; then fail "GITHUB_VISIBILITY invalide accepté"; fi
  ok "GITHUB_VISIBILITY invalide refusé"

  # Cas d'erreur : copie avec .git du template (origin = template lui-même)
  PROJ5="$TMP/projet-origin-template"
  copy_template "$PROJ5"
  ( cd "$PROJ5" && git init -q && git checkout -q -b main \
      && git -c user.email=t@t -c user.name=t commit -q --allow-empty -m init \
      && git remote add origin https://github.com/ldb2000/template-agents.git )
  printf 'PROJECT_CONTEXT=x\nCLAUDE_BIN=c\n' > "$PROJ5/setup.env"
  if ( cd "$PROJ5" && ./setup.sh --yes --no-github ) > "$TMP/out5.log" 2>&1; then fail "setup.sh aurait dû refuser (origin = template)"; fi
  grep -q 'pointe sur le template lui-même' "$TMP/out5.log" || fail "message origin = template absent"
  [ "$(cd "$PROJ5" && git rev-list --count HEAD)" = "1" ] || fail "un commit de paramétrage a été créé malgré le refus (origin = template)"
  ok "origin = template : refus avant toute modification"

  # Fichier --env hors du projet : conservé (jamais supprimé)
  PROJ6="$TMP/projet-env-externe"
  copy_template "$PROJ6"
  printf 'PROJECT_CONTEXT=x\nCLAUDE_BIN=c\n' > "$TMP/partage.env"
  ( cd "$PROJ6" && ./setup.sh --yes --no-github --env "$TMP/partage.env" ) > "$TMP/out6.log" 2>&1 || fail "setup.sh a échoué avec --env externe"
  [ -f "$TMP/partage.env" ] || fail "le fichier --env hors du projet a été supprimé"
  grep -q 'hors du projet conservé' "$TMP/out6.log" || fail "avertissement « hors du projet conservé » absent"
  ok "fichier --env hors du projet conservé"
fi

echo "test-setup : OK"
