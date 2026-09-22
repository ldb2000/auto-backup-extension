# Documentation

Cette section centralise la documentation du projet au-delà du `README.md` racine.

## Architecture et décisions

- **[ADR 0001 — Socle des destinations distantes](adr/0001-destinations-distantes.md)** :
  Architecture décisionnelle du support des destinations cloud (Dropbox, Google Drive).
  Couvre la persistance (entry.options), le registre de fournisseurs, la hiérarchie d'erreurs,
  et la validation sécurisée des chemins distants en POSIX normalisé.

## Destinations distantes

- **[Connecter un compte Dropbox](destinations/dropbox.md)** : création de l'application
  Dropbox (type d'accès, portées à cocher), URI de redirection à déclarer, récupération de
  la clé et du secret, connexion depuis les options, limites connues.

## Maintenance

- **[Suivi de l'upstream](UPSTREAM.md)** : Révision importée de jcwillox/hass-auto-backup,
  écarts documentés, exemptions de lint et procédure de resynchronisation.
- **[Intégration continue](ci.md)** : Workflow GitHub Actions, jobs de lint, tests et validation,
  reproduction en local, versions d'actions épinglées et garde-fous automatisés.

## Tests

- **[Tests](tests.md)** : Lancement, structure des tests, fixtures disponibles, validation
  stricte des entités, tester une destination distante, modification du code upstream.

## Équipe d'agents

- **[Guide des agents](guide-agents.md)** : Workflow de déploiement des issues (fourni par le
  dépôt ; voir aussi `CLAUDE.md` et `./scripts/agents-run.sh`).
