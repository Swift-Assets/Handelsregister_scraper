#!/usr/bin/env python3
"""One request, maximum evidence.

When the worker refuses the portal, the next question is always the same: is
the portal down, or is our own guard misreading a healthy page? Answering it
by trying the whole calibration again costs thirty requests and usually
returns "inconclusive" a second time.

This asks the portal for its front page exactly once, keeps the page and a
screenshot on disk, and prints what our guard saw and why it judged it that
way. It writes nothing to the product tables.

    python -m handelsregister.probe
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .budget import Budget, BudgetDenied, BudgetUnavailable
from .config import ConfigError, load
from .portal import EXPECT_WELCOME, Portal, PortalError, diagnose_page

EVIDENCE_DIR = "/home/hr/evidence"


def judge_saved_page(path: str) -> int:
    """Re-run the guard against a page already on disk. No portal request, no
    database, no budget — so a change to the guard can be proved right before
    it is ever allowed near the portal again."""
    html = Path(path).read_text(encoding="utf-8", errors="replace")
    d = diagnose_page(html, 200, expect=EXPECT_WELCOME)
    print(f"file        : {path}")
    print(f"size        : {d['html_chars']} characters")
    print(f"expected    : {d['expected']}")
    print(f"missing     : {d['missing_expected'] or 'nothing'}")
    print(f"markers hit : {json.dumps(d['matched_markers'], ensure_ascii=False)}")
    print(f"verdict     : {d['kind'] or 'healthy'}")
    print(f"because     : {d['reason'] or 'the page carries what we came for'}")
    return 0 if not d["kind"] else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="One request, maximum evidence")
    ap.add_argument("out_dir", nargs="?", default=EVIDENCE_DIR,
                    help="where to keep the page and the screenshot")
    ap.add_argument("--file", help="judge a page already saved on disk and "
                                   "send nothing to the portal")
    args = ap.parse_args(argv)
    if args.file:
        return judge_saved_page(args.file)
    out_dir = args.out_dir
    try:
        settings = load()
    except ConfigError as exc:
        print(f"configuration refused: {exc}", file=sys.stderr)
        return 2

    budget = Budget(settings.supabase_url, settings.supabase_key, settings.run_id)
    try:
        request_id = budget.claim("session_open", log=print, max_wait_s=0)
    except BudgetDenied as exc:
        print(f"the gate says no: {exc}")
        print("Nothing was sent. Clear the cooldown deliberately, or wait it out.")
        return 3
    except BudgetUnavailable as exc:
        print(f"budget gate unreachable, refusing to start: {exc}", file=sys.stderr)
        return 2

    print(f"[probe] one request to the portal front page, evidence -> {out_dir}")
    status = None
    error: PortalError | None = None
    with Portal(user_agent=settings.user_agent,
                executable_path=settings.chromium_path,
                headless=settings.headless,
                evidence_dir=out_dir) as portal:
        try:
            status = portal.open_welcome()
            kept = portal.keep_evidence("healthy")
        except PortalError as exc:
            error = exc
            kept = exc.evidence
        diagnosis = portal.last_diagnosis or {}

    # Print the findings BEFORE anything else that can fail. The whole point
    # of this run is the evidence; a bookkeeping call must never be able to
    # stand between us and it.
    print("\n=== what the portal sent ===")
    print(f"http status : {diagnosis.get('http_status')}")
    print(f"final url   : {diagnosis.get('url')}")
    print(f"page title  : {diagnosis.get('title')!r}")
    print(f"page size   : {diagnosis.get('html_chars')} characters")
    print("\n=== what our guard saw ===")
    print(f"verdict     : {diagnosis.get('kind') or 'healthy'}")
    print(f"because     : {diagnosis.get('reason') or 'no marker matched'}")
    print(f"markers hit : {json.dumps(diagnosis.get('matched_markers') or {}, ensure_ascii=False)}")
    print(f"evidence    : {kept}")

    budget.finish(request_id, "error" if error else "ok",
                  http_status=diagnosis.get("http_status"),
                  note=(error.kind if error else "welcome page ok"))

    if diagnosis.get("kind"):
        print("\nRead the saved page before believing either story. A tiny page "
              "(a few hundred characters) is the portal refusing us; a full "
              "page that merely contains one of our marker phrases is our own "
              "guard being too eager.")
    return 0 if not error else 1


if __name__ == "__main__":
    sys.exit(main())
