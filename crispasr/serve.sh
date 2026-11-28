#!/bin/sh
# Lance le serveur CrispASR sur le port 8080 avec les modèles de /models/current.
# Les valeurs variables viennent du .env ; une variable manquante arrête le script.
# CRISPASR_API_KEYS (liste séparée par des virgules) est lue directement par le serveur.
set -eu

require() {
  eval "value=\${$1:-}"
  if [ -z "$value" ]; then
    echo "variable d'environnement manquante : $1" >&2
    exit 1
  fi
}

require CRISPASR_API_KEYS
require CRISPASR_THREADS
require ASR_BACKEND

MODELS=/models/current

exec crispasr --server --host 0.0.0.0 --port 8080 \
  --backend "$ASR_BACKEND" \
  -m "$MODELS/asr.gguf" \
  --diarize-model "$MODELS/diar.gguf" \
  -t "$CRISPASR_THREADS" \
  --server-workers 2
