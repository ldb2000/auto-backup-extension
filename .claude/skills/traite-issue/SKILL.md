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
- Pousse la branche courante sous son nom exact (lis-le avec `git rev-parse --abbrev-ref HEAD`, ne le recalcule jamais) : `git push -u origin <nom-exact-de-la-branche>`.
- Écris le corps de la PR dans `.pr-body.md` : « Closes #$ARGUMENTS », puis une section par agent (verdict + points clés), puis la liste des commits. Ouvre la PR avec `gh pr create --base main --title "<type>: <titre> (#$ARGUMENTS)" --body-file .pr-body.md`. Supprime `.pr-body.md` après la création de la PR.
- Remplace `in-progress` par `review`.
- Ne merge jamais. Termine par : numéro de PR, URL, verdicts.
