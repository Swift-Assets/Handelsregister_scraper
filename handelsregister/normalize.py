"""Deterministic normalisation of register identifiers (A02-T06 / A02-T07).

Pure functions, no I/O. The DB's swift_v2.norm_registry_number strips every
non-alphanumeric character — which turns "HR B 5407" into "HRB5407" and
makes the identity key differ from the real register number. v2 below
extracts the FIRST register number (digits + optional court suffix of up to
3 letters, e.g. Flensburg "FL", Kiel "KI") after removing a leading
register-type token, and returns None for non-numbers.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata

_TYPE_PREFIX = re.compile(r"^\s*(?:HR\s*[AB]|H\s*R\s*[AB]|HRA|HRB|HB|GNR|GSR|VR|PR|[AB])\s*[:.]?\s*(?=\d)", re.I)
_NUMBER = re.compile(r"(\d[\d ]{0,9}\d|\d)\s*([A-Za-z]{1,3})?")


def norm_registry_number_v2(raw: str | None) -> str | None:
    """First register number in ``raw``; None when there is none."""
    if not raw:
        return None
    text = str(raw).strip()
    text = _TYPE_PREFIX.sub("", text)
    m = _NUMBER.search(text)
    if not m:
        return None
    digits = re.sub(r"\s+", "", m.group(1))
    suffix = (m.group(2) or "").upper()
    # a suffix that is really the next word ("und", "ehem") is not a court suffix
    if suffix and suffix.lower() in {"und", "bis", "ehe", "ag", "am"}:
        suffix = ""
    return digits + suffix


REGISTER_TYPES = ("HRA", "HRB", "GNR", "GSR", "PR", "VR")


def norm_registry_type(raw: str | None) -> str | None:
    if not raw:
        return None
    t = re.sub(r"[^A-Za-z]", "", str(raw)).upper()
    return t if t in REGISTER_TYPES else None


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


_ALIAS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "court_aliases.json")


def load_court_aliases(path: str = _ALIAS_PATH) -> dict[str, str]:
    """announcement court name (folded) -> portal option label."""
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return {}
    return {_fold(k): v for k, v in raw.items()}


def portal_court_label(court: str | None, portal_options: list[str],
                       aliases: dict[str, str] | None = None) -> str | None:
    """Map a court name as printed in an insolvency announcement to the
    label used in the portal's Registergericht select.

    Order: explicit alias → exact folded match → the single option whose
    folded label is a prefix of the folded court (Freiburg im Breisgau →
    Freiburg) or vice versa. Ambiguity (0 or >1 candidates) → None: the
    caller records search_no_result with reason court_unmapped instead of
    guessing."""
    if not court:
        return None
    aliases = aliases if aliases is not None else load_court_aliases()
    folded = _fold(court)
    if folded in aliases:
        return aliases[folded]
    by_fold = {_fold(o): o for o in portal_options}
    if folded in by_fold:
        return by_fold[folded]
    cands = [o for f, o in by_fold.items()
             if f and (folded.startswith(f + " ") or f.startswith(folded + " ") or f == folded.split(" ")[0])]
    if len(cands) == 1:
        return cands[0]
    return None
