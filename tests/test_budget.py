import unittest

import requests

from handelsregister.budget import Budget, BudgetDenied, BudgetUnavailable


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self._payload


class FakeSession:
    """Scripted PostgREST. Each entry is a payload, a status, or an exception."""

    def __init__(self, script):
        self.headers = {}
        self.script = list(script)
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append((url.rsplit("/", 1)[-1], json))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, tuple):
            return FakeResponse(item[0], item[1])
        return FakeResponse(item)


def make(script, slept=None):
    return Budget("https://db.example", "key", "run-1",
                  session=FakeSession(script),
                  sleep=(slept.append if slept is not None else (lambda s: None)))


GRANTED = {"granted": True, "request_id": "r-1", "remaining_hour": 39}


class TestClaim(unittest.TestCase):
    def test_a_grant_returns_the_request_id(self):
        b = make([GRANTED])
        self.assertEqual(b.claim("search", log=lambda m: None), "r-1")

    def test_it_waits_for_a_transient_refusal_then_proceeds(self):
        slept = []
        b = make([{"granted": False, "reason": "min_gap", "wait_seconds": 12}, GRANTED],
                 slept=slept)
        self.assertEqual(b.claim("document", log=lambda m: None), "r-1")
        self.assertEqual(slept, [12.0])

    def test_it_waits_for_an_exhausted_hour(self):
        slept = []
        b = make([{"granted": False, "reason": "hour_budget_exhausted",
                   "wait_seconds": 300}, GRANTED], slept=slept)
        b.claim("search", log=lambda m: None)
        self.assertEqual(slept, [300.0])

    def test_a_disabled_source_stops_the_run_immediately(self):
        b = make([{"granted": False, "reason": "source_disabled"}])
        with self.assertRaises(BudgetDenied) as cm:
            b.claim("search", log=lambda m: None)
        self.assertEqual(cm.exception.reason, "source_disabled")

    def test_an_open_circuit_is_never_slept_through(self):
        slept = []
        b = make([{"granted": False, "reason": "circuit_open",
                   "wait_seconds": 86400, "detail": "ip_blocked"}], slept=slept)
        with self.assertRaises(BudgetDenied) as cm:
            b.claim("search", log=lambda m: None)
        self.assertEqual(cm.exception.reason, "circuit_open")
        self.assertEqual(slept, [], "a parked source must not be waited out")

    def test_it_refuses_to_sleep_longer_than_the_run_allows(self):
        slept = []
        b = make([{"granted": False, "reason": "day_budget_exhausted",
                   "wait_seconds": 40000}], slept=slept)
        with self.assertRaises(BudgetDenied):
            b.claim("search", max_wait_s=900, log=lambda m: None)
        self.assertEqual(slept, [])

    def test_an_unreachable_gate_stops_the_run(self):
        # No gate, no requests. That is the correct failure direction.
        b = make([requests.RequestException("connection reset")])
        with self.assertRaises(BudgetUnavailable):
            b.claim("search", log=lambda m: None)

    def test_an_http_error_from_the_gate_stops_the_run(self):
        b = make([({"message": "permission denied"}, 403)])
        with self.assertRaises(BudgetUnavailable):
            b.claim("search", log=lambda m: None)

    def test_the_run_id_and_kind_travel_with_every_claim(self):
        b = make([GRANTED])
        b.claim("document", entity_id="e-9", key="k-9", log=lambda m: None)
        name, payload = b.s.calls[0]
        self.assertEqual(name, "registry_claim_request")
        self.assertEqual(payload["p_kind"], "document")
        self.assertEqual(payload["p_run_id"], "run-1")
        self.assertEqual(payload["p_entity_id"], "e-9")


class TestReporting(unittest.TestCase):
    def test_losing_an_outcome_never_aborts_a_healthy_run(self):
        # The request already happened and is already counted; failing to
        # record how it went is a reporting loss, not a budget loss.
        b = make([requests.RequestException("boom")])
        b.finish("r-1", "ok")

    def test_parking_the_source_survives_an_unreachable_gate(self):
        b = make([requests.RequestException("boom")])
        b.open_circuit("ip_blocked", 1440)


if __name__ == "__main__":
    unittest.main()
