FROM python:3.13-slim

# Pas de valeur par défaut : le build échoue sans S2S_REF.
# S2S_REF est un commit, une branche ou un tag de huggingface/speech-to-speech.
ARG S2S_REF
RUN test -n "${S2S_REF}" || { echo "S2S_REF est requise" >&2; exit 1; }

RUN apt-get update \
    && apt-get install -y --no-install-recommends libsndfile1 libportaudio2 \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --extra-index-url https://download.pytorch.org/whl/cpu "speech-to-speech @ https://github.com/huggingface/speech-to-speech/archive/${S2S_REF}.tar.gz"

ENTRYPOINT ["speech-to-speech"]
CMD ["serve", "-f", "/config/conversation.yaml"]
