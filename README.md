# dictee-server

dictee est le point d'entrée audio de la stack supergeoff. Il sert la transcription (Nemotron 3.5 ASR), la séparation des locuteurs (Nemotron 3 Diarization) et la synthèse vocale (Magpie TTS) de NVIDIA, exécutées sur CPU par [NeMo-Speech.cpp](https://github.com/NVIDIA/NeMo-Speech.cpp), derrière une API compatible avec l'API audio d'OpenAI.

Chaque utilisateur se connecte avec PocketID sur une petite page de configuration, où il gère ses clés, son glossaire, ses consignes de reformulation et sa voix.

La spec est dans [`docs/superpowers/specs/2026-10-31-dictee-server-design.md`](docs/superpowers/specs/2026-10-31-dictee-server-design.md), et les règles du dépôt pour les agents sont dans [`AGENTS.md`](AGENTS.md).

## Mise en route

Il faut Docker et `make`. Le déploiement en production passe par Dokploy, qui construit le compose depuis `main` et garde le `.env` : la spec le décrit (section 4).

Le `.env` porte les variables suivantes, dont la spec donne le rôle (section 4.5) : `CLEANUP_MODEL`, `DEFAULT_VOICE`, `GLOSSARY_BOOST`, `LITELLM_API_KEY`, `LITELLM_URL`, `LONG_AUDIO_SECONDS`, `MAX_UPLOAD_MB`, `NEMO_API_KEY`, `NEMO_ASR_THREADS`, `NEMO_TTS_NO_CFG`, `NEMO_TTS_THREADS`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`, `OIDC_ISSUER`, `OPENWEBUI_SERVICE_KEY`, `PUBLIC_URL` et `SESSION_SECRET`. En local, `make integration` en fabrique un de banc avec `integration/make-env.sh` s'il manque, et git l'ignore.

| Commande | Effet |
|---|---|
| `make test` | Lance `pytest` dans une image Docker, avec un faux NeMo et un faux LiteLLM, sans réseau. |
| `make integration` | Construit et lance la stack complète avec les vrais modèles (le premier lancement les télécharge), attend `/health`, puis joue `integration/run.py`. Elle demande `uv`, laisse la stack démarrée et ne tourne pas dans la CI. |
| `make down` | Arrête la stack ; les volumes restent. |
