#!/bin/sh
# Télécharge le modèle de transcription et celui de diarisation dans /models/current, sous les noms stables
# asr.gguf et diar.gguf que serve.sh charge. Chaque fichier est vérifié par son empreinte SHA-256 ; un fichier déjà
# présent et conforme n'est pas retéléchargé, donc un redéploiement ne dépend pas de Hugging Face.
# Une variable manquante arrête le script en la nommant.
set -eu

require() {
  eval "value=\${$1:-}"
  if [ -z "$value" ]; then
    echo "variable d'environnement manquante : $1" >&2
    exit 1
  fi
}

require ASR_MODEL_URL
require ASR_MODEL_SHA256
require DIAR_MODEL_URL
require DIAR_MODEL_SHA256

MODEL_DIR=/models/current
mkdir -p "$MODEL_DIR"

# fetch NOM URL SHA256 : télécharge vers $MODEL_DIR/NOM sauf si le fichier présent a déjà la bonne empreinte.
fetch() {
  target="$MODEL_DIR/$1"
  if [ -f "$target" ] && echo "$3  $target" | sha256sum -c - > /dev/null 2>&1; then
    echo "$1 déjà en place et conforme, aucun téléchargement"
    return 0
  fi
  echo "téléchargement de $1"
  curl -fsSL --retry 3 --retry-connrefused --connect-timeout 10 -o "$target.tmp" "$2"
  if ! echo "$3  $target.tmp" | sha256sum -c - > /dev/null 2>&1; then
    rm -f "$target.tmp"
    echo "empreinte inattendue pour $1 (attendu $3)" >&2
    exit 1
  fi
  mv "$target.tmp" "$target"
}

fetch asr.gguf "$ASR_MODEL_URL" "$ASR_MODEL_SHA256"
fetch diar.gguf "$DIAR_MODEL_URL" "$DIAR_MODEL_SHA256"

echo "modèles prêts dans $MODEL_DIR :"
ls -l "$MODEL_DIR"
