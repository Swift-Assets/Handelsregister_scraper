import unittest

from handelsregister.pacing import CircuitBreaker
from handelsregister.portal import classify_page, parse_registry_triple


class TestCircuitBreaker(unittest.TestCase):
    def test_a_block_opens_it_at_once(self):
        b = CircuitBreaker()
        b.failure("ip_blocked")
        self.assertTrue(b.open)
        self.assertEqual(b.reason, "ip_blocked")
        self.assertEqual(b.cooldown_minutes(), 1440)

    def test_429_opens_it_at_once(self):
        b = CircuitBreaker()
        b.failure("http_429")
        self.assertTrue(b.open)
        self.assertEqual(b.cooldown_minutes(), 720)

    def test_three_ordinary_failures_open_it(self):
        b = CircuitBreaker()
        for _ in range(2):
            b.failure("profile_unusable")
        self.assertFalse(b.open)
        b.failure("profile_unusable")
        self.assertTrue(b.open)
        self.assertIn("consecutive_failures", b.reason)

    def test_a_success_clears_the_streak_but_not_an_open_circuit(self):
        b = CircuitBreaker()
        b.failure("x"); b.failure("x"); b.success(); b.failure("x")
        self.assertFalse(b.open)
        b.failure("ip_blocked")
        b.success()
        self.assertTrue(b.open, "an open circuit never resets inside a run")


class TestClassifyPage(unittest.TestCase):
    def test_http_signals(self):
        self.assertEqual(classify_page("", 403), "http_403")
        self.assertEqual(classify_page("", 429), "http_429")
        self.assertEqual(classify_page("", 503), "portal_error_page")

    def test_page_markers(self):
        self.assertEqual(classify_page("Ihre IP wurde gesperrt"), "ip_blocked")
        self.assertEqual(classify_page("Ihre Sitzung ist abgelaufen"), "session_expired")
        self.assertEqual(classify_page("Es ist ein Fehler aufgetreten"), "portal_error_page")

    def test_a_healthy_page_is_none(self):
        self.assertIsNone(classify_page("<html>Suchergebnisse</html>", 200))


class TestParseRegistryTriple(unittest.TestCase):
    def test_reads_the_portals_own_row_format(self):
        self.assertEqual(
            parse_registry_triple("Bayern Amtsgericht Aschaffenburg HRB 623"),
            ("Aschaffenburg", "HRB", "623"))

    def test_multi_word_court(self):
        self.assertEqual(
            parse_registry_triple("Hessen Amtsgericht Frankfurt am Main HRB 12345"),
            ("Frankfurt am Main", "HRB", "12345"))

    def test_court_suffix_on_the_number(self):
        self.assertEqual(
            parse_registry_triple("Amtsgericht Flensburg HRB 1234 FL"),
            ("Flensburg", "HRB", "1234FL"))

    def test_other_register_types(self):
        for text, want in (("Amtsgericht Kiel VR 1234", "VR"),
                           ("Amtsgericht Kiel GnR 12", "GNR"),
                           ("Amtsgericht Kiel HRA 11401", "HRA")):
            self.assertEqual(parse_registry_triple(text)[1], want)

    def test_a_row_without_a_register_returns_nothing(self):
        # It must be nothing, not a guess: the caller refuses the hit.
        self.assertEqual(parse_registry_triple("Musterfirma GmbH, Berlin"),
                         (None, None, None))
        self.assertEqual(parse_registry_triple(""), (None, None, None))


if __name__ == "__main__":
    unittest.main()
