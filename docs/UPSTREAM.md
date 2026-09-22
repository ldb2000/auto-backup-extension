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

Aucune modification fonctionnelle du code upstream n'a été faite lors de l'import. Les seuls
écarts sont des éléments d'identité du fork :

- `custom_components/auto_backup/manifest.json` : `documentation`, `issue_tracker` et
  `codeowners` pointent vers ce dépôt (`ldb2000/auto-backup-extension`, `@ldb2000`).
  Le domaine reste `auto_backup` et `version` reste `0.0.0`.
- `hacs.json` : propre à ce dépôt, sans publication d'archive (`zip_release`), la chaîne de
  release du fork n'existant pas encore.
- `LICENSE` : licence MIT d'origine, copyright de l'auteur upstream conservé et complété par
  celui du fork.

Toute divergence fonctionnelle ultérieure (destinations Dropbox et Google Drive notamment)
doit être ajoutée à cette liste au fil des évolutions.

## Outillage : pourquoi l'upstream n'est ni reformaté ni linté au même niveau

Le dépôt utilise `ruff` (configuration dans `pyproject.toml`). Le code importé ne respecte ni
le style de formatage de `ruff format`, ni les règles de modernisation (`UP`), de tri des
imports (`I`) ou de simplification (`SIM`, `B`, `RUF`). Le reformater créerait un diff massif
face à l'upstream et rendrait illisible chaque resynchronisation (étape 4 de la procédure
ci-dessous, qui compare fichier par fichier). Le choix retenu est donc de **ne pas modifier le
code importé** et d'adapter la configuration :

- `[tool.ruff.format] exclude` : `custom_components/auto_backup/**` n'est pas reformaté.
- `[tool.ruff.lint.per-file-ignores]` : sur ce même répertoire, seules les règles de
  correction restent actives (`E4`, `E7`, `E9` et `F`, c'est-à-dire pyflakes : imports
  inutilisés, noms indéfinis, etc.). Les familles de style (`E501`, `W`, `I`, `UP`, `B`,
  `SIM`, `RUF`) y sont neutralisées.

Conséquences à connaître :

- `ruff check .` et `ruff format --check .` passent sur le code importé sans l'avoir touché.
- Le code ajouté par le fork **dans ce répertoire** hérite de ces exemptions de style : il
  reste couvert par pyflakes, mais pas par le formatage automatique. Si les ajouts du fork
  deviennent volumineux, préférer un module ou un sous-répertoire dédié qui ne porte pas
  l'exemption plutôt que d'étendre le code upstream sur place.
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
