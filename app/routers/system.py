"""System endpoint: GET /health."""
from fastapi import APIRouter, HTTPException
from pymongo.errors import PyMongoError

from app.database import client, ensure_indexes

router = APIRouter(tags=["system"])


@router.get("/health")
def health():
    """200 only when MongoDB answers a ping, otherwise 503."""
    try:
        client.admin.command("ping")
        # If the app started while MongoDB was down, the indexes were never
        # created, and two identical punch-ins would both succeed instead of one
        # getting a 409. Creating them here lets the app repair itself on the next
        # health check. A failure here is caught below, so it never becomes a 500.
        ensure_indexes()
    except PyMongoError:
        raise HTTPException(503, "MongoDB is not reachable")
    return {"status": "ok"}
