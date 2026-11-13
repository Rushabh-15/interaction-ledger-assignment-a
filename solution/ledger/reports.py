"""Builds the five report files from the database."""
from __future__ import annotations

import csv
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .catalog import Catalog
from .derive import Event, bucket, derive
from .normalize import ist_date, mask_phone, to_ist_iso

BUCKETS = ["yet_to_reach_out", "in_progress_telephony_failed", "in_progress_dialed", "completed"]


def _utc(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def load_events(conn: sqlite3.Connection) -> list[Event]:
    # The join on (workflow_id, phone_token) is the "link" step, done in SQL.
    rows = conn.execute("""
        SELECT e.event_id, e.event_type, e.workflow_id, e.call_id, e.occurred_at,
               e.payload_json, l.lead_id
        FROM events e
        LEFT JOIN leads l ON l.workflow_id = e.workflow_id AND l.phone_token = e.phone_token
    """)
    return [Event(r["event_id"], r["event_type"], r["workflow_id"], r["call_id"], _utc(r["occurred_at"]),
                  json.loads(r["payload_json"]), r["lead_id"]) for r in rows]


def _write_csv(path: Path, header: list[str], rows: list[list]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(header)
        w.writerows(rows)


def _b(v: bool) -> str:
    return "true" if v else "false"


def build_reports(conn: sqlite3.Connection, catalog: Catalog, out_dir: str | Path) -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    disp = catalog.dispositions

    leads = conn.execute("SELECT lead_id, workflow_id, external_ref, batch_id, phone_last4 FROM leads").fetchall()
    derived = derive(load_events(conn), disp)

    calls_by_lead = defaultdict(list)
    for c in derived.calls:
        calls_by_lead[c.lead_id].append(c)

    def success(c) -> bool:
        d = c.effective_disposition
        return d is not None and disp[(c.workflow_id, d)].is_success

    # ---- 3. lead_report.csv (also feeds batch_summary)
    lead_rows, batch_counts = [], defaultdict(lambda: dict.fromkeys(BUCKETS, 0))
    for l in leads:
        calls = sorted(calls_by_lead[l["lead_id"]], key=lambda c: (c.initiated_at, c.call_id))
        b = bucket(calls, disp)
        batch_counts[(l["workflow_id"], l["batch_id"])][b] += 1
        with_disp = [c for c in calls if c.effective_disposition is not None]
        lead_rows.append([
            l["workflow_id"], l["external_ref"], l["batch_id"], mask_phone(l["phone_last4"]), b,
            len(calls), sum(c.connected for c in calls),
            to_ist_iso(calls[-1].initiated_at) if calls else "",
            with_disp[-1].effective_disposition if with_disp else "",
            _b(any(success(c) for c in calls)),
        ])
    # external_ref ties (should not happen) fall back to the masked phone for determinism
    lead_rows.sort(key=lambda r: (r[0], r[1], r[3]))
    _write_csv(out / "lead_report.csv",
               ["workflow_id", "external_ref", "batch_id", "phone_masked", "bucket", "total_calls",
                "connected_calls", "last_call_at", "current_disposition", "ever_success"], lead_rows)

    # ---- 1. batch_summary.csv
    _write_csv(out / "batch_summary.csv",
               ["workflow_id", "batch_id", "total_leads", *BUCKETS],
               [[wf, b, sum(c.values()), *(c[k] for k in BUCKETS)]
                for (wf, b), c in sorted(batch_counts.items())])

    # ---- 2. daily_overview.csv
    day = defaultdict(lambda: {"total": 0, "connected": 0, "success_leads": set()})
    for c in derived.calls:
        d = day[(c.workflow_id, ist_date(c.initiated_at))]
        d["total"] += 1
        d["connected"] += int(c.connected)
        if success(c):
            d["success_leads"].add(c.lead_id)
    _write_csv(out / "daily_overview.csv",
               ["workflow_id", "date", "total_calls", "connected_calls", "connectivity_rate", "success_leads"],
               [[wf, date, d["total"], d["connected"], f"{d['connected'] / d['total']:.4f}",
                 len(d["success_leads"])] for (wf, date), d in sorted(day.items())])

    # ---- 4. rejections.csv (no PII: position + reason only)
    rej = conn.execute("SELECT workflow_id, batch_id, row_number, reason FROM lead_rejections "
                       "ORDER BY workflow_id, batch_id, row_number").fetchall()
    _write_csv(out / "rejections.csv", ["workflow_id", "batch_id", "row_number", "reason"],
               [list(r) for r in rej])

    # ---- 5. data_quality.json
    f = conn.execute("""SELECT COALESCE(SUM(rows_total),0) rt, COALESCE(SUM(rows_rejected),0) rr,
                               COALESCE(SUM(rows_merged),0) rm, COALESCE(SUM(field_warnings),0) fw,
                               COALESCE(SUM(lines_total),0) lt, COALESCE(SUM(lines_malformed),0) lm,
                               COALESCE(SUM(lines_duplicate),0) ld FROM ingested_files""").fetchone()
    by_reason = {r["reason"]: r["n"] for r in conn.execute(
        "SELECT reason, COUNT(*) n FROM lead_rejections GROUP BY reason ORDER BY reason")}
    dq = {
        "lead_rows_total": f["rt"],
        "lead_rows_rejected": f["rr"],
        "rejections_by_reason": by_reason,
        "duplicate_lead_rows_merged": f["rm"],
        "field_warnings": f["fw"],
        "events_total": f["lt"],
        "malformed_events": f["lm"],
        "duplicate_events_dropped": f["ld"],
        "orphan_events": derived.orphan_events,
        "unknown_disposition_events": derived.unknown_disposition_events,
    }
    (out / "data_quality.json").write_text(json.dumps(dq, indent=2) + "\n", encoding="utf-8")
