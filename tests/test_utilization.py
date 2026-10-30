"""
Tests for Fix 5: Deterministic Agent Utilization calculations.
"""

from __future__ import annotations

import pytest
from app.services.metrics import AgentUtilizationTracker, calculate_talk_time_utilization


def test_duty_cycle_utilization_full_capacity():
    """All agents busy 100% of the cycles -> 100.0% utilization."""
    tracker = AgentUtilizationTracker(total_agents=5)
    for _ in range(10):
        tracker.record_cycle(busy_agents=5)
    assert tracker.compute_duty_cycle_utilization() == 100.0


def test_duty_cycle_utilization_half_capacity():
    """Average 2.5 of 5 agents busy -> 50.0% utilization."""
    tracker = AgentUtilizationTracker(total_agents=5)
    # 5 cycles with 2 busy, 5 cycles with 3 busy -> avg 2.5 busy
    for _ in range(5):
        tracker.record_cycle(busy_agents=2)
    for _ in range(5):
        tracker.record_cycle(busy_agents=3)
    assert tracker.compute_duty_cycle_utilization() == 50.0


def test_duty_cycle_utilization_zero_capacity():
    """0 agents busy -> 0.0% utilization."""
    tracker = AgentUtilizationTracker(total_agents=5)
    for _ in range(10):
        tracker.record_cycle(busy_agents=0)
    assert tracker.compute_duty_cycle_utilization() == 0.0


def test_duty_cycle_utilization_empty_tracker():
    """No cycles recorded -> 0.0% utilization."""
    tracker = AgentUtilizationTracker(total_agents=5)
    assert tracker.compute_duty_cycle_utilization() == 0.0


def test_talk_time_utilization_calculation():
    """Talk-time mathematical formula test."""
    # 5 agents * 100 seconds = 500 capacity seconds
    # 250 talk seconds -> 50.0% utilization
    util = calculate_talk_time_utilization(
        total_talk_seconds=250,
        num_agents=5,
        elapsed_seconds=100.0,
    )
    assert util == 50.0

    # Zero agents / zero elapsed time
    assert calculate_talk_time_utilization(100, 0, 100) == 0.0
    assert calculate_talk_time_utilization(100, 5, 0) == 0.0
