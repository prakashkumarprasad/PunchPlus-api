"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

All application code lives in this one file, in this order:
  1. imports        4. helper functions
  2. config         5. request models
  3. database       6. endpoints

The HTTP contract and the business rules R1 - R10 are in openapi.yaml.
What is stored in MongoDB is in DATA_MODEL.md.
"""
# --------------------------------------------------------------------------- #
# 1. IMPORTS
# --------------------------------------------------------------------------- #
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError


# --------------------------------------------------------------------------- #
# 2. CONFIG AND CONSTANTS
# --------------------------------------------------------------------------- #

# A local .env is only a convenience. load_dotenv() does NOT overwrite a variable
# that is already in the environment, so the real MONGO_URI always wins.
load_dotenv()

# The fallback values are for local development only. Nothing secret lives here.
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_dev")

# How long to wait for MongoDB before giving up. The driver default is 30 s,
# which is far too long for a health check.
SERVER_SELECTION_TIMEOUT_MS = 3000

# Indian Standard Time. Every shift time and every attendance date in this
# assignment is in IST, which is UTC+05:30.
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

# The zero point for epoch milliseconds: 1970-01-01T00:00:00Z.
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

# R2: the grace period is 10 minutes, and a punch-in has to be strictly more
# than 10:00 after the shift start to count as late.
GRACE_SECONDS = 600

# R3: overtime only counts once it reaches 30 whole minutes.
MIN_OVERTIME_MINUTES = 30

# R5: a half day is a rounded work_hours below 4.50.
HALF_DAY_HOURS = 4.50

# The contract's allowed range for an instant in epoch milliseconds. Anything
# outside it is 422, which is how "looks like seconds" gets rejected.
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000

# R10: page starts at 1, page_size defaults to 20 and may not exceed 100.
DEFAULT_PAGE = 1
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100

# Default shift for a new employee, as given in the contract.
DEFAULT_SHIFT_START = "09:30"
DEFAULT_SHIFT_END = "18:30"

# The only fields inside a history change that hold an instant rather than a
# plain number, so they need converting to epoch milliseconds too.
HISTORY_TIME_FIELDS = ["punch_in", "punch_out"]

# ABSENT and LEAVE mean "did not work": both punch times stay null and every
# derived number is a zero. No punch-in may create them, only seeded back-office
# data or a correction (PATCH) may.
NON_PRESENT_STATUSES = ["ABSENT", "LEAVE"]

# The seven fields a regularization is allowed to change, in the order
# DATA_MODEL.md section 4 lists them. A history entry gets one key per field
# here whose value actually changed.
CHANGEABLE_FIELDS = ["status", "punch_in", "punch_out", "work_hours",
                     "late_minutes", "overtime_minutes", "half_day"]

# A punch-out (and a corrected punch_out) must be within 24 hours of the
# punch-in. Exactly 24 hours is still allowed; more is 422.
MAX_PUNCH_DURATION = timedelta(hours=24)

# Date strings must look like YYYY-MM-DD. The regex is checked BEFORE
# date.fromisoformat(), because Python 3.11+ also accepts 20260101 and
# 2026-W01-1 and we do not want those.
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# A month is written YYYY-MM. This is the contract's own pattern, handed to
# Query(pattern=...) so "2026-13" or "20267" is a 422 before any code runs.
MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"

# R6: exactly these three statuses count as present. ABSENT and LEAVE do not.
# The analytics pipelines test for membership in this list rather than for
# absence from NON_PRESENT_STATUSES, because a stored status that is in
# neither list must not silently count as a present day.
PRESENT_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]

# R5: a half day counts as 0.5 of a present day, never as a full day.
HALF_DAY_CREDIT = 0.5

# R7: MongoDB's $dayOfWeek counts Sunday as 1 and Saturday as 7, so Monday to
# Friday is exactly this range. Used inside the two analytics pipelines.
FIRST_WORKING_WEEKDAY = 2
LAST_WORKING_WEEKDAY = 6

# The late leaderboard's page size. `limit` caps the RANK, not the number of
# rows, so a tie at the cutoff can return more rows than the limit.
DEFAULT_LEADERBOARD_LIMIT = 10
MAX_LEADERBOARD_LIMIT = 50

# The trend returns exactly one row per calendar day, so the range has to be
# bounded. 92 rows is the most the contract allows; 93 is a 422.
MAX_TREND_ROWS = 92


# --------------------------------------------------------------------------- #
# 3. DATABASE, INDEXES AND THE APP
# --------------------------------------------------------------------------- #

# tz_aware=True means every datetime PyMongo reads back is aware and in UTC
# instead of naive. Without it, subtracting a value read from MongoDB from one
# we built ourselves raises TypeError.
client = MongoClient(
    MONGO_URI,
    tz_aware=True,
    serverSelectionTimeoutMS=SERVER_SELECTION_TIMEOUT_MS,
)
db = client[MONGO_DB]


@asynccontextmanager
async def lifespan(app_instance: FastAPI):
    """Create every index once, when the app starts. Safe to run again."""
    try:
        # A unique index on emp_code is what makes "exactly one create wins" true.
        db.employees.create_index("emp_code", unique=True, name="emp_code_unique")
        # Supports filtering employees by department, read in emp_code order.
        db.employees.create_index(
            [("department", 1), ("emp_code", 1)], name="department_emp_code"
        )
        # Supports the headcount questions: who had joined by a given day (R9).
        db.employees.create_index("joined_on", name="joined_on")

        # At most one record per (emp_code, date). This unique index is what turns
        # a duplicate punch-in into a DuplicateKeyError instead of a second row.
        db.attendance_logs.create_index(
            [("emp_code", 1), ("date", 1)], unique=True, name="emp_code_date_unique"
        )
        # Supports listing by date range in emp_code order, so the sort is free.
        db.attendance_logs.create_index(
            [("date", 1), ("emp_code", 1)], name="date_emp_code"
        )
    except PyMongoError:
        # If MongoDB is not up yet we still start, so that GET /health can answer
        # 503 as the contract requires. Refusing to start would mean the runner
        # gets a connection error instead of a 503.
        pass

    yield

    # Shut the connection pool down cleanly when the app stops.
    client.close()


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# 4. HELPER FUNCTIONS
# --------------------------------------------------------------------------- #

def truncate_seconds(moment: datetime) -> datetime:
    """Drop any sub-second precision, which R1 requires before any calculation."""
    return moment.replace(microsecond=0)


def ms_to_dt(epoch_ms: int) -> datetime:
    """Turn epoch milliseconds into an aware UTC datetime, using integer maths."""
    return EPOCH + timedelta(milliseconds=epoch_ms)


def dt_to_ms(moment: datetime) -> int:
    """Turn a datetime into integer epoch milliseconds. Naive input means UTC."""
    if moment.tzinfo is None:
        # Be forgiving: treat a naive datetime as UTC rather than crashing.
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
    # Check the shape first: date.fromisoformat() is too permissive in 3.11+.
    if DATE_PATTERN.match(text) is None:
        return False
    try:
        date.fromisoformat(text)
    except ValueError:
        return False
    return True


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
        # The shift that starts on 22-06 ends at 06:00 on the following day.
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


def history_value_to_api(value, field_name: str):
    """Convert one history change value. Only the punch fields hold instants."""
    if value is None:
        return None
    if field_name in HISTORY_TIME_FIELDS:
        return dt_to_ms(value)
    return value


def history_entry_to_api(entry: dict) -> dict:
    """One history entry, with its own instant and its change instants in ms."""
    changes = {}
    for field_name, pair in entry.get("changes", {}).items():
        changes[field_name] = {
            "from": history_value_to_api(pair.get("from"), field_name),
            "to": history_value_to_api(pair.get("to"), field_name),
        }
    at_value = entry.get("at")
    return {
        "at": dt_to_ms(at_value) if at_value is not None else None,
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": changes,
    }


def record_to_api(doc: dict) -> dict:
    """Turn one attendance_logs document into the JSON the contract asks for.

    This is the only place that decides what leaves the API: it drops _id,
    converts every instant to epoch milliseconds, and fills in the defaults
    that legacy documents are allowed to be missing.
    """
    return {
        "emp_code": doc.get("emp_code"),
        "date": doc.get("date"),
        "status": doc.get("status"),
        "punch_in": dt_to_ms(doc["punch_in"]) if doc.get("punch_in") else None,
        "punch_out": dt_to_ms(doc["punch_out"]) if doc.get("punch_out") else None,
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": [history_entry_to_api(entry) for entry in doc.get("history", [])],
    }


def employee_to_api(doc: dict) -> dict:
    """Turn one employees document into the JSON the contract asks for."""
    created_at = doc.get("created_at")
    return {
        "emp_code": doc.get("emp_code"),
        "name": doc.get("name"),
        "email": doc.get("email"),
        "department": doc.get("department"),
        "shift_start": doc.get("shift_start"),
        "shift_end": doc.get("shift_end"),
        "joined_on": doc.get("joined_on"),
        "created_at": dt_to_ms(created_at) if created_at is not None else None,
    }


def build_attendance_query(
    emp_code, date_from, date_to, status, page, page_size
):
    """Build the filter, sort, skip and limit that GET /attendance uses.

    This is a separate function on purpose: /admin/explain calls exactly this
    one, so the explained plan can never drift from the real query.
    """
    query = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        # Both ends are inclusive. ISO date strings compare correctly as text.
        date_filter = {}
        if date_from is not None:
            date_filter["$gte"] = date_from
        if date_to is not None:
            date_filter["$lte"] = date_to
        query["date"] = date_filter
    if status is not None:
        query["status"] = status

    # Newest day first, and emp_code ascending within a day, as the contract says.
    sort = [("date", -1), ("emp_code", 1)]
    skip = (page - 1) * page_size
    return query, sort, skip, page_size


def month_range(month: str):
    """Turn "YYYY-MM" into the first and last day of that month, as strings.

    The endpoint checks the shape with MONTH_PATTERN first, so by the time this
    runs `month` is always a real calendar month.
    """
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

    This is a calendar calculation over at most 31 days, not a loop over
    documents, so it stays in Python on purpose. Every loop that touches
    documents lives in an aggregation pipeline.
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


def build_monthly_pipeline(emp_code: str, month: str):
    """The aggregation behind GET /analytics/employees/{emp_code}/monthly.

    /admin/explain calls this same function, so the plan we explain can never
    drift from the plan the endpoint really runs. Everything is done in the
    database except `working_days`, which is a calendar count and is done in
    Python by count_working_days().
    """
    first_day, last_day = month_range(month)
    return [
        # Stage 1 - only this employee, and only inside the month. `date` is a
        # "YYYY-MM-DD" string and MongoDB compares strings in code-point order,
        # which is the same order as the calendar, so $gte/$lte is a correct
        # date range. This is exactly what the (emp_code, date) index serves.
        {"$match": {"emp_code": emp_code, "date": {"$gte": first_day, "$lte": last_day}}},

        # Stage 2 - the weekday number of the attendance date. $dateFromString
        # turns "2026-07-06" into a real date; $dayOfWeek then returns 1 for
        # Sunday through 7 for Saturday, so Mon-Fri is 2 to 6. onError and
        # onNull return null instead of failing the request, so one odd stored
        # date can never turn this endpoint into a 500.
        {"$addFields": {"weekday": {"$dayOfWeek": {"$dateFromString": {
            "dateString": "$date", "onError": None, "onNull": None}}}}},

        # Stage 3 - what this one record is worth towards present_days (R5-R7):
        # 1 for a presence status on a weekday, 0.5 for a half day, otherwise 0.
        # It must be a separate $addFields, because a field added by one
        # $addFields cannot be read by that same $addFields.
        {"$addFields": {"present_credit": {"$cond": [
            {"$and": [
                {"$in": ["$status", PRESENT_STATUSES]},
                {"$gte": ["$weekday", FIRST_WORKING_WEEKDAY]},
                {"$lte": ["$weekday", LAST_WORKING_WEEKDAY]},
            ]},
            {"$cond": [
                {"$eq": [{"$ifNull": ["$half_day", False]}, True]},
                HALF_DAY_CREDIT, 1,
            ]},
            0,
        ]}}},

        # Stage 4 - one row for the whole employee, so every number is a plain
        # sum. "_id": null means "put everything in one bucket". $ifNull makes
        # a legacy document with no late_minutes count as 0 instead of making
        # the whole sum null. Note there is no weekday test here: R7 says a
        # weekend record still adds to the late and overtime totals.
        {"$group": {
            "_id": None,
            "present_days": {"$sum": "$present_credit"},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [
                {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},

        # Stage 5 - name the five numbers and drop the "_id": null. The endpoint
        # reads these five names straight into its JSON response.
        {"$project": {
            "_id": 0,
            "present_days": 1,
            "leave_days": 1,
            "late_count": 1,
            "total_late_minutes": 1,
            "total_overtime_minutes": 1,
        }},
    ]


def build_department_summary_pipeline(month: str, department=None):
    """The aggregation behind GET /analytics/departments/summary.

    It starts on the `employees` collection, not on `attendance_logs`. That one
    decision is what makes somebody with no logs at all still appear in the
    headcount (R9) and somebody who joined after the month stay out of it.

    /admin/explain calls this same function.
    """
    first_day, last_day = month_range(month)

    # R9: joined on or before the last day of the month. The optional
    # department filter goes into the SAME $match below, so the database only
    # ever has to touch one part of the collection.
    who = {"joined_on": {"$lte": last_day}}
    if department is not None:
        # Exact match, case sensitive, so "Sales" and "sales" stay apart.
        who["department"] = department

    return [
        # Stage 1 - the people in scope. `joined_on` is a "YYYY-MM-DD" string
        # and compares correctly as text, and the joined_on index serves this.
        {"$match": who},

        # Stage 2 - this employee's logs for this month only. localField and
        # foreignField do the emp_code equality (so the emp_code index is
        # used) and the inner pipeline adds the month's date range; MongoDB
        # allows both together from 5.0 onwards. Without the inner pipeline
        # this would drag in every month the employee ever worked.
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": first_day, "$lte": last_day}}},
            ],
            "as": "logs",
        }},

        # Stage 3 - turn "one row per employee holding an array of logs" into
        # "one row per log". preserveNullAndEmptyArrays keeps the employee who
        # has no logs this month, as a single row with no `logs` field at all.
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},

        # Stage 4 - the weekday of the log, same trick as the monthly pipeline.
        {"$addFields": {"weekday": {"$dayOfWeek": {"$dateFromString": {
            "dateString": "$logs.date", "onError": None, "onNull": None}}}}},

        # Stage 5 - the four per-record numbers the group stage will sum.
        # hours_value is the work_hours of this record, or null when the record
        # must not be counted towards the average: a non-presence status, or
        # a record that is still open and has no hours yet. $type is the test
        # for "really holds a number", because a missing field compares as
        # NOT equal to null, so $ne null would wrongly accept it.
        {"$addFields": {
            "present_credit": {"$cond": [
                {"$and": [
                    {"$in": ["$logs.status", PRESENT_STATUSES]},
                    {"$gte": ["$weekday", FIRST_WORKING_WEEKDAY]},
                    {"$lte": ["$weekday", LAST_WORKING_WEEKDAY]},
                ]},
                {"$cond": [
                    {"$eq": [{"$ifNull": ["$logs.half_day", False]}, True]},
                    HALF_DAY_CREDIT, 1,
                ]},
                0,
            ]},
            "hours_value": {"$cond": [
                {"$and": [
                    {"$in": ["$logs.status", PRESENT_STATUSES]},
                    {"$in": [{"$type": "$logs.work_hours"},
                             ["double", "int", "long", "decimal"]]},
                ]},
                "$logs.work_hours", None,
            ]},
            "late_flag": {"$cond": [
                {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]},
            "leave_flag": {"$cond": [
                {"$eq": ["$logs.status", "LEAVE"]}, 1, 0]},
            "on_duty_flag": {"$cond": [
                {"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]},
        }},

        # Stage 6 - how many of this department's records went into the hours
        # average. It has to be its own $addFields, for the same reason as
        # stage 3 of the monthly pipeline: a stage cannot read what it just
        # added. $sum below ignores the nulls, which is exactly what we want.
        {"$addFields": {"hours_counted": {"$cond": [
            {"$in": [{"$type": "$hours_value"}, ["double", "int", "long", "decimal"]]},
            1, 0]}}},

        # Stage 7 - one row per department. headcount is the SIZE of the set of
        # distinct emp_codes, because one employee appears once per log; the
        # size itself is taken in the $project below. $addToSet is what makes
        # that count correct no matter how many days somebody worked.
        {"$group": {
            "_id": "$department",
            "members": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": "$present_credit"},
            "hours_sum": {"$sum": "$hours_value"},
            "hours_counted": {"$sum": "$hours_counted"},
            "late_count": {"$sum": "$late_flag"},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "leave_count": {"$sum": "$leave_flag"},
            "on_duty_count": {"$sum": "$on_duty_flag"},
        }},

        # Stage 8 - departments in ascending order, case sensitive, so "Sales"
        # comes before "sales" because 'S' sorts before 's'.
        {"$sort": {"_id": 1}},

        # Stage 9 - rename _id to department, turn the member set into a count,
        # and hand back only the eight fields the contract asks for.
        {"$project": {
            "_id": 0,
            "department": "$_id",
            "headcount": {"$size": "$members"},
            "present_days": 1,
            "hours_sum": 1,
            "hours_counted": 1,
            "late_count": 1,
            "total_late_minutes": 1,
            "leave_count": 1,
            "on_duty_count": 1,
        }},
    ]


def build_leaderboard_pipeline(month: str, limit: int, department=None):
    """The aggregation behind GET /analytics/leaderboard/late.

    Two things this has to get right, both stated in the contract:

    * the rank is STANDARD COMPETITION ranking, so ties share a rank and the
      next rank is skipped (1, 2, 2, 4). MongoDB's $rank does exactly that
      inside $setWindowFields; $denseRank would give 1, 2, 2, 3.
    * `limit` caps the RANK, not the number of rows. So it is applied as a
      $match on `rank` AFTER ranking, never as a $limit.

    /admin/explain calls this same function.
    """
    first_day, last_day = month_range(month)

    # The department filter goes INSIDE the $lookup, which is what makes the
    # ranking happen inside that department only: rows for other departments
    # are dropped before the window is computed, not after.
    employee_filter = {}
    if department is not None:
        # Exact match, case sensitive, so "Sales" and "sales" stay apart.
        employee_filter["department"] = department
    lookup_pipeline = []
    if employee_filter:
        lookup_pipeline.append({"$match": employee_filter})

    return [
        # Stage 1 - the month's records. `date` is a "YYYY-MM-DD" string and
        # strings compare in calendar order, so this is a plain range on the
        # (date, emp_code) index.
        {"$match": {"date": {"$gte": first_day, "$lte": last_day}}},

        # Stage 2 - one row per employee. $ifNull covers a legacy record that
        # has no late_minutes at all.
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "late_count": {"$sum": {"$cond": [
                {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
        }},

        # Stage 3 - only employees who were actually late. Doing it here, before
        # the lookup, means somebody with no late minutes never enters the
        # ranking at all.
        {"$match": {"total_late_minutes": {"$gt": 0}}},

        # Stage 4 - the employee's name and department. `_id` is the emp_code
        # (it is the $group key), and emp_code has a unique index, so this is
        # an indexed lookup rather than a scan.
        {"$lookup": {
            "from": "employees",
            "localField": "_id",
            "foreignField": "emp_code",
            "pipeline": lookup_pipeline,
            "as": "employee",
        }},

        # Stage 5 - one row per employee document. This is also what DROPS the
        # logs of employees who do not exist: the lookup returned an empty array
        # for them, and $unwind with no preserve flag throws the row away.
        {"$unwind": "$employee"},

        # Stage 6 - the rank. $rank is competition ranking: equal sort keys get
        # equal ranks and the following rank is skipped. There is no partition
        # here, so this is one window over the whole (already department-scoped)
        # set. The window is not partitionable at this point because ranking
        # has to happen after the department filter, not before.
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}},
        }},

        # Stage 7 - limit AFTER ranking. Two employees tied at rank 2 both come
        # back for limit=2, so the response can be longer than the limit. This
        # is deliberately NOT a $limit.
        {"$match": {"rank": {"$lte": limit}}},

        # Stage 8 - the row order the contract asks for: most late minutes
        # first, and emp_code ascending to break a tie. $setWindowFields does
        # not promise any order, so this sort is needed as well as the one
        # inside the window.
        {"$sort": {"total_late_minutes": -1, "_id": 1}},

        # Stage 9 - the six response fields. `_id` is the emp_code, so it is
        # renamed rather than dropped.
        {"$project": {
            "_id": 0,
            "rank": 1,
            "emp_code": "$_id",
            "name": "$employee.name",
            "department": "$employee.department",
            "total_late_minutes": 1,
            "late_count": 1,
        }},
    ]


def build_trend_pipeline(department: str, date_from: str, date_to: str):
    """The aggregation behind GET /analytics/departments/{department}/trend.

    The hard part is not the sums, it is that the answer must contain exactly
    one row per calendar day, weekends and days with no records included. That
    is a GAP-FILLING problem, and it is solved in the database, never with a
    loop in Python.

    /admin/explain calls this same function.
    """
    first = date.fromisoformat(date_from)
    last = date.fromisoformat(date_to)

    # The day series is built from an offset list. $densify was measured first
    # and REJECTED, for a reason worth knowing: on 7.0.43 it does fill a range
    # from zero input documents (so the plan's worry is unfounded), but every
    # document it generates carries ONLY the densified field, and neither
    # partitionByFields nor anything else makes it copy the seed's other
    # fields. join_dates and emp_codes would not survive it. $unwind does carry
    # them, so the skeleton is built with $range.
    day_count = (last - first).days + 1

    # $dateAdd needs a real Date to start from. The date a punch is stored
    # against is an IST calendar day, but this Date is only ever stepped
    # through one day at a time and printed back out, so UTC midnight is a
    # fine anchor and keeps the arithmetic obvious.
    range_start = datetime(first.year, first.month, first.day, tzinfo=UTC)

    return [
        # Stage 1 - the people in this department. The endpoint answers 404
        # before it calls this, so there is always at least one document here.
        # Starting from `employees` rather than from `attendance_logs` is what
        # makes the day series possible: it does not depend on anybody having
        # any logs at all.
        {"$match": {"department": department}},

        # Stage 2 - their join dates as real Dates, so they can be compared
        # with the day being built. onError/onNull so one odd joined_on cannot
        # turn the whole request into a 500.
        {"$addFields": {"joined_on_date": {"$dateFromString": {
            "dateString": "$joined_on", "onError": None, "onNull": None}}}},

        # Stage 3 - collapse to ONE document holding every join date and every
        # emp_code. From here on the pipeline never needs the employees
        # collection again.
        {"$group": {
            "_id": None,
            "join_dates": {"$push": "$joined_on_date"},
            "emp_codes": {"$push": "$emp_code"},
        }},

        # Stage 4 - one offset per calendar day in the range: 0, 1, 2, 3 ...
        # This is the gap filling. It does not look at any attendance record,
        # so it produces the same series for a month with no records at all,
        # and it cannot come up empty.
        {"$project": {
            "_id": 0,
            "day_offset": {"$range": [0, day_count]},
            "join_dates": 1,
            "emp_codes": 1,
        }},

        # Stage 5 - split the offsets into one document per day. $unwind keeps
        # every other field on each new document, which is the whole reason
        # this is a $range skeleton and not a $densify.
        {"$unwind": "$day_offset"},

        # Stage 6 - the real Date for this offset, and the calendar date as
        # the string attendance_logs.date holds. One day per row, weekends
        # included.
        {"$addFields": {"day": {"$dateAdd": {
            "startDate": range_start, "unit": "day", "amount": "$day_offset"}}}},
        # ...and that date as a string. It has to be a separate $addFields,
        # because a stage cannot read a field it just added.
        {"$addFields": {"date": {"$dateToString": {
            "date": "$day", "format": "%Y-%m-%d", "timezone": "UTC"}}}},

        # Stage 7 - the weekday number, so stage 8 can test Mon-Fri.
        {"$addFields": {"weekday": {"$dayOfWeek": {"$dateFromString": {
            "dateString": "$date", "onError": None, "onNull": None}}}}},

        # Stage 8 - Mon-Fri, and the headcount for this day (R9: employees who
        # had joined by this day). join_dates was carried from stage 3, so the
        # test is a straight comparison against this day's Date.
        {"$addFields": {
            "is_working_day": {"$and": [
                {"$gte": ["$weekday", FIRST_WORKING_WEEKDAY]},
                {"$lte": ["$weekday", LAST_WORKING_WEEKDAY]},
            ]},
            "headcount": {"$size": {"$filter": {
                "input": "$join_dates",
                "cond": {"$lte": ["$$this", "$day"]},
            }}},
        }},

        # Stage 9 - this day's attendance records for this department. The
        # plain {"date": "$$day_text"} form matches NOTHING, because $$ is only
        # substituted inside expression contexts, so the day has to be compared
        # with $expr. Measured on 7.0.43: that form is still an IXSCAN on the
        # (date, emp_code) index.
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"day_text": "$date", "codes": "$emp_codes"},
            "pipeline": [
                {"$match": {"$expr": {"$eq": ["$date", "$$day_text"]}}},
                {"$match": {"$expr": {"$in": ["$emp_code", "$$codes"]}}},
                # Score each log inside the lookup, so the outer pipeline only
                # has to add them up. Note there is NO weekday test here: the
                # contract counts a presence record on any day, weekday or
                # weekend, unlike present_days in the monthly summary.
                {"$addFields": {
                    "present_credit": {"$cond": [
                        {"$in": ["$status", PRESENT_STATUSES]},
                        {"$cond": [
                            {"$eq": [{"$ifNull": ["$half_day", False]}, True]},
                            HALF_DAY_CREDIT, 1,
                        ]},
                        0,
                    ]},
                    "late_flag": {"$cond": [
                        {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]},
                }},
            ],
            "as": "day_logs",
        }},

        # Stage 10 - one row per log. preserveNullAndEmptyArrays keeps a day
        # with no records, which is the whole point of the gap filling.
        {"$unwind": {"path": "$day_logs", "preserveNullAndEmptyArrays": True}},

        # Stage 11 - back to one row per day. $first carries the three values
        # that are identical for every log of the same day; $ifNull turns a
        # day with no logs into zeros rather than dropping it.
        {"$group": {
            "_id": "$day",
            "date": {"$first": "$date"},
            "is_working_day": {"$first": "$is_working_day"},
            "headcount": {"$first": "$headcount"},
            "present_count": {"$sum": {"$ifNull": ["$day_logs.present_credit", 0]}},
            "late_count": {"$sum": {"$ifNull": ["$day_logs.late_flag", 0]}},
        }},

        # Stage 12 - present_count / headcount, and ONLY on a working day with
        # somebody employed. A weekend has no rate, and a working day with no
        # records is 0.0 rather than null. Left UNROUNDED on purpose: the
        # moving average must average the exact rates, and Python rounds once
        # at the end.
        {"$addFields": {"attendance_rate_raw": {"$cond": [
            {"$and": [
                {"$eq": ["$is_working_day", True]},
                {"$gt": ["$headcount", 0]},
            ]},
            {"$divide": ["$present_count", "$headcount"]},
            None,
        ]}}},

        # Stage 13 - the 7 day moving average. The window is this row plus the
        # six before it, and it can only see rows inside the requested range
        # because the series starts at `from`, which is exactly what the
        # contract asks for. $avg ignores nulls, so a weekend is left out of
        # the average but does NOT shorten the window, and an all-null window
        # gives null.
        {"$setWindowFields": {
            "sortBy": {"_id": 1},
            "output": {"moving_avg_raw": {
                "$avg": "$attendance_rate_raw",
                "window": {"documents": [-6, 0]},
            }},
        }},

        # Stage 14 - chronological order. $setWindowFields does not promise
        # the order of what it returns.
        {"$sort": {"_id": 1}},

        # Stage 15 - hand back the raw numbers. Python rounds them, so that the
        # rounding happens in exactly one place.
        {"$project": {
            "_id": 0,
            "date": 1,
            "is_working_day": 1,
            "headcount": 1,
            "present_count": 1,
            "late_count": 1,
            "attendance_rate_raw": 1,
            "moving_avg_raw": 1,
        }},
    ]


def compute_hours_fields(punch_in, punch_out, date_str, shift_start, shift_end):
    """R3, R4 and R5 for one record: the three numbers punch-out produces.

    A record that is still open has no hours yet, so it keeps the defaults
    DATA_MODEL.md gives it: null hours, 0 overtime and no half day.
    """
    if punch_out is None:
        return {"work_hours": None, "overtime_minutes": 0, "half_day": False}

    work_hours = compute_work_hours(punch_in, punch_out)
    return {
        "work_hours": work_hours,
        "overtime_minutes": compute_overtime_minutes(
            punch_out, date_str, shift_start, shift_end
        ),
        # R5 compares the ROUNDED hours, which is why it takes the value
        # compute_work_hours already rounded rather than the raw seconds.
        "half_day": compute_half_day(work_hours),
    }


def current_state(record: dict) -> dict:
    """The seven changeable fields of a record, exactly as they are stored now.

    The defaults are the ones DATA_MODEL.md promises for a legacy document
    (missing late/overtime means 0, missing half_day means false), and they are
    the same defaults `record_to_api` uses. That matters: the "from" side of an
    audit entry has to be the value the API would have reported anyway, or a
    correction that changes nothing would look like it changed something.
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
    """One {from, to} pair for every field whose value actually changed.

    The comparison is on the final values, so a field the client did not send
    still appears when recomputing it produced a different number.
    """
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

    `sent_punch_in` and `sent_punch_out` are raw epoch milliseconds from the
    request body, or None when the client did not send that field. Everything
    that breaks one of the contract's rules is a 422.
    """
    stored_punch_in = record.get("punch_in")
    stored_punch_out = record.get("punch_out")

    # ABSENT and LEAVE carry no punch times at all, so a client that sends one
    # along with the status has contradicted itself.
    if new_status in NON_PRESENT_STATUSES:
        if sent_punch_in is not None or sent_punch_out is not None:
            raise HTTPException(422, "status ABSENT or LEAVE cannot have punch times")
        return None, None

    # A presence status needs a punch_in: sent here, or already stored.
    if sent_punch_in is not None:
        punch_in = truncate_seconds(ms_to_dt(sent_punch_in))
    elif stored_punch_in is not None:
        punch_in = stored_punch_in
    else:
        raise HTTPException(422, "a presence status requires a punch_in")

    if sent_punch_out is not None:
        punch_out = truncate_seconds(ms_to_dt(sent_punch_out))
    else:
        punch_out = stored_punch_out

    # R1 again: correcting a punch time must not move the record to another day.
    # This is the overnight-aware form, because attendance_date_for knows the
    # employee's shift.
    new_date = attendance_date_for(
        punch_in, employee["shift_start"], employee["shift_end"]
    )
    if new_date != date_str:
        raise HTTPException(422, "punch_in does not belong to this attendance date")

    if punch_out is not None:
        if punch_out <= punch_in:
            raise HTTPException(422, "punch_out must be after punch_in")
        if punch_out > punch_in + MAX_PUNCH_DURATION:
            raise HTTPException(422, "punch_out must be within 24 hours of punch_in")

    return punch_in, punch_out


# --------------------------------------------------------------------------- #
# 5. REQUEST MODELS
# --------------------------------------------------------------------------- #

class EmployeeCreate(BaseModel):
    """Body of POST /employees, with every constraint the contract lists."""

    # Unknown fields are dropped, so a client can never set a computed value.
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default=DEFAULT_SHIFT_START, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default=DEFAULT_SHIFT_END, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def joined_on_must_be_a_real_date(cls, value: str) -> str:
        """joined_on has to be YYYY-MM-DD and a day that exists."""
        if not is_valid_date_string(value):
            raise ValueError("joined_on must be a real date in YYYY-MM-DD format")
        return value

    @model_validator(mode="after")
    def shift_times_must_differ(self):
        """The contract rejects a shift that starts and ends at the same time."""
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInBody(BaseModel):
    """Body of POST /attendance/punch-in."""

    model_config = ConfigDict(extra="ignore")

    emp_code: str
    # StrictInt refuses floats, strings and bools: in Python bool is a subclass
    # of int, so True would otherwise be accepted as 1.
    punched_at: Optional[StrictInt] = Field(
        default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS
    )
    # Only the three presence statuses may be created by a punch-in.
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutBody(BaseModel):
    """Body of POST /attendance/punch-out."""

    model_config = ConfigDict(extra="ignore")

    emp_code: str
    # Same strict integer and same range as punch-in: the contract says a value
    # that looks like seconds must be 422 here too.
    punched_at: Optional[StrictInt] = Field(
        default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS
    )


class RegularizeBody(BaseModel):
    """Body of PATCH /attendance/{emp_code}/{date}.

    `reason` and `regularized_by` are always required because every
    regularization is written into the audit trail. The three fields being
    corrected are all optional: an omitted field means "leave it alone".
    """

    model_config = ConfigDict(extra="ignore")

    # Unlike punch-in, a correction may create ABSENT and LEAVE, so all five
    # statuses are allowed here.
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[StrictInt] = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)
    punch_out: Optional[StrictInt] = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# 6. ENDPOINTS
# --------------------------------------------------------------------------- #

# ------------------------------- system ------------------------------------ #

@app.get("/health", tags=["system"])
def health():
    """200 only when MongoDB answers a ping, otherwise 503."""
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "MongoDB is not reachable")
    return {"status": "ok"}


# ----------------------------- employees ----------------------------------- #

@app.post("/employees", status_code=201, tags=["employees"])
def create_employee(body: EmployeeCreate):
    """Create an employee. The unique index decides who wins a duplicate."""
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC)

    # No "does it exist?" check. The insert and the uniqueness test are one
    # atomic step inside MongoDB, which is what makes this race-free.
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")

    return employee_to_api(doc)


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = None,
    page: int = Query(DEFAULT_PAGE, ge=1),
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
):
    """A page of employees, filtered by department, sorted by emp_code."""
    query = {}
    if department is not None:
        query["department"] = department

    # page 1 is the FIRST page, so nothing is skipped.
    skip = (page - 1) * page_size
    # total must count the filtered set, not the whole collection (R10).
    total = db.employees.count_documents(query)

    cursor = db.employees.find(query, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(page_size)
    items = []
    for doc in cursor:
        items.append(employee_to_api(doc))

    return {"items": items, "total": total, "page": page, "page_size": page_size}


# ----------------------------- attendance ---------------------------------- #

@app.post("/attendance/punch-in", status_code=201, tags=["attendance"])
def punch_in(body: PunchInBody):
    """Create the day's record for an employee, applying R1 and R2."""
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(404, "unknown emp_code")

    # Compare with None, never with truthiness: 0 is a valid-looking but
    # out-of-range value and must not be treated as "not supplied".
    if body.punched_at is None:
        punch_dt = datetime.now(UTC)
    else:
        punch_dt = ms_to_dt(body.punched_at)

    # R1: truncate to whole seconds before anything is calculated or stored.
    punch_dt = truncate_seconds(punch_dt)
    date_str = attendance_date_for(punch_dt, employee["shift_start"], employee["shift_end"])

    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": punch_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(punch_dt, date_str, employee["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    # The unique (emp_code, date) index is the only duplicate check needed.
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")

    return record_to_api(doc)


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(DEFAULT_PAGE, ge=1),
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
):
    """A page of attendance records. Filtering, sorting and paging all happen in MongoDB."""
    # A date that is not YYYY-MM-DD, or not a real day, is a 422.
    if date_from is not None and not is_valid_date_string(date_from):
        raise HTTPException(422, "date_from must be a real date in YYYY-MM-DD format")
    if date_to is not None and not is_valid_date_string(date_to):
        raise HTTPException(422, "date_to must be a real date in YYYY-MM-DD format")
    # A backwards range can never match anything, so it is a mistake worth naming.
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")

    query, sort, skip, limit = build_attendance_query(
        emp_code, date_from, date_to, status, page, page_size
    )

    # The database does the filtering, sorting and paging, so only one page of
    # documents ever reaches this process.
    cursor = db.attendance_logs.find(query, {"_id": 0}).sort(sort).skip(skip).limit(limit)
    items = []
    for doc in cursor:
        items.append(record_to_api(doc))

    total = db.attendance_logs.count_documents(query)
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-out", tags=["attendance"])
def punch_out(body: PunchOutBody):
    """Close the employee's most recent record, applying R3, R4 and R5."""
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(404, "unknown emp_code")

    # Compare with None, never with truthiness, exactly as punch-in does.
    if body.punched_at is None:
        punch_dt = datetime.now(UTC)
    else:
        punch_dt = ms_to_dt(body.punched_at)
    # R1: truncate to whole seconds before anything is calculated or stored.
    punch_dt = truncate_seconds(punch_dt)

    # The record to close is the most recent one whose punch_in is at or before
    # this punch-out. `date` desc is the right order here, because `date` is
    # the IST day the shift started and it is what the (emp_code, date) index
    # already sorts by.
    #
    # `$type: "date"` matters: in MongoDB a null sorts below every date, so an
    # ABSENT or LEAVE record (punch_in null, or missing on an old document)
    # would otherwise match `$lte` and get picked as the record to close.
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$type": "date", "$lte": punch_dt},
        },
        sort=[("date", -1)],
    )
    if record is None:
        raise HTTPException(404, "no punch-in at or before punched_at")

    # 409 comes before the 422 time checks. If the day is already closed there
    # is nothing left to check the times against, and the conflict is the more
    # specific answer.
    if record.get("punch_out") is not None:
        raise HTTPException(409, "that record is already punched out")

    punch_in = record["punch_in"]
    if punch_dt <= punch_in:
        raise HTTPException(422, "punched_at must be after punch_in")
    if punch_dt > punch_in + MAX_PUNCH_DURATION:
        raise HTTPException(422, "punched_at must be within 24 hours of punch_in")

    values_to_set = {"punch_out": punch_dt}
    values_to_set.update(
        compute_hours_fields(
            punch_in, punch_dt, record["date"],
            employee["shift_start"], employee["shift_end"],
        )
    )

    # Compare-and-set. The filter repeats the punch_in that was just read, so if
    # another request closed this record in between, nothing matches the filter,
    # matched_count is 0, and this request answers 409 instead of overwriting it.
    result = db.attendance_logs.update_one(
        {
            "emp_code": body.emp_code,
            "date": record["date"],
            "punch_in": punch_in,
            "punch_out": None,
        },
        {"$set": values_to_set},
    )
    if result.matched_count == 0:
        raise HTTPException(409, "that record was punched out by someone else")

    # The compare-and-set just proved these values are the stored ones, so the
    # response is built from the record we read plus what we just wrote.
    updated = dict(record)
    updated.update(values_to_set)
    return record_to_api(updated)


@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize(emp_code: str, date: str, body: RegularizeBody):
    """Correct one day's record, recompute the derived fields, log the change.

    The parameter named `date` shadows the `datetime.date` class inside this
    function. That is deliberate: the contract's path is
    /attendance/{emp_code}/{date}, so FastAPI needs that exact name. Nothing in
    this function needs the class, and the helper below takes `date_str`.
    """
    if not is_valid_date_string(date):
        raise HTTPException(422, "date must be a real date in YYYY-MM-DD format")

    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(404, "unknown emp_code")
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if record is None:
        raise HTTPException(404, "no attendance record for that date")

    # A field the client omitted means "leave it alone". Comparing with None
    # (never with truthiness) keeps 0 from looking like "not supplied".
    new_status = body.status if body.status is not None else record.get("status")
    new_punch_in, new_punch_out = resolve_regularized_times(
        record, new_status, date, employee, body.punch_in, body.punch_out
    )

    # Recompute R2 - R5 from the final state, not from the old numbers.
    if new_punch_in is None:
        derived = {"late_minutes": 0, "work_hours": None,
                   "overtime_minutes": 0, "half_day": False}
    else:
        derived = {
            "late_minutes": compute_late_minutes(
                new_punch_in, date, employee["shift_start"]
            )
        }
        derived.update(
            compute_hours_fields(
                new_punch_in, new_punch_out, date,
                employee["shift_start"], employee["shift_end"],
            )
        )

    before = current_state(record)
    after = dict(before)
    after["status"] = new_status
    after["punch_in"] = new_punch_in
    after["punch_out"] = new_punch_out
    after.update(derived)

    # `changes` holds only the fields that really moved, so a request that
    # changes nothing is detectable. The contract calls that a 422.
    changes = build_changes(before, after)
    if changes == {}:
        raise HTTPException(422, "the request changes nothing")

    entry = {
        "at": datetime.now(UTC),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # One compare-and-set does everything: the filter carries the record key and
    # the three values that were just read, `$set` writes the new values, and
    # `$push` appends the audit entry. The history array is never read back and
    # rewritten, so two concurrent corrections cannot overwrite each other.
    result = db.attendance_logs.update_one(
        {
            "emp_code": emp_code,
            "date": date,
            "status": before["status"],
            "punch_in": before["punch_in"],
            "punch_out": before["punch_out"],
        },
        {"$set": after, "$push": {"history": entry}},
    )
    if result.matched_count == 0:
        raise HTTPException(409, "the record changed while this request ran")

    updated = dict(record)
    updated.update(after)
    updated["history"] = list(record.get("history", []))
    updated["history"].append(entry)
    return record_to_api(updated)


# ------------------------------ analytics ---------------------------------- #

@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(
    emp_code: str,
    month: str = Query(pattern=MONTH_PATTERN),
):
    """One employee's month in numbers, from one aggregation pipeline.

    Everything except working_days and attendance_pct is summed by MongoDB
    over the STORED derived values; those two are calendar arithmetic.
    """
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(404, "unknown emp_code")

    first_day, last_day = month_range(month)
    pipeline = build_monthly_pipeline(emp_code, month)
    # A $group with no input rows returns NO rows at all, so an employee with
    # no logs this month gives an empty list rather than a row of zeros.
    rows = list(db.attendance_logs.aggregate(pipeline))
    if rows == []:
        totals = {
            "present_days": 0,
            "leave_days": 0,
            "late_count": 0,
            "total_late_minutes": 0,
            "total_overtime_minutes": 0,
        }
    else:
        totals = rows[0]

    # R7: a mid-month joiner is only expected to work from joined_on onwards.
    # Both values are YYYY-MM-DD strings, so they compare correctly as text.
    if employee["joined_on"] > first_day:
        count_from = employee["joined_on"]
    else:
        count_from = first_day
    working_days = count_working_days(count_from, last_day)

    # A percentage of nothing is nothing, and the contract says null, not 0.
    if working_days == 0:
        attendance_pct = None
    else:
        # R8: 2 decimals, half up. Decimal, never round(), and never MongoDB's
        # $round, which rounds half to even.
        percentage = Decimal(str(totals["present_days"])) / Decimal(working_days) * 100
        attendance_pct = float(to_decimal_round(percentage, 2))

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": totals["present_days"],
        "leave_days": totals["leave_days"],
        "late_count": totals["late_count"],
        "total_late_minutes": totals["total_late_minutes"],
        "total_overtime_minutes": totals["total_overtime_minutes"],
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: str = Query(pattern=MONTH_PATTERN),
    department: Optional[str] = None,
):
    """One row per department for a month, from one aggregation pipeline."""
    pipeline = build_department_summary_pipeline(month, department)
    rows = list(db.employees.aggregate(pipeline))

    items = []
    for row in rows:
        # The average is over RECORDS, not over per-employee averages, so the
        # denominator is how many records carried a work_hours value at all.
        if row["hours_counted"] == 0:
            avg_work_hours = None
        else:
            # Round the sum first, to throw away float noise such as
            # 113.24999999999999, then divide with Decimal and round once.
            hours_sum = to_decimal_round(row["hours_sum"], 2)
            average = hours_sum / Decimal(row["hours_counted"])
            avg_work_hours = float(to_decimal_round(average, 2))
        items.append({
            "department": row["department"],
            "headcount": row["headcount"],
            "present_days": row["present_days"],
            "avg_work_hours": avg_work_hours,
            "late_count": row["late_count"],
            "total_late_minutes": row["total_late_minutes"],
            "leave_count": row["leave_count"],
            "on_duty_count": row["on_duty_count"],
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: str = Query(pattern=MONTH_PATTERN),
    limit: int = Query(DEFAULT_LEADERBOARD_LIMIT, ge=1, le=MAX_LEADERBOARD_LIMIT),
    department: Optional[str] = None,
):
    """Rank employees by late minutes in a month, ties sharing a rank.

    Every number here is an integer count or an integer sum of integers, so
    there is nothing to round: R8 applies to the trend's rates instead.
    """
    pipeline = build_leaderboard_pipeline(month, limit, department)
    rows = list(db.attendance_logs.aggregate(pipeline))

    items = []
    for row in rows:
        items.append({
            "rank": row["rank"],
            "emp_code": row["emp_code"],
            "name": row["name"],
            "department": row["department"],
            "total_late_minutes": row["total_late_minutes"],
            "late_count": row["late_count"],
        })

    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str,
    date_from: str = Query(alias="from"),
    date_to: str = Query(alias="to"),
):
    """One row per calendar day for a department, weekends and gaps included.

    `from` and `to` are Python keywords, so the arguments are called
    date_from and date_to and Query(alias=...) maps them back to the wire
    names the contract uses.
    """
    # Argument problems are answered before state problems, so a malformed
    # range is a 422 even for a department nobody has ever heard of.
    if not is_valid_date_string(date_from):
        raise HTTPException(422, "from must be a real date in YYYY-MM-DD format")
    if not is_valid_date_string(date_to):
        raise HTTPException(422, "to must be a real date in YYYY-MM-DD format")
    if date_to < date_from:
        raise HTTPException(422, "to must not be before from")
    # The range is counted in ROWS, both ends included, because the response
    # has one row per calendar day. 92 rows is allowed, 93 is not.
    row_count = (date.fromisoformat(date_to) - date.fromisoformat(date_from)).days + 1
    if row_count > MAX_TREND_ROWS:
        raise HTTPException(422, f"the range must not exceed {MAX_TREND_ROWS} days")

    # A department nobody is in is a 404, which is the one 404 in the
    # analytics. Department names are case sensitive, so "engineering" is not
    # "Engineering" and is a 404 too.
    if db.employees.find_one({"department": department}) is None:
        raise HTTPException(404, "unknown department")

    pipeline = build_trend_pipeline(department, date_from, date_to)
    rows = list(db.employees.aggregate(pipeline))

    items = []
    for row in rows:
        # R8: rates are 4 decimals, half up. MongoDB's $round would round half
        # to even, and rounding inside the pipeline would also feed rounded
        # values into the moving average.
        if row["attendance_rate_raw"] is None:
            attendance_rate = None
        else:
            attendance_rate = float(to_decimal_round(row["attendance_rate_raw"], 4))
        if row["moving_avg_raw"] is None:
            moving_avg_7d = None
        else:
            moving_avg_7d = float(to_decimal_round(row["moving_avg_raw"], 4))
        items.append({
            "date": row["date"],
            "is_working_day": row["is_working_day"],
            "headcount": row["headcount"],
            "present_count": row["present_count"],
            "late_count": row["late_count"],
            "attendance_rate": attendance_rate,
            "moving_avg_7d": moving_avg_7d,
        })

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #