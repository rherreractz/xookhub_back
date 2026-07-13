# Ruta: Dockerfile
# Shared image for both the `api` (uvicorn) and `worker` (celery) services.
# The command differs per service — see docker-compose.yml — but the built
# image is identical, so it's built once and reused.

FROM python:3.12-slim

# Prevent Python from writing .pyc files and buffering stdout/stderr, so
# container logs stream in real time.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# System deps: build-essential is needed by some wheels (asyncpg, psycopg2
# fallbacks); curl is used by the healthcheck.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps first (separate layer) so code changes don't bust the
# dependency cache on every rebuild.
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# Application code + migration tooling.
COPY src ./src
COPY alembic.ini .
COPY alembic ./alembic

EXPOSE 8000

# Default command; docker-compose overrides it for the worker service.
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]