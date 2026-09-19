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

import json
import sys
from pathlib import Path

from .budget import Budget, BudgetDenied, BudgetUnavailable
from .config import ConfigError, load
from .portal import Portal, PortalError

EVIDENCE_DIR = "/home/hr/evidence"


def main(argv: list[str] | None = None) -> int:
    out_dir = (argv or sys.argv[1:] or [EVIDENCE_DIR])[0]
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

    budget.finish(request_id, "error" if error else "ok",
                  http_status=diagnosis.get("http_status"),
                  note=(error.kind if error else "welcome page ok"))

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

    if diagnosis.get("kind"):
        print("\nRead the saved page before believing either story. A tiny page "
              "(a few hundred characters) is the portal refusing us; a full "
              "page that merely contains one of our marker phrases is our own "
              "guard being too eager.")
    return 0 if not error else 1


if __name__ == "__main__":
    sys.exit(main())
