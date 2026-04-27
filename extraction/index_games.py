"""
extraction/index_games.py
--------------------------
Full pipeline: fetch games from DB → extract emotions via LLM → upsert to Qdrant.
Run this once to build the initial index, then let Airflow handle nightly updates.

Usage:
    python extraction/index_games.py --limit 5000 --source reviews
    python extraction/index_games.py --game-id 123  # single game
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import Optional

import psycopg2
from psycopg2.extras import execute_values
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct

sys.path.insert(0, os.path.dirname(__file__))
from emotion_extractor import EmotionExtractor, DIMENSIONS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DB_URL = os.getenv("DATABASE_URL", "postgresql://gamesoul:gamesoul@localhost:5432/gamesoul")
QDRANT_HOST = os.getenv("QDRANT_HOST", "localhost")
QDRANT_PORT = int(os.getenv("QDRANT_PORT", "6333"))
COLLECTION = "game_emotions"
CONFIDENCE_THRESHOLD = 0.5


def ensure_collection(client: QdrantClient):
    collections = client.get_collections().collections
    names = [c.name for c in collections]
    if COLLECTION not in names:
        client.create_collection(
            collection_name=COLLECTION,
            vectors_config=VectorParams(size=9, distance=Distance.COSINE),
        )
        logger.info(f"Created Qdrant collection '{COLLECTION}'")


def get_games_to_index(conn, limit: int, game_id: Optional[int] = None) -> list[dict]:
    with conn.cursor() as cur:
        if game_id:
            cur.execute(
                "SELECT id, name, description FROM games WHERE id = %s",
                (game_id,),
            )
        else:
            cur.execute(
                """SELECT id, name, description FROM games
                   WHERE qdrant_indexed = FALSE
                   ORDER BY ratings_count DESC NULLS LAST
                   LIMIT %s""",
                (limit,),
            )
        rows = cur.fetchall()
    return [{"id": r[0], "name": r[1], "description": r[2]} for r in rows]


def index_games(limit: int = 5000, game_id: Optional[int] = None, source: str = "description"):
    conn = psycopg2.connect(DB_URL)
    qdrant = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
    extractor = EmotionExtractor()
    ensure_collection(qdrant)

    games = get_games_to_index(conn, limit, game_id)
    logger.info(f"Found {len(games)} games to index")

    points = []
    updates = []

    for i, game in enumerate(games):
        try:
            if source == "reviews":
                # In production: load reviews from Steam
                reviews = game.get("reviews_text", game["description"] or "")
                vec = extractor.from_reviews(reviews or game["name"], game["name"])
            else:
                text = game["description"] or game["name"]
                vec = extractor.from_description(text, game["name"])

            # Weighted merge: description (0.3) is the source here
            # In full pipeline: reviews(0.6) + description(0.3) + metadata(0.1)
            dim_updates = {f"dim_{d}": getattr(vec, d) for d in DIMENSIONS}
            dim_updates["extraction_confidence"] = vec.confidence
            dim_updates["extraction_source"] = source
            dim_updates["needs_review"] = vec.confidence < CONFIDENCE_THRESHOLD

            updates.append((game["id"], dim_updates))

            if vec.confidence >= 0.1:
                points.append(PointStruct(
                    id=game["id"],
                    vector=vec.to_list(),
                    payload={"game_id": game["id"], "name": game["name"]},
                ))

            if (i + 1) % 50 == 0:
                _flush_batch(conn, qdrant, points, updates)
                points = []
                updates = []
                logger.info(f"Indexed {i+1}/{len(games)} games")

        except Exception as e:
            logger.error(f"Failed to index game {game['id']} ({game['name']}): {e}")

    if points or updates:
        _flush_batch(conn, qdrant, points, updates)

    logger.info(f"Indexing complete. Total: {len(games)} games")
    conn.close()


def _flush_batch(conn, qdrant, points, updates):
    """Upsert vectors to Qdrant and update DB."""
    if points:
        qdrant.upsert(collection_name=COLLECTION, points=points)

    with conn.cursor() as cur:
        for game_id, dim_updates in updates:
            set_parts = ", ".join(f"{k} = %s" for k in dim_updates)
            values = list(dim_updates.values()) + [game_id]
            cur.execute(
                f"UPDATE games SET {set_parts}, qdrant_indexed=TRUE WHERE id = %s",
                values,
            )
    conn.commit()


def validate_index(known_games: list[dict] = None):
    """Spot-check the index against known game profiles."""
    qdrant = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
    conn = psycopg2.connect(DB_URL)

    # Default validation set
    if not known_games:
        known_games = [
            {"name": "Stardew Valley", "expected": {"pace": 1, "warmth": 9, "tension": 1}},
            {"name": "Dark Souls", "expected": {"tension": 9, "dread": 7, "agency": 8}},
            {"name": "Minecraft", "expected": {"agency": 10, "wonder": 9, "scale": 10}},
        ]

    logger.info("\n=== Index Validation ===")
    with conn.cursor() as cur:
        for g in known_games:
            cur.execute(
                "SELECT id, dim_pace, dim_tension, dim_agency, dim_warmth FROM games WHERE name ILIKE %s",
                (f"%{g['name']}%",),
            )
            row = cur.fetchone()
            if row:
                logger.info(
                    f"{g['name']}: pace={row[1]:.1f}, tension={row[2]:.1f}, "
                    f"agency={row[3]:.1f}, warmth={row[4]:.1f}"
                )
            else:
                logger.warning(f"{g['name']}: NOT FOUND in DB")

    conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=5000)
    parser.add_argument("--game-id", type=int)
    parser.add_argument("--source", choices=["description", "reviews"], default="description")
    parser.add_argument("--validate", action="store_true")
    args = parser.parse_args()

    if args.validate:
        validate_index()
    else:
        index_games(args.limit, args.game_id, args.source)
