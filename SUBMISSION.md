# Submission

## Candidate
- **Name:** Rushabh Chiparikar
- **Assignment chosen:** A (Data)
- **Time spent (honest estimate):** ~5h of my own time over two sessions: setup, data
  exploration, running and verifying the pipeline, debugging the install, and reviewing these
  documents. Code-writing time is not included because I did not write the code.

> **Authorship (please read with AI_USAGE.md):** the design, all code and tests, and the first
> draft of this document were written by Claude at my request after I fell behind schedule.
> The decisions below are choices Claude made and explained to me; I've kept its reasoning so
> it can be reviewed. My own part was setup, data exploration, running and timing the pipeline
> on my machine, finding the install problem, and these write-ups. I am studying the code in
> depth before the follow-up interview.

## How to run
Python 3.11+. From a clean clone, at the repo root:
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data

export PYTHONPATH=solution                 # run from the repo root
export LEDGER_PII_KEY='change-me'          # optional; see "PII" below
python -m ledger ingest data/drop_1
python -m ledger ingest data/drop_2
python -m ledger report --out reports/

pytest solution                            # 77 tests, ~3 s
```
Options: `--db PATH` (default `ledger.db`), `--schemas DIR` (default `assignment-a-data/schemas`).
`python -m ledger ingest data/drop_1 data/drop_2` (all at once) gives byte-identical reports.
No package install is needed: `PYTHONPATH=solution` makes the `ledger` package importable.
**One database per dataset:** to run on a different seed, use a new `--db` file or `rm -f ledger.db*` first.
Pointing a different seed at an existing DB is refused with a clear error (its `batch_001` differs).

## Design

**The core idea: store everything at ingest, decide at report time.** Whether an event belongs to
a lead can change later (its lead may arrive in a later drop), so ingest only applies the two
filters whose answer can never change: *malformed* and *duplicate `event_id`*. Linking, orphans,
unknown dispositions, calls, effective dispositions and buckets are derived from the full
database each time `report` runs. That one choice satisfies three requirements together:
orphans that resolve later, drop-by-drop == all-at-once, and an append-only ledger.

```
 drop_N/leads/*.csv ──► validate/normalise ──► leads (current state, merged)   ─┐
                         └─ rejected ─────────► lead_rejections (no PII)        │  report:
 drop_N/events/*.jsonl ► malformed? dup? ─────► events (append-only ledger)    ─┼► JOIN on (workflow, phone_token)
                                                                                 │   ─► derive.py (pure) ─► 5 files
 every file ──────────────────────────────────► ingested_files (sha256 + counters)┘
```

**Schema (SQLite, `solution/ledger/db.py`)**

| Table | Role | Key constraints |
|---|---|---|
| `ingested_files` | Idempotency log + per-file counters | unique `(workflow_id, batch_id)` for leads; unique `content_sha256` for events |
| `leads` | Current state of each lead | unique `(workflow_id, phone_token)` = lead identity |
| `lead_rejections` | Rejected rows: position + reason only | PK `(workflow_id, batch_id, row_number)` |
| `events` | **Append-only interaction ledger** | PK `event_id` (dedupe across drops); triggers abort any UPDATE/DELETE |
| `meta` | PII key fingerprint | refuse to run with a different key |

- **Idempotency:** each file is applied in **one transaction together with its `ingested_files`
  row**. A re-run skips the file, and a crash mid-file rolls back completely, so the next run
  re-applies it cleanly. Counters (`rows_total`, `duplicate_events_dropped`...) are stored per
  file and summed, so re-runs never double-count.
- **Leads** are mutable *state*, not a ledger: merges overwrite non-null fields (last write
  wins) and `batch_id` is never updated. Defaults are applied on read (after merging), so a later
  explicit value always beats a default.
- **Events** keep every first-seen, well-formed event, including orphans and unknown codes,
  because those can become linkable later and the ledger is the source of truth.
  Overrides are new rows; nothing is edited.

**PII (`solution/ledger/pii.py`).** Fields marked `pii` (name, phone) are never stored in clear.
- Phone → `phone_token = HMAC-SHA256(key, "+91XXXXXXXXXX")`. It is deterministic, so event phones
  (normalised the same way) can be joined to leads. A plain SHA-256 would be reversible: there
  are only ~4×10⁹ valid Indian mobiles, which can all be hashed in minutes. The HMAC key prevents
  that.
- Name and canonical phone → Fernet-encrypted JSON (`pii_enc`), recoverable by an authorised
  process (e.g. the dialler).
- Only `phone_last4` is stored in clear, which `phone_masked` needs. Raw event JSON is *not*
  stored (it contains the phone); payloads are stored separately.
- Key from `LEDGER_PII_KEY`. If unset (local dev), a random key is created at `<db>.key`
  (git-ignored) with a warning. HMAC and encryption keys are derived separately from it. A
  fingerprint in `meta` stops a run with the wrong key from silently turning every event into an
  orphan.

**Code layout (`solution/ledger/`)**: `normalize.py` (pure value rules), `leads.py`,
`events.py`, `derive.py` (pure: linking, calls, effective disposition, buckets),
`reports.py`, `db.py`, `pii.py`, `catalog.py`, `cli.py`.

## Decisions & assumptions

| Decision | Why | Alternative considered |
|---|---|---|
| Link events at report time, not ingest | Orphans must resolve when the lead arrives; keeps the ledger immutable | Re-link orphans on each ingest (more state, easy to get wrong) |
| A lead batch is identified by `(workflow, file stem)`; same content → skip; **different content under the same name → error** | `batch_id` is permanent per spec; silently re-merging a changed batch would rewrite history | Treat as new rows (would double-count `lead_rows_total`) |
| Events files identified by SHA-256 of content | A re-delivered identical file is a no-op; a *different* file with overlapping events is still safe via `event_id` dedupe | Key by filename |
| Header: strip again *after* collapsing `_`/whitespace | Spec trims first, so `_name_` would become `" name "` and never match | Literal reading |
| Two columns mapping to the same field → left-most wins | Deterministic | Last wins / reject the file |
| Dates accept 1- or 2-digit day/month (`1/2/2026`) | Same formats, more lenient; all generator dates are zero-padded | Strict 2-digit |
| Decimal: commas just removed (no grouping check, so `1,23,456` is OK); rounding half-up via `Decimal` | Indian grouping differs from Western; float rounding is unreliable | Validate 3-digit groups |
| Required non-phone field present but invalid → reject `invalid_value:<field>` | Spec only defines `invalid_phone`; cannot happen with the given schemas (required non-phone fields are strings) | Treat as missing |
| `"N/A"` in phone → `invalid_phone` (present but invalid), not `missing_required` | It is a non-empty value | — |
| Event with missing/unparseable `occurred_at` → **malformed** | Cannot be placed on a date or ordered; never occurs in generated data | Store with NULL time |
| Whitespace-only lines are "empty" and not counted in `events_total` | "every non-empty line read" | Count them as malformed |
| Duplicate `event_id`: first occurrence wins even if content differs | Ledger is append-only; matches "count it and skip it" | Last wins |
| Truncated line such as `{"event_id": "evt_1"` is *malformed*, not duplicate | Malformed is checked first and it is not valid JSON | — |
| Effective-disposition ties on `occurred_at` broken by `event_id` | Result must not depend on file order | Arrival order |
| Override links via `(workflow_id, call_id)` to the lead of that call's phone events, preferring `call.initiated`'s lead | Spec; conflicts never occur in the data | — |
| Events attach to a call only if they link to the same lead as its `call.initiated`; multiple `call.initiated` for one call → earliest | Keeps orphans out of reports | — |
| Unknown disposition counted only for *linked* events (orphan check is step 3, before step 4) | Spec order | — |
| Disposition codes matched exactly (case-sensitive) | Catalogue codes are canonical; `RENEWD` etc. are unknown | Case-insensitive |
| `connectivity_rate` formatted with Python `f"{x:.4f}"` | Standard; differs from half-up only on exact .00005 ties | Decimal half-up |
| Batches whose rows were all rejected don't appear in `batch_summary` | It has no leads to summarise | Row of zeros |
| `rejections_by_reason` lists only reasons that occurred, sorted | Example shows a map, not a fixed key set | Fixed key list |
| One database per dataset; pointing a different seed at an existing DB exits with a clear error | Mixing two datasets in one ledger would produce meaningless reports | Silently merge them |
| Added `*.key` and `*.db-*` to the root `.gitignore` (the only change to a provided file besides `requirements.txt`) | The dev PII key file and SQLite WAL files must never be committed | Leave it unmodified and risk committing the key |

## Failure modes considered
- **Re-runs:** whole file skipped (SHA/batch check). Tested.
- **Crash mid-file:** one transaction per file, so nothing partial survives and the next run re-applies.
- **Same events across drops / within a file:** `event_id` PK + `INSERT OR IGNORE`, counted.
- **Event before its lead:** stays in the ledger as an orphan and links automatically once the lead exists. Tested.
- **Wrong PII key:** refused via key fingerprint. Tested.
- **Changed batch re-uploaded under the same name, or a different seed into the same DB:** refused
  with a clear error and exit code 2; nothing from that file is applied. Tested.
- **Mutation of the ledger:** blocked by triggers. Tested.
- **Not handled:** concurrent `ingest` processes on one DB (SQLite `BEGIN IMMEDIATE` serialises
  writers, but there's no job lock); key rotation (would need re-tokenising all leads/events);
  schema changes between drops (schema files are read on each run, not versioned); events for an
  unknown `workflow_id` are simply orphans.

## Testing
- `test_normalize.py`: all 6 generator phone formats plus invalid ones (landline, +1, 9 digits,
  `0091…`), all 4 date formats, impossible dates, leap years, currency variants, negatives, header
  aliases, the 3 timestamp forms, and IST date across midnight.
- `test_rules.py`: rejection order (first failure in schema order), warnings vs. defaults,
  alias mapping, effective disposition (override beats a later set, latest override by time
  not arrival), override linking via `call_id`, unknown codes, calls without `call.initiated`,
  every bucket rule.
- `test_pipeline.py` (end-to-end on a generated seed): re-run is a no-op; drop-by-drop ==
  all-at-once; drop_1 orphans link after drop_2; no plaintext name/phone anywhere in a full DB
  dump; append-only triggers; wrong key refused; a changed batch is refused cleanly.
- **Independent cross-check:** a separate, naive in-memory implementation written straight from
  the spec (not in the repo) produced byte-identical reports on seeds 42, 7, 1, 2024, 99999, 3, 555,
  31337 and on `--scale large`.

## Results
- Small (seed 42): ingest and report each under 1 s.
- **Large** (seed 42, ~144k lead rows, ~633k event lines) on my MacBook Air, Python 3.11.9: ingest 23.8 s, report 7.0 s, about 31 s end to end.
- Data observations (seed 42): 108 rejected rows / 2,057; 57 merged duplicates; 237 duplicate
  events (some across drops); 462 orphans after drop_2; 18 unknown dispositions (`RENEWD`,
  `call_back`, `unknown`); `batch_003` (both workflows) uses renamed and re-ordered columns plus an
  extra `agent_notes`; 10 of 70 `disposition.override` events arrive in drop_2 for calls initiated in drop_1.

## Stretch goals done
- **Performance:** large scale end-to-end in under 1 minute.
- **Incremental:** drop 2 never re-reads drop 1's files. Ingest is incremental; *reporting* is
  a full recompute over the DB. That's deliberate: an orphan's status depends on all leads, so
  recomputing (~7 s at ~630k event lines on my laptop) is simpler and provably correct. At 100× the volume the next step
  would be to materialise a `call_facts` table and recompute only calls touched by new events or new leads.

## If I had another day
1. Move report aggregation into SQL views (`call_facts`, `lead_facts`) so they can also be queried ad hoc.
2. Batch inserts (`executemany`) and a phone-token cache to make ingest roughly 3× faster.
3. Production note for 100k leads/day: Postgres, object-store drops with a scheduler
   (Airflow/Temporal) running `ingest` per drop then `report`; per-file idempotency already makes retries
   and backfills safe; key in KMS; alert when orphan rate or rejection rate jumps.
4. Key rotation tooling and a `ledger verify` command (recount from the ledger vs. stored counters).