"""Command line interface.

    ledger ingest data/drop_1 [data/drop_2 ...]
    ledger report --out reports/

Global options (put them after the sub-command): --db PATH (default ledger.db),
--schemas DIR (default: the repo's assignment-a-data/schemas).
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from .catalog import DEFAULT_SCHEMA_DIR, load_catalog
from .db import check_key, connect
from .events import ingest_event_file
from .leads import BatchConflict, ingest_lead_file
from .pii import PiiVault, load_master_key
from .reports import build_reports


def _open(args):
    conn = connect(args.db)
    vault = PiiVault(load_master_key(args.db))
    check_key(conn, vault.phone_token("__key_fingerprint__"))
    return conn, vault, load_catalog(args.schemas)


def cmd_ingest(args) -> int:
    conn, vault, catalog = _open(args)
    try:
        return _ingest_drops(args, conn, vault, catalog)
    except BatchConflict as exc:
        # Most often: a different dataset (seed) pointed at an existing DB.
        print(f"error: {exc}\nhint: each dataset needs its own database; use --db <new file> "
              f"or remove {args.db}* to start fresh.", file=sys.stderr)
        return 2


def _ingest_drops(args, conn, vault, catalog) -> int:
    for drop in args.drops:
        drop = Path(drop)
        if not drop.is_dir():
            print(f"error: {drop} is not a directory", file=sys.stderr)
            return 2
        # Leads: files in filename order (per workflow); rows in file order.
        for wf_dir in sorted(p for p in (drop / "leads").glob("*") if p.is_dir()):
            schema = catalog.schemas.get(wf_dir.name)
            if schema is None:
                print(f"warning: no schema for workflow {wf_dir.name!r}; skipping {wf_dir}", file=sys.stderr)
                continue
            for f in sorted(wf_dir.glob("*.csv")):
                _report(f, ingest_lead_file(conn, vault, schema, f))
        for f in sorted((drop / "events").glob("*.jsonl")):
            _report(f, ingest_event_file(conn, vault, f))
    return 0


def _report(path: Path, stats: dict | None) -> None:
    if stats is None:
        print(f"skip    {path} (already ingested)")
    else:
        print(f"ingest  {path} " + " ".join(f"{k}={v}" for k, v in stats.items()))


def cmd_report(args) -> int:
    conn, _, catalog = _open(args)
    t = time.perf_counter()
    build_reports(conn, catalog, args.out)
    print(f"wrote 5 reports to {args.out} in {time.perf_counter() - t:.2f}s")
    return 0


def main(argv=None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default="ledger.db", help="SQLite database file (default: ledger.db)")
    common.add_argument("--schemas", default=str(DEFAULT_SCHEMA_DIR), help="schema directory")

    p = argparse.ArgumentParser(prog="ledger", description="Interaction ledger & reporting pipeline")
    sub = p.add_subparsers(dest="cmd", required=True)
    pi = sub.add_parser("ingest", parents=[common], help="ingest one or more drop directories")
    pi.add_argument("drops", nargs="+")
    pi.set_defaults(func=cmd_ingest)
    pr = sub.add_parser("report", parents=[common], help="write the five report files")
    pr.add_argument("--out", required=True)
    pr.set_defaults(func=cmd_report)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
