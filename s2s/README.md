# s2s : service conversation

Ce dossier porte le serveur speech-to-speech de Hugging Face qui sert le mode appel vocal d'Open WebUI. Il enchaîne VAD, transcription, LLM et synthèse vocale, les trois derniers appelant LiteLLM.

- `Dockerfile` construit l'image. La référence de speech-to-speech (commit, branche ou tag de `huggingface/speech-to-speech`, installée depuis l'archive GitHub) est l'argument de build `S2S_REF`, sans valeur par défaut. Dokploy construit depuis `#main:s2s`.
- `conversation.yaml` est la configuration du pipeline, montée dans `/config/conversation.yaml`. Ce dossier en est la source. Le taux d'échantillonnage de la synthèse (24000) est celui que renvoie MAI-Voice-2.1-Flash via OpenRouter (en-tête `audio/pcm;rate=24000;channels=1`).
- `conversation.Caddyfile` est le gardien sur le port 8080 : il ne laisse passer vers `conversation:8765` que l'en-tête `Authorization: Bearer <CONVERSATION_GATE_KEY>`, et répond 401 sinon. Il utilise l'image `caddy:2.11.9-alpine` (nom complet `docker.io/library/caddy:2.11.9-alpine`), la même que dans le compose.
- Variables : `LITELLM_URL`, `CONVERSATION_LITELLM_KEY`, `CONVERSATION_GATE_KEY`.
- `mounts-conversation.json` et `mounts-gate.json` décrivent les deux montages de fichiers Dokploy du compose `dictee`, un catalogue par service. Après une modification de `conversation.yaml` ou du Caddyfile, on écrit les montages avec `python3 ../dokployconfig/deploy/mounts.py --catalog s2s/mounts-<service>.json --apply` (variables `DOKPLOY_URL` et `DOKPLOY_API_KEY`), puis on redémarre le service.

Vérification : `S2S_REF=4d067e7977f05920daa13c089adc396b51a27384 make s2s-check` construit l'image, valide la configuration dedans, puis valide le Caddyfile.

La sonde `probe_litellm.py` mesure ce que LiteLLM renvoie pour la transcription et la synthèse, et ses tests sont dans `tests/test_probe.py`.

`openai_tts_voice_override` impose la voix `fr-FR-Marc:MAI-Voice-2.1-Flash` au serveur, quelle que soit la voix qu'Open WebUI envoie dans `session.update`. `openai_tts_voice` reste la voix par défaut.

`CONVERSATION_GATE_KEY` doit être une valeur aléatoire non vide, sans `*`, parce que le matcher de Caddy lirait une étoile comme un joker.

Le test de compatibilité `compat_test.py` rejoue les événements du bridge d'Open WebUI contre le service déployé, à travers le gardien. Il lit `CONVERSATION_URL`, `CONVERSATION_GATE_KEY`, `LITELLM_URL` et `LITELLM_KEY`, affiche `PASS` ou `FAIL` pour chacun des neuf points de la spec et sort avec le code 1 si l'un échoue. Le serveur n'a qu'une place de session : le script ferme chaque session avant d'en ouvrir une autre. Avec `--skip-gate`, il vise un service local sans gardien (`CONVERSATION_URL=ws://127.0.0.1:8765/v1/realtime`) : le point 1 est affiché `SKIP`, non applicable en local, et `CONVERSATION_GATE_KEY` n'est plus requise. Ses tests sont dans `tests/test_compat.py`.
