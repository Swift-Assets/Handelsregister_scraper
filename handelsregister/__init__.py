"""Registerportal worker — company purpose (Unternehmensgegenstand) only.

Fetches the Strukturierter Registerinhalt (SI, XJustiz XML) of an insolvent
company from the official Registerportal, ONE retrieval per company, and writes
the registered purpose into swift_v2.company_activity_sources so the Cockpit can
say what a company actually did.

Three rules hold everywhere in this package:

  * The budget lives in the database, never in this process. Every request asks
    swift_v2.registry_claim_request first and obeys the answer.
  * The default configuration is safe without a Whitelist-IP and stays safe if
    the application is refused. Nothing here has to change for that case.
  * A natural person's birth data and private address are never stored, never
    logged and never written to disk — they are stripped from the document
    before it leaves memory.
"""

__all__ = ["__version__"]
__version__ = "0.1.0"
