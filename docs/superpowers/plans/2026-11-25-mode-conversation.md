# Mode conversation d'Open WebUI : plan d'implémentation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Un appel vocal d'Open WebUI qui répond à voix haute en français, par le serveur speech-to-speech ajouté au compose `dictee`.

**Architecture:** speech-to-speech 1.1.0 tourne en CPU dans un service `conversation` et ne garde en local que la détection de parole. Il appelle LiteLLM pour la transcription (`transcribe`), la conversation (`medium`) et la voix (`speech`). Un Caddy (`conversation-gate`) vérifie une clé `Bearer` devant lui, et Open WebUI s'y connecte en mode Realtime.

**Tech Stack:** speech-to-speech (PyPI), Python 3.13, Caddy, Dokploy (compose raw), LiteLLM, OpenRouter (Voxtral), Open WebUI 0.13.4.

**Spec:** `docs/superpowers/specs/2026-11-25-mode-conversation-design.md` (à lire avant toute tâche). Carte Kaneo : DIC-28.

## Global Constraints

- Toute écriture dans Dokploy, LiteLLM, Open WebUI ou GitHub hors dépôt attend l'accord explicite de Geoff, demandé au moment où elle arrive. Les étapes concernées portent la mention **[accord]** : l'exécutant s'arrête et rend la main avant elles.
- Aucun secret affiché, écrit dans un fichier, un commit, une carte ou un message. Les secrets se lisent par le skill bitwarden (`run --only`), et les scripts n'impriment que des noms et des empreintes.
- Conventions de `~/projects/dokployconfig/docs/conventions-compose.md` pour tout compose, `.env` et domaine.
- Une branche par dépôt, une PR par dépôt, Geoff fusionne. Une fusion sur `main` de `litellmconfig` ou de `dokployconfig` déploie en production.
- Documentation et commits en français, en phrases complètes. Les commits se terminent par les lignes d'attribution de la session.
- Versions : `S2S_VERSION` = `1.1.0` ; Open WebUI en production : `0.13.4`.
- Modèles : `transcribe` = `mistralai/voxtral-mini-transcribe-3-0`, `speech` = `mistralai/voxtral-mini-tts-3-0`, conversation = `medium`. Voix imposée : `fr_marie_neutral`.
- Domaine : `conversation.supergeoff.top`, enregistrement A vers `146.59.63.80`, créé par Geoff.
- Les machines se joignent par le skill open-terminal (devbox), et Dokploy par `~/projects/dokployconfig/lib/dokploy.py` avec le secret `DOKPLOY`.
- Chaque tâche terminée ajoute un commentaire court à la carte Kaneo DIC-28.

## Review Focus

1. Un appel sans en-tête `Authorization`, ou avec une mauvaise clé, doit être refusé par le gardien (401) sans jamais atteindre speech-to-speech. Test : Task 6, point 1, avec les deux cas.
2. L'audio de sortie doit être du PCM 24 kHz, sinon la voix sort accélérée ou ralentie dans Open WebUI. Test : Task 6, point 4.
3. Le redéploiement ne doit changer ni l'environnement ni l'image de `nemo` et de `dictee`. Test : Task 5, comparaison d'empreintes avant et après.
4. Un appel de plus de quelques minutes ne doit pas être coupé par un délai d'inactivité du gardien ou de Traefik pendant un micro coupé. Test : Task 6, point 8 (30 s sans audio).
5. Une panne de LiteLLM au démarrage fait refuser les sessions par speech-to-speech (requête de mise en route) : le healthcheck doit alors marquer le service malsain plutôt que de le laisser accepter des connexions. Test : Task 5, lecture des journaux du premier démarrage et de l'état sain.

---

### Task 1 : alias `transcribe` et `speech` dans LiteLLM

**Dépôt :** `~/projects/litellmconfig`, branche `feat/alias-audio`.

**Files:**
- Modify: `mounts/config.yaml` (nouvelle section `AUDIO`, juste avant la section `IMAGE`)
- Modify: `docs/appelants-directs.md` (ligne `conversation` dans le tableau des appelants)
- Modify: `grille.yaml` et `docs/grille.html` si `make grille` ou les tests les exigent pour un nouvel appelant
- Test: `tests/test_audio_aliases.py`

**Interfaces:**
- Produces: les alias `transcribe` (`POST /v1/audio/transcriptions`) et `speech` (`POST /v1/audio/speech`), consommés par la Task 2 et par speech-to-speech.

- [ ] **Step 1 : écrire le test qui échoue.** `tests/test_audio_aliases.py` charge `mounts/config.yaml` et vérifie, pour chaque alias :
  - `transcribe` : `litellm_params.model == "openai/mistralai/voxtral-mini-transcribe-3-0"`, `api_base == "https://openrouter.ai/api/v1"`, `api_key == "os.environ/OPENROUTER_API_KEY"`, `input_cost_per_second == 0.000058`, `model_info.mode == "audio_transcription"`.
  - `speech` : `litellm_params.model == "openai/mistralai/voxtral-mini-tts-3-0"`, même `api_base` et `api_key`, `input_cost_per_character == 0.000013`, `model_info.mode == "audio_speech"`.
  - Chaque alias n'apparaît qu'une fois dans `model_list`.
- [ ] **Step 2 :** `make test ARGS=tests/test_audio_aliases.py` (ou `.venv/bin/pytest tests/test_audio_aliases.py -q`). Attendu : FAIL, alias absent.
- [ ] **Step 3 : ajouter les deux entrées** dans `mounts/config.yaml`, sur le modèle des alias d'image : commentaire de section en français qui dit le modèle, le prix relevé le 25 novembre 2026 et l'usage (mode conversation d'Open WebUI), `timeout: 60`, `num_retries: 0`. Relire les prix sur les pages OpenRouter des deux modèles avant d'écrire.
- [ ] **Step 4 : documenter l'appelant.** Dans `docs/appelants-directs.md`, ajouter la ligne `conversation` : réglage dans le fichier monté `conversation.yaml` du compose `dictee` (bloc `llm`, `model_name`), lecture par la procédure A appliquée au montage, valeur `medium`, état « relu le 25 novembre 2026 ». Mettre `grille.yaml` et `docs/grille.html` en accord si le dépôt les exige (le test `tests/test_appelants_doc.py` le dit).
- [ ] **Step 5 :** `make test`. Attendu : toute la suite passe.
- [ ] **Step 6 : commit** puis push de la branche et PR intitulée « Alias transcribe et speech pour le mode conversation ».
- [ ] **Step 7 [accord] :** Geoff fusionne. Vérifier que le workflow de déploiement passe (`gh run list -R supergeoff/litellmconfig -L 1`, attendu : `completed success`).

### Task 2 : essais réels des alias et clé `conversation`

**Dépôt :** `~/projects/dictee-server`, branche `feat/mode-conversation`.

**Files:**
- Create: `s2s/probe_litellm.py` (script `uv run --script`, dépendance `httpx`)
- Create: `s2s/tests/test_probe.py`

**Interfaces:**
- Produces: `wav_sample_rate(data: bytes) -> int` et `pcm_duration_s(n_bytes: int, sample_rate: int) -> float` dans `probe_litellm.py`, réutilisées par la Task 6 ; la valeur mesurée `VOXTRAL_PCM_RATE`, reportée dans la carte et utilisée par la Task 3.

- [ ] **Step 1 : tests qui échouent.** `test_wav_sample_rate` construit un WAV de 0,1 s à 24 000 Hz avec le module `wave` et attend `24000`. `test_pcm_duration_s` attend `pcm_duration_s(48000, 24000) == 1.0` (PCM16 mono).
- [ ] **Step 2 :** `uv run --with pytest --with httpx pytest s2s/tests -q`. Attendu : FAIL.
- [ ] **Step 3 : écrire `probe_litellm.py`.** Il lit `LITELLM_URL` et `LITELLM_KEY` dans l'environnement et fait trois appels :
  1. `POST /audio/speech` (`model: speech`, `voice: fr_marie_neutral`, `input: "Bonjour, ceci est un essai de transcription."`, `response_format: wav`) : affiche le taux d'échantillonnage lu dans l'en-tête.
  2. Le même appel en `response_format: pcm`, `stream_format: audio`, lu en flux : affiche le délai du premier octet, le délai total, le nombre de morceaux reçus et la durée audio.
  3. `POST /audio/transcriptions` (`model: transcribe`) avec le WAV de l'appel 1 : affiche le texte et `OK` s'il contient « essai ».
  Il n'imprime jamais la clé ni les en-têtes.
- [ ] **Step 4 :** relancer les tests. Attendu : PASS.
- [ ] **Step 5 :** lancer la sonde avec la master key : `python3 ~/projects/skills/skills/bitwarden/scripts/bw.py run --only LITELLM -- 'LITELLM_KEY=$LITELLM LITELLM_URL=https://litellm.supergeoff.top/v1 uv run --script s2s/probe_litellm.py'`. Attendu : un taux d'échantillonnage, un texte qui contient « essai », et un premier octet bien avant le délai total. **Critère d'arrêt :** si l'audio arrive d'un seul bloc (un morceau, premier octet égal au total) ou si un appel échoue, s'arrêter et rapporter les chiffres à Geoff.
- [ ] **Step 6 : commit** (`s2s/probe_litellm.py`, `s2s/tests/test_probe.py`) et commentaire DIC-28 avec le taux mesuré et les délais.

### Task 3 : image, configuration et gardien dans `dictee-server/s2s/`

**Dépôt :** `~/projects/dictee-server`, branche `feat/mode-conversation`.

**Files:**
- Create: `s2s/Dockerfile`, `s2s/conversation.yaml`, `s2s/conversation.Caddyfile`, `s2s/README.md`
- Modify: `Makefile` (cible `s2s-check`), `AGENTS.md` (une ligne sur `s2s/`)

**Interfaces:**
- Consumes: `VOXTRAL_PCM_RATE` (Task 2).
- Produces: l'image construite depuis `#main:s2s` avec l'argument `S2S_VERSION`, la commande `speech-to-speech serve -f /config/conversation.yaml`, l'écoute sur `0.0.0.0:8765`, les variables `LITELLM_URL`, `CONVERSATION_LITELLM_KEY` et `CONVERSATION_GATE_KEY`, le gardien sur le port `8080`.

- [ ] **Step 1 : `s2s/Dockerfile`.** `FROM python:3.13-slim`, `ARG S2S_VERSION` sans valeur par défaut (le build échoue sans elle), `apt-get install libsndfile1 libportaudio2`, `pip install --no-cache-dir --extra-index-url https://download.pytorch.org/whl/cpu speech-to-speech==${S2S_VERSION}`, `ENTRYPOINT ["speech-to-speech"]`, `CMD ["serve", "-f", "/config/conversation.yaml"]`.
- [ ] **Step 2 : `s2s/conversation.yaml`.** Exactement ce contenu, avec `VOXTRAL_PCM_RATE` remplacé par la valeur mesurée :

```yaml
schema_version: 1
runtime:
  server: {host: 0.0.0.0, port: 8765}
blocks:
  vad: {kind: vad, backend: silero}
  stt:
    kind: stt
    backend: openai
    settings:
      openai_stt_base_url: {env: LITELLM_URL}
      openai_stt_api_key: {env: CONVERSATION_LITELLM_KEY}
      openai_stt_model: transcribe
  llm:
    kind: llm
    backend: chat-completions
    settings:
      model_name: medium
      responses_api_base_url: {env: LITELLM_URL}
      responses_api_api_key: {env: CONVERSATION_LITELLM_KEY}
  tts:
    kind: tts
    backend: openai
    settings:
      openai_tts_base_url: {env: LITELLM_URL}
      openai_tts_api_key: {env: CONVERSATION_LITELLM_KEY}
      openai_tts_model: speech
      openai_tts_voice_override: fr_marie_neutral
      openai_tts_response_format: pcm
      openai_tts_sample_rate: VOXTRAL_PCM_RATE
pipelines:
  conversation:
    stages: {vad: vad, stt: stt, llm: llm, tts: tts}
```

- [ ] **Step 3 : `s2s/conversation.Caddyfile`.** Il écoute `:8080`, laisse passer vers `conversation:8765` seulement la requête dont l'en-tête `Authorization` vaut exactement `Bearer {$CONVERSATION_GATE_KEY}` (substitution à la lecture du fichier), et répond `401` à tout le reste. Aucun journal d'accès, pour que l'en-tête n'apparaisse nulle part.
- [ ] **Step 4 : cible `make s2s-check`.** Elle construit l'image (`docker build --build-arg S2S_VERSION=$(S2S_VERSION) -t conversation-check s2s`, avec `S2S_VERSION` obligatoire comme `NEMO_VERSION`), valide la configuration dans l'image (`python -c` qui appelle `load_config` puis `validate_config` de `speech_to_speech.config` sur `/config/conversation.yaml` monté), puis valide le Caddyfile (`docker run --rm -e CONVERSATION_GATE_KEY=test -v $(PWD)/s2s/conversation.Caddyfile:/etc/caddy/Caddyfile caddy:<tag> caddy validate --adapter caddyfile --config /etc/caddy/Caddyfile`). Le tag Caddy est le dernier tag stable `x.y.z-alpine`, relevé au moment de la tâche, et le même qu'à la Task 5.
- [ ] **Step 5 :** `S2S_VERSION=1.1.0 make s2s-check` sur la devbox, après `free -h` (au moins 2 Go disponibles). Attendu : build réussi, validation sans erreur, `Valid configuration` pour Caddy. Relever la taille de l'image (`docker image ls conversation-check`), puis supprimer l'image. Une taille au-dessus de 2,5 Go se rapporte à Geoff.
- [ ] **Step 6 : `s2s/README.md`** en quelques lignes : rôle du service, origine des montages (ce dossier en est la source), commande de vérification, sonde et test de compatibilité.
- [ ] **Step 7 : commit.**

### Task 4 : catalogue de l'updater et montages

**Dépôts :** `~/projects/dokployconfig` (branche `feat/catalogue-conversation`) et `~/projects/dictee-server`.

**Files:**
- Modify: `dokployconfig/updater/catalog.json`
- Create: `dictee-server/s2s/mounts.json` (format de `dokployconfig/mounts.json`)

- [ ] **Step 1 : test qui échoue.** Dans `updater/test_updater.py`, un test vérifie que le catalogue a une entrée `pins` `speech-to-speech` (compose `dictee`, `find` qui capture la valeur de `S2S_VERSION:` dans le compose, `github: huggingface/speech-to-speech`, `covers: ["conversation"]`) et une entrée `images` pour `caddy`. Lancer `python3 -m unittest test_updater` depuis `updater/`. Attendu : FAIL.
- [ ] **Step 2 : ajouter les deux entrées** sur le modèle de `NeMo-Speech.cpp`. Le format des tags GitHub se lit sur `gh api repos/huggingface/speech-to-speech/releases --jq '.[0].tag_name'` (avec ou sans `v`), et la `note` dit que l'argument de build est dans le compose `dictee`.
- [ ] **Step 3 :** relancer les tests. Attendu : OK. Commit, push, PR.
- [ ] **Step 4 [accord] : créer les deux montages de fichiers** du compose `dictee` dans Dokploy (`mounts.create`, type `file`, chemins `conversation.yaml` et `conversation.Caddyfile`), avec le contenu des fichiers de la Task 3. Relever les deux `mountId`.
- [ ] **Step 5 :** écrire `s2s/mounts.json` avec ces `mountId`, puis `python3 ~/projects/dokployconfig/deploy/mounts.py --catalog s2s/mounts.json --verify` sous le secret `DOKPLOY`. Attendu : code 0, aucun écart.
- [ ] **Step 6 : commit** de `s2s/mounts.json`, push de `feat/mode-conversation`, PR `dictee-server`.
- [ ] **Step 7 [accord] :** Geoff fusionne les PR `dokployconfig` et `dictee-server`. Vérifier que le workflow de `dokployconfig` passe.

### Task 5 : compose `dictee`, `.env` et domaine

**Interfaces:**
- Consumes: l'image `#main:s2s` (Task 3, fusionnée), les montages (Task 4), les alias (Task 1).
- Produces: `wss://conversation.supergeoff.top/v1/realtime`, protégé par `CONVERSATION_GATE_KEY`.

- [ ] **Step 1 : prérequis.** Vérifier que `conversation.supergeoff.top` résout vers `146.59.63.80` (`getent hosts`). Sinon, s'arrêter et le demander à Geoff. Lire la mémoire disponible de sideboxnode (Beszel ou monitoring Dokploy du serveur). En dessous de 2 Go disponibles, s'arrêter et rapporter.
- [ ] **Step 2 : état de référence.** Sauvegarder le compose actuel dans `/tmp/dictee-compose-avant.yml` (mode 600, hors dépôt). Relever, pour `nemo` et `dictee`, l'empreinte de l'environnement effectif, l'identifiant d'image et l'état sain, par `containers()` et `container_config()` de `lib/dokploy.py`, en n'affichant que des empreintes.
- [ ] **Step 3 : services du compose.** Ajouter, après `dictee`, ces deux services et le volume `conversation-cache:` en bas :

```yaml
  conversation:
    build:
      context: https://github.com/supergeoff/dictee-server.git#main:s2s
      args:
        S2S_VERSION: "1.1.0"
    restart: unless-stopped
    env_file: .env
    expose:
      - "8765"
    volumes:
      - ../files/conversation.yaml:/config/conversation.yaml:ro
      - conversation-cache:/root/.cache
    healthcheck:
      test: ["CMD", "python", "-c", "import socket; socket.create_connection(('127.0.0.1', 8765), 5)"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 180s
    deploy:
      resources:
        limits:
          memory: 1536M
  conversation-gate:
    image: caddy:<tag de la Task 3>
    pull_policy: always
    restart: unless-stopped
    env_file: .env
    expose:
      - "8080"
    volumes:
      - ../files/conversation.Caddyfile:/etc/caddy/Caddyfile:ro
    healthcheck:
      test: ["CMD", "wget", "-q", "-O", "/dev/null", "http://127.0.0.1:2019/config/"]
      interval: 30s
      timeout: 5s
      retries: 3
    depends_on:
      conversation:
        condition: service_healthy
    deploy:
      resources:
        limits:
          memory: 128M
```

- [ ] **Step 4 [accord] : clé, compose et `.env`.** Un seul script, lancé sous les secrets `LITELLM` et `DOKPLOY`, qui n'imprime que des noms et des empreintes : il crée la clé LiteLLM (`POST /key/generate` : `key_alias: conversation`, `models: ["transcribe", "medium", "speech"]`, `metadata: {"tags": ["tool:conversation"]}`), génère `CONVERSATION_GATE_KEY` (`secrets.token_urlsafe(32)`), ajoute ces deux variables (`CONVERSATION_LITELLM_KEY` et `CONVERSATION_GATE_KEY`) au `.env`, le trie par nom et l'écrit par `compose.saveEnvironment`, puis écrit le fichier compose par `compose.update`. Vérifier par empreintes que seules ces deux variables ont changé dans le `.env`.
- [ ] **Step 5 [accord] : domaine.** `domain.create` : `host: conversation.supergeoff.top`, `composeId: xUkfJ_DJkAF2Xrskxsank`, `serviceName: conversation-gate`, `port: 8080`, `https: true`, `certificateType: letsencrypt`, `path: /`, `domainType: compose`.
- [ ] **Step 6 [accord] : redéployer** (`redeploy`, titre « DIC-28 : mode conversation ») et suivre le déploiement jusqu'à `done`.
- [ ] **Step 7 : vérifier.** `conversation` et `conversation-gate` sont sains. `nemo` et `dictee` sont sains, avec les mêmes empreintes d'environnement qu'au Step 2. Les journaux du premier démarrage de `conversation` montrent la requête de mise en route réussie pour la transcription et la voix. `curl -s -o /dev/null -w '%{http_code}' https://conversation.supergeoff.top/v1/realtime` renvoie `401`. Relever la mémoire utilisée par `conversation` au repos. **Retour arrière** en cas d'échec : réécrire le compose sauvegardé, retirer le domaine, redéployer, puis rapporter.
- [ ] **Step 8 :** commentaire DIC-28 et suppression de `/tmp/dictee-compose-avant.yml` une fois la Task 6 passée.

### Task 6 : test de compatibilité avec le bridge d'Open WebUI

**Files:**
- Create: `s2s/compat_test.py` (script `uv run --script`, dépendance `websockets`)
- Create: `s2s/tests/test_compat.py`

**Interfaces:**
- Consumes: `wav_sample_rate`, `pcm_duration_s` (Task 2), le domaine et la clé du gardien (Task 5).

- [ ] **Step 1 : lire le bridge.** Comparer `backend/open_webui/routers/audio/realtime.py` et `src/lib/utils/realtime.ts` entre les tags `v0.13.4` et `v0.13.5` d'open-webui (`git diff v0.13.4 v0.13.5 -- <fichiers>` sur un clone). S'ils diffèrent sur les événements envoyés, le script couvre les deux formes.
- [ ] **Step 2 : tests qui échouent.** `test_session_update_matches_bridge` vérifie que `session_update()` renvoie `type: session.update` avec `session.type == "realtime"`, `output_modalities == ["audio"]`, un format de sortie PCM 24 000 Hz, `turn_detection.create_response is False`, `turn_detection.interrupt_response is False`, et les deux outils `generate_chat_completion` et `play_animation` à l'identique du bridge. `test_out_of_band_response` vérifie `conversation == "none"`, `input == []`, `tools == []`.
- [ ] **Step 3 :** `uv run --with pytest --with websockets pytest s2s/tests -q`. Attendu : FAIL.
- [ ] **Step 4 : écrire `compat_test.py`.** Il lit `CONVERSATION_URL` et `CONVERSATION_GATE_KEY`, produit l'audio français d'entrée par l'alias `speech` en WAV (avec `LITELLM_URL` et `LITELLM_KEY`), le rééchantillonne en PCM 24 kHz, puis joue les neuf points de la spec dans l'ordre, chacun avec un délai maximal. Le point 1 essaie deux cas : sans clé, et avec une mauvaise clé. Il affiche `PASS` ou `FAIL` par point, avec l'événement reçu en cas d'échec, et les cinq mesures du point 9. Il ne lève aucune exception non capturée et sort avec le code 1 si un point échoue.
- [ ] **Step 5 :** relancer les tests. Attendu : PASS.
- [ ] **Step 6 : lancer le test réel.** La clé du gardien est lue dans le `.env` du compose par un script qui l'exporte sans l'afficher. Attendu : les points 1 à 8 en `PASS`. **Critère d'arrêt :** un `FAIL` arrête le chantier, et l'événement exact part à Geoff, sans patch.
- [ ] **Step 7 : commit**, PR de suivi sur `dictee-server`, commentaire DIC-28 avec les résultats et les délais.

### Task 7 : réglage d'Open WebUI, appel réel et clôture

- [ ] **Step 1 : sauvegarde.** `GET /api/v1/audio/config` avec le secret `OPENWEBUI`, enregistré dans `/tmp/owui-audio-avant.json` (mode 600), sans l'afficher.
- [ ] **Step 2 [accord] : écrire.** `POST /api/v1/audio/config/update` avec les blocs `tts` et `stt` lus au Step 1, renvoyés tels quels, et le bloc `realtime` : `ENABLED: true`, `PROVIDER: openai`, `AUTH_TYPE: api_key`, `OPENAI_API_BASE_URL: https://conversation.supergeoff.top/v1`, `OPENAI_API_KEY` (clé du gardien, lue sans affichage), `MODEL: medium`, `VOICE: fr_marie_neutral`, `TRANSCRIPTION_MODEL: transcribe`.
- [ ] **Step 3 : vérifier.** Un nouveau `GET` montre ces valeurs, clé exclue de l'affichage, et les blocs `tts` et `stt` ont la même empreinte qu'au Step 1.
- [ ] **Step 4 : appel de Geoff.** Il appelle depuis son téléphone et pose une question qui demande une délégation. Relever dans Phoenix les traces de la clé `conversation`. S'il trouve l'appel trop lent, essayer d'abord `speculative_reopen_ms` dans `conversation.yaml` (PR sur `dictee-server`, puis `mounts.py --apply` avec son accord).
- [ ] **Step 5 : clôture.** Déplacer la spec et ce plan dans `docs/archive/` (nom `2026-11-25-mode-conversation.md`, comme les chantiers précédents), supprimer les branches fusionnées et les fichiers de `/tmp`, commentaire final sur DIC-28 et passage en `done` après l'accord de Geoff sur l'appel.
