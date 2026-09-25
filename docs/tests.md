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
| `tests/test_televersement.py` | Lecture en flux d'une sauvegarde (Supervisor et Core) et téléversement vers les destinations demandées. |
| `tests/test_purge_distante.py` | Rétention distante : âge, nombre, provenance d'une sauvegarde, tolérance aux erreurs et aux appels qui ne reviennent pas, déclenchements (téléversement et service `purge`), registre persistant. |
| `tests/test_provider_dropbox.py` | Fournisseur Dropbox : enregistrement, portées et accès hors-ligne de l'URL d'autorisation, identification du compte, rafraîchissement, révocation, vérification d'accès. |
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

## Tester le téléversement d'une sauvegarde

`tests/test_televersement.py` couvre l'issue #8 à deux niveaux.

**La lecture en flux d'une sauvegarde locale**, pour les deux handlers upstream :

- `SupervisorHandler` : la fixture `aioclient_mock` de
  `pytest-homeassistant-custom-component` simule `GET /backups/<slug>/download`. Elle renvoie un
  vrai `StreamReader`, ce qui permet de vérifier que la sauvegarde arrive en **plusieurs
  morceaux** (le contenu de test dépasse les 64 Kio d'un morceau de lecture) et que la taille
  vient de l'en-tête `Content-Length` ;
- `BackupHandler` : un `BackupManager` en `MagicMock` renvoie une sauvegarde et un agent local
  dont `get_backup_path()` pointe vers un fichier écrit dans `tmp_path`.

**L'orchestration complète**, de l'appel de service aux événements `auto_backup.upload_*`. Le
montage tient dans la fonction `_demarrer()` :

```python
instance = await _demarrer(hass, fichier_de_sauvegarde)  # destination factice chargée
await hass.services.async_call(
    DOMAIN, SERVICE_BACKUP, {"upload_to": "destination_test"}, blocking=True
)
await hass.async_block_till_done(wait_background_tasks=True)
```

Cinq points méritent l'attention en écrivant un nouveau test :

1. **`wait_background_tasks=True` est obligatoire.** Le téléversement s'exécute dans une tâche
   de fond (`entry.async_create_background_task`) ; sans cet argument, `async_block_till_done()`
   rend la main avant la fin du transfert et le test constate un état vide.
2. **La création de sauvegarde est simulée**, en remplaçant `handler.create_backup` par un
   `AsyncMock` qui renvoie `{"slug": ...}`. Le reste du chemin upstream (événements, expiration,
   `download_path`) n'est pas court-circuité pour autant.
3. **Le fournisseur factice consomme réellement le flux** : `octets_recus`, `taille_recue` et
   `taille_annoncee` permettent de vérifier que ce qui est arrivé chez la destination est bien
   le contenu du fichier. `attente_secondes` simule un transfert lent, ce qui éprouve le délai
   maximum `upload_timeout` sans faire patienter la suite de tests.
4. **La corrélation a une fenêtre.** Une demande de téléversement n'est confirmable par
   `auto_backup.backup_start` que tant que l'appel de service qui l'a enregistrée n'est pas
   terminé. Un test qui pilote le coordinateur à la main (`async_enregistrer()`) travaille donc
   sur une demande déjà armée ; dès qu'il appelle `async_release_upload()`, une demande non
   confirmée disparaît sur-le-champ et plus aucun événement ne peut la réclamer. C'est voulu :
   c'est ce qui empêche une sauvegarde homonyme, créée sans `upload_to`, d'être téléversée (voir
   [`adr/0001-destinations-distantes.md`](adr/0001-destinations-distantes.md)).
5. **Le délai maximum se règle par l'interface**, à l'étape `reglages_televersement` du flux
   d'options (fixture `ouvrir_les_options`). Pour prouver que la valeur saisie est bien celle
   qui borne l'envoi, sans attendre la fin d'un délai réel, `asyncio.timeout` est observé le
   temps du téléversement :

   ```python
   with patch(
       "custom_components.auto_backup.destinations.upload.asyncio.timeout",
       wraps=asyncio.timeout,
   ) as chronometre:
       ...
   ```

   `wraps=` garde le comportement réel : seule la valeur reçue est inspectée.

## Tester la purge distante

`tests/test_purge_distante.py` couvre l'issue #9. Le montage est plus léger que celui du
téléversement : les sauvegardes distantes sont **déposées directement** chez le fournisseur
factice par `destination.ajouter_sauvegarde()`, sans créer ni téléverser quoi que ce soit.

```python
# Déposée chez le fournisseur *et* inscrite au registre : purgeable.
await _deposer(hass, destination, "vieille", jours=10)
# Déposée par l'utilisateur, inconnue du registre : intouchable.
await _deposer(hass, destination, "photos", jours=99, inscrire=False)

supprimes = await _coordinateur(hass).async_purger_toutes()
```

Quatre points à connaître :

1. **Le registre décide de ce qui est purgeable.** `_deposer(..., inscrire=False)` simule un
   fichier que l'utilisateur aurait déposé lui-même : il ne figure pas au registre du fork
   (`hass.data[DATA_REMOTE_BACKUPS]`) et ne doit **jamais** être supprimé. La seconde voie, le
   marqueur `auto_backup` posé dans `RemoteBackup.metadata`, se teste avec
   `metadata=marqueur_auto_backup()`.
2. **Le fournisseur factice note les tentatives.** `destination.suppressions` liste les
   identifiants dont la suppression a été *tentée*, dans l'ordre, y compris celles qui ont
   échoué ; `destination.listages` compte les appels à `async_list_backups()`, et un zéro prouve
   qu'une destination n'a jamais été jointe (ré-authentification requise, aucune rétention
   configurée). `destination.erreurs_de_suppression[remote_id] = ...` programme l'échec d'une
   suppression précise, `destination.erreur_a_lever` celui de toutes les opérations.
3. **Le stockage est celui de `hass_storage`.** Le registre est un `Store` Home Assistant
   (`auto_backup.remote_backups`) : son contenu est lisible dans la fixture `hass_storage`, et
   un rechargement de l'entrée (`async_reload`) prouve qu'il survit à un redémarrage.
4. **Le service `purge` fait les deux purges.** Pour prouver que la purge locale upstream n'a pas
   été perdue en route, le test garnit `gestionnaire._snapshots` d'une sauvegarde expirée et
   remplace `gestionnaire._handler.remove_backup` par un `AsyncMock` : l'appel du service doit
   déclencher la suppression locale (et l'événement `auto_backup.purged_backups`) **et** la
   suppression distante.
5. **Un appel qui ne revient pas se simule, il ne s'attend pas.** Les appels réseau de la purge
   sont bornés par `DEFAULT_PURGE_TIMEOUT` (300 s). Pour éprouver ce filet de sécurité, le délai
   est ramené à quelques millisecondes par l'aide `_delai_de_purge()` du fichier de tests, et
   c'est le fournisseur factice qui simule le fournisseur muet — `attente_de_listage` pour le
   listage, `attentes_de_suppression[remote_id]` pour une suppression précise :

   ```python
   destination.attentes_de_suppression["figee"] = 30

   with _delai_de_purge(0.01):
       supprimes = await _coordinateur(hass).async_purger_toutes()
   ```

   L'attente réelle est donc de quelques millisecondes, pas de 300 secondes. Le même principe
   vaut pour le téléversement, où c'est l'option `upload_timeout` qui est réglée à `0.01` et
   `attente_secondes` qui fait patienter.

## Tester un fournisseur réel

Depuis l'issue #10, un fournisseur est livré : Dropbox
([`tests/test_provider_dropbox.py`](../tests/test_provider_dropbox.py)). Il est enregistré
automatiquement au chargement de l'entrée (`async_setup_destinations()` appelle
`enregistrer_les_fournisseurs()`), donc **présent dans le registre dès qu'une instance de
test démarre l'intégration** : un test qui énumère les fournisseurs doit s'y attendre, et
un test qui veut éprouver le cas « aucun fournisseur » doit simuler un registre vide
(`patch` sur `destinations.flow.list_providers`).

Comme pour le fournisseur factice, tout passe par `aioclient_mock` :

```python
async def test_la_verification_d_acces(hass, entree_dropbox, aioclient_mock):
    aioclient_mock.post(URL_COMPTE, json=reponse_de_compte())

    await _destination(hass, entree_dropbox).async_check_connection()
```

Les règles du fournisseur factice s'appliquent telles quelles — aucune valeur réelle,
instance joignable, options upstream complétées — et deux s'y ajoutent :

- **Le compte est une donnée personnelle.** L'identifiant de compte (`account_id`), le nom
  affiché et l'adresse de courriel figurent dans la liste des valeurs qu'un test vérifie
  absentes des journaux en niveau `debug`, au même titre que les jetons.
- **Les codes d'erreur HTTP sont testés un par un** : `401` et `403` doivent lever
  `DestinationAuthError` *et* créer le problème de ré-autorisation ; `429` et `5xx` doivent
  lever `DestinationError` *sans* le créer. Confondre les deux ferait clignoter une demande
  de ré-autorisation à chaque incident passager chez le fournisseur.

## Modifier l'intégration importée

Le répertoire `custom_components/auto_backup/` contient le code importé de l'upstream
(voir [`docs/UPSTREAM.md`](UPSTREAM.md)). **Aucune ligne de ce code ne doit être supprimée ni
modifiée**, sauf lors d'une resynchronisation intentionnelle avec l'upstream. Les tests valident
cette règle (voir la section « Tests réseau » et
[`tests/test_conformite_upstream.py`](../tests/test_conformite_upstream.py)) : les modules
upstream que le fork complète sont comparés à la révision importée, et le test échoue si une
ligne y a disparu ou changé. La seule exception tolérée est la **ré-indentation** d'une ligne
upstream, déclarée dans `REINDENTATIONS_TOLEREES` et citée mot pour mot dans
[`docs/UPSTREAM.md`](UPSTREAM.md) : le contenu de la ligne doit rester identique au caractère
près. Il n'y en a qu'une aujourd'hui.

Le code propre au fork se range dans un sous-paquet dédié de l'intégration — aujourd'hui
`custom_components/auto_backup/destinations/` — et non dans les modules upstream ni hors de
l'intégration : Home Assistant ne charge que ce qui vit sous `custom_components/auto_backup/`.
Ce code est soumis à l'intégralité des règles de lint et au formatage automatique. Tout nouvel
écart doit être ajouté à [`docs/UPSTREAM.md`](UPSTREAM.md) **et** aux listes de
`tests/test_conformite_upstream.py`.

## Périmètre

Ces tests couvrent le comportement upstream importé (configuration, services, options,
entités), le socle des destinations distantes (contrat, registre, persistance), leur
autorisation OAuth2 vue depuis l'interface (ajout, ré-autorisation, suppression) et le
téléversement après création (lecture en flux, événements, échecs, délai maximum) et la
rétention distante (âge, nombre, provenance, tolérance aux erreurs, registre persistant). Les
fournisseurs cloud eux-mêmes (Dropbox, Google Drive) sont testés par leurs issues respectives.
L'exécution de cette suite en intégration continue est décrite dans [`ci.md`](ci.md).
