# Changelog

Tous les changements notables de ce projet sont documentés dans ce fichier.

Le format est basé sur [Keep a Changelog](https://keepachangelog.com/fr/1.0.0/),
et ce projet adhère à la [Versioning Sémantique](https://semver.org/spec/v2.0.0.html).

## [Non publié]

### Ajouté

- Import de l'intégration `auto_backup` depuis le fork upstream jcwillox/hass-auto-backup (SHA 809295d2737b9613cf6d5f5d15a53ae5a861658d). Refs #2
- Licence MIT double copyright (Joshua Cowie-Willox et Laurent Deberti) dans `LICENSE`.
- Documentation de traçabilité upstream dans `docs/UPSTREAM.md` avec procédure de resynchronisation.
- Stack Python 3.14 via `uv` : `pyproject.toml`, `uv.lock`, `.python-version` (plancher utilisateur HA 2025.1.0 inchangé). Refs #3
- Outillage développeur : pytest avec tests asynchrones automatiques et ruff (formatage et lint, exemptions pour l'upstream documentées dans `docs/UPSTREAM.md`). Refs #3
- Commandes réelles du projet dans `README.md` et `CLAUDE.md` ; scripts d'agents (`scripts/agents-run.sh`, `.claude/`) alignés sur `uv`. Refs #3
- Test du plancher utilisateur : Home Assistant 2025.1.0 déclaré dans `hacs.json`, 2026.9.0 utilisé en développement via `pytest-homeassistant-custom-component`. Refs #3
- Socle de tests Home Assistant : `tests/conftest.py` (intégrations custom activées, fixtures `integration_backup`, `entree_auto_backup`, `gestionnaire_auto_backup`) et tests de non-régression du flux de configuration, des services, du flux d'options et des entités. Refs #4
- Mesure et affichage de la couverture de `custom_components/auto_backup` à chaque `uv run pytest`, et documentation des tests dans `docs/tests.md`. Refs #4
- Intégration continue GitHub Actions (`.github/workflows/ci.yml`) : jobs `lint` (`ruff check` et `ruff format --check`), `tests` (`pytest` sur la version Python de `.python-version`) et `validate` (`hassfest` et validation HACS en catégorie `integration`), déclenchés sur les PR vers `main` et les pushs sur `main`. Refs #5
- Workflow sans secret, `permissions: contents: read`, actions épinglées à une version, cache uv des dépendances Python et annulation des runs obsolètes d'une même PR (`concurrency`). Refs #5
- Badge CI dans `README.md` et page `docs/ci.md` (jobs, versions d'actions épinglées, reproduction en local, écarts documentés) ; garde-fous automatisés dans `tests/test_ci_workflow.py`. Refs #5
- Dépôt rendu public (prérequis HACS pour les contrôles `hacsjson` et `integration_manifest`). Refs #5

### Modifié

- Le script ad hoc `tests/check_issue_2.py` est remplacé par `tests/test_conformite_upstream.py` : les contrôles hors ligne sont exécutés par `pytest`, la comparaison avec le dépôt upstream est marquée `network` et ne s'exécute qu'avec `uv run pytest --tests-reseau`. Refs #4
