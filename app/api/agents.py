"""
FastAPI router: Agents
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.api.schemas import AgentCreate, AgentResponse, AgentStatusUpdate
from app.database import get_db
from app.models.agent import Agent, AgentStatus, AGENT_VALID_TRANSITIONS

router = APIRouter(prefix="/agents", tags=["agents"])


@router.post("", response_model=AgentResponse, status_code=status.HTTP_201_CREATED)
def create_agent(payload: AgentCreate, db: Session = Depends(get_db)):
    agent = Agent(
        id=str(uuid.uuid4()),
        name=payload.name,
        campaign_id=payload.campaign_id,
        status=AgentStatus.OFFLINE,
    )
    db.add(agent)
    db.commit()
    db.refresh(agent)
    return agent


@router.get("", response_model=List[AgentResponse])
def list_agents(campaign_id: str = None, db: Session = Depends(get_db)):
    q = select(Agent)
    if campaign_id:
        q = q.where(Agent.campaign_id == campaign_id)
    return db.execute(q).scalars().all()


@router.get("/{agent_id}", response_model=AgentResponse)
def get_agent(agent_id: str, db: Session = Depends(get_db)):
    agent = db.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent


@router.post("/{agent_id}/availability", response_model=AgentResponse)
def set_agent_availability(
    agent_id: str,
    payload: AgentStatusUpdate,
    db: Session = Depends(get_db),
):
    agent = db.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    try:
        new_status = AgentStatus(payload.status)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid status '{payload.status}'. Valid values: {[s.value for s in AgentStatus]}",
        )

    allowed = AGENT_VALID_TRANSITIONS.get(agent.status, set())
    if new_status not in allowed:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot transition agent from {agent.status.value} to {new_status.value}. "
                   f"Allowed: {[s.value for s in allowed]}",
        )

    agent.status = new_status
    agent.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(agent)
    return agent
