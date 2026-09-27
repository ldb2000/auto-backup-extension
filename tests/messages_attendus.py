"""Messages traduits attendus des échecs de destination (issue #46).

Les tests comparent ce que l'utilisateur lit — champ `error` de l'événement,
détail de l'abandon `echec_fournisseur`, attribut `last_error`, cause d'une
notification — au texte des fichiers de traduction, plutôt qu'à une copie
retapée : une retouche de formulation ne casse ainsi que les tests qui la
concernent.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

from custom_components.auto_backup.destinations.errors import (
    CodeErreur,
    cle_de_traduction_de_l_erreur,
)

TRADUCTIONS = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "auto_backup"
    / "translations"
)


@cache
def _traduction(langue: str) -> dict[str, Any]:
    return json.loads((TRADUCTIONS / f"{langue}.json").read_text(encoding="utf-8"))


def message_d_erreur(code: CodeErreur | str, langue: str = "en") -> str:
    """Message traduit d'un code d'erreur, tel que le fork l'affiche.

    L'anglais par défaut : c'est la langue d'une instance de test.
    """
    cle = cle_de_traduction_de_l_erreur(code)
    return _traduction(langue)["exceptions"][cle]["message"]
