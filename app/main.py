"""
SmartDialer — FastAPI application entry point.
"""

from __future__ import annotations

import logging

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.database import init_db
from app.api import agents, campaigns, dialing, events, health, simulation
from app.providers import register_event_handler
from app.services.event_processor import EventProcessor
from app.database import SessionLocal

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()

    # Register a global provider event handler that processes events
    # asynchronously (from provider simulations) through the EventProcessor.
    async def handle_provider_event(event):
        db = SessionLocal()
        try:
            processor = EventProcessor(db)
            processor.process(event)
        finally:
            db.close()

    register_event_handler(handle_provider_event)
    yield


app = FastAPI(
    title="SmartDialer",
    description=(
        "Progressive and predictive auto-dialer with safety controls, "
        "atomic reservations, and idempotent event processing."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# Include routers
app.include_router(agents.router)
app.include_router(campaigns.router)
app.include_router(dialing.router)
app.include_router(events.router)
app.include_router(health.router)
app.include_router(simulation.router)


@app.get("/")
def root():
    return {
        "service": "SmartDialer",
        "version": "1.0.0",
        "docs": "/docs",
    }
