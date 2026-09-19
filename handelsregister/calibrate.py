#!/usr/bin/env python3
"""The calibration run — the experiment that decides whether this project is
possible at all.

It answers exactly two questions, with evidence:

  1. Does a document retrieval actually deliver a document? The June 2026
     attempt reached this step 21 times and got an empty 269-byte shell 21
     times. If that is still true with a real browser, there is no source for
     the company purpose inside the budget and the project stops here.
  2. How many requests does one company really cost? Everything downstream —
     whether the backlog takes three weeks or nine months — turns on whether
     the answer is 2 or 5.

Safety, enforced in this file and not only in configuration:

  * at most HARD_MAX_REQUESTS requests in total, ever, per invocation;
  * at least MIN_GAP_SECONDS between two requests;
  * it still asks the database's budget gate before every single request, so
    the ledger tells the truth afterwards;
  * it refuses to start without HR_CALIBRATION_CONFIRM=I-UNDERSTAND;
  * it writes nothing to the product tables. Nothing it learns reaches
    company_activity_sources.

Run it ONLY on the fixed-IP host.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .budget import Budget, BudgetDenied, BudgetUnavailable
from .config import ConfigError, load
from .matching import entity_triple, hit_triple, pick_hit
from .normalize import load_court_aliases, norm_registry_number_v2, portal_court_label
from .portal import Portal, PortalError
from .store import Store
from .xjustiz import parse_si, redact_si

HARD_MAX_REQUESTS = 30          # the number we told the owner, in code
MIN_GAP_SECONDS = 60            # one request a minute, no faster
CONFIRM = "I-UNDERSTAND"

# A company we know exists, searched first. The memory of this project says it
# plainly: before trusting a negative from an external form, send one value you
# KNOW exists and vary one field at a time.
CONTROL = {
    "entity_id": None,
    "display_name": "Swift Assets UG (haftungsbeschränkt)",
    "registry_court": "Wuppertal",
    "registry_type": "HRB",
    "registry_number": "37064",
    "registry_identity_key": "control:wuppertal:hrb:37064",
}


class Budgeteer:
    """Wraps the real gate and adds this run's own, stricter ceiling."""

    def __init__(self, budget: Budget, log) -> None:
        self.budget = budget
        self.log = log
        self.spent = 0
        self._last = 0.0

    def claim(self, kind: str, key: str | None = None) -> str:
        if self.spent >= HARD_MAX_REQUESTS:
            raise BudgetDenied("calibration_limit",
                               f"{HARD_MAX_REQUESTS} requests is the whole experiment")
        gap = MIN_GAP_SECONDS - (time.monotonic() - self._last)
        if self._last and gap > 0:
            self.log(f"[cal] pacing: sleeping {int(gap)}s")
            time.sleep(gap)
        request_id = self.budget.claim(kind, None, key, log=self.log)
        self.spent += 1
        self._last = time.monotonic()
        return request_id

    def finish(self, *a, **kw) -> None:
        self.budget.finish(*a, **kw)


def _probe_document(portal, gate, hit, kind, entity, out_dir, log) -> dict:
    """One document retrieval, reported honestly whatever comes back."""
    link = (hit.document_links or {}).get(kind)
    if not link:
        return {"kind": kind, "outcome": "no_link_offered"}

    request_id = gate.claim("document", entity.get("registry_identity_key"))
    try:
        raw = portal.download_document(link, kind)
    except PortalError as exc:
        gate.finish(request_id, "error", note=exc.kind)
        log(f"[cal]   {kind}: {exc.kind}")
        return {"kind": kind, "outcome": exc.kind}
    gate.finish(request_id, "ok", note=f"{len(raw)} bytes")

    result = {"kind": kind, "outcome": "delivered", "bytes": len(raw),
              "looks_like": "xml" if raw.lstrip()[:1] == b"<" else
                            "pdf" if raw.lstrip()[:4] == b"%PDF" else "other"}

    if result["looks_like"] == "xml":
        try:
            redacted, rule = redact_si(raw)
            path = out_dir / f"{kind}-{entity.get('registry_number') or 'x'}.redacted.xml"
            path.write_bytes(redacted)
            result["saved"] = str(path)
            result["redaction_rule"] = rule
            profile = parse_si(raw)
            result["purpose_found"] = profile.has_purpose()
            result["purpose_chars"] = len(profile.gegenstand or "")
            result["fields"] = {k: bool(v) for k, v in {
                "firma": profile.firma, "rechtsform": profile.rechtsform,
                "sitz": profile.sitz, "anschrift": profile.anschrift,
                "stammkapital": profile.stammkapital, "status": profile.status,
                "euid": profile.euid}.items()}
        except ValueError as exc:
            result["outcome"] = f"parse_failed:{exc}"
    elif result["looks_like"] == "pdf":
        # Kept on this machine only, never in the database: an AD carries the
        # directors' birth dates and we have no redactor for PDF.
        path = out_dir / f"{kind}-{entity.get('registry_number') or 'x'}.pdf"
        path.write_bytes(raw)
        result["saved"] = str(path)
        result["warning"] = "PDF holds personal data unredacted — delete after reading"

    log(f"[cal]   {kind}: {result['outcome']} ({result.get('bytes', 0)} bytes, "
        f"purpose={result.get('purpose_found')})")
    return result


def probe_company(portal, gate, entity, courts, aliases, out_dir, log) -> dict:
    name = entity.get("display_name")
    log(f"[cal] {name} — {entity.get('registry_court')} "
        f"{entity.get('registry_type')} {entity.get('registry_number')}")
    record = {"entity": {k: entity.get(k) for k in
                         ("display_name", "registry_court", "registry_type",
                          "registry_number", "registry_identity_key")},
              "requests": 0, "documents": []}

    label = portal_court_label(entity.get("registry_court"), courts, aliases)
    record["court_label_sent"] = label
    if not label:
        log("[cal]   court could not be mapped to a portal option")

    # After a search the portal shows results, which carry no search field.
    # Getting the form back is a real navigation, so it is a real request.
    if not portal.search_form_ready():
        back = gate.claim("other", entity.get("registry_identity_key"))
        try:
            portal.open_search_form()
        except PortalError as exc:
            gate.finish(back, "error", note=exc.kind)
            record["search"] = {"outcome": f"form_unreachable:{exc.kind}"}
            return record
        gate.finish(back, "ok", note="back to the search form")
        record["returned_to_form"] = True

    request_id = gate.claim("search", entity.get("registry_identity_key"))
    try:
        hits = portal.search(
            keywords=name or "",
            register_number=norm_registry_number_v2(entity.get("registry_number")),
            register_type=entity.get("registry_type"),
            court_label=label)
    except PortalError as exc:
        gate.finish(request_id, "error", note=exc.kind)
        record["search"] = {"outcome": exc.kind}
        return record
    gate.finish(request_id, "ok", note=f"{len(hits)} hits")

    decision = pick_hit(hits, entity, "SI")
    record["search"] = {
        "outcome": "ok", "hits": len(hits),
        "document_kinds_offered": sorted({k for h in hits for k in h.document_links}),
        "wanted_triple": entity_triple(entity),
        "seen_triples": [hit_triple(h) for h in hits],
        "match": decision.reason, "match_method": decision.method,
    }
    log(f"[cal]   search: {len(hits)} hits, match={decision.reason}, "
        f"links={record['search']['document_kinds_offered']}")
    log(f"[cal]   wanted : {entity_triple(entity)}")
    log(f"[cal]   saw    : {[hit_triple(h) for h in hits]}")
    for h in hits[:2]:
        snippet = " / ".join(
            ln.strip() for ln in (h.row_text or "").splitlines() if ln.strip())
        log(f"[cal]   row    : {snippet[:220]}")
    record["search"]["row_text_sample"] = [
        (h.row_text or "")[:600] for h in hits[:2]]
    if not decision.accepted:
        return record

    si = _probe_document(portal, gate, decision.hit, "SI", entity, out_dir, log)
    record["documents"].append(si)
    # Only spend a second retrieval when SI failed — that is the whole point of
    # having a fallback, and it is what tells us the real per-company cost.
    if si.get("outcome") != "delivered" or not si.get("purpose_found"):
        record["documents"].append(
            _probe_document(portal, gate, decision.hit, "AD", entity, out_dir, log))
    return record


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Registerportal calibration run")
    ap.add_argument("--limit", type=int, default=5,
                    help="companies from the queue, after the control (default 5)")
    try:                       # so a pipe into tee does not hide the run
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    ap.add_argument("--no-control", action="store_true",
                    help="skip the known-good control company")
    ap.add_argument("--out", default="calibration",
                    help="directory for the log and the saved documents")
    args = ap.parse_args(argv)

    if os.environ.get("HR_CALIBRATION_CONFIRM") != CONFIRM:
        print(f"refusing to start: set HR_CALIBRATION_CONFIRM={CONFIRM}.\n"
              f"This sends up to {HARD_MAX_REQUESTS} real requests to the "
              f"Registerportal from this machine's IP address.", file=sys.stderr)
        return 2
    try:
        settings = load()
    except ConfigError as exc:
        print(f"configuration refused: {exc}", file=sys.stderr)
        return 2

    budget = Budget(settings.supabase_url, settings.supabase_key, settings.run_id)
    try:
        status = budget.status()
    except BudgetUnavailable as exc:
        print(f"budget gate unreachable, refusing to start: {exc}", file=sys.stderr)
        return 2
    if not status or not status.get("enabled"):
        print("swift_v2.registry_source_config says the source is disabled.\n"
              "Enable it with a calibration-sized cap first (see docs/RUNBOOK.md).",
              file=sys.stderr)
        return 2

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    store = Store(settings.supabase_url, settings.supabase_key)
    queue = [] if args.limit < 1 else store.fetch_queue(args.limit)
    targets = ([] if args.no_control else [CONTROL]) + queue

    gate = Budgeteer(budget, print)
    aliases = load_court_aliases()
    report = {"run_id": settings.run_id,
              "started_at": datetime.now(timezone.utc).isoformat(),
              "hard_max_requests": HARD_MAX_REQUESTS,
              "min_gap_seconds": MIN_GAP_SECONDS,
              "cap_at_start": status, "companies": []}
    print(f"[cal] run {settings.run_id} · {len(targets)} companies · "
          f"≤{HARD_MAX_REQUESTS} requests · ≥{MIN_GAP_SECONDS}s apart")

    try:
        with Portal(user_agent=settings.user_agent,
                    executable_path=settings.chromium_path,
                    headless=settings.headless,
                    evidence_dir=str(out_dir / "evidence")) as portal:
            request_id = gate.claim("session_open")
            try:
                portal.open_search()
            except PortalError as exc:
                # Close the ledger row before unwinding: a request whose outcome
                # is never written reads as still in flight forever.
                gate.finish(request_id, "error", note=exc.kind)
                report["portal_diagnosis"] = exc.diagnosis
                report["evidence"] = exc.evidence
                raise
            gate.finish(request_id, "ok")
            courts = portal.court_options()
            report["court_options_seen"] = len(courts)
            print(f"[cal] session open · {len(courts)} court options on the form")

            for entity in targets:
                before = gate.spent
                try:
                    record = probe_company(portal, gate, entity, courts,
                                           aliases, out_dir, print)
                except BudgetDenied as exc:
                    print(f"[cal] stopping: {exc}")
                    break
                except Exception as exc:               # noqa: BLE001
                    # One company that goes wrong is a finding about that
                    # company, not a reason to throw away the whole run.
                    print(f"[cal]   failed: {type(exc).__name__}: {exc}",
                          file=sys.stderr)
                    record = {"entity": {"display_name": entity.get("display_name")},
                              "error": f"{type(exc).__name__}: {exc}"[:400],
                              "documents": []}
                record["requests"] = gate.spent - before
                report["companies"].append(record)
    except BudgetDenied as exc:
        print(f"[cal] stopping: {exc}")
    except PortalError as exc:
        print(f"[cal] portal: {exc}", file=sys.stderr)
        report["portal_error"] = exc.kind
        report.setdefault("portal_diagnosis", exc.diagnosis)
        report.setdefault("evidence", exc.evidence)
        if exc.diagnosis:
            print(f"[cal] why: {exc.diagnosis.get('reason')}", file=sys.stderr)
            print(f"[cal] page: {exc.diagnosis.get('html_chars')} chars, "
                  f"title={exc.diagnosis.get('title')!r}", file=sys.stderr)
        if exc.evidence:
            print(f"[cal] the page itself is saved at {exc.evidence}", file=sys.stderr)
        budget.open_circuit(f"calibration:{exc.kind}", 60)
    except Exception as exc:                           # noqa: BLE001
        # Third time this lesson has arrived today: whatever goes wrong, the
        # findings are written. A run that dies with its evidence unwritten
        # has cost requests and bought nothing.
        print(f"[cal] run stopped: {type(exc).__name__}: {exc}", file=sys.stderr)
        report["fatal_error"] = f"{type(exc).__name__}: {exc}"[:400]

    done = [c for c in report["companies"] if c.get("requests")]
    delivered = [d for c in report["companies"] for d in c["documents"]
                 if d.get("outcome") == "delivered"]
    with_purpose = [d for d in delivered if d.get("purpose_found")]
    report.update({
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "requests_spent": gate.spent,
        "companies_probed": len(done),
        "documents_delivered": len(delivered),
        "documents_with_purpose": len(with_purpose),
        "requests_per_company": round(gate.spent / len(done), 2) if done else None,
        "verdict": ("purpose_reachable" if with_purpose else
                    "no_document_content" if done else "inconclusive"),
    })
    (out_dir / f"calibration-{settings.run_id}.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print("\n=== calibration summary ===")
    print(f"requests spent          : {report['requests_spent']} / {HARD_MAX_REQUESTS}")
    print(f"companies probed        : {report['companies_probed']}")
    print(f"requests per company    : {report['requests_per_company']}")
    print(f"documents delivered     : {report['documents_delivered']}")
    print(f"documents with a purpose: {report['documents_with_purpose']}")
    print(f"verdict                 : {report['verdict']}")
    print(f"report                  : {out_dir}/calibration-{settings.run_id}.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
