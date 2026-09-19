"""The database-backed request budget.

There is exactly one counter for the Registerportal and it is a table. This
module is the worker's only way to reach it: ask for a slot, be told yes or be
told how long to wait. A process that cannot reach the database cannot make a
request, which is the correct failure direction.
"""

from __future__ import annotations

import time
from typing import Any, Callable

import requests

HTTP_TIMEOUT = 30
SOURCE_DEFAULT = "handelsregister"

# A grant we asked for but never used still cost budget — that is deliberate.
# What we refuse to do is guess; if the gate is unreachable, we stop.
class BudgetUnavailable(RuntimeError):
    """The gate could not be reached or answered. The caller must stop."""


class BudgetDenied(RuntimeError):
    """The gate refused for a reason that will not clear by waiting a little."""

    def __init__(self, reason: str, detail: str | None = None) -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


# Reasons that mean "come back later, the situation will change by itself".
TRANSIENT = {"hour_budget_exhausted", "day_budget_exhausted", "min_gap",
             "outside_allowed_hours"}


class Budget:
    def __init__(self, url: str, key: str, run_id: str,
                 source: str = SOURCE_DEFAULT,
                 session: requests.Session | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.base = url.rstrip("/")
        self.source = source
        self.run_id = run_id
        self._sleep = sleep
        self.s = session or requests.Session()
        self.s.headers.update({
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept-Profile": "swift_v2",
            "Content-Profile": "swift_v2",
        })

    # -- plumbing -------------------------------------------------------------
    def _rpc(self, name: str, payload: dict[str, Any]) -> Any:
        try:
            r = self.s.post(f"{self.base}/rest/v1/rpc/{name}",
                            json=payload, timeout=HTTP_TIMEOUT)
        except requests.RequestException as exc:
            raise BudgetUnavailable(f"{name}: {exc}") from exc
        if r.status_code >= 400:
            raise BudgetUnavailable(f"{name}: HTTP {r.status_code} {r.text[:200]}")
        # A function that returns void answers 204 with an empty body. Asking
        # such a reply for JSON raises, which is how a healthy run died on its
        # very last line: the portal work was done, the evidence was on disk,
        # and reporting the outcome threw the whole thing away.
        if r.status_code == 204 or not (r.content or b"").strip():
            return None
        try:
            return r.json()
        except ValueError as exc:
            raise BudgetUnavailable(
                f"{name}: reply was not JSON: {r.text[:200]!r}") from exc

    # -- the gate -------------------------------------------------------------
    def try_claim(self, kind: str, entity_id: str | None = None,
                  key: str | None = None) -> dict[str, Any]:
        """One attempt. Returns the gate's answer verbatim."""
        answer = self._rpc("registry_claim_request", {
            "p_source": self.source, "p_kind": kind,
            "p_entity_id": entity_id, "p_key": key, "p_run_id": self.run_id})
        if not isinstance(answer, dict):
            # No answer is not permission. Anything other than a real verdict
            # stops the run rather than being read as a grant.
            raise BudgetUnavailable(
                f"registry_claim_request: expected a verdict, got {answer!r}")
        return answer

    def claim(self, kind: str, entity_id: str | None = None,
              key: str | None = None, max_wait_s: float = 900.0,
              log: Callable[[str], None] = print) -> str:
        """Wait for a slot and return its request id.

        Waits only for reasons that clear on their own, and only up to
        max_wait_s. Anything else — the source switched off, the circuit open —
        stops the run instead of sleeping through it.
        """
        waited = 0.0
        while True:
            answer = self.try_claim(kind, entity_id, key)
            if answer.get("granted"):
                return answer["request_id"]

            reason = answer.get("reason") or "unknown"
            if reason not in TRANSIENT:
                raise BudgetDenied(reason, answer.get("detail"))

            wait = float(answer.get("wait_seconds") or 1)
            if waited + wait > max_wait_s:
                raise BudgetDenied(
                    reason, f"would wait {int(waited + wait)}s, over the "
                            f"{int(max_wait_s)}s this run allows")
            log(f"[budget] {reason}: sleeping {int(wait)}s")
            self._sleep(wait)
            waited += wait

    def finish(self, request_id: str, outcome: str,
               http_status: int | None = None, note: str | None = None) -> None:
        try:
            self._rpc("registry_finish_request", {
                "p_request_id": request_id, "p_outcome": outcome,
                "p_http_status": http_status, "p_note": note})
        except Exception as exc:                       # noqa: BLE001 - deliberate
            # The request already happened and is already counted. Losing its
            # outcome is a reporting loss, not a budget loss, and must never
            # abort a run that is otherwise behaving — least of all a run whose
            # whole purpose was to collect evidence.
            print(f"[budget] could not record the outcome ({exc}); continuing")

    def open_circuit(self, reason: str, minutes: int = 360) -> None:
        """Park the source in the database, so the next scheduled run finds the
        door shut instead of walking into the same wall from the same IP."""
        try:
            self._rpc("registry_open_circuit", {
                "p_source": self.source, "p_reason": reason, "p_minutes": minutes})
        except Exception as exc:                       # noqa: BLE001 - deliberate
            print(f"[budget] could not park the source ({exc}); continuing")

    def status(self) -> dict[str, Any]:
        return self._rpc("registry_budget_status", {"p_source": self.source})
