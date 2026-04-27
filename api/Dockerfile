FROM python:3.11-slim

WORKDIR /app

COPY api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Shared modules
COPY extraction/emotion_extractor.py .
COPY extraction/index_games.py .
COPY bandit/bandit.py bandit.py
COPY data/ingest.py ingest.py

# API source
COPY api/ .

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
