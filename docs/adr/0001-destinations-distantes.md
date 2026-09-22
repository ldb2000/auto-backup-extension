# 0001 — Socle des destinations distantes

- **Statut** : accepté
- **Date** : 2026-09-22
- **Issues** : [#6](https://github.com/ldb2000/auto-backup-extension/issues/6) (socle) et
  [#8](https://github.com/ldb2000/auto-backup-extension/issues/8) (téléversement après
  création) — epic [#1](https://github.com/ldb2000/auto-backup-extension/issues/1)

## Contexte

Le fork doit permettre d'envoyer les sauvegardes Home Assistant vers Dropbox et Google Drive,
puis d'y appliquer une rétention distante. Les deux fournisseurs partagent le même cycle de vie
(vérifier l'accès, téléverser, lister, supprimer) mais des API très différentes.

L'utilisateur doit pouvoir configurer **plusieurs destinations indépendantes**, chacune avec son
nom, son dossier cible et sa propre rétention, et les retrouver à l'identique après un
redémarrage de Home Assistant. L'intégration upstream, elle, reste mono-entrée : un seul compte
`auto_backup`, configuré par un flux « user » sans champ, et des options (`auto_purge`,
`backup_timeout`) éditées par un flux d'options.

Trois décisions structurantes sont prises ici : où persister la configuration des destinations,
comment un fournisseur s'enregistre, et quelle hiérarchie d'erreurs les fournisseurs remontent.
Elles conditionnent toutes les issues suivantes (#7 à #17), d'où cette trace écrite.

## Décision 1 — persister les destinations dans `entry.options`

### Options étudiées

**A. Sous-entrées de configuration (`ConfigSubentry`, Home Assistant ≥ 2025.3).**
Mécanisme natif conçu exactement pour « plusieurs choses sous une même entrée ». Chaque
sous-entrée a son identifiant, son titre, ses données, son propre flux d'ajout et de
modification, et peut porter ses propres appareils et entités.

- Pour : Modèle officiel, UI d'ajout et de suppression fournie par le cœur.
- Pour : Isolation naturelle des futurs jetons OAuth d'un compte à l'autre.
- Contre : Exige Home Assistant ≥ 2025.3, alors que `hacs.json` annonce **2025.1.0** comme version
  minimale : adopter les sous-entrées relèverait le plancher pour tous les utilisateurs, y
  compris ceux qui n'utilisent aucune destination cloud.
- Contre : Impose de modifier le flux de configuration upstream (`async_get_supported_subentry_types`,
  classes de flux dédiées), donc de diverger davantage d'un code importé à l'identique
  (cf. `docs/UPSTREAM.md`).

**B. Liste de dictionnaires dans `entry.options["destinations"]`.**
Les destinations sont stockées comme une liste sérialisable dans les options de l'entrée, déjà
persistées par Home Assistant dans `.storage/core.config_entries`.

- Pour : Aucun plancher de version supplémentaire : compatible avec le 2025.1.0 annoncé.
- Pour : Deux lignes ajoutées aux modules upstream, sans suppression (cf. `docs/UPSTREAM.md`).
- Pour : Rechargement et écriture triviaux à tester, y compris le redémarrage simulé.
- Contre : L'interface d'ajout et de suppression est à écrire (issue #7) ; les sous-entrées l'auraient
  offerte.
- Contre : Le flux d'options upstream remplace l'intégralité des options par le contenu de son
  formulaire : il faut explicitement reporter les destinations à l'enregistrement.

**C. Store dédié (`homeassistant.helpers.storage.Store`).**
Un fichier `.storage` propre à l'intégration, comme celui déjà utilisé pour l'expiration des
sauvegardes.

- Pour : Liberté totale de format et de versionnage.
- Contre : Configuration invisible depuis l'entrée, non exportée avec elle, et à effacer à la main
  quand l'entrée est supprimée. Une configuration utilisateur n'a pas à vivre hors du
  `ConfigEntry` qui la porte.

### Décision

**Option B.** Les destinations sont persistées dans `entry.options["destinations"]`, sous forme
d'une liste de dictionnaires validés par `DESTINATION_SCHEMA`.

Le plancher de version annoncé aux utilisateurs prime : le fork ne doit pas exclure une
installation qui fonctionne aujourd'hui pour une commodité d'implémentation. S'ajoute le fait que
chaque ligne modifiée dans le code upstream se paie à chaque resynchronisation, et que
l'option B en demande deux fois moins.

Conséquences pratiques :

- l'écriture passe toujours par `async_persist_destinations()`, qui conserve les options
  upstream existantes et **complète** celles qui manquent par leurs valeurs par défaut :
  l'écouteur de mise à jour upstream lit `entry.options["auto_purge"]` sans valeur de repli et
  échouerait sur une entrée n'ayant jamais visité le flux d'options ;
- `preserve_destinations()` est appelé par le flux d'options upstream pour reporter les
  destinations que son formulaire ignore ;
- aucun secret ne transite par cette liste : les jetons d'authentification resteront gérés par
  Home Assistant (issue #7), `DestinationConfig` ne contient que des données de configuration.

### Révision possible

Quand le plancher passera à 2025.3 ou au-delà, migrer vers les sous-entrées redeviendra
pertinent, en particulier pour la gestion des jetons OAuth par compte. La migration consistera à
transformer chaque élément de la liste en sous-entrée dans `async_migrate_entry` : le format
persisté (identifiant stable, fournisseur, nom, dossier, rétentions) est déjà celui qu'une
sous-entrée porterait.

## Décision 2 — un registre de fournisseurs, pas d'import en dur

Le cœur de l'intégration ne connaît aucun fournisseur : il demande une fabrique au registre
(`register_provider` / `provider` / `get_provider` / `list_providers`) à partir de l'identifiant
lu en configuration. Ajouter Dropbox ou Google Drive consiste donc à ajouter un module qui
s'enregistre, sans toucher au gestionnaire ni au flux de configuration. Les tests le démontrent
avec un fournisseur factice entièrement en mémoire, jamais livré.

Un identifiant déjà pris lève `DuplicateProviderError` plutôt que d'écraser silencieusement le
fournisseur existant ; un identifiant inconnu lève `UnknownProviderError`, que le gestionnaire
attrape pour ignorer la destination concernée sans empêcher le démarrage de l'intégration — une
sauvegarde de configuration restaurée sur une installation dépourvue du fournisseur ne doit pas
laisser Home Assistant sans Auto Backup.

## Décision 3 — une hiérarchie d'erreurs typées

Toutes les erreurs dérivent de `DestinationError`, elle-même dérivée de `HomeAssistantError`
afin qu'un appel de service qui en remonte une soit présenté à l'utilisateur sans traitement
particulier.

| Erreur | Signification | Réaction attendue |
| --- | --- | --- |
| `DestinationError` | échec générique du fournisseur | journaliser, notifier (#17) |
| `DestinationAuthError` | jeton expiré ou accès révoqué | déclencher la ré-authentification (#17) |
| `DestinationQuotaError` | quota ou espace de stockage épuisé | alerter sans réessayer |
| `DestinationNotFoundError` | sauvegarde distante absente | ignorer pendant la purge (#9) |
| `DestinationConfigError` | configuration invalide ou incomplète | ignorer la destination, avertir |
| `UnknownProviderError` | fournisseur non enregistré (sous-classe de la précédente) | ignorer la destination |
| `DuplicateProviderError` | deux fournisseurs homonymes | erreur de programmation, remontée telle quelle |

Le découpage suit les **réactions** possibles, pas les codes d'erreur des API : c'est ce qui
permet à `#9` (rétention distante) et `#17` (notifications et ré-authentification) d'être écrites
une seule fois pour tous les fournisseurs. Un fournisseur qui ne sait pas qualifier un échec lève
`DestinationError` : l'appelant reste correct, il perd seulement en finesse de réaction.

## Validation du dossier distant : un chemin relatif POSIX

Le champ `folder` n'est pas un texte libre : les fournisseurs Dropbox (#10) et Google Drive (#13)
le reprendront tel quel pour bâtir le chemin distant d'une sauvegarde. Validé comme simple chaîne
non vide, `folder = "../../etc/passwd"` aurait été accepté, et un téléversement serait sorti du
dossier choisi par l'utilisateur — chez le fournisseur, ou sur toute machine qui synchronise ce
dossier vers un système de fichiers local.

`folder` est donc validé deux fois, comme les rétentions (schéma voluptuous à l'entrée,
`DestinationConfig` à la construction), par `chemin_de_dossier()` — seule implémentation de la
règle, afin que les deux chemins de validation ne puissent pas diverger :

| Refusé | Exemples |
| --- | --- |
| segment de traversée `..` ou `.` | `../x`, `a/../b`, `./a` |
| chemin absolu : `/` en tête ou lettre de lecteur | `/etc/passwd`, `C:/Sauvegardes` |
| séparateur Windows | `a\b`, `\\serveur\partage` |
| segment vide | `a//b`, `a/b/` |
| espace en tête ou en fin de segment | `" Sauvegardes"`, `a/ b` |
| caractère de contrôle ou non imprimable | `Sauvegardes\n`, `Sauvegardes\x00HA` |
| caractère hors liste blanche | `a\u200bb` (U+200B), `Sauvegardes*`, `a:b`, `a?b` |
| longueur excessive | plus de 255 caractères au total, plus de 100 par segment |

Restent acceptés `Sauvegardes`, `Sauvegardes/HA`, les accents et la valeur par défaut
`Home Assistant` : seul `/` sépare les segments, et la valeur renvoyée est le chemin normalisé.

### Normalisation NFKC avant toute vérification

Refuser `..` et `/` ne suffit pas : Unicode offre plusieurs écritures du même caractère, et c'est
la forme **normalisée** qu'un fournisseur ou un système de fichiers finira par interpréter. Un
premier audit de sécurité laissait ainsi passer `"．．"` (U+FF0E deux fois), `"a／..／b"` (U+FF0F)
et `"a/‥"` (U+2025, point de suspension double), dont les formes NFKC valent respectivement `..`,
`a/../b` et `a/..` — c'est-à-dire exactement les traversées que la validation prétendait refuser.

`chemin_de_dossier()` normalise donc la valeur en **NFKC en tout premier**, puis applique
l'intégralité des règles à la chaîne normalisée, et **renvoie cette chaîne normalisée** : ce qui
est validé est exactement ce qui sera écrit dans les options puis transmis au fournisseur. Valider
la forme d'origine et renvoyer autre chose rouvrirait la faille à l'identique.

### Liste blanche de caractères et bornes de longueur

La normalisation seule reste une défense au coup par coup : d'autres confusables existent, et les
caractères invisibles (`Cf`, espaces exotiques) ne se ramènent pas tous à `..` ou à `/`. Le jeu de
caractères est donc une **liste blanche**, pas une liste noire :

- alphanumérique Unicode (`str.isalnum()`), pour que `Sauvegardes/Été` ou un nom non latin
  restent valides — refuser tout ce qui n'est pas ASCII serait hostile aux utilisateurs
  francophones, qui sont le public de ce fork ;
- l'espace ordinaire U+0020, et lui seul, à l'intérieur d'un segment (`Home Assistant`) ;
- la ponctuation sûre `-`, `_`, `.`, `(`, `)`.

Tout le reste est refusé, notamment les catégories Unicode `Cc`, `Cf`, `Zl`, `Zp` et `Zs` (autres
que U+0020), ainsi que `Po`, `Ps`, `Pe`, `Sm` et `So` hors liste blanche : ni `:` ni `*` ni `?`,
dont l'interprétation varie d'un fournisseur et d'un système de fichiers à l'autre.

Enfin le chemin est borné à **255 caractères au total et 100 par segment**. Dropbox et Google
Drive acceptent plus large ; la borne conservatrice évite qu'une valeur démesurée, saisie par
erreur ou par abus, ne se propage dans chaque requête et dans les options persistées.

La règle vit dans le socle et non chez chaque fournisseur : un fournisseur ajouté plus tard hérite
de la protection sans avoir à y penser, et ne reçoit jamais qu'un chemin relatif déjà assaini.

## Téléversement après création (issue #8)

Le socle ci-dessus ne déclenche rien : l'issue #8 branche le téléversement sur la création de
sauvegarde upstream. Trois décisions y sont prises, dans `destinations/upload.py`.

### Corrélation par événement, pas par crochet dans le code upstream

`AutoBackup._async_create_backup()` ne renvoie rien et n'offre aucun point d'extension : y
brancher un appel exigerait de **modifier** des lignes du code importé, ce que le fork
s'interdit (cf. [`../UPSTREAM.md`](../UPSTREAM.md)). L'upstream émet en revanche
`auto_backup.backup_successful` avec le **nom** et le **slug** de la sauvegarde créée.

| Option étudiée | Pourquoi elle n'a pas été retenue |
| --- | --- |
| Appeler le téléversement depuis `_async_create_backup()` | modifie le code upstream, à reporter à chaque resynchronisation |
| Sous-classer `AutoBackup` et surcharger la méthode | dépend d'un détail d'implémentation privé, et l'upstream instancie la classe lui-même |
| Écouter `auto_backup.backup_successful` | **retenue** : n'ajoute rien à l'upstream, et l'événement porte déjà le nom et le slug |

Le gestionnaire de service enregistre donc, **avant** la création, une « demande de
téléversement » ; `CoordinateurTeleversement` écoute l'événement et retrouve la demande.

La clé de corrélation est le **nom de la sauvegarde**. Quand l'appel de service n'en fournit
pas, `async_prepare_upload()` calcule celui que l'upstream aurait généré — en appelant sa
propre méthode `AutoBackup.generate_backup_name()` — et le pose dans les données de l'appel.
`validate_backup_config()` ne le remplace alors plus, puisqu'il ne nomme que les sauvegardes
sans nom : le résultat est identique, mais le nom est connu des deux côtés.

Trois garde-fous complètent la corrélation :

- une demande n'est **honorée qu'après confirmation** : le coordinateur écoute aussi
  `auto_backup.backup_start`, émis par l'upstream juste avant l'appel au Supervisor ou au
  `BackupManager`, et marque la demande homonyme comme confirmée. Seule une demande confirmée
  peut donner lieu à un téléversement ;
- une demande **non confirmée** expire au bout de 30 secondes. C'est le seul cas de fuite
  possible : `validate_backup_config()` peut refuser la configuration avant même d'émettre
  `backup_start` (sauvegarde partielle sur une installation Core, par exemple). Sans cette
  expiration, la demande resterait à attendre indéfiniment, et une sauvegarde **homonyme mais
  sans rapport** créée plus tard l'aurait consommée — donc téléversée à l'insu de
  l'utilisateur. Trente secondes couvrent largement l'écart réel entre l'enregistrement et
  `backup_start` : un aller-retour `get_addons()` au plus, dans le même appel de service ;
- `async_release_upload()`, appelé après la création, oublie une demande que la création n'a
  pas honorée (échec du Supervisor, par exemple).

Limite connue et assumée : deux sauvegardes créées **en parallèle** avec le **même nom
explicite** ne sont pas distinguables ; la première demande enregistrée est consommée par la
première sauvegarde terminée. Le cas suppose deux automatisations simultanées imposant le même
nom, et reste sans incidence sur les sauvegardes locales.

### Téléversement en tâche de fond, avec un délai maximum

Le téléversement est lancé par `entry.async_create_background_task()` : l'appel de service rend
la main dès la sauvegarde créée, et la tâche est annulée si l'entrée est déchargée. Un
téléversement de plusieurs gigaoctets ne bloque donc ni le service, ni Home Assistant.

Les destinations d'une même demande sont traitées **l'une après l'autre** : un flux ne se
consomme qu'une fois, la sauvegarde est donc relue pour chacune. Les traiter en parallèle aurait
imposé soit de garder le contenu en mémoire, soit d'ouvrir autant de lectures simultanées — deux
façons de peser sur une machine qui vient déjà de produire une archive. L'échec de l'une
n'interrompt jamais les suivantes : chaque destination a son propre `auto_backup.upload_failed`,
et la sauvegarde locale n'est jamais touchée.

Le délai maximum est `entry.options["upload_timeout"]`, en secondes, avec **1800 s** par défaut.
Il est relu à chaque téléversement, donc modifiable sans redémarrage. Le formulaire d'options
upstream ne l'expose pas encore : l'y ajouter modifierait le schéma upstream, et l'interface de
configuration des destinations relève de l'issue #7.

### Lecture en flux, jamais en mémoire

Une sauvegarde pèse couramment plusieurs centaines de mégaoctets : elle ne doit être ni chargée
en mémoire, ni recopiée sur le disque avant d'être envoyée. `async_ouvrir_sauvegarde()` renvoie
donc un `ContenuSauvegarde` — un itérateur asynchrone de morceaux de 64 Kio, la taille totale
quand elle est connue, et le nom d'archive — valable le temps d'un contexte :

| Handler upstream | Source lue | Taille |
| --- | --- | --- |
| `SupervisorHandler` | `GET /backups/<slug>/download`, `content.iter_chunked()` | en-tête `Content-Length`, `None` s'il manque |
| `BackupHandler` | fichier de l'agent de sauvegarde local, ouvert par `aiofiles` | `stat().st_size` |

La sélection se fait par `isinstance`, dans le sous-paquet du fork : `handlers.py` reste
identique à l'upstream. Sa méthode `download_backup()` n'était pas réutilisable, puisqu'elle
écrit obligatoirement dans un fichier de destination.

`RemoteDestination.async_upload()` est étendue en conséquence, de façon **rétrocompatible** :
`source` devient facultatif et trois paramètres nommés apparaissent — `stream`, `size` et
`filename`. Un appel de la forme `async_upload(chemin, name=...)` reste valide, mais un
fournisseur doit savoir consommer `stream` : sous Supervisor, la sauvegarde n'existe nulle part
sur le disque de Home Assistant.

Un échec de lecture (Supervisor injoignable, fichier disparu) lève `ErreurLectureSauvegarde`,
distincte de `DestinationError` : l'échec vient d'ici, pas du fournisseur distant.

### Événements émis et validation de `upload_to`

| Événement | Champs |
| --- | --- |
| `auto_backup.upload_start` | `name`, `slug`, `destination`, `destination_name` |
| `auto_backup.upload_successful` | les précédents, plus `size` et `remote_id` |
| `auto_backup.upload_failed` | les champs de `upload_start`, plus `error` |

Une destination inconnue est refusée **avant** la création de la sauvegarde, par une
`ServiceValidationError` en français qui liste les destinations configurées : mieux vaut ne rien
créer que créer une sauvegarde dont l'utilisateur croira, à tort, qu'elle est partie.
`upload_to` accepte un identifiant ou un nom (comparé sans tenir compte de la casse ni des
espaces de bordure) ; un nom porté par plusieurs destinations est refusé plutôt qu'arbitré, en
attendant l'unicité des noms promise par l'issue #7.

Sans `upload_to`, rien de tout cela ne se déclenche : la clé est absente, aucune demande n'est
enregistrée, aucun événement n'est émis, et le déroulement est exactement celui de l'upstream.

## Points ouverts pour les issues suivantes

Cette issue crée le socle ; plusieurs éléments sont volontairement différés :

- **Liste blanche de caractères** : le refus des caractères `' & + ! ,` (apostrophe, ampersand,
  plus, point d'exclamation, virgule) est strict pour cette issue. Une révision ultérieure en #7
  pourra élargir cette liste en fonction des API des fournisseurs, avec un **message d'erreur
  explicite** si la validation rejette un caractère qu'un utilisateur tente d'utiliser.

- **Unicité des noms de destination** : deux destinations homonymes peuvent être configurées ; la
  distinction se fait par identifiant interne. C'est un défaut connu, à traiter en #7 lors de
  l'ajout de l'interface de configuration (formulaire, ajout, édition, suppression), qui devra
  garantir l'unicité à la saisie.

- **Fonction `unregister_provider`** : le registre exporte `unregister_provider` pour faciliter
  les tests (voir `tests/destinations_factices.py`). **Cette fonction est un détail de test et ne
  doit pas être documentée auprès des utilisateurs ni du fork** ; seuls les tests l'utiliseront.

- **Événements** : quatre événements sont définis dans `const.py` (`auto_backup.upload_start`,
  `auto_backup.upload_successful`, `auto_backup.upload_failed`, `auto_backup.remote_purge`).
  Les trois premiers sont **émis depuis #8** (voir la section « Téléversement après création »
  ci-dessus) ; `auto_backup.remote_purge` attend **#9** (rétention distante). Ils sont définis
  ici pour laisser le schéma de constantes stable et lisible, et pour que les issues suivantes
  n'aient qu'à les émettre sans les déclarer.

## Conséquences

- Le code du fork est isolé dans `custom_components/auto_backup/destinations/`, soumis à
  l'intégralité des règles de lint et au formatage automatique, contrairement au code upstream
  importé (cf. `docs/UPSTREAM.md`).
- `const.py` gagne les constantes de configuration et les quatre événements de téléversement et
  de purge distante, nommés selon la convention upstream `<domaine>.<événement>` pour rester
  homogènes dans les automatisations des utilisateurs.
- Les rétentions (`retention_days`, `retention_count`) sont facultatives, indépendantes et
  validées deux fois : par le schéma voluptuous à l'entrée, puis par la dataclass elle-même, de
  sorte qu'aucune destination ne puisse exister avec une rétention nulle ou négative.
- Aucune destination ne peut donc exister avec une rétention nulle ou négative, ni avec un dossier
  capable de désigner autre chose que lui-même : `tests/test_destinations.py` éprouve les valeurs
  refusées et acceptées de `folder` par le schéma, par `from_dict()` et par construction directe.
- Les destinations sont exposées dans `hass.data[DATA_DESTINATIONS]` via un `DestinationManager`
  qui suit les options de l'entrée et disparaît à son déchargement.
- Le téléversement lui-même est branché par `#8` (section « Téléversement après création »
  ci-dessus) ; `#9` y ajoutera la rétention distante.
