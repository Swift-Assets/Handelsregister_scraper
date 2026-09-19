# Registerportal worker — الغرض التجاري للشركات المُعسرة

> **بالعربية، في ثلاثة أسطر.** يجلب هذا العامل **غرض الشركة** (Unternehmensgegenstand) —
> ماذا تعمل هذه الشركة بالضبط — من بوابة السجل التجاري الرسمية، لكل شركة أُعلن إفلاسها،
> استدعاءً واحداً لكل شركة، من خادم واحد بعنوان IP ثابت.
>
> اليوم لدينا وصف موثوق لـ**730 شركة من 17,061** (4.3%)؛ الباقي إمّا لا شيء أو تخمين من
> اسم الشركة. هذا العامل هو ما يغلق تلك الفجوة، وكلفته ≈ 4,50 €/شهر، وهي كلفة الخادم لا أكثر.
>
> **لم يُشغَّل بعد ضد البوابة.** الخطوة التالية هي تشغيلة المعايرة (30 طلباً) على الخادم،
> وهي التي تقرّر إن كان المشروع ممكناً أصلاً.

---

## What it is

One worker, one fixed IP, one field. For every insolvent company in
`swift_v2.portal_entities` it performs at most one search and one document
retrieval against the official Registerportal, reads the registered purpose out
of the Strukturierter Registerinhalt (SI, XJustiz XML), and writes it to
`swift_v2.company_activity_sources` where the Cockpit already reads it.

It is not a register mirror. It fetches one field, for one company, each time
that company appears in a published insolvency announcement.

## Why the design looks the way it does

Two facts shaped everything here.

**The portal counts retrievals per IP address, and its Nutzungsordnung sets 60
an hour.** The previous attempt kept that budget in a Python object, so a
restart, a crash or a second process reset it to zero. The budget is now a
database table with a locked config row, and this code has no counter of its
own: it asks `swift_v2.registry_claim_request` before every single request and
obeys the answer. Six concurrent claims for one remaining slot grant exactly
one.

**The June 2026 attempt reached the document step 21 times and got an empty
269-byte shell 21 times.** Nothing in this repository assumes that is fixed.
`handelsregister/calibrate.py` is a bounded experiment — 30 requests, one a
minute — whose only job is to answer whether a document retrieval delivers a
document, and what one company really costs. Everything else waits on that.

## Safety, without waiting for an answer from anyone

The Whitelist-IP application at the Servicestelle Registerportal (AG Hagen) is
sent and unanswered. **Nothing here depends on it.** The shipped defaults are
40 requests an hour, 700 a day, 45 seconds apart, inside a 05:00–22:00 UTC
window, and the schema refuses any value above 55/h or 900/day while
`whitelist_granted` is false. If the answer never comes, or comes back
negative, the correct configuration is the one already in place.

A block, a 429 or three failures in a row do not just end the run: they write a
cooldown into the config row, so the next scheduled run finds the door shut
instead of walking into the same wall from the same address.

## Personal data

A register document names the company's representatives. This worker keeps
**their names and nothing else**. Birth dates, birth places, private addresses
and contact details are removed by `xjustiz.redact_si` before the bytes are
stored, logged or written to disk, and the rule is a whitelist — a field a
future XJustiz version adds is dropped by default rather than kept by accident.
`tests/test_xjustiz.py` asserts this on a document that carries all of them.

## Layout

| Path | What lives there |
|---|---|
| `handelsregister/config.py` | Environment and the refusals that stop a misconfigured run before it reaches the portal |
| `handelsregister/budget.py` | The client for the database budget gate — the only way a request is allowed |
| `handelsregister/portal.py` | Playwright driver. Every selector in one dict, so a portal redesign is a one-file fix |
| `handelsregister/matching.py` | Whether a search result really is this company. Register triple first, name only as a last resort |
| `handelsregister/xjustiz.py` | SI parser and the redactor |
| `handelsregister/store.py` | PostgREST writes, all idempotent |
| `handelsregister/run.py` | The orchestrator for a systemd timer |
| `handelsregister/calibrate.py` | The 30-request experiment |
| `migrations/0001_*.sql` | Budget config, request ledger, raw-document store, and the gate |
| `docs/RUNBOOK.md` | How to install, calibrate, enable, and what to do when it goes red |

## Tests

No network, no browser, no database:

```
python -m unittest discover -s tests -t .
```

The migration has its own behavioural test, for a throwaway database only:

```
psql -v ON_ERROR_STOP=1 -f migrations/0000_test_stubs.sql
psql -v ON_ERROR_STOP=1 -f migrations/0001_registry_budget_and_ledger.sql
psql -v ON_ERROR_STOP=1 -f migrations/0001_test.sql
```

## Rules that are not negotiable

See `CLAUDE.md`. In short: the budget lives in the database; the defaults are
safe without a Whitelist; no birth data or home addresses, ever; only from the
fixed-IP host; and nothing shown to a customer is called "Handelsregister"
(§ 8 Abs. 2 HGB).
