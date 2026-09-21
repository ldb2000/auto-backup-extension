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
