-- GameSoul Database Schema
-- Run on first startup via docker-entrypoint-initdb.d

-- ── Games ──────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS games (
    id              SERIAL PRIMARY KEY,
    rawg_id         INTEGER UNIQUE,
    igdb_id         INTEGER UNIQUE,
    name            TEXT NOT NULL,
    slug            TEXT,
    description     TEXT,
    release_date    DATE,
    rating          FLOAT,
    ratings_count   INTEGER DEFAULT 0,
    genres          TEXT[],
    platforms       TEXT[],
    is_multiplayer  BOOLEAN DEFAULT FALSE,
    cover_url       TEXT,
    -- Emotional dimensions (0–10)
    dim_pace        FLOAT,
    dim_tension     FLOAT,
    dim_agency      FLOAT,
    dim_warmth      FLOAT,
    dim_scale       FLOAT,
    dim_beauty      FLOAT,
    dim_dread       FLOAT,
    dim_wonder      FLOAT,
    dim_rivalry     FLOAT,
    -- Extraction metadata
    extraction_confidence  FLOAT,
    extraction_source      TEXT,   -- 'reviews' | 'description' | 'metadata'
    needs_review    BOOLEAN DEFAULT FALSE,
    qdrant_indexed  BOOLEAN DEFAULT FALSE,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_games_rawg_id   ON games(rawg_id);
CREATE INDEX IF NOT EXISTS idx_games_slug      ON games(slug);
CREATE INDEX IF NOT EXISTS idx_games_needs_review ON games(needs_review) WHERE needs_review = TRUE;

-- ── Users (anonymous sessions) ────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS user_sessions (
    session_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    input_mode      TEXT,           -- 'text' | 'visual' | 'sound' | 'anchor'
    ab_variants     JSONB,          -- {"exp1": "A", "exp2": "control", ...}
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    last_active_at  TIMESTAMPTZ DEFAULT NOW()
);

-- ── Recommendations ────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS recommendations (
    id              SERIAL PRIMARY KEY,
    session_id      UUID REFERENCES user_sessions(session_id),
    -- The query vector
    query_vector    FLOAT[9],
    input_mode      TEXT,
    raw_input       TEXT,
    -- Top-5 results
    game_ids        INTEGER[],
    similarity_scores FLOAT[],
    explanations    TEXT[],
    -- Bandit
    bandit_arm      TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ── Ratings ────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS ratings (
    id              SERIAL PRIMARY KEY,
    session_id      UUID REFERENCES user_sessions(session_id),
    recommendation_id INTEGER REFERENCES recommendations(id),
    game_id         INTEGER REFERENCES games(id),
    rating          SMALLINT CHECK (rating BETWEEN 1 AND 5),
    thumbs_up       BOOLEAN,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ratings_game_id    ON ratings(game_id);
CREATE INDEX IF NOT EXISTS idx_ratings_session_id ON ratings(session_id);
CREATE INDEX IF NOT EXISTS idx_ratings_created_at ON ratings(created_at);

-- ── A/B Experiments ────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS experiments (
    id              SERIAL PRIMARY KEY,
    name            TEXT NOT NULL,
    description     TEXT,
    hypothesis      TEXT,
    status          TEXT DEFAULT 'running',  -- 'running' | 'paused' | 'concluded'
    variants        JSONB,                   -- {"control": 0.25, "A": 0.25, ...}
    primary_metric  TEXT,
    start_date      DATE DEFAULT CURRENT_DATE,
    end_date        DATE,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS experiment_results (
    id              SERIAL PRIMARY KEY,
    experiment_id   INTEGER REFERENCES experiments(id),
    week_ending     DATE,
    variant         TEXT,
    n_recommendations INTEGER,
    n_ratings       INTEGER,
    avg_rating      FLOAT,
    five_star_rate  FLOAT,
    session_depth   FLOAT,
    discovery_rate  FLOAT,
    p_value         FLOAT,
    significant     BOOLEAN,
    computed_at     TIMESTAMPTZ DEFAULT NOW()
);

-- ── Bandit ─────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS bandit_arms (
    id              SERIAL PRIMARY KEY,
    arm_name        TEXT NOT NULL,          -- input mode or retrieval variant
    user_segment    TEXT DEFAULT 'global',
    alpha           FLOAT DEFAULT 1.0,      -- Thompson: successes + 1
    beta            FLOAT DEFAULT 1.0,      -- Thompson: failures + 1
    total_pulls     INTEGER DEFAULT 0,
    total_rewards   FLOAT DEFAULT 0,
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(arm_name, user_segment)
);

-- Seed initial arms
INSERT INTO bandit_arms (arm_name, user_segment) VALUES
    ('text',   'global'),
    ('visual', 'global'),
    ('sound',  'global'),
    ('anchor', 'global')
ON CONFLICT DO NOTHING;

-- ── Data Quality ───────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS data_quality_alerts (
    id              SERIAL PRIMARY KEY,
    game_id         INTEGER REFERENCES games(id),
    alert_type      TEXT,
    details         TEXT,
    resolved        BOOLEAN DEFAULT FALSE,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ── Pipeline health events ─────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS pipeline_events (
    id              SERIAL PRIMARY KEY,
    dag_id          TEXT,
    event_type      TEXT,   -- 'success' | 'failure' | 'warning'
    message         TEXT,
    details         JSONB,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ── Event Queue (replaces Kafka for cloud deployment) ─────────────────────
-- Simple outbox pattern: producers INSERT, consumers SELECT FOR UPDATE SKIP LOCKED

CREATE TABLE IF NOT EXISTS event_queue (
    id              BIGSERIAL PRIMARY KEY,
    topic           TEXT NOT NULL,       -- 'game.releases' | 'user.feedback' | 'pipeline.health'
    payload         JSONB NOT NULL,
    status          TEXT DEFAULT 'pending',  -- 'pending' | 'processing' | 'done' | 'failed'
    attempts        INTEGER DEFAULT 0,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    processed_at    TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_event_queue_topic_status ON event_queue(topic, status);
CREATE INDEX IF NOT EXISTS idx_event_queue_pending ON event_queue(created_at) WHERE status = 'pending';

-- ── Scheduler jobs (replaces Airflow for cloud deployment) ────────────────

CREATE TABLE IF NOT EXISTS scheduled_jobs (
    id              SERIAL PRIMARY KEY,
    job_name        TEXT UNIQUE NOT NULL,
    schedule        TEXT NOT NULL,       -- cron expression
    last_run_at     TIMESTAMPTZ,
    next_run_at     TIMESTAMPTZ,
    last_status     TEXT DEFAULT 'never_run',
    last_error      TEXT,
    enabled         BOOLEAN DEFAULT TRUE
);

INSERT INTO scheduled_jobs (job_name, schedule, next_run_at) VALUES
    ('embed_new_games',    '0 2 * * *',   NOW()),
    ('ab_evaluation',      '0 6 * * 1',   NOW()),
    ('bandit_retrain',     '0 3 1 * *',   NOW()),
    ('data_quality_check', '0 4 * * *',   NOW())
ON CONFLICT DO NOTHING;
