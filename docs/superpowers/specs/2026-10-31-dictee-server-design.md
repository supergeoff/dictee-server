# dictee-server : design

- **Date** : 31 octobre 2026
- **Statut** : design validé par Geoff, partie par partie, le 31 octobre 2026
- **Mise à jour** : 1er novembre 2026, avec les tâches enregistrées, le découpage en morceaux et la dictée en direct, livrés et décrits ci-dessous. L'histoire de ce changement (objectifs, approches écartées, mesures) est dans `2026-11-01-taches-transcription-design.md`.
- **Dépôt** : `supergeoff/dictee-server`, public

## 1. Objet

dictee devient le point d'entrée unique des besoins audio de la stack de Geoff. Il remplace speaches et l'ancien proxy de dictée du dépôt `openwebuitools`, et sert quatre usages.

| Usage | Appelant typique |
|---|---|
| Transcription longue avec locuteurs | L'app Android dictee, pour les enregistrements du MECHEN V05 |
| Transcription rapide brute | Dictate, qui fait sa propre reformulation |
| Transcription rapide reformulée par un LLM | La dictée d'Open WebUI |
| Synthèse vocale rapide en français | La lecture à voix haute et le mode appel d'Open WebUI |

Le mode appel d'Open WebUI ne demande pas de moteur de conversation : il enchaîne une transcription rapide et une synthèse vocale rapide, deux routes que dictee sert déjà.

Chaque membre de la famille règle son espace sur une petite page de configuration, protégée par PocketID comme le mcp-broker.

## 2. Existant

- **speaches.** Compose `ai-speaches-owaigp` (Dokploy `-kcyUCYKIznpi960QM_D5`, projet `ai`), image `ghcr.io/speaches-ai/speaches:0.9.0-rc.3-cpu`, limite de 3 Go, domaine public `speaches.supergeoff.top`. Il sert Parakeet pour la transcription et Piper (`fr_FR-tom-medium`) pour la synthèse vocale.
- **Proxy dictee.** Compose `ai-dictee-tltwgv` (Dokploy `xUkfJ_DJkAF2Xrskxsank`, projet `ai`), sans domaine. Son code est `services/dictee/app.py` du dépôt `openwebuitools`, monté comme fichier avec le glossaire de Geoff. Il transmet la requête à speaches, fait reformuler le texte par LiteLLM (`flash-low`) et rend le texte brut en cas d'échec.
- **Open WebUI 0.12.4.** La transcription pointe vers `http://ai-dictee-tltwgv-dictee-1:8000/v1`, la synthèse vocale vers `http://ai-speaches-owaigp-speaches-1:8000/v1` avec la voix `tom`, et le mode temps réel est désactivé. Ces réglages vivent dans la base d'Open WebUI, pas dans son compose. `ENABLE_FORWARD_USER_INFO_HEADERS` est actif : Open WebUI joint les en-têtes `X-OpenWebUI-User-*` à ses appels, et LiteLLM attribue chaque appel grâce à `user_header_mappings` sur `X-OpenWebUI-User-Email`.
- **Dictate.** L'app appelle `speaches.supergeoff.top` et reformule elle-même par LiteLLM.

## 3. Décisions

| Sujet | Décision |
|---|---|
| Moteur | NeMo-Speech.cpp (NVIDIA, Apache 2.0) en build CPU, version 0.3.0 du 30 octobre 2026. Cette version corrige l'attribution des mots placés à un changement de locuteur et borne les horodatages à la durée réelle de l'audio (#87). La règle des sept jours la rend adoptable le 6 novembre 2026. |
| Transcription | Nemotron 3.5 ASR streaming 0.6b, réglé comme Handy avec 1,12 s de contexte à droite (`rnnt_right_context` à 13). Un fichier passe par la route REST de NeMo, morceau par morceau (section 6.1). La route temps réel de NeMo sert les réunions, le texte progressif et la dictée en direct. |
| Locuteurs | Nemotron 3 Diarization, preset `v3-offline`, huit locuteurs au plus. |
| Synthèse vocale | Magpie TTS multilingual 357m avec le décodeur NanoCodec. Si elle est trop lente, on coupe le CFG plutôt que de revenir à Piper. |
| Quantization | Q8_0 partout, comme dans Vibe et Handy. |
| Utilisateurs | Un espace par compte PocketID, sans configuration globale gérée par un admin. |
| API | Compatible OpenAI. Toute transcription de fichier sans `stream` est une tâche enregistrée dans la base de dictee : elle survit à la coupure du client et au redémarrage de dictee, elle se lance en arrière-plan avec `background=true`, et un envoi identique rejoint la tâche existante (sections 5.6 et 6.1). |
| Dictée en direct | Une connexion WebSocket sur `/v1/realtime?intent=transcription`, au format du Realtime d'OpenAI en mode transcription, pour le texte brut seulement (section 5.7). |
| Conservation | dictee garde l'audio d'une tâche jusqu'à sa fin et son texte 24 heures après. Le texte progressif (`stream=true`) et la dictée en direct ne sont pas enregistrés (section 7.3). |
| Dépôt | Public, sans rien de personnel dans le code. |
| Construction | Dokploy construit les services depuis ce dépôt, en contexte git sur `main`, car NVIDIA ne publie pas d'image CPU. |
| Version de NeMo | Un argument de build du compose. L'updater du dimanche apprendra à suivre les releases GitHub (carte Kaneo STK-43, hors de ce dépôt). |
| Identité Open WebUI | L'en-tête `X-OpenWebUI-User-Email`, accepté seulement avec la clé de service d'Open WebUI. Le jeton signé d'Open WebUI remplacerait cet en-tête et casserait l'attribution de LiteLLM, donc il reste inactif. |
| Modèle de reformulation | La case `simple-low` de la grille linéaire de LiteLLM, lue dans `CLEANUP_MODEL`. |

## 4. Architecture et déploiement

### 4.1 Compose

On réécrit le compose `dictee` existant au lieu d'en créer un nouveau. Le service garde ainsi son nom réseau `ai-dictee-tltwgv-dictee-1`, déjà connu d'Open WebUI. Le compose reste en mode raw dans Dokploy et suit les conventions de la stack : `restart: unless-stopped` (`"no"` pour un service d'initialisation), `env_file: .env` sur chaque service, `expose` au lieu de `ports`, tags explicites, volumes nommés en bas par ordre alphabétique.

| Service | Rôle | Construction | Mémoire | Réseaux |
|---|---|---|---|---|
| `models` | Télécharge les modèles dans le volume, puis s'arrête | Contexte git `#main:nemo` | 512 Mo | Réseau du compose |
| `nemo` | `nemo-speech serve` : transcription, locuteurs, synthèse vocale | Contexte git `#main:nemo` | 3 Go | Réseau du compose seulement |
| `dictee` | API compatible OpenAI, clés, réglages, page, conversion audio, tâches de transcription, dictée en direct, appel à LiteLLM | Contexte git `#main:dictee` | 512 Mo | Réseau du compose et `dokploy-network` |

`nemo` démarre quand `models` s'est terminé avec succès (`service_completed_successfully`). `dictee` démarre quand `nemo` répond sur `/ready` (`service_healthy`). Seul `dictee` joint `nemo`, et la clé de `nemo` (`--api-key`) refuse tout autre appelant du réseau.

Volumes : `dictee-data` porte la base SQLite de dictee et, le temps d'une tâche, son audio (section 4.7), `nemo-models` porte les modèles.

### 4.2 Image `nemo`

`nemo/Dockerfile` part d'une image Debian slim à tag explicite. Il reçoit `NEMO_VERSION` en argument de build, télécharge l'archive `nemo-speech-${NEMO_VERSION}-linux-x86_64-cpu.tar.gz` de la release GitHub de NVIDIA et compare son empreinte SHA-256 avec celle que GitHub publie pour cette pièce jointe. Une empreinte différente arrête le build. L'image contient `curl`, dont `nemo-speech pull` a besoin, et tourne sous un utilisateur non root.

Le compose écrit la version une seule fois, dans `args` du bloc `x-nemo-build` (`NEMO_VERSION: "0.3.0"`), que `models` et `nemo` reprennent par une ancre YAML : les deux images ne peuvent pas partir de versions différentes. C'est cette ligne que l'updater réécrira quand STK-43 sera fait.

### 4.3 Modèles

Le service `models` lance `nemo-speech pull` pour chaque modèle. `pull` télécharge la révision que NeMo-Speech.cpp épingle et vérifie le fichier. `nemo` charge ensuite les fichiers Q8_0 par leur chemin.

| Nom pour `pull` | Dépôt Hugging Face | Fichier chargé | Taille |
|---|---|---|---|
| `nemotron-3.5` | `nvidia/nemotron-3.5-asr-streaming-0.6b` | `nemotron-3.5-asr-streaming-0.6b.q8_0.gguf` | 742 Mo |
| `nemotron-3-diarization` | `nvidia/Nemotron-3-Diarization` | `Nemotron-3-Diarization.q8_0.gguf` | 107 Mo |
| `magpie` | `nvidia/magpie_tts_multilingual_357m` | `magpie_tts_multilingual_357m.v2607.q8_0.gguf` et le tokenizer | 332 Mo |
| `nano-codec` | `nvidia/nemo-nano-codec-22khz-1.89kbps-21.5fps` | `nemo_nano_codec_22khz_1.89kbps_21.5fps.decoder.f16.gguf` | 79 Mo |

Lors du banc, `pull magpie` a apporté la version f16 de Magpie (569 Mo) avec son tokenizer, et le fichier Q8_0 du même dépôt a été téléchargé à part. Le service `models` télécharge donc ce fichier Q8_0 à la révision épinglée et vérifie son empreinte. Le plan vérifie si on peut éviter le téléchargement de la version f16, que `nemo` ne charge pas. Sans elle, le volume pèse environ 1,3 Go.

Un redéploiement ne dépend pas de Hugging Face quand les modèles sont déjà dans le volume. Après un lancement réussi, `pull-models.sh` écrit `pins.txt` à la racine du volume : une ligne par lien de `/models/current`, avec le dépôt, la révision, le nom de fichier et la taille du fichier lié (`-` pour le dossier du tokenizer). Au lancement suivant, si ce témoin porte les épinglages que le script attend et si les cinq liens pointent vers des fichiers présents à ces dépôt, révision et nom, à la taille relevée, le service sort en 0 sans aucun appel réseau. Dans tous les autres cas (volume neuf, épinglage changé, lien absent ou pendant, fichier tronqué), il interroge Hugging Face, après avoir nommé le fichier dont la taille a changé. Chaque appel `curl` du script passe `--retry 3 --retry-connrefused`, avec 10 s pour se connecter et 60 s par essai. Une panne de Hugging Face plus longue fait alors échouer `models`, donc ni `nemo` ni `dictee` ne démarrent. Les téléchargements de `nemo-speech pull` gardent leurs propres réglages.

### 4.4 Réglages de NeMo

`nemo` lance `nemo-speech serve` sur le port 8000 avec `--host 0.0.0.0` et `--no-ui`, les modèles par leur chemin et les réglages suivants. Les valeurs variables viennent du `.env`.

| Réglage | Valeur | Raison |
|---|---|---|
| `asr.streaming.rnnt_right_context` | 13 | 1,12 s de contexte à droite, comme Handy. Ce réglage ne vaut que pour la route temps réel. |
| `asr.diar.preset` | `v3-offline` | Preset de la séparation des locuteurs, le plus rapide sur CPU pour un fichier entier. |
| `asr.backend.threads` | `NEMO_ASR_THREADS` | Le VPS partage ses 8 vCPU avec toute la stack. |
| `tts.threads` | `NEMO_TTS_THREADS` | Même raison. |
| `tts.no-cfg` | `NEMO_TTS_NO_CFG`, faux par défaut | Coupe le CFG si la synthèse est trop lente. |
| `--api-key` | `NEMO_API_KEY` | Seul dictee appelle `nemo`. |

Le plan vérifie le nom exact de chaque option dans `nemo-speech serve --help` de la version 0.3.0.

### 4.5 Variables du `.env`

Le `.env` reste dans Dokploy, trié par ordre alphabétique.

| Variable | Service | Rôle |
|---|---|---|
| `CLEANUP_MODEL` | dictee | Case LiteLLM de la reformulation, `simple-low` |
| `DEFAULT_VOICE` | dictee | Voix Magpie par défaut |
| `GLOSSARY_BOOST` | dictee | Force du renforcement des termes du glossaire, `1.0` par défaut : la plus haute force mesurée qui n'insère aucun mot que personne n'a dit |
| `LITELLM_API_KEY` | dictee | Clé LiteLLM de dictee |
| `LITELLM_URL` | dictee | Adresse interne de LiteLLM |
| `LONG_AUDIO_SECONDS` | dictee | Seuil d'une transcription longue, 600 par défaut |
| `MAX_QUEUED_LONG` | dictee | Tâches longues en attente, en plus de celle qui tourne, 3 par défaut ; au-delà, l'envoi reçoit 429 `long_audio_busy` |
| `MAX_UPLOAD_MB` | dictee | Taille maximale d'un fichier envoyé, 300 par défaut |
| `NEMO_API_KEY` | nemo, dictee | Clé entre dictee et nemo |
| `NEMO_ASR_THREADS` | nemo | Threads de transcription |
| `NEMO_TTS_NO_CFG` | nemo | Coupe le CFG de Magpie |
| `NEMO_TTS_THREADS` | nemo | Threads de synthèse |
| `OIDC_CLIENT_ID` | dictee | Client PocketID de la page |
| `OIDC_CLIENT_SECRET` | dictee | Secret de ce client |
| `OIDC_ISSUER` | dictee | `https://pocketid.supergeoff.top` |
| `OPENWEBUI_SERVICE_KEY` | dictee | Clé de service d'Open WebUI |
| `PUBLIC_URL` | dictee | `https://dictee.supergeoff.top`, pour le retour de connexion |
| `SESSION_SECRET` | dictee | Signe le cookie de session |
| `TASK_RETENTION_HOURS` | dictee | Durée de conservation du résultat d'une tâche, comptée depuis sa fin, 24 par défaut |

### 4.6 Domaine

`dictee.supergeoff.top` est créé par l'API Dokploy sur le service `dictee`, port 8000. Il n'a pas le middleware basicauth de la convention, car le basicauth occupe l'en-tête `Authorization`, celui où les apps standard placent leur clé. dictee contrôle l'accès lui-même : par clé pour l'API, par PocketID pour la page.

### 4.7 Mémoire, disque et concurrence

Sur le banc (60 s d'audio, 2 threads), la transcription en streaming a pris 0,96 Go. Avec les personnes marquées par la même session, une réunion de 6 min 26 a pris 0,93 Go (essai du 1er novembre). La limite de 3 Go de `nemo` laisse de la marge pour une dictée qui arrive pendant une transcription longue.

NeMo traite les requêtes avec un pool de workers borné et une file d'attente, et répond 429 quand la file est pleine. dictee passe ses tâches par une file à lui. Une tâche courte, dont l'audio dure `LONG_AUDIO_SECONDS` ou moins, part tout de suite. Les tâches longues passent une à la fois, dans l'ordre d'arrivée, et au plus `MAX_QUEUED_LONG` d'entre elles attendent (3 par défaut) : un envoi long de plus reçoit l'erreur 429 `long_audio_busy`.

Une tâche longue réserve sa place pendant le décodage, dès que l'audio dépasse `LONG_AUDIO_SECONDS` : le refus tombe donc avant que dictee ait décodé le reste du fichier. La place se rend à la fin de la tâche, quelle que soit la sortie. Un flux `stream=true` sur un audio long prend sa place de la même façon, sans être enregistré.

Le WAV décodé vit sur le disque, dans le dossier `tasks` à côté de la base (`/data/tasks/<id>/audio.wav`, sur le volume `dictee-data`). Seule la tranche en cours passe en mémoire de dictee : un morceau de 40 s au plus (1,3 Mo) pour un fichier entier, une trame d'une seconde pour une session temps réel. La limite de 512 Mo de dictee ne dépend donc plus de la durée de l'audio. Un WAV pèse 32 000 octets par seconde : une tâche de 2 heures, la durée maximale d'un audio, occupe 230 Mo, soit près de 1 Go pour la tâche longue qui tourne et les trois qui attendent. dictee efface l'audio d'une tâche à sa fin, qu'elle ait réussi, échoué ou été annulée.

## 5. API

### 5.1 Routes

| Route | Rôle |
|---|---|
| `POST /v1/audio/transcriptions` | Toutes les transcriptions de fichier, synchrones ou en arrière-plan |
| `GET /v1/audio/transcriptions/{id}` | État et résultat d'une tâche (section 5.6) |
| `POST /v1/audio/transcriptions/{id}/cancel` | Annulation d'une tâche (section 5.6) |
| `GET /v1/realtime?intent=transcription` (WebSocket) | Dictée en direct (section 5.7) |
| `POST /v1/audio/speech` | Synthèse vocale |
| `GET /v1/models` | Liste des modèles, pour les apps qui découvrent les modèles avant d'appeler |
| `GET /health` | Santé, pour le healthcheck du compose |
| `GET /` | Page de configuration |
| `GET /login`, `GET /auth/callback`, `POST /logout` | Connexion PocketID |

Les routes des tâches et de la dictée en direct demandent la même clé que les autres routes `/v1` (section 5.4).

### 5.2 Transcription

La route suit `POST /v1/audio/transcriptions` d'OpenAI, en multipart. Le modèle demandé choisit le traitement.

| Modèle demandé | Traitement |
|---|---|
| `dictee` | Transcription brute |
| `dictee-clean` | Transcription reformulée par le LLM |
| `dictee-diarize` | Transcription avec locuteurs |
| `gpt-4o-transcribe-diarize` | Transcription avec locuteurs |
| Tout autre nom (`whisper-1`, `gpt-4o-transcribe`…) | Transcription brute |

Le format `diarized_json` impose le traitement avec locuteurs, quel que soit le modèle.

Le traitement et les drapeaux `stream` et `background` choisissent le moteur.

| Demande | Moteur | Tâche enregistrée |
|---|---|---|
| `dictee` ou `dictee-clean`, sans `stream` | Route REST de NeMo, fichier entier par morceaux (section 6.1) | Oui |
| Traitement avec locuteurs, sans `stream` | Route temps réel de NeMo, avec les personnes qu'elle marque (section 6.3) | Oui |
| Tout traitement avec `stream=true` | Route temps réel de NeMo (section 6.4) | Non |
| `stream=true` et `background=true` | Aucun : refus 400, code `unsupported_combination` | Non |

Sans `background`, dictee décode l'envoi, crée la tâche ou rejoint une tâche identique (section 5.6), attend sa fin et répond dans le format demandé. Si le client raccroche, la tâche continue. Une tâche en échec donne l'erreur qu'aurait donnée une requête sans tâche (429, 502 ou 500), et une tâche annulée pendant l'attente donne 409 `task_cancelled`. Avec `background=true`, dictee répond tout de suite 200 avec l'objet tâche. Dans les deux cas, un audio illisible, de plus de 2 heures ou sans place dans la file des tâches longues est refusé avant toute tâche.

| Champ | Usage |
|---|---|
| `file` | L'audio, dans tout format que ffmpeg lit, de 2 heures au plus pour tous les modes |
| `model` | Le traitement, selon le tableau ci-dessus |
| `language` | Code ISO-639-1, `fr` par défaut, converti pour NeMo (`fr` devient `fr-FR`) |
| `prompt` | Accepté et ignoré : NeMo le traite comme une phrase renforcée à une force qui insère des mots que personne n'a dits, donc dictee ne le lui transmet jamais. Le glossaire de l'utilisateur joue ce rôle, avec une force prudente. |
| `response_format` | `json` (défaut), `text`, `srt`, `vtt`, `verbose_json`, `diarized_json` |
| `stream` | `true` envoie des événements SSE, sans tâche enregistrée |
| `background` | `true` lance la tâche en arrière-plan et rend tout de suite son objet ; `false` par défaut. Refusé avec `stream`. |
| `timestamp_granularities[]` | `word` et `segment`, pour `verbose_json` |
| `chunking_strategy`, `temperature`, `known_speaker_names[]`, `known_speaker_references[]` | Acceptés et ignorés |

Formes des réponses :

- `json` : `{"text": ...}`.
- `verbose_json` : `text`, `language`, `duration`, puis `words` ou `segments` selon `timestamp_granularities[]`.
- `diarized_json` : `text`, `duration` et `segments`. Chaque segment porte `id`, `start`, `end`, `speaker`, `text` et `type: "transcript.text.segment"`. Les locuteurs s'appellent A, B, C dans l'ordre de leur première prise de parole.
- `srt` et `vtt` : sous-titres horodatés. Avec locuteurs, chaque sous-titre VTT porte la balise de voix `<v A>`, et chaque sous-titre SRT commence par « A : ».
- `text` : le texte seul. Avec locuteurs, chaque tour de parole forme un paragraphe préfixé par son locuteur.

Événements de streaming : `transcript.text.delta` au fil de la transcription, `transcript.text.segment` pour chaque segment avec locuteur, puis `transcript.text.done` avec le texte complet.

### 5.3 Synthèse vocale

La route suit `POST /v1/audio/speech` d'OpenAI.

| Champ | Usage |
|---|---|
| `input` | Le texte à dire |
| `voice` | Une voix Magpie (John, Sofia, Aria, Jason, Leo), sans distinction de casse. Tout autre nom, dont les voix OpenAI (alloy, nova…), prend la voix de l'utilisateur. La voix de l'utilisateur et `DEFAULT_VOICE` se comparent de même à la liste de NeMo, sans distinction de casse, et partent écrites comme NeMo les écrit. Si NeMo ne propose pas la voix de l'utilisateur, elle part telle quelle (NeMo répond alors) et le journal garde un avertissement avec son nom. |
| `model` | Accepté et sans effet sur la synthèse, car dictee n'a qu'un moteur ; il figure dans la ligne de journal |
| `response_format` | `mp3` (défaut, comme chez OpenAI), `opus`, `aac`, `flac`, `wav`, `pcm` |
| `speed` | De 0,25 à 4, appliquée par ffmpeg |
| `instructions`, `stream_format` | Acceptés et ignorés : la réponse est toujours un flux audio |

La langue de synthèse est le français. dictee envoie le son phrase par phrase (section 6.5).

### 5.4 Accès

Chaque appel porte une clé dans `Authorization: Bearer`.

| Clé | Utilisateur retenu |
|---|---|
| Clé personnelle | Son propriétaire |
| Clé de service d'Open WebUI | Le compte dont l'email PocketID égale `X-OpenWebUI-User-Email`. Les valeurs par défaut s'appliquent si l'en-tête manque ou si l'email est inconnu. |
| Clé absente, inconnue ou révoquée | Refus 401 |

dictee ne lit `X-OpenWebUI-User-Email` qu'avec la clé de service d'Open WebUI. Avec une clé personnelle, il l'ignore.

Les clés personnelles commencent par `dk_`. Elles sont stockées hachées en SHA-256 et ne s'affichent qu'à leur création. La page montre leur préfixe pour les reconnaître.

### 5.5 Erreurs

Les erreurs suivent l'enveloppe d'OpenAI, `{"error": {"message", "type", "param", "code"}}`. dictee répond :

- 400 pour une requête invalide, dont un audio de plus de 2 heures (`audio_too_long`) et `stream` avec `background` (`unsupported_combination`) ;
- 401 pour une clé refusée ;
- 404 pour un identifiant de tâche inconnu, effacé ou d'un autre compte ;
- 409 (`task_cancelled`) à une requête synchrone dont la tâche a été annulée avant sa fin ;
- 413 au-delà de `MAX_UPLOAD_MB` ;
- 422 pour un audio illisible ;
- 429 quand NeMo signale sa file pleine (`overloaded`) ou quand la file des tâches longues est pleine (`long_audio_busy`) ;
- 500 pour une panne imprévue, ou pour une tâche abandonnée après trois démarrages sans avoir fini (`interrupted`) ;
- 502 quand NeMo ne répond pas (`upstream_error`).

### 5.6 Tâches

Une transcription de fichier sans `stream` crée une tâche, ou en rejoint une identique. L'identifiant d'une tâche est `trn_` suivi de 26 caractères, qui se trient dans l'ordre de création. `GET /v1/audio/transcriptions/{id}` rend l'objet de la tâche, et `POST /v1/audio/transcriptions/{id}/cancel` l'annule.

```json
{
  "id": "trn_01JD…",
  "object": "audio.transcription.task",
  "status": "in_progress",
  "model": "dictee-diarize",
  "created_at": 1793500000,
  "completed_at": null,
  "expires_at": null,
  "duration": 7180.4,
  "progress": 0.42,
  "error": null,
  "output": null
}
```

- `status` prend les valeurs du mode `background` d'OpenAI : `queued`, `in_progress`, `completed`, `failed`, `cancelled`.
- `created_at`, `completed_at` et `expires_at` sont des dates en secondes Unix. Les deux dernières valent `null` tant que la tâche n'est pas finie, qu'elle ait réussi, échoué ou été annulée.
- `duration` est la durée de l'audio, en secondes.
- `progress` va de 0 à 1 : c'est la part des morceaux traités pour un fichier entier, et la part de l'audio envoyée à NeMo pour une réunion.
- `error` vaut `{"code", "message"}` pour une tâche en échec, et `null` sinon. Le code est `overloaded`, `upstream_error`, `interrupted`, ou `server_error` pour une panne imprévue (section 5.5).
- `output` n'existe qu'une fois la tâche réussie. C'est la réponse rendue dans le `response_format` et les granularités de la requête qui a créé la tâche, même quand une requête identique la rejoint ensuite : un objet pour `json`, `verbose_json` et `diarized_json`, une chaîne pour `text`, `srt` et `vtt`.

L'annulation arrête la tâche, efface son audio et rend l'objet au statut `cancelled`. Elle est idempotente et sans effet sur une tâche déjà finie, que l'objet rend telle qu'elle est.

Une tâche n'est visible que de son propriétaire, le compte que désigne la clé (le compte par défaut pour la clé de service d'Open WebUI sans email connu). Pour tout autre appelant, l'identifiant répond 404, comme un identifiant inconnu ou effacé.

**Envoi identique.** Deux envois sont identiques quand ils ont le même propriétaire, les mêmes octets de fichier (empreinte SHA-256 calculée pendant la réception), la même langue, le même traitement (brut, reformulé ou avec locuteurs), le même glossaire et la même force de glossaire, plus les mêmes consignes de reformulation pour `dictee-clean`. Le format de réponse et les granularités ne comptent pas : le résultat est gardé sous une forme neutre et rendu à la demande. Un envoi identique à une tâche `queued` ou `in_progress` la rejoint. Identique à une tâche `completed` non effacée, il reçoit son résultat sans retraitement. Une tâche `failed` ou `cancelled` ne se rejoint pas : l'envoi crée une nouvelle tâche. La règle vaut avec ou sans `background`, de sorte qu'une application peut renvoyer sa demande sans créer de doublon.

**Conservation.** Une tâche finie, réussie ou non, reste lisible pendant `TASK_RETENTION_HOURS` heures (24 par défaut) après sa fin. Un ménage, toutes les 10 minutes, efface ensuite la tâche avec ses morceaux, ainsi que les dossiers de tâche sans ligne en base qui datent de plus d'une heure.

### 5.7 Dictée en direct

`GET /v1/realtime?intent=transcription` ouvre une connexion WebSocket qui suit le Realtime d'OpenAI en mode transcription : le client envoie l'audio au fil de la dictée, et dictee renvoie le texte. La connexion porte la clé de l'appelant dans `Authorization: Bearer`, avec les règles de la section 5.4. Une clé refusée, une URL sans `intent=transcription` ou un paramètre `model` qui n'est ni un modèle de dictee ni un modèle dont le nom contient `transcribe` ouvrent la connexion, envoient un événement `error` puis la ferment avec le code 1008. Une dictée en direct ne rend que le texte brut, sans reformulation ni personnes.

| Événement du client | Ce que fait dictee |
|---|---|
| Ouverture | Identifie l'appelant, charge son glossaire et envoie `session.created`. |
| `session.update`, `session.type` à `transcription` | Lit `audio.input.format` (`audio/pcm` seulement, PCM16 mono, `rate` de 8000 à 96000, 24000 par défaut), la langue (`transcription.languages[0]`, sinon `transcription.language`, `fr` par défaut), `transcription.keywords` (ajoutés au glossaire de l'appelant pour cette session) et `turn_detection`. `prompt`, `delay`, `model` et `noise_reduction` sont acceptés et ignorés. Répond `session.updated` avec la session effective. Une fois l'audio commencé, les réglages ne changent plus (erreur `session_already_started`). |
| `turn_detection` absent ou `null` | Le client valide chaque prise de parole par `input_audio_buffer.commit`. |
| `turn_detection` de type `server_vad` ou `semantic_vad` | Refusé par une `error` de code `unsupported_turn_detection` : NeMo 0.3.0 ne ferme pas seul une prise de parole, et dictee n'a pas de détection sémantique. |
| `input_audio_buffer.append` (PCM16 en base64) | Décode l'audio et le transmet à NeMo en trames binaires. |
| `input_audio_buffer.commit` | Envoie `input_audio_buffer.committed` avec l'`item_id` de la prise de parole et le `previous_item_id`, puis transmet le commit à NeMo. Sans audio à valider, une `error` de code `input_audio_buffer_commit_empty` répond, et la session continue. |
| `input_audio_buffer.clear` | Transmet à NeMo, qui vide l'audio de la prise de parole en cours, puis envoie `input_audio_buffer.cleared`. |

Pendant la session, chaque texte partiel de NeMo devient un `conversation.item.input_audio_transcription.delta` (`item_id`, `content_index` à 0, `delta`). Les textes finals d'une prise de parole, joints par une espace, forment un seul `conversation.item.input_audio_transcription.completed` (`item_id`, `content_index`, `transcript`, et `usage` avec la durée de l'audio de la prise de parole en secondes), que dictee envoie après `input_audio_buffer.committed`, quand NeMo a rendu tous les textes de la prise de parole.

L'`item_id` d'une prise de parole (`item_` suivi d'un compteur) naît au premier `append` qui suit le commit précédent : les textes partiels portent donc déjà l'identifiant que `committed` annoncera. Une seule session NeMo sert toute la connexion, car NeMo accepte plusieurs commits dans une même session. dictee ouvre cette session au premier `append`, avec la langue, le glossaire de l'appelant puis les `keywords` qui n'y figurent pas (force `GLOSSARY_BOOST`), et la ferme quand le client part.

Une session s'arrête après 2 heures d'audio reçu (erreur `audio_too_long`) ou 30 minutes sans événement du client (erreur `idle_timeout`), avec le code de fermeture 1000. Une panne de NeMo envoie une `error` et ferme avec le code 1011. Les erreurs partent en événements `error` au format OpenAI (`type`, `code`, `message`, `param`, `event_id`), avec des messages fixes. La dictée en direct n'est pas enregistrée et ne passe pas par la file des tâches longues.

## 6. Traitement interne

### 6.1 Tronc commun de la transcription

1. dictee identifie l'appelant et charge ses réglages.
2. Il reçoit l'envoi dans un fichier temporaire, en calculant l'empreinte SHA-256 du fichier pendant la réception, puis cherche une tâche identique (section 5.6). S'il en trouve une, il la rejoint sans rien décoder.
3. Sinon, ffmpeg convertit l'audio en WAV PCM16 mono 16 kHz, écrit sur le disque dans le dossier d'une tâche neuve (section 4.7). Un audio illisible, de plus de 2 heures ou sans place dans la file des tâches longues est refusé à ce moment, sans créer de tâche. Le fichier temporaire de l'envoi disparaît dès la fin du décodage.
4. dictee enregistre la tâche avec ses réglages figés (langue, traitement, glossaire, force du glossaire et, pour `dictee-clean`, consignes de reformulation), qu'une reprise réutilise tels quels. Il la confie à la file, puis la requête attend sa fin, ou répond tout de suite avec `background=true` (section 5.2).
5. Le traitement suit le mode de la tâche : un fichier entier par morceaux (`dictee` et `dictee-clean`, ci-dessous), ou une réunion en une session temps réel avec les personnes (section 6.3).
6. À la fin de la tâche, réussie ou non, dictee efface son audio. Le résultat reste en base pendant `TASK_RETENTION_HOURS` heures (section 5.6).

**Morceaux.** Un fichier entier passe par la route REST de NeMo, `POST /v1/audio/transcriptions`, un morceau à la fois : les routes REST de NeMo refusent plus de 3600 s d'audio, et une requête unique sur un long fichier consomme trop de mémoire. Un audio de 40 s ou moins forme un seul morceau. Pour un audio plus long, une passe de ffmpeg avec le filtre `silencedetect` (seuil de -35 dB, silence d'au moins 0,3 s) repère les silences. Chaque morceau vise 30 s : sa fin est le milieu du silence le plus proche de 30 s après son début, parmi ceux qui tombent entre 20 et 40 s, et à défaut une coupe nette à 30 s. Les points de coupe sont enregistrés avec la tâche, pour qu'une reprise retrouve les mêmes morceaux. dictee envoie chaque morceau avec `language`, `response_format=verbose_json`, `timestamp_granularities[]=word` et `speech_contexts` (le glossaire de la tâche, avec sa force `GLOSSARY_BOOST`). Les mots de chaque morceau sont décalés du début du morceau, et le texte et les mots sont écrits en base dès la réponse de NeMo. Le texte final joint les textes non vides des morceaux par une espace, et `progress` vaut la part des morceaux faits.

**Reprise.** Au démarrage, dictee remet en file les tâches `queued` et `in_progress`, de la plus ancienne à la plus récente. Une tâche en fichier entier reprend au premier morceau non terminé, sans renvoyer les autres à NeMo. Une réunion repart du début, car NeMo ne reprend pas une session temps réel en cours. La base compte les démarrages d'une tâche : une tâche `in_progress` trouvée au démarrage après trois démarrages passe en `failed`, avec le code `interrupted`.

**Pannes de NeMo.** Quand NeMo est injoignable ou que sa file est pleine, la tâche attend que `/ready` réponde (5 minutes au plus), puis réessaie le morceau ou la session après une pause de 2, 5 puis 10 s : un essai, puis trois reprises au plus. Au-delà, ou pour une erreur que NeMo signale sur la requête elle-même, la tâche passe en `failed` avec le code `overloaded` ou `upstream_error`.

### 6.2 `dictee-clean`

dictee envoie le texte brut à LiteLLM (`CLEANUP_MODEL`) avec `temperature` à 0. Le message système réunit les consignes de l'utilisateur et son glossaire, déformations connues comprises. Le texte part entouré de `<transcript>`. dictee n'envoie plus `reasoning_effort`, car la case de la grille fixe l'effort.

Les garde-fous de l'ancien proxy restent :

- seuls les textes de 4 à 400 mots partent au LLM ;
- la réponse doit garder entre 0,6 et 1,4 fois la longueur du texte brut ;
- le LLM a 12 s pour répondre ;
- en cas d'échec, dictee rend le texte brut.

La reformulation ne vaut que pour `json` et `text`. Pour les autres formats, `dictee-clean` rend la transcription brute, car des sous-titres reformulés ne correspondraient plus aux horodatages.

Pour une tâche, la reformulation s'applique au texte assemblé des morceaux, avec le glossaire et les consignes figés à la création de la tâche. Le texte reformulé est enregistré avec le texte brut.

### 6.3 `dictee-diarize`

Une réunion passe par une seule session sur la route temps réel de NeMo, `/v1/audio/transcriptions/realtime`, ouverte avec les horodatages des mots (`word_timestamps`), `speaker_diarization`, la langue et les termes du glossaire en renforcement de mots (`speech_contexts`, force `GLOSSARY_BOOST`). dictee lit l'audio depuis le WAV de la tâche et l'envoie par trames d'une seconde, aussi vite que NeMo l'accepte, sans attendre la durée réelle de l'enregistrement. La part d'audio envoyée donne `progress`, écrit en base au plus toutes les 5 s.

NeMo rend tous les mots dans son événement final, chacun avec son numéro de personne. dictee renomme les personnes A, B, C dans l'ordre de leur première apparition parmi les mots, et rattache un mot sans personne à la personne du mot qui le précède. Les mots consécutifs d'une même personne forment un segment, et chaque changement de personne ouvre un nouveau segment.

### 6.4 Streaming

Avec `stream=true`, aucune tâche n'est créée : dictee décode l'audio dans un fichier temporaire, qu'il efface à la fin du flux, et NeMo le transcrit par la route temps réel. dictee envoie un `transcript.text.delta` à chaque morceau de texte que NeMo produit en cours de route, puis le reste du texte final. Quand le texte final ne prolonge pas les morceaux déjà envoyés, dictee n'envoie rien de plus pour ce passage et le texte de `transcript.text.done` fait foi. En mode locuteurs, la session temps réel marque elle-même les personnes (section 6.3), et les segments partent à la fin, avant `transcript.text.done`. Avec `dictee-clean`, le texte reformulé part en un seul `transcript.text.delta`, puisque le LLM travaille sur le texte complet. Un audio long prend sa place dans la file des tâches longues (section 4.7). Une coupure du client arrête le traitement, qui n'est ni repris ni enregistré.

### 6.5 Synthèse vocale

1. dictee charge la voix de l'utilisateur.
2. Il découpe le texte en phrases sur la ponctuation finale et les sauts de ligne. Une phrase de plus de 300 caractères est recoupée sur ses virgules.
3. Il envoie chaque phrase à la route `/v1/audio/speech` de NeMo, l'une après l'autre, et reçoit un WAV.
4. Un seul processus ffmpeg par réponse reçoit l'audio des phrases à la suite, applique la vitesse et encode dans le format demandé. dictee transmet sa sortie au client au fil de l'eau : le son de la première phrase part sans attendre les suivantes.

### 6.6 Journaux

dictee écrit une ligne par requête. Elle donne l'identifiant de l'utilisateur (`default` pour le compte par défaut), la route, le modèle, le traitement (`mode`), la durée de l'audio, les temps de NeMo et du LLM, la décision de reformulation, le nombre de passages d'une réponse en continu dont le texte final ne prolonge pas les morceaux déjà envoyés (un nombre, sans le texte), le code de réponse et, en dernier, l'identifiant de la tâche (`task`) que la requête a créée ou rejointe. Pour une requête qui passe par une tâche, `nemo_ms` et `llm_ms` restent à `-` : les temps de la tâche sont sur sa propre ligne.

Chaque tâche écrit une ligne à sa fin, quelle que soit cette fin (réussie, en échec ou annulée), en plus de la ligne de la requête qui l'a créée. Cette ligne donne `task` (l'identifiant), `user`, `mode`, `audio_s` (la durée de l'audio), `chunks` (le nombre de morceaux, `-` pour une réunion), `starts` (le nombre de démarrages), `retries` (le nombre de reprises auprès de NeMo), `nemo_ms` (le temps passé dans les appels de transcription à NeMo, pauses et attentes de `/ready` exclues), `llm_ms` (`-` sans reformulation), `status` et `error` (le code d'échec, `-` sinon).

Une session de dictée en direct écrit une ligne à sa fermeture : `user`, `route=realtime`, `audio_s` (l'audio reçu, en secondes), `turns` (le nombre de prises de parole validées) et `status`, qui dit comment la connexion s'est terminée : `closed` quand le client part, `idle`, `limit`, ou le code de l'erreur qui l'a fermée.

Les journaux ne contiennent jamais le texte, l'audio, le glossaire ni les consignes, et d'une exception imprévue seul le nom de la classe est journalisé. dictee garde l'audio d'une tâche jusqu'à sa fin et son texte 24 heures après (section 7.3).

La ligne de la synthèse donne `user`, `route=speech`, `model` (celui de la requête, rendu sûr pour une ligne de journal), `chars`, `sentences`, `voice`, `format`, `first_ms`, `nemo_ms` et `status`. `first_ms` est le délai entre l'arrivée de la requête et le premier PCM de NeMo : il compte la lecture des voix (à la première requête du processus), le profil et la synthèse de la première phrase. `nemo_ms` est le temps cumulé des appels à NeMo pour cette requête, lecture des voix et synthèse de chaque phrase, y compris un appel qui échoue ou qu'un client qui raccroche fait abandonner. Un champ inconnu s'écrit `-` : `first_ms` quand NeMo n'a rendu aucun PCM, `nemo_ms` quand aucun appel n'a eu lieu.

## 7. Espace utilisateur et page de configuration

### 7.1 Connexion

La page se connecte à PocketID en OpenID Connect (code d'autorisation avec PKCE, portées `openid email profile`). Tout compte PocketID peut se connecter. Le compte dictee se crée à la première connexion et se rattache au `sub` de PocketID. L'email est mis à jour à chaque connexion, car c'est lui qui relie un appel d'Open WebUI à son compte. La session tient dans un cookie signé par `SESSION_SECRET`, avec les attributs `Secure`, `HttpOnly` et `SameSite=Lax`. Elle garde le numéro du compte et son `sub`, et chaque requête compare ce `sub` à celui du compte : une session dont le compte a disparu, ou dont le numéro a été repris par un autre `sub` après une remise à zéro de la base, est vidée comme une session absente.

Le client OIDC se crée dans PocketID avec l'adresse de retour `https://dictee.supergeoff.top/auth/callback`.

### 7.2 Page

La page est unique, rendue par le serveur sans framework JavaScript, et compte quatre blocs. Chaque formulaire porte un jeton anti-CSRF.

| Bloc | Fonctionnement | Valeur par défaut |
|---|---|---|
| Clés | Créer une clé nommée (« Android », « Dictate »), affichée une seule fois. Révoquer une clé. Voir la date de dernière utilisation. | Aucune clé |
| Glossaire | Un terme par ligne, suivi si besoin de ses déformations connues : `Dokploy (souvent entendu : docploy, dopo)`. La forme anglaise de l'ancien glossaire, `(often heard as: …)`, est aussi lue. | Vide |
| Consignes de reformulation | Zone de texte pré-remplie avec les consignes par défaut. Un bouton les rétablit. | Consignes neutres du dépôt |
| Voix | Choix parmi les cinq voix Magpie, avec un bouton qui fait entendre une phrase d'exemple. | `DEFAULT_VOICE` |

La page n'a pas de section d'administration. La clé de service d'Open WebUI, le modèle LiteLLM et les réglages de NeMo restent dans le `.env`.

### 7.3 Base de données

La base SQLite vit dans le volume `dictee-data` et compte cinq tables.

| Table | Colonnes |
|---|---|
| `users` | `id`, `sub` (unique), `email` (unique), `name`, `created_at`, `last_login_at` |
| `api_keys` | `id`, `user_id`, `label`, `prefix`, `hash` (unique), `created_at`, `last_used_at`, `revoked_at` |
| `settings` | `user_id`, `glossary`, `instructions` (vide pour les consignes par défaut), `voice` (vide pour `DEFAULT_VOICE`) |
| `tasks` | `id` (texte `trn_…`), `owner` (numéro du compte, `NULL` pour le compte par défaut), `model`, `mode`, `language` (au format de NeMo, `fr-FR`), `response_format`, `granularities` (JSON), `upload_sha256`, `params_sha256` (langue, mode, glossaire, force et, pour `dictee-clean`, consignes), `params` (glossaire avec ses déformations, force et consignes, figés à la création, JSON), `status`, `error_code`, `error_message`, `duration`, `progress`, `cuts` (points de coupe, JSON, `NULL` tant qu'ils ne sont pas posés), `result` (forme neutre du résultat, JSON), `attempts` (démarrages), `created_at`, `started_at`, `completed_at`, `expires_at` |
| `task_chunks` | `task_id`, `idx`, `start`, `end`, `text`, `words` (JSON) ; clé (`task_id`, `idx`), effacés avec leur tâche |

Les dates des tâches sont des secondes Unix entières. Un index sur (`owner`, `upload_sha256`, `params_sha256`) sert la recherche d'un envoi identique (section 5.6). La forme neutre du résultat porte le texte, les mots, les segments d'une réunion, la durée et la langue, plus le texte reformulé et la décision de reformulation pour `dictee-clean` : le format de réponse se choisit à la lecture.

Le schéma porte sa version dans `pragma user_version` (2 aujourd'hui) : une base neuve, une base créée avant le versionnement et une base à la version 1 passent à 2 à l'ouverture, qui leur ajoute les tables des tâches sans toucher aux trois premières, et une version plus récente reste telle quelle. Une migration future partira de ce numéro.

Une valeur vide dans `settings` renvoie au défaut au lieu de le recopier. Quand les consignes par défaut changent dans le dépôt, tous ceux qui ne les ont pas modifiées en profitent.

**Conservation.** Le texte et les mots d'une tâche restent en base jusqu'à `expires_at`, 24 heures après sa fin (`TASK_RETENTION_HOURS`), puis le ménage les efface. Son audio, sur le volume, disparaît dès sa fin. Le texte progressif et la dictée en direct n'écrivent rien en base.

### 7.4 Valeurs par défaut

Les consignes par défaut reprennent celles de l'ancien proxy, déjà neutres, et vivent dans `dictee/defaults/instructions.txt`. La voix par défaut se choisit à l'écoute pendant le plan. Le glossaire par défaut est vide.

## 8. Tests

**Tests automatiques.** `pytest` couvre ce que dictee décide lui-même, avec un faux NeMo (HTTP et WebSocket) et un faux LiteLLM, sans réseau :

- l'identification par clé personnelle, l'en-tête d'Open WebUI accepté seulement avec la clé de service, et le refus d'une clé révoquée ;
- le choix du traitement selon le modèle et le format, et le refus de `stream` avec `background` ;
- les points de coupe (silence le plus proche de 30 s, coupe nette sans silence, morceaux qui couvrent tout l'audio sans trou ni chevauchement) et l'assemblage des morceaux ;
- les tâches : réponse synchrone identique pour chaque format, tâche qui continue quand le client raccroche, objet tâche en arrière-plan, annulation idempotente, 404 pour un autre propriétaire, envoi identique qui rejoint une tâche ;
- la reprise au redémarrage (un fichier entier reprend au premier morceau non fait, une réunion repart du début) et les pannes de NeMo (attente de `/ready`, trois reprises, puis `failed`) ;
- la file (une tâche courte passe pendant une longue, 429 au-delà de la file) et le ménage (expiration, effacement de l'audio, dossiers orphelins) ;
- le renommage des personnes que NeMo marque, en A, B, C, et le découpage en segments ;
- chaque format de réponse, sous-titres avec locuteurs compris ;
- les garde-fous de la reformulation ;
- le découpage du texte en phrases et le choix de la voix ;
- la dictée en direct : la traduction de chaque événement dans les deux sens, les textes partiels qui portent l'`item_id` de la prise de parole, le refus sans `intent=transcription` et celui de `server_vad` ;
- `prompt`, jamais transmis à NeMo ;
- la compatibilité, vérifiée avec le client Python officiel d'OpenAI sur les routes de transcription (synchrone et en continu) et de synthèse, et sur une session Realtime en transcription.

Une GitHub Action lance ces tests sur chaque pull request.

**Test d'intégration.** `make integration` lance le compose en local avec les vrais modèles et de courts extraits audio sans contenu personnel. Il tourne sur la machine de Geoff et pas dans la CI, parce que les modèles pèsent 1,3 Go.

## 9. Bascule

1. **Mesure de référence.** On chronomètre speaches sur une dictée de 10 s, en passant par l'ancien proxy.
2. **Sauvegarde.** Le compose actuel figure en annexe A. Les montages `app.py` et `glossary.txt` restent dans Dokploy jusqu'au nettoyage.
3. **PocketID.** On crée le client OIDC de dictee.
4. **Modèles.** On déploie le nouveau compose avec une commande Dokploy réduite au service `models`, pendant que speaches tourne. Le téléchargement prend du disque et pas de mémoire.
5. **Arrêt de speaches.** speaches est arrêté dans Dokploy, sans être supprimé.
6. **Déploiement complet.** La commande du compose revient à sa valeur par défaut, et `nemo` puis `dictee` démarrent.
7. **Domaine et clés.** On crée `dictee.supergeoff.top` et on met la clé de service d'Open WebUI dans le `.env`. Geoff crée ses clés Android et Dictate sur la page, puis colle son glossaire actuel dans son espace.
8. **Open WebUI.** La transcription garde l'adresse `http://ai-dictee-tltwgv-dictee-1:8000/v1`, avec la clé de service et le modèle `dictee-clean`. La synthèse vocale passe de speaches à cette même adresse, avec la clé de service.
9. **Dictate.** L'app passe à `https://dictee.supergeoff.top/v1` avec la clé Dictate de Geoff et le modèle `dictee`, puisqu'elle reformule elle-même.
10. **Critères.** On vérifie les critères de la section 10.

Chaque étape qui écrit dans Dokploy, PocketID, Open WebUI ou Dictate attend l'accord explicite de Geoff au moment de l'exécuter.

**Grille LiteLLM.** Si la grille linéaire n'est pas en production au moment de la bascule, `CLEANUP_MODEL` vaut `flash-low` jusqu'à sa mise en service. Le plan de la grille prévoit une PR dans `openwebuitools` pour passer l'ancien proxy sur `simple-low`. Cette PR devient inutile : changer `CLEANUP_MODEL` dans le `.env` de l'ancien compose suffit jusqu'au retrait.

## 10. Critères de réussite

| Critère | Mesure |
|---|---|
| Texte | Sur l'appel et la note du dossier Meets du Drive, plus une dictée, le texte de Nemotron est au moins aussi bon que celui de speaches. Geoff juge sur une lecture côte à côte. |
| Locuteurs | Sur un enregistrement du V05 à plusieurs voix, les locuteurs sont bien séparés. |
| Mémoire | `nemo` reste sous ses 3 Go pendant une transcription longue avec une dictée en parallèle. |
| Rapidité de la transcription | La dictée de 10 s revient au moins aussi vite qu'avec speaches à l'étape 1 de la bascule. |
| Rapidité de la synthèse | En mode appel, la première phrase sonne en moins de 2 s. Sinon, on coupe le CFG et on remesure. |

## 11. Retour arrière et nettoyage

**Retour arrière.** Si un critère échoue sans correction possible, on remet le compose de l'annexe A, qui retrouve ses montages `app.py` et `glossary.txt`. On redémarre speaches, puis on rétablit les réglages audio d'Open WebUI (synthèse vocale vers speaches, voix `tom`) et l'adresse de Dictate.

**Nettoyage.** Quand tous les critères passent, et après l'accord de Geoff :

- le compose speaches et le domaine `speaches.supergeoff.top` sont supprimés ;
- les montages `app.py` et `glossary.txt` sont retirés du compose dictee dans Dokploy ;
- une PR retire de `openwebuitools` les fichiers `services/dictee/app.py`, `services/dictee/README.md` et `tests/test_dictee.py`, et déplace la spec `docs/superpowers/specs/2026-10-24-dictee-design.md` dans ce dépôt, pour garder l'historique du premier proxy à côté de sa suite.

## 12. Vérifications à faire en premier dans le plan

Ces points reposent sur la documentation de NeMo-Speech.cpp ou sur un banc mené en ligne de commande. Le plan les vérifie avant d'écrire le code qui en dépend, et s'arrête pour en parler à Geoff si l'un d'eux échoue. La liste garde sa formulation du 31 octobre : la passe de locuteurs du point 3 est remplacée depuis par la session temps réel avec les personnes (section 6.3), dont les vérifications sont dans la spec du 1er novembre.

1. La route temps réel applique `rnnt_right_context` à 13, accepte `speech_contexts` et rend les horodatages des mots. Le banc a mesuré ce réglage en ligne de commande seulement.
2. Une dictée passe pendant une transcription longue, sans attendre la fin de celle-ci.
3. La passe de locuteurs sur un fichier d'une heure tient dans la limite de 3 Go.
4. Magpie : mémoire, vitesse sur le VPS avec et sans CFG, choix de la voix par défaut.
5. Le téléchargement de Magpie en Q8_0 sans la version f16.
6. Les noms exacts des options de `nemo-speech serve` en 0.3.0.
7. Traefik laisse passer l'envoi d'un fichier d'une heure et une réponse longue.
8. La commande Dokploy réduite au service `models` pour l'étape 4 de la bascule.

## 13. Hors périmètre

- Le mode temps réel d'Open WebUI, désactivé, et la conversation vocale de NeMo-Speech.cpp, réservée à CUDA.
- Le mode conversation du Realtime d'OpenAI et WebRTC : `/v1/realtime` ne sert que la transcription, par WebSocket.
- La reprise d'une réunion à mi-chemin après un redémarrage de dictee, et celle d'un texte progressif (`stream=true`) ou d'une dictée en direct après une coupure.
- La reconnaissance de locuteurs connus (`known_speaker_names[]`).
- L'extension de l'updater aux releases GitHub (carte Kaneo STK-43).
- L'app Android, dans son propre dépôt `supergeoff/dictee`.
- La sauvegarde du volume `dictee-data` par Dokploy, que Geoff pourra ajouter à son périmètre de sauvegarde.

## Annexe A : compose dictee avant la bascule

Compose `ai-dictee-tltwgv` tel que Dokploy le servait le 31 octobre 2026, gardé pour le retour arrière. Le `.env` reste dans Dokploy.

```yaml
services:
  dictee:
    image: ghcr.io/astral-sh/uv:0.13.5-python3.13-trixie-slim
    pull_policy: always
    restart: on-failure:3
    command:
      - uv
      - run
      - --no-project
      - /app/app.py
    env_file: .env
    expose:
      - "8000"
    volumes:
      - ../files/app.py:/app/app.py:ro
      - ../files/glossary.txt:/app/glossary.txt:ro
      - uv-cache:/root/.cache/uv
    mem_limit: 256m
    healthcheck:
      test:
        - CMD
        - python
        - -c
        - import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5)
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 90s
    networks:
      - dokploy-network
volumes:
  uv-cache: null
networks:
  dokploy-network:
    external: true
```
