"""Lead file ingestion: header mapping, validation, rejection, merging."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .catalog import Field, WorkflowSchema
from .normalize import (normalise_date, normalise_decimal, normalise_enum, normalise_header,
                        normalise_phone)
from .pii import PiiVault


# --------------------------------------------------------------------------- pure logic

def map_headers(schema: WorkflowSchema, headers: list[str]) -> dict[str, int]:
    """field name -> column index. Unknown columns are ignored.

    If two columns match the same field, the left-most one wins (decision).
    """
    mapping: dict[str, int] = {}
    for idx, h in enumerate(headers):
        key = normalise_header(h)
        for f in schema.fields:
            if f.name not in mapping and key in f.header_keys():
                mapping[f.name] = idx
                break
    return mapping


def _convert(f: Field, raw: str):
    if f.type == "string":
        return raw
    if f.type == "phone":
        return normalise_phone(raw)
    if f.type == "date":
        return normalise_date(raw)
    if f.type == "decimal":
        return normalise_decimal(raw)
    if f.type == "enum":
        return normalise_enum(raw, list(f.values))
    raise ValueError(f"unknown field type {f.type!r}")


@dataclass
class RowResult:
    values: dict | None        # field -> normalised value (None = null); None if rejected
    reason: str | None         # rejection reason
    warnings: int = 0


def validate_row(schema: WorkflowSchema, raw: dict[str, str | None]) -> RowResult:
    """Walk fields in schema order. The first failure is the row's single reason.

    raw: field name -> raw cell (already trimmed; '' or None = missing).
    """
    values: dict = {}
    warnings = 0
    for f in schema.fields:
        cell = raw.get(f.name)
        cell = cell.strip() if isinstance(cell, str) else None
        if not cell:
            if f.required:
                return RowResult(None, f"missing_required:{f.name}")
            values[f.name] = None
            continue
        value = _convert(f, cell)
        if value is None:
            if f.required:
                # The spec only defines invalid_phone. A required non-phone field that is
                # present but invalid does not occur in the given schemas (decision).
                return RowResult(None, "invalid_phone" if f.type == "phone" else f"invalid_value:{f.name}")
            warnings += 1
        values[f.name] = value
    return RowResult(values, None, warnings)


def apply_defaults(schema: WorkflowSchema, attrs: dict) -> dict:
    """Defaults apply after merging, to fields that are still null."""
    out = dict(attrs)
    for f in schema.fields:
        if out.get(f.name) is None and f.default is not None:
            out[f.name] = f.default
    return out


def is_blank(cells: list[str]) -> bool:
    return all(not (c or "").strip() for c in cells)


# --------------------------------------------------------------------------- ingestion

class BatchConflict(Exception):
    pass


def ingest_lead_file(conn: sqlite3.Connection, vault: PiiVault, schema: WorkflowSchema,
                     path: Path) -> dict | None:
    """Apply one batch file. Returns its counters, or None if already ingested."""
    data = path.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    batch_id = path.stem
    wf = schema.workflow_id

    prev = conn.execute(
        "SELECT content_sha256 FROM ingested_files WHERE kind='leads' AND workflow_id=? AND batch_id=?",
        (wf, batch_id)).fetchone()
    if prev is not None:
        if prev["content_sha256"] == sha:
            return None  # idempotent re-run
        raise BatchConflict(f"{wf}/{batch_id} was already ingested with different content; "
                            f"batch ids are immutable. Rename the file to ingest it as a new batch.")

    pii_fields = [f.name for f in schema.fields if f.pii]
    phone_field = schema.phone_field.name
    ref_field = schema.reference_field.name
    stats = {"rows_total": 0, "rows_rejected": 0, "rows_merged": 0, "field_warnings": 0}

    conn.execute("BEGIN IMMEDIATE")
    try:
        file_id = conn.execute(
            "INSERT INTO ingested_files (kind, workflow_id, batch_id, source_path, content_sha256) "
            "VALUES ('leads', ?, ?, ?, ?)", (wf, batch_id, str(path), sha)).lastrowid

        reader = csv.reader(io.StringIO(data.decode("utf-8-sig"), newline=""))
        headers = next(reader, [])
        mapping = map_headers(schema, headers)
        row_number = 0
        for cells in reader:
            if is_blank(cells):
                continue  # blank rows are skipped and not counted
            row_number += 1
            stats["rows_total"] += 1
            raw = {name: (cells[i] if i < len(cells) else None) for name, i in mapping.items()}
            res = validate_row(schema, raw)
            if res.values is None:
                stats["rows_rejected"] += 1
                conn.execute("INSERT INTO lead_rejections (workflow_id, batch_id, row_number, reason, file_id) "
                             "VALUES (?, ?, ?, ?, ?)", (wf, batch_id, row_number, res.reason, file_id))
                continue
            stats["field_warnings"] += res.warnings
            merged = _upsert_lead(conn, vault, wf, batch_id, file_id, res.values,
                                  pii_fields, phone_field, ref_field)
            stats["rows_merged"] += int(merged)

        conn.execute("UPDATE ingested_files SET rows_total=?, rows_rejected=?, rows_merged=?, "
                     "field_warnings=? WHERE file_id=?",
                     (stats["rows_total"], stats["rows_rejected"], stats["rows_merged"],
                      stats["field_warnings"], file_id))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return stats


def _upsert_lead(conn, vault: PiiVault, wf, batch_id, file_id, values: dict,
                 pii_fields, phone_field, ref_field) -> bool:
    """Create the lead, or merge into it (last non-null write wins). Returns True if merged."""
    phone = values[phone_field]
    token = vault.phone_token(phone)
    pii = {k: values[k] for k in pii_fields}
    attrs = {k: v for k, v in values.items() if k not in pii_fields}

    existing = conn.execute("SELECT lead_id, pii_enc, attrs_json FROM leads "
                            "WHERE workflow_id=? AND phone_token=?", (wf, token)).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO leads (workflow_id, phone_token, phone_last4, pii_enc, external_ref, batch_id, "
            "attrs_json, created_file_id, updated_file_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (wf, token, phone[-4:], vault.encrypt(pii), attrs[ref_field], batch_id,
             json.dumps(attrs, sort_keys=True), file_id, file_id))
        return False

    old_pii = vault.decrypt(existing["pii_enc"])
    old_attrs = json.loads(existing["attrs_json"])
    old_pii.update({k: v for k, v in pii.items() if v is not None})
    old_attrs.update({k: v for k, v in attrs.items() if v is not None})
    conn.execute("UPDATE leads SET pii_enc=?, attrs_json=?, external_ref=?, updated_file_id=? "
                 "WHERE lead_id=?",  # batch_id is deliberately NOT updated
                 (vault.encrypt(old_pii), json.dumps(old_attrs, sort_keys=True),
                  old_attrs[ref_field], file_id, existing["lead_id"]))
    return True
