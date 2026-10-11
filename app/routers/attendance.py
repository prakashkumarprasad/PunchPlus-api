"""Attendance endpoints: punch-in, punch-out, list, and regularize (PATCH)."""
from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Query
from pymongo.errors import DuplicateKeyError

from app.config import (
    DEFAULT_PAGE, DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, MAX_PUNCH_DURATION, UTC,
)
from app.database import db
from app.queries import build_attendance_query
from app.rules import (
    attendance_date_for, build_changes, compute_hours_fields, compute_late_minutes,
    current_state, resolve_regularized_times,
)
from app.schemas import PunchInBody, PunchOutBody, RegularizeBody
from app.serializers import record_to_api
from app.timeutils import is_valid_date_string, ms_to_dt, truncate_seconds

router = APIRouter(tags=["attendance"])


@router.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInBody):
    """Create the day's record for an employee, applying R1 and R2."""
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(404, "unknown emp_code")

    # Compare with None, never with truthiness, so 0 is not "not supplied".
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


@router.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(DEFAULT_PAGE, ge=1),
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
):
    """A page of attendance records. Filtering, sorting and paging all happen in MongoDB."""
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

    # The database filters, sorts and pages, so only one page reaches this process.
    cursor = db.attendance_logs.find(query, {"_id": 0}).sort(sort).skip(skip).limit(limit)
    items = []
    for doc in cursor:
        items.append(record_to_api(doc))

    total = db.attendance_logs.count_documents(query)
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.post("/attendance/punch-out")
def punch_out(body: PunchOutBody):
    """Close the employee's most recent record, applying R3, R4 and R5."""
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(404, "unknown emp_code")

    if body.punched_at is None:
        punch_dt = datetime.now(UTC)
    else:
        punch_dt = ms_to_dt(body.punched_at)
    # R1: truncate to whole seconds before anything is calculated or stored.
    punch_dt = truncate_seconds(punch_dt)

    # The record to close is the most recent one whose punch_in is at or before
    # this punch-out. `date` desc is the right order: `date` is the IST day the
    # shift started, and the (emp_code, date) index already sorts by it.
    #
    # `$type: "date"` matters: null sorts below every date in MongoDB, so an
    # ABSENT or LEAVE record (null or missing punch_in) would otherwise match
    # `$lte` and be picked as the record to close.
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$type": "date", "$lte": punch_dt},
        },
        sort=[("date", -1)],
    )
    if record is None:
        raise HTTPException(404, "no punch-in at or before punched_at")

    # 409 comes before the 422 time checks: if the day is already closed, the
    # conflict is the more specific answer.
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

    # Compare-and-set. The filter repeats the punch_in we just read, so if another
    # request closed this record in between, nothing matches, matched_count is 0,
    # and this request answers 409 instead of overwriting it.
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

    # The compare-and-set proved these are the stored values, so the response is
    # the record we read plus what we just wrote.
    updated = dict(record)
    updated.update(values_to_set)
    return record_to_api(updated)


@router.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeBody):
    """Correct one day's record, recompute the derived fields, log the change.

    The parameter must be called `date` because the contract's path is
    /attendance/{emp_code}/{date}; FastAPI matches path names to argument names.
    """
    if not is_valid_date_string(date):
        raise HTTPException(422, "date must be a real date in YYYY-MM-DD format")

    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(404, "unknown emp_code")
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if record is None:
        raise HTTPException(404, "no attendance record for that date")

    # An omitted field means "leave it alone" (compare with None, not truthiness).
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

    # `changes` holds only the fields that really moved. Nothing moved is a 422.
    changes = build_changes(before, after)
    if changes == {}:
        raise HTTPException(422, "the request changes nothing")

    entry = {
        "at": datetime.now(UTC),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # One compare-and-set: the filter carries the record key and the three values
    # just read, `$set` writes the new values and `$push` appends the audit entry.
    # The history array is never read back and rewritten, so two concurrent
    # corrections cannot overwrite each other.
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
