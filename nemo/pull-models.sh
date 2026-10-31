#!/bin/sh
# Télécharge les modèles de NeMo-Speech.cpp dans $NEMO_SPEECH_MODEL_DIR, puis les relie
# sous /models/current pour que serve.sh les charge par des chemins stables.
# `nemo-speech pull` vérifie la taille et l'empreinte SHA-256 de chaque fichier et ne
# retélécharge pas un fichier déjà vérifié.
set -eu

MODEL_DIR="${NEMO_SPEECH_MODEL_DIR:-/models}"
INDEX=/opt/nemo-speech/share/nemo-speech/model-index.json

# L'index de NeMo-Speech.cpp 0.3.0 épingle deux révisions qui ne conviennent pas à dictee.
# Pour chacune, on pointe vers un commit plus récent du même dépôt (même format de GGUF,
# vérifié par un test de transcription et de synthèse) ; `pull` télécharge et vérifie
# ensuite le fichier avec l'empreinte et la taille que Hugging Face publie pour ce commit.
#
# - Nemotron 3.5 : le GGUF de la révision 1c8deae n'embarque pas le tokenizer SentencePiece,
#   et NeMo désactive alors le renforcement des mots (speech_contexts). Le commit ea30d66
#   (« Re-convert GGUF with embedded SentencePiece tokenizer ») corrige cela.
# - Magpie : la révision 1980687 ne porte que le GGUF f16. Le Q8_0 existe depuis le commit
#   446bff5, qui garde le même fichier .nemo, donc le même tokenizer.
#
# Si une version ultérieure de NeMo épingle des révisions qui conviennent, ces commits n'ont
# plus lieu d'être.
ASR_REPO=nvidia/nemotron-3.5-asr-streaming-0.6b
ASR_FILE=nemotron-3.5-asr-streaming-0.6b.q8_0.gguf
ASR_REVISION=ea30d66debe3740a08b573244286791d423d6b3e
MAGPIE_REPO=nvidia/magpie_tts_multilingual_357m
MAGPIE_FILE=magpie_tts_multilingual_357m.v2607.q8_0.gguf
MAGPIE_REVISION=446bff55e1fe3d3aa29c771574e7d2b0fa322990

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

# pin_artifact INDEX_ENTREE INDEX_SORTIE DÉPÔT RÉVISION RÔLE FICHIER
# Réécrit dans l'index l'entrée du dépôt : révision, et fichier du rôle (nom, taille, SHA-256).
pin_artifact() {
  tree="$(curl -fsSL "https://huggingface.co/api/models/$3/tree/$4")"
  sha="$(printf '%s' "$tree" | jq -r --arg f "$6" '.[] | select(.path == $f) | .lfs.oid')"
  size="$(printf '%s' "$tree" | jq -r --arg f "$6" '.[] | select(.path == $f) | .lfs.size')"
  if [ -z "$sha" ] || [ -z "$size" ]; then
    echo "$6 est absent de $3 à la révision $4" >&2
    exit 1
  fi
  jq --arg repo "$3" --arg rev "$4" --arg role "$5" --arg file "$6" \
     --argjson size "$size" --arg sha "$sha" '
    .models |= map(
      if .repo == $repo then
        .revision = $rev
        | .artifacts |= map(if .role == $role then .filename = $file | .size = $size | .sha256 = $sha else . end)
      else . end)' "$1" > "$2"
}

pin_artifact "$INDEX" "$WORK_DIR/index-asr.json" "$ASR_REPO" "$ASR_REVISION" asr "$ASR_FILE"
pin_artifact "$WORK_DIR/index-asr.json" "$WORK_DIR/model-index.json" "$MAGPIE_REPO" "$MAGPIE_REVISION" tts "$MAGPIE_FILE"
export NEMO_SPEECH_MODEL_INDEX="$WORK_DIR/model-index.json"

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
# Le tokenizer de Magpie reste celui de l'index : `pull` l'extrait du .nemo et le vérifie.
tts_out="$(nemo-speech pull magpie)"

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
