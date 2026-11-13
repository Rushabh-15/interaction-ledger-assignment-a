"""Loads workflow lead schemas and the disposition catalogue."""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path

from .normalize import normalise_header

DEFAULT_SCHEMA_DIR = Path(__file__).resolve().parents[2] / "assignment-a-data" / "schemas"


@dataclass(frozen=True)
class Field:
    name: str
    type: str
    required: bool = False
    pii: bool = False
    reference: bool = False
    values: tuple = ()
    default: str | None = None
    aliases: tuple = ()

    def header_keys(self) -> set[str]:
        return {normalise_header(self.name), *(normalise_header(a) for a in self.aliases)}


@dataclass
class WorkflowSchema:
    workflow_id: str
    fields: list[Field]

    @property
    def reference_field(self) -> Field:
        return next(f for f in self.fields if f.reference)

    @property
    def phone_field(self) -> Field:
        return next(f for f in self.fields if f.type == "phone")


@dataclass(frozen=True)
class Disposition:
    is_terminal: bool
    is_success: bool


@dataclass
class Catalog:
    schemas: dict[str, WorkflowSchema]
    # (workflow_id, code) -> Disposition
    dispositions: dict[tuple[str, str], Disposition] = field(default_factory=dict)


def _bool(s: str) -> bool:
    return s.strip().lower() == "true"


def load_catalog(schema_dir: str | Path = DEFAULT_SCHEMA_DIR) -> Catalog:
    schema_dir = Path(schema_dir)
    schemas: dict[str, WorkflowSchema] = {}
    for p in sorted(schema_dir.glob("*.json")):
        raw = json.loads(p.read_text(encoding="utf-8"))
        fields = [
            Field(
                name=f["name"], type=f["type"], required=bool(f.get("required", False)),
                pii=bool(f.get("pii", False)), reference=bool(f.get("reference", False)),
                values=tuple(f.get("values", ())), default=f.get("default"),
                aliases=tuple(f.get("aliases", ())),
            )
            for f in raw["fields"]
        ]
        schemas[raw["workflow_id"]] = WorkflowSchema(raw["workflow_id"], fields)

    dispositions = {}
    with open(schema_dir / "dispositions.csv", newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            dispositions[(row["workflow_id"].strip(), row["code"].strip())] = Disposition(
                _bool(row["is_terminal"]), _bool(row["is_success"]))
    return Catalog(schemas, dispositions)
