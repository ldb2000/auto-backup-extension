# 0001 — Socle des destinations distantes

- **Statut** : accepté
- **Date** : 2026-09-22
- **Issues** : [#6](https://github.com/ldb2000/auto-backup-extension/issues/6) (socle),
  [#7](https://github.com/ldb2000/auto-backup-extension/issues/7) (autorisation OAuth2 et
  interface), [#10](https://github.com/ldb2000/auto-backup-extension/issues/10) (fournisseur
  Dropbox), [#8](https://github.com/ldb2000/auto-backup-extension/issues/8) (téléversement
  après création), [#13](https://github.com/ldb2000/auto-backup-extension/issues/13)
  (fournisseur Google Drive) et
  [#11](https://github.com/ldb2000/auto-backup-extension/issues/11) (dépôt d'une sauvegarde
  chez Dropbox) — epic [#1](https://github.com/ldb2000/auto-backup-extension/issues/1)

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
n'est connu du reste du code que par le registre. Six points méritent d'être tracés — auxquels
s'ajoute le dépôt d'une sauvegarde, traité plus bas (issue #11).

### Pas de SDK Dropbox

L'API Dropbox v2 est une API HTTP JSON ; les appels dont ce fork a besoin
(`users/get_current_account`, le dépôt en #11, le listage et la suppression en #12) tiennent en
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
  enfin un libellé lisible (« Dropbox ») au lieu de l'identifiant technique, et les
  formulaires d'identifiants et de nommage le reprennent dans leur placeholder
  `{fournisseur}`. C'est le point ouvert « Libellés de fournisseur » ci-dessous, traité ici
  plutôt qu'en #18.
- `async_nom_par_defaut()` et `async_donnees_du_fournisseur()` : **déclarés sur
  `RemoteDestination`** et appelés une fois le jeton obtenu, sur **une seule** destination
  provisoire construite par `create_destination()`. Un fournisseur qui mémorise la réponse
  du service n'a donc qu'un aller-retour à faire pour les deux.

`DestinationConfig` gagne pour cela `provider_data`, un dictionnaire de scalaires JSON
persisté avec la destination (`{"account_id": "dbid:..."}`). Ce ne sont pas des secrets
d'authentification, mais des identifiants de personne : ils sont masqués par `__repr__()`
et par `as_dict(masquer=True)` au même titre que le jeton.

### Échec d'un crochet : l'ajout s'interrompt

Décision **révisée à la convergence des issues #10 et #13**. L'issue #10 avait d'abord
laissé l'ajout se poursuivre — ne pas perdre une autorisation que l'utilisateur vient
d'accorder — mais l'issue #13 a montré le revers : la cause la plus fréquente d'échec
(l'API Drive non activée) est **permanente**, et poursuivre créait une destination qui
échouerait à chaque sauvegarde, sans rien dire de ce qu'il fallait corriger.

La règle retenue est donc d'**échouer tôt** : la première requête sert de test, et son échec
interrompt l'ajout en citant la cause (`options.abort.echec_fournisseur`, dont le détail est
fourni par le fournisseur, en français). Rien n'est écrit dans les options ; l'utilisateur
corrige et relance l'ajout sans redémarrer Home Assistant. Le coût est une autorisation à
réaccorder, ce qui prend un clic, contre une destination muette qu'il aurait fallu
diagnostiquer.

Le signalement de ré-authentification porté par la destination **provisoire** est effacé dans
tous les cas (`finally`), échec compris : un problème Home Assistant nommant « Autorisation en
cours » désignerait une destination qui n'existe pas, que l'utilisateur ne pourrait ni
ré-autoriser ni supprimer.

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

### Dépôt d'une sauvegarde chez Dropbox (issue #11)

Le fournisseur de l'issue #10 n'écrivait rien. L'issue #11 lui ajoute `async_upload()`, sans
toucher au socle : le contrat de `RemoteDestination` et le coordinateur de l'issue #8 étaient
déjà taillés pour ça. Sept décisions méritent d'être tracées.

#### Deux modes d'envoi, choisis sur la taille annoncée

L'API impose le découpage : `files/upload` refuse un corps de 150 Mo ou plus, et il faut alors
passer par une session (`upload_session/start`, `append_v2`, `finish`). Une sauvegarde Home
Assistant complète dépasse très souvent ce seuil.

| Taille annoncée par le coordinateur | Mode retenu |
| --- | --- |
| Connue et inférieure à 150 Mo | Une requête `files/upload`, corps en flux |
| 150 Mo et plus | Session fragmentée |
| **Inconnue** (`Content-Length` absent côté Supervisor) | Session fragmentée |

La taille inconnue bascule en session **par défaut de preuve** : envoyer en une requête une
sauvegarde dont on ignore la taille, c'est parier qu'elle tient sous le seuil, et découvrir le
contraire après avoir transféré les octets. La session, elle, fonctionne quelle que soit la
taille finale.

La taille annoncée est aussi reprise dans l'en-tête `Content-Length` de l'envoi simple. Sans
elle, `aiohttp` bascule en `Transfer-Encoding: chunked`, que les points d'entrée de contenu de
Dropbox ne garantissent pas.

Elle ne fait pour autant **pas foi** : c'est une annonce, pas une mesure. L'envoi simple la
reprenait comme nombre d'octets envoyés, et la vérification finale comparait alors cette annonce
à elle-même dès que Dropbox était d'accord avec le flux réel — une annonce fausse passait
inaperçue sur cette voie, là où la session, qui découpe le flux elle-même, l'aurait vue. Les deux
voies comptent désormais les octets **réellement** transmis : la session en additionnant ses
fragments, l'envoi simple via un compteur qui enveloppe le flux confié à `aiohttp` (`_Compteur`),
puisque le transport le consomme hors de la vue du fournisseur.

#### Fragments de 8 Mio, un seul en mémoire

Dropbox recommande des fragments multiples de 4 Mio. **8 Mio** est le compromis retenu : deux
fois moins de requêtes qu'à 4 Mio, pour une empreinte mémoire qui reste négligeable devant une
sauvegarde de plusieurs gigaoctets, et très en deçà des 150 Mo qu'un fragment peut atteindre.

Le flux de l'issue #8 arrive par tranches de 64 Kio ; `_fragments()` les accumule dans un
**tampon unique**, vidé dès qu'il atteint la taille d'un fragment. Un seul fragment est donc en
mémoire à la fois, quelle que soit la taille de la sauvegarde — c'est l'exigence de l'issue.

Le premier fragment ouvre la session (`start`), les suivants s'ajoutent (`append_v2`) en
indiquant l'**offset** déjà reçu, et `finish` valide le dépôt avec un corps vide. Cette dernière
requête paraît superflue ; elle ne l'est pas : elle donne un point de validation unique, qui
traite de la même façon une sauvegarde vide, une sauvegarde qui tombe pile sur une frontière de
fragment et le cas courant.

À la fin, deux confrontations ont lieu, sur les deux voies d'envoi indifféremment : les octets
réellement transmis face à la taille **annoncée** par l'appelant quand elle est connue, puis face
à la taille **enregistrée par Dropbox**. La première impute l'écart à l'annonce — sur la voie
simple, cette annonce est le `Content-Length` de la requête, et un corps qui ne la respecte pas
est un dépôt mal cadré, pas une nuance de comptabilité ; la seconde le met sur le compte du
transfert. Les deux lèvent une `DestinationError`, avec un message qui dit laquelle a parlé :
mieux vaut un échec bruyant qu'une archive tronquée que la rétention distante (#9) compterait
comme une sauvegarde valide.

Une taille annoncée **négative** est traitée comme une taille inconnue (`_taille_annoncee()`) :
elle ne renseigne rien, n'a donc rien à faire dans un `Content-Length`, et ne doit pas non plus
être confrontée aux octets envoyés — elle ferait échouer un dépôt intact.

#### Nommage : le nom ne suffit pas, le slug l'accompagne

Le fichier déposé s'appelle `<nom de la sauvegarde> [<slug>].tar`. Le nom seul n'identifie pas
une sauvegarde — sur une installation Core, `generate_backup_name()` renvoie toujours
`Core <version>`, exactement le problème qui a imposé la fenêtre d'armement de l'issue #8. Le
slug, unique, est donc accolé entre crochets, ce qui garde un nom lisible dans l'explorateur
Dropbox tout en désignant une sauvegarde précise.

Le nom est **normalisé en NFKC avant filtrage**, comme le dossier distant (voir « Le dossier
distant n'est pas un texte libre » ci-dessus) : sans cela une barre oblique pleine chasse
(U+FF0F) survivrait au filtrage pour redevenir un séparateur de chemin chez le fournisseur. Les
caractères que Dropbox refuse (`/ \ : ? * < > " |`) et les caractères non imprimables
deviennent `_` ; les espaces sont réduites, les points et espaces de bordure retirés (Dropbox
refuse un nom qui s'y termine) ; et le tout est borné à 200 caractères, la troncature portant
sur la partie libre, jamais sur le slug ni sur `.tar`.

La normalisation ne suffit pourtant pas à tenir cette promesse, et c'est le point que le premier
jet manquait : NFKC ne ramène à `/` et `\` que les **formes de compatibilité** — pleine chasse
U+FF0F et U+FF3C, petite forme U+FE68. Les autres confusables du séparateur la traversent
intacts : barre oblique de division U+2215, barre de fraction U+2044, grand solidus U+29F8,
solidus pointé U+2E4A, solidus très gras U+1F67C, diagonales de filet U+2571 et U+2572, et leurs
symétriques inverses U+2216, U+29F5 et U+29F9. Ils sont donc filtrés **explicitement**
(`SOLIDUS_CONFUSABLES`), au même titre que les caractères que Dropbox refuse.

Rien de tout cela n'était exploitable : le nom forme un segment unique, que le fournisseur ne
découpe pas. Deux raisons d'ajouter la liste malgré tout — une garantie annoncée mais partielle
est une garantie sur laquelle une issue suivante s'appuiera à tort (le listage #12 et la purge
#9 liront ces noms), et un nom visuellement indiscernable de `Sauvegardes/octobre.tar` dans
l'explorateur Dropbox trompe l'utilisateur même sans faille technique. Le dossier distant, lui,
est protégé autrement : il passe par une **liste blanche** (`chemin_de_dossier()`), qui refuse
d'emblée tout ce qui n'est pas alphanumérique, espace ordinaire ou `-_.()`.

La convention `nom_de_fichier_sauvegarde()` de l'issue #8 (`Sauvegarde_du_22.tar`, celle de
`download_path`) n'est pas reprise telle quelle : elle passe par `slugify()`, qui écrase les
espaces et les accents, et ne porte pas le slug. Elle sert de repli quand le nom est vide.

#### Aucun écrasement silencieux : `mode: add` et `autorename: false`

Les trois modes de dépôt de Dropbox ont été pesés :

| Mode | Effet si le nom est déjà pris | Retenu ? |
| --- | --- | --- |
| `overwrite` | Le fichier existant est remplacé | Non : une sauvegarde perdue sans un mot |
| `add` + `autorename: true` | Un second fichier est créé sous un nom voisin | Non : un doublon invisible que la rétention compterait à part |
| `add` + `autorename: false` | Dropbox refuse (`path/conflict/file`) | **Oui** : l'échec est explicite et sans dégât |

`mute: true` complète le trio, pour ne pas notifier l'utilisateur sur tous ses appareils à
chaque sauvegarde.

#### Le dossier cible est créé avant le transfert

Dropbox crée les dossiers manquants au moment du dépôt, mais pas avant. `files/create_folder_v2`
est donc appelé en préalable : le dossier apparaît dès la première sauvegarde, et un problème
(chemin déjà occupé par un fichier, espace saturé) est signalé **avant** d'avoir transféré le
moindre octet. Le conflit `path/conflict/folder` n'est pas une erreur — c'est le cas normal à
partir de la deuxième sauvegarde.

#### Nouvelles tentatives : seulement ce qui est encore en mémoire

Trois tentatives au plus, jamais plus d'une minute d'attente. Le délai demandé par
`Retry-After` prime — c'est lui qui évite d'aggraver une limitation de débit — et à défaut
l'attente double à chaque tentative (1 s, 2 s).

Deux restrictions, toutes deux volontaires :

- **le statut ne suffit pas à décider.** Dropbox signale un espace saturé par un `507`, qui est
  bien un `5xx` sans avoir la moindre chance de s'arranger en une minute. Le motif du corps
  (`insufficient_space`, `conflict`) a donc le dernier mot sur le statut ;
- **une requête dont le corps est un flux n'est pas rejouable.** Le flux d'une sauvegarde ne se
  lit qu'une fois (contrat de l'issue #8) : il n'y a rien à renvoyer. L'envoi simple est donc
  tenté une seule fois, et la sauvegarde suivante repartira de zéro. C'est précisément ce que la
  session corrige pour les grosses sauvegardes, dont chaque fragment est encore en mémoire, et
  dont l'envoi coûte trop cher pour être abandonné sur un incident d'une seconde. Les octets de
  l'envoi simple ne sont pas conservés « au cas où » : garder jusqu'à 150 Mo en mémoire pour le
  seul bénéfice d'un rejeu est un prix hors de proportion sur le matériel qui fait tourner Home
  Assistant.

Le jeton est redemandé à chaque tentative : une attente d'une minute peut suffire à le périmer,
et la session OAuth2 de l'issue #7 le rafraîchit alors d'elle-même.

Reprendre un téléversement interrompu (redémarrage de Home Assistant en plein transfert) reste
hors périmètre, comme l'issue le pose : la session serait à persister, et Dropbox refuse un
`append_v2` à un offset qu'il n'attend pas. Rien n'apparaît dans le dossier tant que `finish`
n'a pas eu lieu : une session inachevée ne laisse donc pas de fichier partiel.

Les délais réseau sont explicites (`ClientTimeout(total=None, connect=30, sock_read=120)`) :
la valeur par défaut d'`aiohttp` est de **cinq minutes au total**, ce qui couperait le dépôt
d'une grosse sauvegarde en plein transfert. Seule l'absence prolongée de données est fatale ;
la durée totale, elle, reste bornée par le `upload_timeout` du coordinateur.

Un dernier garde-fou borne **une seule requête** de transfert, pour le cas où elle ne rendrait
jamais la main. Il n'est pas une constante : il est dérivé de ce même `upload_timeout`, lu dans
les options de l'entrée à chaque dépôt. Figé sur la valeur livrée par défaut, il coupait une
requête au bout de trente minutes alors que l'utilisateur avait relevé son budget global pour
une connexion lente — un réglage qui restait donc sans effet sur la seule requête d'un envoi
simple. Il vaut exactement le budget global, donc lui reste **inférieur ou égal** : c'est
toujours le coordinateur qui tranche le premier, et une requête unique peut utiliser tout le
budget que l'utilisateur lui a accordé.

La lecture de l'option vit en un point unique, `delai_de_televersement()` dans
`destinations/config_entry.py`, dont le coordinateur et les fournisseurs dépendent tous les
deux : deux lectures indépendantes finiraient par diverger sur le repli ou sur la tolérance aux
valeurs hors contrat, et c'est précisément cette divergence qui avait laissé la borne par
requête derrière le réglage.

#### Ce que la sauvegarde distante rapporte à la rétention

`RemoteBackup` est renseignée depuis la réponse de Dropbox, et non depuis ce que le fork croit
avoir envoyé : `remote_id` est l'identifiant opaque (`id:...`), seule clé de suppression,
`path` le `path_display`, `size` et `created_at` la taille et le `server_modified` enregistrés
**par Dropbox**.

`metadata` porte le slug, l'empreinte du contenu (`content_hash`) et un marqueur
`auto_backup: True`. Ce marqueur est ce qui autorisera le listage (#12) et la purge distante
(#9) à ne considérer que les fichiers déposés par l'intégration : un fichier que l'utilisateur
a rangé lui-même dans le dossier ne doit jamais être supprimé par une rétention.

Les journaux enfin : l'en-tête `Authorization` est construit dans une seule fonction et n'est
journalisé nulle part, à aucun niveau. L'argument `Dropbox-API-Arg` porte le chemin distant —
donc le nom du dossier et celui de la sauvegarde : il n'apparaît qu'en `debug`. Les niveaux
supérieurs ne citent que des compteurs, le statut HTTP et le nom de la destination.

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

## Fournisseur Google Drive (issue #13)

Second fournisseur réel, branché sur le même socle et sur les mêmes crochets que Dropbox : il
n'ajoute aucune ligne aux modules upstream et ne touche ni au gestionnaire ni au flux
d'options.

**Où il vit.** Dans `destinations/providers/google_drive.py`, déclaré dans la table de
`providers/fournisseurs_livres()` — une ligne par fournisseur. `enregistrer_les_fournisseurs()`
est appelée par `async_setup_destinations()`, et l'import est différé *dans* la fonction : au
niveau du module, le cycle `config_entry` -> `providers` -> `oauth` -> `config_entry` se
refermerait sur un paquet à moitié initialisé.

**Aucun SDK.** Même raison que pour Dropbox : les appels passent par
`async_get_clientsession(hass)`, aucune dépendance n'est ajoutée au manifeste, et le cœur garde
la main sur le pool de connexions. L'API Drive v3 est une API REST JSON ; le SDK Google,
synchrone et volumineux, n'apporterait rien ici.

**Ce qui est demandé à Google, et pourquoi.** Portée `https://www.googleapis.com/auth/drive.file`
et elle seule : l'application n'accède **qu'aux fichiers qu'elle a créés**, ce qui interdit
structurellement à la purge distante (#15) de toucher aux documents de l'utilisateur, et évite la
procédure de vérification que Google impose aux portées étendues. S'y ajoutent
`access_type=offline` (sans lui, aucun jeton de rafraîchissement), `prompt=consent` (Google ne
livre ce jeton qu'au **premier** consentement d'un couple compte/application : sans cette
demande, reconnecter un compte déjà autorisé donnerait un accès non renouvelable) et
`include_granted_scopes=false` (l'autorisation ne doit pas hériter d'autres portées accordées au
même projet).

**Les crochets du socle, tels que #10 les a posés.** `async_nom_par_defaut()` et
`async_donnees_du_fournisseur()` sont surchargés comme méthodes d'instance et lisent
`drive/v3/about` **une seule fois** : le compte est mémorisé sur l'instance, donc le nom proposé
(« Google Drive – <compte> ») et l'adresse persistée sortent du même aller-retour. L'adresse est
conservée dans `DestinationConfig.provider_data`, champ facultatif, absent des options quand il
n'est pas utilisé, validé deux fois (schéma voluptuous et dataclass), borné en nombre de clés
(20) et en longueur de valeur (500 caractères), et **masqué** par `__repr__()` comme par
`as_dict(masquer=True)` : ce n'est pas un secret — les identifiants d'application et le jeton
ont leurs propres champs — mais une adresse de compte identifie une personne.

**Ré-autorisation.** Les crochets sont rejoués et `provider_data` est rafraîchi : une
destination ré-autorisée sur un autre compte le dit, au lieu de garder l'adresse du précédent.
Seuls les champs d'autorisation et les données du compte changent — la destination est recopiée
par `dataclasses.replace()`, pour que rien d'autre ne puisse être perdu en chemin.

**Un 401 signale la ré-autorisation.** La session ne rafraîchit un jeton que lorsqu'il a expiré.
Si Drive refuse un jeton qui n'a pas expiré, c'est que l'autorisation a été révoquée côté Google
ou que le projet Cloud a changé : le fournisseur signale alors lui-même la destination à
ré-autoriser, comme Dropbox le fait pour 401 et 403, faute de quoi elle resterait muette au lieu
d'être réparable depuis les options. Un `403` ne le fait **pas** : API non activée ou quota
épuisé se corrigent dans la console Google, ré-autoriser n'y changerait rien.

**Erreurs Drive et réaction.**

| Réponse | Erreur levée | Conséquence |
| --- | --- | --- |
| `401` | `DestinationAuthError` | Destination signalée à ré-autoriser, problème Home Assistant créé |
| `403 accessNotConfigured` | `DestinationError` | L'API Drive n'est pas activée : le message nomme la manipulation à faire |
| `403 storageQuotaExceeded`, `quotaExceeded` | `DestinationQuotaError` | Espace épuisé côté compte Google |
| `404` | `DestinationNotFoundError` | La ressource n'existe pas (ou plus) |
| Autre `4xx`/`5xx`, réseau, délai dépassé | `DestinationError` | Échec transitoire : l'autorisation n'est pas remise en cause |

**Contrainte assumée : une URL externe publique.** Google n'accepte que des URI de redirection
HTTPS sur un domaine public. Une instance joignable seulement en `.local`, par adresse IP ou en
HTTP ne peut pas connecter Google Drive — ce n'est pas un défaut du fork, et aucun contournement
n'est possible côté intégration. La procédure complète est dans
[`docs/destinations/google-drive.md`](../destinations/google-drive.md).

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
  n'aient qu'à les émettre sans les déclarer.

- **URI de redirection et prérequis d'URL externe** : l'URI `https://<instance>/auth/auto_backup/callback`
  doit être déclarée chez le fournisseur. **Traité en #10 pour Dropbox et en #13 pour Google
  Drive** : [`docs/destinations/dropbox.md`](../destinations/dropbox.md) et
  [`docs/destinations/google-drive.md`](../destinations/google-drive.md) livrent chacune la
  procédure pas à pas (création de l'application ou du projet, portées, identifiants, URI de
  redirection) et énoncent le prérequis d'URL externe HTTPS. Google refuse en outre les URI non
  publiques (`.local`, adresse IP nue) : une instance sans URL externe publique ne peut pas
  connecter Google Drive. Reste la doc utilisateur d'ensemble (#19).

- **Libellés de fournisseur** : le sélecteur affichait l'identifiant technique (`dropbox`,
  `google_drive`) comme libellé utilisateur. **Traité en #10** : un fournisseur déclare son
  libellé par `RemoteDestination.LABEL`, que `provider_label()` expose ; le sélecteur d'ajout,
  les listes de ré-autorisation et de suppression et les placeholders `{fournisseur}` des
  formulaires l'utilisent. Un fournisseur qui n'en déclare pas — ou qui a été retiré du registre
  — reste affiché sous son identifiant. #13 n'a eu qu'à déclarer « Google Drive ».

- **Stabilité des références lors du rafraîchissement du jeton** : un rafraîchissement de jeton
  réécrit les options de l'entrée et recrée les instances de destination du gestionnaire, ce qui
  invalide toute référence antérieure. **Traité en #8** (téléversement) : le coordinateur conserve
  la référence de destination obtenue au début d'une opération plutôt que de la demander à
  nouveau ; #9 (rétention distante) doit appliquer la même règle.

- **Cohérence de la ré-authentification** : un problème Home Assistant (repair issue) est créé
  pour une destination en attente de ré-autorisation, mais il n'est pas réparable automatiquement.
  **À traiter en #17** (notifications et ré-authentification) : si une notification persistante
  est ajoutée pour les destinations en défaut d'accès, elle ne doit pas doubler le problème
  Home Assistant — la faire disparaître en même temps que le problème, une fois la destination
  ré-autorisée ou supprimée.
  Résidu à traiter : la ré-autorisation d'une destination doit détecter un changement de compte
  (dans `provider_data`) sans réécrire le reste de la configuration ; le code porte
  `provider_data=self._provider_data or config.provider_data` qu'il faudra évaluer pour s'assurer
  que les données du fournisseur sont bien rafraîchies lors de la ré-autorisation sur un autre compte.

- **Convergence des API entre fournisseurs** : **traitée en #13** lors de la fusion avec #10.
  Convention retenue : crochets déclarés comme méthodes d'instance sur `RemoteDestination`
  (`async_nom_par_defaut()`, `async_donnees_du_fournisseur()`), libellé `LABEL` exposé par
  `provider_label()`, table explicite `fournisseurs_livres()` dans `destinations/providers/__init__.py`,
  motif de refus `autorisation_annulee` commun, `provider_data` borné. Politique d'échec d'un
  crochet : l'ajout **s'interrompt** (abandon `echec_fournisseur`), voir la section « Échec d'un
  crochet » ci-dessus.

- **Plancher d'Home Assistant** : le fork annonce **2025.1.0** comme version minimale, mais
  l'absence de sous-entrées de configuration — choix retenu en #6 — a imposé de repousser des
  mécanismes robustes vers le flux d'options. Le plancher sera aligné sur **2026.3** par
  l'issue #28 pour lever cette limitation et migrer vers le modèle standard de Home Assistant.
  **Traité en #28** : le `hacs.json` est mis à jour et le garde-fou `tests/test_compatibilite_python.py`
  doit dériver de ce nouveau plancher, et non plus de 2025.1.0, pour que le plancher d'analyse
  reste aligné avec la version minimale déclarée aux utilisateurs.

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

Ajouts de l'issue #13 :

- Les fournisseurs concrets vivent dans `destinations/providers/` et sont chargés par
  `enregistrer_les_fournisseurs()` au démarrage de l'entrée : le registre n'est donc plus vide en
  fonctionnement, et le flux d'ajout propose « Google Drive » sans rien installer de plus.
- `DestinationConfig` porte `provider_data`, facultatif, borné et masqué dans les journaux.
- Le flux d'ajout interroge le fournisseur juste après l'obtention du jeton, ce qui nomme la
  destination d'après le compte autorisé et fait échouer tôt une configuration inexploitable.
- `#14` (téléversement Google Drive) et `#15` (listage et suppression) n'auront que trois
  méthodes à écrire : le reste du fournisseur est en place.

Ajouts de l'issue #11 :

- Une sauvegarde part réellement chez Dropbox : le socle et le coordinateur de `#8` n'ont pas
  bougé d'une ligne, seul `DropboxDestination.async_upload()` a été écrit — la preuve que le
  contrat de `RemoteDestination` tenait la route pour un fournisseur réel.
- Une sauvegarde distante déposée porte le marqueur `auto_backup` dans ses métadonnées : `#9`
  (purge distante) et `#12` (listage et suppression) s'en serviront pour ne jamais toucher un
  fichier que l'utilisateur a rangé lui-même dans le dossier.
- `#14` héritera des mêmes questions — seuil d'envoi simple, fragmentation, rejeu borné — mais
  pas du même code : les deux API n'ont ni le même protocole d'envoi par morceaux, ni les mêmes
  codes d'erreur. Le jour où une troisième s'ajouterait, une fabrique commune de tentatives
  vaudrait d'être extraite ; à deux fournisseurs, elle coûterait plus qu'elle ne rapporte.
