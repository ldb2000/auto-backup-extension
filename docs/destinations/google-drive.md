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
les fichiers que vous avez déposés vous-même. Il ne voit que ce qu'il a créé. C'est la première
des protections de la purge distante : vos propres documents lui sont structurellement invisibles,
et aucune rétention ne peut donc les effacer. En contrepartie, si vous déplacez ou supprimez une
sauvegarde à la main dans Drive, Auto Backup ne la retrouvera pas.

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
   main, le secret n'est plus affiché ensuite. Vous pourrez en régénérer un, mais une
   régénération impose de **supprimer puis ajouter de nouveau** la destination dans Home
   Assistant (voir [Durée de vie de l'autorisation](#durée-de-vie-de-lautorisation)).

Laissez le champ **Origines JavaScript autorisées** vide : Auto Backup n'en utilise pas.

## 5. Ajouter la destination dans Home Assistant

1. **Paramètres > Appareils et services > Auto Backup > Configurer**.
2. Choisissez **Ajouter une destination**, puis **Google Drive**.
3. Collez l'**ID client** de Google dans le champ **Identifiant client**, et le **code secret du
   client** dans le champ **Secret client**. Le secret est masqué à la saisie,
   conservé dans votre entrée de configuration et n'apparaît dans aucun journal.
4. Une fenêtre s'ouvre sur l'écran de consentement Google : choisissez le compte, puis
   autorisez.
5. De retour dans Home Assistant, la destination est pré-nommée d'après le compte autorisé —
   par exemple `Google Drive – Camille Martin`. Ajustez le nom, le **dossier distant**
   (`Sauvegardes/Home Assistant` par exemple) et, si vous le souhaitez, une rétention propre à
   cette destination — une durée en jours, un nombre maximum de sauvegardes, ou les deux. Elle
   s'applique réellement : lisez **« Rétention : ce qui est supprimé, et comment »** ci-dessous
   avant de la régler, les suppressions sur Drive étant définitives.
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
propres documents, qui ne le portent pas, ne peuvent pas être touchés. Auto Backup le relit au
listage — il demande d'ailleurs à Google de ne lui renvoyer *que* les fichiers qui le portent.

### Si le transfert rencontre un incident

- **limitation de débit ou erreur passagère de Google** : l'envoi est retenté jusqu'à trois fois,
  avec un délai croissant, et **reprend exactement là où il s'était arrêté** — les fragments déjà
  reçus ne sont pas renvoyés ;
- **espace de stockage épuisé** : l'envoi s'arrête immédiatement (réessayer n'y changerait rien) et
  l'événement `auto_backup.upload_failed` porte le code `quota_exceeded` et le message
  correspondant ;
- **autorisation révoquée** : la destination est signalée à ré-autoriser, comme décrit plus bas ;
- **dans tous les cas, la sauvegarde locale reste intacte**, et les autres destinations demandées
  sont traitées normalement.

Le **délai maximum** accordé à un téléversement se règle dans les options de l'intégration, entrée
« Réglages du téléversement » (1800 secondes par défaut). Un envoi interrompu par un redémarrage de
Home Assistant n'est pas repris : la sauvegarde est simplement à renvoyer.

## Rétention : ce qui est supprimé, et comment

Si vous avez réglé une rétention sur la destination — une durée en jours, un nombre maximum de
sauvegardes, ou les deux —, Auto Backup fait le ménage dans le dossier distant.

> ### Attention : la suppression est définitive, elle ne passe pas par la corbeille
>
> Une sauvegarde purgée par Auto Backup est **effacée de votre Drive sur-le-champ**. Elle
> n'apparaît pas dans la corbeille, et **aucune récupération n'est possible**, ni depuis Drive, ni
> depuis Home Assistant.
>
> C'est un choix assumé : un fichier mis à la corbeille continue de consommer l'espace de votre
> compte Google pendant trente jours. Votre rétention ne libérerait donc rien pendant un mois —
> exactement l'inverse de ce que vous lui demandez si vous l'avez réglée parce que votre Drive se
> remplit. Le raisonnement complet est dans
> [l'ADR](../adr/0001-destinations-distantes.md#lister-et-supprimer-sur-google-drive-issue-15).
>
> **Conseil : réglez large, puis resserrez.** Commencez par une rétention généreuse (30 jours, ou
> 10 sauvegardes), vérifiez dans le journal ce qui est supprimé, et resserrez ensuite. Un chiffre
> saisi trop bas ne se rattrape pas.

### Ce qui peut être supprimé, et ce qui ne peut jamais l'être

Auto Backup ne supprime un fichier que si **les deux** conditions suivantes sont réunies.

1. **Il l'a déposé lui-même.** La preuve vient du marqueur `auto_backup` porté par le fichier, ou
   du registre que Home Assistant tient de ses propres dépôts. Un document que vous avez déposé
   dans ce dossier n'a ni l'un ni l'autre.
2. **Il dépasse la rétention** que vous avez réglée pour cette destination.

Quatre choses ne sont donc **jamais** touchées, quel que soit leur âge :

- tout fichier que vous avez déposé vous-même dans le dossier — Auto Backup ne le voit même pas,
  la portée `drive.file` le lui interdit ;
- tout fichier du dossier qui ne porte pas le marqueur d'Auto Backup ;
- les fichiers déjà à la corbeille : ils sont ignorés, pas « purgés » une seconde fois ;
- les **sous-dossiers**, y compris ceux qu'Auto Backup a créés pour une autre destination. Si vous
  réglez une destination sur `Sauvegardes` et une autre sur `Sauvegardes/Home Assistant`, la purge
  de la première ne touchera jamais le dossier de la seconde.

Et bien sûr : **le reste de votre Drive est hors d'atteinte**, ainsi que vos sauvegardes locales,
qui suivent la rétention locale de l'intégration (`keep_days`) et non celle de la destination.

> **Important : un dossier par instance.** Le marqueur `auto_backup` ne dit pas quelle instance
> Home Assistant a déposé le fichier. Si plusieurs instances utilisent les mêmes identifiants
> OAuth (le même projet Google Cloud) et le même dossier distant, chacune voit les sauvegardes des
> autres, les compte dans sa rétention et peut les **supprimer définitivement**. **Utilisez un
> dossier distinct pour chaque instance Home Assistant** (`Home Assistant/Maison`,
> `Home Assistant/Chalet`…) : la purge ne descendant jamais dans les sous-dossiers, les instances
> ne se gênent plus.

### Quand la purge s'exécute

- **après chaque sauvegarde téléversée avec succès**, si l'option **purge automatique**
  (`auto_purge`) de l'intégration est active — c'est la même option qui commande la purge locale ;
- **à chaque appel du service `auto_backup.purge`**, pour toutes les destinations configurées.

Une destination sans aucune rétention n'est jamais purgée, et une destination en attente de
ré-autorisation est sautée sans aucun appel réseau.

### Ce que vous voyez dans le journal

Une purge qui supprime quelque chose l'écrit, et émet l'événement `auto_backup.remote_purge` avec
la liste des identifiants supprimés — de quoi bâtir une automatisation de notification :

```text
Purge distante de « Mon Drive » : 2 sauvegarde(s) supprimée(s)
```

Une purge qui ne trouve rien à supprimer ne dit rien et n'émet aucun événement. Une sauvegarde
déjà disparue de Drive (vous l'avez retirée à la main entre-temps) est considérée comme purgée :
elle quitte simplement le registre, avec un avertissement.

### Combien de sauvegardes Auto Backup peut-il lister ?

Le dossier est parcouru page par page, jusqu'à **5 000 sauvegardes**. Au-delà, le listage s'arrête
avec un avertissement dans le journal et la rétention ne s'applique qu'à ce qui a été lu : rien
n'est supprimé par erreur, mais il vous restera du ménage à faire. Aucun usage domestique
n'atteint ce plafond.

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

L'accès peut malgré tout être perdu. Home Assistant crée alors un **problème** nommant la
destination concernée — qu'il ait constaté le refus en renouvelant le jeton ou que Drive ait
rejeté un jeton pourtant valide (HTTP 401) ; les autres destinations continuent de fonctionner.
La façon de la remettre en service dépend de la cause.

**L'autorisation seule est à renouveler** quand :

- vous avez retiré l'accès depuis [votre compte Google](https://myaccount.google.com/permissions) ;
- l'application est restée en mode « Test » et les 7 jours sont écoulés.

Dans ces deux cas : options d'Auto Backup, **Ré-autoriser une destination**, puis la destination
en question. Ses réglages (nom, dossier, rétention) sont conservés, seul l'accès est renouvelé.

**Les identifiants de l'application ont changé** quand le secret client a été régénéré, ou que
l'ID client a été supprimé du projet. La ré-autorisation réutilise l'ID client et le secret
enregistrés : elle échouerait. **Supprimez la destination, puis ajoutez-la de nouveau** avec les
nouveaux identifiants, en reprenant **le même dossier distant**. Si ces nouveaux identifiants
appartiennent à un **autre projet Google Cloud**, la portée `drive.file` ne lui laisse pas voir
les fichiers déposés par l'ancien : les sauvegardes existantes ne seront plus ni listées ni
purgées, retirez-les à la main (voir la [FAQ](../faq.md#home-assistant-me-demande-de-ré-autoriser-une-destination--que-faire-)).

## Supprimer la destination

Depuis les options, **Supprimer une destination**. La configuration, les identifiants et le
jeton sont effacés de Home Assistant. **Les sauvegardes déjà déposées sur Drive ne sont pas
supprimées** : retirez-les à la main si vous le souhaitez. Pensez aussi à retirer l'accès depuis
[les autorisations de votre compte Google](https://myaccount.google.com/permissions).

## Supprimer une sauvegarde à la main

Rien ne l'interdit : retirez le fichier depuis l'interface de Drive. Auto Backup ne le retrouvera
plus au listage suivant et retirera son entrée de son registre sans rien signaler d'alarmant.

L'inverse — **déplacer** une sauvegarde hors du dossier de la destination — la soustrait à la
rétention : Auto Backup ne la voit plus, et ne la supprimera donc jamais. C'est un moyen simple de
mettre une sauvegarde de côté.

## Pour aller plus loin

- [Services, options et rétention distante](../services.md) : toutes les options de service, les
  événements et une automatisation complète de sauvegarde quotidienne envoyée dans le cloud.
- [Questions fréquentes](../faq.md) : taille et quotas, chiffrement, jetons, ré-authentification.
