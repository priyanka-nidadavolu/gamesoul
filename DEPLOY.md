# Deploying GameSoul to Railway

## What changed from local version
| Local | Cloud |
|-------|-------|
| Ollama (4GB LLM) | OpenAI gpt-4o-mini (~$0.01/1000 games) |
| Kafka + Zookeeper | PostgreSQL event queue table |
| Airflow | APScheduler (runs inside API process) |
| Qdrant container | SQL cosine similarity (Qdrant optional) |

## Step 1 — Push to GitHub
```bash
cd gamesoul
git init
git add .
git commit -m "initial commit"
gh repo create gamesoul --public --push
```

## Step 2 — Deploy on Railway
1. Go to railway.app → New Project → Deploy from GitHub repo
2. Select your `gamesoul` repo
3. Railway will detect the `docker-compose.yml`

## Step 3 — Add PostgreSQL
In Railway dashboard: **New** → **Database** → **PostgreSQL**
Railway auto-sets `DATABASE_URL` in your services.

## Step 4 — Set environment variables
In Railway dashboard for the **api** service, add:
```
OPENAI_API_KEY=sk-...
RAWG_API_KEY=your_key
```

For the **streamlit** service, add:
```
API_BASE_URL=https://your-api-service.railway.app
```

## Step 5 — Seed the database
Once deployed, visit:
```
https://your-api.railway.app/admin/ingest?limit=5000
```
This triggers background ingestion + emotion extraction.
Watch progress at:
```
https://your-api.railway.app/health
```

## Step 6 — Open the app
```
https://your-streamlit.railway.app
```

## Estimated monthly cost on Railway
| Service | Cost |
|---------|------|
| API (512MB) | ~$5/mo |
| Streamlit (256MB) | ~$3/mo |
| PostgreSQL | ~$5/mo |
| OpenAI (5000 games, one-time) | ~$0.50 |
| **Total** | **~$13/mo** |
