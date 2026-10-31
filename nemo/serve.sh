#!/bin/sh
# Lance NeMo-Speech.cpp sur le port 8000 avec les modèles de /models/current.
# Les valeurs variables viennent du .env ; une variable manquante arrête le script.
set -eu

require() {
  eval "value=\${$1:-}"
  if [ -z "$value" ]; then
    echo "variable d'environnement manquante : $1" >&2
    exit 1
  fi
}

require NEMO_API_KEY
require NEMO_ASR_THREADS
require NEMO_TTS_THREADS

MODELS=/models/current

set -- \
  --host 0.0.0.0 --port 8000 --no-ui \
  --asr-model "$MODELS/asr.gguf" \
  --diar-model "$MODELS/diar.gguf" \
  --tts-model "$MODELS/tts.gguf" \
  --codec-model "$MODELS/codec.gguf" \
  --tokenizer-dir "$MODELS/tokenizer" \
  --api-key "$NEMO_API_KEY" \
  --asr.streaming.rnnt_right_context 13 \
  --asr.diar.preset v3-offline \
  --asr.backend.threads "$NEMO_ASR_THREADS" \
  --tts.threads "$NEMO_TTS_THREADS"

# Le CFG ne se coupe que sur demande explicite.
if [ "${NEMO_TTS_NO_CFG:-false}" = "true" ]; then
  set -- "$@" --tts.no-cfg
fi

exec nemo-speech serve "$@"
