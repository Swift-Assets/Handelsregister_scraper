# Runbook — the fixed-IP host

Everything here happens on one machine: the Hetzner server in Falkenstein that
carries the Primary IP named in the Whitelist application. Never from a laptop,
never from GitHub Actions, never from an Edge Function. The portal counts
retrievals per address and our application names one address.

---

## 1. Install (Ubuntu 24.04, once)

```bash
sudo apt-get update && sudo apt-get install -y python3-venv
git clone https://github.com/Swift-Assets/handelsregister_scraper.git ~/hr-scraper
cd ~/hr-scraper
python3 -m venv ~/hr && . ~/hr/bin/activate
pip install -r requirements.txt
python -m playwright install --with-deps chromium
```

Confirm the machine really leaves by the registered address — this is the check
that proves the whole arrangement, and it costs nothing:

```bash
curl -4 ifconfig.me      # must print the Primary IP from the Whitelist letter
```

## 2. Secrets

In `/etc/hr-scraper.env`, owned by root, mode 600. Never in the repository:

```
SUPABASE_URL=https://<project>.supabase.co
SUPABASE_SERVICE_ROLE_KEY=<service role key>
HR_CONTACT_EMAIL=<a mailbox a person actually reads>
```

`HR_CONTACT_EMAIL` has no default and the worker refuses to start without it. It
goes into the User-Agent, so a portal operator reading their logs can see who is
asking and write to us. That is the point of it.

## 3. Apply the migration

`migrations/0001_registry_budget_and_ledger.sql`, once, against the Supabase
project. It adds three tables, four functions, one view and one nullable column;
it reads and changes nothing that exists. Its rollback is in its header.

Afterwards the source is **disabled** and nothing can run. That is deliberate.

## 4. The calibration run — do this before anything else

This is the experiment that decides whether the project is possible. It asks:
does a document retrieval deliver a document, and what does one company cost?

Open the door just wide enough for it:

```sql
update swift_v2.registry_source_config
   set enabled = true, max_per_hour = 30, max_per_day = 30,
       min_seconds_between = 60, cooldown_until = null,
       updated_by = 'calibration'
 where source = 'handelsregister';
```

Run it:

```bash
cd ~/hr-scraper && . ~/hr/bin/activate
set -a && . /etc/hr-scraper.env && set +a
HR_CALIBRATION_CONFIRM=I-UNDERSTAND \
python -m handelsregister.calibrate --limit 5 --out ~/calibration
```

It sends at most 30 requests, one a minute, asks the database gate before each
one, writes nothing to the product tables, and prints a verdict:

| verdict | meaning | what happens next |
|---|---|---|
| `purpose_reachable` | a document arrived and carried the purpose | go to step 5 |
| `no_document_content` | the 2026 shell is still there | **stop.** There is no source for the purpose inside the budget; report it |
| `inconclusive` | the run never got far enough | read the JSON report, fix, run again |

Read `requests_per_company` in the summary. At 2 the backlog clears in about
three weeks; at 5 it takes months. Everything downstream is planned off that
number, so do not estimate it — read it.

Close the door again afterwards:

```sql
update swift_v2.registry_source_config
   set enabled = false, updated_by = 'calibration-done'
 where source = 'handelsregister';
```

## 5. Going live

Start narrow: the daily inflow only, a cap well under the line, one week.

```sql
update swift_v2.registry_source_config
   set enabled = true, max_per_hour = 20, max_per_day = 300,
       min_seconds_between = 45,
       allowed_hour_start_utc = 5, allowed_hour_end_utc = 22,
       updated_by = 'phase-4-soak'
 where source = 'handelsregister';
```

A dry run writes nothing and is the right way to watch the first hour:

```bash
HR_DRY_RUN=1 HR_MAX_COMPANIES=5 python -m handelsregister.run
```

Then the timer. `HR_MAX_COMPANIES` is a per-run ceiling, not a rate — the rate
is the database's business:

```ini
# /etc/systemd/system/hr-scraper.service
[Service]
Type=oneshot
User=hr
WorkingDirectory=/home/hr/hr-scraper
EnvironmentFile=/etc/hr-scraper.env
Environment=HR_DRY_RUN=0
Environment=HR_MAX_COMPANIES=15
ExecStart=/home/hr/hr/bin/python -m handelsregister.run
```

```ini
# /etc/systemd/system/hr-scraper.timer
[Timer]
OnCalendar=*-*-* *:07:00
RandomizedDelaySec=600
Persistent=false

[Install]
WantedBy=timers.target
```

`RandomizedDelaySec` matters: a request at exactly :07:00 of every hour for
months is a signature. `Persistent=false` matters too — a machine that was off
must not wake up and fire every missed run at once.

## 5a2. How a hit is confirmed

A result row on this portal reads `<name>  <seat city>  aktuell` — **no court,
no register number**. So the row can never prove identity. Two things do:

1. **The form.** Court, register type and number are search fields. One row
   from a query constrained by court AND number is the portal matching for us.
   The worker records which fields the form actually accepted; a field that
   silently failed to set turns a precise query into a name search, and a
   name search alone is never accepted.
2. **The document.** An SI states its own Registergericht, Registerart and
   Registernummer. That comparison is proof, and it runs after every download.
   A document that names another register entry is refused and never stored.

## 5a. What one company really costs

Measured on the first live run: the portal's result page carries **no search
field**, so every company after the first needs a navigation back to the form
before it can be searched. Counted conservatively that is:

    back to the form  +  search  +  document   =  3 requests per company

not the 2 the Nutzungsordnung's own unit implies. The calibration prints
`requests per company`; plan the backlog off that number and nothing else.

## 5b. When the portal refuses: probe before you conclude

A refusal names a kind — `portal_error_page`, `ip_blocked` — and that name alone
cannot tell a portal that is down from a marker of ours matching ordinary text
on a healthy page. Do not settle it by running the calibration again: that
costs thirty requests and usually returns "inconclusive" a second time.

Spend one request instead:

```bash
su - hr -c '/usr/local/bin/hr-probe'
```

It asks for the front page once, keeps the rendered HTML and a screenshot, and
prints the HTTP status, the page title, its size, and the exact marker our
guard matched. Then read the saved page:

| what the evidence shows | what it means |
|---|---|
| a few hundred characters, or an error title | the portal really is refusing or down — wait it out |
| a full page that merely contains one of our marker phrases | **our guard is too eager**; narrow the marker, do not widen the retry |
| HTTP 403, or `gesperrt` in a small page | a block. Do not clear the cooldown; read §6 |

The probe obeys the same budget gate as everything else, so it will refuse
while a cooldown is open. Clearing that cooldown is a decision, not a
formality — §6 says when it is justified.

**Re-judging a saved page costs nothing at all.** Once a page is on disk, the
guard can be run against it again with no portal request, no database and no
budget:

```bash
su - hr -c '/home/hr/venv/bin/python -m handelsregister.probe --file /home/hr/evidence/<file>.html'
```

Use this to prove a change to the guard is right **before** it is allowed near
the portal again. On 2026-09-19 the guard refused the portal's healthy welcome
page three times in a row because the portal ships an empty error panel on
every page; the fix was verified this way, at zero cost, before a single
further request was sent.

## 6. When it goes red

```sql
select * from swift_v2.v_registry_budget_status;
```

`cooldown_until` in the future means the breaker parked the source, and
`cooldown_reason` says why.

| reason | what it means | what to do |
|---|---|---|
| `ip_blocked`, `http_403` | the portal is refusing this address | **do not clear it.** 24 hours, then read the ledger, then tell the owner. Clearing it and retrying is how an address becomes permanently blocked |
| `http_429` | too fast, by their counting not ours | lower `max_per_hour`, raise `min_seconds_between`, wait out the 12 hours |
| `portal_error_page` | their error page, usually transient | one hour, then a dry run of 1 company |
| `session_expired` | our session went stale mid-run | harmless; the next run opens a new one |
| `consecutive_failures:N` | three in a row, no single fatal signal | read the last ledger rows; it is usually a changed selector |

To clear a cooldown deliberately, after reading why it is there:

```sql
update swift_v2.registry_source_config
   set cooldown_until = null, cooldown_reason = null, updated_by = '<who, and why>'
 where source = 'handelsregister';
```

## 7. If the Whitelist is granted

One statement, with the granted figures and nothing invented:

```sql
update swift_v2.registry_source_config
   set whitelist_granted = true,
       whitelist_note = 'AG Hagen, <date>, <reference>: <granted figures>',
       max_per_hour = <granted>, max_per_day = <granted>,
       updated_by = 'whitelist-granted'
 where source = 'handelsregister';
```

The schema will not accept more than 150/h or 1000/day even then, because those
are the figures the application asked for. Asking for one thing and doing
another is how a registration gets revoked.

## 8. If the Whitelist is refused, or never answered

Nothing to do. The defaults in the migration are the safe configuration, and
they cover the daily inflow of roughly 182 companies with room to spare. Only
the backlog is slower — and a company's registered purpose does not expire, so
a slow backlog loses nothing.

## 9. After the calibration: the files on the machine

`~/calibration/` holds the saved documents. The XML files are redacted. **Any
PDF is not** — we have no redactor for PDF, an AD carries the directors' birth
dates, and the calibration marks those files with a warning. Read them, then
delete them:

```bash
rm -f ~/calibration/*.pdf
```
