"""Turn MongoDB documents into the JSON the contract asks for."""
from app.config import HISTORY_TIME_FIELDS
from app.timeutils import dt_to_ms


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

    The only place that decides what leaves the API: drops _id, converts every
    instant to epoch ms, and fills the defaults legacy documents may be missing.
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
