"""
kafka/producers.py
------------------
Kafka producers for GameSoul events.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Optional

from kafka import KafkaProducer as _KafkaProducer
from kafka.errors import KafkaError

logger = logging.getLogger(__name__)

KAFKA_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")

TOPICS = {
    "game_releases": "game.releases",
    "user_feedback": "user.feedback",
    "user_sessions": "user.sessions",
    "pipeline_health": "pipeline.health",
}


def _make_producer() -> _KafkaProducer:
    return _KafkaProducer(
        bootstrap_servers=KAFKA_SERVERS,
        value_serializer=lambda v: json.dumps(v).encode("utf-8"),
        key_serializer=lambda k: k.encode("utf-8") if k else None,
        acks="all",
        retries=3,
    )


def publish_game_release(game_id: int, name: str, source: str = "rawg"):
    """Publish a new game release event."""
    producer = _make_producer()
    try:
        producer.send(
            TOPICS["game_releases"],
            key=str(game_id),
            value={
                "game_id": game_id,
                "name": name,
                "source": source,
                "timestamp": datetime.utcnow().isoformat(),
            },
        )
        producer.flush()
        logger.info(f"Published game.releases for game {game_id}: {name}")
    except KafkaError as e:
        logger.error(f"Failed to publish game release: {e}")
    finally:
        producer.close()


def publish_user_feedback(
    session_id: str,
    recommendation_id: int,
    game_id: int,
    rating: Optional[int],
    thumbs_up: Optional[bool],
    input_mode: str,
):
    """Publish a user rating event."""
    producer = _make_producer()
    try:
        producer.send(
            TOPICS["user_feedback"],
            key=session_id,
            value={
                "session_id": session_id,
                "recommendation_id": recommendation_id,
                "game_id": game_id,
                "rating": rating,
                "thumbs_up": thumbs_up,
                "input_mode": input_mode,
                "timestamp": datetime.utcnow().isoformat(),
            },
        )
        producer.flush()
    except KafkaError as e:
        logger.error(f"Failed to publish user feedback: {e}")
    finally:
        producer.close()


def publish_session_event(session_id: str, ab_variants: dict, input_mode: str):
    """Publish an anonymous session event for A/B tracking."""
    producer = _make_producer()
    try:
        producer.send(
            TOPICS["user_sessions"],
            key=session_id,
            value={
                "session_id": session_id,
                "ab_variants": ab_variants,
                "input_mode": input_mode,
                "timestamp": datetime.utcnow().isoformat(),
            },
        )
        producer.flush()
    except KafkaError as e:
        logger.error(f"Failed to publish session event: {e}")
    finally:
        producer.close()


def publish_pipeline_health(dag_id: str, status: str, details: dict = None):
    """Publish a pipeline health alert."""
    producer = _make_producer()
    try:
        producer.send(
            TOPICS["pipeline_health"],
            value={
                "dag_id": dag_id,
                "status": status,
                "details": details or {},
                "timestamp": datetime.utcnow().isoformat(),
            },
        )
        producer.flush()
    except KafkaError as e:
        logger.error(f"Failed to publish pipeline health: {e}")
    finally:
        producer.close()
