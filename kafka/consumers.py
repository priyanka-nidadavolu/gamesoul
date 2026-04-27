"""
kafka/consumers.py
------------------
Kafka consumers for GameSoul. Runs as long-running processes.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
from datetime import datetime

import psycopg2
from kafka import KafkaConsumer
from kafka.errors import KafkaError

logger = logging.getLogger(__name__)

KAFKA_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
DB_URL = os.getenv("DATABASE_URL", "postgresql://gamesoul:gamesoul@localhost:5432/gamesoul")

running = True


def _signal_handler(sig, frame):
    global running
    logger.info("Shutting down consumer...")
    running = False


signal.signal(signal.SIGTERM, _signal_handler)
signal.signal(signal.SIGINT, _signal_handler)


def consume_feedback():
    """
    Consume user.feedback topic and update bandit priors in real-time.
    """
    logger.info("Starting user.feedback consumer")
    conn = psycopg2.connect(DB_URL)

    consumer = KafkaConsumer(
        "user.feedback",
        bootstrap_servers=KAFKA_SERVERS,
        group_id="feedback_bandit_updater",
        auto_offset_reset="latest",
        enable_auto_commit=True,
        value_deserializer=lambda m: json.loads(m.decode("utf-8")),
    )

    while running:
        batch = consumer.poll(timeout_ms=1000)
        for tp, messages in batch.items():
            for msg in messages:
                try:
                    _process_feedback(msg.value, conn)
                except Exception as e:
                    logger.error(f"Error processing feedback: {e}", exc_info=True)

    consumer.close()
    conn.close()


def _process_feedback(event: dict, conn):
    """Update bandit arm statistics based on feedback event."""
    input_mode = event.get("input_mode")
    rating = event.get("rating")
    thumbs_up = event.get("thumbs_up")

    if not input_mode:
        return

    reward = 0.0
    if thumbs_up is True:
        reward = 1.0
    elif thumbs_up is False:
        reward = 0.0
    elif rating is not None:
        reward = max(0.0, (rating - 1) / 4.0)  # normalize 1-5 to 0-1

    with conn.cursor() as cur:
        cur.execute(
            """UPDATE bandit_arms
               SET alpha = alpha + %s,
                   beta = beta + %s,
                   total_pulls = total_pulls + 1,
                   total_rewards = total_rewards + %s,
                   updated_at = NOW()
               WHERE arm_name = %s AND user_segment = 'global'""",
            (reward, 1.0 - reward, reward, input_mode),
        )
    conn.commit()
    logger.debug(f"Updated bandit arm '{input_mode}': reward={reward:.2f}")


def consume_game_releases():
    """
    Consume game.releases topic and insert new games into the DB.
    Triggers the embedding pipeline via a flag.
    """
    logger.info("Starting game.releases consumer")
    conn = psycopg2.connect(DB_URL)

    consumer = KafkaConsumer(
        "game.releases",
        bootstrap_servers=KAFKA_SERVERS,
        group_id="new_game_ingestor",
        auto_offset_reset="latest",
        enable_auto_commit=True,
        value_deserializer=lambda m: json.loads(m.decode("utf-8")),
    )

    while running:
        batch = consumer.poll(timeout_ms=1000)
        for tp, messages in batch.items():
            for msg in messages:
                event = msg.value
                game_id = event.get("game_id")
                name = event.get("name", "")
                if game_id:
                    with conn.cursor() as cur:
                        cur.execute(
                            """INSERT INTO games (rawg_id, name)
                               VALUES (%s, %s)
                               ON CONFLICT (rawg_id) DO NOTHING""",
                            (game_id, name),
                        )
                    conn.commit()
                    logger.info(f"Ingested new game from Kafka: {name} ({game_id})")

    consumer.close()
    conn.close()


if __name__ == "__main__":
    import threading

    mode = sys.argv[1] if len(sys.argv) > 1 else "feedback"
    if mode == "feedback":
        consume_feedback()
    elif mode == "releases":
        consume_game_releases()
    elif mode == "all":
        t1 = threading.Thread(target=consume_feedback, daemon=True)
        t2 = threading.Thread(target=consume_game_releases, daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
