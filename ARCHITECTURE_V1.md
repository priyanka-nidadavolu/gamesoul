# GameSoul Architecture V1 (Scale-Ready)

This document defines the first production-grade architecture for GameSoul.
It is intentionally incremental from the current working demo so we preserve
behavior while upgrading scale, reliability, and team velocity.

---

## 1) Goals

- Support high read/write throughput for recommendations and feedback.
- Keep recommendation latency low and predictable.
- Decouple online serving from batch/scheduled processing.
- Make experimentation and ranking improvements safe and measurable.
- Enable independent service ownership and deployments.

---

## 2) Service Boundaries

### 2.1 `api-gateway` (FastAPI)

**Responsibilities**
- Public API endpoints (`/recommend/*`, `/rate`, `/games/search`, health).
- Request validation, auth (future), rate limits (future), response shaping.
- Orchestrate calls to downstream services.

**Does not own**
- Heavy model inference logic.
- Long-running batch jobs.
- Event replay/backfill responsibilities.

### 2.2 `emotion-service`

**Responsibilities**
- Convert user intent (text/visual/sound/anchors) into 9D emotion vectors.
- Provide stable, versioned extraction contract.
- Route model calls (OpenAI/local/other providers) behind one interface.

**Key contract**
- Input: user payload + mode.
- Output: `{vector, confidence, model_name, model_version}`.

### 2.3 `retrieval-service`

**Responsibilities**
- Vector nearest-neighbor retrieval from Qdrant.
- Optional metadata filtering and re-score hooks.
- Return top-k candidate game IDs + similarity scores.

**Fallback behavior**
- If Qdrant unavailable, use SQL fallback path (temporary safety net).

### 2.4 `ranking-service` (or library in `api-gateway` for V1)

**Responsibilities**
- Thompson Sampling (bandit) re-ranking.
- Apply exploration/exploitation policy.
- Emit ranking decision metadata for analytics.

### 2.5 `feedback-consumer`

**Responsibilities**
- Consume feedback/session/recommendation events from Kafka.
- Persist analytics facts.
- Update bandit state and counters asynchronously.

### 2.6 `scheduler` (Airflow DAGs)

**Responsibilities**
- Orchestrate periodic workflows:
  - nightly embedding/index sync
  - daily data quality checks
  - weekly A/B report generation
  - monthly bandit retraining
- Retries, backfills, dependency management, observability.

---

## 3) Data & Infra Components

### 3.1 PostgreSQL (system of record)

Owns transactional entities:
- `games`
- `user_sessions`
- `recommendations`
- `ratings`
- `bandit_arms`
- experiment/reporting tables

### 3.2 Qdrant (vector retrieval plane)

Owns:
- Vector index for game emotion embeddings
- Payload metadata for filtering

### 3.3 Kafka (event backbone)

Primary topics (V1):
- `recommendation.created`
- `feedback.received`
- `session.started`
- `pipeline.health`

### 3.4 Redis (online performance plane)

Use cases:
- Hot cache for repeated queries
- lightweight session/rate-limit state
- optional short-lived feature flags cache

### 3.5 Observability Stack

- Metrics: Prometheus
- Dashboards/alerts: Grafana
- Error tracking: Sentry
- Structured logs with `request_id`, `session_id`, `trace_id`

---

## 4) Runtime Request Flow (Online Path)

1. Client calls `api-gateway` (`/recommend/text` etc).
2. `api-gateway` calls `emotion-service` to produce target vector.
3. `api-gateway` calls `retrieval-service` (Qdrant) for top-k candidates.
4. Candidate list is re-ranked with Thompson Sampling policy.
5. Response returned to client (top recommendations + explanations).
6. `recommendation.created` event emitted to Kafka.
7. `feedback-consumer` updates analytics and bandit state asynchronously.

---

## 5) Batch/Scheduled Flow (Offline Path)

Airflow orchestrates:
- ingest fresh game content
- run extraction/indexing pipeline
- upsert vectors into Qdrant
- run data quality checks (nulls/confidence drift/coverage)
- generate weekly experiment significance reports
- retrain/recalibrate bandit priors from historical signals

---

## 6) Ownership Rules

- `api-gateway` may write transactional session/recommendation rows.
- `feedback-consumer` is primary writer for feedback-derived aggregates.
- Only indexing pipeline writes Qdrant vectors.
- Airflow jobs do not serve user traffic; they publish artifacts/events only.

This prevents cross-service write contention and hidden side effects.

---

## 7) Reliability & SLO Targets (Initial)

- API availability: `99.9%`
- P95 recommendation latency: `< 300ms` (without LLM call), `< 1200ms` (with LLM call)
- Event processing lag (feedback consumer): `< 60s`
- Nightly indexing success rate: `>= 99%`

---

## 8) Security & Governance

- Secrets only via environment/secret manager.
- No credentials in code or repo.
- PII minimized; use opaque session IDs.
- Version model outputs with extractor version and model metadata.

---

## 9) 4-PR Implementation Plan

## PR 1 - Production contracts + observability baseline

**Scope**
- Add shared event schemas (`recommendation.created`, `feedback.received`, `session.started`).
- Add structured logging fields in API.
- Add health endpoints for DB/Qdrant/Kafka dependency checks.
- Add request IDs and correlation propagation.

**Acceptance**
- API logs contain `request_id` and `session_id`.
- Health endpoint reports dependency status.
- Event payloads validate against schema.

## PR 2 - Retrieval upgrade (Qdrant-first)

**Scope**
- Move retrieval path to Qdrant as primary.
- Keep SQL retrieval fallback under feature flag.
- Add indexing sync utility and validation command.

**Acceptance**
- Qdrant retrieval works end-to-end in staging.
- Fallback path can be toggled without code changes.
- Retrieval latency and error metrics visible.

## PR 3 - Event-driven feedback pipeline (Kafka + consumer)

**Scope**
- API emits recommendation/session/feedback events to Kafka.
- Implement `feedback-consumer` to persist analytics + update bandit state.
- Make direct synchronous feedback writes optional fallback.

**Acceptance**
- Feedback consumer catches up in near-real time.
- No data loss under service restarts (offset/consumer-group safe).
- Bandit state updates from event stream verified.

## PR 4 - Airflow migration for scheduled jobs

**Scope**
- Move APScheduler jobs out of API process into Airflow DAGs.
- Add DAGs for:
  - nightly embedding/index sync
  - daily quality checks
  - weekly A/B report
  - monthly bandit retrain
- Remove job scheduling responsibilities from API runtime.

**Acceptance**
- API startup no longer starts scheduler.
- DAG runs are visible/retryable/backfillable.
- Job outputs stored and auditable.

---

## 10) Environment Strategy

- `local-min`: API + Postgres only (developer/demo)
- `local-full`: API + Postgres + Qdrant + Kafka + Airflow (integration)
- `staging`: production-like topology, lower scale
- `prod`: full topology with autoscaling and alerting

---

## 11) Non-Goals for V1

- Multi-region active-active serving
- Personalized deep user embeddings
- Real-time model training in online path

---

## 12) Immediate Next Step

Start with **PR 1** and keep current demo behavior as a reference baseline.
Every PR must preserve recommendation correctness on the local demo dataset.
