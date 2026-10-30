"""
Mock Telecom Provider interface and implementations.

All providers implement the same TelecomProvider protocol.
The dialer must not depend on provider-specific implementation details.

Provider event types emitted:
  INITIATED, RINGING, ANSWERED, COMPLETED, FAILED
"""

from __future__ import annotations

import asyncio
import random
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class ProviderEventType(str, Enum):
    INITIATED = "INITIATED"
    RINGING = "RINGING"
    ANSWERED = "ANSWERED"
    CONNECTED = "CONNECTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


@dataclass
class ProviderEvent:
    """A single event emitted by the telecom provider."""
    event_id: str
    provider_name: str
    provider_call_id: str
    event_type: ProviderEventType
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict = field(default_factory=dict)


@dataclass
class CallInitRequest:
    call_id: str          # Our internal call ID
    campaign_id: str
    borrower_id: str
    phone_number: str
    agent_id: Optional[str] = None


@dataclass
class CallInitResponse:
    success: bool
    provider_call_id: Optional[str] = None
    error: Optional[str] = None


class TelecomProvider(ABC):
    """
    Abstract telecom provider interface.

    The dialer communicates with providers exclusively through this interface.
    Providers are responsible for simulating call flows and emitting events.
    """

    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def is_healthy(self) -> bool: ...

    @property
    @abstractmethod
    def answer_rate(self) -> float: ...

    @property
    @abstractmethod
    def avg_talk_seconds(self) -> int: ...

    def configure(
        self,
        answer_rate: Optional[float] = None,
        avg_talk_seconds: Optional[int] = None,
    ) -> None:
        """Dynamically configure provider behavior parameters."""
        pass

    @abstractmethod
    async def initiate_call(self, request: CallInitRequest) -> CallInitResponse: ...

    def set_outage(self, active: bool) -> None:
        """Simulate a provider outage (default: no-op override in subclasses)."""
        pass


# ---------------------------------------------------------------------------
# Shared event bus — in production this would be a message queue.
# Here it is an in-memory asyncio queue used by the API event endpoint.
# ---------------------------------------------------------------------------
_event_handlers: list = []


def register_event_handler(handler) -> None:
    _event_handlers.append(handler)


def clear_event_handlers() -> None:
    _event_handlers.clear()


async def dispatch_event(event: ProviderEvent) -> None:
    for handler in _event_handlers:
        try:
            if asyncio.iscoroutinefunction(handler):
                await handler(event)
            else:
                handler(event)
        except Exception:
            pass  # Individual handler failures must not crash the provider


# ---------------------------------------------------------------------------
# Provider A — fast, reliable, low failure rate
# ---------------------------------------------------------------------------

class ProviderA(TelecomProvider):
    """
    Fast, reliable provider.
    - Failure rate: ~2%
    - Latency: 50–150 ms
    - Events arrive in order, no duplicates
    """

    _name = "provider_a"
    _failure_rate = 0.02

    def __init__(
        self,
        rng: Optional[random.Random] = None,
        answer_rate: float = 0.30,
        avg_talk_seconds: int = 120,
    ):
        self._rng = rng or random.Random()
        self._outage = False
        self._answer_rate = float(answer_rate)
        self._avg_talk_seconds = int(avg_talk_seconds)

    @property
    def name(self) -> str:
        return self._name

    @property
    def is_healthy(self) -> bool:
        return not self._outage

    @property
    def answer_rate(self) -> float:
        return self._answer_rate

    @property
    def avg_talk_seconds(self) -> int:
        return self._avg_talk_seconds

    def configure(
        self,
        answer_rate: Optional[float] = None,
        avg_talk_seconds: Optional[int] = None,
    ) -> None:
        if answer_rate is not None:
            self._answer_rate = float(answer_rate)
        if avg_talk_seconds is not None:
            self._avg_talk_seconds = int(avg_talk_seconds)

    def set_outage(self, active: bool) -> None:
        self._outage = active

    async def initiate_call(self, request: CallInitRequest) -> CallInitResponse:
        if self._outage:
            return CallInitResponse(success=False, error="Provider A outage")

        # Simulate network latency
        await asyncio.sleep(self._rng.uniform(0.05, 0.15))

        if self._rng.random() < self._failure_rate:
            return CallInitResponse(success=False, error="Provider A: transient failure")

        provider_call_id = f"pA-{uuid.uuid4().hex[:12]}"

        # Schedule the call event sequence asynchronously
        asyncio.create_task(
            self._simulate_call_flow(request, provider_call_id)
        )

        return CallInitResponse(success=True, provider_call_id=provider_call_id)

    async def _simulate_call_flow(
        self, request: CallInitRequest, provider_call_id: str
    ) -> None:
        base_id = f"pA-{provider_call_id}"

        await dispatch_event(ProviderEvent(
            event_id=f"{base_id}-INITIATED",
            provider_name=self._name,
            provider_call_id=provider_call_id,
            event_type=ProviderEventType.INITIATED,
            metadata={"call_id": request.call_id},
        ))
        await asyncio.sleep(self._rng.uniform(0.05, 0.15))

        await dispatch_event(ProviderEvent(
            event_id=f"{base_id}-RINGING",
            provider_name=self._name,
            provider_call_id=provider_call_id,
            event_type=ProviderEventType.RINGING,
            metadata={"call_id": request.call_id},
        ))

        # Configurable answer probability
        await asyncio.sleep(self._rng.uniform(0.1, 0.25))
        if self._rng.random() < self._answer_rate:
            await dispatch_event(ProviderEvent(
                event_id=f"{base_id}-ANSWERED",
                provider_name=self._name,
                provider_call_id=provider_call_id,
                event_type=ProviderEventType.ANSWERED,
                metadata={"call_id": request.call_id},
            ))
            low_dur = max(5, int(self._avg_talk_seconds * 0.75))
            high_dur = max(low_dur + 1, int(self._avg_talk_seconds * 1.25))
            dur = int(self._rng.uniform(low_dur, high_dur))
            await asyncio.sleep(self._rng.uniform(0.15, 0.4))
            await dispatch_event(ProviderEvent(
                event_id=f"{base_id}-COMPLETED",
                provider_name=self._name,
                provider_call_id=provider_call_id,
                event_type=ProviderEventType.COMPLETED,
                metadata={"call_id": request.call_id, "duration_seconds": dur},
            ))
        else:
            await dispatch_event(ProviderEvent(
                event_id=f"{base_id}-FAILED",
                provider_name=self._name,
                provider_call_id=provider_call_id,
                event_type=ProviderEventType.FAILED,
                metadata={"call_id": request.call_id, "reason": "no-answer"},
            ))


# ---------------------------------------------------------------------------
# Provider B — slower, unreliable, emits duplicate/out-of-order events
# ---------------------------------------------------------------------------

class ProviderB(TelecomProvider):
    """
    Slower, occasionally unreliable provider.
    - Failure rate: ~15%
    - Latency: 200 ms – 2 s (occasional timeouts up to 5 s)
    - Duplicate events: ~10% of events are re-sent
    - Out-of-order events: ~5% chance events arrive out of sequence
    """

    _name = "provider_b"
    _failure_rate = 0.15
    _duplicate_rate = 0.10
    _out_of_order_rate = 0.05

    def __init__(
        self,
        rng: Optional[random.Random] = None,
        answer_rate: float = 0.30,
        avg_talk_seconds: int = 120,
    ):
        self._rng = rng or random.Random()
        self._outage = False
        self._answer_rate = float(answer_rate)
        self._avg_talk_seconds = int(avg_talk_seconds)

    @property
    def name(self) -> str:
        return self._name

    @property
    def is_healthy(self) -> bool:
        return not self._outage

    @property
    def answer_rate(self) -> float:
        return self._answer_rate

    @property
    def avg_talk_seconds(self) -> int:
        return self._avg_talk_seconds

    def configure(
        self,
        answer_rate: Optional[float] = None,
        avg_talk_seconds: Optional[int] = None,
    ) -> None:
        if answer_rate is not None:
            self._answer_rate = float(answer_rate)
        if avg_talk_seconds is not None:
            self._avg_talk_seconds = int(avg_talk_seconds)

    def set_outage(self, active: bool) -> None:
        self._outage = active

    async def initiate_call(self, request: CallInitRequest) -> CallInitResponse:
        if self._outage:
            return CallInitResponse(success=False, error="Provider B outage")

        # Simulate slower, occasional timeout
        latency = self._rng.uniform(0.1, 0.4)
        if self._rng.random() < 0.05:
            latency = self._rng.uniform(1.0, 2.0)  # timeout scenario
        await asyncio.sleep(latency)

        if self._rng.random() < self._failure_rate:
            return CallInitResponse(success=False, error="Provider B: transient failure")

        provider_call_id = f"pB-{uuid.uuid4().hex[:12]}"

        asyncio.create_task(
            self._simulate_call_flow(request, provider_call_id)
        )

        return CallInitResponse(success=True, provider_call_id=provider_call_id)

    async def _emit(self, event: ProviderEvent) -> None:
        """Emit with possible duplication."""
        await dispatch_event(event)
        if self._rng.random() < self._duplicate_rate:
            # Duplicate: same event_id, arrives slightly later
            await asyncio.sleep(self._rng.uniform(0.01, 0.05))
            await dispatch_event(event)

    async def _simulate_call_flow(
        self, request: CallInitRequest, provider_call_id: str
    ) -> None:
        base_id = f"pB-{provider_call_id}"

        initiated_event = ProviderEvent(
            event_id=f"{base_id}-INITIATED",
            provider_name=self._name,
            provider_call_id=provider_call_id,
            event_type=ProviderEventType.INITIATED,
            metadata={"call_id": request.call_id},
        )
        ringing_event = ProviderEvent(
            event_id=f"{base_id}-RINGING",
            provider_name=self._name,
            provider_call_id=provider_call_id,
            event_type=ProviderEventType.RINGING,
            metadata={"call_id": request.call_id},
        )

        # Out-of-order: occasionally send RINGING before INITIATED
        if self._rng.random() < self._out_of_order_rate:
            await self._emit(ringing_event)
            await asyncio.sleep(self._rng.uniform(0.02, 0.08))
            await self._emit(initiated_event)
        else:
            await self._emit(initiated_event)
            await asyncio.sleep(self._rng.uniform(0.05, 0.2))
            await self._emit(ringing_event)

        await asyncio.sleep(self._rng.uniform(0.1, 0.3))
        if self._rng.random() < self._answer_rate:
            await self._emit(ProviderEvent(
                event_id=f"{base_id}-ANSWERED",
                provider_name=self._name,
                provider_call_id=provider_call_id,
                event_type=ProviderEventType.ANSWERED,
                metadata={"call_id": request.call_id},
            ))
            low_dur = max(5, int(self._avg_talk_seconds * 0.75))
            high_dur = max(low_dur + 1, int(self._avg_talk_seconds * 1.25))
            dur = int(self._rng.uniform(low_dur, high_dur))
            await asyncio.sleep(self._rng.uniform(0.2, 0.5))
            await self._emit(ProviderEvent(
                event_id=f"{base_id}-COMPLETED",
                provider_name=self._name,
                provider_call_id=provider_call_id,
                event_type=ProviderEventType.COMPLETED,
                metadata={"call_id": request.call_id, "duration_seconds": dur},
            ))
        else:
            await self._emit(ProviderEvent(
                event_id=f"{base_id}-FAILED",
                provider_name=self._name,
                provider_call_id=provider_call_id,
                event_type=ProviderEventType.FAILED,
                metadata={"call_id": request.call_id, "reason": "no-answer"},
            ))


# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------

_PROVIDERS: dict[str, TelecomProvider] = {
    "provider_a": ProviderA(),
    "provider_b": ProviderB(),
}


def get_provider(name: str) -> TelecomProvider:
    if name not in _PROVIDERS:
        raise ValueError(f"Unknown provider: {name}")
    return _PROVIDERS[name]


def get_all_providers() -> dict[str, TelecomProvider]:
    return dict(_PROVIDERS)
