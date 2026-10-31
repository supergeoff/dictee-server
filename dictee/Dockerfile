FROM ghcr.io/astral-sh/uv:0.13.5-python3.13-trixie-slim AS base

# Le venv du projet vit dans /app/.venv ; `python` doit être celui du venv pour le healthcheck du compose.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    PATH=/app/.venv/bin:${PATH}

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Les dépendances d'abord, pour que le cache de couche survive à un changement du code.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev


FROM base AS test

RUN uv sync --frozen
COPY tests ./tests
CMD ["uv", "run", "--frozen", "pytest", "-q"]


FROM base AS runtime

RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin dictee \
    && mkdir -p /data \
    && chown dictee:dictee /data

USER 10001
EXPOSE 8000
CMD ["python", "-m", "dictee"]
