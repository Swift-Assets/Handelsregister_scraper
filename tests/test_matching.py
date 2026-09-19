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
