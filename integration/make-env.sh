#!/bin/sh
# Écrit sur la sortie standard un .env de test pour compose.integration.yaml.
# Les clés sont aléatoires ; LiteLLM est injoignable exprès et les valeurs OIDC sont factices.
# HF_TOKEN et PTTS_MODEL (hf://gradium/phonon-40m-fr@<commit>) viennent de l'environnement de celui qui lance, jamais du
# dépôt. Si l'une manque, le script s'arrête en la nommant avant d'écrire quoi que ce soit.
# Moteurs : transcription de fichier par CrispASR, dictée en direct par un processus CrispASR que dictee lance pour
# chaque session (LIVE_ENGINE=crispasr-stream, modèle LIVE_MODEL_PATH), voix par phonon (serveur compatible OpenAI).
# Aucun rôle ne passe plus par NeMo : dictee ne lit plus NEMO_API_KEY. Les lignes NEMO_* restent parce que
# `make nemo-up` démarre encore le service nemo avec ce .env, et que nemo/serve.sh exige NEMO_API_KEY,
# NEMO_ASR_THREADS et NEMO_TTS_THREADS ; elles partiront avec NeMo. Un .env n'accepte pas de commentaire : ce constat
# vit ici.
set -eu

for name in HF_TOKEN PTTS_MODEL; do
  eval "value=\${$name:-}"
  if [ -z "$value" ]; then
    echo "variable d'environnement manquante : $name" >&2
    exit 1
  fi
done

# Le serveur lit CRISPASR_API_KEYS (liste), dictee CRISPASR_API_KEY : une seule clé pour les deux.
CRISPASR_KEY=$(openssl rand -hex 32)

cat <<EOF
ASR_BACKEND=parakeet
ASR_MODEL_SHA256=96899b633f7513ce5ad95fa74ee39d3c5f60ab6a00b8d4472c9d154e848efb98
ASR_MODEL_URL=https://huggingface.co/cstr/fluid-speech-pro-GGUF/resolve/main/fluid-speech-pro-q8_0.gguf
CLEANUP_MODEL=simple
CRISPASR_API_KEY=${CRISPASR_KEY}
CRISPASR_API_KEYS=${CRISPASR_KEY}
CRISPASR_PARAKEET_LONGFORM_WINDOW=30
CRISPASR_THREADS=4
CRISPASR_WORKERS=2
DEFAULT_VOICE=Lucien
DIAR_METHOD=sortformer
DIAR_MODEL_SHA256=08456d9e22cd9a323c0364d98375f3746d6e68507ebb705cd46438c534c7a3a1
DIAR_MODEL_URL=https://huggingface.co/nvidia/Nemotron-3-Diarization/resolve/main/Nemotron-3-Diarization.q8_0.gguf
HF_TOKEN=${HF_TOKEN}
LITELLM_API_KEY=sk-test-not-a-real-key
LITELLM_URL=http://127.0.0.1:9
LIVE_ENGINE=crispasr-stream
LIVE_MODEL_PATH=/models/current/asr.gguf
NEMO_API_KEY=$(openssl rand -hex 32)
NEMO_ASR_THREADS=4
NEMO_TTS_NO_CFG=false
NEMO_TTS_THREADS=4
OIDC_CLIENT_ID=dictee-test
OIDC_CLIENT_SECRET=not-a-real-secret
OIDC_ISSUER=http://127.0.0.1:9
OPENWEBUI_SERVICE_KEY=$(openssl rand -hex 32)
PTTS_LANG=fr
PTTS_MODEL=${PTTS_MODEL}
PTTS_QUANT=q8
PUBLIC_URL=http://127.0.0.1:18000
SESSION_SECRET=$(openssl rand -hex 32)
STT_ENGINE=crispasr
TTS_ENGINE=openai
VAD_MODEL_SHA256=2aa269b785eeb53a82983a20501ddf7c1d9c48e33ab63a41391ac6c9f7fb6987
VAD_MODEL_URL=https://huggingface.co/ggml-org/whisper-vad/resolve/main/ggml-silero-v6.2.0.bin
EOF
