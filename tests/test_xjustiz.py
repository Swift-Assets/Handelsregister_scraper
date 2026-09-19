import os
import unittest

from handelsregister.xjustiz import (REDACTION_RULE, condense_purpose, parse_si,
                                     redact_si)

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "si_sample.xml")

# A person carrying everything we must never keep: birth date, birth place and
# a private address, alongside a company address that must survive.
PERSON_WITH_EVERYTHING = b"""<?xml version="1.0" encoding="UTF-8"?>
<tns:nachricht xmlns:tns="http://www.xjustiz.de">
  <tns:organisation>
    <tns:bezeichnung.aktuell>Muster Handels GmbH</tns:bezeichnung.aktuell>
    <tns:anschrift><tns:strasse>Firmenweg</tns:strasse><tns:hausnummer>1</tns:hausnummer>
      <tns:postleitzahl>40211</tns:postleitzahl><tns:ort>Duesseldorf</tns:ort></tns:anschrift>
  </tns:organisation>
  <tns:natuerlichePerson>
    <tns:vollerName><tns:vorname>Max</tns:vorname><tns:nachname>Mustermann</tns:nachname></tns:vollerName>
    <tns:geburt><tns:geburtsdatum>1981-07-19</tns:geburtsdatum><tns:geburtsort>Kassel</tns:geburtsort></tns:geburt>
    <tns:anschrift><tns:strasse>Privatstrasse</tns:strasse><tns:hausnummer>9</tns:hausnummer>
      <tns:postleitzahl>50667</tns:postleitzahl><tns:ort>Koeln</tns:ort></tns:anschrift>
    <tns:kontaktdaten><tns:email>max@example.invalid</tns:email></tns:kontaktdaten>
  </tns:natuerlichePerson>
  <tns:gegenstand>Der Handel mit Waren aller Art.</tns:gegenstand>
</tns:nachricht>"""


class TestParseSi(unittest.TestCase):
    def setUp(self):
        with open(FIX, "rb") as f:
            self.xml = f.read()

    def test_extracts_profile_fields(self):
        p = parse_si(self.xml)
        self.assertEqual(p.firma, "Beispiel Bau GmbH")
        self.assertEqual(p.rechtsform, "Gesellschaft mit beschränkter Haftung")
        self.assertEqual(p.sitz, "Solingen")
        self.assertEqual(p.anschrift, "Musterstraße 12, 42651 Solingen")
        self.assertTrue(p.gegenstand.startswith("Die Ausführung von Hochbau"))
        self.assertTrue(p.has_purpose())
        self.assertEqual(p.status, "aktuell")
        self.assertEqual(p.stammkapital, "25000 EUR")
        self.assertEqual((p.registergericht, p.registerart, p.registernummer),
                         ("Wuppertal", "HRB", "37064"))
        self.assertTrue(p.is_usable())

    def test_document_hash_is_sha256(self):
        self.assertEqual(len(parse_si(self.xml).document_hash), 64)

    def test_representative_names_only(self):
        p = parse_si(self.xml)
        self.assertEqual(p.vertretungsberechtigte, ["Erika Musterfrau"])
        self.assertNotIn("1970", repr(p))

    def test_company_address_is_not_a_persons_address(self):
        p = parse_si(PERSON_WITH_EVERYTHING)
        self.assertEqual(p.anschrift, "Firmenweg 1, 40211 Duesseldorf")
        self.assertNotIn("Privatstrasse", p.anschrift)

    def test_html_shell_and_junk_are_rejected(self):
        with self.assertRaises(ValueError) as cm:
            parse_si(b"<!DOCTYPE html><html><body>Bitte warten</body></html>")
        self.assertEqual(str(cm.exception), "html_shell")
        with self.assertRaises(ValueError) as cm:
            parse_si(b"plain text")
        self.assertEqual(str(cm.exception), "not_xml")


class TestRedaction(unittest.TestCase):
    def test_birth_data_and_private_address_are_removed(self):
        out, rule = redact_si(PERSON_WITH_EVERYTHING)
        self.assertEqual(rule, REDACTION_RULE)
        for forbidden in (b"1981-07-19", b"Kassel", b"Privatstrasse",
                          b"50667", b"Koeln", b"max@example.invalid"):
            self.assertNotIn(forbidden, out, f"{forbidden!r} survived redaction")

    def test_name_company_address_and_purpose_survive(self):
        out, _ = redact_si(PERSON_WITH_EVERYTHING)
        for kept in (b"Max", b"Mustermann", b"Firmenweg", b"40211",
                     b"Der Handel mit Waren aller Art."):
            self.assertIn(kept, out)

    def test_redacted_output_is_still_parseable(self):
        out, _ = redact_si(PERSON_WITH_EVERYTHING)
        p = parse_si(out)
        self.assertEqual(p.firma, "Muster Handels GmbH")
        self.assertEqual(p.vertretungsberechtigte, ["Max Mustermann"])

    def test_redaction_is_a_whitelist_so_unknown_person_fields_go(self):
        xml = (b'<?xml version="1.0"?><n xmlns="http://www.xjustiz.de">'
               b"<natuerlichePerson><vollerName><nachname>Doe</nachname></vollerName>"
               b"<steueridentifikationsnummer>12345678901</steueridentifikationsnummer>"
               b"</natuerlichePerson></n>")
        out, _ = redact_si(xml)
        self.assertNotIn(b"12345678901", out)
        self.assertIn(b"Doe", out)

    def test_fixture_birth_date_is_removed(self):
        with open(FIX, "rb") as f:
            out, _ = redact_si(f.read())
        self.assertNotIn(b"1970-01-01", out)
        self.assertIn(b"Musterfrau", out)


class TestCondensePurpose(unittest.TestCase):
    def test_first_two_sentences(self):
        self.assertEqual(condense_purpose("Satz eins. Satz zwei. Satz drei."),
                         "Satz eins. Satz zwei.")

    def test_none_and_cap(self):
        self.assertIsNone(condense_purpose(None))
        self.assertIsNone(condense_purpose("   "))
        self.assertTrue(condense_purpose("A" * 500 + " B").endswith("…"))


if __name__ == "__main__":
    unittest.main()
