"""The business rules R1-R5 and the helpers a correction (PATCH) needs."""
from datetime import date, datetime, timedelta
from decimal import Decimal

from fastapi import HTTPException

from app.config import (
    CHANGEABLE_FIELDS, GRACE_SECONDS, HALF_DAY_HOURS, IST, MAX_PUNCH_DURATION,
    MIN_OVERTIME_MINUTES, NON_PRESENT_STATUSES,
)
from app.timeutils import (
    ms_to_dt, time_string_to_minutes, to_decimal_round, truncate_seconds,
)


def is_overnight_shift(shift_start: str, shift_end: str) -> bool:
    """True when the shift ends at or before it starts, e.g. 22:00 - 06:00."""
    return time_string_to_minutes(shift_end) <= time_string_to_minutes(shift_start)


def shift_start_datetime(date_str: str, shift_start: str) -> datetime:
    """The aware IST instant of the shift start on the attendance date."""
    day = date.fromisoformat(date_str)
    hours, minutes = shift_start.split(":")
    return datetime(day.year, day.month, day.day, int(hours), int(minutes), tzinfo=IST)


def shift_end_datetime(date_str: str, shift_start: str, shift_end: str) -> datetime:
    """The aware IST instant of the shift end. For an overnight shift it is the next day."""
    day = date.fromisoformat(date_str)
    hours, minutes = shift_end.split(":")
    end = datetime(day.year, day.month, day.day, int(hours), int(minutes), tzinfo=IST)
    if is_overnight_shift(shift_start, shift_end):
        end = end + timedelta(days=1)
    return end


def attendance_date_for(punch_dt: datetime, shift_start: str, shift_end: str) -> str:
    """R1: the attendance date of a punch-in. Punch-in must already be truncated."""
    local = punch_dt.astimezone(IST)
    if is_overnight_shift(shift_start, shift_end):
        earlier_than_shift_end = (
            local.hour * 60 + local.minute < time_string_to_minutes(shift_end)
        )
        if earlier_than_shift_end:
            # The shift began the day before, so that is the attendance date.
            local = local - timedelta(days=1)
    return local.date().isoformat()


def compute_late_minutes(punch_in: datetime, date_str: str, shift_start: str) -> int:
    """R2: whole minutes since shift start, but only when past the 10 minute grace."""
    start = shift_start_datetime(date_str, shift_start)
    grace_end = start + timedelta(seconds=GRACE_SECONDS)
    if punch_in <= grace_end:
        return 0
    seconds = int((punch_in - start).total_seconds())
    return seconds // 60


def compute_overtime_minutes(
    punch_out: datetime, date_str: str, shift_start: str, shift_end: str
) -> int:
    """R3: whole minutes after shift end, counted only once they reach 30."""
    end = shift_end_datetime(date_str, shift_start, shift_end)
    seconds = int((punch_out - end).total_seconds())
    if seconds <= 0:
        return 0
    minutes = seconds // 60
    if minutes < MIN_OVERTIME_MINUTES:
        return 0
    return minutes


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: hours between the two punches, 2 decimals, rounded half up."""
    seconds = int((punch_out - punch_in).total_seconds())
    hours = Decimal(seconds) / Decimal(3600)
    return float(to_decimal_round(hours, 2))


def compute_half_day(work_hours) -> bool:
    """R5: true when the rounded work_hours is below 4.50. None means no half day."""
    if work_hours is None:
        return False
    return work_hours < HALF_DAY_HOURS


def compute_hours_fields(punch_in, punch_out, date_str, shift_start, shift_end):
    """R3, R4 and R5 for one record: the three numbers punch-out produces.

    A record that is still open has no hours yet: null hours, 0 overtime, no half day.
    """
    if punch_out is None:
        return {"work_hours": None, "overtime_minutes": 0, "half_day": False}

    work_hours = compute_work_hours(punch_in, punch_out)
    return {
        "work_hours": work_hours,
        "overtime_minutes": compute_overtime_minutes(
            punch_out, date_str, shift_start, shift_end
        ),
        # R5 compares the ROUNDED hours, so it takes the value already rounded.
        "half_day": compute_half_day(work_hours),
    }


def current_state(record: dict) -> dict:
    """The seven changeable fields of a record, exactly as they are stored now.

    Missing late/overtime mean 0 and missing half_day means false, the same
    defaults the API reports, so a no-change correction never looks like a change.
    """
    return {
        "status": record.get("status"),
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes", 0),
        "overtime_minutes": record.get("overtime_minutes", 0),
        "half_day": record.get("half_day", False),
    }


def build_changes(before: dict, after: dict) -> dict:
    """One {from, to} pair for every field whose value actually changed."""
    changes = {}
    for field_name in CHANGEABLE_FIELDS:
        old_value = before.get(field_name)
        new_value = after.get(field_name)
        if old_value != new_value:
            changes[field_name] = {"from": old_value, "to": new_value}
    return changes


def resolve_regularized_times(record: dict, new_status: str, date_str: str,
                              employee: dict, sent_punch_in, sent_punch_out):
    """The punch_in and punch_out the record should have after a correction.

    `sent_punch_in` / `sent_punch_out` are epoch ms from the request, or None
    when the client did not send them. Any broken rule is a 422.
    """
    stored_punch_in = record.get("punch_in")
    stored_punch_out = record.get("punch_out")

    # ABSENT and LEAVE carry no punch times, so sending one contradicts the status.
    if new_status in NON_PRESENT_STATUSES:
        if sent_punch_in is not None or sent_punch_out is not None:
            raise HTTPException(422, "status ABSENT or LEAVE cannot have punch times")
        return None, None

    # A presence status needs a punch_in: sent here, or already stored.
    if sent_punch_in is not None:
        punch_in = truncate_seconds(ms_to_dt(sent_punch_in))
        # A punch-in the client sent must stay on this record's own attendance day,
        # otherwise the record would stop matching its (emp_code, date) key. Only a
        # sent punch-in can do that, so an old record with an inconsistent STORED
        # punch-in can still be corrected through another field.
        new_date = attendance_date_for(
            punch_in, employee["shift_start"], employee["shift_end"]
        )
        if new_date != date_str:
            raise HTTPException(422, "punch_in does not belong to this attendance date")
    elif stored_punch_in is not None:
        punch_in = stored_punch_in
    else:
        raise HTTPException(422, "a presence status requires a punch_in")

    if sent_punch_out is not None:
        punch_out = truncate_seconds(ms_to_dt(sent_punch_out))
    else:
        punch_out = stored_punch_out

    if punch_out is not None:
        if punch_out <= punch_in:
            raise HTTPException(422, "punch_out must be after punch_in")
        if punch_out > punch_in + MAX_PUNCH_DURATION:
            raise HTTPException(422, "punch_out must be within 24 hours of punch_in")

    return punch_in, punch_out
