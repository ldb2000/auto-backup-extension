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
SET_VARS=""   # clés fournies par setup.env à ne pas redemander : toute clé non vide, et les CMD_* même vides
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

# substitute_file FICHIER : remplace tous les {{ VAR }} (mode du fichier préservé)
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
  if [ "$YES" -eq 0 ] && ! ( : </dev/tty ) 2>/dev/null; then
    die "Aucun terminal interactif : lance ./setup.sh --yes avec un setup.env complet (voir setup.env.example)"
  fi

  # 2. Collecte : fichier de réponses
  if [ -f "$ENV_FILE" ]; then
    info "Lecture de $ENV_FILE"
    while IFS= read -r line || [ -n "$line" ]; do
      case "$line" in ''|'#'*) continue ;; esac
      case "$line" in *=*) ;; *) warn "Ligne ignorée (pas de =) : $line"; continue ;; esac
      key="${line%%=*}"; val="${line#*=}"
      val="${val%\"}"; val="${val#\"}"
      case " $VARS " in
        *" $key "*)
          setvar "$key" "$val"
          # Vide = « explicitement vide » pour les commandes seulement ; ailleurs vide = défaut
          case "$key" in
            CMD_*) SET_VARS="$SET_VARS $key" ;;
            *) if [ -n "$val" ]; then SET_VARS="$SET_VARS $key"; fi ;;
          esac
          ;;
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
  ask CLAUDE_BIN "Exécutable Claude Code dans le PATH pour les scripts (ex. claude ; pas un alias ; obligatoire)" ""
  ask MAX_PARALLEL "Sessions parallèles par défaut" "2"
  ask GITHUB_VISIBILITY "Visibilité GitHub si le repo doit être créé (private|public)" "private"

  # 3. Validation
  for v in $REQUIRED; do
    [ -n "$(getvar "$v")" ] || die "Variable obligatoire manquante : $v (renseigne-la dans setup.env ou réponds à la question)"
  done
  command -v "$CLAUDE_BIN" >/dev/null 2>&1 || warn "« $CLAUDE_BIN » n'est pas trouvé dans le PATH : scripts/agents-run.sh échouera tant qu'un exécutable de ce nom n'existe pas (un alias ne suffit pas)."
  case "$GITHUB_VISIBILITY" in private|public) ;; *) die "GITHUB_VISIBILITY doit valoir private ou public (reçu : $GITHUB_VISIBILITY)" ;; esac
  case "$MAX_PARALLEL" in ''|*[!0-9]*) die "MAX_PARALLEL doit être un entier (reçu : $MAX_PARALLEL)" ;; esac
  for v in $VARS; do
    case "$(getvar "$v")" in *"
"*) die "$v ne doit pas contenir de retour à la ligne" ;; esac
  done

  echo
  info "Récapitulatif"
  for v in $VARS; do printf '  %-18s %s\n' "$v" "$(getvar "$v")"; done
  printf '  %-18s %s\n' "(remote origin)" "$(git remote get-url origin 2>/dev/null || echo 'aucun — un repo sera créé')"
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

  # 4b. CLAUDE.md : rendre lisibles les commandes vides
  tmp_claude="CLAUDE.md.tmp.$$"
  sed 's/: ``$/: (aucune)/' CLAUDE.md > "$tmp_claude"
  cat "$tmp_claude" > CLAUDE.md
  rm -f "$tmp_claude"

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

  # 7-8. Git, GitHub (Task 6)
  git_and_github

  # 10. Récapitulatif final
  echo
  ok "Projet « $PROJECT_NAME » prêt."
  echo "Prochaines étapes :"
  echo "  1. $CLAUDE_BIN                 # ouvre Claude Code dans ce dossier"
  echo "  2. /decoupe <objectif>         # l'agent métier propose des issues, tu valides"
  echo "  3. /traite-issue <num>         # première issue de bout en bout, à la main"
  echo "Guide : docs/guide-agents.md"
}

# ask NOM "question" "défaut" : ne demande que si la variable n'a été ni fournie par setup.env ni déjà renseignée.
# En interactif, répondre « - » donne une valeur vide.
ask() {
  var="$1"; prompt="$2"; def="$3"
  case " $SET_VARS " in *" $var "*) return 0 ;; esac
  [ -n "$(getvar "$var")" ] && return 0
  if [ "$YES" -eq 1 ]; then setvar "$var" "$def"; return 0; fi
  if [ -n "$def" ]; then printf '%s [%s, - pour vide] : ' "$prompt" "$def"; else printf '%s : ' "$prompt"; fi
  read -r answer </dev/tty || answer=""
  if [ "$answer" = "-" ]; then answer=""; elif [ -z "$answer" ]; then answer="$def"; fi
  setvar "$var" "$answer"
}

# git_and_github : commit local puis, sauf --no-github, repo/push, labels, protection de main.
git_and_github() {
  info "Git"
  if git rev-parse --git-dir >/dev/null 2>&1; then
    CUR_BRANCH=$(git symbolic-ref --short -q HEAD 2>/dev/null || git rev-parse --short HEAD 2>/dev/null || echo main)
    [ "$CUR_BRANCH" = "main" ] || die "Branche courante « $CUR_BRANCH » : le paramétrage doit se faire sur main (git checkout main) puis relance ./setup.sh"
    if git remote get-url origin >/dev/null 2>&1; then
      ORIGIN_URL=$(git remote get-url origin)
      ORIGIN_NAME=$(basename -s .git "$ORIGIN_URL")
      if [ "$ORIGIN_NAME" = "template-agents" ]; then
        die "Le remote origin pointe sur le template lui-même ($ORIGIN_URL). Tu as copié le dossier avec son .git : fais « rm -rf .git » puis relance ./setup.sh (un nouveau repo sera créé)."
      fi
      [ "$ORIGIN_NAME" = "$PROJECT_NAME" ] || warn "PROJECT_NAME « $PROJECT_NAME » diffère du repo distant « $ORIGIN_NAME » ($ORIGIN_URL) : le push ira sur ce repo distant."
    fi
  fi
  if ! git rev-parse --git-dir >/dev/null 2>&1; then
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
    git push -q -u origin main || die "push vers origin/main refusé (branche protégée ou droits). Le commit local est fait. À la main : git push -u origin main, puis les 5 labels (gh label create …, voir docs/guide-agents.md étape 3) et la protection de main (Settings → Branches)."
    ok "poussé sur origin/main"
  else
    gh repo create "$PROJECT_NAME" "--$GITHUB_VISIBILITY" --source . --push >/dev/null \
      || die "création du repo GitHub échouée. À faire à la main : gh repo create $PROJECT_NAME --$GITHUB_VISIBILITY --source . --push (si le repo existe déjà : git remote add origin <url> && git push -u origin main), puis crée les labels et la protection de main à la main (voir docs/guide-agents.md, étape 3) — le commit local est déjà fait"
    ok "repo GitHub créé ($GITHUB_VISIBILITY) et poussé"
  fi
  REPO=$(gh repo view --json nameWithOwner --jq .nameWithOwner)

  gh label create ready       --repo "$REPO" --color 0E8A16 --description "Prête pour les agents" --force >/dev/null \
    || warn "label ready non créé : gh label create ready --color 0E8A16 --description \"Prête pour les agents\" --force"
  gh label create in-progress --repo "$REPO" --color FBCA04 --description "Prise par un agent" --force >/dev/null \
    || warn "label in-progress non créé : gh label create in-progress --color FBCA04 --description \"Prise par un agent\" --force"
  gh label create review      --repo "$REPO" --color 1D76DB --description "PR ouverte, attend ton merge" --force >/dev/null \
    || warn "label review non créé : gh label create review --color 1D76DB --description \"PR ouverte, attend ton merge\" --force"
  gh label create needs-human --repo "$REPO" --color D93F0B --description "Bloquée, décision humaine requise" --force >/dev/null \
    || warn "label needs-human non créé : gh label create needs-human --color D93F0B --description \"Bloquée, décision humaine requise\" --force"
  gh label create epic        --repo "$REPO" --color 5319E7 --description "Objectif parent" --force >/dev/null \
    || warn "label epic non créé : gh label create epic --color 5319E7 --description \"Objectif parent\" --force"
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
  # Le fichier --env n'est supprimé que s'il est dans le projet (un fichier partagé ailleurs est conservé).
  if [ -f "$ENV_FILE" ]; then
    env_abs="$(cd "$(dirname "$ENV_FILE")" && pwd -P)/$(basename "$ENV_FILE")"
    case "$env_abs" in
      "$(pwd -P)"/*) rm -f "$ENV_FILE" ;;
      *) warn "fichier --env hors du projet conservé : $ENV_FILE (il contient ton contexte métier, ne le commite pas ailleurs)" ;;
    esac
  fi
  ok "fichiers de setup supprimés"
}

main "$@"
