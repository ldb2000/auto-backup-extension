# Tests

## Lancer les tests

```bash
uv sync --group dev
uv run pytest
```

`pytest` est configuré dans `pyproject.toml` (`[tool.pytest.ini_options]`) :

- `asyncio_mode = "auto"` : les tests asynchrones s'écrivent sans décorateur ;
- `testpaths = ["tests"]` ;
- `addopts` mesure et affiche la couverture de `custom_components/auto_backup`
  (`--cov=custom_components/auto_backup --cov-report=term-missing`) et désactive le cache
  pytest (`-p no:cacheprovider`) pour ne rien écrire dans le dépôt.

Les tests s'exécutent contre la version de Home Assistant épinglée par
`pytest-homeassistant-custom-component` (voir `README.md`).

### Tests réseau

Les tests marqués `network` comparent `custom_components/auto_backup/` à la révision upstream
figée dans [`UPSTREAM.md`](UPSTREAM.md) ; ils utilisent le CLI `gh` et sont **ignorés par
défaut**. Pour les exécuter :

```bash
uv run pytest --tests-reseau
```

Ils sont ignorés si `gh` n'est pas installé.

## Structure

| Fichier | Rôle |
| --- | --- |
| `tests/conftest.py` | Socle Home Assistant : chargement de l'intégration et fixtures communes. |
| `tests/test_config_flow.py` | Flux de configuration « user » et flux d'options. |
| `tests/test_init.py` | Cycle de vie de l'entrée de configuration et services du domaine. |
| `tests/test_entities.py` | Entités créées et rattachement au device de service. |
| `tests/test_conformite_upstream.py` | Non-régression de l'import upstream (licence, README, manifeste ; comparaison réseau). |
| `tests/test_integration_packaging.py` | Validité des fichiers livrés (compilation, JSON, manifeste). |
| `tests/test_project_tooling.py` | Cohérence de l'outillage Python déclaré dans `pyproject.toml`. |
| `tests/test_configuration_pytest.py` | Garde-fous sur la configuration `pytest` elle-même. |

## Fixtures disponibles

Toutes sont définies dans `tests/conftest.py`.

| Fixture | Effet |
| --- | --- |
| `auto_enable_custom_integrations` | Appliquée automatiquement : rend `custom_components/auto_backup` chargeable par chaque instance Home Assistant de test. |
| `integration_backup` | Charge l'intégration `backup` du cœur, prérequis d'`auto_backup` hors Supervisor. |
| `entree_auto_backup` | Initialise l'intégration depuis une `MockConfigEntry` et renvoie l'entrée créée. |
| `gestionnaire_auto_backup` | Renvoie l'objet `AutoBackup` stocké dans `hass.data`. |

Écrire un test qui démarre l'intégration tient alors en une ligne :

```python
async def test_mon_comportement(hass, entree_auto_backup):
    assert hass.services.has_service("auto_backup", "purge")
```

## Pourquoi `conftest.py` importe `custom_components`

Home Assistant découvre les intégrations personnalisées en important le paquet
`custom_components` depuis `sys.path`. `pytest-homeassistant-custom-component` ajoute son
propre répertoire de configuration de test à `sys.path`, et le `custom_components` qu'il
embarque est un paquet régulier : il l'emporterait sur celui du dépôt, qui est un paquet
d'espace de noms. `tests/conftest.py` importe donc explicitement le paquet du dépôt avant
toute création d'instance Home Assistant. L'intégration est ainsi chargée depuis ses fichiers
réels, ce qui conditionne aussi la mesure de couverture.

## Périmètre

Ces tests couvrent le comportement upstream importé (configuration, services, options,
entités). Les destinations cloud (Dropbox, Google Drive) sont testées par leurs issues
respectives, et l'exécution en intégration continue est traitée séparément.
