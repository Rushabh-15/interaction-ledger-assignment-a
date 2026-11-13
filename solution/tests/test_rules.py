"""Row validation, effective disposition, linking and bucket logic."""
from datetime import datetime, timedelta, timezone

import pytest
from conftest import REPO

from ledger.catalog import load_catalog
from ledger.derive import Event, bucket, derive, effective_disposition
from ledger.leads import apply_defaults, map_headers, validate_row

CAT = load_catalog(REPO / "assignment-a-data" / "schemas")
MOTOR = CAT.schemas["motor_renewal"]
T0 = datetime(2026, 10, 5, 10, 0, tzinfo=timezone.utc)


def t(minutes):
    return T0 + timedelta(minutes=minutes)


# ----------------------------------------------------------------- lead rows

def row(**kw):
    base = {"customer_name": "A B", "phone": "9876543210", "policy_number": "P1",
            "policy_expiry": "01/12/2026", "premium_amount": "₹1,000", "preferred_language": "HI"}
    base.update(kw)
    return base


def test_valid_row_normalised():
    r = validate_row(MOTOR, row())
    assert r.reason is None and r.warnings == 0
    assert r.values["phone"] == "+919876543210"
    assert r.values["policy_expiry"] == "2026-12-01"
    assert r.values["premium_amount"] == "1000.00"
    assert r.values["preferred_language"] == "hi"


def test_first_failure_in_schema_order_wins():
    # name (field 1) is missing AND phone (field 2) is invalid -> name reported
    assert validate_row(MOTOR, row(customer_name="  ", phone="N/A")).reason == "missing_required:customer_name"
    assert validate_row(MOTOR, row(phone="N/A", policy_number="")).reason == "invalid_phone"
    assert validate_row(MOTOR, row(phone="")).reason == "missing_required:phone"


def test_optional_invalid_is_null_plus_warning():
    r = validate_row(MOTOR, row(policy_expiry="31/02/2026", premium_amount="N/A", preferred_language="Hindi"))
    assert r.reason is None and r.warnings == 3
    assert r.values["policy_expiry"] is None and r.values["preferred_language"] is None


def test_optional_missing_is_not_a_warning_and_gets_default():
    r = validate_row(MOTOR, row(preferred_language="  "))
    assert r.warnings == 0
    assert apply_defaults(MOTOR, r.values)["preferred_language"] == "en"


def test_header_aliases_and_unknown_columns():
    m = map_headers(MOTOR, ["Language", "Insured Name", "Policy No", "Mobile No", "Expiry Date",
                            "Premium", "agent_notes"])
    assert m == {"preferred_language": 0, "customer_name": 1, "policy_number": 2, "phone": 3,
                 "policy_expiry": 4, "premium_amount": 5}


# ----------------------------------------------------------------- effective disposition

def test_override_beats_later_set():
    sets = [(t(0), "e1", "callback_requested"), (t(50), "e3", "not_interested")]
    overrides = [(t(10), "e2", "renewed")]
    assert effective_disposition(sets, overrides) == "renewed"


def test_latest_override_by_time_not_arrival():
    overrides = [(t(30), "e9", "renewed"), (t(60), "e1", "dnd")]
    assert effective_disposition([], overrides) == "dnd"


def test_latest_set_without_override_and_none():
    assert effective_disposition([(t(5), "a", "x"), (t(1), "b", "y")], []) == "x"
    assert effective_disposition([], []) is None


# ----------------------------------------------------------------- derive + buckets

def ev(eid, etype, call, minutes, lead=1, code=None, wf="motor_renewal"):
    payload = {"disposition_code": code} if code else {}
    direct = None if etype == "disposition.override" else lead
    return Event(eid, etype, wf, call, t(minutes), payload, direct)


def test_override_links_via_call_and_unknown_codes_ignored():
    events = [
        ev("1", "call.initiated", "c1", 0), ev("2", "call.connected", "c1", 1),
        ev("3", "disposition.set", "c1", 5, code="callback_requested"),
        ev("4", "disposition.override", "c1", 90, code="renewed"),
        ev("5", "disposition.override", "c1", 99, code="RENEWD"),   # unknown: ignored
        ev("6", "disposition.override", "c_unknown", 99, code="renewed"),  # orphan: call never linked
        ev("7", "call.initiated", "c2", 0, lead=None),               # orphan: phone not a lead
    ]
    d = derive(events, CAT.dispositions)
    assert d.orphan_events == 2 and d.unknown_disposition_events == 1
    (call,) = d.calls
    assert call.connected and call.effective_disposition == "renewed"


def test_call_without_initiated_is_not_counted():
    d = derive([ev("1", "call.connected", "c1", 1)], CAT.dispositions)
    assert d.calls == [] and d.orphan_events == 0


def calls_for(*specs):
    events, n = [], 0
    for i, (failed, code) in enumerate(specs):
        cid = f"c{i}"
        events.append(ev(f"{cid}i", "call.initiated", cid, i * 100))
        if failed:
            events.append(ev(f"{cid}f", "call.telephony_failed", cid, i * 100 + 1))
        if code:
            events.append(ev(f"{cid}d", "disposition.set", cid, i * 100 + 5, code=code))
    return derive(events, CAT.dispositions).calls


@pytest.mark.parametrize("specs,expected", [
    ((), "yet_to_reach_out"),
    (((True, None), (True, None)), "in_progress_telephony_failed"),
    (((True, None), (False, None)), "in_progress_dialed"),
    (((False, "callback_requested"),), "in_progress_dialed"),
    (((True, None), (False, "not_interested")), "completed"),   # terminal beats everything
    (((False, "renewed"), (False, "callback_requested")), "completed"),  # ANY call terminal
])
def test_bucket(specs, expected):
    assert bucket(calls_for(*specs), CAT.dispositions) == expected
