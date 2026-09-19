import os
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


class TestDiagnosePage(unittest.TestCase):
    """A verdict must carry its reason. The first live calibration returned
    'portal_error_page' and nothing else, which cannot tell a portal that is
    down from a marker of ours matching ordinary text."""

    def setUp(self):
        from handelsregister.portal import diagnose_page
        self.diagnose = diagnose_page

    def test_a_healthy_page_says_so_and_names_no_marker(self):
        d = self.diagnose("<html><body>Registerportal</body></html>", 200)
        self.assertIsNone(d["kind"])
        self.assertIsNone(d["reason"])
        self.assertEqual(d["matched_markers"],
                         {"block": [], "session": [], "error": []})

    def test_a_status_code_is_reported_as_the_reason(self):
        for status, kind in ((403, "http_403"), (429, "http_429"),
                             (500, "portal_error_page"), (503, "portal_error_page")):
            d = self.diagnose("", status)
            self.assertEqual(d["kind"], kind)
            self.assertIn(str(status), d["reason"])

    def test_the_exact_marker_that_matched_is_named(self):
        d = self.diagnose("<p>Es ist ein Fehler aufgetreten</p>", 200)
        self.assertEqual(d["kind"], "portal_error_page")
        self.assertIn("Es ist ein Fehler aufgetreten", d["reason"])
        self.assertEqual(d["matched_markers"]["error"], ["Es ist ein Fehler aufgetreten"])

    def test_page_size_travels_with_the_verdict(self):
        # A few hundred characters is the portal refusing us; a full page that
        # merely contains a marker phrase is our own guard being too eager.
        d = self.diagnose("x" * 412, 200)
        self.assertEqual(d["html_chars"], 412)

    def test_a_block_outranks_an_error_marker(self):
        d = self.diagnose("Zugriff verweigert. Es ist ein Fehler aufgetreten", 200)
        self.assertEqual(d["kind"], "ip_blocked")

    def test_classify_page_still_answers_the_old_way(self):
        from handelsregister.portal import classify_page
        self.assertEqual(classify_page("Ihre Sitzung ist abgelaufen"), "session_expired")
        self.assertIsNone(classify_page("<html>ok</html>", 200))


class TestPositiveEvidenceWins(unittest.TestCase):
    """2026-09-19: this guard refused the portal's healthy welcome page three
    times in a row. The portal ships an empty error panel on every page — the
    words "Es ist ein Fehler aufgetreten" and "Fehler ID:" sit in the markup of
    a perfectly working site, hidden, waiting for a real error. Reading the
    source for those words rejects the entire portal, forever."""

    REAL_SHAPE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "fixtures", "welcome_with_hidden_error_panel.html")

    def setUp(self):
        from handelsregister.portal import EXPECT_WELCOME, diagnose_page
        self.diagnose = diagnose_page
        self.expect = EXPECT_WELCOME
        with open(self.REAL_SHAPE, encoding="utf-8") as f:
            self.page = f.read()

    def test_the_page_that_broke_us_is_healthy_once_we_look_for_what_we_need(self):
        d = self.diagnose(self.page, 200, expect=self.expect)
        self.assertIsNone(d["kind"])
        self.assertEqual(d["missing_expected"], [])
        # The scary words are still there. They are simply not the question.
        self.assertTrue(d["matched_markers"]["error"])

    def test_without_that_rule_the_same_page_is_refused(self):
        # Exactly the bug, pinned so it cannot come back unnoticed.
        self.assertEqual(self.diagnose(self.page, 200)["kind"], "portal_error_page")

    def test_hidden_markup_is_not_a_message_to_anyone(self):
        # Visible text carries no error, so nothing is wrong — even with no
        # positive expectation to lean on.
        d = self.diagnose(self.page, 200, visible_text="Registerportal Startseite")
        self.assertIsNone(d["kind"])
        self.assertEqual(d["matched_on"], "visible text")

    def test_a_real_error_page_is_still_caught(self):
        broken = ("<html><body><div class='error-message'>"
                  "Es ist ein Fehler aufgetreten!</div></body></html>")
        d = self.diagnose(broken, 200,
                          visible_text="Es ist ein Fehler aufgetreten!",
                          expect=self.expect)
        self.assertEqual(d["kind"], "portal_error_page")
        self.assertIn("visible text", d["reason"])

    def test_a_status_code_outranks_positive_evidence(self):
        # 403 is 403 however friendly the body looks.
        self.assertEqual(self.diagnose(self.page, 403, expect=self.expect)["kind"],
                         "http_403")

    def test_a_page_missing_what_we_came_for_is_named_as_such(self):
        d = self.diagnose("<html><body>Willkommen</body></html>", 200,
                          visible_text="Willkommen", expect=self.expect)
        self.assertEqual(d["kind"], "unexpected_page")
        self.assertEqual(d["missing_expected"], ["normaleSucheLink"])

    def test_a_block_still_outranks_everything_below_the_status(self):
        d = self.diagnose(self.page, 200, visible_text="Ihre IP wurde gesperrt",
                          expect=self.expect)
        self.assertIsNone(d["kind"], "a page that works is not a block page")
        d2 = self.diagnose("<html>nothing we need</html>", 200,
                           visible_text="Ihre IP wurde gesperrt", expect=self.expect)
        self.assertEqual(d2["kind"], "ip_blocked")
