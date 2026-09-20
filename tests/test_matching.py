import unittest
from dataclasses import dataclass, field

from handelsregister.matching import (METHOD_TRIPLE, METHOD_UNIQUE_NAME,
                                      fold_court, fold_name, pick_hit)


@dataclass
class Hit:
    company_name: str = ""
    registry_court: str | None = None
    registry_type: str | None = None
    registry_number: str | None = None
    document_links: dict = field(default_factory=lambda: {"SI": "l1"})


ENTITY = {"entity_id": "e1", "display_name": "Beispiel Bau GmbH",
          "registry_court": "Wuppertal", "registry_type": "HRB",
          "registry_number": "37064", "registry_identity_key": "k"}


class TestFolding(unittest.TestCase):
    def test_court_folding_drops_amtsgericht_and_state(self):
        self.assertEqual(fold_court("Bayern Amtsgericht Aschaffenburg"),
                         fold_court("Aschaffenburg"))
        self.assertEqual(fold_court("Nordrhein-Westfalen Amtsgericht Köln"),
                         fold_court("Koeln"))
        self.assertEqual(fold_court("Frankfurt am Main"), "frankfurt am main")

    def test_bad_homburg_is_not_homburg(self):
        # Two real, different courts in two different states. A suffix match
        # would merge them and attach companies to the wrong register.
        self.assertNotEqual(fold_court("Bad Homburg v.d. Höhe"), fold_court("Homburg"))

    def test_name_folding_drops_legal_form(self):
        self.assertEqual(fold_name("Beispiel Bau GmbH"), fold_name("Beispiel Bau mbH"))
        self.assertEqual(fold_name("Müller & Co. KG"), fold_name("Mueller & Co KG"))


class TestPickHit(unittest.TestCase):
    def test_triple_match_is_accepted(self):
        r = pick_hit([Hit("Beispiel Bau GmbH", "Wuppertal", "HRB", "37064")], ENTITY)
        self.assertTrue(r.accepted)
        self.assertEqual(r.method, METHOD_TRIPLE)

    def test_triple_match_survives_a_different_court_spelling(self):
        hit = Hit("X", "Bayern Amtsgericht Wuppertal", "HRB", "37064")
        self.assertTrue(pick_hit([hit], ENTITY).accepted)

    def test_same_number_at_another_court_is_refused(self):
        # This is the failure the old name-based rule could not see: an HRB
        # number is unique only inside its court.
        r = pick_hit([Hit("Beispiel Bau GmbH", "Berlin", "HRB", "37064")], ENTITY)
        self.assertFalse(r.accepted)
        self.assertEqual(r.reason, "no_triple_match")

    def test_wrong_register_type_is_refused(self):
        r = pick_hit([Hit("Beispiel Bau GmbH", "Wuppertal", "HRA", "37064")], ENTITY)
        self.assertFalse(r.accepted)

    def test_a_single_hit_with_the_wrong_triple_is_still_refused(self):
        # The old rule accepted "the only row with a document link".
        r = pick_hit([Hit("Beispiel Bau GmbH", "Wuppertal", "HRB", "99999")], ENTITY)
        self.assertFalse(r.accepted)

    def test_two_identical_triples_are_ambiguous(self):
        hits = [Hit("A", "Wuppertal", "HRB", "37064"),
                Hit("B", "Wuppertal", "HRB", "37064")]
        self.assertEqual(pick_hit(hits, ENTITY).reason, "ambiguous_triple")

    def test_missing_si_link_is_reported_as_such(self):
        hits = [Hit("A", "Wuppertal", "HRB", "37064", document_links={"AD": "x"})]
        self.assertEqual(pick_hit(hits, ENTITY).reason, "no_si_link")

    def test_no_hits(self):
        self.assertEqual(pick_hit([], ENTITY).reason, "no_hits")

    def test_name_fallback_only_without_a_triple_of_our_own(self):
        entity = dict(ENTITY, registry_court=None, registry_type=None,
                      registry_number=None)
        r = pick_hit([Hit("Beispiel Bau mbH")], entity)
        self.assertTrue(r.accepted)
        self.assertEqual(r.method, METHOD_UNIQUE_NAME)

    def test_name_fallback_refuses_a_different_company(self):
        entity = dict(ENTITY, registry_court=None, registry_type=None,
                      registry_number=None)
        self.assertEqual(pick_hit([Hit("Ganz Andere GmbH")], entity).reason,
                         "name_mismatch")

    def test_name_fallback_refuses_more_than_one_candidate(self):
        entity = dict(ENTITY, registry_court=None, registry_type=None,
                      registry_number=None)
        self.assertEqual(
            pick_hit([Hit("Beispiel Bau GmbH"), Hit("Beispiel Bau GmbH")], entity).reason,
            "ambiguous_no_triple")


if __name__ == "__main__":
    unittest.main()


class _Profile:
    def __init__(self, court=None, art=None, number=None):
        self.registergericht, self.registerart, self.registernummer = court, art, number


CONSTRAINED = {"keywords": True, "register_number": True,
               "register_type": True, "court": True}
NAME_ONLY = {"keywords": True, "register_number": False,
             "register_type": False, "court": False}


class TestPortalFilteredMatch(unittest.TestCase):
    """Measured on the live portal 2026-09-20: a result row reads
    "<name>  <seat city>  aktuell" and states no court and no register number.
    The row can never confirm the triple. The FORM can — court, register type
    and number are search fields — so one row from a constrained query is the
    portal doing the matching for us."""

    def _row(self, name="Beispiel Bau GmbH"):
        return Hit(name, None, None, None)          # no triple, as in real life

    def test_one_row_from_a_constrained_query_is_accepted(self):
        from handelsregister.matching import METHOD_PORTAL_FILTERED
        r = pick_hit([self._row()], ENTITY, "SI", CONSTRAINED)
        self.assertTrue(r.accepted)
        self.assertEqual(r.method, METHOD_PORTAL_FILTERED)

    def test_a_name_search_alone_is_never_enough(self):
        # Without the court, an HRB number is not unique. Refuse.
        r = pick_hit([self._row()], ENTITY, "SI", NAME_ONLY)
        self.assertFalse(r.accepted)
        self.assertEqual(r.reason, "search_not_constrained")

    def test_a_field_the_form_silently_refused_downgrades_the_match(self):
        # The court field is a PrimeFaces widget that can refuse a value
        # without saying so. When it does, the hit is still usable — but as a
        # CANDIDATE under the weaker method, never as the strong one, and the
        # document then has to confirm it positively before anything is kept.
        from handelsregister.matching import METHOD_NUMBER_AND_NAME
        half = dict(CONSTRAINED, court=False)
        r = pick_hit([self._row()], ENTITY, "SI", half)
        self.assertTrue(r.accepted)
        self.assertEqual(r.method, METHOD_NUMBER_AND_NAME)

    def test_two_rows_are_ambiguous_even_when_constrained(self):
        r = pick_hit([self._row(), self._row("Andere GmbH")], ENTITY, "SI", CONSTRAINED)
        self.assertEqual(r.reason, "ambiguous_portal_filtered")

    def test_a_different_company_is_refused(self):
        r = pick_hit([self._row("Ganz Andere GmbH")], ENTITY, "SI", CONSTRAINED)
        self.assertEqual(r.reason, "name_mismatch")

    def test_a_row_that_does_state_a_triple_is_still_judged_on_it(self):
        from handelsregister.matching import METHOD_TRIPLE
        good = Hit("Beispiel Bau GmbH", "Wuppertal", "HRB", "37064")
        self.assertEqual(pick_hit([good], ENTITY, "SI", CONSTRAINED).method,
                         METHOD_TRIPLE)
        wrong = Hit("Beispiel Bau GmbH", "Berlin", "HRB", "37064")
        self.assertFalse(pick_hit([wrong], ENTITY, "SI", CONSTRAINED).accepted)


class TestDocumentIsTheProof(unittest.TestCase):
    """A search result is inference. The document states its own register
    entry, and that is the only non-circumstantial evidence we get."""

    def test_the_document_confirms_the_entry(self):
        from handelsregister.matching import verify_against_document
        ok, why = verify_against_document(ENTITY, _Profile("Wuppertal", "HRB", "37064"))
        self.assertTrue(ok, why)

    def test_a_document_for_another_company_is_rejected(self):
        from handelsregister.matching import verify_against_document
        ok, why = verify_against_document(ENTITY, _Profile("Berlin", "HRB", "37064"))
        self.assertFalse(ok)
        self.assertIn("entity says", why)

    def test_a_different_number_is_rejected(self):
        from handelsregister.matching import verify_against_document
        ok, _ = verify_against_document(ENTITY, _Profile("Wuppertal", "HRB", "99999"))
        self.assertFalse(ok)

    def test_a_silent_document_is_not_evidence_against_us(self):
        from handelsregister.matching import verify_against_document
        ok, why = verify_against_document(ENTITY, _Profile())
        self.assertTrue(ok)
        self.assertIn("states no triple", why)


NUMBER_ONLY = {"keywords": True, "register_number": True,
               "register_type": False, "court": False}


class TestNumberWithoutCourt(unittest.TestCase):
    """Measured 2026-09-20: the court and register-type fields are PrimeFaces
    widgets that silently refused to take a value, so the live search went out
    with name and number only. A register number is not unique across courts,
    so that hit is a candidate — never a conclusion."""

    def _row(self, name="Beispiel Bau GmbH"):
        return Hit(name, None, None, None)

    def test_number_and_name_alone_buy_a_candidate(self):
        from handelsregister.matching import METHOD_NUMBER_AND_NAME
        r = pick_hit([self._row()], ENTITY, "SI", NUMBER_ONLY)
        self.assertTrue(r.accepted)
        self.assertEqual(r.method, METHOD_NUMBER_AND_NAME,
                         "a weaker match must not be labelled as the strong one")

    def test_with_the_court_it_is_the_strong_match(self):
        from handelsregister.matching import METHOD_PORTAL_FILTERED
        self.assertEqual(pick_hit([self._row()], ENTITY, "SI", CONSTRAINED).method,
                         METHOD_PORTAL_FILTERED)

    def test_no_number_at_all_is_still_refused(self):
        self.assertEqual(pick_hit([self._row()], ENTITY, "SI", NAME_ONLY).reason,
                         "search_not_constrained")

    def test_a_silent_document_cannot_confirm_a_weak_match(self):
        from handelsregister.matching import verify_against_document
        ok, why = verify_against_document(ENTITY, _Profile(), require_positive=True)
        self.assertFalse(ok)
        self.assertIn("not narrowed to a court", why)

    def test_a_silent_document_is_tolerated_after_a_strong_match(self):
        from handelsregister.matching import verify_against_document
        ok, _ = verify_against_document(ENTITY, _Profile(), require_positive=False)
        self.assertTrue(ok)

    def test_a_wrong_document_is_refused_however_the_match_was_made(self):
        from handelsregister.matching import verify_against_document
        wrong = _Profile("Berlin", "HRB", "37064")
        for strict in (True, False):
            self.assertFalse(verify_against_document(ENTITY, wrong, strict)[0])
