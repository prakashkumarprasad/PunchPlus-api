"""Admin endpoint: GET /admin/explain/{endpoint}."""
import json
from typing import Literal, Optional

from bson import json_util
from fastapi import APIRouter, Query
from fastapi.exceptions import RequestValidationError

from app.config import (
    DEFAULT_LEADERBOARD_LIMIT, DEFAULT_PAGE, DEFAULT_PAGE_SIZE,
    MAX_LEADERBOARD_LIMIT, MAX_PAGE_SIZE, MONTH_PATTERN,
)
from app.database import db
from app.queries import (
    build_attendance_query, build_department_summary_pipeline,
    build_leaderboard_pipeline, build_monthly_pipeline, build_trend_pipeline,
)
from app.timeutils import is_valid_date_string

router = APIRouter(tags=["admin"])


def require_present(value, name: str):
    """Raise FastAPI's own 422 shape when a parameter the endpoint needs is missing."""
    if value is None:
        raise RequestValidationError(errors=[{
            "loc": ["query", name],
            "msg": "Field required",
            "type": "missing",
        }])


def check_date_argument(value, name: str):
    """422 when a date parameter is not a real YYYY-MM-DD day (same as the real endpoints)."""
    if value is not None and not is_valid_date_string(value):
        raise RequestValidationError(errors=[{
            "loc": ["query", name],
            "msg": "value is not a real date in YYYY-MM-DD format",
            "type": "value_error",
        }])


def build_explain_find(collection: str, query: dict, sort: list, skip: int, limit: int) -> dict:
    """The explain command for a find, with the endpoint's own filter and sort.

    MongoDB wants the sort as a document, so the list of pairs is turned into one.
    """
    sort_document = {}
    for field_name, direction in sort:
        sort_document[field_name] = direction

    return {
        "explain": {
            "find": collection,
            "filter": query,
            "sort": sort_document,
            "skip": skip,
            "limit": limit,
        },
        # executionStats makes the server count the documents and keys it looked at.
        "verbosity": "executionStats",
    }


def build_explain_aggregate(collection: str, pipeline: list) -> dict:
    """The explain command for an aggregation on the pipeline the endpoint runs."""
    return {
        "explain": {
            "aggregate": collection,
            "pipeline": pipeline,
            "cursor": {},
        },
        "verbosity": "executionStats",
    }


def explain_document_to_json(explain_document: dict) -> dict:
    """Turn MongoDB's explain reply (with BSON types) into plain JSON."""
    return json.loads(json_util.dumps(explain_document))


@router.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_PATTERN),
    department: Optional[str] = None,
    limit: int = Query(DEFAULT_LEADERBOARD_LIMIT, ge=1, le=MAX_LEADERBOARD_LIMIT),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    trend_from: Optional[str] = Query(None, alias="from"),
    trend_to: Optional[str] = Query(None, alias="to"),
    page: int = Query(DEFAULT_PAGE, ge=1),
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
):
    """MongoDB's own explain output for one endpoint's main query.

    Every branch calls the SAME builder the real endpoint calls. Nothing is
    written: explain runs the query for the plan and counters, then discards it.
    `from` and `to` arrive as trend_from and trend_to (Python keywords).
    """
    if endpoint == "attendance_list":
        check_date_argument(date_from, "date_from")
        check_date_argument(date_to, "date_to")
        query, sort, skip, page_limit = build_attendance_query(
            emp_code, date_from, date_to, status, page, page_size
        )
        collection = db.attendance_logs.name
        command = build_explain_find(collection, query, sort, skip, page_limit)

    elif endpoint == "employee_monthly":
        require_present(emp_code, "emp_code")
        require_present(month, "month")
        collection = db.attendance_logs.name
        command = build_explain_aggregate(
            collection, build_monthly_pipeline(emp_code, month)
        )

    # Starts on `employees`, so that is the collection explained.
    elif endpoint == "department_summary":
        require_present(month, "month")
        collection = db.employees.name
        command = build_explain_aggregate(
            collection, build_department_summary_pipeline(month, department)
        )

    elif endpoint == "late_leaderboard":
        require_present(month, "month")
        collection = db.attendance_logs.name
        command = build_explain_aggregate(
            collection, build_leaderboard_pipeline(month, limit, department)
        )

    # The Literal above already turned every other name into a 422, so this
    # branch only ever sees department_trend.
    else:
        require_present(department, "department")
        require_present(trend_from, "from")
        require_present(trend_to, "to")
        check_date_argument(trend_from, "from")
        check_date_argument(trend_to, "to")
        collection = db.employees.name
        command = build_explain_aggregate(
            collection, build_trend_pipeline(department, trend_from, trend_to)
        )

    explain_document = db.command(command)
    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": explain_document_to_json(explain_document),
    }
