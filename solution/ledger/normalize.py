"""Pure, side-effect-free normalisers for every value type in the spec.

Everything here is a plain function of its input, so it is trivial to unit-test.
Each value normaliser returns the normalised value, or ``None`` when the value is
invalid. "Missing" (empty after trim) is decided by the caller, not here.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

IST = timezone(timedelta(hours=5, minutes=30))

# --------------------------------------------------------------------------- headers

_HEADER_SEP = re.compile(r"[_\s]+")


def normalise_header(h: str) -> str:
    """lower-case, trim, collapse every run of '_' / whitespace into one space.

    We strip once more at the end so that '_name_' and 'name' are equal
    (the spec trims before collapsing; a leading '_' would otherwise leave a space).
    """
    h = (h or "").replace("﻿", "")  # tolerate a UTF-8 BOM on the first header
    return _HEADER_SEP.sub(" ", h.lower().strip()).strip()


# --------------------------------------------------------------------------- phone

_NON_DIGIT = re.compile(r"\D")


def normalise_phone(raw: str | None) -> str | None:
    """Indian mobile -> '+91XXXXXXXXXX', or None if invalid."""
    if raw is None:
        return None
    digits = _NON_DIGIT.sub("", str(raw))
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10 and digits[0] in "6789":
        return "+91" + digits
    return None


# --------------------------------------------------------------------------- date

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

_ISO_DATE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
_DMY_SLASH = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")
_DMY_DASH = re.compile(r"^(\d{1,2})-(\d{1,2})-(\d{4})$")
_DMON = re.compile(r"^(\d{1,2})-([A-Za-z]{3})-(\d{4})$")


def normalise_date(raw: str | None) -> str | None:
    """Accepts YYYY-MM-DD, DD/MM/YYYY, DD-MM-YYYY, DD-Mon-YYYY (day-first).

    Returns ISO 'YYYY-MM-DD' or None. Impossible dates (31/02) are invalid
    because ``datetime.date`` refuses to construct them.
    """
    if raw is None:
        return None
    s = raw.strip()
    try:
        if m := _ISO_DATE.match(s):
            y, mo, d = int(m[1]), int(m[2]), int(m[3])
        elif m := (_DMY_SLASH.match(s) or _DMY_DASH.match(s)):
            d, mo, y = int(m[1]), int(m[2]), int(m[3])
        elif m := _DMON.match(s):
            mo = _MONTHS.get(m[2].lower())
            if mo is None:
                return None
            d, y = int(m[1]), int(m[3])
        else:
            return None
        return date(y, mo, d).isoformat()
    except ValueError:
        return None


# --------------------------------------------------------------------------- decimal

# Optional currency prefix (₹ / Rs / Rs. / INR, any case, optional space), then a
# non-negative number with optional ',' thousands separators and decimals.
_DECIMAL = re.compile(r"^(?:₹|rs\.?|inr)?\s*(\d[\d,]*(?:\.\d+)?)$", re.IGNORECASE)


def normalise_decimal(raw: str | None) -> str | None:
    """Returns the amount as a string with exactly 2 decimals, or None.

    Decimal (not float) so that 0.005 rounds half-up exactly. A leading '-' never
    matches the regex, so negatives are invalid, which covers the '>= 0' rule.
    """
    if raw is None:
        return None
    m = _DECIMAL.match(raw.strip())
    if not m:
        return None
    try:
        value = Decimal(m[1].replace(",", ""))
    except InvalidOperation:
        return None
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------- enum

def normalise_enum(raw: str | None, allowed: list[str]) -> str | None:
    if raw is None:
        return None
    v = raw.strip().lower()
    return v if v in {a.lower() for a in allowed} else None


# --------------------------------------------------------------------------- timestamps

def parse_timestamp(raw) -> datetime | None:
    """Parse an event timestamp into an aware UTC datetime.

    '...Z' -> UTC, '...+05:30' -> that offset, naive -> **IST** (per spec).
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    s = raw.strip()
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(timezone.utc)


def to_ist_iso(dt: datetime) -> str:
    """'2026-10-14T17:55:24+05:30'."""
    return dt.astimezone(IST).replace(microsecond=0).isoformat()


def ist_date(dt: datetime) -> str:
    return dt.astimezone(IST).date().isoformat()


def mask_phone(last4: str) -> str:
    return "+91******" + last4
