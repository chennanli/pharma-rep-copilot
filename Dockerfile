# Pharma Rep Copilot — app container.
# Slim Python base; pip is fine for the container build (uv is for dev).
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# System deps:
#   build-essential + libpq-dev → build psycopg
#   postgresql-client            → provides `psql`, used by entrypoint.sh to wait
#                                  for Postgres and apply init.sql (libpq-dev alone
#                                  does NOT include the psql binary)
#   curl                         → misc setup
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential libpq-dev postgresql-client curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps first (layer cache)
COPY requirements.txt .
RUN pip install -r requirements.txt

# Copy source
COPY app           ./app
COPY scripts       ./scripts
COPY benchmarks    ./benchmarks
COPY docs          ./docs

# Entrypoint waits for Postgres, applies migrations, seeds synth on first run,
# then starts uvicorn. Override CMD to skip seeding (e.g. when using seed-real).
COPY scripts/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8080
ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
