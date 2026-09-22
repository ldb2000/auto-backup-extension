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
- Socle des destinations distantes dans `custom_components/auto_backup/destinations/` : contrat abstrait `RemoteDestination` (`async_upload`, `async_list_backups`, `async_delete_backup`, `async_check_connection`), types `DestinationConfig` et `RemoteBackup`, erreurs typées (`DestinationError`, `DestinationAuthError`, `DestinationQuotaError`, `DestinationNotFoundError`, `DestinationConfigError`, `UnknownProviderError`, `DuplicateProviderError`). Refs #6
- Registre de fournisseurs de destinations (`register_provider`, décorateur `provider`, `get_provider`, `list_providers`) : un fournisseur s'ajoute sans modifier le cœur de l'intégration. Refs #6
- Persistance des destinations dans `entry.options` et `DestinationManager` exposé dans `hass.data[DATA_DESTINATIONS]` : la configuration (identifiant, fournisseur, nom, dossier, rétentions) est rechargée à l'identique après redémarrage. Refs #6
- Rétention distante par destination : `retention_days` et `retention_count` facultatifs, validés comme entiers strictement positifs par le schéma voluptuous et par la dataclass. Refs #6
- Constantes du fork dans `const.py` : `DATA_DESTINATIONS`, `CONF_DESTINATIONS`, `CONF_DESTINATION_ID`, `CONF_PROVIDER`, `CONF_FOLDER`, `CONF_RETENTION_DAYS`, `CONF_RETENTION_COUNT`, `DEFAULT_DESTINATION_FOLDER` et les événements `auto_backup.upload_start`, `auto_backup.upload_successful`, `auto_backup.upload_failed`, `auto_backup.remote_purge`. Refs #6
- Décision d'architecture documentée dans `docs/adr/0001-destinations-distantes.md` (support de persistance, registre de fournisseurs, hiérarchie d'erreurs). Refs #6
- Tests du socle des destinations avec un fournisseur factice en mémoire (`tests/destinations_factices.py`, fixture `fournisseur_factice`) : cycle de vie, erreurs typées, registre, validation des rétentions et persistance après rechargement. Refs #6
- Gestion des destinations distantes depuis les options de l'intégration : le flux d'options s'ouvre sur un menu (« Ajouter une destination », « Ré-autoriser une destination », « Supprimer une destination », « Réglages des sauvegardes »), le formulaire upstream restant inchangé comme étape du menu. Refs #7
- Autorisation OAuth2 conduite dans le flux d'options (`destinations/oauth.py`) : l'utilisateur saisit les identifiants de sa propre application (secret masqué à la saisie), est redirigé vers le fournisseur, et Home Assistant reçoit le retour sur la route `/auth/auto_backup/callback` propre au fork — la vue standard ne sachant reprendre qu'un flux de configuration. Refs #7
- Champs d'autorisation facultatifs sur une destination (`client_id`, `client_secret`, `token`), validés par le schéma comme par `DestinationConfig`, et absents des options persistées quand ils ne servent pas. Refs #7
- Rafraîchissement automatique du jeton avant chaque opération (`DestinationOAuth2Session`), jeton renouvelé persisté dans l'entrée et relu à chaque usage. Refs #7
- Détection des accès révoqués : `DestinationAuthError`, marquage « ré-authentification requise » dans le gestionnaire et création d'un problème Home Assistant (« repair issue ») nommant la destination concernée — les autres destinations continuent de fonctionner. Refs #7
- Suppression d'une destination depuis l'interface : configuration, identifiants et jeton effacés des options, destination retirée du gestionnaire et problème associé supprimé. Refs #7
- Unicité du nom d'une destination vérifiée à la saisie, et message d'erreur explicite sur le dossier distant (deux points laissés ouverts par l'issue #6). Refs #7
- Traductions françaises et anglaises des nouvelles étapes, erreurs, abandons et du problème de ré-autorisation (`translations/fr.json`, `translations/en.json`), clés upstream conservées. Refs #7
- Fournisseur factice OAuth2 pour les tests (`DestinationOAuthEnMemoire`, fixtures `fournisseur_oauth_factice` et `ouvrir_les_options`) et couverture du parcours complet avec un mock HTTP : ajout, retour d'autorisation simulé puis réel, rafraîchissement, ré-authentification, suppression. Refs #7

### Modifié

- Le script ad hoc `tests/check_issue_2.py` est remplacé par `tests/test_conformite_upstream.py` : les contrôles hors ligne sont exécutés par `pytest`, la comparaison avec le dépôt upstream est marquée `network` et ne s'exécute qu'avec `uv run pytest --tests-reseau`. Refs #4
- Les destinations configurées sont conservées quand le flux d'options upstream est enregistré, et complétées par les options upstream par défaut lorsqu'elles n'ont jamais été saisies. Refs #6
- `tests/test_conformite_upstream.py` tolère les modules upstream étendus par le fork mais vérifie qu'ils ne subissent que des ajouts, exclut le sous-paquet `destinations/` de la comparaison et exige que chaque écart soit documenté dans `docs/UPSTREAM.md`. Refs #6
- Les exemptions `ruff` de l'upstream sont énumérées module par module dans `pyproject.toml` : le code du fork (`destinations/`) est soumis à toutes les règles et au formatage. Refs #6
- Le flux d'options ne s'ouvre plus directement sur le formulaire `auto_purge` / `backup_timeout` : il s'ouvre sur un menu, où ce formulaire reste accessible sous « Réglages des sauvegardes ». Refs #7
- `docs/adr/0001-destinations-distantes.md` documente le choix de conduire l'autorisation OAuth2 dans le flux d'options plutôt qu'en flux de configuration, le stockage des secrets et le problème de ré-autorisation non réparable automatiquement. Refs #7
- `tests/test_conformite_upstream.py` compte désormais `translations/fr.json` et `translations/en.json` parmi les fichiers upstream étendus : ils ne peuvent recevoir que des ajouts. Refs #7

### Sécurité

- Le dossier distant (`folder`) d'une destination est validé comme chemin relatif POSIX par `chemin_de_dossier()`, appelé par le schéma voluptuous comme par `DestinationConfig` : traversée (`..`), chemin absolu, lettre de lecteur, séparateur Windows, segment vide, espace de bordure et caractère de contrôle sont refusés avant d'atteindre un fournisseur. Refs #6
- Les identifiants d'application et les jetons d'une destination ne sont jamais journalisés : `DestinationConfig.__repr__()` les masque, `as_dict(masquer=True)` produit une copie assainie où le jeton est réduit à ses clés, et un test parcourt l'ajout complet d'une destination avec les journaux en niveau `debug` pour le vérifier. Aucune valeur réelle ne figure dans le code ni dans les tests. Refs #7
- L'état (`state`) du retour d'autorisation est un aléa de 256 bits, à usage unique et expirant au bout de quinze minutes : il sert de jeton anti-CSRF et rejouer un retour ne relance aucun flux. Refs #7
- Le dossier distant est normalisé en NFKC **avant** toute vérification, ce qui referme le contournement par confusables Unicode (U+FF0E, U+FF0F, U+2025 valant `..` ou `/` une fois normalisés) ; les caractères admis sont une liste blanche (alphanumérique Unicode, espace ordinaire, `-`, `_`, `.`, `(`, `)`) et le chemin est borné à 255 caractères au total et 100 par segment. Refs #6
