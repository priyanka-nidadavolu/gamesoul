"""
data/ingest.py
--------------
Pulls games from RAWG and IGDB APIs and stores them in PostgreSQL.
Also handles Steam review fetching for the top games.

Usage:
    python data/ingest.py --source rawg --limit 5000
    python data/ingest.py --source igdb --limit 2000
    python data/ingest.py --source steam-reviews --game-ids 570,730,440
"""

from __future__ import annotations

import argparse
import logging
import os
import time
from typing import Generator

import httpx
import psycopg2
from psycopg2.extras import execute_values

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DB_URL = os.getenv("DATABASE_URL", "postgresql://gamesoul:gamesoul@localhost:5432/gamesoul")
RAWG_API_KEY = os.getenv("RAWG_API_KEY", "")
IGDB_CLIENT_ID = os.getenv("IGDB_CLIENT_ID", "")
IGDB_CLIENT_SECRET = os.getenv("IGDB_CLIENT_SECRET", "")


# ── RAWG Ingestion ────────────────────────────────────────────────────────────

def fetch_rawg_games(limit: int = 50000) -> Generator[dict, None, None]:
    """Paginate RAWG API and yield game dicts."""
    page = 1
    page_size = 40
    fetched = 0

    with httpx.Client(timeout=30) as client:
        while fetched < limit:
            params = {
                "key": RAWG_API_KEY,
                "page": page,
                "page_size": min(page_size, limit - fetched),
                "ordering": "-ratings_count",
            }
            resp = client.get("https://api.rawg.io/api/games", params=params)
            if resp.status_code != 200:
                logger.error(f"RAWG error {resp.status_code}: {resp.text[:200]}")
                break

            data = resp.json()
            results = data.get("results", [])
            if not results:
                break

            for game in results:
                yield {
                    "rawg_id": game["id"],
                    "name": game.get("name", ""),
                    "slug": game.get("slug", ""),
                    "release_date": game.get("released"),
                    "rating": game.get("rating"),
                    "ratings_count": game.get("ratings_count", 0),
                    "genres": [g["name"] for g in game.get("genres", [])],
                    "platforms": [p["platform"]["name"] for p in game.get("platforms", []) or []],
                    "cover_url": game.get("background_image"),
                }
                fetched += 1

            logger.info(f"RAWG: fetched {fetched}/{limit} games (page {page})")
            page += 1
            time.sleep(0.25)  # respect rate limits


def fetch_igdb_token() -> str:
    """Get OAuth token for IGDB."""
    resp = httpx.post(
        "https://id.twitch.tv/oauth2/token",
        params={
            "client_id": IGDB_CLIENT_ID,
            "client_secret": IGDB_CLIENT_SECRET,
            "grant_type": "client_credentials",
        },
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def fetch_igdb_games(limit: int = 2000) -> Generator[dict, None, None]:
    """Paginate IGDB API and yield game dicts."""
    token = fetch_igdb_token()
    headers = {
        "Client-ID": IGDB_CLIENT_ID,
        "Authorization": f"Bearer {token}",
    }
    offset = 0
    batch_size = 500

    with httpx.Client(timeout=30, headers=headers) as client:
        while offset < limit:
            body = f"""
                fields id, name, summary, first_release_date, rating, rating_count,
                       genres.name, platforms.name, multiplayer_modes.*;
                sort rating_count desc;
                limit {min(batch_size, limit - offset)};
                offset {offset};
            """
            resp = client.post("https://api.igdb.com/v4/games", content=body)
            if resp.status_code != 200:
                logger.error(f"IGDB error: {resp.text[:200]}")
                break

            games = resp.json()
            if not games:
                break

            for game in games:
                is_multiplayer = bool(game.get("multiplayer_modes"))
                release_ts = game.get("first_release_date")
                release_date = None
                if release_ts:
                    import datetime
                    release_date = datetime.date.fromtimestamp(release_ts).isoformat()

                yield {
                    "igdb_id": game["id"],
                    "name": game.get("name", ""),
                    "description": game.get("summary", ""),
                    "release_date": release_date,
                    "rating": game.get("rating"),
                    "ratings_count": game.get("rating_count", 0),
                    "genres": [g["name"] for g in game.get("genres", [])],
                    "platforms": [p["name"] for p in game.get("platforms", [])],
                    "is_multiplayer": is_multiplayer,
                }

            offset += batch_size
            logger.info(f"IGDB: fetched {offset}/{limit} games")
            time.sleep(0.25)


def fetch_steam_reviews(app_id: int, max_reviews: int = 200) -> str:
    """Fetch and concatenate top Steam reviews for a game."""
    reviews_text = []
    cursor = "*"
    fetched = 0

    with httpx.Client(timeout=30) as client:
        while fetched < max_reviews:
            params = {
                "json": 1,
                "language": "english",
                "review_type": "positive",
                "num_per_page": min(100, max_reviews - fetched),
                "cursor": cursor,
                "purchase_type": "all",
            }
            resp = client.get(
                f"https://store.steampowered.com/appreviews/{app_id}",
                params=params,
            )
            if resp.status_code != 200:
                break

            data = resp.json()
            if not data.get("success"):
                break

            for review in data.get("reviews", []):
                text = review.get("review", "").strip()
                votes = review.get("votes_up", 0)
                if text and len(text) > 50:
                    # Weight high-voted reviews by repeating them
                    weight = min(3, 1 + votes // 500)
                    reviews_text.extend([text] * weight)
                    fetched += 1

            cursor = data.get("cursor", "")
            if not cursor or not data.get("reviews"):
                break

    return "\n\n---\n\n".join(reviews_text[:max_reviews])


# ── Database Helpers ─────────────────────────────────────────────────────────

def upsert_games(games: list[dict], conn) -> int:
    """Upsert game records. Returns count inserted/updated."""
    if not games:
        return 0

    cols = [
        "rawg_id", "igdb_id", "name", "slug", "description",
        "release_date", "rating", "ratings_count", "genres",
        "platforms", "is_multiplayer", "cover_url",
    ]

    rows = []
    for g in games:
        row = tuple(g.get(c) for c in cols)
        rows.append(row)

    sql = f"""
        INSERT INTO games ({", ".join(cols)})
        VALUES %s
        ON CONFLICT (rawg_id) DO UPDATE SET
            name = EXCLUDED.name,
            rating = EXCLUDED.rating,
            ratings_count = EXCLUDED.ratings_count,
            updated_at = NOW()
        RETURNING id
    """
    with conn.cursor() as cur:
        execute_values(cur, sql, rows)
        result = cur.rowcount
    conn.commit()
    return result


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="GameSoul data ingestion")
    parser.add_argument("--source", choices=["rawg", "igdb", "steam-reviews"], required=True)
    parser.add_argument("--limit", type=int, default=5000)
    parser.add_argument("--game-ids", type=str, help="Comma-separated Steam app IDs")
    args = parser.parse_args()

    conn = psycopg2.connect(DB_URL)

    if args.source == "rawg":
        batch = []
        for game in fetch_rawg_games(args.limit):
            batch.append(game)
            if len(batch) >= 100:
                upsert_games(batch, conn)
                batch = []
        if batch:
            upsert_games(batch, conn)
        logger.info("RAWG ingestion complete")

    elif args.source == "igdb":
        batch = []
        for game in fetch_igdb_games(args.limit):
            batch.append(game)
            if len(batch) >= 100:
                upsert_games(batch, conn)
                batch = []
        if batch:
            upsert_games(batch, conn)
        logger.info("IGDB ingestion complete")

    elif args.source == "steam-reviews":
        if not args.game_ids:
            print("--game-ids required for steam-reviews")
            return
        for app_id in args.game_ids.split(","):
            app_id = int(app_id.strip())
            reviews = fetch_steam_reviews(app_id)
            logger.info(f"Steam app {app_id}: {len(reviews)} chars of reviews")
            # Store in DB or file for extraction pipeline
            with open(f"/tmp/steam_reviews_{app_id}.txt", "w") as f:
                f.write(reviews)

    conn.close()


if __name__ == "__main__":
    main()
