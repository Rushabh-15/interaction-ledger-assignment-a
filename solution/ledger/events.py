"""Event file ingestion into the append-only ledger.

At ingest time we only apply the two filters whose answer can never change:
  1. malformed  (not valid JSON / no event_id / unparseable occurred_at)
  2. duplicate  (event_id already in the ledger, from any drop)
Linking, orphans and unknown dispositions depend on what has been ingested so far,
so they are decided at report time (see derive.py).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from .normalize import normalise_phone, parse_timestamp
from .pii import PiiVault


def parse_event_line(line: str):
    """Returns a dict ready for the ledger, or None if the line is malformed."""
    try:
        ev = json.loads(line)
    except ValueError:
        return None
    if not isinstance(ev, dict):
        return None
    event_id = ev.get("event_id")
    if event_id is None or str(event_id).strip() == "":
        return None
    occurred = parse_timestamp(ev.get("occurred_at"))
    if occurred is None:
        return None  # decision: an event we cannot place in time is malformed
    payload = ev.get("payload")
    return {
        "event_id": str(event_id),
        "event_type": ev.get("event_type"),
        "workflow_id": ev.get("workflow_id"),
        "call_id": ev.get("call_id"),
        "occurred_at": occurred.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "phone": ev.get("phone"),
        "payload": payload if isinstance(payload, dict) else {},
    }


def ingest_event_file(conn: sqlite3.Connection, vault: PiiVault, path: Path) -> dict | None:
    data = path.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    if conn.execute("SELECT 1 FROM ingested_files WHERE kind='events' AND content_sha256=?",
                    (sha,)).fetchone():
        return None  # idempotent re-run

    stats = {"lines_total": 0, "lines_malformed": 0, "lines_duplicate": 0}
    conn.execute("BEGIN IMMEDIATE")
    try:
        file_id = conn.execute("INSERT INTO ingested_files (kind, source_path, content_sha256) "
                               "VALUES ('events', ?, ?)", (str(path), sha)).lastrowid
        for line_no, line in enumerate(data.decode("utf-8-sig").splitlines(), start=1):
            if not line.strip():
                continue
            stats["lines_total"] += 1
            ev = parse_event_line(line)
            if ev is None:
                stats["lines_malformed"] += 1
                continue
            canonical = normalise_phone(ev["phone"]) if ev["phone"] is not None else None
            token = vault.phone_token(canonical) if canonical else None
            cur = conn.execute(
                "INSERT OR IGNORE INTO events (event_id, event_type, workflow_id, call_id, occurred_at, "
                "phone_token, payload_json, file_id, line_no) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ev["event_id"], ev["event_type"], ev["workflow_id"], ev["call_id"], ev["occurred_at"],
                 token, json.dumps(ev["payload"], sort_keys=True), file_id, line_no))
            if cur.rowcount == 0:
                stats["lines_duplicate"] += 1  # first occurrence wins
        conn.execute("UPDATE ingested_files SET lines_total=?, lines_malformed=?, lines_duplicate=? "
                     "WHERE file_id=?", (stats["lines_total"], stats["lines_malformed"],
                                         stats["lines_duplicate"], file_id))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return stats
