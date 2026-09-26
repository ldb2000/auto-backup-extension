# Connecter un compte Dropbox

Auto Backup n'est pas une application Dropbox publique : **vous créez votre propre
application Dropbox**, et vous seul en détenez la clé et le secret. Aucun identifiant n'est
livré dans ce dépôt, et rien ne transite par un serveur tiers — votre Home Assistant parle
directement à Dropbox.

Comptez cinq minutes. La procédure est à faire une seule fois par compte Dropbox.

## Avant de commencer

| Prérequis | Pourquoi |
| --- | --- |
| Une **URL externe HTTPS** configurée dans Home Assistant | Dropbox doit pouvoir vous renvoyer vers votre instance après l'autorisation. |
| Un compte Dropbox personnel | Dropbox Business et les espaces d'équipe ne sont pas pris en charge. |

L'URL externe se règle dans **Paramètres → Système → Réseau → URL Internet**. Elle doit être
en `https://` : Dropbox refuse une URI de redirection en clair (`http://`), à la seule
exception de `http://localhost`, qui ne convient pas ici puisque c'est votre navigateur, et
non Home Assistant, qui suit la redirection. Sans URL externe, l'ajout d'une destination
s'interrompt avec le message « Home Assistant n'a pas d'URL externe configurée ».

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

1. Saisissez l'adresse suivante, en remplaçant `<instance>` par votre URL externe :

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

Si vous révoquez l'accès depuis
[les applications connectées de votre compte Dropbox](https://www.dropbox.com/account/connected_apps),
ou si vous supprimez l'application dans la console développeur, Home Assistant s'en aperçoit
au premier appel : un problème apparaît dans **Paramètres → Système → Réparations**, nommant
la destination concernée. Les autres destinations continuent de fonctionner. Pour la remettre
en service : **Configurer → Ré-autoriser une destination**.

## En cas de problème

| Message | Cause la plus fréquente |
| --- | --- |
| « Home Assistant n'a pas d'URL externe configurée » | L'URL Internet n'est pas renseignée dans Paramètres → Système → Réseau. |
| « Le fournisseur a refusé le code d'autorisation » | Clé ou secret erroné, ou URI de redirection déclarée chez Dropbox différente de celle affichée par Auto Backup. |
| « L'autorisation a été refusée ou annulée » | Vous avez cliqué sur *Cancel* dans la fenêtre Dropbox, ou fermé l'onglet. Rien n'a été créé : recommencez quand vous voulez. |
| « Le fournisseur a refusé la première requête : … » | Juste après l'autorisation, Auto Backup demande à Dropbox qui est le compte connecté. Si cet appel échoue, **l'ajout s'arrête et rien n'est enregistré** : le message cite la cause renvoyée par Dropbox (portée manquante, service indisponible). Corrigez-la, puis relancez **Ajouter une destination** — vous n'avez rien à nettoyer. |
| `missing_scope` dans le message ou le journal | Une portée n'a pas été cochée (ou l'a été après l'autorisation). Cochez-la dans l'onglet *Permissions*, puis recommencez l'ajout — ou, si la destination existe déjà, **Ré-autoriser une destination**. |
| Dropbox ouvre une page « invalid redirect_uri » | L'URI déclarée ne correspond pas exactement (protocole, port, `/` final). |

Le journal détaillé s'active avec :

```yaml
logger:
  logs:
    custom_components.auto_backup.destinations: debug
```

Aucun secret n'y figure : ni clé, ni secret d'application, ni jeton, ni identifiant de
compte.

**Pourquoi l'ajout s'arrête-t-il au lieu de continuer ?** Une destination que Dropbox refuse
déjà d'identifier ne fonctionnerait pas davantage une fois créée : elle échouerait à chaque
sauvegarde, sans rien dire de ce qu'il faut corriger. Mieux vaut recommencer l'ajout — c'est
un clic — que diagnostiquer plus tard une destination muette.

## Limites connues

- **Une URL externe HTTPS et joignable est obligatoire.** Une instance accessible uniquement
  en local (`http://homeassistant.local:8123`, adresse IP nue) ne peut pas recevoir le retour
  d'autorisation de Dropbox.
- **Dropbox Business et les espaces d'équipe ne sont pas pris en charge** : les portées
  d'équipe (`team_*`) ne sont pas demandées et les chemins d'espace partagé ne sont pas gérés.
- **Une application Dropbox non publiée est limitée** à un petit nombre de comptes connectés
  (le vôtre suffit) ; cela n'a aucune incidence sur le volume de fichiers déposés.
- **L'envoi, le listage et la purge distante des sauvegardes arrivent dans les versions
  suivantes** : cette page décrit la connexion du compte. Ajouter la destination dès
  maintenant ne fait donc encore rien remonter chez Dropbox.
