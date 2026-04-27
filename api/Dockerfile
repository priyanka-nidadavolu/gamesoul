FROM python:3.11-slim

WORKDIR /app

COPY api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy shared modules from project root
COPY extraction/emotion_extractor.py .
COPY bandit/bandit.py bandit.py

# Copy API source
COPY api/ .

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
