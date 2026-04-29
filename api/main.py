"""
api/main.py
-----------
GameSoul FastAPI — cloud version.
- Kafka replaced with PostgreSQL event queue
- Airflow replaced with APScheduler (runs in-process)
- OpenAI replaces Ollama
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from urllib.parse import urlparse
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

import asyncpg
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from extraction.emotion_extractor import EmotionExtractor, EmotionVector, DIMENSIONS
from bandit.bandit import ThompsonBandit

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# ── Config ─────────────────────────────────────────────────────────────────
def _build_db_url() -> str:
    """Resolve DB URL with Railway-friendly fallbacks."""
    direct = os.getenv("DATABASE_URL") or os.getenv("DATABASE_PUBLIC_URL")
    if direct:
        return direct

    pg_host = os.getenv("PGHOST")
    if pg_host:
        pg_user = os.getenv("PGUSER", "postgres")
        pg_pass = os.getenv("PGPASSWORD", "")
        pg_port = os.getenv("PGPORT", "5432")
        pg_db = os.getenv("PGDATABASE", "postgres")
        return f"postgresql://{pg_user}:{pg_pass}@{pg_host}:{pg_port}/{pg_db}"

    return "postgresql://gamesoul:gamesoul@localhost:5432/gamesoul"


DB_URL = _build_db_url()
QDRANT_URL = os.getenv("QDRANT_URL", "")       # optional — falls back to DB search if empty
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
COLLECTION = "game_emotions"
TOP_K = 20

# ── Scheduler jobs ──────────────────────────────────────────────────────────

async def job_embed_new_games(db):
    """Process pending game.releases events and extract emotions."""
    if not db:
        return
    rows = await db.fetch(
        """SELECT id, payload FROM event_queue
           WHERE topic='game.releases' AND status='pending'
           ORDER BY created_at LIMIT 50
           FOR UPDATE SKIP LOCKED"""
    )
    if not rows:
        return

    extractor = EmotionExtractor()
    for row in rows:
        event_id = row["id"]
        payload = row["payload"]
        game_id = payload.get("game_id")
        try:
            game = await db.fetchrow(
                "SELECT id, name, description FROM games WHERE id=$1", game_id
            )
            if game:
                vec = extractor.from_description(game["description"] or game["name"], game["name"])
                dim_vals = {f"dim_{d}": getattr(vec, d) for d in DIMENSIONS}
                set_clause = ", ".join(f"{k}=${i+2}" for i, k in enumerate(dim_vals))
                vals = list(dim_vals.values()) + [vec.confidence, game_id]
                await db.execute(
                    f"UPDATE games SET {set_clause}, extraction_confidence=${len(dim_vals)+2} WHERE id=${len(dim_vals)+3}",
                    *vals,
                )
                # If Qdrant configured, upsert vector
                if QDRANT_URL:
                    await _upsert_qdrant(game_id, vec.to_list(), game["name"])

            await db.execute(
                "UPDATE event_queue SET status='done', processed_at=NOW() WHERE id=$1", event_id
            )
        except Exception as e:
            logger.error(f"embed_new_games failed for game {game_id}: {e}")
            await db.execute(
                "UPDATE event_queue SET status='failed', attempts=attempts+1 WHERE id=$1", event_id
            )

    logger.info(f"Embedded {len(rows)} new games")


async def job_data_quality(db):
    """Flag low-confidence games for review."""
    if not db:
        return
    count = await db.fetchval(
        """WITH updated AS (
               UPDATE games
               SET needs_review=TRUE
               WHERE extraction_confidence < 0.5
                 AND extraction_confidence IS NOT NULL
                 AND needs_review=FALSE
               RETURNING 1
           )
           SELECT COUNT(*) FROM updated"""
    )
    logger.info(f"Data quality: flagged {count or 0} games for review")
    await _record_job(db, "data_quality_check", "success")


async def job_bandit_retrain(db):
    """Reload bandit from last 30 days of ratings."""
    if not db:
        return
    rows = await db.fetch(
        """SELECT rec.input_mode,
                  COUNT(r.id) AS pulls,
                  SUM(CASE WHEN r.rating >= 4 OR r.thumbs_up THEN 1 ELSE 0 END) AS rewards
           FROM ratings r
           JOIN recommendations rec ON rec.id = r.recommendation_id
           WHERE r.created_at > NOW() - INTERVAL '30 days'
           GROUP BY rec.input_mode"""
    )
    for row in rows:
        alpha = float(row["rewards"] or 0) + 1
        beta = float(row["pulls"] - (row["rewards"] or 0)) + 1
        await db.execute(
            """INSERT INTO bandit_arms (arm_name, user_segment, alpha, beta, total_pulls, total_rewards)
               VALUES ($1, 'global', $2, $3, $4, $5)
               ON CONFLICT (arm_name, user_segment) DO UPDATE
               SET alpha=$2, beta=$3, total_pulls=$4, total_rewards=$5, updated_at=NOW()""",
            row["input_mode"], alpha, beta, int(row["pulls"]), float(row["rewards"] or 0),
        )
    logger.info(f"Bandit retrained on {len(rows)} arms")
    await _record_job(db, "bandit_retrain", "success")


async def _record_job(db, job_name: str, status: str):
    await db.execute(
        "UPDATE scheduled_jobs SET last_run_at=NOW(), last_status=$1 WHERE job_name=$2",
        status, job_name,
    )


async def _upsert_qdrant(game_id: int, vector: list[float], name: str):
    import httpx
    async with httpx.AsyncClient(timeout=10) as client:
        await client.put(
            f"{QDRANT_URL}/collections/{COLLECTION}/points",
            json={"points": [{"id": game_id, "vector": vector, "payload": {"game_id": game_id, "name": name}}]},
        )


# ── Lifespan ────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    import asyncio

    app.state.extractor = EmotionExtractor(openai_api_key=OPENAI_API_KEY)
    app.state.bandit = ThompsonBandit()
    app.state.db = None
    app.state.scheduler = None

    async def _connect_db():
        parsed = urlparse(DB_URL)
        logger.info(f"Attempting DB connect host={parsed.hostname} port={parsed.port}")
        for attempt in range(8):
            try:
                pool = await asyncpg.create_pool(DB_URL, min_size=1, max_size=10, timeout=10)
                rows = await pool.fetch(
                    "SELECT arm_name, user_segment, alpha, beta FROM bandit_arms"
                )
                app.state.bandit.load_from_db_rows([dict(r) for r in rows])
                app.state.db = pool
                logger.info("DB connected")
                return
            except Exception as e:
                logger.warning(f"DB connection attempt {attempt+1}/8 failed: {e}")
                await asyncio.sleep(4)

    # Connect in background so /health responds immediately during startup
    asyncio.create_task(_connect_db())

    # Start scheduler — jobs reference app.state.db at runtime so they work
    # even if DB connects after the scheduler starts
    try:
        scheduler = AsyncIOScheduler()
        scheduler.add_job(lambda: job_embed_new_games(app.state.db), "cron", hour=2)
        scheduler.add_job(lambda: job_data_quality(app.state.db),    "cron", hour=4)
        scheduler.add_job(lambda: job_bandit_retrain(app.state.db),  "cron", day=1, hour=3)
        scheduler.start()
        app.state.scheduler = scheduler
    except Exception as e:
        logger.warning(f"Scheduler failed to start: {e}")

    logger.info("GameSoul API started")
    yield

    if app.state.scheduler:
        app.state.scheduler.shutdown()
    if app.state.db:
        await app.state.db.close()


app = FastAPI(title="GameSoul API", version="2.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ── DB-based vector search (fallback when Qdrant not configured) ────────────

async def db_search(db, query_vector: list[float], limit: int = TOP_K) -> list[tuple[int, float]]:
    """
    Cosine similarity in pure SQL using the stored dim_* columns.
    Not as fast as Qdrant but works with zero extra infra.
    """
    dims = DIMENSIONS
    # Build dot product and magnitude expressions
    dot = " + ".join(f"COALESCE(dim_{d}, 5) * {query_vector[i]}" for i, d in enumerate(dims))
    mag_db = "SQRT(" + " + ".join(f"POWER(COALESCE(dim_{d}, 5), 2)" for d in dims) + ")"
    mag_q = sum(v**2 for v in query_vector) ** 0.5 or 1.0

    sql = f"""
        SELECT id,
               ({dot}) / (NULLIF({mag_db}, 0) * {mag_q}) AS score
        FROM games
        WHERE extraction_confidence IS NOT NULL
          AND extraction_confidence > 0
        ORDER BY score DESC
        LIMIT {limit}
    """
    rows = await db.fetch(sql)
    return [(row["id"], float(row["score"])) for row in rows]


async def qdrant_search(query_vector: list[float], limit: int = TOP_K) -> list[tuple[int, float]]:
    import httpx
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{QDRANT_URL}/collections/{COLLECTION}/points/search",
            json={"vector": query_vector, "limit": limit, "with_payload": True},
        )
        resp.raise_for_status()
        results = resp.json()["result"]
        return [(r["payload"]["game_id"], r["score"]) for r in results]


async def search_games(db, query_vector: list[float]) -> list[tuple[int, float]]:
    if QDRANT_URL:
        try:
            return await qdrant_search(query_vector)
        except Exception as e:
            logger.warning(f"Qdrant failed ({e}), falling back to DB search")
    return await db_search(db, query_vector)


# ── Event queue helpers (replaces Kafka) ────────────────────────────────────

async def publish(db, topic: str, payload: dict):
    await db.execute(
        "INSERT INTO event_queue (topic, payload) VALUES ($1, $2)",
        topic, json.dumps(payload),
    )


# ── Shared recommendation logic ─────────────────────────────────────────────

async def recommend(query_vec: EmotionVector, input_mode: str, session_id: str, request: Request):
    db = request.app.state.db
    if not db:
        raise HTTPException(503, "Database not ready yet — please retry in a few seconds")
    bandit = request.app.state.bandit
    vec_list = query_vec.to_list()

    candidates = await search_games(db, vec_list)
    if not candidates:
        raise HTTPException(404, "No games indexed yet. Run the indexing script first.")

    game_ids = [gid for gid, _ in candidates]
    scores = dict(candidates)

    rows = await db.fetch(
        "SELECT id, name, cover_url, " + ", ".join(f"dim_{d}" for d in DIMENSIONS) +
        " FROM games WHERE id = ANY($1::int[])", game_ids
    )
    games_map = {row["id"]: dict(row) for row in rows}

    selected = bandit.select(candidates, k=5, segment="global")

    recs = []
    for gid in selected:
        g = games_map.get(gid)
        if not g:
            continue
        game_vec = {d: g.get(f"dim_{d}") or 5.0 for d in DIMENSIONS}
        matches = [d for d in DIMENSIONS if abs(query_vec.to_dict()[d] - game_vec[d]) < 2.0 and query_vec.to_dict()[d] > 6]
        explanation = f"Matches your desire for {' and '.join(matches[:2])}." if matches else "Closely mirrors your emotional target."
        recs.append({
            "game_id": gid, "name": g["name"],
            "similarity_score": round(scores[gid], 3),
            "explanation": explanation,
            "cover_url": g.get("cover_url"),
            "emotion_vector": game_vec,
        })

    # Persist
    sid = uuid.UUID(session_id)
    exists = await db.fetchrow("SELECT 1 FROM user_sessions WHERE session_id=$1", sid)
    if not exists:
        await db.execute(
            "INSERT INTO user_sessions (session_id, input_mode) VALUES ($1, $2)", sid, input_mode
        )
    rec_id = await db.fetchval(
        """INSERT INTO recommendations (session_id, query_vector, input_mode, game_ids, similarity_scores)
           VALUES ($1, $2, $3, $4, $5) RETURNING id""",
        sid, vec_list, input_mode,
        [r["game_id"] for r in recs], [r["similarity_score"] for r in recs],
    )

    # Publish session event to queue
    await publish(db, "user.sessions", {"session_id": session_id, "input_mode": input_mode})

    return {
        "session_id": session_id,
        "recommendation_id": rec_id,
        "query_vector": query_vec.to_dict(),
        "recommendations": recs,
        "input_mode": input_mode,
    }


# ── Request models ──────────────────────────────────────────────────────────

class TextRequest(BaseModel):
    text: str
    session_id: Optional[str] = None

class VisualRequest(BaseModel):
    selected_image_ids: list[str]
    session_id: Optional[str] = None

class SoundRequest(BaseModel):
    selected_clip_ids: list[str]
    session_id: Optional[str] = None

class AnchorRequest(BaseModel):
    loved_game_id: int
    hated_game_id: int
    session_id: Optional[str] = None

class RatingRequest(BaseModel):
    session_id: str
    recommendation_id: int
    game_id: int
    rating: Optional[int] = Field(None, ge=1, le=5)
    thumbs_up: Optional[bool] = None


# ── Endpoints ───────────────────────────────────────────────────────────────

@app.post("/recommend/text")
async def recommend_text(req: TextRequest, request: Request):
    extractor = request.app.state.extractor
    vec = extractor.from_user_text(req.text)
    return await recommend(vec, "text", req.session_id or str(uuid.uuid4()), request)


@app.post("/recommend/visual")
async def recommend_visual(req: VisualRequest, request: Request):
    IMAGE_MAP = {
        "rainy_window":    EmotionVector(pace=2,tension=2,warmth=3,wonder=6,dread=3,beauty=8,agency=5,scale=2,rivalry=0),
        "crowded_market":  EmotionVector(pace=7,tension=5,warmth=6,wonder=5,dread=1,beauty=5,agency=6,scale=5,rivalry=4),
        "lone_mountain":   EmotionVector(pace=2,tension=3,warmth=2,wonder=8,dread=2,beauty=9,agency=7,scale=9,rivalry=0),
        "neon_city":       EmotionVector(pace=8,tension=6,warmth=2,wonder=6,dread=4,beauty=8,agency=7,scale=7,rivalry=5),
        "forest_campfire": EmotionVector(pace=1,tension=1,warmth=9,wonder=5,dread=0,beauty=7,agency=5,scale=3,rivalry=0),
        "storm_at_sea":    EmotionVector(pace=7,tension=8,warmth=1,wonder=7,dread=6,beauty=7,agency=6,scale=8,rivalry=2),
        "empty_desert":    EmotionVector(pace=1,tension=3,warmth=1,wonder=7,dread=4,beauty=6,agency=8,scale=9,rivalry=0),
        "arena_crowd":     EmotionVector(pace=9,tension=9,warmth=3,wonder=3,dread=2,beauty=4,agency=8,scale=5,rivalry=10),
        "cozy_library":    EmotionVector(pace=1,tension=1,warmth=8,wonder=7,dread=0,beauty=8,agency=6,scale=2,rivalry=0),
        "dark_corridor":   EmotionVector(pace=4,tension=7,warmth=1,wonder=4,dread=9,beauty=4,agency=6,scale=3,rivalry=0),
        "deep_space":      EmotionVector(pace=1,tension=3,warmth=1,wonder=9,dread=4,beauty=7,agency=5,scale=10,rivalry=0),
        "sunrise_peak":    EmotionVector(pace=3,tension=2,warmth=5,wonder=8,dread=0,beauty=9,agency=6,scale=7,rivalry=0),
    }
    vecs = [IMAGE_MAP[i] for i in req.selected_image_ids if i in IMAGE_MAP]
    if not vecs:
        raise HTTPException(400, "No recognized image IDs")
    vec = request.app.state.extractor.merge_weighted(vecs, [1.0]*len(vecs))
    return await recommend(vec, "visual", req.session_id or str(uuid.uuid4()), request)


@app.post("/recommend/sound")
async def recommend_sound(req: SoundRequest, request: Request):
    SOUND_MAP = {
        "rain_ambient":    EmotionVector(pace=1,tension=1,warmth=6,wonder=4,dread=1,beauty=7,agency=4,scale=2,rivalry=0),
        "battle_drums":    EmotionVector(pace=9,tension=9,warmth=1,wonder=2,dread=5,beauty=3,agency=8,scale=6,rivalry=7),
        "synthwave":       EmotionVector(pace=7,tension=5,warmth=3,wonder=6,dread=2,beauty=9,agency=7,scale=5,rivalry=3),
        "nature_birds":    EmotionVector(pace=1,tension=1,warmth=8,wonder=7,dread=0,beauty=8,agency=5,scale=4,rivalry=0),
        "deep_space_hum":  EmotionVector(pace=1,tension=3,warmth=1,wonder=9,dread=4,beauty=7,agency=5,scale=10,rivalry=0),
    }
    vecs = [SOUND_MAP[i] for i in req.selected_clip_ids if i in SOUND_MAP]
    if not vecs:
        raise HTTPException(400, "No recognized clip IDs")
    vec = request.app.state.extractor.merge_weighted(vecs, [1.0]*len(vecs))
    return await recommend(vec, "sound", req.session_id or str(uuid.uuid4()), request)


@app.post("/recommend/anchor")
async def recommend_anchor(req: AnchorRequest, request: Request):
    db = request.app.state.db
    cols = ", ".join(f"dim_{d}" for d in DIMENSIONS)
    loved = await db.fetchrow(f"SELECT {cols} FROM games WHERE id=$1", req.loved_game_id)
    hated = await db.fetchrow(f"SELECT {cols} FROM games WHERE id=$1", req.hated_game_id)
    if not loved or not hated:
        raise HTTPException(404, "Game not found")
    loved_vec = EmotionVector(**{d: loved[f"dim_{d}"] or 5.0 for d in DIMENSIONS})
    hated_vec = EmotionVector(**{d: hated[f"dim_{d}"] or 5.0 for d in DIMENSIONS})
    vec = loved_vec.contrast(hated_vec)
    return await recommend(vec, "anchor", req.session_id or str(uuid.uuid4()), request)


@app.post("/rate")
async def rate(req: RatingRequest, request: Request):
    db = request.app.state.db
    bandit = request.app.state.bandit
    await db.execute(
        "INSERT INTO ratings (session_id, recommendation_id, game_id, rating, thumbs_up) VALUES ($1,$2,$3,$4,$5)",
        uuid.UUID(req.session_id), req.recommendation_id, req.game_id, req.rating, req.thumbs_up,
    )
    rec = await db.fetchrow("SELECT input_mode FROM recommendations WHERE id=$1", req.recommendation_id)
    if rec:
        reward = 1.0 if req.thumbs_up or (req.rating and req.rating >= 4) else 0.0
        bandit.update(rec["input_mode"], reward)
        await db.execute(
            """UPDATE bandit_arms SET alpha=alpha+$1, beta=beta+$2,
               total_pulls=total_pulls+1, updated_at=NOW()
               WHERE arm_name=$3 AND user_segment='global'""",
            reward, 1.0 - reward, rec["input_mode"],
        )
        await publish(db, "user.feedback", {
            "session_id": req.session_id, "game_id": req.game_id,
            "rating": req.rating, "thumbs_up": req.thumbs_up, "input_mode": rec["input_mode"],
        })
    return {"status": "ok"}


@app.get("/games/search")
async def search(q: str, limit: int = 10, request: Request = None):
    db = request.app.state.db
    if not db:
        raise HTTPException(503, "Database not ready yet — please retry in a few seconds")
    rows = await db.fetch(
        "SELECT id, name, cover_url FROM games WHERE name ILIKE $1 LIMIT $2", f"%{q}%", limit
    )
    return [dict(r) for r in rows]


@app.get("/admin/ingest")
async def trigger_ingest(limit: int = 1000, request: Request = None):
    """Manually trigger game ingestion (call after deploy to seed DB)."""
    import asyncio
    asyncio.create_task(_run_ingest(request.app.state.db, limit))
    return {"status": "started", "limit": limit}


async def _run_ingest(db, limit: int):
    """Pull games from RAWG and extract emotions — runs in background."""
    logger.info(f"Background ingest started: {limit} games")
    if not db:
        logger.error("Ingest failed: database not ready")
        return

    # Reuse the existing asyncpg pool connection in Railway.
    try:
        from data.ingest import fetch_rawg_games

        insert_sql = """
            INSERT INTO games (
                rawg_id, igdb_id, name, slug, description, release_date, rating,
                ratings_count, genres, platforms, is_multiplayer, cover_url
            )
            VALUES (
                $1, $2, $3, $4, $5, $6, $7,
                $8, $9::text[], $10::text[], $11, $12
            )
            ON CONFLICT (rawg_id) DO UPDATE SET
                name = EXCLUDED.name,
                rating = EXCLUDED.rating,
                ratings_count = EXCLUDED.ratings_count,
                updated_at = NOW()
        """

        async with db.acquire() as conn:
            async with conn.transaction():
                for game in fetch_rawg_games(limit):
                    await conn.execute(
                        insert_sql,
                        game.get("rawg_id"),
                        game.get("igdb_id"),
                        game.get("name", ""),
                        game.get("slug", ""),
                        game.get("description", ""),
                        game.get("release_date"),
                        game.get("rating"),
                        game.get("ratings_count", 0),
                        game.get("genres", []),
                        game.get("platforms", []),
                        game.get("is_multiplayer", False),
                        game.get("cover_url"),
                    )

        logger.info(f"Ingest complete: {limit} games")
    except Exception as e:
        logger.error(f"Ingest failed: {e}")


@app.get("/health")
async def health(request: Request):
    """Instant response — no DB call so Railway healthcheck never times out."""
    db_ready = request.app.state.db is not None
    return {"status": "ok", "version": "2.0.0", "db_ready": db_ready}

@app.get("/")
async def root():
    return {"status": "ok", "service": "GameSoul API"}
