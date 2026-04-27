"""
api/main.py
-----------
FastAPI gateway for GameSoul. Handles the four input modes,
calls the emotion engine, queries Qdrant, applies the bandit,
and returns ranked recommendations.
"""

from __future__ import annotations

import logging
import os
import uuid
from contextlib import asynccontextmanager
from typing import Optional

import asyncpg
from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct, Filter, FieldCondition

from emotion_extractor import EmotionExtractor, EmotionVector, DIMENSIONS
from bandit import ThompsonBandit

logger = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────

DB_URL = os.getenv("DATABASE_URL", "postgresql://gamesoul:gamesoul@localhost:5432/gamesoul")
QDRANT_HOST = os.getenv("QDRANT_HOST", "localhost")
QDRANT_PORT = int(os.getenv("QDRANT_PORT", "6333"))
COLLECTION = "game_emotions"
TOP_K_RETRIEVE = 20  # retrieve 20, bandit selects 5

# ── Lifespan (startup/shutdown) ─────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    app.state.db = await asyncpg.create_pool(DB_URL, min_size=2, max_size=10)
    app.state.qdrant = AsyncQdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
    app.state.extractor = EmotionExtractor()
    app.state.bandit = ThompsonBandit()
    # Ensure Qdrant collection exists
    await ensure_collection(app.state.qdrant)
    logger.info("GameSoul API started")
    yield
    # Shutdown
    await app.state.db.close()
    await app.state.qdrant.close()


app = FastAPI(
    title="GameSoul API",
    description="Emotion-driven game discovery",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


async def ensure_collection(qdrant: AsyncQdrantClient):
    collections = await qdrant.get_collections()
    names = [c.name for c in collections.collections]
    if COLLECTION not in names:
        await qdrant.create_collection(
            collection_name=COLLECTION,
            vectors_config=VectorParams(size=9, distance=Distance.COSINE),
        )
        logger.info(f"Created Qdrant collection '{COLLECTION}'")


# ── Request / Response Models ───────────────────────────────────────────────

class TextInputRequest(BaseModel):
    text: str = Field(..., description="Free text describing desired emotional state")
    session_id: Optional[str] = None
    use_openai: bool = False


class AnchorInputRequest(BaseModel):
    loved_game_id: int = Field(..., description="ID of a game the user loved")
    hated_game_id: int = Field(..., description="ID of a game the user disliked")
    session_id: Optional[str] = None


class VisualInputRequest(BaseModel):
    """Visual mood picker: image tags map to emotion dimension weights."""
    selected_image_ids: list[str] = Field(..., description="IDs of selected mood images")
    session_id: Optional[str] = None


class SoundInputRequest(BaseModel):
    """Sound check: selected audio clips map to emotion vectors."""
    selected_clip_ids: list[str] = Field(..., description="IDs of selected audio clips")
    session_id: Optional[str] = None


class RatingRequest(BaseModel):
    session_id: str
    recommendation_id: int
    game_id: int
    rating: Optional[int] = Field(None, ge=1, le=5)
    thumbs_up: Optional[bool] = None


class GameRecommendation(BaseModel):
    game_id: int
    name: str
    similarity_score: float
    explanation: str
    cover_url: Optional[str]
    emotion_vector: dict


class RecommendationResponse(BaseModel):
    session_id: str
    recommendation_id: int
    query_vector: dict
    recommendations: list[GameRecommendation]
    input_mode: str


# ── Dependency Injection ────────────────────────────────────────────────────

def get_db(request):
    return request.app.state.db

def get_qdrant(request):
    return request.app.state.qdrant

def get_extractor(request):
    return request.app.state.extractor

def get_bandit(request):
    return request.app.state.bandit


# ── Core Recommendation Logic ───────────────────────────────────────────────

async def recommend(
    query_vector: EmotionVector,
    input_mode: str,
    db,
    qdrant: AsyncQdrantClient,
    bandit: ThompsonBandit,
    session_id: str,
    use_popularity: bool = False,
) -> RecommendationResponse:
    """Core recommendation: vector search → bandit selection → persist."""
    vec_list = query_vector.to_list()

    # 1. Qdrant nearest-neighbor search
    results = await qdrant.search(
        collection_name=COLLECTION,
        query_vector=vec_list,
        limit=TOP_K_RETRIEVE,
        with_payload=True,
    )

    if not results:
        raise HTTPException(status_code=404, detail="No games indexed yet")

    # 2. Fetch game details from PostgreSQL
    game_ids = [r.payload["game_id"] for r in results]
    scores = {r.payload["game_id"]: r.score for r in results}

    games_rows = await db.fetch(
        "SELECT id, name, cover_url, dim_pace, dim_tension, dim_agency, dim_warmth, "
        "dim_scale, dim_beauty, dim_dread, dim_wonder, dim_rivalry "
        "FROM games WHERE id = ANY($1::int[])",
        game_ids,
    )
    games_map = {row["id"]: dict(row) for row in games_rows}

    # 3. Optional popularity re-rank (Experiment 3)
    candidates = []
    for gid in game_ids:
        if gid not in games_map:
            continue
        score = scores[gid]
        if use_popularity:
            pop_row = await db.fetchrow(
                "SELECT rating FROM games WHERE id=$1", gid
            )
            pop_score = (pop_row["rating"] or 5.0) / 10.0
            score = score * 0.9 + pop_score * 0.1
        candidates.append((gid, score))

    candidates.sort(key=lambda x: x[1], reverse=True)

    # 4. Bandit selects top 5 from top 20
    selected_ids = bandit.select(candidates[:TOP_K_RETRIEVE], k=5, segment="global")

    # 5. Build recommendations with explanations
    recommendations = []
    for gid in selected_ids:
        g = games_map.get(gid)
        if not g:
            continue
        game_vec = {d: g.get(f"dim_{d}", 5.0) for d in DIMENSIONS}
        explanation = _build_explanation(query_vector.to_dict(), game_vec, g["name"])
        recommendations.append(
            GameRecommendation(
                game_id=gid,
                name=g["name"],
                similarity_score=round(scores[gid], 3),
                explanation=explanation,
                cover_url=g.get("cover_url"),
                emotion_vector=game_vec,
            )
        )

    # 6. Persist session, recommendation
    async with db.transaction():
        session_exists = await db.fetchrow(
            "SELECT session_id FROM user_sessions WHERE session_id=$1",
            uuid.UUID(session_id),
        )
        if not session_exists:
            await db.execute(
                "INSERT INTO user_sessions (session_id, input_mode) VALUES ($1, $2)",
                uuid.UUID(session_id), input_mode,
            )

        rec_id = await db.fetchval(
            """INSERT INTO recommendations
               (session_id, query_vector, input_mode, game_ids, similarity_scores)
               VALUES ($1, $2, $3, $4, $5)
               RETURNING id""",
            uuid.UUID(session_id),
            vec_list,
            input_mode,
            [r.game_id for r in recommendations],
            [r.similarity_score for r in recommendations],
        )

    return RecommendationResponse(
        session_id=session_id,
        recommendation_id=rec_id,
        query_vector=query_vector.to_dict(),
        recommendations=recommendations,
        input_mode=input_mode,
    )


def _build_explanation(query: dict, game: dict, game_name: str) -> str:
    """Generate a 1-line emotional match explanation."""
    # Find the top 2 matching dimensions
    matches = []
    for d in DIMENSIONS:
        q_val = query.get(d, 5)
        g_val = game.get(d, 5)
        if abs(q_val - g_val) < 2.0 and (q_val > 6 or q_val < 4):
            matches.append(d)

    dim_labels = {
        "pace": "intense pace",
        "tension": "high-stakes tension",
        "agency": "player agency",
        "warmth": "emotional warmth",
        "scale": "epic scale",
        "beauty": "artistic beauty",
        "dread": "atmospheric dread",
        "wonder": "sense of wonder",
        "rivalry": "competitive rivalry",
    }

    if matches:
        top = [dim_labels[m] for m in matches[:2]]
        return f"Matches your desire for {' and '.join(top)}."
    return f"Closely mirrors your emotional target across multiple dimensions."


# ── Endpoints ──────────────────────────────────────────────────────────────

@app.post("/recommend/text", response_model=RecommendationResponse)
async def recommend_from_text(req: TextInputRequest, request=None):
    """Input mode 1: Free text emotion description."""
    from fastapi import Request
    extractor: EmotionExtractor = request.app.state.extractor
    db = request.app.state.db
    qdrant = request.app.state.qdrant
    bandit = request.app.state.bandit

    # A/B: route to OpenAI or Ollama
    if req.use_openai:
        extractor.openai_api_key = os.getenv("OPENAI_API_KEY", "")
    query_vec = extractor.from_user_text(req.text)
    session_id = req.session_id or str(uuid.uuid4())
    return await recommend(query_vec, "text", db, qdrant, bandit, session_id)


@app.post("/recommend/anchor", response_model=RecommendationResponse)
async def recommend_from_anchor(req: AnchorInputRequest, request=None):
    """Input mode 4: Love it / hate it contrast."""
    from fastapi import Request
    db = request.app.state.db
    qdrant = request.app.state.qdrant
    bandit = request.app.state.bandit

    # Load emotion vectors for both games
    loved_row = await db.fetchrow(
        "SELECT dim_pace, dim_tension, dim_agency, dim_warmth, dim_scale, "
        "dim_beauty, dim_dread, dim_wonder, dim_rivalry FROM games WHERE id=$1",
        req.loved_game_id,
    )
    hated_row = await db.fetchrow(
        "SELECT dim_pace, dim_tension, dim_agency, dim_warmth, dim_scale, "
        "dim_beauty, dim_dread, dim_wonder, dim_rivalry FROM games WHERE id=$1",
        req.hated_game_id,
    )

    if not loved_row or not hated_row:
        raise HTTPException(status_code=404, detail="Game not found")

    loved_vec = EmotionVector(**{d: loved_row[f"dim_{d}"] or 5.0 for d in DIMENSIONS})
    hated_vec = EmotionVector(**{d: hated_row[f"dim_{d}"] or 5.0 for d in DIMENSIONS})
    query_vec = loved_vec.contrast(hated_vec)

    session_id = req.session_id or str(uuid.uuid4())
    return await recommend(query_vec, "anchor", db, qdrant, bandit, session_id)


@app.post("/recommend/visual", response_model=RecommendationResponse)
async def recommend_from_visual(req: VisualInputRequest, request=None):
    """Input mode 2: Visual mood picker."""
    # Visual image → emotion vector mapping
    IMAGE_EMOTION_MAP = {
        "rainy_window":    EmotionVector(pace=2, tension=2, warmth=3, wonder=6, dread=3, beauty=8, agency=5, scale=2, rivalry=0),
        "crowded_market":  EmotionVector(pace=7, tension=5, warmth=6, wonder=5, dread=1, beauty=5, agency=6, scale=5, rivalry=4),
        "lone_mountain":   EmotionVector(pace=2, tension=3, warmth=2, wonder=8, dread=2, beauty=9, agency=7, scale=9, rivalry=0),
        "neon_city":       EmotionVector(pace=8, tension=6, warmth=2, wonder=6, dread=4, beauty=8, agency=7, scale=7, rivalry=5),
        "forest_campfire": EmotionVector(pace=1, tension=1, warmth=9, wonder=5, dread=0, beauty=7, agency=5, scale=3, rivalry=0),
        "storm_at_sea":    EmotionVector(pace=7, tension=8, warmth=1, wonder=7, dread=6, beauty=7, agency=6, scale=8, rivalry=2),
        "empty_desert":    EmotionVector(pace=1, tension=3, warmth=1, wonder=7, dread=4, beauty=6, agency=8, scale=9, rivalry=0),
        "arena_crowd":     EmotionVector(pace=9, tension=9, warmth=3, wonder=3, dread=2, beauty=4, agency=8, scale=5, rivalry=10),
        "cozy_library":    EmotionVector(pace=1, tension=1, warmth=8, wonder=7, dread=0, beauty=8, agency=6, scale=2, rivalry=0),
        "dark_corridor":   EmotionVector(pace=4, tension=7, warmth=1, wonder=4, dread=9, beauty=4, agency=6, scale=3, rivalry=0),
    }

    vecs = [IMAGE_EMOTION_MAP.get(img_id, EmotionVector()) for img_id in req.selected_image_ids]
    if not vecs:
        raise HTTPException(status_code=400, detail="No recognized image IDs")

    weights = [1.0] * len(vecs)
    extractor = request.app.state.extractor
    query_vec = extractor.merge_weighted(vecs, weights)

    session_id = req.session_id or str(uuid.uuid4())
    return await recommend(
        query_vec, "visual",
        request.app.state.db, request.app.state.qdrant, request.app.state.bandit,
        session_id,
    )


@app.post("/recommend/sound", response_model=RecommendationResponse)
async def recommend_from_sound(req: SoundInputRequest, request=None):
    """Input mode 3: Sound check."""
    SOUND_EMOTION_MAP = {
        "rain_ambient":    EmotionVector(pace=1, tension=1, warmth=6, wonder=4, dread=1, beauty=7, agency=4, scale=2, rivalry=0),
        "battle_drums":    EmotionVector(pace=9, tension=9, warmth=1, wonder=2, dread=5, beauty=3, agency=8, scale=6, rivalry=7),
        "synthwave":       EmotionVector(pace=7, tension=5, warmth=3, wonder=6, dread=2, beauty=9, agency=7, scale=5, rivalry=3),
        "nature_birds":    EmotionVector(pace=1, tension=1, warmth=8, wonder=7, dread=0, beauty=8, agency=5, scale=4, rivalry=0),
        "deep_space_hum":  EmotionVector(pace=1, tension=3, warmth=1, wonder=9, dread=4, beauty=7, agency=5, scale=10, rivalry=0),
    }

    vecs = [SOUND_EMOTION_MAP.get(clip_id, EmotionVector()) for clip_id in req.selected_clip_ids]
    if not vecs:
        raise HTTPException(status_code=400, detail="No recognized clip IDs")

    extractor = request.app.state.extractor
    query_vec = extractor.merge_weighted(vecs, [1.0] * len(vecs))
    session_id = req.session_id or str(uuid.uuid4())
    return await recommend(
        query_vec, "sound",
        request.app.state.db, request.app.state.qdrant, request.app.state.bandit,
        session_id,
    )


@app.post("/rate")
async def submit_rating(req: RatingRequest, request=None):
    """Submit a rating for a recommendation."""
    db = request.app.state.db
    bandit = request.app.state.bandit

    await db.execute(
        """INSERT INTO ratings (session_id, recommendation_id, game_id, rating, thumbs_up)
           VALUES ($1, $2, $3, $4, $5)""",
        uuid.UUID(req.session_id), req.recommendation_id,
        req.game_id, req.rating, req.thumbs_up,
    )

    # Update bandit in real-time
    rec_row = await db.fetchrow(
        "SELECT input_mode FROM recommendations WHERE id=$1", req.recommendation_id
    )
    if rec_row:
        reward = 1.0 if req.thumbs_up or (req.rating and req.rating >= 4) else 0.0
        bandit.update(arm=rec_row["input_mode"], reward=reward, segment="global")
        # Persist bandit state
        await db.execute(
            """UPDATE bandit_arms
               SET alpha = alpha + $1, beta = beta + $2,
                   total_pulls = total_pulls + 1, total_rewards = total_rewards + $1,
                   updated_at = NOW()
               WHERE arm_name = $3 AND user_segment = 'global'""",
            reward, 1.0 - reward, rec_row["input_mode"],
        )

    return {"status": "ok"}


@app.get("/games/search")
async def search_games(q: str, limit: int = 10, request=None):
    """Search games by name (for anchor input mode)."""
    db = request.app.state.db
    rows = await db.fetch(
        "SELECT id, name, cover_url FROM games WHERE name ILIKE $1 LIMIT $2",
        f"%{q}%", limit,
    )
    return [dict(r) for r in rows]


@app.get("/games/{game_id}")
async def get_game(game_id: int, request=None):
    """Get a single game with its emotion profile."""
    db = request.app.state.db
    row = await db.fetchrow("SELECT * FROM games WHERE id=$1", game_id)
    if not row:
        raise HTTPException(status_code=404, detail="Game not found")
    return dict(row)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "gamesoul-api"}
