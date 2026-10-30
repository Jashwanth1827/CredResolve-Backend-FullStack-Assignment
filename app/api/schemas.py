"""
Pydantic schemas for API request/response models.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional, List, Any, Dict

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Agent schemas
# ---------------------------------------------------------------------------

class AgentCreate(BaseModel):
    name: str
    campaign_id: Optional[str] = None


class AgentStatusUpdate(BaseModel):
    status: str  # Target status string


class AgentResponse(BaseModel):
    id: str
    name: str
    status: str
    campaign_id: Optional[str]
    current_call_id: Optional[str]
    lease_expires_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Campaign schemas
# ---------------------------------------------------------------------------

class CampaignCreate(BaseModel):
    name: str
    max_concurrent_calls: int = 10
    provider_name: str = "provider_a"
    seed_answer_rate: float = Field(default=0.3, ge=0.0, le=1.0)


class CampaignResponse(BaseModel):
    id: str
    name: str
    status: str
    max_concurrent_calls: int
    provider_name: str
    total_calls_initiated: int
    total_calls_answered: int
    total_talk_seconds: int
    estimated_answer_rate: float
    estimated_avg_talk_seconds: float
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Borrower schemas
# ---------------------------------------------------------------------------

class BorrowerCreate(BaseModel):
    name: str
    phone_number: str
    max_retries: int = 3


class BorrowerResponse(BaseModel):
    id: str
    campaign_id: str
    name: str
    phone_number: str
    status: str
    retry_count: int
    max_retries: int
    last_attempted_at: Optional[datetime]
    created_at: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Call schemas
# ---------------------------------------------------------------------------

class CallResponse(BaseModel):
    id: str
    campaign_id: str
    borrower_id: str
    agent_id: Optional[str]
    status: str
    provider_name: Optional[str]
    provider_call_id: Optional[str]
    initiated_at: Optional[datetime]
    answered_at: Optional[datetime]
    completed_at: Optional[datetime]
    talk_seconds: Optional[int]
    failure_reason: Optional[str]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Dialing schemas
# ---------------------------------------------------------------------------

class DialRequest(BaseModel):
    pass  # campaign_id comes from path parameter


class DialResponse(BaseModel):
    calls_initiated: int
    calls_attempted: int
    calls_failed: int
    pacing_explanation: Optional[str]
    safety_decision: Optional[str]
    errors: List[str]


# ---------------------------------------------------------------------------
# Provider event schemas
# ---------------------------------------------------------------------------

class ProviderEventRequest(BaseModel):
    event_id: str
    provider_name: str
    provider_call_id: str
    event_type: str
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ProviderEventResponse(BaseModel):
    processed: bool
    message: str


# ---------------------------------------------------------------------------
# Simulation schemas
# ---------------------------------------------------------------------------

class SimulationScenario(BaseModel):
    name: str = "Scenario A"
    answer_rate: float = Field(default=0.20, ge=0.0, le=1.0)
    avg_talk_seconds: int = Field(default=120, ge=1)
    num_agents: int = Field(default=5, ge=1, le=50)
    num_borrowers: int = Field(default=20, ge=1, le=200)
    provider_name: str = "provider_a"
    simulate_provider_outage: bool = False
    outage_after_calls: Optional[int] = None
    random_seed: Optional[int] = None
    mode: str = "progressive"  # "progressive" | "predictive"


class SimulationResult(BaseModel):
    scenario_name: str
    total_agents: int
    calls_initiated: int
    calls_answered: int
    calls_connected: int
    calls_completed: int
    calls_failed: int
    agent_utilization_pct: float
    pacing_decisions: List[str]
    safety_decisions: List[str]
    summary: str


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

class HealthResponse(BaseModel):
    status: str
    providers: Dict[str, bool]
    db: str
