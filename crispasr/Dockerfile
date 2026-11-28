FROM debian:trixie-20261020-slim

# La version vient du compose (build.args) et n'est répétée nulle part ailleurs.
ARG CRISPASR_VERSION
# TARGETARCH est posé par BuildKit ; sans lui (builder classique), on lit l'architecture du système.
ARG TARGETARCH

RUN set -eu; \
    test -n "${CRISPASR_VERSION}" || { echo "CRISPASR_VERSION est requis (build arg)" >&2; exit 1; }; \
    DEB_ARCH="${TARGETARCH:-$(dpkg --print-architecture)}"; \
    case "${DEB_ARCH}" in \
      amd64) ARCH=x86_64 ;; \
      arm64) ARCH=arm64 ;; \
      *) echo "architecture non prise en charge : '${DEB_ARCH}'" >&2; exit 1 ;; \
    esac; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl jq ffmpeg; \
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
    rm -f "/tmp/${ASSET}"

RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin crispasr \
    && mkdir -p /models \
    && chown crispasr:crispasr /models

# Le binaire charge ses bibliothèques partagées (openblas, gomp) depuis son propre dossier.
ENV PATH=/opt/crispasr:${PATH}
ENV LD_LIBRARY_PATH=/opt/crispasr

COPY pull-models.sh serve.sh /usr/local/bin/
RUN chmod 0755 /usr/local/bin/pull-models.sh /usr/local/bin/serve.sh

USER crispasr
WORKDIR /home/crispasr
