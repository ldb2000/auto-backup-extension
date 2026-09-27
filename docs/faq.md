# Questions fréquentes

Réponses courtes aux questions qui reviennent le plus souvent sur l'envoi des sauvegardes vers
Dropbox et Google Drive. Pour la mise en place, voir les guides [Dropbox](destinations/dropbox.md)
et [Google Drive](destinations/google-drive.md) ; pour les options de service et la rétention,
voir [Services, options et rétention distante](services.md).

## Taille des sauvegardes et quotas cloud

### Quelle place mes sauvegardes vont-elles occuper dans le cloud ?

Chaque sauvegarde envoyée occupe chez le fournisseur **la taille du fichier `.tar` local**, ni
plus ni moins : Auto Backup ne recompresse ni ne découpe le fichier déposé. L'espace consommé
par une destination est donc, à peu près, la taille d'une sauvegarde multipliée par le nombre de
sauvegardes que sa rétention conserve. Avec une sauvegarde de 1,5 Go et une rétention de
14 sauvegardes, comptez environ 21 Go.

Pour estimer la taille d'une sauvegarde, regardez celle des sauvegardes existantes dans
**Paramètres → Système → Sauvegardes**, ou le champ `size` (en octets) de l'événement
`auto_backup.upload_successful`.

### Comment réduire la taille des sauvegardes ?

- Excluez la base de données (`exclude_database: true`) si l'historique ne vous est pas
  indispensable : c'est souvent le plus gros poste.
- Sur une installation supervisée, excluez les dossiers volumineux (`media`, `share`) ou les
  modules complémentaires dont les données se reconstruisent (`exclude` de
  `auto_backup.backup_full`, ou `auto_backup.backup_partial`).
- Gardez `compressed: true` (valeur par défaut).

### Que se passe-t-il quand mon espace cloud est plein ?

L'envoi échoue aussitôt, sans nouvelle tentative, avec un message qui le dit ; l'événement
`auto_backup.upload_failed` est émis, une notification persistante apparaît et l'entité
« *Destination* : problème de téléversement » s'allume. **La sauvegarde locale reste intacte.**
Libérez de la place, ou réduisez la rétention de la destination.

Gardez en tête que l'offre gratuite des fournisseurs est modeste (à la date de rédaction :
2 Go chez Dropbox Basic, 15 Go chez Google, partagés avec Gmail et Google Photos). Les
sauvegardes supprimées par la rétention libèrent l'espace de votre compte chez les deux
fournisseurs. Chez Google Drive, la suppression est définitive (sans corbeille) ; chez Dropbox,
les fichiers supprimés restent récupérables pendant la durée prévue par votre offre.

### Y a-t-il une taille maximale de sauvegarde ?

Pas en pratique. Les grosses sauvegardes partent par fragments de 8 Mio chez les deux
fournisseurs, sans jamais être chargées entières en mémoire. La vraie limite est le **délai
maximum d'un envoi** (1800 secondes par défaut) : sur une connexion lente, augmentez-le dans
**Configurer → Réglages du téléversement** (option `upload_timeout`).

## Chiffrement et mot de passe

### Mes sauvegardes sont-elles chiffrées dans le cloud ?

**Seulement si vous les chiffrez à la création.** Auto Backup envoie le fichier tel que Home
Assistant l'a produit : il ne chiffre pas, ne déchiffre pas et ne relit pas son contenu. Deux
options des services de sauvegarde le chiffrent :

- `password` : un mot de passe de votre choix ;
- `encrypted: true` : la clé de chiffrement par défaut définie dans les paramètres de sauvegarde
  de Home Assistant.

Sans l'une ou l'autre, la copie déposée chez Dropbox ou Google Drive **n'est pas chiffrée** : elle
n'est protégée que par la sécurité de votre compte cloud.

### Pourquoi est-ce recommandé ?

Une sauvegarde de Home Assistant contient votre configuration, et donc les jetons et mots de passe
de vos intégrations — y compris le jeton d'accès et le secret d'application d'Auto Backup
lui-même (voir plus bas). Toute personne qui obtiendrait le fichier en lirait le contenu. **Pour
une sauvegarde envoyée hors de chez vous, fixez un mot de passe.**

### Où ranger le mot de passe ?

Dans `secrets.yaml`, jamais en clair dans une automatisation :

```yaml
# secrets.yaml
mot_de_passe_sauvegarde: "une-phrase-de-passe-longue-et-unique"
```

```yaml
# dans l'automatisation
action: auto_backup.backup_full
data:
  password: !secret mot_de_passe_sauvegarde
  upload_to: Dropbox – Jeanne Dupont
```

**Conservez aussi ce mot de passe hors de Home Assistant** (gestionnaire de mots de passe) :
si l'instance est perdue, c'est lui qui permettra d'ouvrir la sauvegarde. Sans lui, une
sauvegarde chiffrée est irrécupérable.

### Comment restaurer une sauvegarde déposée dans le cloud ?

Auto Backup ne restaure pas depuis le cloud. Téléchargez le fichier `.tar` depuis Dropbox ou
Google Drive, puis importez-le dans **Paramètres → Système → Sauvegardes** (ou pendant
l'intégration initiale d'une nouvelle instance), en fournissant le mot de passe ou la clé de
chiffrement si la sauvegarde est chiffrée.

## Portées OAuth et stockage des jetons

### Quels accès Auto Backup demande-t-il ?

Le strict nécessaire, et rien d'autre :

| Fournisseur | Portées demandées | Ce qu'elles permettent |
| --- | --- | --- |
| Dropbox | `account_info.read`, `files.metadata.read`, `files.content.write` | Identifier le compte, lister les sauvegardes déposées, déposer et supprimer des fichiers. Pas de lecture du contenu de vos fichiers. |
| Google Drive | `https://www.googleapis.com/auth/drive.file` | Accéder **aux seuls fichiers créés par Auto Backup** : le reste de votre Drive lui est invisible. |

Les deux autorisations sont demandées en **accès hors-ligne** : le fournisseur délivre un jeton de
rafraîchissement, grâce auquel Home Assistant renouvelle l'accès tout seul. Le détail et la
justification de chaque portée sont dans les guides [Dropbox](destinations/dropbox.md#2-cocher-les-portées-onglet--permissions-)
et [Google Drive](destinations/google-drive.md#ce-quauto-backup-demande-à-google-et-pourquoi).

### Mes identifiants transitent-ils par un serveur tiers ?

Non. Vous créez **votre propre application** chez le fournisseur, et votre instance dialogue
directement avec Dropbox ou Google. Aucun identifiant n'est livré avec l'intégration, et aucun
service intermédiaire (pas même `my.home-assistant.io`) n'intervient.

### Où sont stockés les jetons ?

Dans la configuration de l'entrée Auto Backup, c'est-à-dire dans le fichier
`.storage/core.config_entries` du dossier de configuration de Home Assistant — là où Home
Assistant range les réglages de toutes ses intégrations. Pour chaque destination y figurent :
l'identifiant et le secret de votre application, le jeton d'accès et le jeton de
rafraîchissement, ainsi que l'identifiant ou l'adresse du compte connecté.

Ce fichier n'est pas chiffré par Home Assistant : protégez l'accès au dossier de configuration
(partages réseau, modules complémentaires d'édition de fichiers). Il fait aussi partie de vos
sauvegardes, d'où la recommandation de les chiffrer.

Le registre des sauvegardes déposées (`.storage/auto_backup.remote_backups`) ne contient, lui,
aucun secret : identifiants distants, noms, slugs, dates et tailles.

### Les jetons apparaissent-ils dans les journaux ou les notifications ?

Non. Ni les jetons, ni le secret d'application, ni l'en-tête d'autorisation ne sont journalisés,
même en niveau `debug`. Les messages d'erreur affichés — notifications persistantes, attribut
`last_error` de l'entité de problème, champ `error` de l'événement `auto_backup.upload_failed` —
sont masqués des secrets avant affichage.

### Comment retirer l'accès d'Auto Backup ?

**Configurer → Supprimer une destination** efface la configuration, les identifiants et les jetons
de Home Assistant. Retirez ensuite l'accès chez le fournisseur :
[applications connectées Dropbox](https://www.dropbox.com/account/connected_apps) ou
[autorisations du compte Google](https://myaccount.google.com/permissions). Les sauvegardes déjà
déposées ne sont pas supprimées.

## Ré-authentification

### Home Assistant me demande de ré-autoriser une destination : que faire ?

1. Ouvrez **Paramètres → Appareils et services → Auto Backup → Configurer**.
2. Choisissez **Ré-autoriser une destination**, puis la destination signalée.
3. Suivez de nouveau l'autorisation chez le fournisseur, **avec le même compte**.

Le nom, le dossier distant et la rétention de la destination sont conservés : seul l'accès est
renouvelé. Le problème affiché dans **Paramètres → Système → Réparations** et la notification
persistante disparaissent d'eux-mêmes.

La ré-autorisation réutilise l'identifiant et le secret d'application déjà enregistrés. Si vous
les avez changés chez le fournisseur (secret régénéré, application recréée), elle échouera :
**supprimez la destination, puis ajoutez-la de nouveau** avec les nouveaux identifiants, en
reprenant le même dossier distant. Ce qui arrive aux sauvegardes déjà déposées dépend du cas :

- **secret régénéré, même application** : une fois la destination supprimée puis ajoutée de
  nouveau, elles restent reconnues et soumises à la rétention ;
- **Dropbox, application supprimée ou recréée** : en accès « App folder », la nouvelle
  application travaille dans son propre dossier `Applications/<nom de la nouvelle app>` et ne
  voit pas celui de l'ancienne ; les anciennes sauvegardes ne sont plus ni listées ni purgées,
  retirez-les à la main depuis Dropbox ;
- **Google Drive, application créée dans un autre projet** Google Cloud : elle ne voit pas les
  fichiers déposés par l'ancienne (portée `drive.file`) ; les anciennes sauvegardes ne sont plus
  ni listées ni purgées, retirez-les à la main.

### Pourquoi l'accès a-t-il été perdu ?

Les causes les plus fréquentes :

- vous avez retiré l'accès depuis votre compte Dropbox ou Google ;
- l'application a été supprimée, ou son secret régénéré, dans la console du fournisseur (il faut
  alors supprimer puis ajouter de nouveau la destination, voir ci-dessus) ;
- **Google Drive** : l'application Google est restée en mode « Test », où l'autorisation expire au
  bout de 7 jours. Publiez-la en mode « Production » pour ne plus avoir à ré-autoriser (voir le
  [guide Google Drive](destinations/google-drive.md#mode--test--ou-mode--production--)).

### Que deviennent mes sauvegardes en attendant ?

Elles continuent d'être **créées localement**, normalement. Les envois vers la destination
concernée échouent aussitôt, sans contacter le fournisseur, avec l'événement
`auto_backup.upload_failed` (« ré-authentification requise ») ; sa purge distante est sautée. Les
autres destinations continuent de fonctionner. Une fois la destination ré-autorisée, les
sauvegardes suivantes repartent ; celles créées entre-temps ne sont pas renvoyées
automatiquement.

### J'ai ré-autorisé avec un autre compte par erreur

Avant d'enregistrer quoi que ce soit, l'interface nomme le compte précédent et le nouveau, et
demande confirmation. Sans confirmation, rien n'est modifié : relancez la ré-autorisation avec le
bon compte. Si vous confirmez, les sauvegardes déjà déposées sur l'ancien compte ne sont plus ni
listées ni purgées par Auto Backup.

## Plusieurs instances Home Assistant

### Puis-je envoyer les sauvegardes de plusieurs instances vers le même compte ?

Oui, à une condition : **un dossier distant par instance**. Auto Backup reconnaît ses sauvegardes
à leur nom chez Dropbox et à leur marqueur chez Google Drive, sans distinguer l'instance qui les a
déposées. Deux instances qui partagent un dossier comptent donc chacune les sauvegardes de l'autre
dans leur rétention, et peuvent les supprimer.

Donnez à chaque instance son propre dossier, par exemple `Home Assistant/Maison` et
`Home Assistant/Chalet` : la purge ne descend jamais dans les sous-dossiers, les deux instances ne
se gênent donc pas. Cette règle vaut pour Dropbox comme pour Google Drive.
