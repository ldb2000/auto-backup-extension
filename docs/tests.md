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

Prérequis : `gh` doit être installé et vous devez être authentifié avec `gh auth login`.
Les tests sont ignorés si l'authentification échoue ou si `gh` n'est pas disponible.

## Structure

| Fichier | Rôle |
| --- | --- |
| `tests/conftest.py` | Socle Home Assistant : chargement de l'intégration et fixtures communes. |
| `tests/test_config_flow.py` | Flux de configuration « user » et flux d'options. |
| `tests/test_init.py` | Cycle de vie de l'entrée de configuration et services du domaine. |
| `tests/test_entities.py` | Entités créées et rattachement au device de service. |
| `tests/test_destinations.py` | Socle des destinations distantes : contrat, types, erreurs, registre, gestionnaire. |
| `tests/test_destinations_persistance.py` | Persistance des destinations dans l'entrée et rechargement après redémarrage. |
| `tests/destinations_factices.py` | Fournisseur de destination factice, en mémoire (aide, pas un module de tests). |
| `tests/test_conformite_upstream.py` | Non-régression de l'import upstream (licence, README, manifeste, écarts documentés ; comparaison réseau). |
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
| `fournisseur_factice` | Enregistre le fournisseur de destination factice dans le registre le temps du test, puis le retire. |

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

## Validation stricte des entités

Le fichier `tests/test_entities.py` valide que la liste complète des entités créées par
l'intégration correspond à celle attendue. Celle-ci est définie en constante `ENTITES_ATTENDUES`
au début du fichier, organisée par domaine de plateforme (`sensor`, `binary_sensor`, `button`).

Quand des entités sont ajoutées ou supprimées à l'intégration, cette liste doit être mise à jour
en conséquence, sinon les tests échoueront. Il en est de même pour l'ordre ou l'identifiant
unique (`unique_id`) de chaque entité.

## Tester une destination distante

Les destinations cloud n'ont pas de fournisseur livré tant que Dropbox (#10) et Google Drive
(#13) ne sont pas implémentés. Les tests s'appuient donc sur le fournisseur factice de
[`tests/destinations_factices.py`](../tests/destinations_factices.py), entièrement en mémoire :
aucun fichier n'est lu, aucun appel réseau n'est fait.

```python
async def test_mon_comportement(hass, fournisseur_factice):
    destination = DestinationEnMemoire(
        hass, DestinationConfig.from_dict(config_factice())
    )
    destination.erreur_a_lever = DestinationQuotaError("quota dépassé")

    with pytest.raises(DestinationQuotaError):
        await destination.async_upload("/backup/ha.tar", name="ha")
```

La fixture `fournisseur_factice` enregistre le fournisseur dans le registre global du processus
puis l'en retire : tout test qui enregistre un fournisseur doit faire de même, sous peine de
polluer les tests suivants.

Le champ `folder` d'une destination est un **chemin relatif POSIX** : `tests/test_destinations.py`
éprouve, pour le schéma voluptuous comme pour `DestinationConfig` (par `from_dict()` et par
construction directe), les valeurs refusées — traversée `..`, chemin absolu, séparateur Windows,
segment vide, espace de bordure, caractère de contrôle, confusable Unicode dont la forme NFKC est
une traversée (`\uff0e\uff0e`, `a\uff0f..\uff0fb`, `a/\u2025`), caractère hors liste blanche et
longueur excessive — et les valeurs acceptées. Ces dernières sont écrites sous la forme
`(entrée, valeur attendue)` : la validation normalise en NFKC, la sortie doit donc être comparée
à la forme normalisée (`"Sauvegardes/\uff28"` vaut `"Sauvegardes/H"`) et non à l'entrée brute. La
règle est justifiée dans [l'ADR des destinations distantes](adr/0001-destinations-distantes.md).

Les cas Unicode sont écrits en séquences d'échappement (`\uff0e`) et non avec le caractère
littéral : `ruff` refuse les caractères ambigus dans le code (RUF001/RUF002), et un confusable
copié tel quel serait de toute façon illisible en revue.

## Modifier l'intégration importée

Le répertoire `custom_components/auto_backup/` contient le code importé de l'upstream
(voir [`docs/UPSTREAM.md`](UPSTREAM.md)). **Aucune ligne de ce code ne doit être supprimée ni
modifiée**, sauf lors d'une resynchronisation intentionnelle avec l'upstream. Les tests valident
cette règle (voir la section « Tests réseau » et
[`tests/test_conformite_upstream.py`](../tests/test_conformite_upstream.py)) : les modules
upstream que le fork complète sont comparés à la révision importée, et le test échoue si une
ligne y a disparu ou changé.

Le code propre au fork se range dans un sous-paquet dédié de l'intégration — aujourd'hui
`custom_components/auto_backup/destinations/` — et non dans les modules upstream ni hors de
l'intégration : Home Assistant ne charge que ce qui vit sous `custom_components/auto_backup/`.
Ce code est soumis à l'intégralité des règles de lint et au formatage automatique. Tout nouvel
écart doit être ajouté à [`docs/UPSTREAM.md`](UPSTREAM.md) **et** aux listes de
`tests/test_conformite_upstream.py`.

## Périmètre

Ces tests couvrent le comportement upstream importé (configuration, services, options,
entités) et le socle des destinations distantes (contrat, registre, persistance). Les
fournisseurs cloud eux-mêmes (Dropbox, Google Drive) sont testés par leurs issues respectives,
et l'exécution en intégration continue est traitée séparément.
