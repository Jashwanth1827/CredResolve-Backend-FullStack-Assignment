"""
SmartDialer — Agent Utilization & Campaign Metrics Tracker.

Mathematical definitions:
1. Time-averaged duty-cycle utilization:
   Evaluated across all evaluation intervals / dialing cycles k in 1..K:
       busy_agents(k) = count(agents in {RESERVED, DIALING, CONNECTED, WRAP_UP})
       utilization_pct = (sum(busy_agents(k)) / (K * total_agents)) * 100

2. Talk-time capacity utilization:
       utilization_pct = min(100.0, (sum(talk_seconds) / (total_agents * elapsed_seconds)) * 100)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


@dataclass
class AgentUtilizationTracker:
    """
    Tracks agent busy status at each evaluation cycle of a simulation
    to compute continuous time-averaged utilization across the whole timeline.
    """
    total_agents: int
    cycle_busy_counts: List[int] = field(default_factory=list)

    def record_cycle(self, busy_agents: int) -> None:
        """Sample how many agents were busy during a dialing cycle."""
        self.cycle_busy_counts.append(min(busy_agents, self.total_agents))

    def compute_duty_cycle_utilization(self) -> float:
        """
        Calculate time-averaged agent capacity utilization across all recorded cycles.
        Returns a percentage between 0.0% and 100.0%.
        """
        if not self.cycle_busy_counts or self.total_agents <= 0:
            return 0.0
        total_possible_busy = len(self.cycle_busy_counts) * self.total_agents
        total_actual_busy = sum(self.cycle_busy_counts)
        pct = (total_actual_busy / total_possible_busy) * 100.0
        return round(min(100.0, max(0.0, pct)), 1)


def calculate_talk_time_utilization(
    total_talk_seconds: int,
    num_agents: int,
    elapsed_seconds: float,
) -> float:
    """
    Calculate agent utilization from talk time and duration.
    """
    if num_agents <= 0 or elapsed_seconds <= 0:
        return 0.0
    total_capacity = num_agents * elapsed_seconds
    pct = (total_talk_seconds / total_capacity) * 100.0
    return round(min(100.0, max(0.0, pct)), 1)
