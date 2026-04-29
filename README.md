# GameSoul — Emotion-Driven Game Discovery

> Find your next game by how you want to **feel**, not what genre you want to play.

---

## What is GameSoul?

GameSoul maps 50,000+ games across **9 emotional dimensions** and matches them to how you feel right now. Unlike genre tags or collaborative filtering, GameSoul answers the actual question: *how will this game make me feel?*

A player who loved **Free Fire** might discover they also love **Superhot** or **Into the Breach** — because they share the same emotional DNA (high agency under pressure, split-second decisions, no warmth needed) — even though they look nothing alike on the surface.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Streamlit App (4 input modes)                              │
│  └─ Free text │ Visual mood │ Sound check │ Anchor games    │
└────────────────────┬────────────────────────────────────────┘
                     │ HTTP
┌────────────────────▼────────────────────────────────────────┐
│  FastAPI Gateway                                            │
│  └─ /recommend/text | /recommend/visual | /recommend/sound  │
│  └─ /recommend/anchor | /rate | /games/search               │
└──────┬─────────────────────────────────┬────────────────────┘
       │                                 │
┌──────▼──────┐              ┌───────────▼──────────┐
│ LLM Emotion  │              │ Qdrant Vector Search │
│ Engine       │              │ (9-dim cosine sim)   │
│ (Ollama/GPT) │              └───────────┬──────────┘
└─────────────┘                           │
                                          │ top-20
                              ┌───────────▼──────────┐
                              │ Thompson Sampling     │
                              │ Bandit (select top-5) │
                              └───────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│  Kafka Topics                                               │
│  game.releases │ user.feedback │ user.sessions │ pipeline.health │
└────────────────────┬────────────────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────────────────┐
│  Airflow DAGs                                               │
│  nightly_embed_new_games    (2am daily)                     │
│  weekly_ab_evaluation       (Mon 6am)                       │
│  monthly_bandit_retrain     (1st of month)                  │
│  daily_data_quality         (4am daily)                     │
└────────────────────┬────────────────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────────────────┐
│  PostgreSQL                                                 │
│  games │ user_sessions │ recommendations │ ratings          │
│  experiments │ experiment_results │ bandit_arms             │
└─────────────────────────────────────────────────────────────┘
```

---

## Quick Start

### Prerequisites
- Docker + Docker Compose
- 8 GB RAM recommended (Ollama + all services)
- Optional: NVIDIA GPU for faster local LLM inference

### 1. Clone and configure

```bash
git clone https://github.com/you/gamesoul.git
cd gamesoul
cp .env.example .env
# Edit .env: add RAWG_API_KEY, IGDB_CLIENT_ID/SECRET, optional OPENAI_API_KEY
```

### 2. Start all services

```bash
docker compose up -d
```

Services started:
| Service | URL |
|---------|-----|
| Streamlit app | http://localhost:8501 |
| FastAPI docs | http://localhost:8000/docs |
| Airflow | http://localhost:8080 (admin/admin) |
| Qdrant UI | http://localhost:6333/dashboard |

### 3. Pull the local LLM

```bash
docker exec gamesoul_ollama ollama pull llama3
```

### 4. Seed the database

```bash
# Pull 50,000 games from RAWG
docker exec gamesoul_api python data/ingest.py --source rawg --limit 50000

# Extract emotions and index into Qdrant (starts with top 5,000)
docker exec gamesoul_api python extraction/index_games.py --limit 5000

# Validate the index
docker exec gamesoul_api python extraction/index_games.py --validate
```

---

## The 9 Emotional Dimensions

| Dimension | Range | What it captures |
|-----------|-------|-----------------|
| **Pace** | 0–10 | How fast the game demands you respond and move |
| **Tension** | 0–10 | How much every decision feels like it matters right now |
| **Agency** | 0–10 | How much the outcome depends on your choices |
| **Warmth** | 0–10 | Whether the game feels welcoming, nurturing, or human |
| **Scale** | 0–10 | The felt size of the world and your place in it |
| **Beauty** | 0–10 | How much visual or sonic artistry is part of the experience |
| **Dread** | 0–10 | Fear, unease, the feeling that something is wrong |
| **Wonder** | 0–10 | Surprise, discovery, the feeling of a world larger than you expected |
| **Rivalry** | 0–10 | How much the thrill comes from competing against other humans |

### Sample game profiles

| Game | Pace | Tension | Agency | Warmth | Scale | Beauty | Dread | Wonder | Rivalry |
|------|------|---------|--------|--------|-------|--------|-------|--------|---------|
| Free Fire | 9 | 9 | 7 | 2 | 5 | 3 | 6 | 2 | 9 |
| Stardew Valley | 1 | 1 | 8 | 9 | 2 | 8 | 0 | 6 | 0 |
| Dark Souls | 4 | 9 | 8 | 2 | 5 | 8 | 7 | 8 | 1 |
| Minecraft | 3 | 2 | 10 | 5 | 10 | 5 | 2 | 9 | 2 |
| Disco Elysium | 1 | 2 | 8 | 5 | 2 | 9 | 4 | 9 | 0 |

---

## The Four Input Modes

1. **Free text** — "I want to feel alert and in control under pressure"
2. **Visual mood picker** — Scroll through images (rainy window, crowded market, lone mountain) and tap what pulls you in
3. **Sound check** — 5 ambient audio clips, pick the ones that match your current state
4. **Love it / Hate it** — Give two anchor games; the system extracts the emotional contrast

---

## A/B Experiments

Three parallel experiments run automatically:

| Experiment | Hypothesis | Metric | Status |
|------------|-----------|--------|--------|
| Input mode effectiveness | Different modes → different satisfaction | 5-star rate | Running |
| LLM size (LLaMA 3 vs GPT-4o) | Larger LLM → better emotion vectors | Avg rating | Running |
| Retrieval (cosine vs hybrid) | Popularity re-rank improves results | Avg rating | Running |

Results are auto-computed every Monday by the `weekly_ab_evaluation` DAG.

---

## Running Tests

```bash
pip install pytest numpy scipy
pytest tests/ -v
```

## Free Deploy

For a free hosted setup (Render + Neon), see `DEPLOY_FREE.md`.

---

## Repository Structure

```
gamesoul/
├── data/
│   ├── schema.sql              # PostgreSQL schema
│   └── ingest.py               # RAWG + IGDB + Steam ingestion
├── extraction/
│   ├── emotion_extractor.py    # LLM → 9-dim vector
│   └── index_games.py          # Full indexing pipeline
├── api/
│   ├── main.py                 # FastAPI gateway (4 endpoints)
│   ├── requirements.txt
│   └── Dockerfile
├── streamlit_app/
│   ├── app.py                  # Full Streamlit UI
│   ├── requirements.txt
│   └── Dockerfile
├── kafka/
│   ├── producers.py            # Event publishers
│   └── consumers.py            # Feedback + release consumers
├── airflow/
│   └── dags/
│       └── gamesoul_dags.py    # All 4 DAGs
├── bandit/
│   └── bandit.py               # Thompson sampling
├── experiments/
│   └── ab_framework.py         # Assignment + significance testing
├── powerbi/
│   └── data_connector.py       # CSV exports for Power BI
├── tests/
│   └── test_emotion_extractor.py
├── docker-compose.yml
└── README.md
```

---

## Power BI Dashboard

Connect Power BI to the CSV exports (run `powerbi/data_connector.py`) or directly to PostgreSQL.

**Dashboard pages:**
- **Emotion Gap Map** — heatmap of user demand vs game supply across all 9 dimensions
- **Input Mode Performance** — A/B results: which input mode gets the best-rated recommendations
- **Recommendation Quality** — avg rating per week as the bandit learns
- **Discovery Rate** — how often GameSoul surfaces a game the user has never heard of
- **Dimension Correlation** — which emotional dimensions co-occur in games people love

---

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `RAWG_API_KEY` | Yes | From rawg.io (free) |
| `IGDB_CLIENT_ID` | Yes | Twitch Developer App |
| `IGDB_CLIENT_SECRET` | Yes | Twitch Developer App |
| `OPENAI_API_KEY` | No | For GPT-4o in Experiment 2 |
| `DATABASE_URL` | Auto | Set by Docker Compose |
| `QDRANT_HOST` | Auto | Set by Docker Compose |
| `OLLAMA_HOST` | Auto | Set by Docker Compose |
| `KAFKA_BOOTSTRAP_SERVERS` | Auto | Set by Docker Compose |

---

## Skills This Project Demonstrates

| Skill | Implementation |
|-------|---------------|
| **Kafka** | 4 production topics, consumer groups, real-time feedback streaming |
| **Airflow** | 4 DAGs: nightly, weekly, monthly, daily |
| **A/B Testing** | 3 full experiments with chi-square significance testing |
| **Power BI** | 5-page emotion gap dashboard |
| **Thompson Sampling** | Bandit for input mode and game selection |
| **Vector Search** | 9-dim Qdrant cosine similarity retrieval |
| **LLM Integration** | Ollama local + OpenAI fallback |
| **Streamlit** | Full 4-mode interactive app |
| **Docker** | One-command deployment |
| **FastAPI** | Async REST API |
