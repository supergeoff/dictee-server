# s2s : service conversation

Ce dossier porte le serveur speech-to-speech de Hugging Face qui sert le mode appel vocal d'Open WebUI. Il enchaîne VAD, transcription, LLM et synthèse vocale, les trois derniers appelant LiteLLM.

- `Dockerfile` construit l'image. La référence de speech-to-speech (commit, branche ou tag de `huggingface/speech-to-speech`, installée depuis l'archive GitHub) est l'argument de build `S2S_REF`, sans valeur par défaut. Dokploy construit depuis `#main:s2s`.
- `conversation.yaml` est la configuration du pipeline, montée dans `/config/conversation.yaml`. Ce dossier en est la source. Le taux d'échantillonnage de la synthèse (24000) est celui mesuré sur Voxtral via OpenRouter.
- `gate/` est le relais devant le service : c'est un petit serveur Python (`gate/gate.py`, `aiohttp` en version exacte dans `gate/requirements.txt`) qui écoute sur le port 8080 du service `conversation-gate`. Dokploy construit son image depuis `#main:s2s/gate`.
- Variables : `LITELLM_URL`, `CONVERSATION_LITELLM_KEY`, `CONVERSATION_GATE_KEY`.
- `mounts-conversation.json` décrit le montage de fichier Dokploy de `conversation.yaml` dans le compose `dictee`. Après une modification de `conversation.yaml`, on écrit le montage avec `python3 ../dokployconfig/deploy/mounts.py --catalog s2s/mounts-conversation.json --apply` (variables `DOKPLOY_URL` et `DOKPLOY_API_KEY`), puis on redémarre le service. Le relais n'a aucun montage : sa seule configuration est la variable `CONVERSATION_GATE_KEY`.

Vérification : `S2S_REF=4d067e7977f05920daa13c089adc396b51a27384 make s2s-check` construit l'image, valide la configuration dedans, puis construit l'image du relais et y lance ses tests.

La sonde `probe_litellm.py` mesure ce que LiteLLM renvoie pour la transcription et la synthèse, et ses tests sont dans `tests/test_probe.py`.

`openai_tts_voice_override` impose la voix `fr_marie_neutral` au serveur, quelle que soit la voix qu'Open WebUI envoie dans `session.update`. `openai_tts_voice` reste la voix par défaut.

## Relais

Le relais sert à deux choses. Il protège le service par une clé, et il retire un événement que speech-to-speech ne connaît pas.

- Toute requête doit porter `Authorization: Bearer <CONVERSATION_GATE_KEY>`, comparée à temps constant. Sans cet en-tête ou avec une autre valeur, la réponse est 401 sans corps.
- `CONVERSATION_GATE_KEY` doit être une valeur aléatoire non vide. Le relais refuse de démarrer si elle manque ou si elle est vide. Aucun autre caractère n'est interdit : la clé est comparée telle quelle, sans motif ni joker.
- Une ouverture WebSocket autorisée est relayée vers `ws://conversation:8765` avec le même chemin et la même query string. Une requête autorisée qui n'est pas une ouverture WebSocket reçoit 404.
- Côté client vers serveur, une trame texte dont le JSON a pour `type` `input_audio_buffer.clear` est retirée. Open WebUI l'envoie à chaque coupure du micro, speech-to-speech y répond par un `error` et le bridge d'Open WebUI met fin à l'appel. Toute autre trame, texte ou binaire, passe sans changement et dans l'ordre. Côté serveur vers client, tout passe sans changement. On retire ce filtre quand speech-to-speech gérera l'événement.
- Une fermeture d'un côté ferme l'autre avec le même code. Si le service est injoignable, le client est fermé avec le code 1011.
- Les journaux ont une ligne par ouverture et une par fermeture, avec le nombre d'événements `clear` retirés. Ils ne contiennent ni en-tête, ni clé, ni chemin, ni contenu de trame.
- Le conteneur écoute sur `0.0.0.0:8080` et tourne sous un utilisateur sans privilège. Il n'a aucun volume. Son contrôle de santé est une simple connexion TCP au port 8080, parce que toute requête HTTP sans la clé reçoit 401.

Les tests sont dans `gate/tests/test_gate.py` et tournent dans l'étape `test` de `gate/Dockerfile` (`docker build --target test`), que `make s2s-check` lance. L'étape finale, `runtime`, est celle que Dokploy construit.

Le test de compatibilité `compat_test.py` rejoue les événements du bridge d'Open WebUI contre le service déployé, à travers le gardien. Il lit `CONVERSATION_URL`, `CONVERSATION_GATE_KEY`, `LITELLM_URL` et `LITELLM_KEY`, affiche `PASS` ou `FAIL` pour chacun des dix points de la spec et sort avec le code 1 si l'un échoue. Le point 10 (micro coupé) envoie `input_audio_buffer.clear` après de l'audio, puis vérifie que la session reste ouverte et répond à une question parlée : sans le relais il échoue, même avec `--skip-gate`. Le serveur n'a qu'une place de session : le script ferme chaque session avant d'en ouvrir une autre. Avec `--skip-gate`, il vise un service local sans gardien (`CONVERSATION_URL=ws://127.0.0.1:8765/v1/realtime`) : le point 1 est affiché `SKIP`, non applicable en local, et `CONVERSATION_GATE_KEY` n'est plus requise. Ses tests sont dans `tests/test_compat.py`.
