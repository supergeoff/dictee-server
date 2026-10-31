FROM debian:trixie-20261020-slim

# La version vient du compose (build.args) et n'est répétée nulle part ailleurs.
ARG NEMO_VERSION
# TARGETARCH est posé par BuildKit ; sans lui, on lit l'architecture du système.
ARG TARGETARCH

RUN set -eu; \
    test -n "${NEMO_VERSION}" || { echo "NEMO_VERSION est requis (build arg)" >&2; exit 1; }; \
    DEB_ARCH="${TARGETARCH:-$(dpkg --print-architecture)}"; \
    case "${DEB_ARCH}" in \
      amd64) ARCH=x86_64 ;; \
      arm64) ARCH=aarch64 ;; \
      *) echo "architecture non prise en charge : '${DEB_ARCH}'" >&2; exit 1 ;; \
    esac; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl jq; \
    rm -rf /var/lib/apt/lists/*; \
    ASSET="nemo-speech-${NEMO_VERSION}-linux-${ARCH}-cpu.tar.gz"; \
    BASE="https://github.com/NVIDIA/NeMo-Speech.cpp/releases/download/v${NEMO_VERSION}"; \
    API="https://api.github.com/repos/NVIDIA/NeMo-Speech.cpp/releases/tags/v${NEMO_VERSION}"; \
    EXPECTED="$(curl -fsSL "${API}" | jq -r --arg name "${ASSET}" '.assets[] | select(.name == $name) | .digest' | sed 's/^sha256://')"; \
    test -n "${EXPECTED}" || { echo "empreinte introuvable pour ${ASSET}" >&2; exit 1; }; \
    curl -fsSL -o "/tmp/${ASSET}" "${BASE}/${ASSET}"; \
    echo "${EXPECTED}  /tmp/${ASSET}" | sha256sum -c -; \
    mkdir -p /opt/nemo-speech; \
    tar -xzf "/tmp/${ASSET}" -C /opt/nemo-speech --strip-components=1; \
    rm -f "/tmp/${ASSET}"

RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin nemo \
    && mkdir -p /models \
    && chown nemo:nemo /models

ENV PATH=/opt/nemo-speech/bin:${PATH}
ENV NEMO_SPEECH_MODEL_DIR=/models

COPY pull-models.sh serve.sh /usr/local/bin/
RUN chmod 0755 /usr/local/bin/pull-models.sh /usr/local/bin/serve.sh

USER nemo
WORKDIR /home/nemo
