"""Derived facts: linking, calls, effective dispositions, buckets.

Pure functions over plain data (no DB), so the trickiest rules are unit-testable.
Run on *everything ingested so far* each time a report is produced; that is what
makes orphans resolve once their lead arrives and makes drop-by-drop ingestion give
the same answer as all-at-once.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .catalog import Disposition

OVERRIDE = "disposition.override"
SET = "disposition.set"


@dataclass
class Event:
    event_id: str
    event_type: str | None
    workflow_id: str | None
    call_id: str | None
    occurred_at: datetime
    payload: dict
    direct_lead: int | None = None  # lead matched via (workflow_id, phone_token)


@dataclass
class Call:
    workflow_id: str
    call_id: str
    lead_id: int
    initiated_at: datetime
    connected: bool = False
    telephony_failed: bool = False
    # (occurred_at, event_id, code) of known-code disposition events
    sets: list = field(default_factory=list)
    overrides: list = field(default_factory=list)

    @property
    def effective_disposition(self) -> str | None:
        return effective_disposition(self.sets, self.overrides)


def effective_disposition(sets: list, overrides: list) -> str | None:
    """Latest override by occurred_at wins; else latest set; else None.

    Items are (occurred_at, event_id, code). Ties on occurred_at are broken by
    event_id (decision), so the result never depends on file order.
    """
    if overrides:
        return max(overrides)[2]
    if sets:
        return max(sets)[2]
    return None


def bucket(calls: list[Call], catalog: dict[tuple[str, str], Disposition]) -> str:
    if not calls:
        return "yet_to_reach_out"
    for c in calls:
        d = c.effective_disposition
        if d is not None and catalog[(c.workflow_id, d)].is_terminal:
            return "completed"
    if all(c.telephony_failed for c in calls):
        return "in_progress_telephony_failed"
    return "in_progress_dialed"


@dataclass
class Derived:
    calls: list[Call]
    orphan_events: int
    unknown_disposition_events: int


def link_events(events: list[Event]) -> dict[str, int | None]:
    """event_id -> lead_id (None = orphan).

    Phone-bearing events link directly. A disposition.override links through its
    call_id to the lead of that call's other (linked) events; we prefer the lead of
    the call.initiated event if the call's events disagree (decision; never seen).
    """
    call_lead: dict[tuple, int] = {}
    for e in sorted(events, key=lambda e: e.event_type == "call.initiated"):  # initiated last => wins
        if e.event_type != OVERRIDE and e.direct_lead is not None and e.call_id:
            call_lead[(e.workflow_id, e.call_id)] = e.direct_lead
    out = {}
    for e in events:
        if e.event_type == OVERRIDE:
            out[e.event_id] = call_lead.get((e.workflow_id, e.call_id))
        else:
            out[e.event_id] = e.direct_lead
    return out


def derive(events: list[Event], catalog: dict[tuple[str, str], Disposition]) -> Derived:
    links = link_events(events)
    orphans = sum(1 for e in events if links[e.event_id] is None)
    linked = [e for e in events if links[e.event_id] is not None]

    # 1. calls exist only where a linked call.initiated exists (earliest one wins)
    calls: dict[tuple, Call] = {}
    for e in linked:
        if e.event_type == "call.initiated" and e.call_id:
            key = (e.workflow_id, e.call_id)
            if key not in calls or e.occurred_at < calls[key].initiated_at:
                calls[key] = Call(e.workflow_id, e.call_id, links[e.event_id], e.occurred_at)

    # 2. attach the other events to their call (only if they belong to the same lead)
    unknown = 0
    for e in linked:
        if e.event_type in (SET, OVERRIDE):
            code = e.payload.get("disposition_code")
            if (e.workflow_id, code) not in catalog:
                unknown += 1
                continue
        call = calls.get((e.workflow_id, e.call_id))
        if call is None or call.lead_id != links[e.event_id]:
            continue
        if e.event_type == "call.connected":
            call.connected = True
        elif e.event_type == "call.telephony_failed":
            call.telephony_failed = True
        elif e.event_type == SET:
            call.sets.append((e.occurred_at, e.event_id, e.payload["disposition_code"]))
        elif e.event_type == OVERRIDE:
            call.overrides.append((e.occurred_at, e.event_id, e.payload["disposition_code"]))

    return Derived(list(calls.values()), orphans, unknown)
