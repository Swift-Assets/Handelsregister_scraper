#!/usr/bin/env python3
"""Orchestrator: queue → search → SI → purpose, under the database's budget
and a circuit breaker that survives the end of the run.

Designed for a systemd timer on the fixed-IP host. It is never run from CI or
any shared address: the portal counts retrievals per IP, and our Whitelist
application names one address.

Environment
  SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY   required
  HR_CONTACT_EMAIL     required — goes into the User-Agent, no default
  HR_MAX_COMPANIES     companies per run (default 20)
  HR_DRY_RUN           1 = fetch and parse, write nothing (default 1)
  HR_KEEP_RAW          1 = keep the redacted document for evaluation (default 1)
  HR_CHROMIUM_PATH     optional executable path
  HR_LICENCE           provenance label (default official_portal)

The request caps are NOT here. They are rows in swift_v2.registry_source_config.
"""

from __future__ import annotations

import json
import os
import sys

from .budget import Budget, BudgetDenied, BudgetUnavailable
from .config import ConfigError, load
from .matching import pick_hit
from .normalize import load_court_aliases, norm_registry_number_v2, portal_court_label
from .pacing import CircuitBreaker
from .portal import Portal, PortalError
from .store import Store
from .xjustiz import parse_si

DOCUMENT_KIND = "SI"


def process_one(portal, store, budget, breaker, entity, court_label,
                licence, settings, log=print) -> str:
    """One company: at most one search and one document retrieval."""
    key = entity.get("registry_identity_key")
    number = norm_registry_number_v2(entity.get("registry_number"))

    req = budget.claim("search", entity.get("entity_id"), key, log=log)
    try:
        hits = portal.search(
            keywords=entity.get("display_name") or "",
            register_number=number,
            register_type=entity.get("registry_type"),
            court_label=court_label)
    except PortalError as exc:
        budget.finish(req, "error", note=exc.kind)
        if not settings.dry_run:
            store.record_failure(entity, "search_error", exc.kind, str(exc))
        breaker.failure(exc.kind)
        return exc.kind
    budget.finish(req, "ok", note=f"{len(hits)} hits")

    decision = pick_hit(hits, entity, DOCUMENT_KIND)
    if not decision.accepted:
        if not settings.dry_run:
            store.record_failure(entity, "search_no_result", decision.reason,
                                 f"{len(hits)} hits")
        breaker.success()          # the portal answered; it just was not our row
        return f"no_match:{decision.reason}"

    req = budget.claim("document", entity.get("entity_id"), key, log=log)
    try:
        raw = portal.download_document(
            decision.hit.document_links[DOCUMENT_KIND], DOCUMENT_KIND)
    except PortalError as exc:
        budget.finish(req, "error", note=exc.kind)
        if not settings.dry_run:
            store.record_failure(entity, "document_attempt", exc.kind, str(exc))
        breaker.failure(exc.kind)
        return exc.kind
    budget.finish(req, "ok", note=f"{len(raw)} bytes")

    try:
        profile = parse_si(raw)
    except ValueError as exc:
        kind = str(exc).split(":", 1)[0]
        if not settings.dry_run:
            store.record_failure(entity, "document_attempt", kind, str(exc))
        breaker.failure(kind)
        return kind

    breaker.success()
    if not profile.is_usable():
        if not settings.dry_run:
            store.record_failure(entity, "document_attempt", "profile_unusable")
        return "profile_unusable"

    if settings.dry_run:
        return "profile" if profile.has_purpose() else "profile_no_purpose"

    raw_sha = None
    if settings.keep_raw_documents:
        raw_sha = store.store_raw_document(raw, DOCUMENT_KIND)
    store.record_profile(entity, profile, licence, raw_sha, decision.method)
    store.record_activity(
        entity, profile,
        source_ref=f"Registerportal {DOCUMENT_KIND} {key or entity['entity_id']}")
    return "profile" if profile.has_purpose() else "profile_no_purpose"


def main() -> int:
    try:
        settings = load()
    except ConfigError as exc:
        print(f"configuration refused: {exc}", file=sys.stderr)
        return 2

    licence = os.environ.get("HR_LICENCE", "official_portal")
    store = Store(settings.supabase_url, settings.supabase_key)
    budget = Budget(settings.supabase_url, settings.supabase_key, settings.run_id)
    breaker = CircuitBreaker()

    try:
        status = budget.status()
    except BudgetUnavailable as exc:
        print(f"budget gate unreachable, refusing to start: {exc}", file=sys.stderr)
        return 2
    if not status or not status.get("enabled"):
        print("source disabled in swift_v2.registry_source_config — nothing to do")
        return 0

    queue = store.fetch_queue(settings.max_companies)
    aliases = load_court_aliases()
    outcomes: dict[str, int] = {}
    print(f"[hr] run {settings.run_id} · {len(queue)} companies · "
          f"{'DRY-RUN' if settings.dry_run else 'LIVE'} · "
          f"cap {status.get('max_per_hour')}/h, {status.get('max_per_day')}/day")

    try:
        with Portal(user_agent=settings.user_agent,
                    executable_path=settings.chromium_path,
                    headless=settings.headless) as portal:
            req = budget.claim("session_open")
            try:
                portal.open_search()
            except PortalError as exc:
                budget.finish(req, "error", note=exc.kind)
                breaker.failure(exc.kind)
                raise
            budget.finish(req, "ok")
            courts = portal.court_options()

            for entity in queue:
                if breaker.open:
                    print(f"[hr] circuit open ({breaker.reason}) — stopping",
                          file=sys.stderr)
                    break
                label = portal_court_label(entity.get("registry_court"), courts, aliases)
                outcome = process_one(portal, store, budget, breaker, entity,
                                      label, licence, settings)
                outcomes[outcome] = outcomes.get(outcome, 0) + 1
                print(f"[hr] {entity.get('registry_identity_key') or entity['entity_id']}"
                      f": {outcome}")
    except BudgetDenied as exc:
        print(f"[hr] budget gate said stop: {exc}", file=sys.stderr)
    except PortalError as exc:
        print(f"[hr] portal: {exc}", file=sys.stderr)
    finally:
        if breaker.open:
            budget.open_circuit(breaker.reason or "unknown", breaker.cooldown_minutes())
            print(f"[hr] source parked for {breaker.cooldown_minutes()} min "
                  f"({breaker.reason})", file=sys.stderr)

    print(json.dumps({"run_id": settings.run_id, "dry_run": settings.dry_run,
                      "processed": sum(outcomes.values()), "outcomes": outcomes,
                      "circuit_open": breaker.open, "reason": breaker.reason}))
    return 1 if breaker.open else 0


if __name__ == "__main__":
    sys.exit(main())
