"""Employee endpoints: POST /employees and GET /employees."""
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pymongo.errors import DuplicateKeyError

from app.config import DEFAULT_PAGE, DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, UTC
from app.database import db
from app.schemas import EmployeeCreate
from app.serializers import employee_to_api

router = APIRouter(tags=["employees"])


@router.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    """Create an employee. The unique index decides who wins a duplicate."""
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC)

    # No "does it exist?" check: the insert and the uniqueness test are one
    # atomic step inside MongoDB, which makes this race-free.
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")

    return employee_to_api(doc)


@router.get("/employees")
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
    # total counts the filtered set, not the whole collection (R10).
    total = db.employees.count_documents(query)

    cursor = db.employees.find(query, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(page_size)
    items = []
    for doc in cursor:
        items.append(employee_to_api(doc))

    return {"items": items, "total": total, "page": page, "page_size": page_size}
