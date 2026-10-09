# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints). For each one:

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | | | | |

Also note anything you looked at and decided was **not** a defect, and why.

---

## The starter's defects, and what I changed (stage 1 audit + stage 2 fix)

Line numbers in the "where" column refer to the **original starter**; the fix column gives
the function in the **current** `app/main.py`. Everything in the evidence column is real
output captured by `dev_tools/audit_starter.py` before the fix. After stage 2 the same
script reports **0 defects** out of 68 probes.

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes`, line 35 | Built the shift start by overwriting the hour on the punch-in's **UTC** datetime, so it read the UTC wall clock and called it IST. On a UTC host every day-shift punch-in looked hours early. | Punch in 10:05 IST, shift 09:30. With `TZ=UTC` got `late_minutes=0`, want 35. Stored punch came back `2026-07-20T04:35:00` instead of `10:05`. | Rewrote as `attendance_date_for` (line 225) + `compute_late_minutes` (line 238). Both take the attendance **date** and convert to IST with `astimezone(IST)`, so the host's zone is never consulted. Verified: same input with `TZ=UTC` now gives 35. |
| 2 | `compute_late_minutes`, lines 36-37 | Floored the elapsed minutes **before** comparing with the grace period, so the boundary second was lost. | Direct calls: `09:40:01` -> 0 (want 10); `10:00:30` -> 0 (want 30); `10:15:59` -> 0 (want 45); shift 22:00 with `22:11:00` -> 0 (want 11). | `compute_late_minutes` (line 238) compares **instants** first (`punch_in <= start + GRACE_SECONDS`, lines 241-242), and only then counts `seconds // 60` (line 245). Late minutes are still measured from shift start, not from the end of the grace. |
| 3 | `compute_work_hours`, line 41 | Used Python `round()`, which is half-to-even and works on binary floats. R4 wants half-up. | 9630 s (2.675 h exactly) -> `2.67`, want `2.68`. 450 s (0.125 h exactly) -> `0.12`, want `0.13`. | `compute_work_hours` (line 262) divides with `Decimal(seconds) / Decimal(3600)` and rounds through the new `to_decimal_round` (line 176) using `ROUND_HALF_UP`. |
| 4 | `compute_overtime`, line 47 | No 30-minute minimum, so any overtime counted. | Out 18:59:59 with shift end 18:30 -> `overtime_minutes=29`, want 0. | `compute_overtime_minutes` (line 248) computes `minutes = seconds // 60` then returns 0 unless `minutes >= MIN_OVERTIME_MINUTES` (lines 256-259). |
| 5 | `compute_overtime`, line 46 | Never handled an overnight shift; it built the shift end on the attendance date instead of the next day. | Shift 22:00-06:00 starting 2026-07-20, out 06:40 IST on 07-21. The starter reported 1150 minutes; the rule says 40. | `shift_end_datetime` (line 214) adds one day when `is_overnight_shift` (line 202) is true. `compute_overtime_minutes` uses it. Verified: 40. |
| 6 | `MongoClient(...)`, line 22 | No `tz_aware=True`, so PyMongo returned **naive** datetimes and the helper subtracted an aware one from a naive one. | `compute_overtime(datetime(2026,7,20,13,5), "18:30", "2026-07-20")` -> `TypeError: can't subtract offset-naive and offset-aware datetimes`, i.e. a 500 on any real punch-out. | `MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=...)` (lines 104-108). Every datetime from MongoDB is now aware and UTC, so the subtraction is valid. `dt_to_ms` (line 167) also treats a naive value as UTC rather than crashing. |
| 7 | `MongoClient(...)`, line 22 | No `serverSelectionTimeoutMS`, so the driver's 30-second default applied. | With MongoDB down: `GET /health` -> 200 in 0.2 s, `GET /employees` -> **500 in 30.5 s**. | `SERVER_SELECTION_TIMEOUT_MS = 3000` (line 44), passed on line 107. The same request now fails in about 3 s. |
| 8 | whole file | **No indexes at all**, and nothing created them at start-up. Every read was a collection scan and the database would not stop a duplicate. | `index_information()` showed `['_id_']` only. Two `insert_one` calls with the same `emp_code` both succeeded -> 2 documents. Two with the same `(emp_code, date)` both succeeded -> 2 documents. | `lifespan` (line 113) creates five indexes idempotently: unique `emp_code`, `(department, emp_code)`, `joined_on`, unique `(emp_code, date)`, `(date, emp_code)`. Both duplicate inserts now raise `DuplicateKeyError`. |
| 9 | `GET /health`, lines 72-74 | Returned a hard-coded `{"status": "ok"}` and never touched MongoDB. | Started a second instance with `MONGO_URI=mongodb://127.0.0.1:29999` (nothing listening) -> **200** `{"status":"ok"}`. Should be 503. | `health` (line 422) runs `client.admin.command("ping")` and converts any `PyMongoError` into `HTTPException(503, ...)`. Verified: 503. |
| 10 | `POST /employees`, line 82 | `created_at` was `datetime.now()` - naive **and** server-local - and was returned as an ISO string. | `POST /employees` with a valid body -> `created_at="2026-10-09T22:59:06.135889"`. The contract wants an integer. | `datetime.now(UTC)` (line 437), converted on the way out by `employee_to_api` (line 323) with `dt_to_ms`. Verified: `created_at` is an int. |
| 11 | `POST /employees`, lines 79-83 | Check-then-insert race: `find_one` then `insert_one`, two round trips apart. | 20 threads posting the same `emp_code` gave `[4, 3, 1]` x 201 over three runs. Run 1 stored **4** employees with that code. | Deleted the pre-check entirely. `create_employee` (line 434) just inserts and maps `DuplicateKeyError` to 409 (lines 441-444). Verified: `[1, 1, 1]` x 201 over five runs, one row stored. |
| 12 | `EmployeeIn`, lines 53-60 | No validation at all on the request body. | All of these returned 201 instead of 422: `emp_code="NOPE"`, `emp_code="EMP1"`, `email="not-an-email"`, `name=""`, `name` 500 chars, `shift_start="99:99"`, `shift_start="9:30"`, `shift_start==shift_end`, `joined_on="2026-02-30"`, `joined_on="tomorrow"`. | `EmployeeCreate` (line 370) declares the contract's `Field` constraints, a `@field_validator` for `joined_on` that calls `is_valid_date_string`, and a `@model_validator` for `shift_start != shift_end`. All 11 now return 422. |
| 13 | `GET /employees`, line 93 | `skip = page * page_size`, so page 1 skipped the first page of employees. | `GET /employees?page=1&page_size=5` returned `['EMP0006'...'EMP0010']`. Should return `EMP0001..EMP0005`. | `skip = (page - 1) * page_size` (line 461). Verified: page 1 starts at `EMP0001` and page 2 continues at `EMP0006` with no overlap. |
| 14 | `GET /employees`, line 94 | `total` counted the whole collection, ignoring the department filter. | `GET /employees?department=Engineering` reported `total=14` with 14 employees present. Should be 4. | `count_documents(query)` with the same filter dict (line 463). Verified: 4, and `?department=engineering` gives 0 because matching is case sensitive. |
| 15 | `GET /employees`, line 95 | No sort at all, so results came back in natural order. | Created `EMP0000` **last** (it sorts before `EMP0001`); it came back at position **14 of 15**. | `.sort("emp_code", 1)` (line 465), served by the `(department, emp_code)` index. Verified: `EMP0000` comes back at position 0. |
| 16 | `GET /employees`, lines 89 and 93 | No paging validation; a negative page produced a negative `skip` that MongoDB rejected. | `?page=-1` -> **500**. `?page=0` -> 200. `?page_size=1000` -> 200. All three should be 422. | `page: int = Query(DEFAULT_PAGE, ge=1)` and `page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE)` (lines 452-453). Verified: all four, plus `page_size=101`, `page_size=0` and `page=abc`, return 422. |
| 17 | `POST /attendance/punch-in`, lines 101 and 113 | Looked the employee up and then read `emp["shift_start"]` without a `None` check. | `POST /attendance/punch-in {"emp_code": "EMP_DOES_NOT_EXIST"}` -> **500**. Should be 404. | `if employee is None: raise HTTPException(404, ...)` (lines 479-480). |
| 18 | `POST /attendance/punch-in`, lines 102-103 | `datetime.fromtimestamp()` and `datetime.now()` were called with no time zone, so the attendance date was the **host's** local date. | With `TZ=UTC`, a punch-in at 02:00 IST on 2026-07-25 was stored as `date="2026-07-24"`. R1 says `2026-07-25`. | `ms_to_dt` (line 162) builds an aware UTC instant, and `attendance_date_for` (line 225) converts it with `astimezone(IST)` before taking the date. Verified: `TZ=UTC` now stores `2026-07-25`. |
| 19 | `POST /attendance/punch-in`, line 103 | No overnight rule; the code never looked at the shift. | `EMP0005` (22:00-06:00) punching in at 05:30 IST on 2026-07-25 stored `date="2026-07-25"`. R1 says the shift started the previous day: `2026-07-24`. | `attendance_date_for` (line 225) checks `is_overnight_shift` and steps back a day when the IST time is strictly earlier than `shift_end`. Verified: `2026-07-24`. |
| 20 | `POST /attendance/punch-in`, line 102 | `if body.punched_at:` treated `0` as "not supplied", so `punched_at: 0` punched the employee in **now**. | `{"emp_code":"EMP0001","punched_at":0}` -> 201, `date="2026-10-09"` (today). Should be 422. | `if body.punched_at is None:` (line 484), plus `ge=MIN_EPOCH_MS` on the model (line 409) so `0` is rejected before the endpoint runs. |
| 21 | `PunchInIn`, lines 63-67 | `punched_at: Optional[int]` was not strict and had no range check, so Pydantic coerced strings, bools and whole floats. | `"1784941380000"` accepted; `1784941380` (seconds) stored `1970-01-21`; `true` stored `1970-01-01T05:30:00.001000`; `1784941380000.0` accepted; `-1` -> **500**; `99999999999999` -> **500**. All should be 422. | `punched_at: Optional[StrictInt] = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)` (lines 408-410). `StrictInt` is what rejects `true`, because `bool` subclasses `int`. All seven now 422. |
| 22 | `PunchInIn`, line 66 | `status: str` accepted anything, including `ABSENT` and `LEAVE`, which only seeded data or a correction may create. | `status="ABSENT"` -> 201 with `status='ABSENT'` and a punch_in. `status="GARBAGE"` -> 201 stored as `'GARBAGE'`. Both should be 422. | `status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"` (line 412). |
| 23 | `POST /attendance/punch-in`, lines 104-105 | Same check-then-insert race as row 11. | 20 threads gave `1 x 201` on all three runs, so it did not trip in that window. But row 8 shows the database had no unique `(emp_code, date)` index, so if it did trip, two rows for the same day would be stored. | Removed the pre-check. `punch_in` (line 476) inserts and maps `DuplicateKeyError` to 409 (lines 507-510), backed by the unique index from row 8. Verified: `[1, 1, 1]` x 201 over five trials, one row stored. |
| 24 | `POST /attendance/punch-in`, lines 106-121 | Instants were not truncated to whole seconds, and came back as naive ISO strings instead of epoch milliseconds. | `punched_at=1784692680900` (09:28:00.900) kept its microseconds. A normal punch returned `punch_in="2026-07-20T10:05:00"`, a string. | `truncate_seconds` (line 157) is applied on line 490 before any calculation or storage, and `record_to_api` (line 302) converts with `dt_to_ms`. Verified: `punch_in=1784692680000`, and `punch_in % 1000 == 0`. |
| 25 | `POST /attendance/punch-in`, line 119 | Added `id` to the response, which is the MongoDB `_id`. | Response keys were `['date','emp_code','half_day','history','id','late_minutes','overtime_minutes','punch_in','punch_out','status','work_hours']`. | `record_to_api` (line 302) builds a brand new dict listing exactly the ten contract fields, so `_id` has no path into the response. No `pop("_id")` anywhere. Verified: no `id`, no `_id`. |
| 26 | `GET /attendance`, lines 144-147 | Loaded **every** matching document into memory and sorted in Python, then sliced. | `list(db.attendance_logs.find(q))` then a Python `sort` and slice. On 100,000 records that reads all of them to show 20. | `build_attendance_query` (line 338) returns filter, sort, skip and limit; `list_attendance` (line 516) passes them to `find().sort().skip().limit()` (line 540), so only one page reaches the process. `count_documents` gives `total`. |
| 27 | `GET /attendance`, line 145 | Sorted only by `date`, with no `emp_code` tie-break. | Punched `EMP0000` in on 2026-07-01 (inserted last, sorts first). It came back **last**: `['EMP0001',...,'EMP0000']`. | `sort = [("date", -1), ("emp_code", 1)]` (line 361), served by the `(date, emp_code)` index. Verified: `EMP0000` comes back first. |
| 28 | `GET /attendance`, lines 133-143 | No validation of the query parameters. | All 200 instead of 422: `?date_from=garbage`, `?date_from=2026-07-10&date_to=2026-07-01`, `?status=GARBAGE`, `?page_size=1000`, `?page=-1`. | `is_valid_date_string` checks on lines 526-529, a backwards-range check on lines 531-532, `status` as an `Optional[Literal[...]]` (line 520), and `Query` bounds on lines 521-522. |
| 29 | `GET /attendance`, line 149 | Leaked `_id` as `id`, same as row 25. | `GET /attendance?page=1&page_size=100` -> **31 of 31** items carried an `id` key, e.g. `6ac9212ec7ce844226c53081`. | Same `record_to_api` (line 302), plus `{"_id": 0}` in the projection on line 540 as a second barrier. |
| 30 | `GET /attendance`, line 144 | Legacy documents missing `history` and `half_day` came back incomplete. | `?emp_code=EMP0001&date_from=2026-07-06&date_to=2026-07-06` returned keys with no `history` and no `half_day`. | `record_to_api` fills the defaults: `history` -> `[]` (line 319), `half_day` -> `False` (line 318), `late_minutes`/`overtime_minutes` -> 0 (lines 316-317). Verified: all ten fields present. |
| 31 | `GET /attendance`, line 144 | Datetimes returned as ISO strings instead of epoch milliseconds, same as row 24. | `?emp_code=EMP0001` gave `punch_in` values like `['2026-07-07T03:58:00', ...]`. | Same `record_to_api` conversion. Verified: integers. |

### A defect I introduced myself, and how I found it

Creating the indexes in `lifespan` introduced a **new** bug. With `try/except` absent,
MongoDB being down made the lifespan raise `ServerSelectionTimeoutError`, uvicorn aborted,
and `/health` never answered at all. The contract requires **503**, and a connection error
is not a 503 - a grader polling `/health` would see nothing.

The audit harness caught it: `app on port 8124 never came up`.

Fixed by wrapping index creation in `except PyMongoError: pass` (lines 115-138), with a
comment saying why. The app now starts regardless and `/health` answers 503.

### Things I looked at and decided were **not** a defect

| Thing | Where (starter) | Why it is fine |
|---|---|---|
| `load_dotenv()` | line 20 | By default it does **not** overwrite a variable already in the environment. That is exactly what the contract wants: the grader's real `MONGO_URI` wins over my local `.env`. The tempting "fix" is `load_dotenv(override=True)`, which would break the contract. Verified: `MONGO_URI=... python app` uses the shell value. |
| Unknown fields in a request body ignored | `EmployeeIn`, `PunchInIn` | The starter already dropped them, so a client could not set a computed value. That matches the contract. I made it explicit with `extra="ignore"` so it is a decision, not an accident, and added a test. |
| Duplicate `emp_code` returned 409 | lines 79-80 | The status code and message were right. The **way** it detected the duplicate was the defect (row 11), not the response. |
| `/health` returned `{"status": "ok"}` when MongoDB was up | lines 72-74 | Correct shape and code. Only the missing ping was wrong (row 9). |
| `overtime_minutes` for a large overrun | line 47 | Out at 19:00:00 gave 30 and at 19:10:00 gave 40. The arithmetic worked; only the missing 30-minute floor (row 4) and the overnight shift (row 5) were wrong. |
| `round(4.495, 2)` returning 4.5 | line 41 | Looks correct, but only by luck of the binary representation: the same line gives 2.67 where the rule wants 2.68. Worth knowing so nobody says "round is fine here". |
| `punched_at` with a fractional millisecond | `PunchInIn` | `1783312500000.5` was already rejected with 422 (`int_from_float`). One narrow slice of validation worked; the rest did not (row 21). |
| Default `MONGO_URI` / `MONGO_DB` fallbacks | lines 22-23 | Falling back to `mongodb://localhost:27017` for local work is acceptable. Not a hard-coded secret, and real environment variables win. Kept, with a comment saying it is local-only. |
| `page=0` on `GET /employees` returned the right rows | line 93 | It returned the first page by accident, because `0 * page_size` is 0. Still wrong: the contract says `page` starts at 1, so `0` must be 422 (row 16) - and "fixing" the formula to `(page - 1) * page_size` without the validation would turn it into a 500. |

## How I verified the fixes

All of these run against the real MongoDB 7.0.43 and are re-runnable.

| Script | Result |
|---|---|
| `python dev_tools/test_rules.py` | **102 passed, 0 failed, 0 skipped** (stage 2: 83/0/0) |
| `python dev_tools/smoke_test.py` | **268 passed, 0 failed** (stage 2: 121) |
| `python dev_tools/concurrency_test.py` | **109 passed, 0 failed** (stage 2: 42) |
| `python dev_tools/check_dev_db.py` | **45 passed, 0 failed** |
| `python dev_tools/audit_starter.py` | 68 probes, **0 defects** (was 60) |

The concurrency evidence, 20 threads, five trials each:

```
POST /employees       trial 1..5:  1 x 201, 19 x 409, 0 x 500, 1 row stored
POST /attendance/punch-in  trial 1..5:  1 x 201, 19 x 409, 0 x 500, 1 row stored
POST /attendance/punch-out trial 1..5:  1 x 200, 19 x 409, 0 x 500
PATCH, same correction trial 1..5:  1 x 200, 0 x 409, 19 x 422, 0 x 500, history length 1
PATCH, 20 corrections:  15 x 200, 5 x 409, 0 x 500, history length 15
```

The last line is the one that matters for "concurrent regularizations must not silently
lose each other's history": **15 winners, 15 audit entries, nothing lost.**

and the guarantee underneath it:

```
two insert_one calls with the same emp_code       -> second raises DuplicateKeyError
two insert_one calls with the same (emp_code, date) -> second raises DuplicateKeyError
```

### Still to do, not yet fixed

- The four analytics endpoints and `GET /admin/explain/{endpoint}` do not exist yet, so I
  cannot yet prove `IXSCAN` versus `COLLSCAN` on `attendance_big`. Rows 8 and 26 predict
  that every pipeline would COLLSCAN today. **That is a prediction, not a measurement**,
  until Stage 6.

---

## Stage 3: the rows above, applied to code I then wrote

Stage 3 added `POST /attendance/punch-out` and `PATCH /attendance/{emp_code}/{date}`.
Those two endpoints use exactly the helpers the rows above said were broken, so the rows
became a checklist for the new code rather than history. What each one looks like now:

| Row | Defect | Where it would have shown up in the new code | How I know it is not there |
|---|---|---|---|
| 4 | no 30-minute overtime minimum | `compute_hours_fields` calling overtime for a punch-out | `smoke_test.py`: `R3 5 minutes past shift end is under the 30 minute minimum -> 0` |
| 5 | overnight shift never handled | punch-out and PATCH computing overtime for a 22:00-06:00 shift | `smoke_test.py`: `R3 overnight 06:40 against a 06:00 shift end is 40`, and the PATCH correction gives the same 40 |
| 6 | naive and aware datetimes mixed | `punch_out - punch_in` on line 734, and `punch_in + MAX_PUNCH_DURATION` on line 736 | every value comes from `MongoClient(tz_aware=True)`, and `test_rules.py` still shows no `TypeError` |
| 8 | no indexes | picking the record to close would be a collection scan | the filter is an equality on `emp_code` plus a `date` sort, both served by `emp_code_date_unique` |
| 21 | `punched_at` not strict | `PunchOutBody` would have accepted `"1"` | `smoke_test.py`: six `reject punch-out punched_at ...` cases, all 422 |
| 23 | check-then-insert race | punch-out is read-then-update, which is exactly that shape | `concurrency_test.py`: `1 x 200, 19 x 409` over five trials |
| 24 | instants not truncated | `punch_out` would keep its milliseconds and R1's rules would drift | `truncate_seconds` runs at line 707 and inside the PATCH helper at line 450 |
| 25 | `_id` leaks | returning the raw document from either endpoint | both return `record_to_api(...)`, and `smoke_test.py` asserts no `id` and no `_id` |
| 30 | legacy documents come back incomplete | PATCH on a record with no `history` and no `half_day` | `smoke_test.py`: `...and now has exactly one history entry` and `...and the missing half_day was filled in` |

### Two new problems this stage introduced, and how they were caught

1. **`current_state` used `record.get("half_day")` with no default.** A legacy document with
   no `half_day` would then compare `None != False`, so a correction that changed nothing
   would be recorded as a change and the contract's "a request that changes nothing is 422"
   would never fire for those records. Caught by `test_rules.py`'s
   *"a missing half_day reads as false"* case. Fixed by using the same defaults as
   `record_to_api`.
2. **The punch-out lookup did not exclude `ABSENT` records.** In MongoDB `null` sorts below
   every date, so `{punch_in: {"$lte": <a date>}}` also matches a record whose `punch_in` is
   `null` - or has no `punch_in` field at all. The most recent `ABSENT` day would have been
   picked as the record to close, and the code would then have subtracted from `None`:
   **a 500**. Fixed with `{"punch_in": {"$type": "date", "$lte": punch_dt}}` at line 720 and
   proved by `smoke_test.py`'s *"punch-out cannot pick an ABSENT record -> 404"*.