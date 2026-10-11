"""The MongoDB queries: the attendance list filter and the four aggregation pipelines.

Each builder is a plain function. The endpoint AND /admin/explain both call it,
so the plan that gets explained is always the plan that really runs.
"""
from datetime import date, datetime

from app.config import (
    FIRST_WORKING_WEEKDAY, HALF_DAY_CREDIT, LAST_WORKING_WEEKDAY,
    PRESENT_STATUSES, UTC,
)
from app.timeutils import month_range


def build_attendance_query(emp_code, date_from, date_to, status, page, page_size):
    """Build the filter, sort, skip and limit that GET /attendance uses."""
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


def build_monthly_pipeline(emp_code: str, month: str):
    """Pipeline for GET /analytics/employees/{emp_code}/monthly (runs on attendance_logs).

    Everything is done in the database except `working_days`, a calendar count.
    """
    first_day, last_day = month_range(month)
    return [
        # Stage 1 - this employee, inside the month. `date` is a "YYYY-MM-DD"
        # string and strings compare in calendar order, so $gte/$lte is a correct
        # date range. The (emp_code, date) index serves this.
        {"$match": {"emp_code": emp_code, "date": {"$gte": first_day, "$lte": last_day}}},

        # Stage 2 - weekday number of the date: $dayOfWeek gives 1 for Sunday to
        # 7 for Saturday, so Mon-Fri is 2 to 6. onError/onNull give null instead
        # of failing, so one odd stored date can never cause a 500.
        {"$addFields": {"weekday": {"$dayOfWeek": {"$dateFromString": {
            "dateString": "$date", "onError": None, "onNull": None}}}}},

        # Stage 3 - what this record is worth towards present_days (R5-R7): 1 for
        # a presence status on a weekday, 0.5 for a half day, otherwise 0. A
        # separate $addFields, because one $addFields cannot read a field that
        # it adds itself.
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

        # Stage 4 - one bucket for the whole employee ("_id": null). $ifNull makes
        # a legacy document with no late_minutes count as 0. There is no weekday
        # test: a weekend record still adds to late and overtime totals (R7).
        {"$group": {
            "_id": None,
            "present_days": {"$sum": "$present_credit"},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [
                {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},

        # Stage 5 - keep the five numbers, drop the "_id": null.
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
    """Pipeline for GET /analytics/departments/summary (runs on employees).

    It starts on `employees`, not `attendance_logs`, so somebody with no logs
    still counts in the headcount (R9) and somebody who joined after the month
    stays out of it.
    """
    first_day, last_day = month_range(month)

    # R9: joined on or before the last day of the month. The optional department
    # filter goes in the same $match.
    who = {"joined_on": {"$lte": last_day}}
    if department is not None:
        # Exact match, case sensitive, so "Sales" and "sales" stay apart.
        who["department"] = department

    return [
        # Stage 1 - the people in scope (joined_on index).
        {"$match": who},

        # Stage 2 - each employee's logs for this month only. localField and
        # foreignField do the emp_code equality (emp_code index); the inner
        # pipeline adds the month's date range (supported from MongoDB 5.0).
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": first_day, "$lte": last_day}}},
            ],
            "as": "logs",
        }},

        # Stage 3 - one row per log. preserveNullAndEmptyArrays keeps the
        # employee who has no logs, as one row with no `logs` field.
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},

        # Stage 4 - weekday of the log, same trick as the monthly pipeline.
        {"$addFields": {"weekday": {"$dayOfWeek": {"$dateFromString": {
            "dateString": "$logs.date", "onError": None, "onNull": None}}}}},

        # Stage 5 - the per-record numbers the group stage sums. hours_value is
        # the record's work_hours, or null when it must not count towards the
        # average (non-presence status, or an open record with no hours). $type
        # tests "really holds a number", because a missing field is not equal to
        # null.
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

        # Stage 6 - how many records went into the hours average (its own
        # $addFields: a stage cannot read what it just added). $sum ignores nulls.
        {"$addFields": {"hours_counted": {"$cond": [
            {"$in": [{"$type": "$hours_value"}, ["double", "int", "long", "decimal"]]},
            1, 0]}}},

        # Stage 7 - one row per department. headcount is the size of the set of
        # distinct emp_codes (an employee appears once per log).
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

        # Stage 8 - departments ascending, case sensitive.
        {"$sort": {"_id": 1}},

        # Stage 9 - rename _id to department, turn the member set into a count.
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
    """Pipeline for GET /analytics/leaderboard/late (runs on attendance_logs).

    * The rank is standard competition ranking (1, 2, 2, 4): $rank in
      $setWindowFields does exactly that ($denseRank would give 1, 2, 2, 3).
    * `limit` caps the RANK, so it is a $match on `rank` AFTER ranking, never a
      $limit.
    """
    first_day, last_day = month_range(month)

    # The department filter goes INSIDE the $lookup, so ranking happens inside
    # that department only.
    employee_filter = {}
    if department is not None:
        employee_filter["department"] = department
    lookup_pipeline = []
    if employee_filter:
        lookup_pipeline.append({"$match": employee_filter})

    return [
        # Stage 1 - the month's records (range on the date index).
        {"$match": {"date": {"$gte": first_day, "$lte": last_day}}},

        # Stage 2 - one row per employee.
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "late_count": {"$sum": {"$cond": [
                {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
        }},

        # Stage 3 - only employees who were actually late.
        {"$match": {"total_late_minutes": {"$gt": 0}}},

        # Stage 4 - name and department. `_id` is the emp_code, which has a
        # unique index, so this is an indexed lookup.
        {"$lookup": {
            "from": "employees",
            "localField": "_id",
            "foreignField": "emp_code",
            "pipeline": lookup_pipeline,
            "as": "employee",
        }},

        # Stage 5 - one row per employee document. This also DROPS logs of
        # employees that do not exist (empty lookup array, no preserve flag).
        {"$unwind": "$employee"},

        # Stage 6 - the rank. No partition is needed: the department filter
        # already ran in the $lookup, so this one window ranks only that department.
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}},
        }},

        # Stage 7 - limit AFTER ranking: two employees tied at rank 2 both come
        # back for limit=2, so the response can be longer than the limit.
        {"$match": {"rank": {"$lte": limit}}},

        # Stage 8 - the row order the contract asks for. $setWindowFields does
        # not promise an order, so this sort is needed.
        {"$sort": {"total_late_minutes": -1, "_id": 1}},

        # Stage 9 - the six response fields (`_id` is the emp_code).
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
    """Pipeline for GET /analytics/departments/{department}/trend (runs on employees).

    The answer must have exactly one row per calendar day, weekends and empty days
    included. That gap filling is done in the database, not with a Python loop.
    """
    first = date.fromisoformat(date_from)
    last = date.fromisoformat(date_to)

    # The days are built from a list of day numbers ($range + $unwind) instead of
    # $densify, because $densify only keeps the field it fills in. Every row it
    # added would hold just the date, losing the join_dates and emp_codes the
    # later stages need. $unwind keeps every other field.
    day_count = (last - first).days + 1

    # $dateAdd needs a real Date to start from. This Date is only stepped one day
    # at a time and printed back, so UTC midnight is a fine anchor.
    range_start = datetime(first.year, first.month, first.day, tzinfo=UTC)

    return [
        # Stage 1 - the people in this department. The endpoint answers 404
        # before calling this, so there is always at least one. Starting from
        # `employees` means the day series does not depend on any logs existing.
        {"$match": {"department": department}},

        # Stage 2 - join dates as real Dates (onError/onNull: no 500 on odd data).
        {"$addFields": {"joined_on_date": {"$dateFromString": {
            "dateString": "$joined_on", "onError": None, "onNull": None}}}},

        # Stage 3 - ONE document holding every join date and every emp_code.
        {"$group": {
            "_id": None,
            "join_dates": {"$push": "$joined_on_date"},
            "emp_codes": {"$push": "$emp_code"},
        }},

        # Stage 4 - one offset per calendar day: 0, 1, 2, ... This is the gap
        # filling; it never looks at attendance records, so it cannot come up empty.
        {"$project": {
            "_id": 0,
            "day_offset": {"$range": [0, day_count]},
            "join_dates": 1,
            "emp_codes": 1,
        }},

        # Stage 5 - one document per day, keeping every other field.
        {"$unwind": "$day_offset"},

        # Stage 6 - the real Date for this offset...
        {"$addFields": {"day": {"$dateAdd": {
            "startDate": range_start, "unit": "day", "amount": "$day_offset"}}}},
        # ...and the same day as the "YYYY-MM-DD" string attendance_logs.date holds
        # (separate $addFields: it reads the field added just above).
        {"$addFields": {"date": {"$dateToString": {
            "date": "$day", "format": "%Y-%m-%d", "timezone": "UTC"}}}},

        # Stage 7 - weekday number, so stage 8 can test Mon-Fri.
        {"$addFields": {"weekday": {"$dayOfWeek": {"$dateFromString": {
            "dateString": "$date", "onError": None, "onNull": None}}}}},

        # Stage 8 - Mon-Fri flag, and this day's headcount (R9: employees who had
        # joined by this day).
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

        # Stage 9 - this day's records for this department. The day is compared
        # inside $expr, because $$variables are only substituted in expression
        # contexts (a plain {"date": "$$day_text"} would match nothing). The date
        # index is still used.
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"day_text": "$date", "codes": "$emp_codes"},
            "pipeline": [
                {"$match": {"$expr": {"$eq": ["$date", "$$day_text"]}}},
                {"$match": {"$expr": {"$in": ["$emp_code", "$$codes"]}}},
                # Score each log here. No weekday test: a presence record counts
                # on any day (unlike present_days in the monthly summary).
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

        # Stage 10 - one row per log; preserve keeps a day with no records.
        {"$unwind": {"path": "$day_logs", "preserveNullAndEmptyArrays": True}},

        # Stage 11 - back to one row per day. $first carries the values that are
        # the same for every log of a day; $ifNull gives zeros for an empty day.
        {"$group": {
            "_id": "$day",
            "date": {"$first": "$date"},
            "is_working_day": {"$first": "$is_working_day"},
            "headcount": {"$first": "$headcount"},
            "present_count": {"$sum": {"$ifNull": ["$day_logs.present_credit", 0]}},
            "late_count": {"$sum": {"$ifNull": ["$day_logs.late_flag", 0]}},
        }},

        # Stage 12 - present_count / headcount, only on a working day with people
        # employed (weekend: null; working day without records: 0.0). Left
        # unrounded so the moving average uses exact rates; Python rounds once.
        {"$addFields": {"attendance_rate_raw": {"$cond": [
            {"$and": [
                {"$eq": ["$is_working_day", True]},
                {"$gt": ["$headcount", 0]},
            ]},
            {"$divide": ["$present_count", "$headcount"]},
            None,
        ]}}},

        # Stage 13 - 7 day moving average: this row plus the six before it, only
        # inside the requested range. $avg ignores nulls (a weekend does not
        # shorten the window); an all-null window gives null.
        {"$setWindowFields": {
            "sortBy": {"_id": 1},
            "output": {"moving_avg_raw": {
                "$avg": "$attendance_rate_raw",
                "window": {"documents": [-6, 0]},
            }},
        }},

        # Stage 14 - chronological order ($setWindowFields does not promise one).
        {"$sort": {"_id": 1}},

        # Stage 15 - raw numbers back to Python, which rounds in one place.
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
