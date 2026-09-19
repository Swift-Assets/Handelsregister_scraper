"""Tolerant parser and redactor for the Strukturierter Registerinhalt (SI).

Namespaces and wrapper elements differ between XJustiz versions, so nothing
here matches on a path: every lookup is by LOCAL element name.

Two jobs:

  parse_si()  — pull out what a company profile needs. The one field the
                product actually asked for is ``gegenstand``; the rest arrive
                in the same document at no extra cost.
  redact_si() — produce the bytes we are allowed to keep. Inside every
                natuerlichePerson only the name survives; birth data and
                private addresses are removed. The rule is a WHITELIST, so a
                field a future XJustiz version adds is dropped by default
                rather than kept by accident.
"""

from __future__ import annotations

import hashlib
import io
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

REDACTION_RULE = "person-name-only/v1"

# The only things that may survive inside a natural person.
ALLOWED_PERSON_TAGS = {
    "vollerName", "vorname", "nachname", "namensbestandteil",
    "namenszusatz", "titel", "anrede", "ref.rollennummer",
}
# ...and never these, even nested inside something allowed.
FORBIDDEN_PERSON_TAGS = {
    "anschrift", "wohnort", "kontaktdaten", "telefon", "telefax", "email",
    "staatsangehoerigkeit", "auswahl_geschlecht", "geschlecht", "beruf",
}
_FORBIDDEN_PREFIXES = ("geburt",)


@dataclass
class SiProfile:
    firma: str | None = None
    rechtsform: str | None = None
    sitz: str | None = None
    anschrift: str | None = None
    gegenstand: str | None = None
    stammkapital: str | None = None
    status: str | None = None
    registergericht: str | None = None
    registerart: str | None = None
    registernummer: str | None = None
    euid: str | None = None
    vertretungsberechtigte: list[str] = field(default_factory=list)
    document_hash: str | None = None

    def is_usable(self) -> bool:
        return bool(self.firma) and bool(self.gegenstand or self.anschrift or self.status)

    def has_purpose(self) -> bool:
        return bool(self.gegenstand and self.gegenstand.strip())


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _is_forbidden_person_tag(name: str) -> bool:
    lowered = name.lower()
    return (name in FORBIDDEN_PERSON_TAGS
            or any(lowered.startswith(p) for p in _FORBIDDEN_PREFIXES))


def _text(el: ET.Element | None) -> str | None:
    if el is None:
        return None
    t = re.sub(r"\s+", " ", " ".join(el.itertext())).strip()
    return t or None


def _find_first(root: ET.Element, *names: str) -> ET.Element | None:
    wanted = set(names)
    for el in root.iter():
        if _local(el.tag) in wanted:
            return el
    return None


def _find_all(root: ET.Element, name: str) -> list[ET.Element]:
    return [el for el in root.iter() if _local(el.tag) == name]


def _child_text(el: ET.Element, *names: str) -> str | None:
    for ch in el.iter():
        if ch is not el and _local(ch.tag) in names:
            return _text(ch)
    return None


def _money(el: ET.Element | None) -> str | None:
    if el is None:
        return None
    amount = _child_text(el, "zahl", "betrag", "wert")
    if amount:
        currency = _child_text(el, "waehrung", "currency") or "EUR"
        return f"{amount} {currency}".strip()
    return _text(el)


def _company_address(root: ET.Element) -> str | None:
    """First address that is NOT inside a natural person: street + number,
    postcode, city. A company's seat is company data; a director's home is not,
    and never reaches this function because we skip person subtrees."""
    for person in _find_all(root, "natuerlichePerson"):
        for el in person.iter():
            el.set("_swift_person", "1")
    try:
        for el in _find_all(root, "anschrift"):
            if el.get("_swift_person"):
                continue
            strasse = _child_text(el, "strasse")
            hausnummer = _child_text(el, "hausnummer")
            plz = _child_text(el, "postleitzahl", "plz")
            ort = _child_text(el, "ort")
            parts = [" ".join(p for p in (strasse, hausnummer) if p),
                     " ".join(p for p in (plz, ort) if p)]
            line = ", ".join(p for p in parts if p)
            if line:
                return line
        return None
    finally:
        for el in root.iter():
            el.attrib.pop("_swift_person", None)


def _person_names(root: ET.Element) -> list[str]:
    """Display names of the representatives. Names only — no birth data is read
    here, and redact_si() makes sure none is kept anywhere else either."""
    out: list[str] = []
    for el in _find_all(root, "natuerlichePerson"):
        nach = _child_text(el, "nachname")
        vor = _child_text(el, "vorname")
        name = " ".join(p for p in (vor, nach) if p)
        if name and name not in out:
            out.append(name)
    return out


def _parse_bytes(xml_bytes: bytes) -> ET.Element:
    head = xml_bytes.lstrip()[:200].lower()
    if not head.startswith(b"<"):
        raise ValueError("not_xml")
    if b"<html" in head or b"<!doctype html" in head:
        raise ValueError("html_shell")
    try:
        return ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise ValueError(f"xml_parse_error:{exc}") from exc


def parse_si(xml_bytes: bytes) -> SiProfile:
    """Parse an SI document. Raises ValueError when the portal handed us an
    HTML shell instead of a document — which is exactly what it did to the
    June 2026 attempt, 21 times out of 21."""
    root = _parse_bytes(xml_bytes)

    p = SiProfile(document_hash=hashlib.sha256(xml_bytes).hexdigest())
    org = _find_first(root, "rechtstraeger", "organisation")
    if org is not None:
        p.firma = _child_text(org, "bezeichnung.aktuell", "bezeichnung", "name", "firma")
        p.rechtsform = _child_text(org, "rechtsform")
        p.sitz = _child_text(org, "sitz", "ort")
    if not p.firma:
        p.firma = _text(_find_first(root, "bezeichnung.aktuell", "firma", "bezeichnung"))
    if not p.rechtsform:
        p.rechtsform = _text(_find_first(root, "rechtsform"))

    p.gegenstand = (_text(_find_first(root, "gegenstand"))
                    or _text(_find_first(root, "geschaeftszweck")))
    p.status = _text(_find_first(root, "statusRechtstraeger", "status"))
    p.stammkapital = _money(_find_first(root, "stammkapital", "grundkapital", "kapital"))
    p.anschrift = _company_address(root)
    p.registergericht = _text(_find_first(root, "registergericht", "gericht"))
    p.registerart = _text(_find_first(root, "registerart"))
    p.registernummer = _text(_find_first(root, "registernummer", "aktenzeichen"))
    p.euid = _text(_find_first(root, "euid", "europaeischeEindeutigeKennung"))
    p.vertretungsberechtigte = _person_names(root)
    return p


def _prune_person(person: ET.Element) -> None:
    """Whitelist the inside of one natuerlichePerson, depth first."""
    for child in list(person):
        name = _local(child.tag)
        if name not in ALLOWED_PERSON_TAGS or _is_forbidden_person_tag(name):
            person.remove(child)
        else:
            _strip_forbidden(child)


def _strip_forbidden(el: ET.Element) -> None:
    for child in list(el):
        if _is_forbidden_person_tag(_local(child.tag)):
            el.remove(child)
        else:
            _strip_forbidden(child)


def _declared_namespaces(xml_bytes: bytes) -> dict[str, str]:
    seen: dict[str, str] = {}
    try:
        for event, payload in ET.iterparse(io.BytesIO(xml_bytes), events=("start-ns",)):
            prefix, uri = payload
            seen.setdefault(prefix, uri)
    except ET.ParseError:
        pass
    return seen


def redact_si(xml_bytes: bytes) -> tuple[bytes, str]:
    """Return (redacted_bytes, rule). Every natuerlichePerson keeps its name and
    nothing else; birth date, birth place and private address are gone before
    these bytes are stored, logged or written to disk."""
    root = _parse_bytes(xml_bytes)
    for prefix, uri in _declared_namespaces(xml_bytes).items():
        try:
            ET.register_namespace(prefix, uri)
        except ValueError:
            pass
    for person in _find_all(root, "natuerlichePerson"):
        _prune_person(person)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True), REDACTION_RULE


def condense_purpose(text: str | None, max_chars: int = 400) -> str | None:
    """Deterministic 1–2 sentence condensation — first two sentences and a hard
    cap. No model, no paraphrase, nothing invented."""
    if not text:
        return None
    t = re.sub(r"\s+", " ", text).strip()
    out = " ".join(re.split(r"(?<=[.;])\s+", t)[:2]).strip()
    if len(out) > max_chars:
        out = out[:max_chars].rsplit(" ", 1)[0] + " …"
    return out or None
