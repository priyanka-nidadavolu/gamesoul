"""
airflow/dags/gamesoul_dags.py
------------------------------
All four GameSoul Airflow DAGs in a single file:
  1. nightly_embed_new_games
  2. weekly_ab_evaluation
  3. monthly_bandit_retrain
  4. daily_data_quality
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from typing import Optional

from airflow import DAG
from airflow.operators.python import PythonOperator
from airflow.providers.postgres.hooks.postgres import PostgresHook

logger = logging.getLogger(__name__)

QDRANT_HOST = os.getenv("QDRANT_HOST", "qdrant")
KAFKA_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "kafka:29092")
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://ollama:11434")
CONN_ID = "gamesoul_db"

default_args = {
    "owner": "gamesoul",
    "retries": 2,
    "retry_delay": timedelta(minutes=5),
    "email_on_failure": False,
}

# ─────────────────────────────────────────────────────────────────────────────
# DAG 1: nightly_embed_new_games
# ─────────────────────────────────────────────────────────────────────────────

def _pull_new_games_from_kafka(**ctx):
    """Consume game.releases topic and return list of game IDs."""
    from kafka import KafkaConsumer
    import json

    consumer = KafkaConsumer(
        "game.releases",
        bootstrap_servers=KAFKA_SERVERS,
        group_id="nightly_embed",
        auto_offset_reset="earliest",
        enable_auto_commit=False,
        consumer_timeout_ms=10000,
        value_deserializer=lambda m: json.loads(m.decode("utf-8")),
    )

    game_ids = []
    for msg in consumer:
        game_id = msg.value.get("game_id")
        if game_id:
            game_ids.append(game_id)

    consumer.commit()
    consumer.close()
    logger.info(f"Found {len(game_ids)} new games to embed")
    ctx["ti"].xcom_push(key="game_ids", value=game_ids)
    return game_ids


def _extract_emotions_for_games(**ctx):
    """Run LLM emotion extraction for each new game."""
    import sys
    sys.path.insert(0, "/opt/airflow/dags")
    sys.path.insert(0, "/app/extraction")

    from emotion_extractor import EmotionExtractor, DIMENSIONS

    game_ids = ctx["ti"].xcom_pull(key="game_ids", task_ids="pull_new_games")
    if not game_ids:
        logger.info("No new games to process")
        return []

    hook = PostgresHook(postgres_conn_id=CONN_ID)
    extractor = EmotionExtractor(ollama_host=OLLAMA_HOST)
    results = []

    for game_id in game_ids:
        row = hook.get_first(
            "SELECT id, name, description FROM games WHERE id=%s AND qdrant_indexed=FALSE",
            (game_id,),
        )
        if not row:
            continue

        gid, name, description = row
        try:
            if description:
                vec = extractor.from_description(description, name)
            else:
                vec = extractor.from_description(name, name)

            # Update game record
            updates = {f"dim_{d}": getattr(vec, d) for d in DIMENSIONS}
            updates["extraction_confidence"] = vec.confidence
            updates["extraction_source"] = "description"
            updates["needs_review"] = vec.confidence < 0.5

            set_clause = ", ".join(f"{k} = %s" for k in updates)
            values = list(updates.values()) + [gid]
            hook.run(f"UPDATE games SET {set_clause} WHERE id = %s", parameters=values)

            results.append({"game_id": gid, "vector": vec.to_list()})
            logger.info(f"Extracted emotions for game {gid}: {name}")
        except Exception as e:
            logger.error(f"Failed to extract emotions for game {gid}: {e}")

    ctx["ti"].xcom_push(key="extracted", value=results)
    return results


def _upsert_to_qdrant(**ctx):
    """Upsert emotion vectors into Qdrant."""
    from qdrant_client import QdrantClient
    from qdrant_client.models import PointStruct

    extracted = ctx["ti"].xcom_pull(key="extracted", task_ids="extract_emotions")
    if not extracted:
        return

    hook = PostgresHook(postgres_conn_id=CONN_ID)
    client = QdrantClient(host=QDRANT_HOST, port=6333)

    points = []
    for item in extracted:
        gid = item["game_id"]
        vector = item["vector"]
        if len(vector) == 9:
            points.append(PointStruct(id=gid, vector=vector, payload={"game_id": gid}))

    if points:
        client.upsert(collection_name="game_emotions", points=points)
        ids = [p.id for p in points]
        hook.run(
            "UPDATE games SET qdrant_indexed=TRUE WHERE id = ANY(%s)",
            parameters=(ids,),
        )
        logger.info(f"Upserted {len(points)} vectors to Qdrant")

    # Publish pipeline health event
    from kafka import KafkaProducer
    producer = KafkaProducer(
        bootstrap_servers=KAFKA_SERVERS,
        value_serializer=lambda v: json.dumps(v).encode(),
    )
    producer.send("pipeline.health", {
        "dag": "nightly_embed_new_games",
        "games_processed": len(points),
        "status": "success",
        "timestamp": datetime.utcnow().isoformat(),
    })
    producer.flush()


with DAG(
    dag_id="nightly_embed_new_games",
    default_args=default_args,
    description="Pull new games from Kafka, extract emotions, upsert to Qdrant",
    schedule_interval="0 2 * * *",  # 2am daily
    start_date=datetime(2025, 1, 1),
    catchup=False,
    tags=["gamesoul", "embedding"],
) as nightly_dag:

    pull_new_games = PythonOperator(
        task_id="pull_new_games",
        python_callable=_pull_new_games_from_kafka,
    )
    extract_emotions = PythonOperator(
        task_id="extract_emotions",
        python_callable=_extract_emotions_for_games,
    )
    upsert_qdrant = PythonOperator(
        task_id="upsert_qdrant",
        python_callable=_upsert_to_qdrant,
    )

    pull_new_games >> extract_emotions >> upsert_qdrant


# ─────────────────────────────────────────────────────────────────────────────
# DAG 2: weekly_ab_evaluation
# ─────────────────────────────────────────────────────────────────────────────

def _compute_experiment_metrics(**ctx):
    """Compute per-variant metrics for all running experiments."""
    hook = PostgresHook(postgres_conn_id=CONN_ID)

    week_ago = (datetime.utcnow() - timedelta(days=7)).isoformat()

    experiments = hook.get_records(
        "SELECT id, name, variants FROM experiments WHERE status='running'"
    )

    all_metrics = []
    for exp_id, exp_name, variants_json in experiments:
        variants = variants_json if isinstance(variants_json, dict) else json.loads(variants_json)
        for variant in variants:
            rows = hook.get_records(
                """
                SELECT
                    COUNT(rec.id) AS n_recommendations,
                    COUNT(r.id) AS n_ratings,
                    COALESCE(AVG(r.rating), 0) AS avg_rating,
                    COALESCE(SUM(CASE WHEN r.rating=5 THEN 1 ELSE 0 END)::float / NULLIF(COUNT(r.id), 0), 0) AS five_star_rate,
                    COALESCE(AVG(session_depth.depth), 1) AS session_depth
                FROM recommendations rec
                LEFT JOIN ratings r ON r.recommendation_id = rec.id
                JOIN user_sessions us ON us.session_id = rec.session_id
                LEFT JOIN (
                    SELECT session_id, COUNT(*) as depth FROM recommendations GROUP BY session_id
                ) session_depth ON session_depth.session_id = rec.session_id
                WHERE rec.created_at > %s
                  AND us.ab_variants->%s = %s
                """,
                (week_ago, exp_name, variant),
            )
            if rows:
                row = rows[0]
                metrics = {
                    "experiment_id": exp_id,
                    "experiment_name": exp_name,
                    "variant": variant,
                    "n_recommendations": int(row[0]),
                    "n_ratings": int(row[1]),
                    "avg_rating": float(row[2]),
                    "five_star_rate": float(row[3]),
                    "session_depth": float(row[4]),
                }
                all_metrics.append(metrics)

    ctx["ti"].xcom_push(key="metrics", value=all_metrics)
    return all_metrics


def _run_significance_tests(**ctx):
    """Run chi-square / t-tests and save results."""
    import sys
    sys.path.insert(0, "/app/experiments")
    from ab_framework import (
        VariantMetrics, compute_significance, generate_weekly_report,
        EXPERIMENTS,
    )

    metrics = ctx["ti"].xcom_pull(key="metrics", task_ids="compute_metrics")
    hook = PostgresHook(postgres_conn_id=CONN_ID)

    reports = []
    metrics_by_exp = {}
    for m in metrics:
        metrics_by_exp.setdefault(m["experiment_name"], {})[m["variant"]] = m

    for exp_name, variant_data in metrics_by_exp.items():
        control = variant_data.get("control")
        if not control:
            continue
        for variant_name, vdata in variant_data.items():
            if variant_name == "control":
                continue
            ctrl_vm = VariantMetrics(
                variant="control",
                n_recommendations=control["n_recommendations"],
                n_ratings=max(control["n_ratings"], 1),
                avg_rating=control["avg_rating"],
                five_star_rate=control["five_star_rate"],
                session_depth=control["session_depth"],
                discovery_rate=0.3,
            )
            var_vm = VariantMetrics(
                variant=variant_name,
                n_recommendations=vdata["n_recommendations"],
                n_ratings=max(vdata["n_ratings"], 1),
                avg_rating=vdata["avg_rating"],
                five_star_rate=vdata["five_star_rate"],
                session_depth=vdata["session_depth"],
                discovery_rate=0.3,
            )
            # Map experiment name to key
            exp_key = next((k for k in EXPERIMENTS if exp_name in k), None)
            if not exp_key:
                continue
            result = compute_significance(exp_key, ctrl_vm, var_vm)
            reports.append(result)

            # Store in DB
            hook.run(
                """INSERT INTO experiment_results
                   (experiment_id, week_ending, variant, n_recommendations, n_ratings,
                    avg_rating, five_star_rate, p_value, significant)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                parameters=(
                    1, datetime.utcnow().date(), variant_name,
                    vdata["n_recommendations"], vdata["n_ratings"],
                    vdata["avg_rating"], vdata["five_star_rate"],
                    result.p_value, result.significant,
                ),
            )

    if reports:
        report_text = generate_weekly_report(reports)
        logger.info(f"\n{report_text}")
        # In production: email via Airflow EmailOperator
        with open(f"/tmp/ab_report_{datetime.utcnow().date()}.txt", "w") as f:
            f.write(report_text)


with DAG(
    dag_id="weekly_ab_evaluation",
    default_args=default_args,
    description="Compute experiment metrics and significance tests",
    schedule_interval="0 6 * * 1",  # Monday 6am
    start_date=datetime(2025, 1, 1),
    catchup=False,
    tags=["gamesoul", "experiments"],
) as weekly_dag:

    compute_metrics = PythonOperator(
        task_id="compute_metrics",
        python_callable=_compute_experiment_metrics,
    )
    significance_tests = PythonOperator(
        task_id="significance_tests",
        python_callable=_run_significance_tests,
    )

    compute_metrics >> significance_tests


# ─────────────────────────────────────────────────────────────────────────────
# DAG 3: monthly_bandit_retrain
# ─────────────────────────────────────────────────────────────────────────────

def _retrain_bandit(**ctx):
    """Reload bandit priors from accumulated feedback data."""
    hook = PostgresHook(postgres_conn_id=CONN_ID)

    # Compute arm stats from last 30 days of ratings
    rows = hook.get_records(
        """
        SELECT
            rec.input_mode,
            COUNT(r.id) AS pulls,
            SUM(CASE WHEN r.rating >= 4 OR r.thumbs_up THEN 1 ELSE 0 END) AS rewards
        FROM ratings r
        JOIN recommendations rec ON rec.id = r.recommendation_id
        WHERE r.created_at > NOW() - INTERVAL '30 days'
        GROUP BY rec.input_mode
        """
    )

    for arm_name, pulls, rewards in rows:
        # Update Beta parameters: alpha = successes + 1, beta = failures + 1
        alpha = float(rewards) + 1
        beta = float(pulls - rewards) + 1
        hook.run(
            """UPDATE bandit_arms
               SET alpha=%s, beta=%s, total_pulls=%s, total_rewards=%s, updated_at=NOW()
               WHERE arm_name=%s AND user_segment='global'""",
            parameters=(alpha, beta, int(pulls), float(rewards), arm_name),
        )
        logger.info(f"Bandit arm '{arm_name}': alpha={alpha:.1f}, beta={beta:.1f}")

    logger.info("Bandit retrain complete")


with DAG(
    dag_id="monthly_bandit_retrain",
    default_args=default_args,
    description="Retrain Thompson sampling bandit on accumulated feedback",
    schedule_interval="0 3 1 * *",  # 1st of month, 3am
    start_date=datetime(2025, 1, 1),
    catchup=False,
    tags=["gamesoul", "bandit"],
) as monthly_dag:

    retrain_bandit = PythonOperator(
        task_id="retrain_bandit",
        python_callable=_retrain_bandit,
    )


# ─────────────────────────────────────────────────────────────────────────────
# DAG 4: daily_data_quality
# ─────────────────────────────────────────────────────────────────────────────

def _check_data_quality(**ctx):
    """Flag games with low-confidence dimension scores for human review."""
    from kafka import KafkaProducer

    hook = PostgresHook(postgres_conn_id=CONN_ID)

    # Find games with low extraction confidence
    low_conf = hook.get_records(
        """SELECT id, name, extraction_confidence
           FROM games
           WHERE extraction_confidence < 0.5
             AND qdrant_indexed = TRUE
             AND needs_review = FALSE
           LIMIT 100"""
    )

    alerts = []
    for game_id, name, confidence in low_conf:
        hook.run(
            "UPDATE games SET needs_review=TRUE WHERE id=%s",
            parameters=(game_id,),
        )
        hook.run(
            """INSERT INTO data_quality_alerts (game_id, alert_type, details)
               VALUES (%s, %s, %s)""",
            parameters=(game_id, "low_confidence", f"Confidence: {confidence:.2f}"),
        )
        alerts.append(game_id)

    # Check for games missing key dimensions
    missing_dims = hook.get_first(
        """SELECT COUNT(*) FROM games
           WHERE qdrant_indexed=TRUE AND (
               dim_pace IS NULL OR dim_tension IS NULL OR dim_agency IS NULL
           )"""
    )
    missing_count = missing_dims[0] if missing_dims else 0

    # Publish to Kafka
    producer = KafkaProducer(
        bootstrap_servers=KAFKA_SERVERS,
        value_serializer=lambda v: json.dumps(v).encode(),
    )
    producer.send("pipeline.health", {
        "dag": "daily_data_quality",
        "low_confidence_flagged": len(alerts),
        "missing_dimensions": missing_count,
        "status": "warning" if alerts or missing_count else "ok",
        "timestamp": datetime.utcnow().isoformat(),
    })
    producer.flush()

    logger.info(
        f"Data quality check: {len(alerts)} flagged for review, "
        f"{missing_count} missing dimensions"
    )


with DAG(
    dag_id="daily_data_quality",
    default_args=default_args,
    description="Check for low-confidence embeddings and flag for human review",
    schedule_interval="0 4 * * *",  # 4am daily
    start_date=datetime(2025, 1, 1),
    catchup=False,
    tags=["gamesoul", "quality"],
) as quality_dag:

    data_quality_check = PythonOperator(
        task_id="data_quality_check",
        python_callable=_check_data_quality,
    )
