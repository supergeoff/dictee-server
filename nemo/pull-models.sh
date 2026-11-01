#!/bin/sh
# Télécharge les modèles de NeMo-Speech.cpp dans $NEMO_SPEECH_MODEL_DIR, puis les relie
# sous /models/current pour que serve.sh les charge par des chemins stables.
# `nemo-speech pull` vérifie la taille et l'empreinte SHA-256 de chaque fichier et ne
# retélécharge pas un fichier déjà vérifié.
#
# Un redéploiement ne dépend pas de Hugging Face quand les modèles sont déjà là : si les
# liens de /models/current pointent vers les dépôt, révision et fichier attendus, et si le
# fichier témoin pins.txt porte ces mêmes épinglages, le script sort en 0 sans appel réseau.
# Sinon il interroge Hugging Face, avec trois nouveaux essais sur les erreurs passagères.
set -eu

MODEL_DIR="${NEMO_SPEECH_MODEL_DIR:-/models}"
INDEX=/opt/nemo-speech/share/nemo-speech/model-index.json
# Fichier témoin : les épinglages du dernier lancement réussi, un par lien de current/.
WITNESS="$MODEL_DIR/pins.txt"

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
# La diarisation et le codec gardent la révision de l'index de NeMo ; le script ne vérifie
# que leur dépôt et leur nom de fichier.
DIAR_REPO=nvidia/Nemotron-3-Diarization
DIAR_FILE=Nemotron-3-Diarization.q8_0.gguf
CODEC_REPO=nvidia/nemo-nano-codec-22khz-1.89kbps-21.5fps
CODEC_FILE=nemo_nano_codec_22khz_1.89kbps_21.5fps.decoder.f16.gguf

# hf_get URL
# Appel à Hugging Face : trois nouveaux essais sur un délai dépassé, un refus de connexion ou
# une erreur 5xx, et un délai par essai. Une indisponibilité brève ne doit pas faire échouer
# le service `models`, car compose ne démarrerait alors ni `nemo` ni `dictee`.
hf_get() {
  curl -fsSL --retry 3 --retry-connrefused --connect-timeout 10 --max-time 60 "$1"
}

# Le dossier du modèle est <dépôt>/<révision>/<fichier> : on compare ces trois niveaux à ce
# que serve.sh doit charger, pour ne jamais lier un autre fichier (f16 de Magpie, GGUF de
# Nemotron sans tokenizer) si l'épinglage échoue sans bruit.
index_revision() {
  rev="$(jq -r --arg repo "$1" '[.models[] | select(.repo == $repo)] | if length == 1 then .[0].revision else empty end' "$INDEX")"
  if [ -z "$rev" ]; then
    echo "$1 est absent de l'index de NeMo (ou y figure plusieurs fois)" >&2
    exit 1
  fi
  printf '%s\n' "$rev"
}

# path_matches CHEMIN DÉPÔT RÉVISION NOM : réussit si le chemin se termine par
# <dépôt>/<révision>/<nom>.
path_matches() {
  [ "$(basename "$1")" = "$4" ] \
    && [ "$(basename "$(dirname "$1")")" = "$3" ] \
    && [ "$(basename "$(dirname "$(dirname "$1")")")" = "${2#*/}" ]
}

# check_path CHEMIN DÉPÔT RÉVISION NOM : arrête le script si le chemin n'est pas le bon.
check_path() {
  if ! path_matches "$1" "$2" "$3" "$4"; then
    echo "modèle inattendu : $1" >&2
    echo "  attendu : $2 / $3 / $4" >&2
    exit 1
  fi
}

diar_revision="$(index_revision "$DIAR_REPO")"
codec_revision="$(index_revision "$CODEC_REPO")"

# expected_pins
# Les épinglages attendus, une ligne "lien<TAB>dépôt<TAB>révision<TAB>nom" par lien de
# current/. Le même texte sert de fichier témoin.
expected_pins() {
  printf '%s\t%s\t%s\t%s\n' \
    asr.gguf "$ASR_REPO" "$ASR_REVISION" "$ASR_FILE" \
    diar.gguf "$DIAR_REPO" "$diar_revision" "$DIAR_FILE" \
    tts.gguf "$MAGPIE_REPO" "$MAGPIE_REVISION" "$MAGPIE_FILE" \
    codec.gguf "$CODEC_REPO" "$codec_revision" "$CODEC_FILE" \
    tokenizer "$MAGPIE_REPO" "$MAGPIE_REVISION" tokenizer
}

# models_in_place
# Réussit si le fichier témoin porte les épinglages attendus et si chaque lien de current/
# pointe vers un fichier présent, au bon dépôt, à la bonne révision et sous le bon nom.
models_in_place() {
  [ -f "$WITNESS" ] && [ "$(cat "$WITNESS")" = "$(expected_pins)" ] || return 1
  expected_pins | while IFS="$(printf '\t')" read -r link repo revision name; do
    target="$(readlink "$MODEL_DIR/current/$link")" || exit 1
    { [ -e "$target" ] && path_matches "$target" "$repo" "$revision" "$name"; } || exit 1
  done
}

if models_in_place; then
  echo "modèles déjà en place aux révisions épinglées, aucun appel réseau :"
  ls -l "$MODEL_DIR/current"
  exit 0
fi
echo "modèles absents ou épinglages changés : téléchargement depuis Hugging Face"

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

# pin_artifact INDEX_ENTREE INDEX_SORTIE DÉPÔT RÉVISION RÔLE FICHIER
# Réécrit dans l'index l'entrée du dépôt : révision, et fichier du rôle (nom, taille, SHA-256).
pin_artifact() {
  tree="$(hf_get "https://huggingface.co/api/models/$3/tree/$4")"
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
  # Le filtre ne change rien, sans erreur, si le dépôt ou le rôle n'existe plus dans l'index
  # (schéma modifié par une version ultérieure de NeMo) : on relit donc l'index produit.
  if ! jq -e --arg repo "$3" --arg rev "$4" --arg role "$5" --arg file "$6" \
       --argjson size "$size" --arg sha "$sha" '
    [.models[] | select(.repo == $repo)] as $m
    | ($m | length) == 1
      and $m[0].revision == $rev
      and ([$m[0].artifacts[] | select(.role == $role)] | length) == 1
      and ([$m[0].artifacts[] | select(.role == $role and .filename == $file
                                       and .size == $size and .sha256 == $sha)] | length) == 1' \
       "$2" > /dev/null; then
    echo "l'épinglage de $3 ($4, rôle $5) n'a pas pris effet : le dépôt ou le rôle a changé dans l'index de NeMo" >&2
    exit 1
  fi
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

check_path "$asr_path" "$ASR_REPO" "$ASR_REVISION" "$ASR_FILE"
check_path "$diar_path" "$DIAR_REPO" "$diar_revision" "$DIAR_FILE"
check_path "$tts_path" "$MAGPIE_REPO" "$MAGPIE_REVISION" "$MAGPIE_FILE"
check_path "$codec_path" "$CODEC_REPO" "$codec_revision" "$CODEC_FILE"
check_path "$tokenizer_path" "$MAGPIE_REPO" "$MAGPIE_REVISION" tokenizer

# Le témoin tombe avant de relier et n'est réécrit qu'une fois les cinq liens posés : un
# lancement interrompu entre les deux ne laisse jamais croire que les modèles sont en place.
mkdir -p "$MODEL_DIR/current"
rm -f "$WITNESS"
link_model asr.gguf "$asr_path"
link_model diar.gguf "$diar_path"
link_model tts.gguf "$tts_path"
link_model codec.gguf "$codec_path"
link_model tokenizer "$tokenizer_path"
expected_pins > "$WITNESS.tmp"
mv "$WITNESS.tmp" "$WITNESS"

echo "modèles prêts dans $MODEL_DIR/current :"
ls -l "$MODEL_DIR/current"
