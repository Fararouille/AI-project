# syntax=docker/dockerfile:1

# ---- frontend build -------------------------------------------------------
FROM node:22-alpine AS ui
WORKDIR /ui
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ---- runtime --------------------------------------------------------------
FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY data/corpus ./data/corpus
COPY eval ./eval
RUN pip install ".[loaders]"

COPY --from=ui /ui/dist ./frontend/dist

# Bake the index at build time so first boot is instant and deterministic.
RUN python -m cite ingest --corpus data/corpus --out data/index.json

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health')"

CMD ["python", "-m", "cite", "serve", "--host", "0.0.0.0", "--port", "8000"]
