# Moteurs audio de dictee : plan d'implémentation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** dictee choisit ses moteurs audio par rôle dans le `.env`, passe sa transcription à CrispASR (Fluid Speech Pro, Nemotron 3 Diarization) et sa voix à phonon-40m-fr (Lucien), sans changer ce que voient ses clients, puis NeMo-Speech.cpp sort.

**Architecture:** Un paquet `dictee/engines/` décrit trois rôles par des protocoles Python (fichier, direct, voix). Le code actuel devient l'adaptateur `nemo`, gardé pour le retour arrière. Trois adaptateurs s'ajoutent : `crispasr` (HTTP vers le serveur CrispASR), `crispasr-stream` (un processus `crispasr --stream` par session) et `openai` (HTTP vers le serveur de phonon). dictee corrige lui-même le glossaire après chaque transcription.

**Tech Stack:** Python 3.13, Starlette, httpx, pytest, ffmpeg, CrispASR 0.8.55 (binaire de release), `ghcr.io/gradium-ai/ptts-openai-server`, Docker Compose, Dokploy.

**Spec:** `docs/superpowers/specs/2026-11-28-moteurs-audio-design.md` (à lire avant toute tâche). Carte Kaneo : DIC-29.

## Global Constraints

- La surface externe ne change pas : mêmes routes, mêmes modèles (`dictee`, `dictee-clean`, `dictee-diarize`, `gpt-4o-transcribe-diarize`), mêmes formats, mêmes codes d'erreur. Les tests de contrat existants (`test_transcriptions.py`, `test_tasks_api.py`, `test_live.py`, `test_speech.py`) ne voient leurs attentes modifiées que là où une tâche le dit.
- dictee n'envoie jamais de `hotwords` à CrispASR.
- Rien de personnel, aucun secret et aucun enregistrement dans le dépôt, dans une image, dans un commit ou dans un message. Les journaux ne contiennent ni texte transcrit, ni texte à dire, ni glossaire, ni audio.
- Toute écriture dans Dokploy, `dokployconfig`, LiteLLM, Open WebUI ou GitHub (push, PR) attend l'accord explicite de Geoff, demandé au moment où elle arrive. Les étapes concernées portent la mention **[accord]** : l'exécutant s'arrête et rend la main avant elles.
- Le travail se fait sur la branche `feat/moteurs-audio`, dans un worktree. Geoff fusionne lui-même sur `main`.
- Chaque tâche de code suit le cycle : test qui échoue, code, test qui passe, commit. Les tests se lancent par `make test` (image Docker), avec `ARGS` pour un fichier : `make test ARGS="tests/test_glossary.py"`.
- Documentation et messages de commit en français, en phrases complètes. Code et termes techniques en anglais. Les commits se terminent par les lignes d'attribution de la session.
- Versions et valeurs : `CRISPASR_VERSION` = `0.8.55` ; moteur de voix ptts `0.7.1` ; `DEFAULT_VOICE` = `Lucien` ; `LIVE_MAX_SESSIONS` = `2` ; PCM de la voix à 24000 Hz ; morceaux d'un fichier en flux bornés à 15 s ; critère du direct : 95 % des mots.
- Conventions de compose : `dokployconfig/docs/conventions-compose.md` (un seul `.env`, `env_file: .env`, aucune variable dans `environment`, `expose`, `pull_policy: always`, volumes nommés). La sidebox n'a pas `buildx` : un build vient d'un contexte git.
- Chaque tâche terminée ajoute un commentaire court à la carte Kaneo DIC-29.

## Review Focus

1. Une variante du glossaire qui est le début d'un mot plus long (`IT` dans `ITLM`), ou qui contient un caractère spécial d'expression régulière, ne doit rien remplacer à tort. Test : Task 4, `test_variant_inside_longer_word_is_kept` et `test_variant_with_regex_characters`.
2. Un client qui raccroche au milieu d'une dictée en direct ne doit laisser aucun processus `crispasr` en vie (1,7 Go chacun). Test : Task 8, `test_process_is_killed_when_session_closes` et `test_process_is_killed_when_cancelled`.
3. Une troisième dictée simultanée doit recevoir l'erreur de saturation, et la place doit se libérer à la fermeture d'une session. Test : Task 8, `test_third_session_is_overloaded_then_slot_frees`.
4. Un serveur CrispASR encore en chargement (`/health` à 503) au démarrage de dictee ne doit pas faire échouer une tâche : elle attend. Test : Task 6, `test_ready_is_false_while_loading`, et Task 6 étape 6 sur `processing.wait_ready`.
5. Un profil dont la voix n'existe plus (`Leo`) doit obtenir du son avec la voix par défaut, et un serveur de voix injoignable doit donner une erreur 502 et non 500. Test : Task 9, `test_unknown_profile_voice_falls_back_to_default` et `test_unreachable_voice_server_is_upstream_error`.

---

## Carte des fichiers

| Fichier | Rôle |
|---|---|
| `dictee/src/dictee/engines/__init__.py` | `Engines`, `build_engines` : choix des adaptateurs d'après les réglages |
| `dictee/src/dictee/engines/ports.py` | Protocoles des trois rôles, `LiveSession`, exceptions `Engine*` |
| `dictee/src/dictee/engines/nemo.py` | L'actuel `dictee/src/dictee/nemo.py`, déplacé (retiré en Task 14) |
| `dictee/src/dictee/engines/crispasr.py` | Adaptateur de fichier : serveur CrispASR |
| `dictee/src/dictee/engines/crispasr_stream.py` | Adaptateur du direct : processus `crispasr --stream` |
| `dictee/src/dictee/engines/openai_tts.py` | Adaptateur de voix : serveur compatible OpenAI |
| `dictee/src/dictee/glossary.py` | Correction déterministe du glossaire |
| `crispasr/Dockerfile`, `crispasr/pull-models.sh`, `crispasr/serve.sh` | Image du serveur CrispASR, sur le schéma de `nemo/` |
| `dictee/tests/fakes.py` | `FakeCrispasr`, `FakeVoiceServer` et le faux binaire du direct s'ajoutent à `FakeNemo` |

---

### Task 1 : les trois rôles, sans changement de comportement

**Files:**
- Create: `dictee/src/dictee/engines/__init__.py`, `dictee/src/dictee/engines/ports.py`
- Move: `dictee/src/dictee/nemo.py` → `dictee/src/dictee/engines/nemo.py` ; `dictee/tests/test_nemo.py` → `dictee/tests/test_engine_nemo.py`
- Modify: `dictee/src/dictee/app.py`, `errors.py`, `processing.py`, `transcribe.py`, `live.py`, `speech.py`, `web.py`, `settings.py`, `runner.py` ; `dictee/tests/conftest.py`, `fakes.py` et les tests qui lisent `state.nemo`
- Test: `dictee/tests/test_engines.py`

**Interfaces:**
- Produces, dans `ports.py` :
  - `class EngineUnavailable(Exception)`, `class EngineOverloaded(Exception)`, `class EngineError(Exception)`, `ENGINE_ERRORS = (EngineOverloaded, EngineUnavailable, EngineError)`. Ils remplacent `NemoUnavailable`, `NemoOverloaded`, `NemoError` et `NEMO_ERRORS`, avec le même sens.
  - `class FileTranscriber(Protocol)` : attribut `speakers: bool` ; `transcribe(wav: Path, *, language: str, phrases: Sequence[str], boost: float, speakers: bool = False, partials: bool = False, on_progress: Callable[[float], None] | None = None) -> AsyncIterator[Final | Partial]`, dont `on_progress` reçoit les secondes d'audio traitées ; `async transcribe_chunk(wav: bytes, *, language: str, phrases: Sequence[str], boost: float) -> Final` ; `async ready() -> bool`.
  - `class LiveSession(Protocol)` : `async send_audio(pcm: bytes)`, `async commit()`, `async clear()`, `events() -> AsyncIterator[Partial | Final | Committed | Cleared]`.
  - `class LiveTranscriber(Protocol)` : attributs `server_vad: bool` et `many_commits: bool` ; `live(*, sample_rate: int, language: str, phrases: Sequence[str], boost: float, endpointing_ms: int | None = None) -> AbstractAsyncContextManager[LiveSession]`.
  - `class Synthesizer(Protocol)` : attribut `sample_rate: int` ; `async speak(text: str, *, voice: str, language: str) -> bytes` ; `async voices() -> list[str]` ; `async ready() -> bool`.
- Produces, dans `engines/__init__.py` : `@dataclass(frozen=True) class Engines` avec `stt: FileTranscriber`, `live: LiveTranscriber`, `tts: Synthesizer` ; `def build_engines(settings: Settings, http: httpx.AsyncClient) -> Engines`.
- Produces, dans `settings.py` : `stt_engine: str = "nemo"`, `live_engine: str = "nemo"`, `tts_engine: str = "nemo"` (variables `STT_ENGINE`, `LIVE_ENGINE`, `TTS_ENGINE`).
- Le code des routes lit `app.state.engines.stt`, `.live` et `.tts` à la place de `app.state.nemo`. `create_app(settings, engines=None, http=None)` remplace le paramètre `nemo`. `Services` porte `engines: Engines` à la place de `nemo`.
- `NemoClient` reçoit les attributs `speakers = True`, `server_vad` et `many_commits` (les valeurs actuelles des constantes `SERVER_VAD` et `NEMO_MANY_COMMITS` de `live.py`, qui disparaissent) et `sample_rate = 22050` (l'actuelle `TTS_SAMPLE_RATE`).
- La langue garde sa forme actuelle aux points d'appel (le résultat de `to_nemo_language`, par exemple `fr-FR` ou `auto`).

- [x] **Step 1 : écrire les tests qui échouent** dans `tests/test_engines.py` :
  - `test_build_engines_defaults_to_nemo` : avec `REQUIRED_ENV`, `build_engines` rend un `Engines` dont les trois rôles sont la même instance de `NemoClient`.
  - `test_unknown_engine_names_the_variable` : `STT_ENGINE=foo` lève `ValueError` dont le message contient `STT_ENGINE` et `foo`. Même test pour `LIVE_ENGINE` et `TTS_ENGINE`.
  - `test_nemo_declares_its_capabilities` : `speakers` vrai, `sample_rate == 22050`.
  - `test_engine_without_speakers_is_refused_at_startup` : un moteur de fichier dont `speakers` est faux fait lever à `build_engines` une `ValueError` qui nomme `STT_ENGINE` et le modèle `dictee-diarize`. Aucun adaptateur livré n'est dans ce cas : le test passe par un faux moteur. La progression par paliers fixes d'un moteur qui ne rend pas de progression n'est pas construite, puisque les deux adaptateurs livrés en rendent une.
- [x] **Step 2 : lancer** `make test ARGS="tests/test_engines.py"`. Attendu : échec à l'import de `dictee.engines`.
- [x] **Step 3 : écrire `ports.py` et `engines/__init__.py`, déplacer `nemo.py`, puis remplacer chaque usage** de `state.nemo`, `services.nemo`, `Nemo*` et des trois constantes. Dans `fakes.py`, `FakeNemo` reçoit les mêmes attributs de capacité, et `conftest.py` construit `Engines(stt=fake, live=fake, tts=fake)`.
- [x] **Step 4 : lancer** `make test`. Attendu : toute la suite passe, sans autre changement dans les tests que les renommages.
- [x] **Step 5 : commit** « Sépare le moteur en trois rôles derrière des protocoles ».

### Task 2 : l'image `crispasr/` et le compose de test

**Files:**
- Create: `crispasr/Dockerfile`, `crispasr/pull-models.sh`, `crispasr/serve.sh`
- Modify: `compose.integration.yaml`, `Makefile`, `integration/make-env.sh`

**Interfaces:**
- Produces : l'image construite depuis `crispasr/` avec l'argument de build `CRISPASR_VERSION`. Le Dockerfile reprend le schéma de `nemo/Dockerfile` : base `debian:trixie-…-slim`, téléchargement de `crispasr-linux-x86_64.tar.gz` de la release `v${CRISPASR_VERSION}` du dépôt `CrispStrobe/CrispASR`, empreinte lue dans l'API des releases et vérifiée, `ffmpeg` installé, utilisateur `10001`, binaire `crispasr` dans le `PATH`.
- `pull-models.sh` télécharge dans `/models/current/` les fichiers `asr.gguf` (`ASR_MODEL_URL`, `ASR_MODEL_SHA256`) et `diar.gguf` (`DIAR_MODEL_URL`, `DIAR_MODEL_SHA256`), vérifie chaque empreinte et ne retélécharge pas un fichier déjà bon. Une variable manquante arrête le script en la nommant.
- `serve.sh` lance `crispasr --server --host 0.0.0.0 --port 8080 --backend "$ASR_BACKEND" -m /models/current/asr.gguf --diarize-model /models/current/diar.gguf -t "$CRISPASR_THREADS" --server-workers "$CRISPASR_WORKERS"`, avec les clés de `CRISPASR_API_KEYS`. Une variable manquante arrête le script en la nommant.
- `compose.integration.yaml` reçoit les services `crispasr-models` et `crispasr` (port local `127.0.0.1:18002:8080`, limite 3 Go, healthcheck sur `/health`) et `phonon` (image `ghcr.io/gradium-ai/ptts-openai-server` au tag de la release `0.7.1`, `PTTS_MODEL`, `PTTS_LANG=fr`, `PTTS_QUANT=q8`, `HF_TOKEN`, port local `127.0.0.1:18003:8880`, limite 768 Mo). Les services `nemo` et `models` restent jusqu'à la Task 14.
- `Makefile` : cible `check-crispasr-version`, sur le modèle de `check-nemo-version`, exigée par `integration`.

- [x] **Step 1 : écrire les trois fichiers de `crispasr/`** et les services du compose de test. `integration/make-env.sh` écrit les nouvelles variables avec les valeurs de la spec (tableau « Images, modèles et compose ») ; `HF_TOKEN` et `PTTS_MODEL` viennent de l'environnement de celui qui lance, jamais du dépôt. `PTTS_MODEL` s'écrit `hf://gradium/phonon-40m-fr@<commit>` : relever le commit courant de la branche `main` de ce dépôt Hugging Face et le noter dans DIC-29, pour le `.env` de Dokploy.
- [x] **Step 2 : vérifier** `CRISPASR_VERSION=0.8.55 docker compose -f compose.integration.yaml up -d --build crispasr-models crispasr`, puis `curl -fs http://127.0.0.1:18002/health`. Attendu : `{"status": "ok", …}` après le chargement.
- [x] **Step 3 : relever le format réel des réponses** sur un court extrait public de `dictee/tests/fixtures/` : une requête `verbose_json` avec mots, une requête avec `diarize=true` et `diarize_method=sortformer`, et `GET /progress`. Ranger les trois réponses dans `dictee/tests/fixtures/crispasr/` (`chunk.json`, `diarized.json`, `progress.json`). Elles fondent les faux serveurs des Tasks 6 et 7.
- [x] **Step 4 : commit** « Ajoute l'image du serveur CrispASR et son compose de test ».

### Task 3 : le critère du direct, avant de construire le reste

Cette tâche est une mesure. Son résultat décide si la Task 8 se construit telle quelle.

**Files:**
- Create: `integration/live_words.py` (script `uv run --script`, sans dépendance au paquet dictee)

**Interfaces:**
- Produces : `integration/live_words.py --audio <wav 16 kHz> --model <gguf> [--stream-length MS] [--stream-step MS]` envoie l'audio au rythme réel à `crispasr --stream --stream-json --vad`, transcrit le même audio hors direct, et imprime le nombre de mots des textes finals, celui de la transcription hors direct, leur rapport, et les délais médians du texte partiel et du texte final. Il n'imprime aucun texte transcrit.

- [x] **Step 1 : écrire le script.**
- [x] **Step 2 : préparer l'audio.** Lire l'enregistrement du banc dans le Drive de Geoff (voir la spec, « Les enregistrements des mesures »), en extraire le passage de 255 à 431 s en WAV mono 16 kHz dans un dossier temporaire hors du dépôt.
- [x] **Step 3 : mesurer** dans le conteneur `crispasr` du compose de test, avec les réglages par défaut puis avec `--stream-length` et `--stream-step` ajustés.
- [x] **Step 4 : décider.** Si un réglage atteint 95 % des mots, noter ce réglage et les délais dans DIC-29 : la Task 8 l'utilise. Sinon, **s'arrêter et rapporter à Geoff** avec les mesures : la spec prévoit alors un adaptateur `window`, qui demande son accord.
- [x] **Step 5 : effacer la copie de travail de l'audio**, puis commit « Ajoute la mesure des mots rendus par le direct de CrispASR ».

### Task 4 : la correction du glossaire

**Files:**
- Create: `dictee/src/dictee/glossary.py`
- Modify: `dictee/src/dictee/transcribe.py`, `processing.py`, `live.py`
- Test: `dictee/tests/test_glossary.py`, et un test dans `test_transcriptions.py`, `test_processing.py` et `test_live.py`

**Interfaces:**
- Consumes : `GlossaryEntry(term: str, heard_as: tuple[str, ...])` de `dictee/profile.py`, `Final` et `Word` de `dictee/types.py`.
- Produces : `def correct_text(text: str, entries: Iterable[GlossaryEntry]) -> str` ; `def correct_words(words: Sequence[Word], entries: Iterable[GlossaryEntry]) -> tuple[Word, ...]` ; `def correct_final(final: Final, entries: Iterable[GlossaryEntry]) -> Final`.
- Règles : chaque variante de `heard_as` est remplacée par `term`, par mot entier, sans tenir compte de la casse. Les variantes les plus longues passent d'abord. Une variante de plusieurs mots remplace la suite de mots correspondante par un seul `Word` (début du premier, fin du dernier, personne du premier). Ce qui entoure la variante dans le mot, avant comme après (élision, préfixe et trait d'union, ponctuation), est gardé. Une entrée sans variante ne change rien.
- `correct_final` s'applique à chaque `Final` qui sort d'un moteur, aux trois endroits où il est lu : la boucle de `_Job.recognize` (`transcribe.py`), les deux chemins de `processing.py` (morceau et session), et le relais des textes finals de `live.py`. Le glossaire utilisé est celui que ces endroits passent déjà au moteur.

- [x] **Step 1 : écrire les tests qui échouent** dans `tests/test_glossary.py` :
  - `test_variant_is_replaced_by_term` : `correct_text("le projet tarvél avance", [GlossaryEntry("Tarvel", ("tarvél",))]) == "le projet Tarvel avance"`.
  - `test_match_ignores_case` : `"TARVÉL"` et `"Tarvél"` donnent `"Tarvel"`.
  - `test_variant_inside_longer_word_is_kept` : avec `GlossaryEntry("IT", ("it",))`, `"itlm"` reste `"itlm"`.
  - `test_variant_with_regex_characters` : une variante `"c++ (lang)"` se remplace sans erreur et sans rien toucher d'autre.
  - `test_longest_variant_wins` : avec les variantes `"claude"` et `"claude enterprise"` de deux entrées, `"claude enterprise"` donne le terme de la seconde.
  - `test_multi_word_variant_merges_words` : trois `Word` dont deux forment la variante deviennent deux `Word`, avec le début du premier et la fin du dernier.
  - `test_punctuation_is_kept` : `"tarvél,"` donne `"Tarvel,"`.
  - `test_entry_without_variant_changes_nothing`.
- [x] **Step 2 : lancer** `make test ARGS="tests/test_glossary.py"`. Attendu : échec à l'import.
- [x] **Step 3 : écrire `glossary.py`.** Une seule expression régulière compilée par appel, faite des variantes échappées et triées par longueur décroissante, bornée par des limites de mot Unicode.
- [x] **Step 4 : brancher `correct_final`** aux trois endroits, avec un test par endroit qui passe un glossaire à une variante et lit le terme corrigé dans la réponse (`verbose_json` d'une tâche, texte d'un flux, texte final du direct).
- [x] **Step 5 : lancer** `make test`. Attendu : tout passe.
- [x] **Step 6 : commit** « Corrige le glossaire dans dictee après chaque transcription ».

### Task 5 : les réglages des nouveaux adaptateurs

**Files:**
- Modify: `dictee/src/dictee/settings.py`, `dictee/src/dictee/engines/__init__.py`
- Test: `dictee/tests/test_settings.py`, `dictee/tests/test_engines.py`

**Interfaces:**
- Produces, dans `Settings` (variable d'environnement, valeur par défaut) : `crispasr_url` (`CRISPASR_URL`, `http://crispasr:8080`), `crispasr_api_key` (`CRISPASR_API_KEY`, vide), `live_command` (`LIVE_COMMAND`, `crispasr`), `live_model_path` (`LIVE_MODEL_PATH`, `/models/current/asr.gguf`), `live_max_sessions` (`LIVE_MAX_SESSIONS`, `2`), `live_threads` (`LIVE_THREADS`, `4`), `live_stream_args` (`LIVE_STREAM_ARGS`, vide : les réglages de fenêtre retenus en Task 3), `tts_url` (`TTS_URL`, `http://phonon:8880`), `tts_sample_rate` (`TTS_SAMPLE_RATE`, `24000`), `tts_max_concurrent` (`TTS_MAX_CONCURRENT`, `1`).
- `NEMO_API_KEY` n'est exigée que si un des trois rôles vaut `nemo`. `CRISPASR_API_KEY` n'est exigée que si `STT_ENGINE` vaut `crispasr`.

- [x] **Step 1 : tests qui échouent** : `test_nemo_key_not_required_without_nemo_engine`, `test_crispasr_key_required_with_crispasr_engine` (le message nomme `CRISPASR_API_KEY`), `test_live_max_sessions_must_be_an_integer`.
- [x] **Step 2 : lancer**, voir l'échec, **écrire les champs**, relancer, voir le succès.
- [x] **Step 3 : commit** « Ajoute les réglages des adaptateurs CrispASR et de voix ».

### Task 6 : adaptateur `crispasr`, morceau et disponibilité

**Files:**
- Create: `dictee/src/dictee/engines/crispasr.py`
- Modify: `dictee/src/dictee/engines/__init__.py`, `dictee/tests/fakes.py`
- Test: `dictee/tests/test_engine_crispasr.py`

**Interfaces:**
- Consumes : les fixtures de la Task 2, `Settings.crispasr_url` et `crispasr_api_key`.
- Produces : `class CrispasrTranscriber` avec `__init__(self, base_url: str, api_key: str, http: httpx.AsyncClient)`, `speakers = True`, `async transcribe_chunk(...) -> Final`, `async ready() -> bool`. `build_engines` le rend pour `STT_ENGINE=crispasr`.
- `FakeCrispasr` dans `fakes.py` : un `httpx.MockTransport` qui sert `/health`, `/progress` et `/v1/audio/transcriptions` d'après les fixtures et garde la liste des requêtes reçues (champs du formulaire compris).
- Comportement : `POST /v1/audio/transcriptions` en multipart avec `response_format=verbose_json`, l'horodatage par mot et `language` (le code reçu ramené à ISO-639-1 par `to_iso_language`, `auto` gardé tel quel). L'en-tête `Authorization: Bearer` porte la clé. `phrases` et `boost` sont reçus et ignorés. Une erreur de connexion, un délai dépassé ou un 503 lèvent `EngineUnavailable` ; une autre réponse d'erreur lève `EngineError` avec un message fixe, sans reprendre le corps de la réponse.

- [x] **Step 1 : tests qui échouent** :
  - `test_chunk_returns_words_with_times` : les mots de `chunk.json` sortent en `Word` avec `start` et `end`.
  - `test_chunk_never_sends_hotwords` : avec `phrases=["Tarvel"]`, le formulaire reçu ne contient ni `hotwords` ni `hotwords_boost`.
  - `test_language_is_iso` : `fr-FR` part en `fr`, `auto` part en `auto`.
  - `test_ready_is_false_while_loading` : `/health` à 503 donne `False`, à 200 donne `True`.
  - `test_unreachable_server_is_unavailable` et `test_error_message_does_not_quote_the_body`.
- [x] **Step 2 : lancer**, voir l'échec.
- [x] **Step 3 : écrire `CrispasrTranscriber`.**
- [x] **Step 4 : lancer**, voir le succès.
- [x] **Step 5 : jouer `test_processing.py` avec ce moteur** : un test `test_task_runs_on_crispasr_chunks` monte `Services` avec `FakeCrispasr` et vérifie le résultat d'une tâche sans locuteurs.
- [x] **Step 6 : vérifier l'attente du démarrage** : `test_task_waits_while_engine_loads` fait répondre 503 puis 200 à `/health` et vérifie que la tâche finit sans échec.
- [x] **Step 7 : commit** « Ajoute l'adaptateur CrispASR pour les morceaux de fichier ».

### Task 7 : adaptateur `crispasr`, locuteurs et fichier en flux

**Files:**
- Modify: `dictee/src/dictee/engines/crispasr.py`, `dictee/src/dictee/cuts.py`
- Test: `dictee/tests/test_engine_crispasr.py`, `dictee/tests/test_cuts.py`, `dictee/tests/test_transcriptions.py`

**Interfaces:**
- Produces : `CrispasrTranscriber.transcribe(wav, *, language, phrases, boost, speakers=False, partials=False, on_progress=None) -> AsyncIterator[Final | Partial]`.
  - Avec `speakers=True` : une seule requête pour le fichier entier, avec `diarize=true` et `diarize_method=sortformer`. Pendant la requête, `GET /progress` est lu toutes les 5 s et sa valeur (0 à 100), rapportée à la durée du fichier, appelle `on_progress` avec les secondes d'audio traitées. Le résultat sort en un seul `Final` dont chaque `Word` porte sa personne.
  - Avec `speakers=False` : le fichier est coupé aux silences en morceaux de 15 s au plus, chaque morceau passe par `transcribe_chunk`, et chaque morceau fini sort en un `Final` dont les mots sont décalés à leur instant dans le fichier. Aucun `Partial` ne sort.
- Produces, dans `cuts.py` : `plan_cuts(duration: float, silences: Sequence[Silence], max_chunk_s: float = MAX_CHUNK_S) -> list[float]`. La valeur par défaut garde le comportement actuel.
- Constante `STREAM_CHUNK_S = 15.0` dans `crispasr.py`.

- [x] **Step 1 : tests qui échouent** :
  - `test_speakers_request_asks_for_sortformer` : le formulaire porte `diarize=true` et `diarize_method=sortformer`, et les mots de `diarized.json` sortent avec leur personne.
  - `test_progress_is_reported_in_seconds_of_audio` : `/progress` à 50 sur un fichier de 20 s appelle `on_progress(10.0)`.
  - `test_stream_yields_one_final_per_chunk` : un audio de 40 s avec deux silences donne trois `Final`, aux instants décalés.
  - `test_plan_cuts_respects_max_chunk` : avec `max_chunk_s=15`, aucun morceau ne dépasse 15 s.
  - Dans `test_transcriptions.py`, `test_stream_emits_one_delta_per_chunk_on_crispasr` : une requête `stream=true` rend autant de `transcript.text.delta` que de morceaux, puis `transcript.text.done` avec le texte entier.
- [x] **Step 2 : lancer**, voir l'échec.
- [x] **Step 3 : écrire `transcribe` et le paramètre de `plan_cuts`.**
- [x] **Step 4 : lancer** `make test`. Attendu : tout passe.
- [x] **Step 5 : commit** « Ajoute les locuteurs et le fichier en flux à l'adaptateur CrispASR ».

### Task 8 : adaptateur `crispasr-stream`, la dictée en direct

**Files:**
- Create: `dictee/src/dictee/engines/crispasr_stream.py`, `dictee/tests/fixtures/fake_crispasr_stream.py`
- Modify: `dictee/src/dictee/engines/__init__.py`, `dictee/src/dictee/live.py`
- Test: `dictee/tests/test_engine_crispasr_stream.py`, `dictee/tests/test_live.py`

**Interfaces:**
- Consumes : `Settings.live_command`, `live_model_path`, `live_max_sessions`, `live_threads`, `live_stream_args` ; les réglages de fenêtre retenus en Task 3.
- Produces : `class CrispasrLive` avec `__init__(self, command: str, model_path: str, *, max_sessions: int, threads: int, stream_args: Sequence[str] = ())`, `server_vad = False`, `many_commits = True`, et `live(...)` qui rend une session. `build_engines` le rend pour `LIVE_ENGINE=crispasr-stream`.
- Une session lance `<command> --stream --stream-json --vad -m <model_path> -l <langue ISO> -t <threads> <stream_args>`, précédé d'un `ffmpeg` qui ramène le PCM reçu (`sample_rate`) à du PCM16 mono 16 kHz sans mise en tampon.
- Texte partiel : CrispASR envoie le texte entier et révisé de la prise de parole. La session garde le texte déjà émis et n'émet en `Partial` que les mots nouveaux qui suivent le plus long préfixe commun entre le texte précédent et le texte reçu, une fois ce préfixe confirmé par deux événements successifs. Elle ne retire jamais un mot émis.
- Texte final : l'événement `final` sort en `Final(text, words=())`.
- `commit()` envoie du silence jusqu'à l'événement `final` de la prise de parole en cours (au moins le silence que CrispASR attend pour la fermer, 800 ms par défaut), puis émet `Committed`. Une prise de parole sans parole émet `Committed` sans `Final`.
- `clear()` écarte la prise de parole en cours et son `final` à venir, puis émet `Cleared`.
- Au-delà de `max_sessions` sessions ouvertes, `live(...)` lève `EngineOverloaded`. Un binaire introuvable ou un processus qui s'arrête lève `EngineUnavailable`. La sortie du bloc `async with`, une annulation ou une erreur tue les deux processus et libère la place.
- `fake_crispasr_stream.py` est un script Python exécutable qui joue le rôle du binaire dans les tests : il lit l'entrée standard et écrit des événements JSON d'après un scénario donné par une variable d'environnement.

- [x] **Step 1 : tests qui échouent** :
  - `test_partial_emits_only_stable_new_words` : les textes `"bonjour"`, `"bonjour à"`, `"bonjour à tous"` donnent les `Partial` `"bonjour"`, `" à"`, puis `" tous"` après confirmation.
  - `test_revised_partial_never_retracts` : après `"bonjour à"` émis, un texte `"bonsoir à tous"` n'émet rien qui retire `"bonjour"`.
  - `test_final_event_becomes_final`.
  - `test_commit_sends_silence_then_committed` : le faux binaire reçoit au moins 800 ms de silence après le commit, et la session émet `Final` puis `Committed`.
  - `test_commit_without_speech_emits_committed_only`.
  - `test_clear_drops_the_turn_and_emits_cleared`.
  - `test_audio_reaches_the_process_at_16khz` : 1 s de PCM à 24 kHz arrive en 32000 octets environ.
  - `test_third_session_is_overloaded_then_slot_frees`.
  - `test_process_is_killed_when_session_closes` et `test_process_is_killed_when_cancelled` : aucun processus fils ne reste.
  - `test_missing_binary_is_unavailable`.
- [x] **Step 2 : lancer**, voir l'échec.
- [x] **Step 3 : écrire `CrispasrLive` et sa session.**
- [x] **Step 4 : rejouer `test_live.py` avec ce moteur** : `test_live_dictation_on_crispasr_stream` ouvre `/v1/realtime`, envoie de l'audio, valide, et lit `conversation.item.input_audio_transcription.delta` puis `completed`. `test_live_overloaded_closes_with_engine_busy` vérifie l'erreur que `live.py` rend déjà pour un moteur saturé.
- [x] **Step 5 : lancer** `make test`. Attendu : tout passe.
- [x] **Step 6 : commit** « Ajoute la dictée en direct par un processus CrispASR par session ».

### Task 9 : adaptateur `openai`, la voix

**Files:**
- Create: `dictee/src/dictee/engines/openai_tts.py`
- Modify: `dictee/src/dictee/engines/__init__.py`, `dictee/src/dictee/speech.py`, `dictee/src/dictee/web.py`, `dictee/tests/fakes.py`
- Test: `dictee/tests/test_engine_openai_tts.py`, `dictee/tests/test_speech.py`, `dictee/tests/test_web.py`

**Interfaces:**
- Produces : `class OpenAISpeech` avec `__init__(self, base_url: str, http: httpx.AsyncClient, *, sample_rate: int, max_concurrent: int)`, l'attribut `sample_rate`, `async speak(text, *, voice, language) -> bytes`, `async voices() -> list[str]`, `async ready() -> bool`. `build_engines` le rend pour `TTS_ENGINE=openai`.
- `speak` envoie `POST /v1/audio/speech` avec `{"model": "tts-1", "input": text, "voice": voice, "response_format": "pcm"}` et rend le corps, du PCM16 mono à `sample_rate`. Il n'envoie pas `speed`.
- `voices` lit `GET /v1/audio/voices`, dont la réponse est `{"voices": [...], "aliases": {...}}`, et rend la liste `voices`.
- `ready` lit `GET /health`.
- File : un sémaphore de `max_concurrent` places borne les appels à `speak`. Un 429 reçu malgré cela est réessayé toutes les 0,5 s pendant 10 s au plus, puis lève `EngineOverloaded`. Une erreur de connexion lève `EngineUnavailable`, une autre réponse d'erreur lève `EngineError` avec un message fixe.
- `speech.py` : `encode_stream(..., input_rate=state.engines.tts.sample_rate)`. `resolve_voice(requested, profile_voice, available, default_voice)` rend la voix demandée si le moteur la propose, sinon celle du profil si le moteur la propose, sinon `default_voice`. Une voix de profil inconnue ne part plus telle quelle au moteur ; l'avertissement au journal reste.
- `FakeVoiceServer` dans `fakes.py` : un `httpx.MockTransport` qui sert les trois routes et garde les requêtes reçues.

- [x] **Step 1 : tests qui échouent** :
  - `test_speak_asks_for_pcm_without_speed`.
  - `test_voices_reads_the_voices_list` : `{"voices": ["Lucien", "Arnaud"], "aliases": {"nova": "Lucien"}}` donne `["Lucien", "Arnaud"]`.
  - `test_calls_wait_for_their_turn` : deux `speak` lancés ensemble avec `max_concurrent=1` n'atteignent jamais le serveur en même temps.
  - `test_busy_server_is_retried_then_overloaded`.
  - `test_unreachable_voice_server_is_upstream_error` : dans `test_speech.py`, la route rend 502 avec le code `upstream_error`.
  - `test_unknown_profile_voice_falls_back_to_default` : un profil à la voix `Leo` obtient du son, et le serveur a reçu `Lucien`.
  - `test_output_rate_follows_the_engine` : avec `sample_rate=24000`, un WAV rendu dure le bon temps.
- [x] **Step 2 : lancer**, voir l'échec.
- [x] **Step 3 : écrire `OpenAISpeech` et adapter `speech.py` et `web.py`.**
- [x] **Step 4 : lancer** `make test`. Attendu : tout passe.
- [x] **Step 5 : commit** « Ajoute l'adaptateur de voix compatible OpenAI ».

### Task 10 : l'image de dictee et l'intégration avec les vrais modèles

**Files:**
- Modify: `dictee/Dockerfile`, `compose.integration.yaml`, `Makefile`, `integration/run.py`, `integration/make-env.sh`
- Create: `integration/check_crispasr.py` (remplace `check_nemo.py` à la Task 14)

**Interfaces:**
- Produces : l'étape `runtime` de `dictee/Dockerfile` reçoit l'argument de build `CRISPASR_VERSION` et installe le binaire `crispasr` de la même release, avec la même vérification d'empreinte que `crispasr/Dockerfile`. L'étape `test` ne l'installe pas.
- `compose.integration.yaml` : le service `dictee` monte le volume des modèles en lecture seule, dépend de `crispasr` et de `phonon`, et son `.env` de test met `STT_ENGINE=crispasr`, `LIVE_ENGINE=crispasr-stream`, `TTS_ENGINE=openai`, `DEFAULT_VOICE=Lucien`.
- `integration/run.py` garde ses contrôles (transcription, flux, tâche avec locuteurs, direct, voix, raccrochage) et les joue contre les nouveaux moteurs.

- [x] **Step 1 : modifier l'image, le compose de test et les contrôles.**
- [x] **Step 2 : lancer** `CRISPASR_VERSION=0.8.55 make integration`. Attendu : tous les contrôles passent. Le contrôle de voix vérifie que le WAV rendu est à 24000 Hz et qu'il dure plus de 1 s pour la phrase d'essai.
- [x] **Step 3 : relever** la mémoire du conteneur `dictee` au repos et pendant une dictée en direct, et la noter dans DIC-29.
- [x] **Step 4 : commit** « Branche l'intégration sur CrispASR et le serveur de voix ».

### Task 11 : documentation et demande de fusion

**Files:**
- Modify: `README.md`, `AGENTS.md`

- [x] **Step 1 : mettre à jour `README.md`** : les moteurs et les trois variables, le glossaire porté par dictee, le rythme des deltas d'un fichier en flux, les voix, et les licences (liste de la spec, « Ce qui reste au constructeur »).
- [x] **Step 2 : mettre à jour `AGENTS.md`** : les principes parlent des moteurs par rôle et non plus de NeMo seul, la commande `make integration` demande `CRISPASR_VERSION`, et les specs en cours citent la spec du 28 novembre.
- [x] **Step 3 : commit** « Documente les moteurs par rôle ».
- [ ] **Step 4 : [accord] pousser la branche et ouvrir la PR.** Geoff relit et fusionne. La fusion ne déploie rien.

### Task 12 : le catalogue de l'updater

**Dépôt :** `dokployconfig`, branche `feat/dictee-moteurs-audio`.

- [ ] **Step 1 : lire** `updater/catalog.json` et la façon dont `NEMO_VERSION` et l'image du service `conversation` y sont inscrites.
- [ ] **Step 2 : ajouter** la pin `CRISPASR_VERSION` (releases GitHub de `CrispStrobe/CrispASR`) et l'image `ghcr.io/gradium-ai/ptts-openai-server`, en suivant les tests du dépôt.
- [ ] **Step 3 : [accord] ouvrir la PR.** Geoff fusionne.

### Task 13 : le compose de Dokploy et la bascule

Toutes les écritures de cette tâche demandent l'accord de Geoff. Les lectures et les écritures passent par `dokployconfig/lib/dokploy.py` avec le secret `DOKPLOY` (skill bitwarden, `run --only`), et n'impriment que des noms et des empreintes.

**Constats de la construction à prendre en compte.**

- **Ordre de fusion.** L'image de dictee arrête son build quand l'argument `CRISPASR_VERSION` manque, et Dokploy construit depuis `main`. L'argument de build entre donc dans le compose de Dokploy, pour le service `dictee`, avant la fusion de la branche ; il est sans effet sur l'image d'avant.
- **Variables en plus.** `CRISPASR_WORKERS` manque au tableau des variables de la spec, et `crispasr/serve.sh` refuse de démarrer sans elle. `VAD_MODEL_URL` et `VAD_MODEL_SHA256` ont été ajoutées au tableau pendant la construction, et `DIAR_METHOD`, qui y figure, est désormais lue par dictee. Ces quatre variables doivent être dans le `.env` de Dokploy.
- **Voix par défaut.** `DEFAULT_VOICE=Lucien` doit être en place avant `TTS_ENGINE=openai`, parce que la valeur par défaut du code (`Leo`) est une voix de l'ancien moteur, que le serveur de voix refuse.
- **Mémoire de dictee.** Sa limite doit couvrir les processus de dictée en direct : la mesure donne 913 Mio avec une session et 1,1 Gio avec deux, et elle est à refaire sur la sidebox.
- **Fils de calcul.** Deux sessions de direct à 4 fils et le serveur CrispASR à 4 fils dépassent les 6 vCPU de la sidebox. `LIVE_THREADS` et `LIVE_STREAM_ARGS` se règlent donc à la mesure ; par exemple, `--stream-partial-decode-ms 2000` a été essayé avec succès sur une machine lente.
- **Vérifications à faire après la bascule.** Il faut lancer une transcription réelle, parce que `/health` de CrispASR se lit sans clé et qu'une clé fausse ne se voit qu'à la première requête. Il faut aussi lancer une dictée en direct, parce que dictee démarre sain même sans le volume des modèles. Il faut vérifier que les apps ferment le WebSocket après chaque dictée, parce qu'une connexion garde sa place et son processus jusqu'à sa fermeture. Il faut enfin transcrire une réunion de plus d'une heure avec locuteurs, pour la mémoire et la durée.

- [ ] **Step 1 : relever l'état de départ** : la mémoire disponible de la sidebox, les empreintes de l'environnement des conteneurs `dictee`, `nemo`, `conversation` et `conversation-gate`, et le compose actuel gardé pour le retour arrière.
- [ ] **Step 2 : [accord] ajouter les services** `crispasr-models`, `crispasr` et `phonon`, les arguments de build `CRISPASR_VERSION`, le volume des modèles monté en lecture seule dans `dictee`, et les variables du tableau de la spec. `CRISPASR_API_KEYS` reçoit une valeur aléatoire générée sur place. `HF_TOKEN` est copié depuis Bitwarden dans le `.env` par le script, sans jamais être affiché. Les trois variables de moteur restent sur `nemo`.
- [ ] **Step 3 : redéployer et vérifier** : les six services sont sains, l'environnement de `nemo`, `conversation` et `conversation-gate` n'a pas changé (empreintes), et la mémoire disponible est relevée. Si elle ne suffit pas, appliquer l'ordre de la spec : `LIVE_MAX_SESSIONS=1`, puis un seul worker CrispASR.
- [ ] **Step 4 : [accord] basculer** : `STT_ENGINE=crispasr`, `LIVE_ENGINE=crispasr-stream`, `TTS_ENGINE=openai`, `DEFAULT_VOICE=Lucien`, puis redémarrer `dictee`.
- [ ] **Step 5 : mesurer à travers dictee** et inscrire dans DIC-29 : une tâche `dictee-diarize` sur les 5 minutes du banc (temps, RTF, RAM et CPU du service `crispasr`, nombre de personnes), une dictée en direct (délai du texte partiel et du texte final, RAM et CPU de `dictee`), la phrase lue (temps avant le premier son, temps total, RAM de `phonon`), et le délai d'une dictée courte lancée pendant une tâche longue.
- [ ] **Step 6 : mesurer une réunion longue** : une tâche avec locuteurs sur un enregistrement de plus d'une heure. Si la mémoire de `crispasr` dépasse sa limite, **s'arrêter et rapporter à Geoff** avec la mesure.
- [ ] **Step 7 : rendre la main à Geoff** pour ses essais : une réunion depuis dictee-app, une dictée dans Dictate, la dictée et la lecture à voix haute d'Open WebUI. Le retour arrière tient en trois variables remises sur `nemo`.

### Task 14 : la sortie de NeMo

Cette tâche ne commence qu'après le feu vert de Geoff sur ses essais.

**Files:**
- Delete: `dictee/src/dictee/engines/nemo.py`, `dictee/tests/test_engine_nemo.py`, `nemo/`, `integration/check_nemo.py`, `integration/nemo_rt.py`, `integration/boost.py`
- Modify: `dictee/src/dictee/settings.py`, `engines/__init__.py`, `modes.py`, `dictee/tests/fakes.py`, `compose.integration.yaml`, `Makefile`, `README.md`, `AGENTS.md`, `docs/superpowers/specs/2026-10-31-dictee-server-design.md`

- [ ] **Step 1 : tests qui échouent** : `test_engines_default_to_crispasr_and_openai` (les valeurs par défaut des trois variables deviennent `crispasr`, `crispasr-stream`, `openai`) et `test_nemo_is_not_an_engine` (`STT_ENGINE=nemo` lève `ValueError`).
- [ ] **Step 2 : retirer** l'adaptateur, ses tests, `FakeNemo` (remplacé dans `conftest.py` par les faux moteurs des Tasks 6 à 9), les réglages `NEMO_*` et `GLOSSARY_BOOST`, `to_nemo_language` (les points d'appel passent un code ISO), le dossier `nemo/` et les services `nemo` et `models` du compose de test.
- [ ] **Step 3 : lancer** `make test` puis `CRISPASR_VERSION=0.8.55 make integration`. Attendu : tout passe.
- [ ] **Step 4 : mettre à jour** `README.md`, `AGENTS.md` et les sections « moteur » de la spec de référence, puis commit « Retire NeMo-Speech.cpp ».
- [ ] **Step 5 : [accord] PR**, fusion par Geoff.
- [ ] **Step 6 : [accord] Dokploy** : retirer le service `nemo`, l'ancien service `models`, les variables `NEMO_*` et `GLOSSARY_BOOST` et l'argument `NEMO_VERSION`, redéployer et vérifier que les services restants sont sains.
- [ ] **Step 7 : [accord] supprimer le volume des modèles de NeMo**, après avoir décrit le volume et sa taille à Geoff.
- [ ] **Step 8 : [accord] retirer la pin `NEMO_VERSION`** du catalogue de l'updater (PR sur `dokployconfig`).

### Task 15 : le speech to speech en local, à l'essai

Le service `conversation` ne change pas dans cette tâche.

- [ ] **Step 1 : mesurer** depuis la sidebox, sur cinq essais, le délai d'une transcription courte par dictee (`POST /v1/audio/transcriptions`, modèle `dictee`, une phrase de 5 s) et le temps avant le premier son d'une phrase par `POST /v1/audio/speech`.
- [ ] **Step 2 : comparer** aux délais des alias hébergés `transcribe` et `speech`, relevés par `s2s/probe_litellm.py`.
- [ ] **Step 3 : rapporter à Geoff** les deux séries de délais. Il décide s'il veut un chantier pour faire passer le mode conversation par dictee.

### Task 16 : clôture

- [ ] **Step 1 : [accord] supprimer le compose temporaire `dictee-bench`** de Dokploy et son volume.
- [ ] **Step 2 : effacer les fichiers de travail** : le dossier `.scratch-voix` du dépôt, le dossier `dictee-bench` de la devbox (qui contient des extraits audio de travail) et les copies de l'enregistrement du banc.
- [ ] **Step 3 : archiver le plan** : `git mv docs/superpowers/plans/2026-11-28-moteurs-audio.md docs/archive/`, commit « Archive le plan des moteurs audio ».
- [ ] **Step 4 : inscrire dans DIC-29** le bilan : les mesures finales, ce qui a été retiré, et ce qui reste ouvert s'il en reste.
