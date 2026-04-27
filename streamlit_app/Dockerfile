FROM python:3.11-slim

WORKDIR /app

COPY streamlit_app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy Streamlit source
COPY streamlit_app/ .

EXPOSE 8501
CMD ["streamlit", "run", "app.py", "--server.address", "0.0.0.0", "--server.port", "8501", "--server.headless", "true"]
