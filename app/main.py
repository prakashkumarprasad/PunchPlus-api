"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

Layout (each file has one job):
  config.py       settings and the business-rule constants
  database.py     MongoDB connection and indexes
  timeutils.py    time, date and rounding helpers
  rules.py        business rules R1-R5 and the PATCH helpers
  serializers.py  documents to API JSON
  schemas.py      request bodies (Pydantic)
  queries.py      the attendance filter and the four aggregation pipelines
  routers/        the endpoints, one file per area
  main.py         builds the app and plugs everything in
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pymongo.errors import PyMongoError

from app.database import client, ensure_indexes
from app.routers import admin, analytics, attendance, employees, system


@asynccontextmanager
async def lifespan(app_instance: FastAPI):
    """Create the indexes at start-up. Safe to run again."""
    try:
        ensure_indexes()
    except PyMongoError:
        # MongoDB is not up yet, so we still start. GET /health then answers 503
        # as the contract requires, and it calls ensure_indexes() again on every
        # call, so the indexes appear as soon as MongoDB is reachable.
        pass

    yield

    # Shut the connection pool down cleanly when the app stops.
    client.close()


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)

app.include_router(system.router)
app.include_router(employees.router)
app.include_router(attendance.router)
app.include_router(analytics.router)
app.include_router(admin.router)
