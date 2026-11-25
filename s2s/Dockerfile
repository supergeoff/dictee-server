FROM python:3.13-slim

# Pas de valeur par défaut : le build échoue sans S2S_VERSION.
ARG S2S_VERSION
RUN test -n "${S2S_VERSION}" || { echo "S2S_VERSION est requise" >&2; exit 1; }

RUN apt-get update \
    && apt-get install -y --no-install-recommends libsndfile1 libportaudio2 \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --extra-index-url https://download.pytorch.org/whl/cpu speech-to-speech==${S2S_VERSION}

ENTRYPOINT ["speech-to-speech"]
CMD ["serve", "-f", "/config/conversation.yaml"]
