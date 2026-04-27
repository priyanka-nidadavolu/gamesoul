"""
experiments/ab_framework.py
---------------------------
A/B experiment assignment and significance testing.
Runs chi-square tests for binary metrics and t-tests for continuous ones.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Optional

import numpy as np
from scipy import stats

logger = logging.getLogger(__name__)


EXPERIMENTS = {
    "exp1_input_mode": {
        "description": "Input mode effectiveness",
        "hypothesis": "Different input modes produce different recommendation satisfaction rates.",
        "variants": {"control": 0.25, "A": 0.25, "B": 0.25, "C": 0.25},
        "primary_metric": "five_star_rate",
        "secondary_metric": "session_depth",
        "min_detectable_effect": 0.05,
    },
    "exp2_llm_size": {
        "description": "Emotion extraction model",
        "hypothesis": "Larger LLM produces more accurate emotion vectors.",
        "variants": {"control": 0.50, "variant": 0.50},
        "primary_metric": "avg_rating",
        "secondary_metric": "vector_accuracy",
    },
    "exp3_retrieval": {
        "description": "Retrieval strategy",
        "hypothesis": "Hybrid retrieval outperforms pure cosine similarity.",
        "variants": {"control": 0.50, "variant": 0.50},
        "primary_metric": "avg_rating",
        "secondary_metric": "discovery_rate",
    },
}


def assign_variant(session_id: str, experiment_id: str) -> str:
    """
    Deterministic assignment: hash(session_id + experiment_id) mod buckets.
    This ensures the same session always gets the same variant.
    """
    exp = EXPERIMENTS.get(experiment_id)
    if not exp:
        raise ValueError(f"Unknown experiment: {experiment_id}")

    key = f"{session_id}:{experiment_id}"
    hash_int = int(hashlib.sha256(key.encode()).hexdigest(), 16)
    bucket = (hash_int % 10000) / 10000  # [0, 1)

    cumulative = 0.0
    for variant, weight in exp["variants"].items():
        cumulative += weight
        if bucket < cumulative:
            return variant

    return list(exp["variants"].keys())[-1]


@dataclass
class VariantMetrics:
    variant: str
    n_recommendations: int
    n_ratings: int
    avg_rating: float
    five_star_rate: float
    session_depth: float
    discovery_rate: float


@dataclass
class SignificanceResult:
    experiment_id: str
    primary_metric: str
    control_value: float
    variant_value: float
    relative_lift: float
    p_value: float
    significant: bool
    confidence_interval: tuple[float, float]
    sample_sizes: dict[str, int]


def chi_square_test(
    control_successes: int,
    control_n: int,
    variant_successes: int,
    variant_n: int,
    alpha: float = 0.05,
) -> tuple[float, bool]:
    """Chi-square test for proportions."""
    contingency = np.array([
        [control_successes, control_n - control_successes],
        [variant_successes, variant_n - variant_successes],
    ])
    chi2, p_value, _, _ = stats.chi2_contingency(contingency)
    return float(p_value), p_value < alpha


def t_test(
    control_values: list[float],
    variant_values: list[float],
    alpha: float = 0.05,
) -> tuple[float, bool]:
    """Two-sample t-test for continuous metrics."""
    if len(control_values) < 2 or len(variant_values) < 2:
        return 1.0, False
    t_stat, p_value = stats.ttest_ind(control_values, variant_values)
    return float(p_value), p_value < alpha


def compute_significance(
    experiment_id: str,
    control: VariantMetrics,
    variant: VariantMetrics,
) -> SignificanceResult:
    """Compute statistical significance for an experiment."""
    exp = EXPERIMENTS[experiment_id]
    metric = exp["primary_metric"]

    if metric == "five_star_rate":
        # Binary metric: chi-square
        ctrl_s = int(control.five_star_rate * control.n_ratings)
        var_s = int(variant.five_star_rate * variant.n_ratings)
        p_val, significant = chi_square_test(
            ctrl_s, control.n_ratings, var_s, variant.n_ratings
        )
        ctrl_val = control.five_star_rate
        var_val = variant.five_star_rate
    else:
        # Continuous metric: use averages directly
        ctrl_val = control.avg_rating
        var_val = variant.avg_rating
        # Approximate with t-test using normal approximation
        p_val, significant = t_test(
            [control.avg_rating] * control.n_ratings,
            [variant.avg_rating] * variant.n_ratings,
        )

    relative_lift = (var_val - ctrl_val) / (ctrl_val + 1e-9)

    # 95% CI for proportion difference
    se = np.sqrt(
        ctrl_val * (1 - ctrl_val) / (control.n_ratings + 1) +
        var_val * (1 - var_val) / (variant.n_ratings + 1)
    )
    diff = var_val - ctrl_val
    ci = (diff - 1.96 * se, diff + 1.96 * se)

    return SignificanceResult(
        experiment_id=experiment_id,
        primary_metric=metric,
        control_value=round(ctrl_val, 4),
        variant_value=round(var_val, 4),
        relative_lift=round(relative_lift, 4),
        p_value=round(p_val, 4),
        significant=significant,
        confidence_interval=(round(ci[0], 4), round(ci[1], 4)),
        sample_sizes={control.variant: control.n_ratings, variant.variant: variant.n_ratings},
    )


def generate_weekly_report(results: list[SignificanceResult]) -> str:
    """Generate a human-readable weekly experiment report."""
    lines = ["=" * 60, "GameSoul Weekly A/B Experiment Report", "=" * 60, ""]

    for r in results:
        exp = EXPERIMENTS.get(r.experiment_id, {})
        lines.append(f"Experiment: {r.experiment_id}")
        lines.append(f"  Hypothesis: {exp.get('hypothesis', 'N/A')}")
        lines.append(f"  Metric: {r.primary_metric}")
        lines.append(f"  Control:  {r.control_value:.3f}  (n={r.sample_sizes.get('control', '?')})")
        lines.append(f"  Variant:  {r.variant_value:.3f}  (n={list(r.sample_sizes.values())[-1]})")
        lines.append(f"  Lift:     {r.relative_lift:+.1%}")
        lines.append(f"  p-value:  {r.p_value:.4f}  {'✓ SIGNIFICANT' if r.significant else '✗ not significant'}")
        lines.append(f"  95% CI:   [{r.confidence_interval[0]:.3f}, {r.confidence_interval[1]:.3f}]")
        lines.append("")

    return "\n".join(lines)
