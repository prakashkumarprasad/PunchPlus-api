"""Small helpers for time, dates and rounding. No business rules here."""
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from app.config import DATE_PATTERN, EPOCH, UTC


def truncate_seconds(moment: datetime) -> datetime:
    """Drop any sub-second precision, which R1 requires before any calculation."""
    return moment.replace(microsecond=0)


def ms_to_dt(epoch_ms: int) -> datetime:
    """Turn epoch milliseconds into an aware UTC datetime, using integer maths."""
    return EPOCH + timedelta(milliseconds=epoch_ms)


def dt_to_ms(moment: datetime) -> int:
    """Turn a datetime into integer epoch milliseconds. Naive input means UTC."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    difference = moment - EPOCH
    return difference // timedelta(milliseconds=1)


def to_decimal_round(value, places: int) -> Decimal:
    """Round to `places` decimals, half away from zero. This is R8's rounding."""
    step = Decimal(1).scaleb(-places)
    return Decimal(str(value)).quantize(step, rounding=ROUND_HALF_UP)


def time_string_to_minutes(time_str: str) -> int:
    """Turn a "HH:MM" shift time into minutes since midnight."""
    hours, minutes = time_str.split(":")
    return int(hours) * 60 + int(minutes)


def is_valid_date_string(text) -> bool:
    """True when the text is YYYY-MM-DD and really is a day that exists."""
    if not isinstance(text, str):
        return False
    if DATE_PATTERN.match(text) is None:
        return False
    try:
        date.fromisoformat(text)
    except ValueError:
        return False
    return True


def month_range(month: str):
    """Turn "YYYY-MM" into the first and last day of that month, as strings."""
    year = int(month[0:4])
    month_number = int(month[5:7])
    first_day = date(year, month_number, 1)
    # The last day of a month is one day before the first day of the next one.
    if month_number == 12:
        first_of_next_month = date(year + 1, 1, 1)
    else:
        first_of_next_month = date(year, month_number + 1, 1)
    last_day = first_of_next_month - timedelta(days=1)
    return first_day.isoformat(), last_day.isoformat()


def count_working_days(start: str, end: str) -> int:
    """R7: how many Monday-to-Friday days are in an inclusive YYYY-MM-DD range.

    A calendar calculation over at most 31 days, not a loop over documents.
    """
    current = date.fromisoformat(start)
    last = date.fromisoformat(end)
    count = 0
    while current <= last:
        # weekday() is 0 for Monday and 6 for Sunday, so Mon-Fri is 0 to 4.
        if current.weekday() <= 4:
            count = count + 1
        current = current + timedelta(days=1)
    return count
