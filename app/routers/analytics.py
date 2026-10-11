"""Analytics endpoints: monthly, department summary, late leaderboard, department trend."""
from datetime import date
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from app.config import (
    DEFAULT_LEADERBOARD_LIMIT, MAX_LEADERBOARD_LIMIT, MAX_TREND_ROWS, MONTH_PATTERN,
)
from app.database import db
from app.queries import (
    build_department_summary_pipeline, build_leaderboard_pipeline,
    build_monthly_pipeline, build_trend_pipeline,
)
from app.timeutils import (
    count_working_days, is_valid_date_string, month_range, to_decimal_round,
)

router = APIRouter(tags=["analytics"])


@router.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(pattern=MONTH_PATTERN),
):
    """One employee's month in numbers, from one aggregation pipeline.

    MongoDB sums the STORED derived values; working_days and attendance_pct are
    calendar arithmetic.
    """
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(404, "unknown emp_code")

    first_day, last_day = month_range(month)
    pipeline = build_monthly_pipeline(emp_code, month)
    # A $group with no input rows returns NO rows, so an employee with no logs
    # this month gives an empty list rather than a row of zeros.
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

    # A percentage of nothing is null, not 0.
    if working_days == 0:
        attendance_pct = None
    else:
        # R8: 2 decimals, half up (Decimal, never round() or MongoDB's $round).
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


@router.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(pattern=MONTH_PATTERN),
    department: Optional[str] = None,
):
    """One row per department for a month, from one aggregation pipeline."""
    pipeline = build_department_summary_pipeline(month, department)
    rows = list(db.employees.aggregate(pipeline))

    items = []
    for row in rows:
        # The average is over RECORDS, not per-employee averages, so the
        # denominator is how many records carried a work_hours value.
        if row["hours_counted"] == 0:
            avg_work_hours = None
        else:
            # Round the sum first to throw away float noise (113.24999999999999),
            # then divide with Decimal and round once.
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


@router.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(pattern=MONTH_PATTERN),
    limit: int = Query(DEFAULT_LEADERBOARD_LIMIT, ge=1, le=MAX_LEADERBOARD_LIMIT),
    department: Optional[str] = None,
):
    """Rank employees by late minutes in a month, ties sharing a rank.

    Every number is an integer count or sum, so there is nothing to round.
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


@router.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    date_from: str = Query(alias="from"),
    date_to: str = Query(alias="to"),
):
    """One row per calendar day for a department, weekends and gaps included.

    `from` and `to` are Python keywords, so the arguments are date_from and
    date_to and Query(alias=...) maps them back to the contract's names.
    """
    # Argument problems are answered before state problems.
    if not is_valid_date_string(date_from):
        raise HTTPException(422, "from must be a real date in YYYY-MM-DD format")
    if not is_valid_date_string(date_to):
        raise HTTPException(422, "to must be a real date in YYYY-MM-DD format")
    if date_to < date_from:
        raise HTTPException(422, "to must not be before from")
    # Counted in ROWS, both ends included: 92 rows is allowed, 93 is not.
    row_count = (date.fromisoformat(date_to) - date.fromisoformat(date_from)).days + 1
    if row_count > MAX_TREND_ROWS:
        raise HTTPException(422, f"the range must not exceed {MAX_TREND_ROWS} days")

    # A department nobody is in is a 404 (case sensitive).
    if db.employees.find_one({"department": department}) is None:
        raise HTTPException(404, "unknown department")

    pipeline = build_trend_pipeline(department, date_from, date_to)
    rows = list(db.employees.aggregate(pipeline))

    items = []
    for row in rows:
        # R8: rates are 4 decimals, half up, rounded here once (MongoDB's $round
        # is half-to-even, and rounding in the pipeline would feed rounded values
        # into the moving average).
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
