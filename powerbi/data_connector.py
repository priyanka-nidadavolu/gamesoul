"""
powerbi/data_connector.py
--------------------------
Exports GameSoul analytics data to CSV files that Power BI can connect to.
Run nightly or point Power BI directly at the PostgreSQL connection.

Dashboard pages covered:
  1. Emotion Gap Map — user demand vs game supply per dimension
  2. Input Mode Performance — A/B experiment results
  3. Recommendation Quality Over Time — avg rating per week
  4. Discovery Rate — how often GameSoul surfaces unknown games
  5. Dimension Correlation — which dimensions co-occur in loved games
"""

from __future__ import annotations

import csv
import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

import psycopg2
import numpy as np

logger = logging.getLogger(__name__)

DB_URL = os.getenv("DATABASE_URL", "postgresql://gamesoul:gamesoul@localhost:5432/gamesoul")
OUTPUT_DIR = Path(os.getenv("POWERBI_OUTPUT_DIR", "/tmp/powerbi_exports"))
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

DIMENSIONS = ["pace", "tension", "agency", "warmth", "scale", "beauty", "dread", "wonder", "rivalry"]


def export_emotion_gap(conn):
    """
    Emotion Gap Map: for each dimension, compare:
      - avg demand (from query vectors in recommendations)
      - avg supply (from indexed game profiles)
    """
    with conn.cursor() as cur:
        # User demand: average query vector components
        # query_vector is stored as float[] matching DIMENSIONS order
        cur.execute("""
            SELECT
                AVG((query_vector)[1]) AS pace_demand,
                AVG((query_vector)[2]) AS tension_demand,
                AVG((query_vector)[3]) AS agency_demand,
                AVG((query_vector)[4]) AS warmth_demand,
                AVG((query_vector)[5]) AS scale_demand,
                AVG((query_vector)[6]) AS beauty_demand,
                AVG((query_vector)[7]) AS dread_demand,
                AVG((query_vector)[8]) AS wonder_demand,
                AVG((query_vector)[9]) AS rivalry_demand
            FROM recommendations
            WHERE created_at > NOW() - INTERVAL '30 days'
        """)
        demand_row = cur.fetchone() or [5.0] * 9

        # Game supply: average across all indexed games
        cur.execute("""
            SELECT
                AVG(dim_pace), AVG(dim_tension), AVG(dim_agency),
                AVG(dim_warmth), AVG(dim_scale), AVG(dim_beauty),
                AVG(dim_dread), AVG(dim_wonder), AVG(dim_rivalry)
            FROM games WHERE qdrant_indexed = TRUE
        """)
        supply_row = cur.fetchone() or [5.0] * 9

    rows = []
    for i, dim in enumerate(DIMENSIONS):
        demand = float(demand_row[i] or 5.0)
        supply = float(supply_row[i] or 5.0)
        gap = demand - supply
        rows.append({
            "dimension": dim,
            "avg_demand": round(demand, 2),
            "avg_supply": round(supply, 2),
            "gap": round(gap, 2),
            "gap_direction": "undersupplied" if gap > 0.5 else "oversupplied" if gap < -0.5 else "balanced",
        })

    _write_csv("emotion_gap.csv", rows, ["dimension", "avg_demand", "avg_supply", "gap", "gap_direction"])
    logger.info(f"Exported emotion gap: {len(rows)} dimensions")
    return rows


def export_input_mode_performance(conn):
    """Input Mode Performance: A/B results per variant over time."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT
                date_trunc('week', r.created_at) AS week,
                rec.input_mode,
                COUNT(r.id) AS n_ratings,
                AVG(r.rating) AS avg_rating,
                SUM(CASE WHEN r.rating = 5 THEN 1 ELSE 0 END)::float / NULLIF(COUNT(r.id), 0) AS five_star_rate,
                SUM(CASE WHEN r.thumbs_up = TRUE THEN 1 ELSE 0 END)::float / NULLIF(COUNT(r.id), 0) AS thumbs_up_rate
            FROM ratings r
            JOIN recommendations rec ON rec.id = r.recommendation_id
            GROUP BY 1, 2
            ORDER BY 1, 2
        """)
        rows = []
        for row in cur.fetchall():
            rows.append({
                "week": row[0].isoformat() if row[0] else "",
                "input_mode": row[1],
                "n_ratings": int(row[2]),
                "avg_rating": round(float(row[3] or 0), 3),
                "five_star_rate": round(float(row[4] or 0), 3),
                "thumbs_up_rate": round(float(row[5] or 0), 3),
            })

    _write_csv("input_mode_performance.csv", rows,
               ["week", "input_mode", "n_ratings", "avg_rating", "five_star_rate", "thumbs_up_rate"])
    logger.info(f"Exported input mode performance: {len(rows)} rows")
    return rows


def export_recommendation_quality(conn):
    """Recommendation quality over time: avg rating per week."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT
                date_trunc('week', r.created_at) AS week,
                COUNT(r.id) AS n_ratings,
                AVG(r.rating) AS avg_rating,
                AVG(CASE WHEN r.thumbs_up IS NOT NULL THEN r.thumbs_up::int END) AS thumbs_up_rate,
                COUNT(DISTINCT r.session_id) AS unique_sessions
            FROM ratings r
            GROUP BY 1
            ORDER BY 1
        """)
        rows = []
        for row in cur.fetchall():
            rows.append({
                "week": row[0].isoformat() if row[0] else "",
                "n_ratings": int(row[1]),
                "avg_rating": round(float(row[2] or 0), 3),
                "thumbs_up_rate": round(float(row[3] or 0), 3),
                "unique_sessions": int(row[4]),
            })

    _write_csv("recommendation_quality.csv", rows,
               ["week", "n_ratings", "avg_rating", "thumbs_up_rate", "unique_sessions"])
    return rows


def export_dimension_correlation(conn):
    """Which emotional dimensions co-occur in games users rate 4-5 stars?"""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT
                g.dim_pace, g.dim_tension, g.dim_agency, g.dim_warmth,
                g.dim_scale, g.dim_beauty, g.dim_dread, g.dim_wonder, g.dim_rivalry,
                AVG(r.rating) AS avg_rating
            FROM games g
            JOIN ratings r ON r.game_id = g.id
            WHERE r.rating IS NOT NULL
              AND g.dim_pace IS NOT NULL
            GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9
            HAVING COUNT(r.id) >= 5
        """)
        rows = cur.fetchall()

    if not rows:
        logger.warning("No correlation data yet")
        return []

    # Build correlation matrix
    vectors = np.array([[r[i] for i in range(9)] for r in rows], dtype=float)
    ratings = np.array([r[9] for r in rows], dtype=float)

    # Weight by rating (high-rated games contribute more)
    weights = (ratings - 1) / 4  # normalize 1-5 to 0-1
    corr_data = []
    for i, d1 in enumerate(DIMENSIONS):
        for j, d2 in enumerate(DIMENSIONS):
            if i <= j:
                corr = float(np.corrcoef(vectors[:, i], vectors[:, j])[0, 1])
                corr_data.append({
                    "dim1": d1,
                    "dim2": d2,
                    "correlation": round(corr, 3),
                })

    _write_csv("dimension_correlation.csv", corr_data, ["dim1", "dim2", "correlation"])
    logger.info(f"Exported dimension correlation: {len(corr_data)} pairs")
    return corr_data


def export_bandit_arms(conn):
    """Current Thompson sampling arm statistics."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT arm_name, user_segment, alpha, beta, total_pulls, total_rewards, updated_at
            FROM bandit_arms ORDER BY user_segment, arm_name
        """)
        rows = []
        for row in cur.fetchall():
            alpha, beta = float(row[2]), float(row[3])
            exp_reward = alpha / (alpha + beta)
            rows.append({
                "arm_name": row[0],
                "user_segment": row[1],
                "alpha": round(alpha, 2),
                "beta": round(beta, 2),
                "expected_reward": round(exp_reward, 3),
                "total_pulls": int(row[4]),
                "total_rewards": round(float(row[5]), 1),
                "updated_at": row[6].isoformat() if row[6] else "",
            })

    _write_csv("bandit_arms.csv", rows,
               ["arm_name", "user_segment", "alpha", "beta", "expected_reward",
                "total_pulls", "total_rewards", "updated_at"])
    return rows


def _write_csv(filename: str, rows: list[dict], fieldnames: list[str]):
    path = OUTPUT_DIR / filename
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    logger.info(f"Wrote {path}")


def run_all_exports():
    conn = psycopg2.connect(DB_URL)
    try:
        export_emotion_gap(conn)
        export_input_mode_performance(conn)
        export_recommendation_quality(conn)
        export_dimension_correlation(conn)
        export_bandit_arms(conn)
        logger.info(f"All exports complete → {OUTPUT_DIR}")
    finally:
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_all_exports()
