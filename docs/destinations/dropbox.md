# Connecter un compte Dropbox

Auto Backup n'est pas une application Dropbox publique : **vous créez votre propre
application Dropbox**, et vous seul en détenez la clé et le secret. Aucun identifiant n'est
livré dans ce dépôt, et rien ne transite par un serveur tiers — votre Home Assistant parle
directement à Dropbox.

Comptez cinq minutes. La procédure est à faire une seule fois par compte Dropbox.

## Avant de commencer

| Prérequis | Pourquoi |
| --- | --- |
| Une **URL externe HTTPS** configurée dans Home Assistant (ou, pour un test sur la machine même de Home Assistant, une URL locale `http://localhost:8123`) | Dropbox doit pouvoir vous renvoyer vers votre instance après l'autorisation. |
| Un compte Dropbox personnel | Dropbox Business et les espaces d'équipe ne sont pas pris en charge. |

L'URL externe se règle dans **Paramètres → Système → Réseau → URL Internet**. Elle doit être
en `https://` : Dropbox refuse une URI de redirection en clair (`http://`), à la seule
exception de `http://localhost`.

### Adresse de retour utilisée

Auto Backup construit l'adresse de retour (`…/auth/auto_backup/callback`) à partir de la
première de ces adresses qui convient :

1. l'**URL Internet** (URL externe), ou l'adresse Home Assistant Cloud ;
2. à défaut, l'**URL de réseau local** (URL interne), **seulement** si elle est en `https://`
   ou en `http://localhost` (ou `http://127.0.0.1`).

L'adresse par laquelle votre navigateur ouvre Home Assistant n'est pas utilisée : l'interface
de Home Assistant ne la transmet pas aux options d'une intégration, où se fait l'ajout d'une
destination.

Si aucune adresse ne convient, l'ajout s'interrompt :

- « Home Assistant n'a pas d'URL externe configurée, ni d'URL locale utilisable » : ni URL
  Internet, ni URL de réseau local ;
- « L'adresse … ne peut pas servir au retour d'autorisation » : l'adresse trouvée est en
  `http://` sans être `localhost` (par exemple `http://homeassistant.local:8123`), et Dropbox
  la refuserait.

### Tester en local, sans URL externe

C'est votre **navigateur** qui suit la redirection de Dropbox : `http://localhost` n'atteint
Home Assistant que si le navigateur tourne **sur la même machine** que lui (instance de
développement, par exemple). Dans ce cas :

1. dans **Paramètres → Système → Réseau**, réglez l'**URL de réseau local** sur
   `http://localhost:8123` (laissez l'URL Internet vide) ;
2. ouvrez Home Assistant à l'adresse `http://localhost:8123` ;
3. déclarez chez Dropbox (étape 3) l'URI `http://localhost:8123/auth/auto_backup/callback`.

Depuis un autre poste, `localhost` désignerait ce poste : l'autorisation n'aboutirait pas.

> **Nextcloud, reverse proxy, Nabu Casa…** peu importe la façon dont votre instance est
> publiée : seule compte l'adresse que vous utilisez pour y accéder depuis l'extérieur.

## 1. Créer l'application Dropbox

1. Ouvrez la console développeur : <https://www.dropbox.com/developers/apps>.
2. Cliquez sur **Create app**.
3. Choisissez **Scoped access** — c'est le seul type proposé aujourd'hui, et celui qu'Auto
   Backup utilise.
4. Choisissez le type d'accès :

   | Choix | Ce que l'application peut voir | Recommandation |
   | --- | --- | --- |
   | **App folder** | Un unique dossier `Applications/<nom de votre app>`, créé par Dropbox | **Recommandé** |
   | **Full Dropbox** | La totalité de votre Dropbox | À éviter |

   **Choisissez « App folder ».** Si l'application était un jour compromise, elle ne pourrait
   toucher qu'à ce dossier, jamais au reste de vos fichiers.

   **Conséquence sur le dossier distant** : avec « App folder », les chemins sont *relatifs à
   ce dossier*. Le dossier que vous saisirez dans Auto Backup (par exemple
   `Sauvegardes/Home Assistant`) sera donc créé **dans**
   `Applications/<nom de votre app>/`, et non à la racine de votre Dropbox. Il n'y a rien de
   plus à faire : vous n'avez pas à mentionner `Applications/...` dans Auto Backup.

5. Donnez un nom à l'application. Il doit être unique chez Dropbox : `auto-backup-<votre
   pseudo>` fait l'affaire. C'est aussi le nom du dossier créé si vous avez choisi
   « App folder ».
6. Validez avec **Create app**.

## 2. Cocher les portées (onglet « Permissions »)

Ouvrez l'onglet **Permissions** de l'application et cochez **exactement** ces trois cases :

| Portée | À quoi elle sert dans Auto Backup |
| --- | --- |
| `account_info.read` | Identifier le compte connecté : Auto Backup propose son nom comme nom de destination et s'en sert pour vérifier que l'accès fonctionne. |
| `files.content.write` | Déposer une sauvegarde dans le dossier choisi, et supprimer celles qui ont dépassé la rétention. |
| `files.metadata.read` | Lister les sauvegardes déjà déposées, avec leur date et leur taille — c'est ce qui permet d'appliquer la rétention sans rien supprimer à l'aveugle. |

**Ne cochez rien d'autre.** Auto Backup ne demande pas `files.content.read` : il n'a jamais
besoin de relire le contenu de vos sauvegardes (les restaurer depuis Dropbox ne fait pas
partie de ce qu'il sait faire). Il ne demande pas davantage de partage (`sharing.*`), de
demandes de fichiers, de contacts, ni la moindre permission d'équipe. Une portée cochée
« au cas où » serait un accès que vous accordez sans contrepartie.

Cliquez sur **Submit** en bas de l'onglet pour enregistrer les portées.

> **Important** : les portées doivent être enregistrées **avant** de connecter le compte dans
> Home Assistant. Une portée ajoutée après coup n'est pas accordée au jeton déjà obtenu : il
> faudrait alors repasser par « Ré-autoriser une destination ».

## 3. Déclarer l'URI de redirection (onglet « Settings »)

Toujours dans votre application, onglet **Settings**, section **OAuth 2 → Redirect URIs** :

1. Saisissez l'adresse suivante, en remplaçant `<instance>` par votre URL externe (pour un
   test local, `http://localhost:8123/auth/auto_backup/callback`) :

   ```text
   https://<instance>/auth/auto_backup/callback
   ```

   Par exemple : `https://maison.exemple.fr/auth/auto_backup/callback`.

2. Cliquez sur **Add**.

L'adresse doit être **identique au caractère près** à celle qu'Auto Backup affiche pendant
l'ajout d'une destination (elle y est rappelée, prête à être copiée) : même protocole, même
nom d'hôte, même port, même chemin, et pas de `/` final surnuméraire. Dropbox compare cette
valeur à l'identique et refuse l'autorisation à la moindre différence.

> **Pourquoi ce chemin plutôt que celui, habituel, de Home Assistant ?** Les destinations
> d'Auto Backup se configurent depuis les *options* de l'intégration, et la page de retour
> standard de Home Assistant (`/auth/external/callback`) ne sait reprendre qu'un flux de
> *configuration*. Le fork expose donc sa propre page de retour. Le raccourci
> `my.home-assistant.io` n'est pas utilisable pour la même raison. Le détail est dans
> [l'ADR des destinations distantes](../adr/0001-destinations-distantes.md).

## 4. Relever la clé et le secret

Dans l'onglet **Settings**, section **App key** et **App secret** :

- **App key** : visible directement. C'est l'« identifiant client ».
- **App secret** : cliquez sur **Show** pour l'afficher. C'est le « secret client ».

Ces deux valeurs sont des **secrets** : elles ne doivent apparaître ni dans une capture
d'écran, ni dans un ticket, ni dans un dépôt Git. Auto Backup les range dans la
configuration de l'intégration, au même endroit que Home Assistant range les identifiants
d'application de toutes ses intégrations OAuth2, et ne les journalise jamais — pas même en
niveau `debug`.

## 5. Connecter le compte dans Home Assistant

1. **Paramètres → Appareils et services → Auto Backup → Configurer**.
2. Choisissez **Ajouter une destination**, puis **Dropbox**.
3. Collez la clé et le secret de l'application. L'URI de redirection à déclarer chez Dropbox
   est rappelée sur cet écran : vérifiez qu'elle correspond à celle de l'étape 3.
4. Une fenêtre Dropbox s'ouvre et récapitule les accès demandés. Cliquez sur **Autoriser**.
5. De retour dans Home Assistant, Auto Backup a identifié votre compte et propose un nom :
   « Dropbox – *votre nom* ». Vous pouvez le remplacer.
6. Choisissez le **dossier distant** (par défaut `Home Assistant`) et, si vous le souhaitez,
   une rétention propre à cette destination : un nombre de jours, un nombre maximum de
   sauvegardes, ou les deux.
7. Validez. La destination est enregistrée.

Le dossier distant est un chemin relatif : lettres, chiffres, espaces et `- _ . ( )`, avec
`/` pour séparer les niveaux (`Sauvegardes/Home Assistant`). Les chemins absolus et les
remontées (`..`) sont refusés.

## Ce qui se passe ensuite

Auto Backup demande à Dropbox un **accès hors-ligne**
(`token_access_type=offline`) : le jeton d'accès, valable quatre heures, est accompagné d'un
jeton de rafraîchissement. Votre instance renouvelle donc l'accès toute seule, indéfiniment,
sans que vous ayez à revenir ici.

Si l'accès est perdu, Home Assistant s'en aperçoit au premier appel : un problème apparaît dans
**Paramètres → Système → Réparations**, nommant la destination concernée. Les autres
destinations continuent de fonctionner. La façon de la remettre en service dépend de la cause :

| Cause | Que faire |
| --- | --- |
| Vous avez révoqué l'accès depuis [les applications connectées de votre compte Dropbox](https://www.dropbox.com/account/connected_apps) | **Configurer → Ré-autoriser une destination** : l'application et son secret sont inchangés, seule l'autorisation est à renouveler. |
| Vous avez **régénéré le secret** de l'application (même application) | La ré-autorisation réutilise la clé et le secret enregistrés : elle échouerait. **Supprimez la destination, puis ajoutez-la de nouveau** avec la même clé et le nouveau secret, en reprenant **le même dossier distant** : les sauvegardes déjà déposées restent reconnues à leur nom et soumises à la rétention. |
| Vous avez **supprimé ou recréé l'application** dans la console développeur | **Supprimez la destination, puis ajoutez-la de nouveau** avec la clé et le secret de la nouvelle application. En « App folder », celle-ci travaille dans un **nouveau dossier** `Applications/<nom de la nouvelle app>` et ne voit pas celui de l'ancienne : les sauvegardes déposées par l'ancienne application ne sont plus ni listées ni purgées. Retirez-les à la main depuis Dropbox. |

## Téléverser une sauvegarde

Une fois la destination créée, ajoutez `upload_to` à votre appel de service (ou à votre
automatisation) : la sauvegarde part chez Dropbox dès qu'elle est créée, en tâche de fond.

```yaml
service: auto_backup.backup
data:
  name: Sauvegarde du soir
  upload_to: Dropbox de Jeanne
```

### Où le fichier arrive, et sous quel nom

Le fichier est déposé dans le **dossier distant** que vous avez choisi à l'ajout de la
destination, sous un nom de la forme :

```text
Sauvegarde du soir [a1b2c3d4].tar
```

Le nom de la sauvegarde seul ne suffirait pas à s'y retrouver : sur une installation Home
Assistant Core, les sauvegardes automatiques s'appellent toutes `Core <version>`. Le **slug**
— l'identifiant unique que Home Assistant donne à chaque sauvegarde — est donc accolé entre
crochets. Les caractères que Dropbox refuse dans un nom de fichier (`/ \ : ? * < > " |`) sont
remplacés par `_`, ainsi que les caractères Unicode qui *ressemblent* à une barre oblique sans en
être une (U+2215, U+2044...), pour qu'un nom déposé ne puisse jamais faire croire à un
sous-dossier. Un nom très long est raccourci, le slug et le `.tar` étant conservés.

> **Avec « App folder »**, tout cela se passe à l'intérieur de
> `Applications/<nom de votre app>/` : le dossier distant y est créé, et vous n'avez rien à
> changer dans Auto Backup.

**Le dossier est créé s'il n'existe pas**, y compris ses niveaux intermédiaires. Aucun réglage
n'est nécessaire, et un dossier déjà présent n'est jamais un problème.

**Aucun fichier n'est jamais écrasé.** Si le dossier contient déjà un fichier portant exactement
ce nom, Auto Backup ne le remplace pas et ne dépose pas non plus une copie sous un nom voisin :
le téléversement échoue avec un message le disant. Le cas ne se produit en pratique que si vous
relancez une sauvegarde portant le même nom *et* le même slug, ou si vous avez déposé le fichier
à la main.

### Les grosses sauvegardes

L'API Dropbox refuse d'un seul tenant un fichier de **150 Mo ou plus**, ce qu'une sauvegarde
complète dépasse très souvent. Auto Backup choisit donc tout seul :

| Taille de la sauvegarde | Comment elle part |
| --- | --- |
| Moins de 150 Mo | Une seule requête. |
| 150 Mo et plus, ou taille inconnue | Une **session d'envoi** : la sauvegarde est découpée en fragments de 8 Mio, envoyés l'un après l'autre, puis validés en bloc. |

Dans les deux cas la sauvegarde n'est **jamais chargée entièrement en mémoire** : elle est lue
et envoyée au fil de l'eau. Et dans les deux cas, à la fin du dépôt, la taille enregistrée par
Dropbox est comparée au nombre d'octets réellement partis ; en cas d'écart, le téléversement est
déclaré en échec plutôt que de laisser passer une archive tronquée.

La taille est « inconnue » sur une installation supervisée quand le Supervisor n'annonce pas la
taille du téléchargement : la session d'envoi est alors utilisée par précaution.

### Si Dropbox refuse ou tarde

| Situation | Ce que fait Auto Backup |
| --- | --- |
| Limitation de débit (`429`) ou panne passagère (`5xx`) | Jusqu'à **trois tentatives**, en respectant le délai demandé par Dropbox, sans jamais attendre plus d'une minute. |
| Espace de stockage saturé | Échec immédiat, avec un message invitant à libérer de la place ou à réduire la rétention. Inutile de réessayer : c'est à vous de jouer. |
| Nom déjà pris dans le dossier | Échec immédiat : rien n'est écrasé. |
| Accès révoqué ou portée manquante | Échec, et la destination est signalée **à ré-autoriser** dans Réparations. |

Un échec émet l'événement `auto_backup.upload_failed`, dont le champ `error_code` porte le code
stable de la cause (`quota_exceeded`, `rate_limited`…) et le champ `error` le message traduit
(voir [les codes d'erreur](../services.md#codes-derreur)).
**La sauvegarde locale n'est jamais supprimée ni altérée** par un échec de téléversement, et les
autres destinations de la même sauvegarde sont traitées normalement.

Une nuance utile : les nouvelles tentatives ne s'appliquent qu'aux envois **fragmentés**, dont
chaque morceau est encore disponible. Une sauvegarde de moins de 150 Mo est lue une seule fois,
au fil de l'eau : si Dropbox la refuse en cours de route, l'envoi est abandonné et c'est la
sauvegarde suivante qui repartira. Le délai maximum global (« Réglages du téléversement »,
30 minutes par défaut) s'applique par-dessus tout cela.

Ce même réglage borne aussi chaque requête prise isolément, pour le cas où Dropbox cesserait de
répondre sans fermer la connexion. Autrement dit, **si votre connexion est lente, il suffit
d'augmenter ce délai** : la valeur que vous choisissez vaut pour le téléversement entier comme
pour la requête qui transporte la sauvegarde, et vous n'avez rien d'autre à régler.

## Rétention : ce qui est supprimé, et ce qui ne l'est jamais

Si vous avez réglé une rétention sur la destination — un nombre de jours, un nombre maximum de
sauvegardes, ou les deux —, Auto Backup **supprime pour de bon** les sauvegardes en trop dans
votre Dropbox. La purge a lieu après chaque téléversement réussi, si l'option « Purge
automatique » est active, et à chaque appel du service `auto_backup.purge`.

### Ce qui est purgé

Seuls les fichiers qu'Auto Backup reconnaît comme **les siens**. L'API Dropbox ne permet pas
d'apposer une marque invisible sur un fichier : la reconnaissance passe donc par deux voies,
dans cet ordre.

1. Le **registre** que Home Assistant tient de ses propres téléversements, dans son stockage
   interne (`auto_backup.remote_backups`).
2. À défaut, le **nom du fichier**, s'il a exactement la forme que produit le dépôt —
   `Sauvegarde du soir [a1b2c3d4].tar` : un nom, un slug entre crochets, l'extension `.tar`.

La seconde voie sert les cas où le registre ne sait rien : une réinstallation de Home
Assistant, un stockage interne effacé, ou un dépôt qui a abouti chez Dropbox après avoir été
rapporté en échec (voir « Limites connues »). Sans elle, ces fichiers resteraient chez vous
indéfiniment, sans que rien ne puisse plus les rattacher à Auto Backup.

> **Important : un dossier par instance** — Le registre de purge est local à chaque instance Home
> Assistant. Si plusieurs instances partagent le même dossier distant, le registre d'une instance
> ne connaît pas les sauvegardes déposées par l'autre, et les **deux** instances peuvent compter et
> purger les sauvegardes de l'autre : la reconnaissance par le nom ne distingue pas les instances.
> **Utilisez un dossier distinct pour chaque instance Home Assistant** ; le listage n'étant pas
> récursif, un sous-dossier par instance dans le même dossier Dropbox suffit.

### Ce qui n'est jamais touché

Tout le reste du dossier : vos propres fichiers, les sauvegardes d'un autre outil, les
sous-dossiers et ce qu'ils contiennent — la purge ne descend pas d'un niveau. Un fichier que
les deux voies ci-dessus n'atteignent pas est ignoré, quelles que soient son ancienneté et sa
taille.

> **Une précaution, du coup** : ne déposez pas vous-même, dans le dossier de la destination, un
> fichier nommé comme une sauvegarde d'Auto Backup (`… [quelque chose].tar`). Il serait pris
> pour l'une des siennes, et la rétention pourrait le supprimer. Rangez vos fichiers ailleurs,
> ou dans un sous-dossier.

### Ce que vous voyez

Une purge qui supprime quelque chose émet l'événement `auto_backup.remote_purge` — il nomme la
destination et liste les identifiants supprimés — et écrit une ligne dans le journal de Home
Assistant. Une sauvegarde déjà disparue de Dropbox, que vous auriez supprimée à la main, n'est
pas une erreur : elle est simplement rayée du registre.

## En cas de problème

| Message | Cause la plus fréquente |
| --- | --- |
| « Home Assistant n'a pas d'URL externe configurée, ni d'URL locale utilisable » | Ni URL Internet ni URL de réseau local dans Paramètres → Système → Réseau. Renseignez l'URL Internet en `https://` (ou, pour un test sur la machine même, l'URL de réseau local `http://localhost:8123`). |
| « L'adresse … ne peut pas servir au retour d'autorisation » | L'adresse trouvée est en `http://` sans être `localhost` (par exemple `http://homeassistant.local:8123`). Renseignez une URL Internet en `https://`, ou suivez « Tester en local, sans URL externe ». |
| « Le fournisseur a refusé le code d'autorisation » | Clé ou secret erroné, ou URI de redirection déclarée chez Dropbox différente de celle affichée par Auto Backup. |
| « L'autorisation a été refusée ou annulée » | Vous avez cliqué sur *Cancel* dans la fenêtre Dropbox, ou fermé l'onglet. Rien n'a été créé : recommencez quand vous voulez. |
| « Le fournisseur a refusé la première requête : … » | Juste après l'autorisation, Auto Backup demande à Dropbox qui est le compte connecté. Si cet appel échoue, **l'ajout s'arrête et rien n'est enregistré** : le message nomme la cause (permission manquante, panne passagère de Dropbox), et le détail renvoyé par Dropbox est consigné dans le journal de Home Assistant. Corrigez-la, puis relancez **Ajouter une destination** — vous n'avez rien à nettoyer. |
| « il manque une permission » dans le message, ou `missing_scope` dans le journal | Une portée n'a pas été cochée (ou l'a été après l'autorisation). Cochez-la dans l'onglet *Permissions*, puis recommencez l'ajout — ou, si la destination existe déjà, **Ré-autoriser une destination**. |
| Dropbox ouvre une page « invalid redirect_uri » | L'URI déclarée ne correspond pas exactement (protocole, port, `/` final). |
| « un fichier nommé … existe déjà chez Dropbox » | Le dossier contient déjà une sauvegarde portant ce nom et ce slug. Auto Backup n'écrase rien : supprimez ou renommez le fichier chez Dropbox si vous voulez le remplacer. |
| « l'espace de stockage Dropbox … est saturé » | Votre compte Dropbox est plein. Libérez de la place, ou réduisez la rétention distante de la destination. |
| « le dépôt de … est incomplet : Dropbox a enregistré … » | La taille enregistrée par Dropbox ne correspond pas à ce qui a été envoyé (transfert interrompu). Le fichier partiel reste chez Dropbox : supprimez-le avant de relancer. |
| « le dépôt de … est incomplet : … octets ont été lus pour … annoncés » | La sauvegarde lue n'avait pas la taille que Home Assistant avait annoncée. Rien n'est déposé de fiable : relancez la sauvegarde, et signalez le cas s'il se reproduit. |
| « délai de téléversement dépassé » | La sauvegarde n'a pas fini de partir dans le temps imparti. Augmentez-le dans **Configurer → Réglages du téléversement**. |
| « la sauvegarde distante … n'existe plus chez Dropbox » | Le fichier avait déjà disparu au moment de le supprimer. Ce n'est pas une erreur : la purge le compte comme supprimé et continue. |
| « le listage des sauvegardes … n'a pas abouti dans le temps imparti » | Dropbox tarde à répondre, ou le dossier contient énormément de fichiers. La purge reprendra à la prochaine sauvegarde ; rien n'est supprimé entre-temps. |
| « Listage Dropbox … tronqué après 20 pages » | Le dossier contient plus de 20 000 fichiers. Au-delà de cette limite, certaines sauvegardes peuvent ne pas être traitées tant que le dossier dépasse la limite (Dropbox ne garantit pas l'ordre des listes) — faites du ménage si le message revient. Aucune suppression à tort n'est possible : seules les sauvegardes reconnaissables au registre ou par la convention de nommage sont purgées. |

Le journal détaillé s'active avec :

```yaml
logger:
  logs:
    custom_components.auto_backup.destinations: debug
```

Aucun secret n'y figure : ni clé, ni secret d'application, ni jeton, ni identifiant de
compte, ni en-tête d'autorisation — y compris pendant un téléversement.

**Pourquoi l'ajout s'arrête-t-il au lieu de continuer ?** Une destination que Dropbox refuse
déjà d'identifier ne fonctionnerait pas davantage une fois créée : elle échouerait à chaque
sauvegarde, sans rien dire de ce qu'il faut corriger. Mieux vaut recommencer l'ajout — c'est
un clic — que diagnostiquer plus tard une destination muette.

## Modifier la destination

**Configurer → Modifier une destination** change le nom, le dossier distant et la rétention d'une
destination Dropbox **sans repasser par Dropbox** : la clé, le secret et le jeton sont conservés,
tout comme les entités de la destination et la liste des sauvegardes déjà déposées. Le nom
affiché des entités suit le nouveau nom.

Changer de **dossier distant** demande une confirmation : les sauvegardes déjà déposées dans
l'ancien dossier (`Applications/<nom de votre app>/<ancien dossier>`) y restent, mais ne sont
plus ni listées, ni purgées, ni comptées par Auto Backup. Le nouveau dossier est créé au premier
téléversement.

Elle ne touche ni la clé ni le secret de l'application : quand ceux-ci changent, la marche à
suivre reste celle du tableau de « Ce qui se passe ensuite ».

## Limites connues

- **Une URL externe HTTPS et joignable est obligatoire**, sauf pour un test sur la machine
  même de Home Assistant par `http://localhost:8123`. Une instance accessible uniquement en
  local sur le réseau (`http://homeassistant.local:8123`, adresse IP nue) ne peut pas recevoir
  le retour d'autorisation de Dropbox.
- **Dropbox Business et les espaces d'équipe ne sont pas pris en charge** : les portées
  d'équipe (`team_*`) ne sont pas demandées et les chemins d'espace partagé ne sont pas gérés.
- **Une application Dropbox non publiée est limitée** à un petit nombre de comptes connectés
  (le vôtre suffit) ; cela n'a aucune incidence sur le volume de fichiers déposés.
- **Un dépôt signalé en échec peut, rarement, avoir abouti.** Si Dropbox enregistre le fichier
  mais que la réponse n'arrive pas telle qu'attendue (nom déjà pris au moment de valider la
  session, taille enregistrée différente de ce qui a été envoyé, délai dépassé juste après la
  validation), Home Assistant annonce un échec alors que le fichier est bien là. Il n'est pas
  perdu de vue pour autant : la purge le reconnaît à son nom et l'inclut dans la rétention comme
  les autres. En revanche, si vous **renommez** un fichier déposé par Auto Backup, il cesse
  d'être reconnu et ne sera plus jamais supprimé automatiquement.
- **Un très grand dossier n'est parcouru que partiellement à chaque purge** : le listage
  s'arrête à 20 000 fichiers, en le disant dans le journal. Au-delà de cette limite, certaines
  sauvegardes peuvent ne pas être traitées tant que le dossier dépasse le seuil — Dropbox ne
  garantit pas l'ordre du listage d'un appel à l'autre. Aucune sauvegarde n'est supprimée par
  erreur : l'algorithme de purge ne supprime que ce qu'il a lui-même reconnu. Recommandation :
  faites du ménage si le message revient.
- **La corbeille Dropbox n'est pas vidée** : un fichier supprimé par la rétention reste
  récupérable depuis les fichiers supprimés de Dropbox pendant la durée prévue par votre offre.
- **Restaurer une sauvegarde depuis Dropbox n'est pas possible** depuis Home Assistant :
  téléchargez le fichier `.tar` depuis Dropbox, puis utilisez la restauration habituelle. Auto
  Backup ne demande d'ailleurs pas la permission de relire vos fichiers.
- **Un téléversement interrompu ne reprend pas** : un redémarrage de Home Assistant en plein
  envoi abandonne le transfert, et la sauvegarde suivante repartira de zéro. Rien n'apparaît
  dans votre dossier tant qu'une session d'envoi n'a pas été validée : une session inachevée ne
  laisse pas de fichier partiel derrière elle.
- **Une sauvegarde de moins de 150 Mo n'est pas renvoyée** si Dropbox la refuse en cours de
  route (voir « Si Dropbox refuse ou tarde » ci-dessus).

## Pour aller plus loin

- [Services, options et rétention distante](../services.md) : toutes les options de service, les
  événements et une automatisation complète de sauvegarde quotidienne envoyée dans le cloud.
- [Questions fréquentes](../faq.md) : taille et quotas, chiffrement, jetons, ré-authentification.
