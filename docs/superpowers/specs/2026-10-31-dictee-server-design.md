# dictee-server : design

- **Date** : 31 octobre 2026
- **Statut** : design validé par Geoff, partie par partie, le 31 octobre 2026
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
| Transcription | Nemotron 3.5 ASR streaming 0.6b, réglé comme Handy avec 1,12 s de contexte à droite (`rnnt_right_context` à 13). |
| Locuteurs | Nemotron 3 Diarization, preset `v3-offline`, huit locuteurs au plus. |
| Synthèse vocale | Magpie TTS multilingual 357m avec le décodeur NanoCodec. Si elle est trop lente, on coupe le CFG plutôt que de revenir à Piper. |
| Quantization | Q8_0 partout, comme dans Vibe et Handy. |
| Utilisateurs | Un espace par compte PocketID, sans configuration globale gérée par un admin. |
| API | Compatible OpenAI et synchrone, sans tâche asynchrone maison. |
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
| `dictee` | API compatible OpenAI, clés, réglages, page, conversion audio, appel à LiteLLM | Contexte git `#main:dictee` | 512 Mo | Réseau du compose et `dokploy-network` |

`nemo` démarre quand `models` s'est terminé avec succès (`service_completed_successfully`). `dictee` démarre quand `nemo` répond sur `/ready` (`service_healthy`). Seul `dictee` joint `nemo`, et la clé de `nemo` (`--api-key`) refuse tout autre appelant du réseau.

Volumes : `dictee-data` porte la base SQLite de dictee, `nemo-models` porte les modèles.

### 4.2 Image `nemo`

`nemo/Dockerfile` part d'une image Debian slim à tag explicite. Il reçoit `NEMO_VERSION` en argument de build, télécharge l'archive `nemo-speech-${NEMO_VERSION}-linux-x86_64-cpu.tar.gz` de la release GitHub de NVIDIA et compare son empreinte SHA-256 avec celle que GitHub publie pour cette pièce jointe. Une empreinte différente arrête le build. L'image contient `curl`, dont `nemo-speech pull` a besoin, et tourne sous un utilisateur non root.

Le compose fixe la version dans `build.args` (`NEMO_VERSION: "0.3.0"`). C'est cette ligne que l'updater réécrira quand STK-43 sera fait.

### 4.3 Modèles

Le service `models` lance `nemo-speech pull` pour chaque modèle. `pull` télécharge la révision que NeMo-Speech.cpp épingle et vérifie le fichier. `nemo` charge ensuite les fichiers Q8_0 par leur chemin.

| Nom pour `pull` | Dépôt Hugging Face | Fichier chargé | Taille |
|---|---|---|---|
| `nemotron-3.5` | `nvidia/nemotron-3.5-asr-streaming-0.6b` | `nemotron-3.5-asr-streaming-0.6b.q8_0.gguf` | 742 Mo |
| `nemotron-3-diarization` | `nvidia/Nemotron-3-Diarization` | `Nemotron-3-Diarization.q8_0.gguf` | 107 Mo |
| `magpie` | `nvidia/magpie_tts_multilingual_357m` | `magpie_tts_multilingual_357m.v2607.q8_0.gguf` et le tokenizer | 332 Mo |
| `nano-codec` | `nvidia/nemo-nano-codec-22khz-1.89kbps-21.5fps` | `nemo_nano_codec_22khz_1.89kbps_21.5fps.decoder.f16.gguf` | 79 Mo |

Lors du banc, `pull magpie` a apporté la version f16 de Magpie (569 Mo) avec son tokenizer, et le fichier Q8_0 du même dépôt a été téléchargé à part. Le service `models` télécharge donc ce fichier Q8_0 à la révision épinglée et vérifie son empreinte. Le plan vérifie si on peut éviter le téléchargement de la version f16, que `nemo` ne charge pas. Sans elle, le volume pèse environ 1,3 Go.

### 4.4 Réglages de NeMo

`nemo` lance `nemo-speech serve` sur le port 8000 avec `--host 0.0.0.0` et `--no-ui`, les modèles par leur chemin et les réglages suivants. Les valeurs variables viennent du `.env`.

| Réglage | Valeur | Raison |
|---|---|---|
| `asr.streaming.rnnt_right_context` | 13 | 1,12 s de contexte à droite, comme Handy. Ce réglage ne vaut que pour la route temps réel. |
| `asr.diar.preset` | `v3-offline` | La passe de locuteurs porte sur le fichier entier, et ce preset est le plus rapide sur CPU. |
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
| `GLOSSARY_BOOST` | dictee | Force du renforcement des termes du glossaire, fixée par le plan |
| `LITELLM_API_KEY` | dictee | Clé LiteLLM de dictee |
| `LITELLM_URL` | dictee | Adresse interne de LiteLLM |
| `LONG_AUDIO_SECONDS` | dictee | Seuil d'une transcription longue, 600 par défaut |
| `MAX_UPLOAD_MB` | dictee | Taille maximale d'un fichier envoyé |
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

### 4.6 Domaine

`dictee.supergeoff.top` est créé par l'API Dokploy sur le service `dictee`, port 8000. Il n'a pas le middleware basicauth de la convention, car le basicauth occupe l'en-tête `Authorization`, celui où les apps standard placent leur clé. dictee contrôle l'accès lui-même : par clé pour l'API, par PocketID pour la page.

### 4.7 Mémoire et concurrence

Sur le banc (60 s d'audio, 2 threads), la transcription en streaming a pris 0,96 Go et la passe de locuteurs 0,23 Go. Le plan mesure Magpie. La limite de 3 Go de `nemo` laisse de la marge pour une dictée qui arrive pendant une transcription longue.

NeMo traite les requêtes avec un pool de workers borné et une file d'attente, et répond 429 quand la file est pleine. dictee ne lance qu'une transcription longue à la fois : au-delà de `LONG_AUDIO_SECONDS` d'audio, une requête attend son tour, et les requêtes courtes passent pendant ce temps.

## 5. API

### 5.1 Routes

| Route | Rôle |
|---|---|
| `POST /v1/audio/transcriptions` | Toutes les transcriptions |
| `POST /v1/audio/speech` | Synthèse vocale |
| `GET /v1/models` | Liste des modèles, pour les apps qui découvrent les modèles avant d'appeler |
| `GET /health` | Santé, pour le healthcheck du compose |
| `GET /` | Page de configuration |
| `GET /login`, `GET /auth/callback`, `POST /logout` | Connexion PocketID |

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

| Champ | Usage |
|---|---|
| `file` | L'audio, dans tout format que ffmpeg lit |
| `model` | Le traitement, selon le tableau ci-dessus |
| `language` | Code ISO-639-1, `fr` par défaut, converti pour NeMo (`fr` devient `fr-FR`) |
| `prompt` | Transmis à NeMo, qui le traite comme une phrase renforcée |
| `response_format` | `json` (défaut), `text`, `srt`, `vtt`, `verbose_json`, `diarized_json` |
| `stream` | `true` envoie des événements SSE |
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
| `voice` | Une voix Magpie (John, Sofia, Aria, Jason, Leo), sans distinction de casse. Tout autre nom, dont les voix OpenAI (alloy, nova…), prend la voix de l'utilisateur. |
| `model` | Accepté et ignoré, car dictee n'a qu'un moteur de synthèse |
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

Les erreurs suivent l'enveloppe d'OpenAI, `{"error": {"message", "type", "param", "code"}}`. dictee répond 400 pour une requête invalide, 401 pour une clé refusée, 413 au-delà de `MAX_UPLOAD_MB`, 422 pour un audio illisible, 429 quand NeMo signale sa file pleine et 502 quand NeMo ne répond pas.

## 6. Traitement interne

### 6.1 Tronc commun de la transcription

1. dictee identifie l'appelant et charge ses réglages.
2. ffmpeg convertit l'audio en WAV PCM16 mono 16 kHz.
3. dictee ouvre une session sur la route temps réel de NeMo, `/v1/audio/transcriptions/realtime`, avec les horodatages des mots (`word_timestamps`). Il envoie l'audio aussi vite que NeMo l'accepte, sans attendre la durée réelle de l'enregistrement.
4. Dans la même session, dictee transmet les termes du glossaire de l'utilisateur en renforcement de mots (`speech_contexts`), avec la force `GLOSSARY_BOOST`.

### 6.2 `dictee-clean`

dictee envoie le texte brut à LiteLLM (`CLEANUP_MODEL`) avec `temperature` à 0. Le message système réunit les consignes de l'utilisateur et son glossaire, déformations connues comprises. Le texte part entouré de `<transcript>`. dictee n'envoie plus `reasoning_effort`, car la case de la grille fixe l'effort.

Les garde-fous de l'ancien proxy restent :

- seuls les textes de 4 à 400 mots partent au LLM ;
- la réponse doit garder entre 0,6 et 1,4 fois la longueur du texte brut ;
- le LLM a 12 s pour répondre ;
- en cas d'échec, dictee rend le texte brut.

La reformulation ne vaut que pour `json` et `text`. Pour les autres formats, `dictee-clean` rend la transcription brute, car des sous-titres reformulés ne correspondraient plus aux horodatages.

### 6.3 `dictee-diarize`

Après le tronc commun, dictee envoie le WAV entier à la route `/v1/audio/diarizations` de NeMo. Les deux passes se suivent au lieu de tourner en même temps, pour ne pas cumuler leur mémoire. dictee attribue ensuite chaque mot au locuteur actif au milieu du mot, ou au locuteur le plus proche dans le temps quand aucun n'est actif. Les mots consécutifs d'un même locuteur forment un segment, et chaque changement de locuteur ouvre un nouveau segment.

### 6.4 Streaming

Avec `stream=true`, dictee envoie un `transcript.text.delta` à chaque morceau de texte que NeMo produit en cours de route, puis le reste du texte final. Quand le texte final ne prolonge pas les morceaux déjà envoyés, dictee n'envoie rien de plus pour ce passage et le texte de `transcript.text.done` fait foi. En mode locuteurs, les segments arrivent à la fin, après la passe de locuteurs qui a besoin du fichier entier. Avec `dictee-clean`, le texte reformulé part en un seul `transcript.text.delta`, puisque le LLM travaille sur le texte complet.

### 6.5 Synthèse vocale

1. dictee charge la voix de l'utilisateur.
2. Il découpe le texte en phrases sur la ponctuation finale et les sauts de ligne. Une phrase de plus de 300 caractères est recoupée sur ses virgules.
3. Il envoie chaque phrase à la route `/v1/audio/speech` de NeMo, l'une après l'autre, et reçoit un WAV.
4. Un seul processus ffmpeg par réponse reçoit l'audio des phrases à la suite, applique la vitesse et encode dans le format demandé. dictee transmet sa sortie au client au fil de l'eau : le son de la première phrase part sans attendre les suivantes.

### 6.6 Journaux

dictee écrit une ligne par requête. Elle donne l'identifiant de l'utilisateur, la route, le modèle, la durée de l'audio, les temps de NeMo et du LLM, la décision de reformulation, le nombre de passages d'une réponse en continu dont le texte final ne prolonge pas les morceaux déjà envoyés (un nombre, sans le texte) et le code de réponse. Les journaux ne contiennent jamais le texte ni l'audio, et dictee ne stocke ni l'un ni l'autre.

## 7. Espace utilisateur et page de configuration

### 7.1 Connexion

La page se connecte à PocketID en OpenID Connect (code d'autorisation avec PKCE, portées `openid email profile`). Tout compte PocketID peut se connecter. Le compte dictee se crée à la première connexion et se rattache au `sub` de PocketID. L'email est mis à jour à chaque connexion, car c'est lui qui relie un appel d'Open WebUI à son compte. La session tient dans un cookie signé par `SESSION_SECRET`, avec les attributs `Secure`, `HttpOnly` et `SameSite=Lax`.

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

La base SQLite vit dans le volume `dictee-data` et compte trois tables.

| Table | Colonnes |
|---|---|
| `users` | `id`, `sub` (unique), `email` (unique), `name`, `created_at`, `last_login_at` |
| `api_keys` | `id`, `user_id`, `label`, `prefix`, `hash` (unique), `created_at`, `last_used_at`, `revoked_at` |
| `settings` | `user_id`, `glossary`, `instructions` (vide pour les consignes par défaut), `voice` (vide pour `DEFAULT_VOICE`) |

Une valeur vide dans `settings` renvoie au défaut au lieu de le recopier. Quand les consignes par défaut changent dans le dépôt, tous ceux qui ne les ont pas modifiées en profitent.

### 7.4 Valeurs par défaut

Les consignes par défaut reprennent celles de l'ancien proxy, déjà neutres, et vivent dans `dictee/defaults/instructions.txt`. La voix par défaut se choisit à l'écoute pendant le plan. Le glossaire par défaut est vide.

## 8. Tests

**Tests automatiques.** `pytest` couvre ce que dictee décide lui-même, avec un faux NeMo (HTTP et WebSocket) et un faux LiteLLM, sans réseau :

- l'identification par clé personnelle, l'en-tête d'Open WebUI accepté seulement avec la clé de service, et le refus d'une clé révoquée ;
- le choix du traitement selon le modèle et le format ;
- l'attribution des mots aux locuteurs et le découpage en segments ;
- chaque format de réponse, sous-titres avec locuteurs compris ;
- les garde-fous de la reformulation ;
- le découpage du texte en phrases et le choix de la voix ;
- la compatibilité, vérifiée avec le client Python officiel d'OpenAI sur les deux routes, streaming compris.

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

Ces points reposent sur la documentation de NeMo-Speech.cpp ou sur un banc mené en ligne de commande. Le plan les vérifie avant d'écrire le code qui en dépend, et s'arrête pour en parler à Geoff si l'un d'eux échoue.

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
