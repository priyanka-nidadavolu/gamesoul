"""
tests/test_emotion_extractor.py
--------------------------------
Tests for the emotion extraction pipeline.
Run: pytest tests/ -v
"""

import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../extraction"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../bandit"))

from emotion_extractor import EmotionExtractor, EmotionVector, DIMENSIONS
from bandit import ThompsonBandit


# ── EmotionVector Tests ────────────────────────────────────────────────────────

def test_emotion_vector_to_list():
    vec = EmotionVector(pace=3.0, tension=7.0, agency=9.0, warmth=2.0,
                        scale=5.0, beauty=6.0, dread=4.0, wonder=8.0, rivalry=1.0)
    lst = vec.to_list()
    assert len(lst) == 9
    assert lst[0] == 3.0  # pace is first
    assert lst[1] == 7.0  # tension


def test_emotion_vector_to_dict():
    vec = EmotionVector(pace=3.0, tension=7.0)
    d = vec.to_dict()
    assert set(d.keys()) == set(DIMENSIONS)
    assert d["pace"] == 3.0


def test_contrast_extraction():
    """Loved high-pace game vs hated low-pace game → target should be high pace."""
    loved = EmotionVector(pace=9, tension=9, agency=7, warmth=2, scale=5,
                          beauty=3, dread=6, wonder=2, rivalry=9)
    hated = EmotionVector(pace=1, tension=1, agency=8, warmth=9, scale=2,
                          beauty=8, dread=0, wonder=6, rivalry=0)
    target = loved.contrast(hated)
    # Target pace should be high (loved high - hated low → big positive delta)
    assert target.pace > 7
    # Target warmth should be low (loved low - hated high → negative delta)
    assert target.warmth < 4


def test_midpoint_default():
    vec = EmotionVector.midpoint()
    assert vec.pace == 5.0
    assert vec.tension == 5.0


def test_merge_weighted():
    extractor = EmotionExtractor()
    v1 = EmotionVector(pace=8.0, tension=8.0, agency=7.0, warmth=2.0,
                       scale=5.0, beauty=3.0, dread=6.0, wonder=2.0, rivalry=9.0)
    v2 = EmotionVector(pace=2.0, tension=2.0, agency=8.0, warmth=9.0,
                       scale=2.0, beauty=8.0, dread=0.0, wonder=6.0, rivalry=0.0)
    merged = extractor.merge_weighted([v1, v2], [0.6, 0.4])
    # Weighted average: pace = 8*0.6 + 2*0.4 = 5.6
    assert abs(merged.pace - 5.6) < 0.01
    assert abs(merged.warmth - (2*0.6 + 9*0.4)) < 0.01


def test_parse_valid_llm_response():
    extractor = EmotionExtractor()
    response = '''{
        "pace": 8.5, "tension": 9.0, "agency": 7.0, "warmth": 2.0,
        "scale": 5.0, "beauty": 4.0, "dread": 6.0, "wonder": 3.0,
        "rivalry": 9.0, "confidence": 0.85,
        "justifications": {"pace": "Very fast-paced game."}
    }'''
    vec = extractor._parse_response(response)
    assert vec.pace == 8.5
    assert vec.confidence == 0.85
    assert vec.justifications.get("pace") == "Very fast-paced game."


def test_parse_response_with_markdown():
    """Handles markdown code blocks from some LLMs."""
    extractor = EmotionExtractor()
    response = '```json\n{"pace": 5.0, "tension": 5.0, "agency": 5.0, "warmth": 5.0, "scale": 5.0, "beauty": 5.0, "dread": 5.0, "wonder": 5.0, "rivalry": 5.0, "confidence": 0.7}\n```'
    vec = extractor._parse_response(response)
    assert vec.pace == 5.0
    assert vec.confidence == 0.7


def test_parse_invalid_response_returns_default():
    extractor = EmotionExtractor()
    vec = extractor._parse_response("This is not JSON at all")
    assert isinstance(vec, EmotionVector)
    assert vec.confidence == 0.1


# ── Thompson Bandit Tests ─────────────────────────────────────────────────────

def test_bandit_initial_arms():
    bandit = ThompsonBandit()
    stats = bandit.arm_stats("global")
    assert set(stats.keys()) == {"text", "visual", "sound", "anchor"}


def test_bandit_select_arm():
    bandit = ThompsonBandit()
    arm = bandit.select_arm("global")
    assert arm in ["text", "visual", "sound", "anchor"]


def test_bandit_update_shifts_expected_reward():
    bandit = ThompsonBandit()
    # Repeatedly reward "text" arm
    for _ in range(50):
        bandit.update("text", 1.0, "global")

    stats = bandit.arm_stats("global")
    # text arm should have highest expected reward
    rewards = {k: v["expected_reward"] for k, v in stats.items()}
    assert rewards["text"] == max(rewards.values())


def test_bandit_select_returns_k():
    bandit = ThompsonBandit()
    candidates = [(i, 0.9 - i * 0.05) for i in range(20)]
    selected = bandit.select(candidates, k=5, segment="global")
    assert len(selected) == 5
    assert all(isinstance(x, int) for x in selected)


def test_bandit_select_fewer_than_k():
    """When fewer candidates than k, return all."""
    bandit = ThompsonBandit()
    candidates = [(1, 0.9), (2, 0.7), (3, 0.5)]
    selected = bandit.select(candidates, k=5, segment="global")
    assert len(selected) == 3


def test_bandit_load_from_db_rows():
    bandit = ThompsonBandit()
    rows = [
        {"arm_name": "text", "user_segment": "global", "alpha": 20.0, "beta": 5.0},
        {"arm_name": "visual", "user_segment": "global", "alpha": 10.0, "beta": 10.0},
    ]
    bandit.load_from_db_rows(rows)
    stats = bandit.arm_stats("global")
    assert abs(stats["text"]["expected_reward"] - 20/25) < 0.01
    assert abs(stats["visual"]["expected_reward"] - 0.5) < 0.01


# ── A/B Framework Tests ────────────────────────────────────────────────────────

def test_variant_assignment_deterministic():
    from experiments.ab_framework import assign_variant
    session = "abc-123-xyz"
    v1 = assign_variant(session, "exp1_input_mode")
    v2 = assign_variant(session, "exp1_input_mode")
    assert v1 == v2


def test_variant_assignment_distribution():
    """Rough check that variants are ~evenly distributed."""
    from experiments.ab_framework import assign_variant
    counts = {"control": 0, "A": 0, "B": 0, "C": 0}
    for i in range(10000):
        v = assign_variant(f"session_{i}", "exp1_input_mode")
        counts[v] += 1
    # Each variant should be ~2500 ± 300
    for v, n in counts.items():
        assert 2000 < n < 3000, f"Variant {v} count {n} out of expected range"


def test_significance_test():
    from experiments.ab_framework import VariantMetrics, compute_significance
    ctrl = VariantMetrics("control", 1000, 500, 3.5, 0.30, 2.1, 0.25)
    variant = VariantMetrics("variant", 1000, 500, 4.2, 0.55, 2.8, 0.40)
    result = compute_significance("exp1_input_mode", ctrl, variant)
    # 30% vs 55% five_star_rate with n=500 each should be significant
    assert result.significant
    assert result.p_value < 0.05
    assert result.relative_lift > 0


def test_insignificant_result():
    from experiments.ab_framework import VariantMetrics, compute_significance
    # Nearly identical metrics → not significant
    ctrl = VariantMetrics("control", 100, 50, 3.5, 0.30, 2.1, 0.25)
    variant = VariantMetrics("variant", 100, 50, 3.51, 0.31, 2.1, 0.25)
    result = compute_significance("exp1_input_mode", ctrl, variant)
    assert not result.significant
