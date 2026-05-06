FROM python:3.11-slim

WORKDIR /app

COPY api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Shared modules (preserve package structure for imports)
COPY extraction/ ./extraction/
COPY bandit/ ./bandit/
COPY data/ ./data/

# API source
COPY api/ ./api/

EXPOSE 8000
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
