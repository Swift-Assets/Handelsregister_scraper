"""The circuit breaker.

The sliding-window budget that used to live here is gone: it counted in this
process's memory, so a restart or a second process reset it to zero. The
counter is swift_v2.registry_request_ledger now (see budget.py).

What stays here is the breaker, because it is about this run's health. Its
durable half — parking the source so the NEXT run does not repeat the mistake —
is Budget.open_circuit().
"""

from __future__ import annotations

# Signals that mean the portal is pushing back. One is enough; there is no
# counting up to these.
FATAL = {"http_403", "http_429", "ip_blocked", "portal_error_page",
         "session_expired"}

# How long the source is parked, by kind of signal. A block costs a day; a
# single error page costs an hour.
COOLDOWN_MINUTES = {
    "http_403": 1440,
    "ip_blocked": 1440,
    "http_429": 720,
    "portal_error_page": 60,
    "session_expired": 30,
}
DEFAULT_COOLDOWN_MINUTES = 360


class CircuitBreaker:
    """Opens on a fatal signal, or after ``max_consecutive`` failures in a row.
    Once open the run stops; it never resets itself inside a run."""

    def __init__(self, max_consecutive: int = 3) -> None:
        self.max_consecutive = max_consecutive
        self.consecutive = 0
        self.open = False
        self.reason: str | None = None

    def success(self) -> None:
        self.consecutive = 0

    def failure(self, kind: str) -> None:
        self.consecutive += 1
        if kind in FATAL:
            self.open, self.reason = True, kind
        elif self.consecutive >= self.max_consecutive:
            self.open, self.reason = True, f"consecutive_failures:{self.consecutive}"

    def cooldown_minutes(self) -> int:
        return COOLDOWN_MINUTES.get(self.reason or "", DEFAULT_COOLDOWN_MINUTES)
