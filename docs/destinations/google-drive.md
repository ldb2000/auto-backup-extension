# Connecter Google Drive

Cette page décrit, pas à pas, comment autoriser Auto Backup à déposer vos sauvegardes sur
votre Google Drive. Tout se passe **chez vous** : vous créez votre propre application Google,
et ses identifiants restent dans votre instance Home Assistant. Aucun identifiant n'est livré
avec l'intégration.

> **À lire avant de commencer.** Google n'accepte de renvoyer l'utilisateur que vers une adresse
> **HTTPS sur un domaine public**. Une instance joignable uniquement en `http://`, en `.local`
> ou par une adresse IP (`192.168.1.10`, `100.x.y.z`) **ne peut pas** connecter Google Drive :
> Google refusera l'URI de redirection au moment où vous l'enregistrez. Voir
> [Prérequis : une URL externe publique](#prérequis--une-url-externe-publique).

> **Attention : la console Google Cloud a changé en 2025.** Les noms des écrans et des onglets
> ont été renommés — ce qui s'appelait auparavant les écrans du projet s'appelle désormais
> « Google Auth Platform », avec de nouveaux onglets (Branding, Audience, Clients). Si les
> libellés de cette page ne correspondent pas exactement à votre écran, consultez la structure
> générale (cherchez Identifiants, Écran de consentement OAuth) et adaptez à la terminologie
> actuelle. Les étapes restent identiques, seuls les noms ont changé.

## Ce qu'Auto Backup demande à Google, et pourquoi

| Demande | Valeur | Raison |
| --- | --- | --- |
| Portée | `https://www.googleapis.com/auth/drive.file` | Accès **aux seuls fichiers créés par l'application**. Auto Backup ne voit jamais le reste de votre Drive. |
| Accès hors-ligne | `access_type=offline` | Sans lui, Google ne fournit pas de jeton de rafraîchissement : l'accès expirerait au bout d'une heure. |
| Consentement forcé | `prompt=consent` | Google ne renvoie un jeton de rafraîchissement qu'au **premier** consentement. Sans cette demande, reconnecter le même compte donnerait un accès non renouvelable. |
| Portées héritées | `include_granted_scopes=false` | L'autorisation reste exactement celle décrite ici, sans hériter d'autres portées accordées au même projet. |

**Conséquence de la portée `drive.file` :** Auto Backup ne peut ni lire, ni lister, ni supprimer
les fichiers que vous avez déposés vous-même. Il ne voit que ce qu'il a créé. C'est voulu : la
purge distante ne peut donc jamais effacer vos propres documents. En contrepartie, si vous
déplacez ou supprimez une sauvegarde à la main dans Drive, Auto Backup ne la retrouvera pas.

## Prérequis : une URL externe publique

L'autorisation se termine par une redirection du navigateur vers votre instance, sur l'adresse
`https://<votre-instance>/auth/auto_backup/callback`. Google impose à cette adresse :

- le schéma **HTTPS** — `http://` est refusé (sauf `http://localhost`, inutilisable ici, car
  c'est votre navigateur qui doit joindre Home Assistant) ;
- un **nom de domaine public** — `homeassistant.local`, `.internal`, `.home` et les adresses IP
  nues sont refusés ;
- une **correspondance exacte** — pas de joker, et le chemin compte.

Renseignez donc l'URL externe de votre instance dans **Paramètres > Système > Réseau > URL
internet** (par exemple `https://ha.mondomaine.fr`) avant de commencer. Sans elle, le flux
d'ajout s'interrompt avec le message « Home Assistant n'a pas d'URL externe configurée ».

> Le raccourci habituel `https://my.home-assistant.io/redirect/oauth` **n'est pas utilisable** :
> il ne sait rediriger que vers l'adresse standard de Home Assistant, alors que les destinations
> d'Auto Backup sont créées par un flux d'options, qui a sa propre adresse de retour
> (cf. [l'ADR](../adr/0001-destinations-distantes.md)).

## 1. Créer un projet Google Cloud

1. Ouvrez la [console Google Cloud](https://console.cloud.google.com/).
2. En haut à gauche, cliquez sur le sélecteur de projet, puis sur **Nouveau projet**.
3. Nommez-le, par exemple `Home Assistant Auto Backup`, puis validez.
4. Vérifiez que ce projet est bien celui sélectionné avant de continuer.

Un compte Google personnel suffit ; aucune facturation n'est nécessaire pour l'usage décrit ici.

## 2. Activer l'API Google Drive

1. Dans le menu, ouvrez **API et services > Bibliothèque**.
2. Cherchez **Google Drive API**, ouvrez-la, puis cliquez sur **Activer**.

Si vous oubliez cette étape, l'autorisation semblera réussir, mais l'ajout de la destination
s'interrompra avec le message « l'API Google Drive n'est pas activée sur votre projet Google
Cloud ». Activez l'API, puis relancez simplement l'ajout : aucun redémarrage n'est nécessaire.

## 3. Configurer l'écran de consentement

1. Ouvrez **API et services > Écran de consentement OAuth**.
2. Choisissez le type **Externe** — le type « Interne » n'existe que pour les organisations
   Google Workspace.
3. Renseignez le nom de l'application (celui que vous verrez au moment d'autoriser), votre
   adresse d'assistance et votre adresse de contact.
4. À l'étape des portées, vous pouvez laisser la liste vide : Auto Backup demande sa portée au
   moment de l'autorisation.
5. À l'étape **Utilisateurs test**, ajoutez l'adresse Gmail du compte dont vous utiliserez le
   Drive. **C'est indispensable tant que l'application reste en mode « Test ».**

### Mode « Test » ou mode « Production » ?

| Mode | Qui peut autoriser | Durée de l'accès |
| --- | --- | --- |
| **Test** | uniquement les comptes listés comme utilisateurs test (100 maximum) | l'autorisation expire au bout de **7 jours** : la destination devra être ré-autorisée |
| **Production** | tout compte Google | l'autorisation ne s'interrompt plus d'elle-même |

Pour une installation domestique, **publiez l'application en mode Production** (bouton
**Publier l'application** sur l'écran de consentement). Avec la seule portée `drive.file`,
considérée comme non sensible par Google, la publication ne déclenche **aucune procédure de
vérification** : elle est immédiate. Rester en mode Test condamne à ré-autoriser la destination
chaque semaine.

## 4. Créer les identifiants OAuth

1. Ouvrez **API et services > Identifiants**, puis **Créer des identifiants > ID client OAuth**.
2. Type d'application : **Application Web**. N'utilisez ni « Ordinateur », ni « Application
   Web limitée », ni un compte de service : seul ce type accepte une URI de redirection.
3. Donnez-lui un nom, par exemple `Auto Backup`.
4. Dans **URI de redirection autorisés**, cliquez sur **Ajouter un URI** et saisissez
   exactement :

   ```text
   https://<votre-instance>/auth/auto_backup/callback
   ```

   en remplaçant `<votre-instance>` par votre URL externe, **sans barre oblique finale**.
   Home Assistant vous affiche l'adresse exacte à l'étape « Identifiants de votre application »
   du flux d'ajout : le plus sûr est de la copier depuis là.
5. Validez : Google affiche l'**ID client** et le **code secret du client**. Gardez-les sous la
   main, le secret n'est plus affiché ensuite (vous pourrez en régénérer un).

Laissez le champ **Origines JavaScript autorisées** vide : Auto Backup n'en utilise pas.

## 5. Ajouter la destination dans Home Assistant

1. **Paramètres > Appareils et services > Auto Backup > Configurer**.
2. Choisissez **Ajouter une destination**, puis **Google Drive**.
3. Collez l'**ID client** et le **code secret du client**. Le secret est masqué à la saisie,
   conservé dans votre entrée de configuration et n'apparaît dans aucun journal.
4. Une fenêtre s'ouvre sur l'écran de consentement Google : choisissez le compte, puis
   autorisez.
5. De retour dans Home Assistant, la destination est pré-nommée d'après le compte autorisé —
   par exemple `Google Drive – Camille Martin`. Ajustez le nom, le **dossier distant**
   (`Sauvegardes/Home Assistant` par exemple) et, si vous le souhaitez, une rétention propre à
   cette destination — elle est enregistrée dès maintenant, mais ne s'appliquera qu'avec
   l'issue #15 (voir « Ce qui n'est pas encore disponible »).
6. Validez : la destination apparaît dans les options.

L'adresse du compte autorisé est conservée avec la destination, ce qui permet de savoir plus
tard quel compte Google elle utilise.

## Téléversement des sauvegardes

Une fois la destination ajoutée, il suffit d'indiquer son nom (ou son identifiant) dans l'option
`upload_to` d'un service de sauvegarde :

```yaml
service: auto_backup.backup
data:
  name: Sauvegarde quotidienne
  upload_to: Google Drive – Camille Martin
```

La sauvegarde est créée localement, puis envoyée en tâche de fond. Elle n'est jamais chargée
entière en mémoire, ni recopiée sur le disque : elle part par fragments de 8 Mio, en mode
« resumable » — le mode d'envoi que Google prévoit pour les gros fichiers.

### Où arrivent les fichiers

Dans le **dossier distant** saisi au moment de l'ajout de la destination (`Sauvegardes/Home
Assistant`, par exemple). Ce dossier est **créé par Auto Backup** au premier téléversement, puis
réutilisé. C'est une conséquence directe de la portée `drive.file` : l'intégration ne voit que les
fichiers qu'elle a créés elle-même.

> **Un dossier du même nom que vous auriez créé à la main ne sera donc pas réutilisé** : Auto
> Backup ne peut pas le voir, et créera le sien à côté. Si vous déplacez ou renommez le dossier
> depuis Drive, il reste retrouvé (son identifiant, lui, ne change pas). Si vous le **supprimez**,
> un nouveau dossier est créé au téléversement suivant.

Chaque fichier est nommé `<nom de la sauvegarde> [<slug>].tar` — par exemple
`Sauvegarde quotidienne [a1b2c3d4].tar`. Le slug est l'identifiant de la sauvegarde côté Home
Assistant : il évite que deux sauvegardes portant le même nom ne se recouvrent, ce qui est le cas
courant sur Home Assistant Core, où les sauvegardes sans nom explicite s'appellent toutes
`Core <version>`.

Les fichiers portent aussi un **marqueur d'origine** invisible (une propriété privée
`auto_backup`). C'est lui que la purge distante exige avant de supprimer quoi que ce soit : vos
propres documents, qui ne le portent pas, ne peuvent pas être touchés. Le listage capable de le
relire chez Google arrive avec l'issue #15 (voir « Ce qui n'est pas encore disponible »).

### Si le transfert rencontre un incident

- **limitation de débit ou erreur passagère de Google** : l'envoi est retenté jusqu'à trois fois,
  avec un délai croissant, et **reprend exactement là où il s'était arrêté** — les fragments déjà
  reçus ne sont pas renvoyés ;
- **espace de stockage épuisé** : l'envoi s'arrête immédiatement (réessayer n'y changerait rien) et
  l'événement `auto_backup.upload_failed` porte le message correspondant ;
- **autorisation révoquée** : la destination est signalée à ré-autoriser, comme décrit plus bas ;
- **dans tous les cas, la sauvegarde locale reste intacte**, et les autres destinations demandées
  sont traitées normalement.

Le **délai maximum** accordé à un téléversement se règle dans les options de l'intégration, entrée
« Réglages du téléversement » (1800 secondes par défaut). Un envoi interrompu par un redémarrage de
Home Assistant n'est pas repris : la sauvegarde est simplement à renvoyer.

## En cas d'échec

| Message | Cause probable | Correction |
| --- | --- | --- |
| « Home Assistant n'a pas d'URL externe configurée » | aucune URL externe, ou elle n'est pas publique | Paramètres > Système > Réseau, puis recommencez |
| `redirect_uri_mismatch` (affiché par Google) | l'URI déclarée ne correspond pas exactement | recopiez l'adresse affichée par le formulaire, sans barre oblique finale |
| « Le fournisseur a refusé le code d'autorisation » | ID client ou secret erroné (`invalid_client`), ou URI de redirection différente | vérifiez les identifiants dans la console Google, puis relancez l'ajout |
| « L'autorisation a été refusée ou annulée chez le fournisseur » | consentement annulé, ou compte absent des utilisateurs test | autorisez avec un compte autorisé, ou publiez l'application |
| « l'API Google Drive n'est pas activée… » | étape 2 oubliée | activez l'API Drive, puis relancez l'ajout |

Toutes ces erreurs interrompent l'ajout **sans rien enregistrer** : il suffit de corriger la
cause et de relancer « Ajouter une destination ». Aucun redémarrage de Home Assistant n'est
nécessaire.

## Durée de vie de l'autorisation

Le jeton d'accès de Google expire au bout d'une heure ; Auto Backup le renouvelle tout seul
avec le jeton de rafraîchissement, avant chaque opération. Vous n'avez rien à faire.

L'accès peut malgré tout être révoqué :

- vous l'avez retiré depuis [votre compte Google](https://myaccount.google.com/permissions) ;
- l'application est restée en mode « Test » et les 7 jours sont écoulés ;
- le secret client a été régénéré, ou l'application supprimée du projet.

Home Assistant crée alors un **problème** nommant la destination concernée — qu'il ait
constaté le refus en renouvelant le jeton ou que Drive ait rejeté un jeton pourtant valide
(HTTP 401) ; les autres destinations continuent de fonctionner. Pour la remettre en service : options d'Auto Backup,
**Ré-autoriser une destination**, puis la destination en question. Ses réglages (nom, dossier,
rétention) sont conservés, seul l'accès est renouvelé.

## Supprimer la destination

Depuis les options, **Supprimer une destination**. La configuration, les identifiants et le
jeton sont effacés de Home Assistant. **Les sauvegardes déjà déposées sur Drive ne sont pas
supprimées** : retirez-les à la main si vous le souhaitez. Pensez aussi à retirer l'accès depuis
[les autorisations de votre compte Google](https://myaccount.google.com/permissions).

## Ce qui n'est pas encore disponible

La connexion du compte et le **dépôt des sauvegardes** sont disponibles. Reste à venir
[#15](https://github.com/ldb2000/auto-backup-extension/issues/15) : **lister et supprimer** les
sauvegardes déjà déposées sur Drive.

**Conséquence sur la rétention distante.** La rétention distante existe (issue #9) et le champ
est bien enregistré pour une destination Google Drive, mais elle **ne peut pas encore
s'appliquer** ici : supprimer suppose de lister d'abord, et c'est précisément ce que #15 apporte.
Tant qu'elle manque, la purge d'une destination Google Drive s'arrête au listage et se contente
d'une ligne dans le journal :

```text
Purge distante de « Mon Drive » abandonnée : listage impossible
(le listage des sauvegardes Google Drive n'est pas encore implémenté, voir l'issue #15)
```

Ce message est attendu et sans danger : rien n'est supprimé, ni sur Drive, ni en local.

En attendant, les sauvegardes déposées s'accumulent dans le dossier distant : surveillez l'espace
disponible de votre compte Google, ou faites le ménage à la main de temps en temps.

Une destination configurée aujourd'hui profitera de ces ajouts sans rien reconfigurer : la
rétention que vous réglez dès maintenant s'appliquera dès que #15 sera livrée.
