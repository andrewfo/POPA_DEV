# Production image for the POPA wharf data layer.
#
# One image runs all four roles in docker-compose.prod.yml — the API server
# (default CMD), the one-shot migrate/seed, the AIS ingestor, and the occupancy
# worker — each selected by overriding the command. Build:
#
#   docker build -t popa-wharf .
#
# psycopg[binary] and shapely ship manylinux wheels, so no apt build deps are
# needed on a glibc (slim) base.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install dependencies first (cached unless pyproject changes). The package is
# installed in a second step so source edits don't bust the dependency layer.
COPY pyproject.toml ./
COPY app ./app
RUN pip install .[prod]

# Migrate/seed need these at runtime; the package install above already vendored
# `app/`, but Alembic config and the GIS seed data live outside the package.
COPY alembic.ini ./
COPY alembic ./alembic
COPY data ./data

# Run as a non-root user.
RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8000

# Default role: the API server. Gunicorn master + uvicorn workers (graceful
# worker recycling, real timeouts). Worker count tunable via WEB_CONCURRENCY.
CMD ["sh", "-c", "gunicorn -k uvicorn.workers.UvicornWorker app.main:app -b 0.0.0.0:8000 -w ${WEB_CONCURRENCY:-4}"]
