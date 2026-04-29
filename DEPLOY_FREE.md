# Free Deployment Guide (Render + Neon)

This is the fastest free setup that matches the current project architecture:

- API: Render free web service
- UI: Render free web service (Streamlit)
- Database: Neon free PostgreSQL

---

## 1) Create free PostgreSQL on Neon

1. Go to [Neon](https://neon.tech/) and create a project.
2. Copy the connection string from Neon (it should start with `postgresql://`).
3. Run schema once using Neon SQL Editor:
   - open `data/schema.sql`
   - paste in Neon SQL Editor
   - run it

Optional: seed a few demo rows in `games` for first recommendation tests.

---

## 2) Deploy API on Render

1. Push this repository to GitHub.
2. In Render, create a **New Blueprint** and select this repo.
3. Render will read `render.yaml` and create `gamesoul-api` + `gamesoul-streamlit` using the repo's Dockerfiles.
4. Set env vars on `gamesoul-api`:
   - `DATABASE_URL` = Neon connection string
   - `OPENAI_API_KEY` = your key (optional, but recommended for text mode)
   - `RAWG_API_KEY` = optional (needed for `/admin/ingest`)
   - `QDRANT_URL` = leave empty for now (API falls back to SQL search)
5. Deploy API and verify:
   - `GET /health` returns `{"status":"ok",...}`
   - `db_ready` becomes `true` after startup retries

---

## 3) Deploy Streamlit on Render

1. Open `gamesoul-streamlit` service in Render.
2. Add env var:
   - `API_BASE_URL` = your API URL (for example: `https://gamesoul-api.onrender.com`)
3. Redeploy streamlit service.
4. Open streamlit URL and test recommendation flow.

---

## 4) First-time smoke tests

Run these against your deployed API:

```bash
curl https://YOUR-API-URL/health
```

```bash
curl -X POST "https://YOUR-API-URL/recommend/text" \
  -H "Content-Type: application/json" \
  -d '{"text":"I want something fast and competitive"}'
```

If no games are indexed yet, load sample rows first or run `/admin/ingest` with `RAWG_API_KEY`.

---

## 5) Notes on free-tier behavior

- Render free services can sleep; first request may be slow.
- Use this for demo/validation; move to paid infra for reliable high traffic.
- Keep scheduler expectations low on free tiers (containers may restart/sleep).
