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
- Outillage développeur : pytest (28 tests, asyncio automatique) et ruff (formatage et lint, exemptions pour l'upstream documentées dans `docs/UPSTREAM.md`). Refs #3
- Commandes réelles du projet dans `README.md` et `CLAUDE.md` ; scripts d'agents (`scripts/agents-run.sh`, `.claude/`) alignés sur `uv`. Refs #3
- Test du plancher utilisateur : Home Assistant 2025.1.0 déclaré dans `hacs.json`, 2026.9.0 utilisé en développement via `pytest-homeassistant-custom-component`. Refs #3
