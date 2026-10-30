"""
Tests for Fix 1: Provider behavior honoring configurable scenario parameters.
"""

from __future__ import annotations

import asyncio
import random
import uuid
import pytest

from app.providers import ProviderA, ProviderB, CallInitRequest, ProviderEventType, clear_event_handlers, register_event_handler


@pytest.mark.asyncio
async def test_provider_a_honors_configured_parameters():
    """Provider A honors configured answer_rate and talk duration."""
    rng1 = random.Random(123)
    provider = ProviderA(rng=rng1, answer_rate=1.0, avg_talk_seconds=180)

    assert provider.answer_rate == 1.0
    assert provider.avg_talk_seconds == 180

    received_events = []

    async def on_event(evt):
        received_events.append(evt)

    clear_event_handlers()
    register_event_handler(on_event)

    try:
        req = CallInitRequest(
            call_id=str(uuid.uuid4()),
            campaign_id="c1",
            borrower_id="b1",
            phone_number="+15550001",
        )
        resp = await provider.initiate_call(req)
        assert resp.success

        # Allow async events to fire
        await asyncio.sleep(0.8)

        event_types = [e.event_type for e in received_events]
        assert ProviderEventType.INITIATED in event_types
        assert ProviderEventType.RINGING in event_types
        assert ProviderEventType.ANSWERED in event_types
        assert ProviderEventType.COMPLETED in event_types

        completed_evt = next(e for e in received_events if e.event_type == ProviderEventType.COMPLETED)
        # avg_talk_seconds is 180, so duration should be around 180 (+/- 25%)
        assert 130 <= completed_evt.metadata["duration_seconds"] <= 230
    finally:
        clear_event_handlers()


@pytest.mark.asyncio
async def test_provider_b_honors_configured_zero_answer_rate():
    """Provider B honors 0.0 answer rate (all calls fail)."""
    rng = random.Random(456)
    provider = ProviderB(rng=rng, answer_rate=0.0, avg_talk_seconds=60)

    received_events = []

    async def on_event(evt):
        received_events.append(evt)

    clear_event_handlers()
    register_event_handler(on_event)

    try:
        req = CallInitRequest(
            call_id=str(uuid.uuid4()),
            campaign_id="c1",
            borrower_id="b1",
            phone_number="+15550002",
        )
        resp = await provider.initiate_call(req)
        if resp.success:
            await asyncio.sleep(1.0)
            event_types = [e.event_type for e in received_events]
            assert ProviderEventType.FAILED in event_types
            assert ProviderEventType.ANSWERED not in event_types
    finally:
        clear_event_handlers()


@pytest.mark.asyncio
async def test_provider_dynamic_reconfiguration():
    """Provider configure() method dynamically updates parameters mid-run."""
    provider = ProviderA(answer_rate=0.20, avg_talk_seconds=90)
    assert provider.answer_rate == 0.20
    assert provider.avg_talk_seconds == 90

    provider.configure(answer_rate=0.75, avg_talk_seconds=200)
    assert provider.answer_rate == 0.75
    assert provider.avg_talk_seconds == 200


@pytest.mark.asyncio
async def test_provider_outage_and_recovery():
    """Provider outage rejects calls; lifting outage resumes calls."""
    provider = ProviderA()
    req = CallInitRequest(
        call_id=str(uuid.uuid4()),
        campaign_id="c1",
        borrower_id="b1",
        phone_number="+15550003",
    )

    # 1. Outage active
    provider.set_outage(True)
    assert not provider.is_healthy
    resp_outage = await provider.initiate_call(req)
    assert not resp_outage.success
    assert "outage" in resp_outage.error.lower()

    # 2. Outage lifted
    provider.set_outage(False)
    assert provider.is_healthy
    resp_recovered = await provider.initiate_call(req)
    assert resp_recovered.success
