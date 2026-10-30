"""
FastAPI router: Health check
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.schemas import HealthResponse
from app.database import get_db
from app.providers import get_all_providers

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
def health_check(db: Session = Depends(get_db)):
    # DB check
    try:
        db.execute(text("SELECT 1"))
        db_status = "ok"
    except Exception as e:
        db_status = f"error: {e}"

    # Provider health
    providers = {name: p.is_healthy for name, p in get_all_providers().items()}

    return HealthResponse(
        status="ok" if db_status == "ok" else "degraded",
        providers=providers,
        db=db_status,
    )
