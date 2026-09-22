# Intégration continue

Le workflow [`.github/workflows/ci.yml`](../.github/workflows/ci.yml) s'exécute sur toute
**pull request vers `main`** et sur tout **push sur `main`**. Il ne requiert aucun secret :
seul le `GITHUB_TOKEN` fourni par GitHub est utilisé, en lecture seule
(`permissions: contents: read` au niveau du workflow, aucun job ne l'élargit).

Un `concurrency` groupé par référence (`${{ github.workflow }}-${{ github.ref }}`) annule les
runs devenus obsolètes lorsqu'une PR reçoit un nouveau push. Les runs de `main` ne sont jamais
annulés.

## Jobs

| Job | Ce qu'il vérifie | Équivalent local |
| --- | --- | --- |
| `lint` | `ruff check .` (analyse statique) puis `ruff format --check .` (formatage). | `uv run ruff check . && uv run ruff format --check .` |
| `tests` | La suite `pytest` complète, couverture de `custom_components/auto_backup` affichée. | `uv run pytest` |
| `validate` | `hassfest` (manifeste de l'intégration Home Assistant) puis la validation HACS en catégorie `integration`. | non rejouable en local (voir plus bas) |

Les trois jobs sont indépendants et s'exécutent en parallèle : une erreur de lint, un test en
échec ou une non-conformité HACS rend le workflow rouge, et le motif est lisible dans les logs
de l'étape concernée.

### Environnement Python

Les jobs `lint` et `tests` installent uv avec
[`astral-sh/setup-uv`](https://github.com/astral-sh/setup-uv) et `enable-cache: true` : le cache
GitHub Actions des dépendances uv est indexé sur `pyproject.toml` et `uv.lock`, ce qui évite de
retélécharger l'environnement (Home Assistant et ses dépendances) à chaque run.

- `uv python install` installe la version de Python lue dans `.python-version` (3.14) ;
- `uv sync --group dev --frozen` installe strictement ce que fige `uv.lock`. Le drapeau
  `--frozen` fait échouer la CI si `uv.lock` n'est plus cohérent avec `pyproject.toml`, au lieu
  de résoudre silencieusement d'autres versions que celles testées en local.

### Tests réseau exclus de la CI

Les tests marqués `network` (comparaison de `custom_components/auto_backup/` avec la révision
upstream, voir [`tests.md`](tests.md)) **ne sont pas exécutés en CI** : le job ne passe pas
`--tests-reseau`. Ils s'appuient sur le CLI `gh` authentifié, ce qui n'est pas garanti sur un
runner, et rendraient la CI dépendante de la disponibilité de l'API GitHub. Ils restent à
lancer manuellement (`uv run pytest --tests-reseau`) lors d'une resynchronisation upstream.

## Reproduire la CI en local

```bash
uv sync --group dev --frozen
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

Pour contrôler le workflow lui-même :

```bash
# YAML valide
uv run python -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml'))"
# Linter dédié aux workflows GitHub Actions (facultatif : brew install actionlint)
actionlint .github/workflows/ci.yml
# Garde-fous automatisés sur le workflow (inclus dans `uv run pytest`)
uv run pytest tests/test_ci_workflow.py
```

`hassfest` peut se rejouer localement si Docker est disponible, avec la même image que
l'action :

```bash
docker run --rm -v "$PWD"://github/workspace ghcr.io/home-assistant/hassfest
```

La validation HACS, elle, **n'est pas reproductible en local** : l'action interroge l'API GitHub
pour lire les métadonnées du dépôt (description, sujets, licence, arborescence). Elle ne peut
être constatée que sur un run GitHub Actions.

## Versions des actions et écarts documentés

| Action | Référence épinglée | Remarque |
| --- | --- | --- |
| `actions/checkout` | `v7.0.1` | Tag de version. |
| `astral-sh/setup-uv` | `v10.2.0` | Tag de version. |
| `home-assistant/actions/hassfest` | SHA `58bff37c8947f690ace498be413a9b78d6f30f93` | Le dépôt n'a qu'un tag `1.0.0` (avril 2020), antérieur à la réécriture de l'action et inutilisable. Le SHA épinglé correspond à `master` au 2026-09-10, c'est-à-dire à l'usage recommandé `@master` mais figé. |
| `hacs/action` | `22.5.0` | Dernier tag publié (mai 2022). |

Trois limites à connaître, inhérentes à ces actions :

- `hacs/action` est une action Docker dont le `action.yml` référence l'image
  `ghcr.io/hacs/action:main`, y compris sur les tags : épingler `22.5.0` fige le contrat de
  l'action, pas le code de validation, qui suit toujours `main` côté HACS ;
- `home-assistant/actions/hassfest` exécute l'image `ghcr.io/home-assistant/hassfest` (tag
  flottant) : hassfest suit donc la dernière version de Home Assistant ;
- `hacs/action` lit `hacs.json` et `manifest.json` en brut via `raw.githubusercontent.com`,
  sans authentification : sur un dépôt privé, ces deux requêtes renvoient 404 et les contrôles
  `hacsjson` et `integration_manifest` échouent (« Got None »). Le dépôt doit donc être public
  pour que ces contrôles passent (voir
  [Prérequis côté dépôt GitHub](#prérequis-côté-dépôt-github)).

Pour les deux premières, la conformité HA/HACS peut évoluer sans changement dans ce dépôt —
c'est le comportement voulu, le but étant de rester conforme aux règles en vigueur. La
troisième ne dépend pas du code mais de la visibilité du dépôt.

### Choix de configuration

- `hacs/action` est appelée avec `comment: "false"`. Par défaut, l'action poste un commentaire
  de résultat sur la PR, ce qui exigerait `pull-requests: write` : incompatible avec le critère
  « permissions minimales ». Le résultat reste intégralement lisible dans les logs du job.
- Aucun contrôle HACS n'est neutralisé (`ignore` n'est pas utilisé).

### Prérequis côté dépôt GitHub

La validation HACS ne porte pas que sur les fichiers : elle contrôle aussi les métadonnées du
dépôt, et certains contrôles supposent que celui-ci soit lisible publiquement. Trois prérequis
côté dépôt, dont aucun ne concerne le code :

- **description du dépôt**, pour satisfaire le contrôle `description` de HACS ;
- **sujets / topics**, pour satisfaire le contrôle `topics` de HACS : `home-assistant`, `hacs`,
  `custom-component`, `backup`, `dropbox`, `google-drive` ;
- **dépôt public**, pour satisfaire les contrôles `hacsjson` et `integration_manifest` :
  `hacs/action` ne lit pas ces deux fichiers via l'API GitHub authentifiée, elle les télécharge
  en brut sur `raw.githubusercontent.com` **sans authentification**. Tant que le dépôt est
  privé, ces requêtes renvoient 404 et les deux contrôles échouent (« Got None »). C'est un
  prérequis côté dépôt au même titre que la description et les sujets, pas un défaut du code
  ni du workflow.

Les autres contrôles passent sans aménagement, y compris sur un dépôt privé : `license` (MIT
détectée par GitHub), `archived`, `issues`, `information` (`README.md`) et `brands` (le domaine
`auto_backup` est déjà déclaré dans
[home-assistant/brands](https://github.com/home-assistant/brands)). Le contenu de `hacs.json`
est lui aussi valide tel quel (l'absence de `zip_release` est correcte, cf.
[`UPSTREAM.md`](UPSTREAM.md)) : seul son accès en lecture anonyme manque tant que le dépôt
reste privé.

La protection de la branche `main` (CI obligatoire avant merge) relève du même prérequis de
visibilité : sur un dépôt **privé**, les règles de protection de branche ne sont disponibles
qu'avec un abonnement GitHub Pro ; sur un dépôt **public**, elles le sont sans abonnement.

Aucun fichier de `custom_components/auto_backup/` n'a été modifié pour satisfaire `hassfest` ou
HACS : le code upstream importé passe les deux validations tel quel.

## Hors périmètre

Le workflow de publication (release, archive HACS) et la publication de la documentation ne
sont pas traités ici.
