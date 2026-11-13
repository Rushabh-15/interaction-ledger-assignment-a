import pytest

from ledger.normalize import (ist_date, normalise_date, normalise_decimal, normalise_enum,
                              normalise_header, normalise_phone, parse_timestamp, to_ist_iso)


@pytest.mark.parametrize("raw", [
    "9876543210", "+91 98765 43210", "09876543210", "91-98765-43210", "(+91) 9876543210",
    "+919876543210", " 98765-43210 ",
])
def test_phone_all_generator_formats(raw):
    assert normalise_phone(raw) == "+919876543210"


@pytest.mark.parametrize("raw", [
    "987654321",          # 9 digits
    "5876543210",         # bad first digit
    "022-2345-6789",      # landline: 11 digits starting 0 -> 10 digits starting 2
    "N/A", "", "+1 415 555 0134",
    "0091 9876543210",    # 13 digits: no rule strips '0091'
    "919876543210123",
])
def test_phone_invalid(raw):
    assert normalise_phone(raw) is None


@pytest.mark.parametrize("raw,expected", [
    ("2026-12-01", "2026-12-01"),
    ("01/12/2026", "2026-12-01"),     # day-first
    ("01-12-2026", "2026-12-01"),
    ("01-Dec-2026", "2026-12-01"),
    ("01-DEC-2026", "2026-12-01"),    # month name case-insensitive
    ("29/02/2028", "2028-02-29"),     # leap year
])
def test_date_valid(raw, expected):
    assert normalise_date(raw) == expected


@pytest.mark.parametrize("raw", ["31/02/2026", "2026-13-01", "29/02/2026", "soon", "TBD",
                                 "01-Foo-2026", "2026/12/01", "12-01-26"])
def test_date_invalid(raw):
    assert normalise_date(raw) is None


@pytest.mark.parametrize("raw,expected", [
    ("27554", "27554.00"), ("₹27,554", "27554.00"), ("₹ 27,554", "27554.00"),
    ("Rs. 6850", "6850.00"), ("rs6850", "6850.00"), ("RS 6850", "6850.00"),
    ("INR 18678.3", "18678.30"), ("inr18678.3", "18678.30"),
    ("1,23,456.785", "123456.79"),    # Indian grouping; half-up rounding
    ("0", "0.00"),
])
def test_decimal_valid(raw, expected):
    assert normalise_decimal(raw) == expected


@pytest.mark.parametrize("raw", ["-5", "N/A", "-", "twelve thousand", "₹", "USD 10", "1.2.3"])
def test_decimal_invalid(raw):
    assert normalise_decimal(raw) is None


def test_enum_case_insensitive_and_lowercased():
    langs = ["en", "hi"]
    assert normalise_enum("HI", langs) == "hi"
    assert normalise_enum("Hindi", langs) is None


@pytest.mark.parametrize("raw,expected", [
    ("Mobile No", "mobile no"), ("  mobile__no ", "mobile no"), ("Mobile \t _ No", "mobile no"),
    ("customer_name", "customer name"), ("﻿customer_name", "customer name"),
])
def test_header(raw, expected):
    assert normalise_header(raw) == expected


def test_timestamps_three_forms_same_instant():
    a = parse_timestamp("2026-10-05T07:50:21Z")
    b = parse_timestamp("2026-10-05T13:20:21+05:30")
    c = parse_timestamp("2026-10-05 13:20:21")        # naive => IST
    assert a == b == c
    assert to_ist_iso(a) == "2026-10-05T13:20:21+05:30"


def test_ist_date_crosses_midnight():
    # 20:00 UTC is 01:30 the next day in IST: reports must use the IST date
    assert ist_date(parse_timestamp("2026-10-05T20:00:00Z")) == "2026-10-06"


def test_bad_timestamp():
    assert parse_timestamp("yesterday") is None
    assert parse_timestamp(None) is None
