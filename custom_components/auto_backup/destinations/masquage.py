"""Point unique de masquage des secrets du fork.

Tout texte venant d'un fournisseur — cause d'un échec au premier chef — peut
contenir un jeton : les API recopient volontiers la requête refusée, en-tête
`Authorization` compris, dans leur message d'erreur. Le fork ne contrôle pas ce
texte, et l'affiche pourtant à des endroits **durables et lisibles par tous** :
une notification persistante (#17), un attribut d'entité recopié dans chaque
état historisé par l'enregistreur (#16), le journal.

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
   reconnaissable.

Vient enfin la **troncature** facultative (`longueur_max`), pour un affichage
qui ne peut pas accueillir une trace de pile complète.

**Réserves assumées.** Le masquage est volontairement large : mieux vaut masquer
un code d'erreur HTTP (« code=500 » devient « code=*** ») que laisser fuir un
jeton de rafraîchissement. Il épargne en revanche le mot ordinaire qui suit un
mot-clé séparé par une simple espace (« token expiré », « code de la
sauvegarde ») : le masquer n'aurait rien protégé et aurait rendu illisibles les
messages en français. Restent trois angles morts connus et acceptés :

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
- **Mot français interminable.** Un mot de vingt caractères ou plus commençant
  par une majuscule est pris pour une suite opaque et masqué.
"""

from __future__ import annotations

import re

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


def masquer(texte: str, *, longueur_max: int | None = None) -> str:
    """Remplace par `***` ce qu'un affichage ne doit jamais montrer.

    Le masquage est **défensif** : il porte sur un texte que le fork ne contrôle
    pas. Les messages du fork, eux, ne citent déjà aucun secret (cf.
    `docs/adr/0001-destinations-distantes.md`). Mieux vaut masquer un mot de
    trop qu'afficher un jeton dans une notification ou un attribut d'entité que
    l'utilisateur recopiera dans un ticket d'assistance.

    `longueur_max`, s'il est fourni, borne le résultat : le texte est coupé et
    terminé par une ellipse. Les passes et les réserves assumées sont décrites
    en tête de module.
    """
    masque = _MOTIF_EMAIL.sub(VALEUR_MASQUEE, texte)
    masque = _MOTIF_PORTEUR.sub(
        lambda trouve: f"{trouve.group(1)} {VALEUR_MASQUEE}", masque
    )
    masque = _MOTIF_AFFECTATION.sub(_masquer_l_affectation, masque)
    for motif in _MOTIFS_DE_JETON:
        masque = motif.sub(VALEUR_MASQUEE, masque)
    masque = _MOTIF_CHEMIN.sub(VALEUR_MASQUEE, masque)
    masque = _MOTIF_SUITE_OPAQUE.sub(_masquer_la_suite_opaque, masque)
    if longueur_max is not None and len(masque) > longueur_max:
        masque = masque[: longueur_max - 1].rstrip() + "…"
    return masque


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
    """Masque une suite longue, sauf si elle n'est qu'un mot interminable."""
    valeur = trouve[0]
    return VALEUR_MASQUEE if _ressemble_a_un_secret(valeur) else valeur
