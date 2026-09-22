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

Ces tests **ne sont pas exécutés par l'intégration continue** : le job `tests` lance
`uv run pytest` sans `--tests-reseau`. Un runner GitHub n'a pas de `gh` authentifié garanti et
la CI ne doit pas dépendre de la disponibilité de l'API GitHub. Ils sont donc à lancer
manuellement, en particulier lors d'une resynchronisation upstream (voir [`ci.md`](ci.md)).

## Structure

| Fichier | Rôle |
| --- | --- |
| `tests/conftest.py` | Socle Home Assistant : chargement de l'intégration et fixtures communes. |
| `tests/test_config_flow.py` | Flux de configuration « user » et flux d'options. |
| `tests/test_init.py` | Cycle de vie de l'entrée de configuration et services du domaine. |
| `tests/test_entities.py` | Entités créées et rattachement au device de service. |
| `tests/test_destinations.py` | Socle des destinations distantes : contrat, types, erreurs, registre, gestionnaire. |
| `tests/test_destinations_persistance.py` | Persistance des destinations dans l'entrée et rechargement après redémarrage. |
| `tests/test_destinations_oauth.py` | Autorisation OAuth2 : déclaration d'un fournisseur, masquage des secrets, états, rafraîchissement du jeton, ré-authentification requise. |
| `tests/test_destinations_flux_options.py` | Interface : menu des options, ajout, ré-autorisation et suppression d'une destination, vue de retour d'autorisation. |
| `tests/test_provider_google_drive.py` | Fournisseur Google Drive : déclaration OAuth2, URL d'autorisation, ajout complet, identification du compte, erreurs, rafraîchissement et révocation. |
| `tests/destinations_factices.py` | Fournisseurs de destination factices, en mémoire (aide, pas un module de tests). |
| `tests/test_conformite_upstream.py` | Non-régression de l'import upstream (licence, README, manifeste, écarts documentés ; comparaison réseau). |
| `tests/test_integration_packaging.py` | Validité des fichiers livrés (compilation, JSON, manifeste). |
| `tests/test_project_tooling.py` | Cohérence de l'outillage Python déclaré dans `pyproject.toml`. |
| `tests/test_configuration_pytest.py` | Garde-fous sur la configuration `pytest` elle-même. |
| `tests/test_ci_workflow.py` | Garde-fous sur le workflow d'intégration continue. |

## Fixtures disponibles

Toutes sont définies dans `tests/conftest.py`.

| Fixture | Effet |
| --- | --- |
| `auto_enable_custom_integrations` | Appliquée automatiquement : rend `custom_components/auto_backup` chargeable par chaque instance Home Assistant de test. |
| `integration_backup` | Charge l'intégration `backup` du cœur, prérequis d'`auto_backup` hors Supervisor. |
| `entree_auto_backup` | Initialise l'intégration depuis une `MockConfigEntry` et renvoie l'entrée créée. |
| `gestionnaire_auto_backup` | Renvoie l'objet `AutoBackup` stocké dans `hass.data`. |
| `fournisseur_factice` | Enregistre le fournisseur de destination factice dans le registre le temps du test, puis le retire. |
| `fournisseur_oauth_factice` | Idem pour `DestinationOAuthEnMemoire`, la variante qui déclare une `OAUTH2_SPEC` et exige un jeton valide avant chaque opération. |
| `ouvrir_les_options` | Ouvre le flux d'options et choisit une étape de son menu : `await ouvrir_les_options(entry_id, "init")`. |

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

Les tests du **socle** s'appuient sur le fournisseur factice de
[`tests/destinations_factices.py`](../tests/destinations_factices.py), entièrement en mémoire :
aucun fichier n'est lu, aucun appel réseau n'est fait. Ils démontrent qu'un fournisseur s'ajoute
par le seul registre, sans rien changer au cœur de l'intégration. Un **fournisseur réel** se
teste autrement : voir [Tester un fournisseur réel](#tester-un-fournisseur-réel-google-drive).

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

## Tester l'autorisation OAuth2 d'une destination

Depuis l'issue #7, `tests/destinations_factices.py` fournit aussi `DestinationOAuthEnMemoire`
(fournisseur `factice_oauth`, fixture `fournisseur_oauth_factice`) : elle déclare une
`OAUTH2_SPEC` pointant vers un fournisseur imaginaire du domaine réservé `.test` et demande un
jeton valide avant chaque opération. Les jetons qu'elle a effectivement utilisés sont
mémorisés dans `destination.jetons_utilises`, ce qui permet de vérifier qu'un jeton expiré a
bien été rafraîchi **avant** l'appel.

Le point de jeton du fournisseur est simulé par la fixture `aioclient_mock` de
`pytest-homeassistant-custom-component` : aucun appel réseau n'est fait.

```python
async def test_le_jeton_expire_est_rafraichi(hass, entree_oauth, aioclient_mock):
    aioclient_mock.post(URL_JETON_FACTICE, json=reponse_de_jeton_factice())

    destination = hass.data[DATA_DESTINATIONS].async_get("destination_oauth")
    await destination.async_check_connection()

    assert destination.jetons_utilises == ["acces-factice-2"]
```

Un accès révoqué se simule par une réponse d'erreur : `aioclient_mock.post(URL_JETON_FACTICE,
status=400, json={"error": "invalid_grant"})`. L'opération lève alors `DestinationAuthError`,
la destination est marquée « ré-authentification requise » dans le gestionnaire et un problème
Home Assistant est créé pour elle seule.

Trois règles pour ces tests :

- **Aucune valeur réelle.** Identifiants d'application, codes et jetons sont des chaînes
  reconnaissables (`identifiant-application-factice`, `acces-factice-1`), et les URL pointent
  vers `.test` (RFC 2606). Un test vérifie qu'aucune de ces valeurs n'apparaît dans les
  journaux, même en niveau `debug`.
- **L'instance doit être joignable.** L'URI de redirection dérive de l'URL externe de
  l'instance : `await async_process_ha_core_config(hass, {"external_url": ...})` avant de
  démarrer un flux d'autorisation, sans quoi celui-ci s'interrompt sur `url_indisponible`.
- **Les écritures directes dans `entry.options` doivent porter les options upstream.**
  L'écouteur de mise à jour upstream lit `auto_purge` et `backup_timeout` sans valeur de repli ;
  le code du fork passe pour cela par `options_avec_destinations()`, mais un test qui écrit à la
  main doit les inclure.

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

## Tester un fournisseur réel (Google Drive)

Depuis l'issue #13, l'intégration livre un vrai fournisseur : `google_drive`, enregistré par
`enregistrer_les_fournisseurs()` au démarrage de l'entrée. Deux conséquences pour les tests :

- **le registre n'est jamais vide** dès qu'une entrée est configurée. Un test qui exige un
  registre vide doit neutraliser `list_providers` (voir
  `test_sans_fournisseur_enregistre_l_ajout_est_impossible`), et un test qui liste les
  fournisseurs proposés doit tolérer la présence de Google Drive ;
- **les points d'accès de Google sont simulés**, jamais appelés :
  `aioclient_mock.post(URL_JETON, ...)` pour l'échange et le rafraîchissement du jeton,
  `aioclient_mock.get(URL_ABOUT, ...)` pour l'identification du compte.

```python
async def test_la_verification_de_connexion_interroge_drive(
    hass, entree_google, aioclient_mock
):
    aioclient_mock.get(URL_ABOUT, json=reponse_about())

    await _destination(hass).async_check_connection()

    (methode, url, _, entetes) = _appels(aioclient_mock, URL_ABOUT)[0]
    assert url.query["fields"] == "user"
    assert entetes["Authorization"] == "Bearer acces-google-factice-1"
```

Les règles du fournisseur factice s'appliquent telles quelles, avec une précision : **aucune
valeur réelle**, ni identifiant d'application, ni jeton, ni compte. Le compte de test vit dans
le domaine réservé `.test` (`camille.martin@exemple.test`), et un test vérifie qu'aucune de ces
valeurs — l'adresse du compte comprise, car elle identifie une personne — n'apparaît dans les
journaux en niveau `debug`.

Un échec de l'API Drive se simule par son corps d'erreur habituel, ce qui permet d'éprouver la
qualification des erreurs (`accessNotConfigured` -> API non activée, `storageQuotaExceeded` ->
`DestinationQuotaError`, 401 -> `DestinationAuthError`) :

```python
aioclient_mock.get(
    URL_ABOUT, status=403, json=erreur_google(403, "accessNotConfigured")
)
```

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
entités), le socle des destinations distantes (contrat, registre, persistance), leur
autorisation OAuth2 vue depuis l'interface (ajout, ré-autorisation, suppression) et la
connexion d'un compte Google Drive (issue #13). Le téléversement (#14) et la purge distante
(#15) sont testés par leurs issues respectives, comme le fournisseur Dropbox (#10 à #12).
L'exécution de cette suite en intégration continue est décrite dans [`ci.md`](ci.md).
