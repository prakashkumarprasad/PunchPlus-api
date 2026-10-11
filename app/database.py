"""The MongoDB connection and the indexes."""
from pymongo import MongoClient

from app.config import MONGO_DB, MONGO_URI, SERVER_SELECTION_TIMEOUT_MS

# tz_aware=True means every datetime PyMongo reads back is aware (UTC), not naive.
# Without it, subtracting a stored value from one we built raises TypeError.
client = MongoClient(
    MONGO_URI,
    tz_aware=True,
    serverSelectionTimeoutMS=SERVER_SELECTION_TIMEOUT_MS,
)
db = client[MONGO_DB]


def ensure_indexes():
    """Create every index this app needs, in one place.

    Calling this more than once is harmless: MongoDB keeps an index that is
    already there. That is what lets start-up and GET /health both call it.
    """
    # A unique index on emp_code is what makes "exactly one create wins" true.
    db.employees.create_index("emp_code", unique=True, name="emp_code_unique")
    # Filtering employees by department, read in emp_code order.
    db.employees.create_index(
        [("department", 1), ("emp_code", 1)], name="department_emp_code"
    )
    # Headcount questions: who had joined by a given day (R9).
    db.employees.create_index("joined_on", name="joined_on")

    # At most one record per (emp_code, date): a duplicate punch-in becomes a
    # DuplicateKeyError instead of a second row.
    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", 1)], unique=True, name="emp_code_date_unique"
    )
    # GET /attendance sorts date DESC then emp_code ASC. An index only supplies a
    # sort when its order matches, and walking an index backwards flips every
    # field, so this order needs its own index. Month range scans use it too.
    db.attendance_logs.create_index(
        [("date", -1), ("emp_code", 1)], name="date_emp_code_desc"
    )
