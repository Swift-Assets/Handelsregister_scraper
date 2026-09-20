"""Deciding whether a search result really is the company we asked about.

The rule the old code used was "accept the only row that has a document link,
or the one whose name matches the entity's exactly". Both are unsafe here: an
HRB number is unique only inside its court, and a company's registered name
differs from the announcement's spelling often enough ("GmbH" vs "mbH", a
comma, a legal-form suffix the court writes and the announcement does not).

So the strong rule is the register triple, which our announcements carry for
98.9 % of company rows. The name is a fallback, used only when there is no
triple to compare and exactly one candidate.
"""

from __future__ import annotations

from dataclasses import dataclass

from .normalize import _fold, norm_registry_number_v2, norm_registry_type

METHOD_TRIPLE = "registry_triple"
METHOD_PORTAL_FILTERED = "portal_filtered_single_hit"
METHOD_NUMBER_AND_NAME = "number_and_name_single_hit"
METHOD_UNIQUE_NAME = "unique_name_no_triple"

# German transliteration BEFORE accent folding: the portal writes "Müller" and
# an announcement may write "Mueller". Stripping the diaeresis alone turns the
# first into "Muller" and the two stop matching, which is the quiet kind of
# miss that looks like "the company is not in the register".
_UMLAUT = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss",
                         "Ä": "ae", "Ö": "oe", "Ü": "ue"})

# The portal prints a court as "<Bundesland> Amtsgericht <Ort>". The state is
# noise on our side, and it is removed by name rather than by position: a rule
# like "drop the first word" would turn "Bad Homburg" into "Homburg", and
# Amtsgericht Homburg is a different court in a different state.
_BUNDESLAENDER = (
    "baden wuerttemberg", "bayern", "berlin", "brandenburg", "bremen",
    "hamburg", "hessen", "mecklenburg vorpommern", "niedersachsen",
    "nordrhein westfalen", "rheinland pfalz", "saarland", "sachsen anhalt",
    "sachsen", "schleswig holstein", "thueringen",
)

# Legal-form noise that neither side writes consistently.
_NAME_NOISE = (
    " gesellschaft mit beschrankter haftung", " gmbh und co kg", " gmbh co kg",
    " gmbh", " mbh", " ug haftungsbeschrankt", " ug", " ag", " kg", " ohg",
    " e k", " e v", " eg", " se", " co kg",
)


@dataclass(frozen=True)
class MatchResult:
    hit: object | None
    method: str | None
    reason: str

    @property
    def accepted(self) -> bool:
        return self.hit is not None


def _fold_de(value: str | None) -> str:
    return _fold(str(value or "").translate(_UMLAUT))


def fold_name(name: str | None) -> str:
    """Fold a company name to something comparable: no case, no punctuation,
    umlauts spelled out, and no legal-form suffix."""
    folded = _fold_de(name)
    changed = True
    while changed:
        changed = False
        for noise in _NAME_NOISE:
            if folded.endswith(noise):
                folded = folded[: -len(noise)].strip()
                changed = True
    return folded


def fold_court(court: str | None) -> str:
    """Fold a court name. 'Amtsgericht' and a leading Bundesland are dropped,
    because one side writes 'Bayern Amtsgericht Aschaffenburg' and the other
    just 'Aschaffenburg'."""
    folded = _fold_de(court)
    for token in ("amtsgericht", "amtsger", " ag "):
        folded = (" " + folded + " ").replace(" " + token.strip() + " ", " ").strip()
    folded = " ".join(folded.split())
    for land in _BUNDESLAENDER:
        if folded.startswith(land + " "):
            folded = folded[len(land) + 1:]
            break
    return folded.strip()


def _triple(court, rtype, number) -> tuple[str, str, str] | None:
    c = fold_court(court)
    t = norm_registry_type(rtype)
    n = norm_registry_number_v2(number)
    if not (c and t and n):
        return None
    return c, t, n


def entity_triple(entity: dict) -> tuple[str, str, str] | None:
    return _triple(entity.get("registry_court"), entity.get("registry_type"),
                   entity.get("registry_number"))


def hit_triple(hit) -> tuple[str, str, str] | None:
    return _triple(getattr(hit, "registry_court", None),
                   getattr(hit, "registry_type", None),
                   getattr(hit, "registry_number", None))


def _courts_agree(a: str, b: str) -> bool:
    """Court names are written differently on the two sides ('Freiburg' vs
    'Freiburg im Breisgau'). Equal, or one a leading word-prefix of the other,
    counts as agreement.

    Deliberately NOT a suffix match: 'Bad Homburg' ends with 'Homburg', and
    Amtsgericht Homburg is a different court in a different state. A rule that
    accepted that would attach a company to the wrong register entry, and the
    fill-only writes downstream freeze a wrong value forever.
    """
    if a == b:
        return True
    return a.startswith(b + " ") or b.startswith(a + " ")


def search_was_constrained(filters: dict | None) -> bool:
    """The strong case: the portal filtered by BOTH court and register number,
    so one row can only be this company.

    Measured 2026-09-20: a result row reads "<name>  <seat city>  aktuell" and
    carries no court and no register number at all. The row can never confirm
    the triple — the FORM can, because court, register type and number are
    search fields.
    """
    f = filters or {}
    return bool(f.get("register_number")) and bool(f.get("court"))


def search_had_a_number(filters: dict | None) -> bool:
    """The weaker case: the number went in but the court did not. A register
    number is not unique across courts, so a single hit here is a CANDIDATE,
    never a conclusion — the document has to confirm it before anything is
    stored."""
    return bool((filters or {}).get("register_number"))


def pick_hit(hits, entity: dict, document_kind: str = "SI",
             search_filters: dict | None = None) -> MatchResult:
    """Choose the row that is this entity, or refuse and say why."""
    usable = [h for h in (hits or []) if getattr(h, "document_links", None)
              and document_kind in h.document_links]
    if not hits:
        return MatchResult(None, None, "no_hits")
    if not usable:
        return MatchResult(None, None, f"no_{document_kind.lower()}_link")

    want = entity_triple(entity)
    if want:
        matches = []
        for h in usable:
            got = hit_triple(h)
            if got and got[1] == want[1] and got[2] == want[2] \
                    and _courts_agree(got[0], want[0]):
                matches.append(h)
        if len(matches) == 1:
            return MatchResult(matches[0], METHOD_TRIPLE, "ok")
        if len(matches) > 1:
            return MatchResult(None, None, "ambiguous_triple")

        # No row states a triple — the normal case on this portal. Fall back to
        # what the portal was asked: a court- and number-constrained query that
        # returned exactly one row, whose name agrees with ours.
        if not any(hit_triple(h) for h in usable):
            if not search_had_a_number(search_filters):
                return MatchResult(None, None, "search_not_constrained")
            if len(usable) > 1:
                return MatchResult(None, None, "ambiguous_portal_filtered")
            agree = _names_agree(entity, usable[0])
            if search_was_constrained(search_filters):
                # Court + register type + number IS the register identity, and
                # the portal answered it with one row. That row is the register
                # entry whatever its name column reads: names drift, are
                # abbreviated, or arrive glued to a city. The disagreement is
                # recorded, and the document still has the last word.
                return MatchResult(usable[0], METHOD_PORTAL_FILTERED,
                                   "ok" if agree else "ok_name_differs")
            if not agree:
                return MatchResult(None, None, "name_mismatch")
            # Number and name only. Good enough to spend one document on;
            # never good enough to store. verify_against_document() decides,
            # and with require_positive it will not accept a silent document.
            return MatchResult(usable[0], METHOD_NUMBER_AND_NAME, "ok")
        return MatchResult(None, None, "no_triple_match")

    # No triple on our side — the ~1 % of companies whose announcement never
    # named a register. One candidate and an agreeing name, or nothing.
    if len(usable) == 1:
        if _names_agree(entity, usable[0]):
            return MatchResult(usable[0], METHOD_UNIQUE_NAME, "ok")
        return MatchResult(None, None, "name_mismatch")
    return MatchResult(None, None, "ambiguous_no_triple")


def _names_agree(entity: dict, hit) -> bool:
    want = fold_name(entity.get("display_name"))
    got = fold_name(getattr(hit, "company_name", ""))
    return bool(want) and bool(got) and want == got


def verify_against_document(entity: dict, profile,
                            require_positive: bool = False) -> tuple[bool, str]:
    """The last word on identity, and the only one that is not circumstantial.

    An SI document states its own Registergericht, Registerart and
    Registernummer. Comparing those with the entity's is proof, where a search
    result is only inference. Returns (ok, reason).

    ``require_positive`` is for the weak match: when the search could not be
    narrowed to a court, a document that says nothing about its own register
    is not permission to store it.
    """
    want = entity_triple(entity)
    got = _triple(getattr(profile, "registergericht", None),
                  getattr(profile, "registerart", None),
                  getattr(profile, "registernummer", None))
    if not want:
        return True, "entity has no triple to compare"
    if not got:
        if require_positive:
            return False, "document states no register entry, and the search "\
                          "was not narrowed to a court — nothing confirms this"
        return True, "document states no triple"
    if got[1] == want[1] and got[2] == want[2] and _courts_agree(got[0], want[0]):
        return True, "document confirms the register entry"
    return False, f"document says {got}, entity says {want}"
