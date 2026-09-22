# 0001 — Socle des destinations distantes

- **Statut** : accepté
- **Date** : 2026-09-22
- **Issues** : [#6](https://github.com/ldb2000/auto-backup-extension/issues/6) (socle), [#7](https://github.com/ldb2000/auto-backup-extension/issues/7) (autorisation OAuth2 et interface), [#10](https://github.com/ldb2000/auto-backup-extension/issues/10) (fournisseur Dropbox), [#13](https://github.com/ldb2000/auto-backup-extension/issues/13) (fournisseur Google Drive) — epic [#1](https://github.com/ldb2000/auto-backup-extension/issues/1)

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
n'est connu du reste du code que par le registre. Quatre points méritent d'être tracés.

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
  `auto_backup.upload_successful`, `auto_backup.upload_failed`, `auto_backup.remote_purge`),
  mais leur **émission ne commence qu'à partir de #8** (téléversement) et **#9** (rétention
  distante). Ils sont définis ici pour laisser le schéma de constantes stable et lisible, et
  pour que les issues suivantes n'aient qu'à les émettre sans les déclarer.

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

- **Plancher d'Home Assistant** : le fork annonce **2025.1.0** comme version minimale, mais
  l'absence de sous-entrées de configuration — choix retenu en #6 — a imposé de repousser des
  mécanismes robustes vers le flux d'options. Le plancher sera aligné sur **2026.3** par
  l'issue #28 pour lever cette limitation et migrer vers le modèle standard de Home Assistant.

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
- Rien n'est téléversé à ce stade : `#8` branchera le téléversement sur la création de
  sauvegarde, `#9` la rétention distante.

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
- `#8` (téléversement) et `#9` (purge distante) devront consulter
  `DestinationManager.reauthentification_requise()` avant d'appeler une destination : une
  destination en attente de ré-autorisation échouerait de toute façon.

Ajouts de l'issue #13 :

- Les fournisseurs concrets vivent dans `destinations/providers/` et sont chargés par
  `enregistrer_les_fournisseurs()` au démarrage de l'entrée : le registre n'est donc plus vide en
  fonctionnement, et le flux d'ajout propose « Google Drive » sans rien installer de plus.
- `DestinationConfig` porte `provider_data`, facultatif, borné et masqué dans les journaux.
- Le flux d'ajout interroge le fournisseur juste après l'obtention du jeton, ce qui nomme la
  destination d'après le compte autorisé et fait échouer tôt une configuration inexploitable.
- `#14` (téléversement Google Drive) et `#15` (listage et suppression) n'auront que trois
  méthodes à écrire : le reste du fournisseur est en place.
