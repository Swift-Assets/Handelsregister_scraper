"""Registerportal driver (Playwright, headless Chromium).

Every selector the portal exposes lives in SEL, in one place, because a portal
redesign is then a one-file fix. The 2025 redesign is what broke every
open-source client for this portal; ours should survive it in an afternoon.

This module does the mechanics only. It never decides whether a request may be
made — the caller asks swift_v2.registry_claim_request first, every time.
"""

from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

WELCOME = "https://www.handelsregister.de/rp_web/welcome.xhtml"

SEL = {
    "normale_suche": "#naviForm\\:normaleSucheLink",
    "schlagwoerter": "#form\\:schlagwoerter",
    "register_nummer": "#form\\:registerNummer",
    "register_art": "#form\\:registerArt",
    "gericht_input": "#form\\:registergericht_input",
    "gericht_select": "select[id*='registergericht']",
    "btn_suche": "#form\\:btnSuche",
    "results": "table[id*='ergebnis'], #ergebnissForm, .RegPortErg_AZ",
}

# Markers are matched against the page's VISIBLE text, never its source.
# The portal ships a hidden error panel on every page — an empty "Fehler ID:"
# and the sentence below, waiting to be filled in when something really goes
# wrong. Searching the source for those words refuses every healthy page.
ERROR_MARKERS = ("Fehler ID", "Es ist ein Fehler aufgetreten")

# What a page must contain for us to call it the page we asked for. Presence of
# what we need outranks the presence of words we fear.
EXPECT_WELCOME = ("normaleSucheLink",)
EXPECT_SEARCH_FORM = ("schlagwoerter",)
SESSION_MARKERS = ("Ihre Sitzung ist abgelaufen", "Sitzung ist abgelaufen")
BLOCK_MARKERS = ("gesperrt", "zu viele Anfragen", "Too Many Requests",
                 "Zugriff verweigert")

REGISTER_TYPES = ("HRA", "HRB", "GnR", "GsR", "PR", "VR")

# "Bayern Amtsgericht Aschaffenburg HRB 623" -> court, type, number.
_TRIPLE_RE = re.compile(
    r"(?:Amtsgericht|AG)\s+(?P<court>[^\d]{2,60}?)\s+"
    r"(?P<type>" + "|".join(REGISTER_TYPES) + r")\s+"
    r"(?P<number>\d[\d ]{0,9}\d|\d)\s*(?P<suffix>[A-Za-z]{1,3})?\b",
    re.IGNORECASE)


class PortalError(Exception):
    def __init__(self, kind: str, message: str = "",
                 diagnosis: dict | None = None, evidence: str | None = None) -> None:
        parts = [p for p in (message, f"evidence: {evidence}" if evidence else "") if p]
        super().__init__(f"{kind}: {' | '.join(parts)}" if parts else kind)
        self.kind = kind
        self.diagnosis = diagnosis or {}
        self.evidence = evidence


@dataclass
class SearchHit:
    company_name: str
    row_text: str
    document_links: dict[str, str]           # "SI" -> element id
    registry_court: str | None = None
    registry_type: str | None = None
    registry_number: str | None = None


def diagnose_page(html: str, status: int | None = None,
                  visible_text: str | None = None,
                  expect: tuple[str, ...] = ()) -> dict:
    """Pure. Says what is wrong with a page AND why we think so.

    Two rules, learned the hard way on 2026-09-19 when this guard refused the
    portal's perfectly healthy welcome page three times in a row:

    1. **Accept on positive evidence.** If the page carries what we came for —
       the link, the form, the results — it is the page we asked for, whatever
       else it also contains. The portal ships a hidden, empty error panel on
       every single page; a guard that reads the source for the word "Fehler"
       will reject the whole portal forever.
    2. **Match markers against visible text, not source.** Hidden template
       markup is not a message to the user, and must not be read as one.

    An HTTP status still outranks both: 403 is 403 whatever the body says.
    """
    html = html or ""
    haystack = html if visible_text is None else visible_text
    matched = {
        "block": [m for m in BLOCK_MARKERS if m in haystack],
        "session": [m for m in SESSION_MARKERS if m in haystack],
        "error": [m for m in ERROR_MARKERS if m in haystack],
    }
    missing = [m for m in expect if m not in html]
    found_expected = bool(expect) and not missing

    kind = None
    reason = None
    if status == 403:
        kind, reason = "http_403", "http status 403"
    elif status == 429:
        kind, reason = "http_429", "http status 429"
    elif status is not None and status >= 500:
        kind, reason = "portal_error_page", f"http status {status}"
    elif found_expected:
        kind, reason = None, None          # positive evidence wins
    elif matched["block"]:
        kind, reason = "ip_blocked", f"visible text contains {matched['block']}"
    elif matched["session"]:
        kind, reason = "session_expired", f"visible text contains {matched['session']}"
    elif matched["error"]:
        kind, reason = "portal_error_page", f"visible text contains {matched['error']}"
    elif missing:
        kind, reason = "unexpected_page", f"page does not contain {missing}"
    return {"kind": kind, "reason": reason, "http_status": status,
            "matched_markers": matched, "html_chars": len(html),
            "expected": list(expect), "missing_expected": missing,
            "matched_on": "source" if visible_text is None else "visible text"}


def classify_page(html: str, status: int | None = None) -> str | None:
    """Pure: map a page or response to a failure kind, or None when healthy."""
    return diagnose_page(html, status)["kind"]


FATAL_KINDS = ("http_403", "http_429", "ip_blocked", "portal_error_page",
               "session_expired", "unexpected_page")


def parse_registry_triple(text: str) -> tuple[str | None, str | None, str | None]:
    """Read court, register type and number out of a result row's own text.
    Returns (None, None, None) when the row does not state them — the caller
    must then refuse the hit rather than guess."""
    m = _TRIPLE_RE.search(text or "")
    if not m:
        return None, None, None
    court = re.sub(r"\s+", " ", m.group("court")).strip(" ,-")
    number = re.sub(r"\s+", "", m.group("number")) + (m.group("suffix") or "").upper()
    return court or None, m.group("type").upper(), number or None


def jitter(base: float, spread: float = 0.35) -> float:
    return base * (1 + random.uniform(-spread, spread))


class Portal:
    def __init__(self, user_agent: str, executable_path: str | None = None,
                 headless: bool = True, min_delay_s: float = 4.0,
                 evidence_dir: str | None = None) -> None:
        self.user_agent = user_agent
        self.executable_path = executable_path
        self.headless = headless
        self.min_delay_s = min_delay_s
        self.evidence_dir = Path(evidence_dir) if evidence_dir else None
        self.last_diagnosis: dict | None = None
        self._pw = self._browser = self._ctx = self._page = None

    # -- lifecycle ------------------------------------------------------------
    def __enter__(self) -> "Portal":
        from playwright.sync_api import sync_playwright  # lazy: tests need none
        self._pw = sync_playwright().start()
        kw = {"headless": self.headless}
        if self.executable_path:
            kw["executable_path"] = self.executable_path
        self._browser = self._pw.chromium.launch(**kw)
        self._ctx = self._browser.new_context(
            locale="de-DE", user_agent=self.user_agent, accept_downloads=True)
        self._page = self._ctx.new_page()
        return self

    def __exit__(self, *exc) -> None:
        for closer in (lambda: self._ctx.close(),
                       lambda: self._browser.close(),
                       lambda: self._pw.stop()):
            try:
                closer()
            except Exception:
                pass

    def _pause(self) -> None:
        time.sleep(jitter(self.min_delay_s))

    def keep_evidence(self, label: str) -> str | None:
        """Write the page we are looking at to disk: the rendered HTML and a
        screenshot. Costs no portal request and is the difference between
        knowing and assuming."""
        if not self.evidence_dir:
            return None
        try:
            self.evidence_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            base = self.evidence_dir / f"{stamp}-{label}"
            base.with_suffix(".html").write_text(self._page.content(), encoding="utf-8")
            try:
                self._page.screenshot(path=str(base.with_suffix(".png")), full_page=True)
            except Exception:
                pass
            return str(base.with_suffix(".html"))
        except Exception:
            return None

    def _visible_text(self) -> str | None:
        """What a person would actually read on this page. Hidden template
        markup is not a message to anyone and must not be read as one."""
        try:
            return self._page.inner_text("body")
        except Exception:
            return None

    def _check(self, status: int | None = None,
               expect: tuple[str, ...] = ()) -> None:
        diagnosis = diagnose_page(self._page.content(), status,
                                  visible_text=self._visible_text(),
                                  expect=expect)
        diagnosis["url"] = self._page.url
        try:
            diagnosis["title"] = self._page.title()
        except Exception:
            diagnosis["title"] = None
        self.last_diagnosis = diagnosis
        if diagnosis["kind"]:
            evidence = self.keep_evidence(diagnosis["kind"])
            raise PortalError(diagnosis["kind"],
                              f"{diagnosis['reason']} at {diagnosis['url']}",
                              diagnosis=diagnosis, evidence=evidence)

    # -- steps ----------------------------------------------------------------
    def open_welcome(self) -> int | None:
        """Load the portal's front page and nothing else. Returns the HTTP
        status. This is the smallest thing we can ask the portal, which makes
        it the right probe when we need to know whether it is answering at
        all."""
        resp = self._page.goto(WELCOME, wait_until="networkidle", timeout=60_000)
        status = resp.status if resp else None
        self._check(status, expect=EXPECT_WELCOME)
        return status

    def open_search(self) -> None:
        """Counts as one request: the session has to be opened before anything."""
        self.open_welcome()
        self._pause()
        self._page.click(SEL["normale_suche"])
        self._page.wait_for_selector(SEL["schlagwoerter"], timeout=30_000)
        self._check(expect=EXPECT_SEARCH_FORM)

    def court_options(self) -> list[str]:
        """Labels of the Registergericht select, read once per session. Reading
        a control already on the page is not a retrieval."""
        try:
            return self._page.eval_on_selector_all(
                SEL["gericht_select"] + " option",
                "els => els.map(e => e.textContent.trim()).filter(Boolean)")
        except Exception:
            return []

    def _set_optional(self, selector: str, value: str, as_select: bool) -> bool:
        try:
            if not self._page.query_selector(selector):
                return False
            if as_select:
                self._page.select_option(selector, label=value)
            else:
                self._page.fill(selector, value)
            return True
        except Exception:
            return False

    def search(self, keywords: str, register_number: str | None = None,
               register_type: str | None = None,
               court_label: str | None = None) -> list[SearchHit]:
        """One counted retrieval.

        Court and register type are sent whenever we know them. An HRB number
        is unique only inside its court — 118 courts appear in our data and
        HRB 5407 exists in many of them — so a search that omits the court is a
        search that can return the wrong company.
        """
        self._page.fill(SEL["schlagwoerter"], keywords or "")
        if register_number:
            self._set_optional(SEL["register_nummer"], register_number, as_select=False)
        if register_type:
            self._set_optional(SEL["register_art"], register_type.upper(), as_select=True)
        if court_label:
            if not self._set_optional(SEL["gericht_select"], court_label, as_select=True):
                self._set_optional(SEL["gericht_input"], court_label, as_select=False)

        self._pause()
        self._page.click(SEL["btn_suche"])
        self._page.wait_for_selector(SEL["results"], timeout=45_000)
        self._check()

        rows = self._page.evaluate(
            """() => {
                 const kinds = ['AD','CD','HD','DK','UT','SI','VÖ','VOE'];
                 const seen = new Map();
                 for (const a of Array.from(document.querySelectorAll('a'))) {
                   const label = (a.textContent || '').trim();
                   if (!kinds.includes(label)) continue;
                   const row = a.closest('tr') || a.parentElement;
                   if (!row) continue;
                   if (!seen.has(row)) seen.set(row, {text: (row.innerText || ''), links: {}});
                   if (a.id) seen.get(row).links[label] = a.id;
                 }
                 return Array.from(seen.values());
               }""")

        hits: list[SearchHit] = []
        for r in rows:
            text = r.get("text") or ""
            first_line = next((ln.strip() for ln in text.split("\n") if ln.strip()), "")
            court, rtype, number = parse_registry_triple(text)
            hits.append(SearchHit(company_name=first_line, row_text=text,
                                  document_links=r.get("links") or {},
                                  registry_court=court, registry_type=rtype,
                                  registry_number=number))
        return hits

    def download_document(self, link_id: str, kind: str = "SI") -> bytes:
        """One counted retrieval. Returns the raw bytes the portal delivered.

        A body that does not begin with '<' or '%PDF' is the empty shell the
        June 2026 attempt got 21 times out of 21 — it is reported as its own
        failure kind so it can never be mistaken for a parser bug.
        """
        self._pause()
        with self._page.expect_download(timeout=60_000) as dl:
            self._page.click(f'[id="{link_id}"]')
        with open(dl.value.path(), "rb") as f:
            data = f.read()
        self._check()
        head = data.lstrip()[:16]
        if not (head.startswith(b"<") or head.startswith(b"%PDF")):
            raise PortalError("shell_only_no_document_content",
                              f"{kind}: {len(data)} bytes")
        return data
