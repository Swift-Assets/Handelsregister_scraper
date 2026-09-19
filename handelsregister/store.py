"""PostgREST store (service_role; no other role can write these tables).

Every write is idempotent by a stable source_external_id or a content hash, so
re-running a company never duplicates anything.

What is written about people: their NAME, and nothing else. Birth dates and
private addresses are removed by xjustiz.redact_si() before any bytes reach
this module, and are never parsed into a column in the first place.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from datetime import datetime, timezone
from typing import Any

import requests

from .xjustiz import SiProfile, condense_purpose, redact_si

SOURCE_NAME = "handelsregister_direct"
ACTIVITY_SOURCE = "handelsregister"
HTTP_TIMEOUT = 60


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, url: str, key: str, session: requests.Session | None = None) -> None:
        self.base = url.rstrip("/")
        self.s = session or requests.Session()
        self.s.headers.update({
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept-Profile": "swift_v2",
            "Content-Profile": "swift_v2",
        })

    # -- reads ----------------------------------------------------------------
    def fetch_queue(self, limit: int, method: str | None = "lookup_by_registry") -> list[dict]:
        q = (f"{self.base}/rest/v1/v_handelsregister_pending_queue"
             f"?select=entity_id,display_name,city,legal_form,registry_court,"
             f"registry_type,registry_number,registry_identity_key,"
             f"recommended_lookup_method&limit={int(limit)}")
        if method:
            q += f"&recommended_lookup_method=eq.{method}"
        r = self.s.get(q, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        return r.json()

    # -- writes ---------------------------------------------------------------
    def _post(self, path: str, row: dict[str, Any], on_conflict: str) -> None:
        r = self.s.post(
            f"{self.base}/rest/v1/{path}?on_conflict={on_conflict}",
            headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
            data=json.dumps(row), timeout=HTTP_TIMEOUT)
        r.raise_for_status()

    def store_raw_document(self, raw: bytes, document_kind: str) -> str | None:
        """Keep the document for evaluation: redacted, gzipped, and addressed by
        the hash of what we actually store, so the same document twice costs
        nothing the second time. Returns the hash, or None if it was not XML."""
        try:
            redacted, rule = redact_si(raw)
        except ValueError:
            return None                      # not XML (a PDF, or a shell) — not ours to keep
        digest = hashlib.sha256(redacted).hexdigest()
        payload = gzip.compress(redacted, compresslevel=9)
        self._post("registry_raw_documents", {
            "content_sha256": digest,
            "source": ACTIVITY_SOURCE,
            "document_kind": document_kind,
            "content_gzip": "\\x" + payload.hex(),
            "bytes_original": len(redacted),
            "bytes_stored": len(payload),
            "redaction_rule": rule,
        }, on_conflict="content_sha256")
        return digest

    def record_profile(self, entity: dict, profile: SiProfile, licence: str,
                       raw_sha256: str | None = None,
                       match_method: str | None = None) -> None:
        key = entity.get("registry_identity_key") or entity["entity_id"]
        self._post("source_handelsregister_records", {
            "source_name": SOURCE_NAME,
            "source_external_id": f"{key}:profile",
            "entity_id": entity["entity_id"],
            "fetched_at": _now(),
            "registry_court": entity.get("registry_court") or profile.registergericht,
            "registry_type": entity.get("registry_type") or profile.registerart,
            "registry_number": entity.get("registry_number") or profile.registernummer,
            "registry_identity_key": entity.get("registry_identity_key"),
            "company_name": profile.firma,
            "legal_form": profile.rechtsform or entity.get("legal_form"),
            "seat_city": profile.sitz,
            "address": profile.anschrift,
            "status": profile.status,
            "event_type": "profile",
            "business_purpose": profile.gegenstand,
            "share_capital": profile.stammkapital,
            "document_kind": "SI",
            "source_document_hash": profile.document_hash,
            "raw_document_sha256": raw_sha256,
            "licence": licence,
            # Names only. The jsonb column is exposed by no view (0152).
            "managing_directors": [{"name": n} for n in profile.vertretungsberechtigte] or None,
            "raw_json": {"parser": "xjustiz.parse_si/v2",
                         "match_method": match_method,
                         "euid": profile.euid},
        }, on_conflict="source_name,source_external_id")

    def record_failure(self, entity: dict, event_type: str, error_kind: str,
                       message: str | None = None) -> None:
        now = _now()
        key = entity.get("registry_identity_key") or entity["entity_id"]
        self._post("source_handelsregister_records", {
            "source_name": SOURCE_NAME,
            "source_external_id": f"{key}:{event_type}:{now[:10]}",
            "entity_id": entity["entity_id"],
            "fetched_at": now,
            "registry_court": entity.get("registry_court"),
            "registry_type": entity.get("registry_type"),
            "registry_number": entity.get("registry_number"),
            "registry_identity_key": entity.get("registry_identity_key"),
            "company_name": entity.get("display_name"),
            "event_type": event_type,
            "raw_json": {"error_kind": error_kind,
                         "error_message": (message or "")[:300]},
        }, on_conflict="source_name,source_external_id")

    def record_activity(self, entity: dict, profile: SiProfile, source_ref: str) -> bool:
        """The product output: what this company is registered to do.

        activity_ar stays NULL — translating is a separate, budgeted step, and
        an empty field is honest where a machine translation would not be.
        """
        de = condense_purpose(profile.gegenstand)
        if not de:
            return False
        self._post("company_activity_sources", {
            "entity_id": entity["entity_id"],
            "source": ACTIVITY_SOURCE,
            "activity_de": de,
            "activity_ar": None,
            "confidence": "high",
            "source_ref": source_ref,
            "matched_hrb": entity.get("registry_number") or profile.registernummer,
            "extracted_at": _now(),
        }, on_conflict="entity_id,source")
        return True
