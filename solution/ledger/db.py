"""SQLite schema and connection helpers.

Design in one paragraph:
  * ``ingested_files`` is the idempotency log: one row per file we have applied,
    with its SHA-256 and the counters it produced. A file is applied in ONE
    transaction together with its log row, so a crash leaves it either fully
    applied or not applied at all, and a re-run skips it.
  * ``leads`` is *current state* (one row per (workflow_id, phone_token)); merges
    update it. PII lives only as an HMAC token + Fernet ciphertext + last 4 digits.
  * ``lead_rejections`` keeps rejected rows (no PII, just position and reason).
  * ``events`` is the **append-only interaction ledger**: every well-formed,
    first-seen event, *including* ones that cannot be linked yet. Triggers make
    UPDATE/DELETE impossible. Linking to leads is computed at report time, which
    is what lets an orphan from drop_1 count once its lead arrives in drop_2.
"""
from __future__ import annotations

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingested_files (
    file_id         INTEGER PRIMARY KEY,
    kind            TEXT NOT NULL CHECK (kind IN ('leads', 'events')),
    workflow_id     TEXT,                 -- leads only
    batch_id        TEXT,                 -- leads only (file stem)
    source_path     TEXT NOT NULL,
    content_sha256  TEXT NOT NULL,
    ingested_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    -- lead counters
    rows_total      INTEGER NOT NULL DEFAULT 0,
    rows_rejected   INTEGER NOT NULL DEFAULT 0,
    rows_merged     INTEGER NOT NULL DEFAULT 0,
    field_warnings  INTEGER NOT NULL DEFAULT 0,
    -- event counters
    lines_total     INTEGER NOT NULL DEFAULT 0,
    lines_malformed INTEGER NOT NULL DEFAULT 0,
    lines_duplicate INTEGER NOT NULL DEFAULT 0
);
-- A batch is identified by (workflow, file stem); an events file by its content.
CREATE UNIQUE INDEX IF NOT EXISTS ux_files_batch
    ON ingested_files (workflow_id, batch_id) WHERE kind = 'leads';
CREATE UNIQUE INDEX IF NOT EXISTS ux_files_events
    ON ingested_files (content_sha256) WHERE kind = 'events';

CREATE TABLE IF NOT EXISTS leads (
    lead_id         INTEGER PRIMARY KEY,
    workflow_id     TEXT NOT NULL,
    phone_token     TEXT NOT NULL,        -- HMAC-SHA256(canonical phone)
    phone_last4     TEXT NOT NULL CHECK (length(phone_last4) = 4),
    pii_enc         BLOB NOT NULL,        -- Fernet(JSON of all pii fields)
    external_ref    TEXT NOT NULL,
    batch_id        TEXT NOT NULL,        -- batch of the FIRST valid row; never changes
    attrs_json      TEXT NOT NULL,        -- non-PII fields, nulls kept (defaults applied on read)
    created_file_id INTEGER NOT NULL REFERENCES ingested_files(file_id),
    updated_file_id INTEGER NOT NULL REFERENCES ingested_files(file_id),
    UNIQUE (workflow_id, phone_token)
);

CREATE TABLE IF NOT EXISTS lead_rejections (
    workflow_id TEXT NOT NULL,
    batch_id    TEXT NOT NULL,
    row_number  INTEGER NOT NULL,
    reason      TEXT NOT NULL,
    file_id     INTEGER NOT NULL REFERENCES ingested_files(file_id),
    PRIMARY KEY (workflow_id, batch_id, row_number)
);

CREATE TABLE IF NOT EXISTS events (
    event_id     TEXT PRIMARY KEY,         -- dedupe key across all drops
    event_type   TEXT,
    workflow_id  TEXT,
    call_id      TEXT,
    occurred_at  TEXT,                     -- UTC, 'YYYY-MM-DDTHH:MM:SSZ' (sortable)
    phone_token  TEXT,                     -- NULL if no phone or phone invalid
    payload_json TEXT NOT NULL,
    file_id      INTEGER NOT NULL REFERENCES ingested_files(file_id),
    line_no      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_link ON events (workflow_id, phone_token);
CREATE INDEX IF NOT EXISTS ix_events_call ON events (workflow_id, call_id);

-- The ledger is append-only: corrections arrive as new events (disposition.override).
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events ledger is append-only'); END;
"""


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None)  # we manage transactions explicitly
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.executescript(SCHEMA)
    return conn


def check_key(conn: sqlite3.Connection, fingerprint: str) -> None:
    """Refuse to run with a different PII key than the DB was built with.

    Otherwise phone tokens silently stop matching and every new event is an orphan.
    """
    row = conn.execute("SELECT value FROM meta WHERE key = 'pii_key_fingerprint'").fetchone()
    if row is None:
        conn.execute("INSERT INTO meta (key, value) VALUES ('pii_key_fingerprint', ?)", (fingerprint,))
    elif row["value"] != fingerprint:
        raise SystemExit("PII key does not match the one this database was created with. "
                         "Set LEDGER_PII_KEY to the original key.")
