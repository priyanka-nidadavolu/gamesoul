"""
bandit/bandit.py
----------------
Thompson sampling multi-armed bandit for input mode selection.
Each arm is a Beta(alpha, beta) distribution. We sample from each
and pick the arm with the highest sample.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

ARMS = ["text", "visual", "sound", "anchor"]


@dataclass
class BetaArm:
    name: str
    alpha: float = 1.0   # successes + 1 (prior)
    beta: float = 1.0    # failures + 1 (prior)

    def sample(self) -> float:
        return float(np.random.beta(self.alpha, self.beta))

    def update(self, reward: float):
        """reward should be in [0, 1]."""
        self.alpha += reward
        self.beta += 1.0 - reward

    @property
    def expected_reward(self) -> float:
        return self.alpha / (self.alpha + self.beta)

    @property
    def total_pulls(self) -> int:
        return int(self.alpha + self.beta - 2)


class ThompsonBandit:
    """
    Per-segment Thompson sampling bandit.
    Used to select which input mode to present more prominently
    and which games to surface from the candidate list.
    """

    def __init__(self, arms: list[str] = None):
        self.arms: list[str] = arms or ARMS
        # segment → arm_name → BetaArm
        self._state: dict[str, dict[str, BetaArm]] = {}

    def _get_arms(self, segment: str) -> dict[str, BetaArm]:
        if segment not in self._state:
            self._state[segment] = {arm: BetaArm(arm) for arm in self.arms}
        return self._state[segment]

    def select_arm(self, segment: str = "global") -> str:
        """Pick the arm with the highest Thompson sample."""
        arm_dict = self._get_arms(segment)
        samples = {name: arm.sample() for name, arm in arm_dict.items()}
        return max(samples, key=samples.__getitem__)

    def update(self, arm: str, reward: float, segment: str = "global"):
        """Record a reward for an arm."""
        arms = self._get_arms(segment)
        if arm not in arms:
            logger.warning(f"Unknown arm '{arm}' for segment '{segment}'")
            return
        arms[arm].update(reward)

    def select(
        self,
        candidates: list[tuple[int, float]],
        k: int = 5,
        segment: str = "global",
    ) -> list[int]:
        """
        From a list of (game_id, score) candidates, select k using Thompson sampling.
        This blends the cosine score with bandit uncertainty.
        """
        if len(candidates) <= k:
            return [gid for gid, _ in candidates]

        # Use the arm expected reward as a bonus multiplier
        arm_dict = self._get_arms(segment)
        # For game selection, use the overall bandit confidence as exploration bonus
        global_arm = arm_dict.get("text", BetaArm("text"))
        explore_bonus = 1.0 - global_arm.expected_reward  # explore more when uncertain

        # Thompson-sample each candidate
        sampled = []
        for game_id, score in candidates:
            # Add Beta-distributed noise proportional to exploration bonus
            noise = float(np.random.beta(
                max(1, score * 10),
                max(1, (1 - score) * 10),
            ))
            combined = score * (1 - explore_bonus) + noise * explore_bonus
            sampled.append((game_id, combined))

        sampled.sort(key=lambda x: x[1], reverse=True)
        return [gid for gid, _ in sampled[:k]]

    def arm_stats(self, segment: str = "global") -> dict:
        """Return stats for all arms in a segment."""
        arms = self._get_arms(segment)
        return {
            name: {
                "alpha": arm.alpha,
                "beta": arm.beta,
                "expected_reward": round(arm.expected_reward, 3),
                "total_pulls": arm.total_pulls,
            }
            for name, arm in arms.items()
        }

    def load_from_db_rows(self, rows: list[dict]):
        """Load bandit state from DB rows (arm_name, user_segment, alpha, beta)."""
        for row in rows:
            seg = row["user_segment"]
            name = row["arm_name"]
            if seg not in self._state:
                self._state[seg] = {}
            self._state[seg][name] = BetaArm(
                name=name,
                alpha=float(row["alpha"]),
                beta=float(row["beta"]),
            )
