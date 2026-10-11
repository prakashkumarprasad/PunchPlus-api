"""Settings and constants: environment variables, time zones and the business-rule numbers."""
import os
import re
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

# A local .env is only a convenience. load_dotenv() does NOT overwrite a variable
# that is already in the environment, so the real MONGO_URI always wins.
load_dotenv()

# The fallback values are for local development only. Nothing secret lives here.
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_dev")

# How long to wait for MongoDB before giving up (the driver default is 30 s).
SERVER_SELECTION_TIMEOUT_MS = 3000

# Indian Standard Time (UTC+05:30). Every shift time and attendance date is in IST.
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

# The zero point for epoch milliseconds: 1970-01-01T00:00:00Z.
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

# R2: grace period of 10 minutes; a punch-in must be strictly more than that to be late.
GRACE_SECONDS = 600
# R3: overtime only counts once it reaches 30 whole minutes.
MIN_OVERTIME_MINUTES = 30
# R5: a half day is a rounded work_hours below 4.50.
HALF_DAY_HOURS = 4.50

# Allowed range for an instant in epoch milliseconds (rejects "looks like seconds").
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000

# R10: paging.
DEFAULT_PAGE = 1
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100

# Default shift for a new employee.
DEFAULT_SHIFT_START = "09:30"
DEFAULT_SHIFT_END = "18:30"

# History change fields that hold an instant, so they are converted to epoch ms.
HISTORY_TIME_FIELDS = ["punch_in", "punch_out"]

# ABSENT and LEAVE: no punch times, every derived number is zero.
NON_PRESENT_STATUSES = ["ABSENT", "LEAVE"]

# The seven fields a regularization may change (DATA_MODEL.md section 4).
CHANGEABLE_FIELDS = ["status", "punch_in", "punch_out", "work_hours",
                     "late_minutes", "overtime_minutes", "half_day"]

# A punch-out must be within 24 hours of the punch-in (exactly 24 h is allowed).
MAX_PUNCH_DURATION = timedelta(hours=24)

# YYYY-MM-DD shape. Checked BEFORE date.fromisoformat(), which in Python 3.11+
# also accepts 20260101 and 2026-W01-1.
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# YYYY-MM, given to Query(pattern=...) so a bad month is a 422 before any code runs.
MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"

# R6: exactly these statuses count as present.
PRESENT_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]

# R5: a half day counts as 0.5 of a present day.
HALF_DAY_CREDIT = 0.5

# R7: $dayOfWeek counts Sunday as 1 and Saturday as 7, so Mon-Fri is 2..6.
FIRST_WORKING_WEEKDAY = 2
LAST_WORKING_WEEKDAY = 6

# Late leaderboard: `limit` caps the RANK, so ties can return more rows than the limit.
DEFAULT_LEADERBOARD_LIMIT = 10
MAX_LEADERBOARD_LIMIT = 50

# The trend returns one row per calendar day: at most 92 rows, 93 is a 422.
MAX_TREND_ROWS = 92
