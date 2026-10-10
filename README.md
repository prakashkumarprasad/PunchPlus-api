# Employee Attendance & Analytics API

A FastAPI + MongoDB service for punch-in / punch-out, manual corrections with an audit
trail, and four attendance analytics. Everything is in `app/main.py`.

## Run it

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows; on macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

set MONGO_URI=mongodb://localhost:27017
set MONGO_DB=attendance_dev
uvicorn app.main:app --port 8000
```

`MONGO_URI` and `MONGO_DB` are read from the environment; a local `.env` file is used if it
exists, but it never overrides a real environment variable. MongoDB **6.0 or newer**
(tested on 7.0.43). Swagger UI is at `http://localhost:8000/docs`; `GET /health` returns
503 when MongoDB is down. All six indexes are created at start-up, so there is no manual
step.

## The endpoints

| | |
|---|---|
| `GET /health` | 200 only when MongoDB answers a ping |
| `POST /employees`, `GET /employees` | create and list, filtered by department |
| `POST /attendance/punch-in`, `POST /attendance/punch-out` | the day's record, R1-R5 applied |
| `PATCH /attendance/{emp_code}/{date}` | manual correction, with a history entry |
| `GET /attendance` | a filtered, sorted, paged list |
| `GET /analytics/employees/{emp_code}/monthly` | one employee's month |
| `GET /analytics/departments/summary` | one row per department |
| `GET /analytics/leaderboard/late` | late-comers ranking, ties share a rank |
| `GET /analytics/departments/{department}/trend` | one row per calendar day, gaps filled |
| `GET /admin/explain/{endpoint}` | the raw MongoDB plan for any of the five main queries |

## Choices I made where the contract was open

1. **Punch-out on an already-closed record answers 409** before the 422 time checks,
   because once the day is closed the time rules have nothing to check against.
2. **The trend range may be 92 rows inclusive**; 93 or more is a 422. "Exceeds 92 days"
   reads as "more than 92 is too much".
3. **`moving_avg_7d` averages the unrounded daily rates** and rounds once at the end, so
   the answer cannot depend on the rounding of the individual days.
4. **A PATCH that changes nothing is a 422**, decided after truncating to whole seconds.
5. **`working_days` is counted with a small loop over at most 31 calendar days**; all
   document work is inside aggregation pipelines.

Also: `GET /analytics/departments/summary` has no 404 and returns 200 with an empty list
for an unknown department, because the contract lists only 200 and 422 for it - while the
trend endpoint does 404. Following the contract literally in each place beat being
consistent.

## What is missing

- **No authentication or authorisation.** Anyone who can reach the port can create
  employees and rewrite records.
- **No holiday calendar.** Working days are Monday to Friday only, so a public holiday is
  counted as a working day.
- **A synchronous driver.** Endpoints are plain `def` functions with PyMongo, so a slow
  query occupies a worker thread. Fine at this size; `Motor` would be the change at 100x.
- **`late_minutes` for an early-morning punch-in on an overnight shift is the literal
  reading of R2**, which gives 450 minutes rather than 0. It is recorded in `REVIEW.md` and
  `DECISIONS.md` rather than silently "fixed".

## More detail

`REVIEW.md` lists every defect found in the starter with a failing example for each.
`DECISIONS.md` answers the five design questions.
