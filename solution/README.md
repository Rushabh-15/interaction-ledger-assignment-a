# solution/ — Interaction Ledger & Reporting Pipeline (Assignment A)

See the repo-root `SUBMISSION.md` for design and decisions.

```bash
pip install -r requirements.txt     # from repo root
export PYTHONPATH=solution
python -m ledger ingest data/drop_1
python -m ledger ingest data/drop_2
python -m ledger report --out reports/
pytest solution
```

| Module | What it does |
|---|---|
| `ledger/normalize.py` | Pure value rules: headers, phone, date, decimal, enum, timestamps → IST |
| `ledger/catalog.py` | Loads workflow schemas and `dispositions.csv` |
| `ledger/pii.py` | HMAC phone tokens + Fernet encryption of PII |
| `ledger/db.py` | SQLite schema (append-only `events` ledger, idempotency log) |
| `ledger/leads.py` | Lead CSV ingestion: map headers → validate → reject or merge |
| `ledger/events.py` | JSONL ingestion: malformed / duplicate filtering into the ledger |
| `ledger/derive.py` | Pure: link events, build calls, effective disposition, buckets |
| `ledger/reports.py` | Writes the five report files |
| `ledger/cli.py` | `ingest` / `report` commands |
