# 0001 — Socle des destinations distantes

- **Statut** : accepté
- **Date** : 2026-09-22
- **Issues** : [#6](https://github.com/ldb2000/auto-backup-extension/issues/6) (socle),
  [#7](https://github.com/ldb2000/auto-backup-extension/issues/7) (autorisation OAuth2 et
  interface), [#10](https://github.com/ldb2000/auto-backup-extension/issues/10) (fournisseur
  Dropbox) et [#8](https://github.com/ldb2000/auto-backup-extension/issues/8) (téléversement
  après création) — epic [#1](https://github.com/ldb2000/auto-backup-extension/issues/1)

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
- `preserve_fork_options()` est appelé par le flux d'options upstream pour reporter les clés
  que son formulaire ignore — les destinations, et depuis l'issue #8 toute option du fork
  inscrite dans `CLES_DU_FORK` ;
- les secrets d'autorisation vivent dans cette même liste depuis l'issue #7 : le support de
  persistance retenu étant `entry.options`, il n'existe pas d'autre endroit propre où mettre le
  jeton d'une destination (voir la décision 4 ci-dessous).

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

## Décision 4 — conduire l'autorisation OAuth2 dans le flux d'options (issue #7)

### Contexte

L'utilisateur crée lui-même son application OAuth2 chez le fournisseur (aucun secret n'est livré
dans le code, règle de `CLAUDE.md`), saisit ses identifiants, autorise l'accès dans son
navigateur, puis Home Assistant reçoit un jeton à conserver et à rafraîchir.

Home Assistant offre pour cela `homeassistant.helpers.config_entry_oauth2_flow`. Mais son
`AbstractOAuth2FlowHandler` n'est utilisable que dans un **config flow** : sa vue de retour
standard (`/auth/external/callback`) reprend le flux par
`hass.config_entries.flow.async_configure()`. Or la décision 1 place les destinations dans
`entry.options` : c'est donc un flux d'**options** qui les crée, et lui seul sait les écrire.

### Options étudiées

**A. Une entrée de configuration par destination, avec `AbstractOAuth2FlowHandler`.**
Le mécanisme natif fonctionnerait tel quel, jeton compris.

- Contre : contredit la décision 1, revient sur le support de persistance et impose de modifier
  le flux de configuration upstream (`single_instance` à lever, entrées multiples à gérer).
- Contre : une entrée par destination ferait apparaître autant d'intégrations « Auto Backup »
  dans l'interface, chacune avec ses entités et son appareil de service.

**B. Un config flow secondaire, déclenché par le flux d'options.**
Le flux d'options lancerait un config flow dédié à l'autorisation, puis récupérerait son jeton.

- Contre : deux flux imbriqués, deux boîtes de dialogue, et un aller-retour à inventer entre
  eux ; l'entrée créée par le config flow devrait être supprimée aussitôt.

**C. Conduire l'autorisation dans le flux d'options, avec une vue de retour propre au fork.**
Le flux d'options utilise `async_external_step()` — disponible sur tout `FlowHandler`, flux
d'options compris — et le fork enregistre sa propre vue sur `/auth/auto_backup/callback`, qui
reprend le flux par `hass.config_entries.options.async_configure()`.

- Pour : un seul flux, une seule boîte de dialogue, et les destinations restent où la décision 1
  les a placées.
- Pour : l'échange du code et le rafraîchissement du jeton restent ceux du cœur de Home
  Assistant (`LocalOAuth2Implementation`), y compris la qualification des erreurs
  (`OAuth2TokenRequestReauthError` pour un 4xx, transitoire pour un 5xx).
- Contre : l'URI de redirection à déclarer chez le fournisseur n'est pas celle, habituelle, de
  Home Assistant, et le raccourci My Home Assistant (`https://my.home-assistant.io/redirect/oauth`)
  n'est pas utilisable : il ne sait rediriger que vers `/auth/external/callback`.
- Contre : une instance sans URL externe configurée ne peut pas recevoir le retour. Le flux
  s'interrompt alors avec un message explicite (`options.abort.url_indisponible`) plutôt que de
  fabriquer une URL fausse.

### Décision

**Option C.** L'autorisation est conduite par le flux d'options, l'URL d'autorisation est
construite par une sous-classe de `LocalOAuth2Implementation`, et le retour est reçu par la vue
`RetourAutorisationOAuthView` du fork.

L'**état** (`state`) transmis au fournisseur n'est pas le JWT du cœur — il encode un identifiant
de config flow — mais un aléa de 256 bits (`secrets.token_urlsafe(32)`) associé, côté Home
Assistant, au flux d'options à reprendre et à l'URI de redirection utilisée. Il est à usage
unique et expire au bout de quinze minutes : il sert à la fois de clé de reprise et de jeton
anti-CSRF, et rejouer un retour d'autorisation ne relance rien.

### Où vivent les secrets

`DestinationConfig` porte désormais `client_id`, `client_secret` et `token`, persistés dans
`entry.options["destinations"][i]`, c'est-à-dire dans `.storage/core.config_entries` — le même
fichier que celui où Home Assistant range les identifiants d'application
(`application_credentials`) et les jetons de toutes les intégrations OAuth2. Le support n'est
donc pas moins protégé que l'usage courant du cœur.

Deux garde-fous accompagnent ce choix :

- `DestinationConfig.__repr__()` masque les trois champs, et `as_dict(masquer=True)` produit une
  copie assainie où le jeton est réduit à ses clés. Seul `as_dict()` — sans masquage — est écrit
  dans l'entrée. Un test parcourt l'ajout complet d'une destination avec les journaux en niveau
  `debug` et vérifie qu'aucun secret n'y apparaît.
- Les messages d'erreur portant sur un secret ne citent jamais la valeur reçue, contrairement
  aux autres validateurs du socle.

### Rafraîchissement et ré-autorisation

`DestinationOAuth2Session` est la seule porte d'entrée des fournisseurs vers le jeton :
`async_get_access_token()` rafraîchit le jeton s'il est expiré (marge d'horloge comprise), le
persiste, puis le renvoie. Le jeton est relu dans l'entrée à chaque appel, et non dans la
configuration en mémoire : un rafraîchissement fait foi immédiatement, y compris pour une
destination instanciée avant lui.

Quand le fournisseur refuse le renouvellement (`invalid_grant`, 4xx), ou qu'aucun jeton de
rafraîchissement n'existe, la session lève `DestinationAuthError` et signale la destination :

- le `DestinationManager` la marque « ré-authentification requise » — un marquage par
  destination, que le téléversement (#8) et la purge distante (#9) pourront consulter ;
- un problème (« repair issue ») est créé dans Home Assistant, nommant la destination.

**Le problème est déclaré `is_fixable=False`.** Un problème réparable exige un module
`repairs.py` à la racine de l'intégration, c'est-à-dire un module du fork hors de son
sous-paquet — ce que `tests/test_conformite_upstream.py` interdit — et son `RepairsFlow` ne
pourrait de toute façon qu'ouvrir un nouveau parcours d'autorisation, que les options offrent
déjà. Le problème décrit donc la marche à suivre : options de l'intégration, « Ré-autoriser une
destination », puis la destination nommée. Il disparaît dès que celle-ci est ré-autorisée ou
supprimée.

Ce choix est révisable : si un module `repairs.py` devient acceptable (ou si Home Assistant
autorise un flux de réparation déclaré ailleurs), le problème pourra devenir réparable sans rien
changer au reste.

### Interface

Le flux d'options s'ouvre désormais sur un menu (`async_show_menu`) : ajouter, ré-autoriser ou
supprimer une destination, et les réglages upstream. **Le formulaire upstream n'est pas
modifié** : il reste l'étape `init`, simplement atteinte depuis le menu. Le branchement se fait
en deux lignes ajoutées à la fin de `config_flow.py` (cf. `docs/UPSTREAM.md`).

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

## Fournisseur Dropbox (issue #10)

Premier fournisseur réel. Il ne change rien au socle : il se range dans
`destinations/providers/dropbox.py`, déclare une `OAUTH2_SPEC` et une fabrique, et
n'est connu du reste du code que par le registre. Six points méritent d'être tracés.

### Pas de SDK Dropbox

L'API Dropbox v2 est une API HTTP JSON ; les trois appels dont ce fork a besoin
(`users/get_current_account`, puis le dépôt et le listage en #11 et #12) tiennent en
quelques lignes d'`aiohttp`. Le SDK officiel (`dropbox`) apporterait une dépendance
supplémentaire — et sa propre gestion de jeton, redondante avec celle du socle — à
l'installation de **tous** les utilisateurs de l'intégration, y compris ceux qui
n'utilisent pas Dropbox. Le fournisseur utilise donc la session aiohttp partagée de
Home Assistant (`async_get_clientsession`), et `manifest.json` reste sans
`requirements`.

### Portées demandées, et pourquoi chacune

| Portée | Justification |
| --- | --- |
| `account_info.read` | Identifier le compte à l'autorisation : nom proposé par défaut et vérification d'accès (`async_check_connection`). |
| `files.content.write` | Déposer une sauvegarde (#11) et supprimer celles qui expirent (`files/delete_v2`, #12). |
| `files.metadata.read` | Lister les sauvegardes déjà déposées avec leur date et leur taille : sans elles, la rétention distante supprimerait à l'aveugle (#12). |

`files.content.read` est volontairement absente : aucune opération du périmètre ne relit le
contenu d'une sauvegarde déposée (l'envoi, le listage et la suppression s'en passent), et la
restauration depuis le nuage est hors périmètre de l'epic #1. La demander « au cas où »
contredirait le critère de moindre privilège de l'issue #10 ; elle sera ajoutée avec la
fonctionnalité qui la justifiera, au prix d'une ré-autorisation par l'utilisateur.

Aucune portée de partage, de demande de fichier, de contact ni d'équipe n'est demandée.
`token_access_type=offline` est ajouté à la demande d'autorisation : sans lui Dropbox ne
délivre pas de jeton de rafraîchissement et l'accès expirerait au bout de quatre heures,
ce que la décision 4 (rafraîchissement automatique) suppose acquis.

### « App folder » recommandé plutôt que « Full Dropbox »

La documentation utilisateur ([`docs/destinations/dropbox.md`](../destinations/dropbox.md))
recommande le type d'accès **App folder** : l'application ne voit alors qu'un dossier
`Applications/<nom>` et ne peut rien atteindre d'autre, ce qui borne les conséquences d'une
fuite de la clé et du secret. La conséquence est documentée : les chemins deviennent
relatifs à ce dossier, donc le dossier distant saisi dans Auto Backup y est créé. Le code
n'a rien à faire de particulier — Dropbox opère la translation — et fonctionne aussi bien
avec « Full Dropbox » : le choix reste celui de l'utilisateur, la recommandation est un
conseil de sécurité, pas une contrainte technique.

### Deux crochets facultatifs ajoutés au socle

L'identification du compte a demandé deux ajouts, tous deux **facultatifs et
rétrocompatibles** — un fournisseur qui ne les surcharge pas se comporte exactement comme
avant l'issue #10 :

- `RemoteDestination.LABEL` et `provider_label()` : le sélecteur du flux d'options affiche
  enfin un libellé lisible (« Dropbox ») au lieu de l'identifiant technique. C'est le point
  ouvert « Libellés de fournisseur » ci-dessous, traité ici plutôt qu'en #18.
- `async_nom_par_defaut()` et `async_donnees_du_fournisseur()` : appelés une fois le jeton
  obtenu, sur une destination provisoire, pour pré-remplir le nom et retenir l'identifiant
  de compte. **Leur échec n'interrompt pas l'ajout** : une autorisation que l'utilisateur
  vient d'accorder ne doit pas être perdue parce que l'API a bafouillé ; le formulaire de
  nommage s'ouvre alors sans proposition.

`DestinationConfig` gagne pour cela `provider_data`, un dictionnaire de scalaires JSON
persisté avec la destination (`{"account_id": "dbid:..."}`). Ce ne sont pas des secrets
d'authentification, mais des identifiants de personne : ils sont masqués par `__repr__()`
et par `as_dict(masquer=True)` au même titre que le jeton.

### Erreurs Dropbox et réaction

| Réponse | Erreur levée | Conséquence |
| --- | --- | --- |
| `401` (jeton expiré côté Dropbox, accès révoqué) | `DestinationAuthError` | Destination signalée à ré-autoriser, problème Home Assistant créé |
| `403` (portée manquante, compte désactivé) | `DestinationAuthError` | Idem : seule une nouvelle autorisation, avec les portées cochées, y remédie |
| `429`, `5xx`, réseau, délai dépassé | `DestinationError` | Échec transitoire : l'autorisation n'est pas remise en cause |
| Réponse illisible ou sans `account_id` | `DestinationError` | La réponse d'un service externe n'est jamais tenue pour acquise |

Le corps d'une réponse d'erreur est résumé (`error_summary`) et borné à 200 caractères
avant de figurer dans un message : de quoi diagnostiquer, pas de quoi déverser une réponse
entière dans un journal.

### Message dédié au refus d'autorisation

`access_denied` — le code que renvoient Dropbox comme Google Drive quand l'utilisateur
ferme la page d'autorisation — reçoit son propre message (`options.abort.autorisation_annulee`)
au lieu d'être affiché brut. La table `MOTIFS_DE_REFUS` du flux est ouverte : tout autre
code reste rendu par le message générique, qui le cite.

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

#### Le nom seul ne suffit pas : la fenêtre d'armement

Le premier jet corrélait la demande **au seul nom de la sauvegarde**. C'était insuffisant, et
dangereux : sur une installation Core, `AutoBackup.generate_backup_name()` renvoie toujours
`Core <version>`. Toutes les sauvegardes sans nom explicite sont donc homonymes, et une demande
restée en attente pouvait être consommée par une sauvegarde **sans aucun rapport**, créée sans
`upload_to` — un téléversement non demandé, à l'insu de l'utilisateur. Le chemin était réel :
`validate_backup_config()` refuse `exclude` hors Supervisor **après** l'enregistrement de la
demande et **avant** `auto_backup.backup_start`.

La corrélation retenue tient donc à **deux conditions cumulées**.

1. **Une fenêtre d'armement bornée par l'appel de service.** `async_prepare_upload()` enregistre
   la demande « en cours » (`DemandeTeleversement.en_cours`), et `async_release_upload()` la
   désarme. Le gestionnaire de service l'appelle dans un `finally`, donc y compris quand la
   création lève : une demande jamais confirmée est alors **supprimée sur-le-champ**. Une demande
   ne survit ainsi jamais à l'appel de service qui l'a produite. Ce `finally` est la seule
   entorse du fork au code upstream — une ligne ré-indentée, détaillée dans
   [`../UPSTREAM.md`](../UPSTREAM.md).
2. **Le nom de la sauvegarde**, qui identifie la demande *à l'intérieur* de cette fenêtre. Quand
   l'appel de service n'en fournit pas, `async_prepare_upload()` calcule celui que l'upstream
   aurait généré — en appelant sa propre méthode `AutoBackup.generate_backup_name()` — et le pose
   dans les données de l'appel. `validate_backup_config()` ne le remplace alors plus, puisqu'il
   ne nomme que les sauvegardes sans nom : le résultat est identique, mais le nom est connu des
   deux côtés.

Chaque demande porte en plus un **identifiant unique** (`uuid4`). Il ne sert pas à la
corrélation — aucun événement upstream ne le porte — mais à suivre une demande dans les
journaux, de son enregistrement à son téléversement ou à son abandon, y compris entre demandes
homonymes.

#### Cycle de vie d'une demande

| Étape | Effet sur la demande |
| --- | --- |
| `async_prepare_upload()` | enregistrée, `en_cours`, non confirmée |
| `auto_backup.backup_start` | **confirmée** — seulement si elle est `en_cours` et homonyme |
| `auto_backup.backup_successful` | réclamée si confirmée : le téléversement démarre |
| `auto_backup.backup_failed` | réclamée si confirmée : la création a échoué, la demande est abandonnée |
| `async_release_upload()` (dans le `finally`) | désarmée ; supprimée immédiatement si non confirmée |
| expiration (30 s) | filet : purge une demande non confirmée oubliée là |

Deux conséquences importantes :

- `backup_start` **ne confirme qu'une demande armée**. Une demande déjà désarmée n'est plus
  confirmable : une sauvegarde homonyme lancée après coup ne peut plus la réclamer ;
- `backup_failed` est écouté au même titre que `backup_successful`. Sans lui, une demande
  **confirmée** dont la création échoue resterait en attente indéfiniment — l'expiration ne purge
  pas les demandes confirmées, puisqu'une création légitime peut durer plus longtemps qu'elle —
  et la sauvegarde homonyme suivante l'aurait consommée.

L'expiration de 30 secondes est conservée comme **dernier filet** (coordinateur rechargé, appelant
qui oublierait le `finally`). Elle couvre largement l'écart réel entre l'enregistrement et
`backup_start` : un aller-retour `get_addons()` au plus, dans le même appel de service.

Limite connue et assumée : deux appels **concurrents** portant le **même nom explicite** et
`upload_to` ne sont pas distinguables à l'intérieur de leurs fenêtres d'armement, qui se
chevauchent ; la première demande enregistrée est confirmée par la première sauvegarde démarrée.
Le cas suppose deux automatisations simultanées imposant le même nom, les deux avec `upload_to` ;
le pire effet est une inversion des destinations entre deux sauvegardes, et les sauvegardes
locales ne sont pas touchées. Le nom explicite reste donc le seul mode où la corrélation peut se
tromper — lever cette limite exigerait un identifiant porté par les événements upstream, donc une
modification du code importé.

#### Vigilance de resynchronisation : le nom doit rester celui qui a été demandé

La corrélation suppose que `auto_backup.backup_successful` porte **exactement** le `name` inscrit
dans les données de l'appel de service. C'est vrai de la révision importée : l'upstream reprend le
nom du résultat de la création ou, à défaut, des données de l'appel, et `validate_backup_config()`
ne nomme que les sauvegardes qui n'ont pas de nom — d'où le nom calculé à l'avance par
`async_prepare_upload()`.

Si une version ultérieure de l'upstream **normalisait** ce nom (passage par `slugify()`, troncature,
horodatage ajouté, remplacement par le nom renvoyé par le Supervisor), l'événement ne
correspondrait plus à la demande : celle-ci, déjà confirmée par `backup_start`, ne serait réclamée
ni par `backup_successful` ni par `backup_failed`. Elle resterait en attente — l'expiration ne
purge pas les demandes confirmées — et la sauvegarde ne partirait pas, en silence. Il faut donc,
à chaque resynchronisation, vérifier que les trois événements portent toujours le nom demandé ;
`tests/test_televersement.py` l'éprouve, en particulier pour une sauvegarde sans nom explicite.

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
Il est relu à chaque téléversement, donc modifiable sans redémarrage.

Il se règle depuis l'interface, par l'entrée « Réglages du téléversement » du menu d'options, et
non par un champ ajouté au formulaire upstream (`OPTIONS_SCHEMA`) : ce formulaire reste
l'étape `init`, inchangée, et le réglage du fork vit dans une étape à lui
(`destinations/flow.py`). Le schéma upstream n'est donc pas modifié, conformément à la règle du
fork (cf. [`../UPSTREAM.md`](../UPSTREAM.md)).

| Option étudiée | Pourquoi elle n'a pas été retenue |
| --- | --- |
| Ajouter le champ à `OPTIONS_SCHEMA` | modifie une ligne upstream, à reporter à chaque resynchronisation |
| Laisser l'option non exposée | le critère d'acceptation exige un délai **configurable dans les options** ; l'éditer à la main n'est pas une configuration |
| Une étape propre au fork dans le menu d'options | **retenue** : le formulaire upstream reste intact, et le réglage est accessible sans quitter l'interface |

Une option du fork vivant dans `entry.options`, elle disparaîtrait au premier enregistrement du
formulaire upstream, qui remplace l'intégralité des options par son contenu. `CLES_DU_FORK`
(`const.py`) énumère donc ces clés — `destinations`, `upload_timeout` — et
`preserve_fork_options()` les reporte toutes : inscrire une option future dans cette liste suffit
à la protéger. La saisie est validée en un seul endroit (`_delai_de_televersement()`) : seul un
nombre entier de secondes strictement positif est accepté, un délai nul ou négatif coupant tout
téléversement avant même qu'il commence.

### Lecture en flux, jamais en mémoire

Une sauvegarde pèse couramment plusieurs centaines de mégaoctets : elle ne doit être ni chargée
en mémoire, ni recopiée sur le disque avant d'être envoyée. `async_ouvrir_sauvegarde()` renvoie
donc un `ContenuSauvegarde` — un itérateur asynchrone de morceaux de 64 Kio, la taille totale
quand elle est connue, et le nom d'archive — valable le temps d'un contexte :

| Handler upstream | Source lue | Taille |
| --- | --- | --- |
| `SupervisorHandler` | `GET /backups/<slug>/download`, `content.iter_chunked()` | en-tête `Content-Length`, `None` s'il manque |
| `BackupHandler` | fichier de l'agent de sauvegarde local, ouvert par `aiofiles` | `stat().st_size` |

Le slug est **encodé** (`urllib.parse.quote(slug, safe="")`) avant d'entrer dans l'URL du
Supervisor : il vient d'un événement, donc d'une réponse du Supervisor ou du `BackupManager`, et
aucune valeur inattendue (`/`, `?`, `..`) ne doit pouvoir changer le chemin appelé sur une API
non authentifiée par l'utilisateur.

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

**`size` peut valoir `null`.** Sous Supervisor, la taille vient de l'en-tête `Content-Length` de
`GET /backups/<slug>/download` ; s'il manque, la sauvegarde est téléversée quand même, mais sa
taille reste inconnue, et le fournisseur n'en renvoie pas toujours une non plus. Une automatisation
qui affiche ou additionne `size` doit donc tolérer l'absence de valeur (`{{ trigger.event.data.size
| default(0) }}`, par exemple), et les entités d'état de l'issue #16 ne doivent pas se mettre en
`unavailable` pour autant : « taille inconnue » n'est pas un échec de téléversement.

Une destination inconnue est refusée **avant** la création de la sauvegarde, par une
`ServiceValidationError` en français qui liste les destinations configurées : mieux vaut ne rien
créer que créer une sauvegarde dont l'utilisateur croira, à tort, qu'elle est partie.
`upload_to` accepte un identifiant ou un nom (comparé sans tenir compte de la casse ni des
espaces de bordure) ; un nom porté par plusieurs destinations est refusé plutôt qu'arbitré, en
attendant l'unicité des noms promise par l'issue #7.

Sans `upload_to`, rien de tout cela ne se déclenche : la clé est absente, aucune demande n'est
enregistrée, aucun événement n'est émis, et le déroulement est exactement celui de l'upstream.

## Entités d'état des destinations (issue #16)

L'upstream expose des capteurs sur les sauvegardes **locales**. Sans équivalent distant, un
téléversement qui échoue depuis des semaines passe inaperçu. Chaque destination configurée
reçoit donc trois entités, définies dans `destinations/entities.py` :

| Entité | Domaine | État | Attributs |
| --- | --- | --- | --- |
| `dernier_televersement` | `sensor` (`device_class` TIMESTAMP) | date du dernier téléversement réussi | — |
| `sauvegardes_distantes` | `sensor` (`state_class` MEASUREMENT) | nombre de sauvegardes chez le fournisseur | — |
| `probleme` | `binary_sensor` (`device_class` PROBLEM) | le dernier téléversement a échoué | `last_error`, `last_failed_slug`, `last_failed_at` |

### Brancher les entités sans récrire les plateformes upstream

`sensor.py` et `binary_sensor.py` sont des modules **importés de l'upstream** : le fork ne
supprime ni ne modifie aucune de leurs lignes (cf. `docs/UPSTREAM.md`). Trois lignes leur sont
ajoutées dans chacun — un import et un appel **à la fin** de leur `async_setup_entry()` :

```python
    # Fork (#16) : une entité d'état par destination distante configurée.
    await async_setup_destination_sensors(hass, entry, async_add_entities)
```

Les options écartées :

- **une plateforme dédiée** (`Platform.SENSOR` déclarée une seconde fois) : Home Assistant
  n'accepte qu'une plateforme par domaine et par intégration ;
- **un `sensor.py` du fork qui réexporterait l'upstream** : cela aurait fait sortir
  `sensor.py` de la comparaison ligne à ligne avec l'upstream, alors que l'écart réel se
  résume à deux appels.

### Un état par destination, partagé par ses trois entités

`CoordinateurEntitesDestinations` écoute **une seule fois** `auto_backup.upload_successful`,
`auto_backup.upload_failed` et `auto_backup.remote_purge`, met à jour l'`EtatDestination`
correspondant, puis prévient les entités concernées par un signal de dispatcher
(`auto_backup_destination_maj_<entry_id>_<destination_id>`).

Pourquoi ne pas laisser chaque entité écouter le bus, comme le font les capteurs upstream :
avec trois entités par destination et autant de destinations que l'utilisateur en configure,
le nombre d'abonnements croît vite, et surtout l'état deviendrait *dupliqué* — le capteur
binaire et le capteur d'horodatage doivent réagir de façon **cohérente** au même événement
(un succès efface l'erreur active *et* horodate le succès). Un état commun garantit cette
cohérence ; le dispatcher ne transporte aucune donnée, il ne fait que dire « relis ton état ».

`last_error` porte l'erreur **active** : elle est effacée au premier téléversement réussi, ce
qui fait retomber le capteur « problème ». `last_failed_slug` et `last_failed_at` gardent en
revanche la trace du dernier échec connu, même après un succès : ils répondent à « qu'est-ce
qui avait échoué, et quand », pas à « y a-t-il un problème maintenant ».

### Aucun secret dans un attribut d'entité

Un attribut d'entité est lisible par toute personne ayant accès à l'instance, et l'enregistreur
le conserve dans son historique. Or le message d'erreur d'un fournisseur recopie parfois la
requête refusée, en-tête `Authorization` compris. `assainir_le_message()` masque donc, avant
toute exposition, les valeurs de `Bearer`/`Basic` et celles des clés sensibles
(`access_token`, `refresh_token`, `client_secret`, `token`, `code`...), par la même valeur
constante `***` que `DestinationConfig.as_dict(masquer=True)`, puis borne le message à 255
caractères. Le masquage est volontairement **large** : masquer un code d'erreur HTTP coûte
moins cher que laisser fuir un jeton de rafraîchissement.

### Compteur persisté plutôt qu'inventaire distant

Le nombre de sauvegardes distantes pourrait être obtenu en appelant `async_list_backups()` sur
la destination. Ce serait un appel réseau à chaque rafraîchissement, sur un chemin que rien ne
rend indispensable — et impossible pour une destination en attente de ré-autorisation. Le
capteur tient donc un **compteur** : `+1` sur `upload_successful`, `−n` sur `remote_purge`,
restauré au redémarrage par `RestoreSensor`.

La rétention distante (issue #9) tiendra, elle, un registre persistant des sauvegardes
déposées. Plutôt que d'attendre cette issue — développée en parallèle —,
`destinations/entities.py` expose `async_enregistrer_source_des_comptes(hass, source)` : la
source enregistrée fait autorité quand elle sait répondre, sinon le compteur interne sert de
repli. Aucune des deux issues n'a donc besoin de l'autre pour être livrée, et le branchement
se fera en une ligne.

### Restauration après redémarrage

`RestoreSensor` et `RestoreEntity` suffisent : le dernier succès, le compteur et l'état du
capteur binaire — avec ses trois attributs — sont restitués sans magasin de données propre.
Un `Store` dédié aurait ajouté un fichier de stockage, sa migration et son nettoyage pour une
information que Home Assistant sait déjà conserver.

La valeur restaurée ne s'impose pas : un événement traité entre le démarrage du coordinateur et
l'ajout des entités l'emporte. L'écouteur d'options s'exécute en effet sans attente, donc le
coordinateur connaît une destination — et traite ses événements — avant que Home Assistant
n'ait fini de monter ses entités.

### Suivre l'ajout et la suppression d'une destination

Le coordinateur écoute les options de l'entrée, comme le gestionnaire de destinations :

- **destination ajoutée** : ses entités sont créées par les `async_add_entities` mémorisés à
  la déclaration de chaque plateforme, sans rechargement de l'intégration ;
- **destination supprimée** : ses entités sont retirées du **registre d'entités**, dont Home
  Assistant déduit la suppression de l'entité elle-même — y compris pour une entité
  désactivée, qui n'avait jamais été instanciée. Sans ce retrait, elles resteraient
  indisponibles dans les tableaux de bord de l'utilisateur.

La liste des destinations est relue dans `hass.data[DATA_DESTINATIONS]`, et non dans les
options brutes : le gestionnaire est la seule source de vérité de ce qui est réellement
utilisable, et son écouteur de mise à jour est enregistré par `async_setup_destinations()`,
donc **avant** celui d'une plateforme — il s'est déjà rechargé quand le coordinateur est
prévenu.

## Points ouverts pour les issues suivantes

Cette issue crée le socle ; plusieurs éléments sont volontairement différés :

- **Liste blanche de caractères** : le refus des caractères `' & + ! ,` (apostrophe, ampersand,
  plus, point d'exclamation, virgule) est strict pour cette issue. Une révision ultérieure
  pourra élargir cette liste en fonction des API des fournisseurs. **Traité partiellement en
  #7** : la liste n'a pas été élargie — aucun fournisseur réel n'étant encore implémenté, rien
  ne justifie de la desserrer —, mais le formulaire d'ajout affiche désormais un **message
  d'erreur explicite** (`options.error.dossier_invalide`) qui énumère les caractères admis,
  au lieu de laisser l'utilisateur deviner.

- **Unicité des noms de destination** : deux destinations homonymes peuvent être configurées ; la
  distinction se fait par identifiant interne. **Traité en #7** : le formulaire d'ajout refuse un
  nom déjà porté par une autre destination, comparaison faite sans tenir compte de la casse ni
  des espaces de bordure (`options.error.nom_deja_utilise`). L'unicité est garantie à la saisie
  et non dans `DestinationConfig` : deux destinations homonymes créées avant #7, ou par une
  édition manuelle des options, restent chargées plutôt que d'empêcher le démarrage.

- **Fonction `unregister_provider`** : le registre exporte `unregister_provider` pour faciliter
  les tests (voir `tests/destinations_factices.py`). **Cette fonction est un détail de test et ne
  doit pas être documentée auprès des utilisateurs ni du fork** ; seuls les tests l'utiliseront.

- **Événements** : quatre événements sont définis dans `const.py` (`auto_backup.upload_start`,
  `auto_backup.upload_successful`, `auto_backup.upload_failed`, `auto_backup.remote_purge`).
  Les trois premiers sont **émis depuis #8** (voir la section « Téléversement après création »
  ci-dessus) ; `auto_backup.remote_purge` attend **#9** (rétention distante). Ils sont définis
  ici pour laisser le schéma de constantes stable et lisible, et pour que les issues suivantes
  n'aient qu'à les émettre sans les déclarer. **#16 les consomme** : les entités d'état d'une
  destination se mettent à jour sur `upload_successful`, `upload_failed` et `remote_purge`.
  De cet événement-là, #16 lit deux champs qu'elle a dû nommer avant #9 : `deleted` (nombre de
  sauvegardes supprimées, ou la liste de leurs identifiants) et `remaining` (nombre restant,
  facultatif, qui fait alors autorité sur le compteur). **#9 doit les émettre sous ces noms**,
  aux côtés de `destination` et `destination_name` ; à défaut, le compteur de sauvegardes
  distantes ne redescendra pas après une purge.

- **URI de redirection et prérequis d'URL externe** : l'URI `https://<instance>/auth/auto_backup/callback`
  doit être déclarée chez le fournisseur. **Traité pour Dropbox en #10**
  ([`docs/destinations/dropbox.md`](../destinations/dropbox.md) décrit la déclaration pas à
  pas, le prérequis d'URL externe HTTPS et les messages d'erreur correspondants) ;
  reste **à traiter en #13 (Google Drive)** :
  chaque fournisseur doit livrer une procédure pas à pas claire pour cette déclaration. Google
  Drive refuse les URI non HTTPS et non publiques (`.local`, adresse IP nue), ce qui signifie
  qu'une instance sans URL externe publique ne pourra pas connecter Google Drive. La doc utilisateur
  (#19) devra expliciter ce prérequis au moment de la découverte du fournisseur.

- **Libellés de fournisseur** : le sélecteur affichait l'identifiant technique (`dropbox`,
  `google_drive`) comme libellé utilisateur. **Traité en #10** : un fournisseur déclare son
  libellé par `RemoteDestination.LABEL`, que `provider_label()` expose et que le sélecteur
  utilise ; un fournisseur qui n'en déclare pas reste affiché sous son identifiant. #13 n'a
  qu'à déclarer « Google Drive ».

- **Stabilité des références lors du rafraîchissement du jeton** : un rafraîchissement de jeton
  réécrit les options de l'entrée et recrée les instances de destination du gestionnaire, ce qui
  invalide toute référence antérieure. **À traiter en #8** (téléversement) : conserver la référence
  obtenue au début d'une opération (vers le gestionnaire, vers une destination), plutôt que de
  la demander à nouveau, pour garantir que le reste de l'opération utilise les données stables
  de son début.

- **Cohérence de la ré-authentification** : un problème Home Assistant (repair issue) est créé
  pour une destination en attente de ré-autorisation, mais il n'est pas réparable automatiquement.
  **À traiter en #17** (notifications et ré-authentification) : si une notification persistante
  est ajoutée pour les destinations en défaut d'accès, elle ne doit pas doubler le problème
  Home Assistant — la faire disparaître en même temps que le problème, une fois la destination
  ré-autorisée ou supprimée.

- **Convergence des API entre fournisseurs lors de la fusion de #13 (Google Drive)** : l'issue
  #10 (Dropbox) a ajouté plusieurs crochets facultatifs à la classe de base `RemoteDestination`
  (`LABEL`, `async_nom_par_defaut()`, `async_donnees_du_fournisseur()`) et une fonction utilitaire
  `provider_label()`. Une fois Google Drive (#13) fusionnée, **vérifier la stabilité** de ces
  interfaces sur les deux implémentations et, si des divergences émergent :
  - Consacrer une table explicite dans `destinations/providers/__init__.py` pour que le sélecteur
    du flux d'options bascule sur un mécanisme plus robuste qu'une méthode `provider_label()`
    importée du registre.
  - Uniformiser le message de refus d'autorisation (`options.abort.autorisation_annulea`) et la
    politique d'échec gracieux des crochets (actuellement : l'ajout ne s'interrompt pas si
    `async_nom_par_defaut()` ou `async_donnees_du_fournisseur()` lèvent une exception).

- **Renommage d'une destination (issue #16)** : le nom d'une destination est figé dans le nom
  d'origine de ses entités au moment où le registre les enregistre. Le flux d'options ne sait
  pas renommer une destination aujourd'hui ; le jour où il le saura, il devra mettre à jour
  l'`original_name` des entités concernées dans le registre, faute de quoi elles continueront
  d'afficher l'ancien nom. Le contournement actuel — supprimer puis recréer la destination —
  fonctionne mais fait repartir ses compteurs de zéro.

- **Plancher d'Home Assistant** : le fork annonce **2025.1.0** comme version minimale, mais
  l'absence de sous-entrées de configuration — choix retenu en #6 — a imposé de repousser des
  mécanismes robustes vers le flux d'options. Le plancher sera aligné sur **2026.3** par
  l'issue #28 pour lever cette limitation et migrer vers le modèle standard de Home Assistant.

- **Limitation d'une corrélation par nom en présence d'appels concurrents (FAQ, issue #19)** :
  deux appels **concurrents** portant le **même nom explicite** et `upload_to` ne sont pas
  distinguables à l'intérieur de leurs fenêtres d'armement, qui se chevauchent ; la première
  demande enregistrée est confirmée par la première sauvegarde démarrée. Le cas suppose deux
  automatisations simultanées imposant le même nom, les deux avec `upload_to` ; le pire effet
  est une inversion des destinations entre deux sauvegardes, et les sauvegardes locales ne sont
  pas touchées. La FAQ #19 documenta la marche à suivre pour éviter ce scénario (noms explicites
  différents, ou une seule automation avec `upload_to`).

- **Traduction des champs de `services.yaml` (issue #18)** : le champ `upload_to` ajouté aux
  trois services de sauvegarde reste libellé en anglais dans `services.yaml`, comme tout le reste
  du fichier upstream (source de vérité des libellés par défaut). Les traductions françaises
  vivent dans `translations/fr.json`, qui n'a pas encore de section `services` au moment de #8 ;
  la traduction du champ y sera ajoutée en #18 pour que la formulation côté utilisateur soit
  cohérente.

- **Destination à ré-autoriser lors d'une purge (issue #9)** : une destination en attente de
  ré-authentification (décision 4 de cet ADR) ne doit pas être contactée lors d'une opération de
  purge distante (issue #9). Le gestionnaire expose `reauthentification_requise(destination_id)`
  pour le vérifier ; l'issue #9 l'appelera avant d'appeler `async_list_backups()` et
  `async_delete_backup()`, exactement comme #8 le fait pour le téléversement (voir la section
  « Téléversement après création » ci-dessus).

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

Ajouts de l'issue #16 :

- Chaque destination configurée expose deux capteurs et un capteur binaire, rattachés au device
  de service upstream et nommés avec le nom de la destination par un `translation_key` à
  marqueur (`{destination}`).
- `sensor.py` et `binary_sensor.py` ne gagnent qu'un import et un appel chacun : toute la
  logique vit dans `destinations/entities.py`.
- Le nombre de sauvegardes distantes est un compteur persisté, que la rétention distante (#9)
  pourra remplacer par son registre en appelant `async_enregistrer_source_des_comptes()`.
- Les messages d'erreur exposés en attribut sont masqués et bornés par
  `assainir_le_message()` : aucun jeton ne peut atteindre l'historique d'états.

Ajouts de l'issue #7 :

- `DestinationConfig` porte `client_id`, `client_secret` et `token`, facultatifs, validés par le
  schéma comme par la dataclass, et masqués par `__repr__()` et `as_dict(masquer=True)`. Une
  destination qui ne les utilise pas est persistée exactement comme avant : les clés absentes ne
  sont pas ajoutées.
- Un fournisseur déclare son usage d'OAuth2 par `RemoteDestination.OAUTH2_SPEC` et obtient un
  jeton valide par `async_session_de_la_destination()`. Il n'a ni URL de retour, ni état, ni
  rafraîchissement à écrire.
- Le fork expose une route HTTP supplémentaire, `/auth/auto_backup/callback`, enregistrée
  seulement lorsqu'une autorisation démarre : une installation sans destination OAuth2 n'ouvre
  aucune route nouvelle.
- Le flux d'options upstream devient une étape d'un menu, sans qu'aucune de ses lignes change.
- `#8` (téléversement) consulte `DestinationManager.reauthentification_requise()` avant
  d'appeler une destination : une destination en attente de ré-autorisation échouerait de toute
  façon, et l'échec est signalé sans aucun appel réseau. `#9` (purge distante) devra faire de
  même.
