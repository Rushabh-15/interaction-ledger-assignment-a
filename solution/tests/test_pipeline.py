"""End-to-end: idempotency, incremental == all-at-once, orphan resolution, PII, append-only."""
import sqlite3
import subprocess
import sys

import pytest
from conftest import REPO

from ledger.cli import main

GENERATOR = REPO / "assignment-a-data" / "tools" / "generate_data.py"
REPORTS = ["batch_summary.csv", "daily_overview.csv", "lead_report.csv", "rejections.csv",
           "data_quality.json"]


@pytest.fixture(scope="module")
def data(tmp_path_factory):
    out = tmp_path_factory.mktemp("gen") / "data"
    subprocess.run([sys.executable, str(GENERATOR), "--seed", "1234", "--out", str(out)],
                   check=True, capture_output=True)
    return out


def run_report(db, out):
    main(["report", "--db", str(db), "--out", str(out)])
    return {name: (out / name).read_text() for name in REPORTS}


def test_rerun_and_incremental_match_all_at_once(data, tmp_path):
    inc, once = tmp_path / "inc.db", tmp_path / "once.db"
    main(["ingest", str(data / "drop_1"), "--db", str(inc)])
    main(["ingest", str(data / "drop_2"), "--db", str(inc)])
    first = run_report(inc, tmp_path / "r1")
    main(["ingest", str(data / "drop_1"), str(data / "drop_2"), "--db", str(inc)])  # re-run: no-op
    assert run_report(inc, tmp_path / "r2") == first

    main(["ingest", str(data / "drop_1"), str(data / "drop_2"), "--db", str(once)])
    assert run_report(once, tmp_path / "r3") == first


def test_orphans_resolve_when_lead_arrives(data, tmp_path):
    """drop_1 has calls for leads that only arrive in drop_2 (batch_003)."""
    db = tmp_path / "x.db"
    linked_drop1_events = """
        SELECT COUNT(*) FROM events e
        JOIN leads l ON l.workflow_id = e.workflow_id AND l.phone_token = e.phone_token
        JOIN ingested_files f ON f.file_id = e.file_id
        WHERE f.source_path LIKE '%drop_1%'"""
    main(["ingest", str(data / "drop_1"), "--db", str(db)])
    before = sqlite3.connect(db).execute(linked_drop1_events).fetchone()[0]
    main(["ingest", str(data / "drop_2"), "--db", str(db)])
    after = sqlite3.connect(db).execute(linked_drop1_events).fetchone()[0]
    assert after > before  # the same, unchanged drop_1 events now link


def test_no_plaintext_pii_in_database(data, tmp_path):
    db = tmp_path / "pii.db"
    main(["ingest", str(data / "drop_1"), "--db", str(db)])
    import csv
    with open(next((data / "drop_1" / "leads" / "motor_renewal").glob("*.csv")), newline="") as fh:
        rows = list(csv.DictReader(fh))
    first = next(r for r in rows if r["customer_name"].strip() and r["phone"].strip())
    digits = "".join(c for c in first["phone"] if c.isdigit())[-10:]
    dump = "\n".join(sqlite3.connect(db).iterdump())
    assert first["customer_name"] not in dump
    assert digits not in dump


def test_ledger_is_append_only(data, tmp_path):
    db = tmp_path / "ao.db"
    main(["ingest", str(data / "drop_1"), "--db", str(db)])
    conn = sqlite3.connect(db)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("UPDATE events SET event_type = 'x'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("DELETE FROM events")


def test_changed_batch_is_refused_cleanly(data, tmp_path, capsys):
    """A different dataset (or an edited batch) under the same batch name: clear error, no crash."""
    import shutil
    db = tmp_path / "c.db"
    main(["ingest", str(data / "drop_1"), "--db", str(db)])
    other = tmp_path / "other_drop"
    shutil.copytree(data / "drop_1", other)
    batch = next((other / "leads" / "motor_renewal").glob("batch_001.csv"))
    batch.write_text(batch.read_text() + "Extra Person,9123456789,MOT/X/1,,,\n")
    assert main(["ingest", str(other), "--db", str(db)]) == 2
    assert "already ingested with different content" in capsys.readouterr().err
    assert sqlite3.connect(db).execute(
        "SELECT COUNT(*) FROM ingested_files WHERE source_path LIKE '%other_drop%'").fetchone()[0] == 0


def test_wrong_pii_key_is_refused(data, tmp_path, monkeypatch):
    db = tmp_path / "k.db"
    main(["ingest", str(data / "drop_1"), "--db", str(db)])
    monkeypatch.setenv("LEDGER_PII_KEY", "a-different-key")
    with pytest.raises(SystemExit):
        main(["ingest", str(data / "drop_2"), "--db", str(db)])
