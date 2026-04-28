# Deploying GameSoul to Railway

Railway deploys each service as a **separate project**. Do this twice —
once for the API, once for Streamlit.

---

## Service 1: API

### In Railway dashboard:
1. New Project → Deploy from GitHub repo → select `gamesoul`
2. Railway auto-detects `railway.toml` → uses `Dockerfile.api` ✓
3. Add **PostgreSQL** plugin: click **New** → **Database** → **PostgreSQL**
   - Railway auto-sets `DATABASE_URL` ✓
4. Add environment variables:
   ```
   OPENAI_API_KEY=sk-...
   RAWG_API_KEY=your_rawg_key
   ```
5. Deploy — Railway gives you a URL like:
   `https://gamesoul-api-production.up.railway.app`

### Seed the database (run once after first deploy):
Visit this URL in your browser:
```
https://YOUR-API-URL.railway.app/admin/ingest?limit=5000
```

---

## Service 2: Streamlit

1. New Project → Deploy from GitHub repo → select `gamesoul` again
2. **Override** the Dockerfile in Railway settings:
   - Settings → Build → Dockerfile Path → type: `Dockerfile.streamlit`
3. Add environment variable:
   ```
   API_BASE_URL=https://YOUR-API-URL.railway.app
   ```
4. Deploy → Railway gives you:
   `https://gamesoul-streamlit-production.up.railway.app`

---

## Done!
Open your Streamlit URL and the app is live.
