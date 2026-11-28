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

# Le binaire `crispasr` sert la dictée en direct : dictee le lance par son nom, une fois par session. La version vient
# du compose (build.args) et n'est répétée nulle part ailleurs ; le schéma est celui de crispasr/Dockerfile.
ARG CRISPASR_VERSION
# TARGETARCH est posé par BuildKit ; sans lui (builder classique), on lit l'architecture du système.
ARG TARGETARCH

RUN set -eu; \
    test -n "${CRISPASR_VERSION:-}" || { echo "CRISPASR_VERSION est requis (build arg)" >&2; exit 1; }; \
    DEB_ARCH="${TARGETARCH:-$(dpkg --print-architecture)}"; \
    case "${DEB_ARCH}" in \
      amd64) ARCH=x86_64 ;; \
      arm64) ARCH=arm64 ;; \
      *) echo "architecture non prise en charge : '${DEB_ARCH}'" >&2; exit 1 ;; \
    esac; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl jq; \
    rm -rf /var/lib/apt/lists/*; \
    ASSET="crispasr-linux-${ARCH}.tar.gz"; \
    BASE="https://github.com/CrispStrobe/CrispASR/releases/download/v${CRISPASR_VERSION}"; \
    API="https://api.github.com/repos/CrispStrobe/CrispASR/releases/tags/v${CRISPASR_VERSION}"; \
    EXPECTED="$(curl -fsSL "${API}" | jq -r --arg name "${ASSET}" '.assets[] | select(.name == $name) | .digest' | sed 's/^sha256://')"; \
    test -n "${EXPECTED}" || { echo "empreinte introuvable pour ${ASSET}" >&2; exit 1; }; \
    curl -fsSL -o "/tmp/${ASSET}" "${BASE}/${ASSET}"; \
    echo "${EXPECTED}  /tmp/${ASSET}" | sha256sum -c -; \
    mkdir -p /opt/crispasr; \
    tar -xzf "/tmp/${ASSET}" -C /opt/crispasr --strip-components=1; \
    rm -f "/tmp/${ASSET}"; \
    apt-get purge -y --auto-remove curl jq

# Le binaire charge ses bibliothèques partagées (openblas, gomp) depuis son propre dossier.
ENV PATH=/opt/crispasr:${PATH} \
    LD_LIBRARY_PATH=/opt/crispasr

RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin dictee \
    && mkdir -p /data \
    && chown dictee:dictee /data

USER 10001
EXPOSE 8000
CMD ["python", "-m", "dictee"]
