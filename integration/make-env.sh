#!/bin/sh
# Écrit sur la sortie standard un .env de test pour compose.integration.yaml.
# Les clés sont aléatoires ; LiteLLM est injoignable exprès et les valeurs OIDC sont factices.
set -eu

cat <<EOF
CLEANUP_MODEL=simple-low
DEFAULT_VOICE=Leo
LITELLM_API_KEY=sk-test-not-a-real-key
LITELLM_URL=http://127.0.0.1:9
NEMO_API_KEY=$(openssl rand -hex 32)
NEMO_ASR_THREADS=4
NEMO_TTS_NO_CFG=false
NEMO_TTS_THREADS=4
OIDC_CLIENT_ID=dictee-test
OIDC_CLIENT_SECRET=not-a-real-secret
OIDC_ISSUER=http://127.0.0.1:9
OPENWEBUI_SERVICE_KEY=$(openssl rand -hex 32)
PUBLIC_URL=http://127.0.0.1:18000
SESSION_SECRET=$(openssl rand -hex 32)
EOF
