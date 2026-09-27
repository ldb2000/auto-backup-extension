"""Point unique de masquage des secrets du fork.

Tout texte venant d'un fournisseur — cause d'un échec au premier chef — peut
contenir un jeton : les API recopient volontiers la requête refusée, en-tête
`Authorization` compris, dans leur message d'erreur. Le fork ne contrôle pas ce
texte, et l'affiche pourtant à des endroits **durables et lisibles par tous** :
une notification persistante (#17), un attribut d'entité recopié dans chaque
état historisé par l'enregistreur (#16), le journal (#35), le champ `error` de
l'événement `auto_backup.upload_failed` (#44). Ce dernier est masqué dès
l'émission, puis de nouveau par les notifications et les entités qui le
lisent : `masquer()` est idempotent, et ce double passage n'y change rien.

Le masquage vivait en deux exemplaires, aux couvertures différentes ; c'était
une faille en soi, l'un laissant passer ce que l'autre arrêtait. Ce module est
désormais **le seul** : `masquer()` porte l'union des deux, et tout code du fork
qui affiche un texte non maîtrisé l'appelle.

Les passes vont de la plus précise à la plus générale :

1. **adresses électroniques** — donnée personnelle, masquée entièrement ;
2. **en-têtes** `Bearer` et `Basic` ;
3. **affectations d'une clé sensible** (`access_token=…`,
   `"client_secret": "…"`, `upload_id=…`, `accessToken: …`, `tokens=[…]`), y
   compris quand le fournisseur écrit simplement « invalid token sl.xxx »,
   séparateur espace ;
4. **formes connues** de jetons, masquées même nues, sans clé ni en-tête autour :
   Dropbox (`sl.`), Google (`ya29.`, `1//`) ;
5. **chemins de fichiers absolus** des racines usuelles d'une instance Home
   Assistant, d'un conteneur ou d'un Supervisor (`/config/…`, `/backup/…`) : ils
   révèlent l'arborescence de l'hôte et le nom des sauvegardes ;
6. **suites opaques** d'au moins `LONGUEUR_MIN_SUITE_OPAQUE` caractères qui ne
   ressemblent pas à un mot — dernier filet pour un secret sans clé ni forme
   reconnaissable. C'est la seule passe qu'un appelant peut écarter
   (`dernier_filet=False`, cf. `masquer_un_nom()`). Elle épargne les **codes
   d'erreur connus des fournisseurs** (`CODES_D_ERREUR_CONNUS`, #48), et eux
   seuls : la suite entière doit être égale, caractère pour caractère, à l'un
   d'eux — jamais un motif, un préfixe ni une comparaison sans casse.

Vient enfin la **troncature** facultative (`longueur_max`), pour un affichage
qui ne peut pas accueillir une trace de pile complète.

**Le journal passe aussi par ici (#35).** Tout appel de journalisation du fork
qui relaie un texte non maîtrisé — message d'une exception, motif d'erreur d'un
fournisseur, valeur d'en-tête reçue — l'enveloppe dans `masquer()`. Une erreur
**inattendue** ne se journalise pas par `_LOGGER.exception()` : la trace
standard recopie le message brut de chaque exception de la chaîne, or celui
d'une erreur `aiohttp` porte l'URL de la requête — pour un envoi reprenable
Google Drive, l'URI de session et son `upload_id`, qui suffit à écrire dans le
compte. `journaliser_une_exception()` journalise à la place le type et le
message **masqués** en `error`, puis, en `debug` seulement, une trace dont les
cadres sont intacts (fichier, ligne, fonction, ligne de code : rien d'autre que
l'emplacement du code) et dont chaque message d'exception est masqué. Le
diagnostic reste possible, le secret ne passe à aucun niveau.

**Un nom de la configuration du fork n'est pas un texte de fournisseur.** Les
passes 1 à 5 reconnaissent un secret à sa forme ; la passe 6 ne reconnaît qu'une
suite longue sans espace, et dévore donc les noms parfaitement ordinaires que
l'utilisateur donne à ses destinations et à ses sauvegardes dès qu'ils n'ont pas
d'espace (« Dropbox-Compte-Familial », « sauvegarde-complete-2026-09-26 »). Or
ces noms sont de la **configuration du fork** : le problème Home Assistant de
`destinations/reauth.py`, le journal et les listes du menu d'options les
affichent déjà en clair. Les masquer ne protège donc rien, et retire à
l'utilisateur le seul moyen de savoir **laquelle** de ses destinations a lâché.
`masquer_un_nom()` leur applique les passes 1 à 5 seulement ; le dernier filet
reste pour la cause d'un échec, seul texte affiché que le fork ne maîtrise pas.

**Réserves assumées.** Le masquage est volontairement large : mieux vaut masquer
un code d'erreur HTTP (« code=500 » devient « code=*** ») que laisser fuir un
jeton de rafraîchissement. Il épargne en revanche le mot ordinaire qui suit un
mot-clé séparé par une simple espace (« token expiré », « code de la
sauvegarde ») : le masquer n'aurait rien protégé et aurait rendu illisibles les
messages en français. Restent quatre angles morts connus et acceptés :

- **Base64 standard.** Un secret contenant `/`, `=` ou `.` est découpé par ces
  caractères, exclus du jeu de la passe 6 pour ne pas masquer les URL. Il peut
  donc n'être masqué que **partiellement, voire pas du tout** si ses tronçons
  font chacun moins de `LONGUEUR_MIN_SUITE_OPAQUE` caractères : les deux
  tronçons de quinze caractères de `b64secretchunk1/b64secretchunk2==` sortent
  intacts. Non exploitable aujourd'hui : Dropbox et Google émettent du base64url
  (`-` et `_`, jamais `/`), et leurs formes sont déjà couvertes par la passe 4.
- **Secret court hors de portée d'un mot-clé.** Une valeur de moins de vingt
  caractères voisine d'un mot-clé **absent** de `_CLES_SENSIBLES`
  (« session id sess_AbCdEf123456 expired ») ou séparée d'un mot-clé listé par
  un mot intercalé (« secret is <valeur> ») ne déclenche aucune passe et fuit
  intégralement. Les motifs ne sont **pas** étendus pour tolérer des mots
  intercalés : cela multiplierait les faux positifs sur les phrases françaises
  que la réserve ci-dessus protège justement, pour un gain nul sur les deux
  seuls fournisseurs intégrés, dont les jetons sont longs et reconnus par leur
  forme. À revoir en même temps que l'ajout d'un fournisseur aux jetons courts.
- **Mot interminable.** La passe 6 masque toute suite de
  `LONGUEUR_MIN_SUITE_OPAQUE` caractères ou plus qui mêle casses, chiffres ou
  `_+` : un mot français à capitale initiale (« Anticonstitutionnellement »),
  cas théorique, ou un code d'erreur de fournisseur **absent** de
  `CODES_D_ERREUR_CONNUS`. Les codes de la liste, eux, restent lisibles depuis
  #48 dans le journal (`expired_access_token`, `userRateLimitExceeded`). Le
  champ `error` de `auto_backup.upload_failed` ne les porte plus depuis #46 :
  il est le message traduit du code stable du fork, `error_code`, sur lequel
  une automatisation filtre. La liste est exacte et fermée : un code nouveau
  d'un fournisseur reste masqué jusqu'à ce qu'on l'y ajoute, source à l'appui.
  Cette réserve ne porte que sur la cause d'un échec : les noms passent par
  `masquer_un_nom()`, hors de portée de cette passe.
- **Caractère non ASCII dans une suite opaque (#54).** La passe 6 ne
  reconnaît une suite que sur l'alphabet ASCII `[A-Za-z0-9_+-]`. Un caractère
  non ASCII — un homoglyphe cyrillique (U+0430) glissé dans un secret, par
  exemple — la découpe en morceaux qui, s'ils font chacun moins de
  `LONGUEUR_MIN_SUITE_OPAQUE` caractères, échappent au dernier filet : la
  suite sort intacte. Non exploitable aujourd'hui : les jetons des
  fournisseurs intégrés sont en ASCII et reconnus par leur forme (passe 4), et
  aucun secret n'est fabriqué par un tiers — le texte masqué vient du
  fournisseur, qui n'a aucune raison d'y glisser un homoglyphe. Le motif n'est
  **pas** élargi à l'Unicode, ni le texte normalisé avant masquage : le
  français accentué sans espace de vingt caractères ou plus
  (« sauvegarde-planifiée-échouée »), que ses accents coupent aujourd'hui,
  deviendrait une suite opaque et serait masqué. À rouvrir seulement avec un
  fournisseur dont les jetons sortent de l'ASCII.

**Pourquoi la liste blanche ne rouvre aucune fuite.** Elle n'agit qu'en passe
6, sur une suite que les passes précédentes n'ont pas déjà remplacée : la
valeur d'une clé sensible (`access_token=expired_access_token`, passe 3) et un
jeton reconnaissable (`sl.…`, passe 4) restent masqués même s'ils contenaient
ou valaient un code. Chaque code est un identifiant documenté, composé de mots
anglais, sans chiffre : il ne peut pas coïncider avec un jeton réel, qui est
aléatoire et porte des chiffres ou un préfixe (`sl.`, `ya29.`, `1//`). Et la
comparaison porte sur la suite **entière** : un secret qui commencerait ou
finirait par un code (`expired_access_tokenAbC123`) reste masqué en entier.
"""

from __future__ import annotations

import logging
import re
import traceback
from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from .models import VALEUR_MASQUEE

# Longueur à partir de laquelle une suite de caractères sans espace est tenue
# pour un secret potentiel plutôt que pour un mot : aucun mot de la langue
# courante n'atteint cette longueur en mêlant chiffres, casses ou `_+`.
LONGUEUR_MIN_SUITE_OPAQUE = 20

# Clés dont la valeur est masquée. Les plus longues viennent en premier :
# l'alternative d'une expression régulière est évaluée de gauche à droite, et
# `token` ne doit pas l'emporter sur `refresh_token`. `[_-]?` rend le séparateur
# facultatif, ce qui couvre du même coup les formes `accessToken` et
# `access-token` des API et des en-têtes.
#
# `upload_id` en fait partie : l'identifiant d'une session de téléversement
# reprenable (Google Drive) vaut jeton de reprise pour qui le connaît — l'URL de
# session suffit à écrire dans le compte, sans autre justificatif.
_CLES_SENSIBLES = (
    r"refresh[_-]?token",
    r"client[_-]?secret",
    r"authorization",
    r"access[_-]?token",
    r"client[_-]?id",
    r"upload[_-]?id",
    r"id[_-]?token",
    r"password",
    r"api[_-]?key",
    r"secret",
    r"token",
    r"code",
)

# Bornes d'un mot-clé sensible. `\b` ne conviendrait pas : `_` est un caractère
# de mot, et `authorization_code=...` doit être reconnu sur sa clé `code`.
_DEBUT_DE_CLE = r"(?<![A-Za-z0-9])"
_FIN_DE_CLE = r"(?![A-Za-z0-9])"

# Affixes alphanumériques absorbés dans la clé : un fournisseur écrit aussi bien
# `dropboxToken=…` que `tokens=[…]`, et ces formes doivent être reconnues. Le
# prix est une sur-couverture (« tokenizer: python » devient
# « tokenizer: *** ») : une perte de diagnostic, jamais une fuite.
_AFFIXE_DE_CLE = r"[A-Za-z0-9]*"

# Caractères qu'on ne trouve pas dans un mot, mais bien dans un jeton encodé.
_CARACTERES_DE_JETON = "_+"

# Une adresse électronique dans un message de fournisseur (« compte
# jean.dupont@example.com non autorisé ») est une donnée personnelle : elle n'a
# pas sa place dans une notification ni dans l'historique d'états.
_MOTIF_EMAIL = re.compile(r"(?i)[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")

# « Bearer <jeton> », « Basic <identifiants> » : l'en-tête d'autorisation tel
# qu'un fournisseur le renvoie parfois dans son message d'erreur.
_MOTIF_PORTEUR = re.compile(r"(?i)\b(bearer|basic)\s+\S+")

# `access_token=...`, `"client_secret": "..."`, `token: ...`, mais aussi
# `invalid token sl.xxx` : la valeur est masquée, la clé conservée pour que le
# message reste diagnosticable. La valeur peut être une chaîne entre
# guillemets, une liste ou un objet JSON entier (`"token": {"access": "..."}`,
# `tokens=[...]`) — sans quoi seul son premier élément serait masqué et le reste
# de la collection fuirait — ou, à défaut, une suite jusqu'au prochain séparateur.
_MOTIF_AFFECTATION = re.compile(
    r"(?i)(?P<cle>"
    + _DEBUT_DE_CLE
    + _AFFIXE_DE_CLE
    + r"(?:"
    + "|".join(_CLES_SENSIBLES)
    + r")"
    + _AFFIXE_DE_CLE
    + _FIN_DE_CLE
    + r")"
    r"(?P<separateur>\"?\s*[:=]\s*|\s+)"
    r"(?P<valeur>\"[^\"\n]*\"|'[^'\n]*'|\[[^\]\n]*\]|\{[^}\n]*\}"
    r"|[^\s,;&)\]}]+)"
)

# Jetons reconnaissables à leur seule forme, donc masqués même nus, sans clé ni
# en-tête autour : un fournisseur écrit volontiers « invalid access token
# sl.B1a2… » ou recopie l'URL de session qui porte le jeton.
_MOTIFS_DE_JETON = (
    re.compile(r"sl\.[A-Za-z0-9_-]{15,}"),  # Dropbox, jeton court ou durable
    re.compile(r"ya29\.[A-Za-z0-9_-]{10,}"),  # Google, jeton d'accès
    re.compile(r"1//[A-Za-z0-9_-]{10,}"),  # Google, jeton de rafraîchissement
)

# Racines de chemins absolus : `/config/...` d'une instance Home Assistant, mais
# aussi les autres racines usuelles d'un conteneur, d'un Supervisor ou d'un
# poste de développement.
_RACINES_DE_CHEMIN = (
    "config",
    "backup",
    "backups",
    "share",
    "media",
    "ssl",
    "addons",
    "addon_configs",
    "data",
    "root",
    "home",
    "tmp",
    "var",
    "usr",
    "etc",
    "opt",
    "srv",
    "mnt",
    "Users",
)

# Caractères qui terminent un chemin dans une phrase : un chemin cité dans un
# message d'erreur est borné par une espace, une ponctuation ou un guillemet.
_DELIMITEURS_DE_CHEMIN = r"\s,;\"'()<>»"

# Le préfixe négatif évite de mutiler le chemin d'une URL (`https://hôte/config`),
# qui n'est pas un chemin local et dont le diagnostic a besoin. `~` n'y figure
# pas : `~/backups/nuit.tar` est bien un chemin local, et doit être masqué.
#
# Les racines sont triées de la plus longue à la plus courte, et la suivante doit
# être un séparateur : sinon `backup` l'emporterait sur `backups` et
# `/backups/nuit.tar` ne serait masqué qu'en partie, laissant fuir le nom de la
# sauvegarde (`***s/nuit.tar`). Une racine inconnue (`/backupfoo`) n'est pas
# masquée du tout, plutôt que masquée à moitié.
_MOTIF_CHEMIN = re.compile(
    r"(?<![\w./-])/(?:"
    + "|".join(sorted(_RACINES_DE_CHEMIN, key=len, reverse=True))
    + r")"
    r"(?=[/" + _DELIMITEURS_DE_CHEMIN + r"]|$)"
    r"(?:/[^" + _DELIMITEURS_DE_CHEMIN + r"]*)*"
)

# Dernier filet : toute suite opaque assez longue pour être un secret. `/`, `=`
# et `.` sont exclus du jeu de caractères, donc coupent la suite : sans quoi un
# chemin d'URL entier (« …googleapis.com/upload/drive/v3/files ») serait masqué
# et le message perdrait tout intérêt de diagnostic.
_MOTIF_SUITE_OPAQUE = re.compile(
    r"[A-Za-z0-9_+-]{" + str(LONGUEUR_MIN_SUITE_OPAQUE) + r",}"
)

# Codes d'erreur connus des fournisseurs, épargnés par la passe 6 (#48). Ce ne
# sont pas des secrets mais le nom de la cause : un jeton expiré, un quota
# épuisé, une limitation de débit. Les masquer privait une automatisation du
# seul moyen de distinguer ces causes dans le champ `error` de
# `auto_backup.upload_failed`.
#
# Liste **exacte** : la suite opaque trouvée par la passe 6 doit être égale,
# casse comprise, à l'un de ces codes. Un code n'entre ici que s'il figure dans
# la documentation du fournisseur, citée en regard. Les codes de moins de
# `LONGUEUR_MIN_SUITE_OPAQUE` caractères n'atteignent pas la passe 6 ; ils sont
# listés quand le fork les reconnaît, pour que la liste reste la référence des
# codes lisibles.
CODES_D_ERREUR_CONNUS: Final[Mapping[str, str]] = MappingProxyType(
    {
        # --- Dropbox API v2 (spécification officielle, dépôt
        # github.com/dropbox/dropbox-api-spec) ---
        # Jeton d'accès expiré : `auth.stone`, union `AuthError`. Recopié par
        # `_erreur_d_acces()` du fournisseur Dropbox (HTTP 401).
        "expired_access_token": "Dropbox API v2, auth.AuthError",
        # Jeton d'accès invalide ou révoqué : `auth.stone`, union `AuthError`.
        "invalid_access_token": "Dropbox API v2, auth.AuthError",
        # Trop d'écritures simultanées dans le compte : `auth.stone`, union
        # `RateLimitReason` ; `files.stone`, unions `WriteError` et
        # `UploadSessionFinishError`.
        "too_many_write_operations": (
            "Dropbox API v2, auth.RateLimitReason, files.WriteError, "
            "files.UploadSessionFinishError"
        ),
        # Espace saturé : `files.stone`, union `WriteError`. Reconnu par le
        # fournisseur Dropbox (`MOTIF_ESPACE`).
        "insufficient_space": "Dropbox API v2, files.WriteError",
        # --- Google Drive API v3 (guide « Resolve errors »,
        # developers.google.com/workspace/drive/api/guides/handle-errors),
        # champ `error.errors[].reason` ---
        # Limitation de débit par utilisateur (403 ou 429). Réessayée par
        # l'envoi Google Drive (`RAISONS_REESSAYABLES`).
        "userRateLimitExceeded": "Google Drive API, guide Resolve errors",
        # Quota de stockage du compte épuisé (403). Reconnu par le fournisseur
        # Google Drive (`RAISONS_DE_QUOTA`).
        "storageQuotaExceeded": "Google Drive API, guide Resolve errors",
        # Plafond de création d'éléments du compte atteint (403).
        "activeItemCreationLimitExceeded": "Google Drive API, guide Resolve errors",
        # Droits insuffisants sur le fichier (403).
        "insufficientFilePermissions": "Google Drive API, guide Resolve errors",
        # Application non autorisée sur le fichier, cas de la portée
        # `drive.file` qu'utilise le fork (403).
        "appNotAuthorizedToFile": "Google Drive API, guide Resolve errors",
        # Trop d'éléments dans un même dossier, hors racine (403) : le dossier
        # des sauvegardes peut l'atteindre.
        "numChildrenInNonRootLimitExceeded": "Google Drive API, guide Resolve errors",
    }
)


def masquer(
    texte: str, *, longueur_max: int | None = None, dernier_filet: bool = True
) -> str:
    """Remplace par `***` ce qu'un affichage ne doit jamais montrer.

    Le masquage est **défensif** : il porte sur un texte que le fork ne contrôle
    pas. Les messages du fork, eux, ne citent déjà aucun secret (cf.
    `docs/adr/0001-destinations-distantes.md`). Mieux vaut masquer un mot de
    trop qu'afficher un jeton dans une notification ou un attribut d'entité que
    l'utilisateur recopiera dans un ticket d'assistance.

    `longueur_max`, s'il est fourni, borne le résultat : le texte est coupé et
    terminé par une ellipse.

    `dernier_filet=False` écarte la seule passe 6, celle des suites opaques, et
    n'a de sens que pour un nom venu de la configuration du fork : passer par
    `masquer_un_nom()`, qui porte ce raisonnement. Les passes et les réserves
    assumées sont décrites en tête de module.
    """
    masque = _MOTIF_EMAIL.sub(VALEUR_MASQUEE, texte)
    masque = _MOTIF_PORTEUR.sub(
        lambda trouve: f"{trouve.group(1)} {VALEUR_MASQUEE}", masque
    )
    masque = _MOTIF_AFFECTATION.sub(_masquer_l_affectation, masque)
    for motif in _MOTIFS_DE_JETON:
        masque = motif.sub(VALEUR_MASQUEE, masque)
    masque = _MOTIF_CHEMIN.sub(VALEUR_MASQUEE, masque)
    if dernier_filet:
        masque = _MOTIF_SUITE_OPAQUE.sub(_masquer_la_suite_opaque, masque)
    if longueur_max is not None and len(masque) > longueur_max:
        masque = masque[: longueur_max - 1].rstrip() + "…"
    return masque


def masquer_un_nom(texte: str, *, longueur_max: int | None = None) -> str:
    """Masque un nom de destination ou de sauvegarde, sans le dernier filet.

    Un nom est de la **configuration du fork**, pas un texte de fournisseur : il
    est déjà affiché en clair par le problème Home Assistant d'une destination à
    ré-autoriser, par le journal et par les listes du menu d'options. Les passes
    1 à 5 restent appliquées — un utilisateur peut avoir collé une adresse
    électronique, un chemin ou un jeton dans un nom — mais la passe 6 est
    écartée : elle réduisait à `***` tout nom de vingt caractères ou plus sans
    espace (« Dropbox-Compte-Familial »), sans rien protéger, et l'utilisateur
    ne pouvait plus dire quelle destination avait lâché quand il en a deux.
    """
    return masquer(texte, longueur_max=longueur_max, dernier_filet=False)


def decrire_l_exception(erreur: BaseException) -> str:
    """« Type: message » d'une exception, son message passé par `masquer()`.

    Le nom de la classe est du code, pas un texte de fournisseur : il reste
    lisible. Une exception sans message (`TimeoutError()`) se réduit à son type.
    """
    nom = type(erreur).__name__
    try:
        message = str(erreur)
    except Exception:  # un `__str__` défaillant ne doit rien casser
        return f"{nom}: <message illisible>"
    return f"{nom}: {masquer(message)}" if message else nom


# Liens entre deux exceptions d'une même chaîne, repris de la trace standard pour
# qu'un développeur y retrouve ses repères.
_LIEN_CAUSE = "The above exception was the direct cause of the following exception:"
_LIEN_CONTEXTE = "During handling of the above exception, another exception occurred:"


def trace_masquee(erreur: BaseException) -> str:
    """Trace de pile d'une exception, chaque message d'exception masqué.

    Même forme que la trace standard (`traceback.format_exception`), chaîne
    `__cause__` / `__context__` comprise, du plus ancien au plus récent. Les
    cadres sont repris tels quels : fichier, numéro de ligne, fonction et ligne
    de code source ne sont que l'emplacement du code, jamais une donnée reçue —
    les variables locales ne sont pas capturées. Seuls les messages des
    exceptions, et leurs notes, portent un texte non maîtrisé : ils passent par
    `masquer()`.
    """
    chaine: list[tuple[BaseException, str | None]] = []
    vues: set[int] = set()
    courante: BaseException | None = erreur
    lien: str | None = None
    while courante is not None and id(courante) not in vues:
        vues.add(id(courante))
        chaine.append((courante, lien))
        if courante.__cause__ is not None:
            courante, lien = courante.__cause__, _LIEN_CAUSE
        elif courante.__context__ is not None and not courante.__suppress_context__:
            courante, lien = courante.__context__, _LIEN_CONTEXTE
        else:
            courante = None

    lignes: list[str] = []
    for exception, lien_vers_la_suivante in reversed(chaine):
        if exception.__traceback__ is not None:
            lignes.append("Traceback (most recent call last):\n")
            lignes.extend(traceback.format_tb(exception.__traceback__))
        lignes.append(decrire_l_exception(exception) + "\n")
        notes = getattr(exception, "__notes__", None)
        if isinstance(notes, list | tuple):
            lignes.extend(f"{masquer(str(note))}\n" for note in notes)
        if lien_vers_la_suivante is not None:
            lignes.append(f"\n{lien_vers_la_suivante}\n\n")
    return "".join(lignes).rstrip("\n")


def journaliser_une_exception(
    journal: logging.Logger, erreur: BaseException, message: str, *arguments: object
) -> None:
    """Journalise une erreur inattendue sans jamais relayer son texte brut.

    Remplace `journal.exception(message, *arguments)`, dont la trace recopie
    le message brut de chaque exception de la chaîne. Le message du fork est
    journalisé en `error`, suivi du type et du message **masqués** de l'erreur ;
    la trace, masquée elle aussi, n'est produite qu'en `debug` : elle sert au
    développeur, pas à l'utilisateur qui consulte son journal.
    """
    journal.error(f"{message} : %s", *arguments, decrire_l_exception(erreur))
    if journal.isEnabledFor(logging.DEBUG):
        journal.debug(
            "Trace de l'erreur ci-dessus, messages masqués :\n%s",
            trace_masquee(erreur),
        )


def _est_un_mot_ordinaire(valeur: str) -> bool:
    """Vrai si la valeur est un mot de la langue, jamais un secret."""
    return valeur.isalpha() and len(valeur) < LONGUEUR_MIN_SUITE_OPAQUE


def _ressemble_a_un_secret(valeur: str) -> bool:
    """Vrai si une suite longue a l'allure d'un secret plutôt que d'un mot.

    Un mot, même long et composé, reste d'une seule casse et sans chiffre ;
    un jeton encodé mêle presque toujours chiffres, majuscules et minuscules,
    ou porte les caractères propres au base64 (`_`, `+`).
    """
    return (
        any(caractere.isdigit() for caractere in valeur)
        or any(caractere in _CARACTERES_DE_JETON for caractere in valeur)
        or (not valeur.islower() and not valeur.isupper())
    )


def _masquer_l_affectation(trouve: re.Match[str]) -> str:
    """Remplace la valeur d'une clé sensible, en gardant la forme du message."""
    valeur = trouve["valeur"]
    if trouve["separateur"].isspace() and _est_un_mot_ordinaire(valeur):
        # « token expiré », « code de la sauvegarde » : le mot qui suit le
        # mot-clé n'est pas la valeur d'un secret, seulement du français.
        return trouve[0]
    guillemet = valeur[0] if valeur[:1] in {'"', "'"} else ""
    return (
        f"{trouve['cle']}{trouve['separateur']}{guillemet}{VALEUR_MASQUEE}{guillemet}"
    )


def _masquer_la_suite_opaque(trouve: re.Match[str]) -> str:
    """Masque une suite longue, sauf mot interminable ou code connu (#48).

    Le code n'est épargné que si la suite **entière** lui est égale : un
    préfixe, un suffixe ou une casse différente suffit à la masquer.
    """
    valeur = trouve[0]
    if valeur in CODES_D_ERREUR_CONNUS:
        return valeur
    return VALEUR_MASQUEE if _ressemble_a_un_secret(valeur) else valeur
