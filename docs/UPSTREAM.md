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
typées, registre de fournisseurs et gestionnaire de destinations (issue #6). Les fournisseurs
Dropbox et Google Drive viendront s'y greffer sans toucher au code upstream.

Les modules upstream ne sont, eux, **que complétés** : aucune ligne upstream n'est supprimée ni
modifiée, ce qui garde la resynchronisation en simple report de diff.

- `custom_components/auto_backup/const.py` : ajout de l'import de type `DestinationManager` et
  d'un bloc de constantes du fork — `DATA_DESTINATIONS`, `CONF_DESTINATIONS`,
  `CONF_DESTINATION_ID`, `CONF_PROVIDER`, `CONF_FOLDER`, `CONF_RETENTION_DAYS`,
  `CONF_RETENTION_COUNT`, `DEFAULT_DESTINATION_FOLDER`, `EVENT_UPLOAD_START`,
  `EVENT_UPLOAD_SUCCESSFUL`, `EVENT_UPLOAD_FAILED`, `EVENT_REMOTE_PURGE`. Aucune constante
  upstream n'est renommée ni modifiée, et les noms d'événements suivent la convention upstream
  `<domaine>.<événement>`.
- `custom_components/auto_backup/__init__.py` : deux lignes ajoutées — l'import de
  `async_setup_destinations` et son appel dans `async_setup_entry`, qui charge les destinations
  configurées et les expose dans `hass.data[DATA_DESTINATIONS]`. Le nettoyage au déchargement
  passe par `entry.async_on_unload()`, ce qui évite de toucher à `async_unload_entry`.
- `custom_components/auto_backup/config_flow.py` : deux lignes ajoutées — l'import de
  `preserve_destinations` et son appel dans `OptionsFlowHandler.async_step_init`. Le flux
  d'options upstream remplace l'intégralité des options par le contenu de son formulaire, qui
  ignore les destinations : sans ce report, enregistrer les options effacerait les destinations
  configurées.

Le choix de persister les destinations dans `entry.options` plutôt qu'en sous-entrées de
configuration est justifié dans
[`adr/0001-destinations-distantes.md`](adr/0001-destinations-distantes.md).

### Comment ces écarts sont contrôlés

`tests/test_conformite_upstream.py` énumère les écarts et vérifie qu'ils restent bornés :

- les fichiers réécrits (`manifest.json`) sortent de la comparaison ligne à ligne mais sont
  contrôlés par leurs propres tests ;
- les modules upstream étendus (`__init__.py`, `const.py`, `config_flow.py`) sont comparés à
  l'upstream : le test échoue si une ligne upstream y a été supprimée ou modifiée ;
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
