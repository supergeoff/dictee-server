#!/bin/sh
# Télécharge les modèles de NeMo-Speech.cpp dans $NEMO_SPEECH_MODEL_DIR, puis les relie
# sous /models/current pour que serve.sh les charge par des chemins stables.
# `nemo-speech pull` vérifie la taille et l'empreinte SHA-256 de chaque fichier et ne
# retélécharge pas un fichier déjà vérifié.
set -eu

MODEL_DIR="${NEMO_SPEECH_MODEL_DIR:-/models}"
INDEX=/opt/nemo-speech/share/nemo-speech/model-index.json

MAGPIE_REPO=nvidia/magpie_tts_multilingual_357m
MAGPIE_Q8_FILE=magpie_tts_multilingual_357m.v2607.q8_0.gguf
# La révision que l'index de NeMo-Speech.cpp 0.3.0 épingle pour Magpie (1980687) ne porte que
# le GGUF f16. Le Q8_0 existe depuis ce commit, qui garde le même fichier .nemo, donc le
# même tokenizer. Si une version ultérieure épingle une révision qui porte le Q8_0, ce
# commit n'a plus lieu d'être.
MAGPIE_Q8_REVISION=446bff55e1fe3d3aa29c771574e7d2b0fa322990

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

# `nemo-speech pull` écrit sur stdout une ligne "dépôt<TAB>rôle<TAB>chemin" par fichier.
role_path() {
  path="$(printf '%s\n' "$1" | awk -F '\t' -v role="$2" '$2 == role { print $3 }')"
  if [ -z "$path" ] || [ ! -e "$path" ]; then
    echo "chemin introuvable pour le rôle $2" >&2
    exit 1
  fi
  printf '%s\n' "$path"
}

link_model() {
  ln -sfn "$2" "$MODEL_DIR/current/$1"
}

asr_out="$(nemo-speech pull nemotron-3.5)"
diar_out="$(nemo-speech pull nemotron-3-diarization)"
codec_out="$(nemo-speech pull nano-codec)"

# Magpie : un index dérivé de celui de NeMo-Speech.cpp remplace le GGUF f16 par le Q8_0,
# dont l'empreinte est l'oid LFS publié par Hugging Face pour ce commit. Le tokenizer reste
# celui de l'index, que `pull` extrait du .nemo et vérifie membre par membre.
tree="$(curl -fsSL "https://huggingface.co/api/models/$MAGPIE_REPO/tree/$MAGPIE_Q8_REVISION")"
q8_sha="$(printf '%s' "$tree" | jq -r --arg f "$MAGPIE_Q8_FILE" '.[] | select(.path == $f) | .lfs.oid')"
q8_size="$(printf '%s' "$tree" | jq -r --arg f "$MAGPIE_Q8_FILE" '.[] | select(.path == $f) | .lfs.size')"
if [ -z "$q8_sha" ] || [ -z "$q8_size" ]; then
  echo "$MAGPIE_Q8_FILE est absent de $MAGPIE_REPO à la révision $MAGPIE_Q8_REVISION" >&2
  exit 1
fi
jq --arg repo "$MAGPIE_REPO" --arg rev "$MAGPIE_Q8_REVISION" --arg file "$MAGPIE_Q8_FILE" \
   --argjson size "$q8_size" --arg sha "$q8_sha" '
  .models |= map(
    if .repo == $repo then
      .revision = $rev
      | .artifacts |= map(if .role == "tts" then .filename = $file | .size = $size | .sha256 = $sha else . end)
    else . end)' "$INDEX" > "$WORK_DIR/model-index.json"
tts_out="$(NEMO_SPEECH_MODEL_INDEX="$WORK_DIR/model-index.json" nemo-speech pull magpie)"

asr_path="$(role_path "$asr_out" asr)"
diar_path="$(role_path "$diar_out" diarization)"
tts_path="$(role_path "$tts_out" tts)"
codec_path="$(role_path "$codec_out" codec)"
tokenizer_path="$(role_path "$tts_out" tokenizer)"

mkdir -p "$MODEL_DIR/current"
link_model asr.gguf "$asr_path"
link_model diar.gguf "$diar_path"
link_model tts.gguf "$tts_path"
link_model codec.gguf "$codec_path"
link_model tokenizer "$tokenizer_path"

echo "modèles prêts dans $MODEL_DIR/current :"
ls -l "$MODEL_DIR/current"
