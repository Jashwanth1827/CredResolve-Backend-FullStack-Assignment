"""
Tests: FastAPI endpoints (integration tests via TestClient).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


def test_health_endpoint(client: TestClient):
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "providers" in data
    assert "provider_a" in data["providers"]
    assert "provider_b" in data["providers"]


def test_create_and_list_agents(client: TestClient):
    # Create agent
    resp = client.post("/agents", json={"name": "Alice"})
    assert resp.status_code == 201
    agent = resp.json()
    assert agent["name"] == "Alice"
    assert agent["status"] == "OFFLINE"

    # List agents
    resp = client.get("/agents")
    assert resp.status_code == 200
    agents = resp.json()
    assert len(agents) == 1


def test_agent_availability_transition(client: TestClient):
    resp = client.post("/agents", json={"name": "Bob"})
    agent_id = resp.json()["id"]

    # OFFLINE → AVAILABLE
    resp = client.post(f"/agents/{agent_id}/availability", json={"status": "AVAILABLE"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "AVAILABLE"

    # AVAILABLE → PAUSED
    resp = client.post(f"/agents/{agent_id}/availability", json={"status": "PAUSED"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "PAUSED"


def test_invalid_agent_transition_rejected(client: TestClient):
    resp = client.post("/agents", json={"name": "Charlie"})
    agent_id = resp.json()["id"]

    # OFFLINE → CONNECTED is not valid
    resp = client.post(f"/agents/{agent_id}/availability", json={"status": "CONNECTED"})
    assert resp.status_code == 409


def test_create_and_list_campaigns(client: TestClient):
    resp = client.post("/campaigns", json={"name": "Test Campaign"})
    assert resp.status_code == 201
    campaign = resp.json()
    assert campaign["name"] == "Test Campaign"
    assert campaign["status"] == "DRAFT"

    resp = client.get("/campaigns")
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_add_and_list_borrowers(client: TestClient):
    resp = client.post("/campaigns", json={"name": "Campaign 1"})
    campaign_id = resp.json()["id"]

    resp = client.post(
        f"/campaigns/{campaign_id}/borrowers",
        json={"name": "John", "phone_number": "+15551234567"},
    )
    assert resp.status_code == 201
    borrower = resp.json()
    assert borrower["name"] == "John"

    resp = client.get(f"/campaigns/{campaign_id}/borrowers")
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_progressive_dial_no_agents(client: TestClient):
    """Dialing a campaign with no agents returns 0 initiated calls."""
    resp = client.post("/campaigns", json={"name": "Empty Campaign"})
    campaign_id = resp.json()["id"]
    client.post(f"/campaigns/{campaign_id}/activate")
    client.post(
        f"/campaigns/{campaign_id}/borrowers",
        json={"name": "Borrower 1", "phone_number": "+15559999999"},
    )

    resp = client.post(f"/campaigns/{campaign_id}/dial")
    assert resp.status_code == 200
    data = resp.json()
    assert data["calls_initiated"] == 0


def test_provider_event_idempotency_via_api(client: TestClient):
    """Duplicate provider events via POST /provider/events are handled correctly."""
    event_payload = {
        "event_id": "api-evt-001",
        "provider_name": "provider_a",
        "provider_call_id": "pA-nonexistent-call",
        "event_type": "RINGING",
        "metadata": {},
    }

    resp1 = client.post("/provider/events", json=event_payload)
    assert resp1.status_code == 200
    assert resp1.json()["processed"] is True

    # Duplicate
    resp2 = client.post("/provider/events", json=event_payload)
    assert resp2.status_code == 200
    assert resp2.json()["processed"] is False
    assert "duplicate" in resp2.json()["message"]


def test_provider_event_unknown_type(client: TestClient):
    resp = client.post("/provider/events", json={
        "event_id": "bad-type",
        "provider_name": "provider_a",
        "provider_call_id": "pA-xyz",
        "event_type": "UNKNOWN_EVENT",
        "metadata": {},
    })
    assert resp.status_code == 200
    assert resp.json()["processed"] is False
