"""Hiérarchie d'erreurs typées des destinations distantes.

Toutes les erreurs dérivent de `DestinationError`, elle-même dérivée de
`HomeAssistantError` : un appel de service qui remonte l'une d'elles est
présenté à l'utilisateur par Home Assistant sans traitement particulier.

Les appelants peuvent donc :

- attraper `DestinationError` pour traiter n'importe quel échec de destination ;
- attraper une sous-classe pour réagir spécifiquement (relancer une
  authentification, alerter sur un quota, ignorer une sauvegarde déjà purgée).

**Code d'erreur stable (issue #46).** Chaque erreur porte un `code`, valeur de
`CodeErreur`, attribué **par le fork** au moment où il qualifie l'échec — jamais
recopié du fournisseur : Dropbox et Google Drive nomment différemment la même
cause (`insufficient_space`, `storageQuotaExceeded`), le fork les ramène à un
seul code (`quota_exceeded`). Le code sert à deux choses :

- choisir le texte **traduit** affiché à l'utilisateur (section `exceptions`
  de `translations/*.json`, clé `erreur_<code>`), le texte de l'exception, lui,
  ne servant plus qu'au journal, masqué ;
- donner aux automatisations une clé de filtrage stable, le champ `error_code`
  de l'événement `auto_backup.upload_failed`.

Une sous-classe fixe le code par défaut de sa cause (`DestinationAuthError`
→ `access_revoked`) ; un site de levée peut le préciser (`code=`). Une erreur
que rien ne qualifie reste `unknown` : l'utilisateur lit un message générique
traduit, et le détail ne va qu'au journal.

Les valeurs de `CodeErreur` sont un **contrat public** (documenté dans
`docs/services.md`) : on en ajoute, on n'en renomme ni n'en retire aucune.
"""

from __future__ import annotations

from enum import StrEnum
from typing import ClassVar

from homeassistant.exceptions import HomeAssistantError

from ..const import DOMAIN


class CodeErreur(StrEnum):
    """Codes stables des échecs de destination (issue #46).

    Chaque valeur a sa traduction, sous `exceptions.erreur_<valeur>.message`,
    en français et en anglais.
    """

    ACCES_REVOQUE = "access_revoked"
    PORTEE_MANQUANTE = "missing_scope"
    QUOTA_DEPASSE = "quota_exceeded"
    LIMITATION_DE_DEBIT = "rate_limited"
    DELAI_DEPASSE = "timeout"
    RESEAU_INJOIGNABLE = "network_error"
    FOURNISSEUR_EN_PANNE = "provider_unavailable"
    DOSSIER_INVALIDE = "invalid_folder"
    API_DESACTIVEE = "api_disabled"
    INTROUVABLE = "not_found"
    DESTINATION_INCONNUE = "unknown_destination"
    SAUVEGARDE_ILLISIBLE = "local_backup_unreadable"
    CONFIGURATION_INVALIDE = "invalid_config"
    FOURNISSEUR_INCONNU = "unknown_provider"
    INCONNUE = "unknown"


PREFIXE_CLE_ERREUR = "erreur_"


def cle_de_traduction_de_l_erreur(code: CodeErreur | str) -> str:
    """Clé de traduction (section `exceptions`) du message d'un code d'erreur."""
    return f"{PREFIXE_CLE_ERREUR}{code}"


# Toutes les clés de traduction des messages d'erreur, dans l'ordre des codes.
CLES_DES_ERREURS = tuple(cle_de_traduction_de_l_erreur(code) for code in CodeErreur)


class DestinationError(HomeAssistantError):
    """Erreur générique d'une destination distante.

    Le message passé au constructeur est le **détail** technique, en français :
    il n'est destiné qu'au journal, masqué. Ce qu'on montre à l'utilisateur est
    le texte traduit de `code`. La clé de traduction est aussi posée sur
    l'exception (`translation_domain`, `translation_key`) : une erreur qui
    remonterait telle quelle d'un appel de service serait affichée traduite par
    l'interface de Home Assistant.
    """

    code_par_defaut: ClassVar[CodeErreur] = CodeErreur.INCONNUE

    def __init__(self, *args: object, code: CodeErreur | None = None) -> None:
        """Mémorise le détail (`args`) et le code stable de l'échec."""
        self.code: CodeErreur = code or self.code_par_defaut
        super().__init__(
            *args,
            translation_domain=DOMAIN,
            translation_key=cle_de_traduction_de_l_erreur(self.code),
        )


class DestinationAuthError(DestinationError):
    """Le fournisseur refuse les identifiants : jeton expiré ou accès révoqué.

    C'est cette erreur qui déclenchera la ré-authentification côté Home Assistant.
    """

    code_par_defaut = CodeErreur.ACCES_REVOQUE


class DestinationQuotaError(DestinationError):
    """Le fournisseur refuse l'écriture : quota ou espace de stockage épuisé."""

    code_par_defaut = CodeErreur.QUOTA_DEPASSE


class DestinationNotFoundError(DestinationError):
    """La sauvegarde distante visée n'existe pas (ou plus).

    Utile à la purge distante : une sauvegarde supprimée entre le listage et la
    suppression ne doit pas faire échouer le cycle de rétention.
    """

    code_par_defaut = CodeErreur.INTROUVABLE


class DestinationConfigError(DestinationError):
    """La configuration d'une destination est invalide ou incomplète."""

    code_par_defaut = CodeErreur.CONFIGURATION_INVALIDE


class UnknownProviderError(DestinationConfigError):
    """Le fournisseur demandé n'est pas enregistré dans le registre."""

    code_par_defaut = CodeErreur.FOURNISSEUR_INCONNU


class DuplicateProviderError(DestinationError):
    """Un fournisseur est enregistré deux fois sous le même identifiant."""
