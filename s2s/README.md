# s2s : service conversation

Ce dossier porte le serveur speech-to-speech de Hugging Face qui sert le mode appel vocal d'Open WebUI. Il enchaîne VAD, transcription, LLM et synthèse vocale, les trois derniers appelant LiteLLM.

- `Dockerfile` construit l'image. La version est l'argument de build `S2S_VERSION`, sans valeur par défaut. Dokploy construit depuis `#main:s2s`.
- `conversation.yaml` est la configuration du pipeline, montée dans `/config/conversation.yaml`. Ce dossier en est la source. Le taux d'échantillonnage de la synthèse (24000) est celui mesuré sur Voxtral via OpenRouter.
- `conversation.Caddyfile` est le gardien sur le port 8080 : il ne laisse passer vers `conversation:8765` que l'en-tête `Authorization: Bearer <CONVERSATION_GATE_KEY>`, et répond 401 sinon. Il utilise l'image `caddy:2.11.9-alpine` (nom complet `docker.io/library/caddy:2.11.9-alpine`), la même que dans le compose.
- Variables : `LITELLM_URL`, `CONVERSATION_LITELLM_KEY`, `CONVERSATION_GATE_KEY`.
- `mounts-conversation.json` et `mounts-gate.json` décrivent les deux montages de fichiers Dokploy du compose `dictee`, un catalogue par service. Après une modification de `conversation.yaml` ou du Caddyfile, on écrit les montages avec `python3 ../dokployconfig/deploy/mounts.py --catalog s2s/mounts-<service>.json --apply` (variables `DOKPLOY_URL` et `DOKPLOY_API_KEY`), puis on redémarre le service.

Vérification : `S2S_VERSION=1.1.0 make s2s-check` construit l'image, valide la configuration dedans, puis valide le Caddyfile.

La sonde `probe_litellm.py` mesure ce que LiteLLM renvoie pour la transcription et la synthèse, et ses tests sont dans `tests/test_probe.py`.

`openai_tts_voice` n'est qu'une voix par défaut : la voix envoyée par le client dans `session.update` la remplace, donc Open WebUI doit envoyer `fr_marie_neutral`.

`CONVERSATION_GATE_KEY` doit être une valeur aléatoire non vide, sans `*`, parce que le matcher de Caddy lirait une étoile comme un joker.
