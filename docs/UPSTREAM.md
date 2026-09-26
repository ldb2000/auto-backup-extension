# Suivi de l'upstream

Ce dépôt est un fork de [jcwillox/hass-auto-backup](https://github.com/jcwillox/hass-auto-backup)
(licence MIT). Le répertoire `custom_components/auto_backup/` provient d'une révision upstream
figée, copiée à l'identique, puis étendue par ce fork.

## Révision importée

| Élément | Valeur |
| --- | --- |
| Dépôt upstream | `jcwillox/hass-auto-backup` (branche `main`) |
| SHA importé | `809295d2737b9613cf6d5f5d15a53ae5a861658d` |
| Date du commit upstream | 2026-05-11 |
| Date de l'import dans ce fork | 2026-09-22 |
| Licence | MIT — voir `LICENSE` |

Lien direct vers la révision :
<https://github.com/jcwillox/hass-auto-backup/tree/809295d2737b9613cf6d5f5d15a53ae5a861658d>

## Écarts volontaires par rapport à l'upstream

### Identité du fork

- `custom_components/auto_backup/manifest.json` : `documentation`, `issue_tracker` et
  `codeowners` pointent vers ce dépôt (`ldb2000/auto-backup-extension`, `@ldb2000`).
  Le domaine reste `auto_backup` et `version` reste `0.0.0`.
- `hacs.json` : propre à ce dépôt, sans publication d'archive (`zip_release`), la chaîne de
  release du fork n'existant pas encore.
- `LICENSE` : licence MIT d'origine, copyright de l'auteur upstream conservé et complété par
  celui du fork.

### Extensions fonctionnelles du fork

Le code propre au fork vit dans le sous-paquet `custom_components/auto_backup/destinations/`,
absent de l'upstream : contrat commun des destinations distantes, types de données, erreurs
typées, registre de fournisseurs et gestionnaire de destinations (issue #6), puis autorisation
OAuth2 (`oauth.py`), signalement des destinations à ré-autoriser (`reauth.py`) et étapes
d'interface du flux d'options (`flow.py`, issue #7), l'orchestration du téléversement
après création (`destinations/upload.py`, issue #8), enfin la rétention et la purge distantes
(`destinations/retention.py`, issue #9). Les fournisseurs réellement livrés vivent
dans le sous-paquet `destinations/providers/` — Dropbox depuis l'issue #10, Google Drive depuis
l'issue #13 — et sont enregistrés en un point unique, `enregistrer_les_fournisseurs()`, appelé
par `async_setup_destinations()` : aucun code upstream n'est touché pour ajouter un fournisseur.

La purge distante (#9) suit la même règle : elle vit entièrement dans
`destinations/retention.py`, s'ajoute au service `auto_backup.purge` par une ré-inscription faite
dans `__init__.py` (voir plus bas) et ne touche pas à la rétention locale de `manager.py`. Elle
écrit en revanche un **second fichier de stockage**, `.storage/auto_backup.remote_backups`
(registre des sauvegardes déposées chez les fournisseurs), à côté de
`.storage/auto_backup.snapshots_expiry` que l'upstream gère seul : les deux clés sont distinctes
et le fork ne lit ni n'écrit celle de l'upstream.

`handlers.py` et `manager.py` ne sont, eux, **pas modifiés du tout** : `destinations/upload.py`
lit une sauvegarde en flux en s'appuyant sur `isinstance(handler, SupervisorHandler |
BackupHandler)` et sur des attributs privés de l'upstream. Leur méthode `download_backup()`
écrit obligatoirement dans un fichier, ce que le téléversement veut justement éviter.

Ce sont les points de couplage à revérifier lors d'une resynchronisation :

| Attribut ou méthode privée | Porté par | Ce que le fork en fait |
| --- | --- | --- |
| `AutoBackup._handler` | `manager.py` | retrouver le handler choisi au démarrage, pour lire la sauvegarde |
| `SupervisorHandler._session`, `._ip`, `._headers` | `handlers.py` | appeler `GET /backups/<slug>/download` en streaming |
| `BackupHandler._manager` | `handlers.py` | atteindre l'agent de sauvegarde local et son fichier |
| `async_service_handler` (fonction locale d'`async_setup_entry`) | `__init__.py` | la passer à `async_setup_remote_purge()`, qui ré-inscrit `auto_backup.purge` en l'enveloppant (#9) |

`AutoBackup.generate_backup_name()` est également appelée, mais c'est une méthode **publique**.
Si l'upstream renomme l'un de ces attributs, `tests/test_televersement.py` échoue
immédiatement.

Les modules upstream ne sont, eux, **que complétés** : aucune ligne upstream n'est supprimée ni
modifiée, ce qui garde la resynchronisation en simple report de diff. Une seule exception, décrite
ci-dessous : une ligne d'`__init__.py` est **ré-indentée**, son contenu restant identique au
caractère près.

- `custom_components/auto_backup/const.py` : ajout de l'import de type `DestinationManager` et
  d'un bloc de constantes du fork — `DATA_DESTINATIONS`, `CONF_DESTINATIONS`,
  `CONF_DESTINATION_ID`, `CONF_PROVIDER`, `CONF_FOLDER`, `CONF_RETENTION_DAYS`,
  `CONF_RETENTION_COUNT`, `DEFAULT_DESTINATION_FOLDER`, `EVENT_UPLOAD_START`,
  `EVENT_UPLOAD_SUCCESSFUL`, `EVENT_UPLOAD_FAILED`, `EVENT_REMOTE_PURGE`. Le téléversement
  (#8) ajoute à la suite l'import de type `CoordinateurTeleversement`, la clé `DATA_UPLOADS`,
  l'option de service `ATTR_UPLOAD_TO`, les champs d'événement `ATTR_DESTINATION`,
  `ATTR_DESTINATION_NAME`, `ATTR_SIZE`, `ATTR_REMOTE_ID`, `CONF_UPLOAD_TIMEOUT` et
  `DEFAULT_UPLOAD_TIMEOUT`, enfin `CLES_DU_FORK` — la liste des options d'entrée propres au
  fork, décrite plus bas. Viennent ensuite les constantes d'autorisation OAuth2 de l'issue #7
  (`OAUTH_CALLBACK_PATH`, `DATA_OAUTH_STATES`, `DATA_OAUTH_VIEW`, `OAUTH_STATE_TTL`,
  `OAUTH_AUTHORIZE_URL_TIMEOUT`, `OAUTH_TOKEN_TIMEOUT`, `ISSUE_REAUTH_PREFIX`) — dont
  `IDENTIFIANT_PROVISOIRE`, partagé par le flux d'ajout et le signalement de
  ré-authentification —, puis `CONF_PROVIDER_DATA` (issues #10 et #13) et, à la fin du bloc, les
  constantes de la rétention distante (issue #9) : l'import de type des deux classes de
  `destinations/retention.py`, `STORAGE_KEY_REMOTE_BACKUPS`, `STORAGE_VERSION_REMOTE_BACKUPS`,
  `DATA_REMOTE_BACKUPS`, `DATA_REMOTE_PURGE`, `ATTR_CREATED_AT`, `ATTR_REMOTE_IDS` et
  `DEFAULT_PURGE_TIMEOUT` — ce dernier borne les appels réseau de la purge et n'est
  **pas** inscrit dans `CLES_DU_FORK` : ce n'est pas une option d'entrée, rien ne le persiste.
  L'ordre du fichier est donc : #6, #8, #7, #10/#13, puis #9.
  Aucune constante upstream n'est renommée ni modifiée, et les noms d'événements suivent la
  convention upstream `<domaine>.<événement>`.
- `custom_components/auto_backup/__init__.py` : deux lignes ajoutées par #6 — l'import de
  `async_setup_destinations` et son appel dans `async_setup_entry`, qui charge les destinations
  configurées et les expose dans `hass.data[DATA_DESTINATIONS]`. Le nettoyage au déchargement
  passe par `entry.async_on_unload()`, ce qui évite de toucher à `async_unload_entry`.
  L'issue #8 y ajoute : l'import d'`ATTR_UPLOAD_TO` et celui des trois fonctions de
  `destinations/upload.py` ; la clé `upload_to` de `SCHEMA_BACKUP_BASE`, donc des trois
  services de sauvegarde à la fois ; l'appel `async_setup_upload(hass, entry)` ; et, dans le
  gestionnaire de service, `async_prepare_upload()` **avant** la création de la sauvegarde puis
  `async_release_upload()` dans un `finally`. L'issue #9 y ajoute deux blocs, l'un et l'autre en
  ajout pur : l'import d'`async_setup_remote_purge()` et son appel **après** la boucle
  d'inscription des services, qui ré-inscrit `auto_backup.purge` avec un gestionnaire enveloppant
  celui de l'upstream (purge locale d'abord, purge distante ensuite). La boucle upstream et
  `async_unload_entry()` restent intacts : le service est retiré par son nom, comme les trois
  autres. Tout cela est ajouté, à une ré-indentation près, décrite juste en dessous.
- `custom_components/auto_backup/services.yaml` : un champ `upload_to` ajouté aux services
  `backup`, `backup_full` et `backup_partial` (défini une fois avec l'ancre YAML `&upload_to`,
  référencé deux fois), à la fin de la liste des champs de chacun. Aucun champ upstream n'est
  touché. Son libellé et sa description restent **en anglais**, comme tout le reste du fichier
  upstream : ce fichier est la source de vérité des libellés par défaut de l'interface, et les
  traductions françaises vivent dans `translations/fr.json`, qui n'a pas encore de section
  `services`. Traduire ce seul champ rendrait le formulaire bilingue pour tout le monde.
- `custom_components/auto_backup/config_flow.py` : deux lignes ajoutées par l'issue #6 —
  l'import de `preserve_fork_options()` et son appel dans `OptionsFlowHandler.async_step_init`.
  Le flux d'options upstream remplace l'intégralité des options par le contenu de son
  formulaire, qui ignore les options du fork : sans ce report, enregistrer les réglages de
  sauvegarde effacerait les destinations configurées et le délai de téléversement. Ces deux
  lignes sont propres au fork : l'issue #8 les a récrites pour passer de
  `preserve_destinations()`, qui ne reportait que les destinations, à `preserve_fork_options()`,
  qui reporte **toutes** les clés de `CLES_DU_FORK` — une option ajoutée demain est donc
  protégée du seul fait d'y être inscrite, sans nouvelle retouche ici. L'issue #7 ajoute deux
  lignes **à la fin du fichier** :
  l'import de `etendre_le_flux_d_options()` et la réaffectation
  `OptionsFlowHandler = etendre_le_flux_d_options(OptionsFlowHandler)`. La méthode upstream
  `ConfigFlow.async_get_options_flow()` résout ce nom au moment de l'appel : lui substituer une
  sous-classe suffit à ajouter le menu et les étapes d'ajout, de ré-autorisation et de
  suppression d'une destination, sans modifier ni la classe upstream ni sa méthode. Le
  formulaire upstream reste l'étape `init`, désormais atteinte depuis le menu.
- `custom_components/auto_backup/translations/fr.json` et
  `custom_components/auto_backup/translations/en.json` : clés ajoutées par l'issue #7 —
  `issues.reauthentification_requise` et, sous `options`, les sections `abort`, `error` et les
  étapes `menu`, `ajouter_destination`, `identifiants`, `autorisation`, `destination`,
  `reautoriser_destination`, `supprimer_destination`, plus un `title` pour l'étape `init`.
  L'issue #8 y ajoute l'étape `reglages_televersement`, son entrée de menu et l'erreur
  `options.error.delai_invalide`. Les issues #10 et #13 y ajoutent deux abandons, **à la fin du
  bloc du fork** : `options.abort.autorisation_annulee` (l'utilisateur a refusé ou fermé l'écran
  d'autorisation) et `options.abort.echec_fournisseur`, où le fournisseur explique en français
  ce qui a échoué à sa première requête (API Drive non activée, par exemple).
  Toutes les clés upstream sont conservées telles quelles, et les ajouts sont insérés **avant**
  les clés existantes : leurs virgules de fin de ligne ne changent pas, donc aucune ligne
  upstream n'est modifiée. Les autres langues livrées par l'upstream (`cs`, `de`, `pt_PT`,
  `sk`, `ur`) ne sont pas touchées : Home Assistant retombe sur l'anglais pour les clés
  absentes, et leur traduction relève de l'issue #17.
- Le fork enregistre une vue HTTP propre, `/auth/auto_backup/callback`, au moment où une
  autorisation OAuth2 démarre. Elle est nécessaire parce que la vue standard
  (`/auth/external/callback`) ne sait reprendre qu'un *config flow*, alors que les destinations
  sont éditées par un flux d'**options** (cf. l'ADR).

Le choix de persister les destinations dans `entry.options` plutôt qu'en sous-entrées de
configuration est justifié dans
[`adr/0001-destinations-distantes.md`](adr/0001-destinations-distantes.md), tout comme la
corrélation par événement retenue pour le téléversement.

#### Seule ligne upstream ré-indentée : l'appel de création dans `__init__.py`

Dans `async_service_handler`, la ligne upstream

```python
            await auto_backup.async_create_backup(data)
```

est **décalée de quatre espaces** pour entrer dans le `try` du fork :

```python
            demande_televersement = async_prepare_upload(hass, data)
            try:
                await auto_backup.async_create_backup(data)
            finally:
                async_release_upload(hass, demande_televersement)
```

Son contenu est inchangé ; seule son indentation l'est. C'est le seul endroit du fork où une
ligne upstream n'est pas reprise telle quelle, et c'est assumé :

- **pourquoi c'est nécessaire** : une demande de téléversement enregistrée avant la création ne
  doit jamais survivre à l'appel de service qui l'a créée. Or `async_create_backup()` lève avant
  d'émettre `auto_backup.backup_start` quand `validate_backup_config()` refuse la configuration
  (une sauvegarde partielle sur une installation Core, par exemple). Sans `finally`,
  `async_release_upload()` était sauté, la demande restait en attente, et la sauvegarde suivante
  portant le même nom — sur Core, `generate_backup_name()` renvoie toujours `Core <version>`,
  donc *n'importe quelle* sauvegarde sans nom explicite — la réclamait : un téléversement non
  demandé. Le détail du mécanisme est dans
  [`adr/0001-destinations-distantes.md`](adr/0001-destinations-distantes.md) ;
- **pourquoi pas autrement** : déplacer l'appel dans une fonction du fork appelée à sa place
  aurait **supprimé** la ligne upstream, ce qui est pire qu'un décalage d'indentation ; laisser
  la libération au seul filet de l'expiration (30 s) aurait gardé une fenêtre de récupération
  bien réelle ;
- **coût en resynchronisation** : si l'upstream modifie cette ligne, le report doit être refait à
  la main dans le `try`. Le diff reste lisible (`diff -w` l'ignore même complètement) et
  `tests/test_conformite_upstream.py` en fait un cas nommé, pas une exemption silencieuse.

#### Service `auto_backup.purge` : enveloppé, jamais réécrit

La purge distante (#9) devait s'exécuter à chaque appel du service upstream. Deux branchements
ont été écartés :

- **modifier `manager.py`** (`AutoBackup.purge_backups()`) : c'est du code upstream, que le fork
  ne touche pas ;
- **écouter `auto_backup.purged_backups`** : l'upstream n'émet cet événement que si une
  sauvegarde locale a **réellement** été supprimée. Sur une installation qui n'utilise pas
  `keep_days`, appeler le service n'aurait alors purgé aucune destination distante.

Le fork **ré-inscrit** donc le service, après la boucle upstream, avec un gestionnaire qui
appelle d'abord `async_service_handler` (la fonction upstream, passée en paramètre) puis la purge
distante. Home Assistant remplace une inscription de service par la dernière reçue ; la purge
locale garde donc exactement son comportement, ses conditions et ses journaux.

Deux points de vigilance à la resynchronisation :

- si l'upstream **renomme** `async_service_handler` ou change sa signature, la ligne d'appel du
  fork ne compile plus : l'erreur est immédiate, jamais silencieuse ;
- si l'upstream déplace l'inscription des services **après** la ligne du fork, sa version
  reprendrait la main et la purge distante ne s'exécuterait plus. `tests/test_purge_distante.py`
  le détecte (`test_le_service_purge_purge_le_local_et_le_distant`).

#### Option `upload_timeout` : une étape du fork, pas un champ du formulaire upstream

Le délai maximum d'un téléversement est lu dans `entry.options[CONF_UPLOAD_TIMEOUT]`, avec
`DEFAULT_UPLOAD_TIMEOUT` (1800 s) comme valeur de repli. Il se règle depuis l'interface, par
l'entrée de menu « Réglages du téléversement » (étape `reglages_televersement` de
`destinations/flow.py`), et **non** par un champ ajouté au formulaire upstream : `OPTIONS_SCHEMA`
et l'étape `init` de `config_flow.py` restent intacts, ce qui garde la resynchronisation en
simple report de diff. Le coordinateur relit l'option à chaque téléversement : elle s'applique
sans redémarrage.

Deux conséquences, valables pour toute option que le fork ajoutera :

- l'écriture passe par `options_avec_reglage()` (`destinations/config_entry.py`), qui complète
  au passage les options upstream manquantes — l'écouteur de mise à jour upstream lit
  `entry.options["auto_purge"]` et `["backup_timeout"]` **sans valeur de repli** et lèverait sur
  une entrée qui n'a jamais visité son formulaire ;
- la clé doit figurer dans `CLES_DU_FORK` (`const.py`), sans quoi le premier enregistrement du
  formulaire upstream l'effacerait en silence — c'est exactement ce qui arrivait à
  `upload_timeout` avant l'issue #8.

### Comment ces écarts sont contrôlés

`tests/test_conformite_upstream.py` énumère les écarts et vérifie qu'ils restent bornés :

- les fichiers réécrits (`manifest.json`) sortent de la comparaison ligne à ligne mais sont
  contrôlés par leurs propres tests ;
- les fichiers upstream étendus (`__init__.py`, `const.py`, `config_flow.py`, `services.yaml`,
  `translations/fr.json`, `translations/en.json`) sont comparés à l'upstream : le test échoue
  si une ligne upstream y a été supprimée ou modifiée ;
- les seules divergences tolérées dans ces fichiers sont les ré-indentations énumérées dans
  `REINDENTATIONS_TOLEREES` (aujourd'hui la seule ligne ci-dessus) : la ligne doit se retrouver
  telle quelle dans le fichier du fork, au décalage d'indentation près, et être citée mot pour
  mot dans cette page — `test_les_reindentations_tolerees_sont_justifiees` le vérifie ;
- le reste du répertoire doit rester identique à la révision importée ;
- aucun module du fork ne doit apparaître à la racine de `custom_components/auto_backup/` ;
- chaque écart doit être listé dans cette page.

Toute divergence fonctionnelle ultérieure doit donc être ajoutée ici **et** aux listes de
`tests/test_conformite_upstream.py`.

## Outillage : pourquoi l'upstream n'est ni reformaté ni linté au même niveau

Le dépôt utilise `ruff` (configuration dans `pyproject.toml`). Le code importé ne respecte ni
le style de formatage de `ruff format`, ni les règles de modernisation (`UP`), de tri des
imports (`I`) ou de simplification (`SIM`, `B`, `RUF`). Le reformater créerait un diff massif
face à l'upstream et rendrait illisible chaque resynchronisation (étape 4 de la procédure
ci-dessous, qui compare fichier par fichier). Le choix retenu est donc de **ne pas modifier le
code importé** et d'adapter la configuration :

- `[tool.ruff.format] exclude` : les modules upstream ne sont pas reformatés.
- `[tool.ruff.lint.per-file-ignores]` : sur ces mêmes modules, seules les règles de
  correction restent actives (`E4`, `E7`, `E9` et `F`, c'est-à-dire pyflakes : imports
  inutilisés, noms indéfinis, etc.). Les familles de style (`E501`, `W`, `I`, `UP`, `B`,
  `SIM`, `RUF`) y sont neutralisées.

Les deux listes énumèrent les modules upstream **un par un**, au lieu d'un motif du type
`custom_components/auto_backup/*.py` : dans les motifs de `ruff`, `*` traverse les séparateurs
de chemin, et un tel motif exempterait aussi le code du fork rangé dans les sous-paquets.
`tests/test_project_tooling.py` vérifie que ces listes correspondent exactement aux modules
upstream livrés : ajouter un module upstream lors d'une resynchronisation oblige à compléter
`pyproject.toml`.

Conséquences à connaître :

- `ruff check .` et `ruff format --check .` passent sur le code importé sans l'avoir touché.
- Le code du fork (`custom_components/auto_backup/destinations/` et tout futur sous-paquet)
  est soumis à **l'intégralité** des règles et au formatage automatique : c'est du code neuf,
  il n'a pas à hériter des exemptions de l'upstream. Un ajout du fork se range donc dans un
  sous-paquet dédié plutôt que dans un module upstream.
- `tests/` reste soumis à l'intégralité des règles et au formatage.

## Procédure de resynchronisation

1. Relever le SHA upstream visé :

   ```bash
   gh api repos/jcwillox/hass-auto-backup/commits/main --jq .sha
   ```

2. Récupérer l'arborescence de cette révision dans un répertoire temporaire :

   ```bash
   SHA=<sha-visé>
   gh api "repos/jcwillox/hass-auto-backup/tarball/$SHA" > /tmp/upstream.tar.gz
   mkdir -p /tmp/upstream && tar -xzf /tmp/upstream.tar.gz -C /tmp/upstream
   ```

3. Comparer l'upstream visé avec l'upstream actuellement importé pour connaître l'ampleur des
   changements :

   ```bash
   # <sha-importé> = SHA du tableau ci-dessus
   gh api "repos/jcwillox/hass-auto-backup/compare/<sha-importé>...$SHA" \
     --jq '.files[] | "\(.status)\t\(.filename)"'
   ```

4. Comparer le résultat avec le code du fork, fichier par fichier, et reporter manuellement les
   changements upstream sans écraser les ajouts du fork :

   ```bash
   diff -ru /tmp/upstream/jcwillox-hass-auto-backup-*/custom_components/auto_backup \
            custom_components/auto_backup
   ```

5. Vérifier que le fork compile toujours, que le lint passe et que sa suite de tests passe :

   ```bash
   uv sync --group dev
   uv run python -m compileall custom_components/auto_backup
   uv run ruff check . && uv run ruff format --check .
   uv run pytest
   ```

6. Mettre à jour le tableau « Révision importée » ci-dessus (SHA, dates) ainsi que la liste des
   écarts volontaires, puis commiter la resynchronisation dans une branche dédiée.

> La resynchronisation se fait par report manuel des diffs, jamais par écrasement du répertoire
> `custom_components/auto_backup/` : celui-ci contient aussi les ajouts propres au fork.
