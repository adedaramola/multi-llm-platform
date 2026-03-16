# Local development — mirrors Lambda Python 3.12 arm64 runtime
FROM python:3.12-slim

WORKDIR /app

# System deps for asyncpg
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONPATH=/app
ENV ENVIRONMENT=dev
ENV LOG_LEVEL=DEBUG

# Start with uvicorn for local development
CMD ["uvicorn", "ai_platform.gateway.app:app", "--host", "0.0.0.0", "--port", "8080", "--reload"]
