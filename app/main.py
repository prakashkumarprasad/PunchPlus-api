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

# Date strings must look like YYYY-MM-DD. The regex is checked BEFORE
# date.fromisoformat(), because Python 3.11+ also accepts 20260101 and
# 2026-W01-1 and we do not want those.
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


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


# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   POST  /attendance/punch-out
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #