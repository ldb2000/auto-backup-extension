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
| `tests/test_provider_dropbox.py` | Fournisseur Dropbox : enregistrement, portées et accès hors-ligne de l'URL d'autorisation, identification du compte, rafraîchissement, révocation, vérification d'accès, et refus typé des crochets encore à écrire (#12). |
| `tests/test_provider_dropbox_upload.py` | Dépôt d'une sauvegarde chez Dropbox : envoi simple, session fragmentée, dossier cible, refus traduits en erreurs typées, nouvelles tentatives, garde-fou par requête, sauvegarde distante renvoyée. |
| `tests/test_provider_dropbox_upload_cas_limites.py` | Cas limites du même dépôt : taille annoncée mensongère, deux téléversements successifs, nom ou slug hostile. |
| `tests/test_provider_google_drive.py` | Fournisseur Google Drive : déclaration OAuth2, URL d'autorisation, ajout complet, identification du compte, erreurs, rafraîchissement et révocation. |
| `tests/test_provider_google_drive_upload.py` | Téléversement Google Drive : dossier cible, envoi resumable par fragments, reprises, erreurs typées, journaux et parcours complet depuis le service. |
| `tests/destinations_factices.py` | Fournisseurs de destination factices, en mémoire (aide, pas un module de tests). |
| `tests/test_conformite_upstream.py` | Non-régression de l'import upstream (licence, README, manifeste, écarts documentés ; comparaison réseau). |
| `tests/test_integration_packaging.py` | Validité des fichiers livrés (compilation, JSON, manifeste). |
| `tests/test_project_tooling.py` | Cohérence de l'outillage Python déclaré dans `pyproject.toml`. |
| `tests/test_compatibilite_python.py` | Le code livré reste analysable par le plancher Python des utilisateurs (voir « Compatibilité Python »). |
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
teste autrement : voir [Tester un fournisseur réel](#tester-un-fournisseur-réel).

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

Cinq points à connaître :

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

Deux fournisseurs sont livrés : Dropbox
([`tests/test_provider_dropbox.py`](../tests/test_provider_dropbox.py), issue #10) et Google
Drive ([`tests/test_provider_google_drive.py`](../tests/test_provider_google_drive.py),
issue #13). Tous deux sont enregistrés automatiquement au chargement de l'entrée
(`async_setup_destinations()` appelle `enregistrer_les_fournisseurs()`), donc **présents dans le
registre dès qu'une instance de test démarre l'intégration** : un test qui énumère les
fournisseurs doit s'y attendre, et un test qui veut éprouver le cas « aucun fournisseur » doit
simuler un registre vide (`patch` sur `destinations.flow.list_providers`).

Comme pour le fournisseur factice, tout passe par `aioclient_mock` — `URL_JETON` pour l'échange
et le rafraîchissement du jeton, le point d'accès « compte » du fournisseur pour son
identification (`users/get_current_account` chez Dropbox, `drive/v3/about` chez Google) :

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

Les règles du fournisseur factice s'appliquent telles quelles — aucune valeur réelle, instance
joignable, options upstream complétées — et trois s'y ajoutent :

- **Le compte est une donnée personnelle.** L'identifiant de compte (`account_id`), le nom
  affiché et l'adresse de courriel figurent dans la liste des valeurs qu'un test vérifie
  absentes des journaux en niveau `debug`, au même titre que les jetons. Le compte de test vit
  dans le domaine réservé `.test` (`camille.martin@exemple.test`).
- **Les codes d'erreur HTTP sont testés un par un** : ceux qui n'ont d'issue qu'une nouvelle
  autorisation (`401` chez Google, `401` et `403` chez Dropbox) doivent lever
  `DestinationAuthError` *et* créer le problème de ré-autorisation ; les échecs passagers ou
  corrigeables ailleurs (`403 accessNotConfigured` chez Google, `429` et `5xx`) doivent lever
  une `DestinationError` *sans* le créer. Confondre les deux ferait clignoter une demande de
  ré-autorisation à chaque incident passager chez le fournisseur. Un échec de l'API se simule
  par son corps d'erreur habituel :

  ```python
  aioclient_mock.get(
      URL_ABOUT, status=403, json=erreur_google(403, "accessNotConfigured")
  )
  ```

- **Les crochets du flux d'ajout sont joués sur une seule destination provisoire** : un test du
  parcours complet vérifie qu'un **unique** appel au point « compte » sert le nom proposé et les
  données persistées, et qu'un échec de ce crochet **interrompt** l'ajout (abandon
  `echec_fournisseur`) sans laisser de problème de ré-authentification orphelin.
- **Un crochet encore à écrire qui est appelé en fonctionnement échoue par une erreur typée**, et
  un test l'exige. Le listage Dropbox (#12) est appelé par la purge distante après **chaque**
  sauvegarde dès qu'une rétention est configurée : une `NotImplementedError` tombait dans la
  clause de dernier recours du coordinateur, qui journalisait une trace d'appel complète à chaque
  fois. Le test déroule donc le service `auto_backup.purge` sur une destination Dropbox porteuse
  d'une rétention et vérifie le **journal** : aucun enregistrement porteur d'une trace, et une
  seule ligne d'erreur, qui nomme la destination et renvoie à l'issue.

  ```python
  with caplog.at_level(logging.DEBUG):
      await hass.services.async_call(DOMAIN, SERVICE_PURGE, blocking=True)
      await hass.async_block_till_done()

  assert [enr.message for enr in caplog.records if enr.exc_info] == []
  ```

## Tester le dépôt d'une sauvegarde chez un fournisseur

[`tests/test_provider_dropbox_upload.py`](../tests/test_provider_dropbox_upload.py) (issue #11)
éprouve l'envoi réel d'une sauvegarde, et
[`tests/test_provider_dropbox_upload_cas_limites.py`](../tests/test_provider_dropbox_upload_cas_limites.py)
ses cas limites, en réutilisant ses fixtures plutôt qu'en les dupliquant. Six particularités
s'ajoutent à celles ci-dessus.

**Les seuils sont réduits par `patch`, jamais atteints pour de vrai.** Fabriquer 150 Mo d'octets
pour franchir le seuil de fragmentation coûterait plus cher que ce que le test prouve ; ce sont
les **mécanismes** qui sont vérifiés — nombre d'appels, offsets successifs, validation finale :

```python
with (
    patch(f"{MODULE_DROPBOX}.SEUIL_ENVOI_SIMPLE", fragment),
    patch(f"{MODULE_DROPBOX}.TAILLE_FRAGMENT", fragment),
):
    await televerser(destination, contenu=contenu)
```

Un test distinct garde les **valeurs livrées** sous surveillance (150 Mo, 8 Mio, multiple de
4 Mio) : sans lui, un seuil changé par erreur passerait inaperçu, tous les autres tests le
remplaçant par le leur. Pour que ces `patch` mordent, le code relit la constante au lieu de la
figer en valeur par défaut d'un paramètre.

**Le corps d'une requête se lit dans `aioclient_mock.mock_calls`**, dont chaque entrée est un
quadruplet `(méthode, url, corps, en-têtes)`. Deux pièges :

- un corps vide (la requête `finish`) est ramené à `None` par le simulateur ;
- un corps **en flux** est enregistré tel quel, sans être consommé. Le lire après coup ne
  fonctionne que si sa source est en mémoire ; quand le flux vient d'un vrai fichier refermé à la
  fin du téléversement, il faut le consommer **pendant** la requête, avec un `side_effect` —
  c'est ce que fait `servir_en_consommant()`.

**Le simulateur de `files/upload` doit lire le corps de la requête**, et pas seulement le
mémoriser : le fournisseur compte les octets **réellement** transmis pour vérifier la taille du
dépôt, et un simulateur qui n'itère jamais le flux lui fait voir zéro octet — le test échouerait
sur un code intact, ce qui n'apprend rien à personne. L'aide `simuler_l_envoi()` enregistre donc
un `side_effect` consommant et rend les corps reçus :

```python
simuler_le_dossier(aioclient_mock)
recu = simuler_l_envoi(aioclient_mock)

await televerser(destination)

assert recu == [CONTENU]
```

La session fragmentée n'a pas ce besoin : elle découpe le flux elle-même, ses fragments sont donc
des `bytes` déjà lus quand la requête part.

**Les réponses successives d'une même URL passent par `side_effect`.** Enregistrer deux fois la
même URL sur `aioclient_mock` ne sert que la première ; l'aide `servir()` rend les réponses
l'une après l'autre, la dernière valant pour tous les appels suivants. C'est ce qui permet
d'éprouver « `429` avec `Retry-After`, puis succès » en une seule opération.

**`asyncio.sleep` est neutralisé** par la fixture `sommeil`, qui mémorise les délais demandés :
un test vérifie le délai **observé**, pas le temps écoulé. Les tests qui en ont besoin
l'appellent directement sur la destination, hors de toute tâche de fond.

**Le journal est éprouvé à deux moments** : un téléversement réussi, et un téléversement qui
échoue après plusieurs tentatives. Dans les deux cas, ni `Authorization`, ni `Bearer`, ni aucun
jeton ne doit apparaître, même en `debug`.

## Tester le téléversement vers Google Drive

`tests/test_provider_google_drive_upload.py` (issue #14) va plus loin que la simulation d'un point
d'accès : il embarque un **simulateur du protocole resumable**, `_FauxDrive`, branché sur
`aioclient_mock`. Le simulateur tient l'état réel de la session d'envoi — il refuse un fragment qui
ne commence pas là où le précédent s'est arrêté, répond `308` avec l'en-tête `Range` tant que
l'envoi n'est pas terminé, et ne renvoie les métadonnées du fichier qu'une fois la taille totale
atteinte. Un test qui découpe mal son contenu échoue donc pour la bonne raison.

Trois réglages rendent ces tests rapides et lisibles :

1. **`fragments_courts`** ramène `TAILLE_FRAGMENT` à 512 octets par `monkeypatch` : c'est le
   découpage qui est éprouvé, pas la capacité de la machine à brasser 8 Mio. Un test dédié vérifie
   à part que la valeur réelle est bien un multiple de 256 Kio, comme Google l'exige.
2. **`delais`** remplace `async_attendre_avant_reprise()` par une fonction qui **relève** le délai
   au lieu de l'attendre. Les reprises se testent alors en quelques millisecondes, et la liste
   obtenue (`[1.0, 2.0]`) prouve le caractère exponentiel et borné du calcul :

   ```python
   assert delais == [1.0, 2.0]
   ```

3. **Les anomalies se déclarent à la construction du simulateur**, pas en bricolant les mocks :

   ```python
   faux = _FauxDrive(aioclient_mock, pannes=[(429, {"Retry-After": "5"}, ...)])
   faux = _FauxDrive(aioclient_mock, pannes=[TimeoutError()])  # coupure réseau
   faux = _FauxDrive(aioclient_mock, pertes={512})  # session amnésique
   ```

   Une panne est soit un triplet `(statut, en-têtes, corps)`, soit une exception à lever. Elle
   n'est consommée que par un vrai fragment : une **demande d'état** de la session répond toujours
   l'état réel, sans quoi la reprise ne pourrait pas être vérifiée.

Deux points d'attention en ajoutant un test :

- **l'identifiant du dossier est persisté pendant l'envoi**, ce qui réécrit les options de l'entrée
  et **recrée les instances de destination**. Un test qui veut éprouver la mémoire d'instance garde
  sa référence (`destination = _destination(hass)`), un test qui veut éprouver la mémoire persistée
  redemande la destination au gestionnaire après `await hass.async_block_till_done()` ;
- **l'URL de session est une valeur sensible** au même titre qu'un jeton : elle figure dans la
  liste des chaînes qu'un test vérifie absentes des journaux en niveau `debug`.

Le fichier se termine par deux **parcours complets**, qui suivent les règles de la section
« Tester le téléversement d'une sauvegarde » ci-dessus — `wait_background_tasks=True` compris :

- appel de `auto_backup.backup` avec `upload_to`, création de sauvegarde simulée, puis vérification
  que l'événement `auto_backup.upload_successful` porte l'identifiant distant renvoyé par Drive ;
- le même parcours avec une **rétention configurée** sur la destination, qui déclenche la purge
  distante (#9) après le téléversement. Le listage Google Drive n'existant pas avant #15, cette
  purge échoue nécessairement : le test vérifie que l'échec est journalisé **sans trace d'appel**
  (`"Traceback" not in caplog.text`, et aucun enregistrement porteur d'`exc_info`) et que le
  message renvoie à l'issue. C'est un test de **bruit de journal** : sans lui, remplacer la
  `DestinationError` des deux méthodes différées par une `NotImplementedError` repasserait
  inaperçu, alors qu'il en résulterait une trace d'appel à chaque sauvegarde.

## Compatibilité Python

Le dépôt se développe et se teste sur l'interpréteur exigé par la dernière version de Home
Assistant (`requires-python` dans `pyproject.toml`, aujourd'hui **3.14**). L'intégration est en
revanche **installée** chez des utilisateurs dont le plancher annoncé est celui de `hacs.json` :
**Home Assistant 2026.3, qui exige Python 3.14.2** (plancher relevé de 2025.1.0 par l'issue
#28).

**Règle : tout ce qui vit sous `custom_components/auto_backup/` doit rester analysable par
Python 3.14.** Une syntaxe plus récente ne casse rien en développement, mais lève une
`SyntaxError` au chargement de l'intégration chez ces utilisateurs, avant l'exécution de la
moindre ligne de logique. La règle ne s'applique qu'au code livré : `tests/` et les scripts du
dépôt ne tournent que sur l'interpréteur de développement.

Les deux planchers coïncident depuis #28, mais ils restent indépendants : le dépôt suit la
dernière version de Home Assistant, alors que la version annoncée dans `hacs.json` ne bouge que
sur décision explicite. Ils divergeront de nouveau dès la prochaine version de Python, et la
règle ci-dessus redeviendra contraignante — d'où le maintien du garde-fou.

Le piège rencontré sur l'issue #13 est la PEP 758 : `except A, B:` sans parenthèses, valide à
partir de Python 3.14 seulement. Elle passerait le plancher actuel, mais le dépôt garde la forme
parenthésée, valide sur toutes les versions :

```python
except (ClientError, ValueError, UnicodeDecodeError) as err:
    ...
```

Les parenthèses sont **obligatoires**, et le `as err` doit être réellement utilisé (ici un
journal `debug`) : la cible de `ruff format` est l'interpréteur de développement
(`target-version = "py314"` dans `pyproject.toml`), et le formateur retirerait des parenthèses
qu'il juge superflues sur une clause sans `as`.

Le garde-fou est [`tests/test_compatibilite_python.py`](../tests/test_compatibilite_python.py) :
il analyse chaque module de l'intégration avec `ast.parse(..., feature_version=(3, 14))` et
échoue en nommant le fichier, la ligne et la construction fautive. Deux tests l'accompagnent :
l'un vérifie que le mécanisme refuse bien une syntaxe postérieure au plancher qu'on lui donne
(sans quoi le garde-fou pourrait passer à côté de ce qu'il surveille), l'autre relie le plancher
testé à `hacs.json` — monter la version minimale de Home Assistant annoncée oblige à revoir
`PLANCHER_UTILISATEUR` et cette section plutôt qu'à les laisser diverger en silence.

Ce premier test s'exerce sur Python 3.13, une version *antérieure* au plancher, et non sur le
plancher lui-même : la PEP 758 étant valide en 3.14, plus aucune syntaxe connue ne lui est
postérieure, et CPython borne de toute façon `feature_version` à la version de l'interpréteur
courant. Il démontre donc que le mécanisme mord toujours, sans prétendre que 3.13 soit le
plancher contrôlé.

Limite assumée : `feature_version` est donnée pour « best effort » par CPython et ne couvre pas
l'intégralité des évolutions de syntaxe. Ce garde-fou ne remplace pas une exécution réelle sur
le plancher, mais il est instantané et bloque la régression la plus probable le jour où les deux
planchers divergeront de nouveau.

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
autorisation OAuth2 vue depuis l'interface (ajout, ré-autorisation, suppression), le
téléversement après création (lecture en flux, événements, échecs, délai maximum), la connexion
d'un compte chez les deux fournisseurs livrés — Dropbox (issue #10) et Google Drive (issue #13) —
la rétention distante (âge, nombre, provenance, tolérance aux erreurs, registre persistant) et le
dépôt réel d'une sauvegarde chez Dropbox (issue #11). L'envoi effectif d'un fichier chez Google
Drive (#14), le listage et la suppression chez chaque fournisseur (#12 et #15) sont testés par
leurs issues respectives.

L'exécution de cette suite en intégration continue est décrite dans [`ci.md`](ci.md).
