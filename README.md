# dictee-server

dictee est le point d'entrée audio de la stack supergeoff. Il sert la transcription, la séparation des locuteurs et la synthèse vocale, exécutées sur CPU, derrière une API compatible avec l'API audio d'OpenAI. Chacun de ces rôles passe par un moteur que le `.env` choisit (voir « Les moteurs »). Le déploiement visé transcrit avec [CrispASR](https://github.com/CrispStrobe/CrispASR) (Fluid Speech Pro et Nemotron 3 Diarization pour les locuteurs) et synthétise la voix avec phonon-40m-fr. [NeMo-Speech.cpp](https://github.com/NVIDIA/NeMo-Speech.cpp), le moteur d'avant, reste choisissable jusqu'à la fin de la bascule.

Chaque utilisateur se connecte avec PocketID sur une petite page de configuration, où il gère ses clés, son glossaire, ses consignes de reformulation et sa voix.

La spec est dans [`docs/superpowers/specs/2026-10-31-dictee-server-design.md`](docs/superpowers/specs/2026-10-31-dictee-server-design.md), mise à jour pour décrire les tâches, le découpage en morceaux et la dictée en direct. L'histoire de ce changement est dans [`docs/superpowers/specs/2026-11-01-taches-transcription-design.md`](docs/superpowers/specs/2026-11-01-taches-transcription-design.md), et celle du passage aux moteurs par rôle dans [`docs/superpowers/specs/2026-11-28-moteurs-audio-design.md`](docs/superpowers/specs/2026-11-28-moteurs-audio-design.md). Les règles du dépôt pour les agents sont dans [`AGENTS.md`](AGENTS.md).

## Mise en route

Il faut Docker et `make`. Le compose de production vit dans Dokploy, seule source des composes : Dokploy construit les images depuis `main` et garde le `.env`, et la spec le décrit (section 4). Le dépôt ne garde qu'un compose de test, `compose.integration.yaml`, que `make integration` lance en local.

Le `.env` et ses variables sont décrits dans la section suivante.

| Commande | Effet |
|---|---|
| `make test` | Lance `pytest` dans une image Docker, avec de faux moteurs (NeMo, CrispASR, serveur de voix) et un faux LiteLLM, sans réseau. |
| `make integration` | Construit et lance la stack complète avec les vrais modèles (le premier lancement les télécharge), attend `/health`, puis joue `integration/run.py`. Elle demande `uv` et trois variables : `CRISPASR_VERSION`, la version de CrispASR du compose de Dokploy, `PTTS_MODEL` (`hf://gradium/phonon-40m-fr@<commit>`) et `HF_TOKEN`, que `integration/make-env.sh` lit pour fabriquer le `.env` (`CRISPASR_VERSION=<version> PTTS_MODEL=<modèle> HF_TOKEN=<jeton> make integration`). Elle laisse la stack démarrée et ne tourne pas dans la CI. |
| `make nemo-up` | Démarre seulement les services `models` et `nemo`, sans dictee. Elle demande la variable `NEMO_VERSION`, la version de NeMo-Speech.cpp du compose de Dokploy. |
| `make down` | Arrête la stack ; les volumes restent. |

## Les moteurs

Trois rôles audio passent chacun par un moteur, que trois variables du `.env` choisissent. Chaque rôle se change seul, et une valeur absente ou vide vaut `nemo`, le moteur d'avant.

| Variable | Rôle | Valeurs | Moteur visé par le déploiement |
|---|---|---|---|
| `STT_ENGINE` | Transcription de fichier et locuteurs | `nemo`, `crispasr` | `crispasr` : le serveur CrispASR avec Fluid Speech Pro et Nemotron 3 Diarization |
| `LIVE_ENGINE` | Dictée en direct | `nemo`, `crispasr-stream` | `crispasr-stream` : un processus CrispASR par session, lancé dans le conteneur de dictee |
| `TTS_ENGINE` | Synthèse vocale | `nemo`, `openai` | `openai` : un serveur compatible avec `POST /v1/audio/speech`, ici ptts avec phonon-40m-fr |

Le `.env` porte aussi des variables que dictee lit quel que soit le moteur, dont la spec du 31 octobre donne le rôle (section 4.5) : `CLEANUP_MODEL`, `DEFAULT_VOICE`, `LITELLM_API_KEY`, `LITELLM_URL`, `LONG_AUDIO_SECONDS`, `MAX_QUEUED_LONG`, `MAX_UPLOAD_MB`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`, `OIDC_ISSUER`, `OPENWEBUI_SERVICE_KEY`, `PUBLIC_URL`, `SESSION_SECRET` et `TASK_RETENTION_HOURS`. `DEFAULT_VOICE` vaut `Leo` quand elle est absente, et le déploiement visé la fixe à `Lucien`. `GLOSSARY_BOOST` fait aussi partie de ces variables : dictee la lit dans tous les cas, mais elle n'a d'effet qu'avec le moteur `nemo`, car les adaptateurs CrispASR ignorent le glossaire de la requête. Les autres variables ne servent qu'à un moteur, comme le montre ce tableau, et la spec du 28 novembre décrit leur rôle.

| Moteur | Variables de dictee | Variables des services |
|---|---|---|
| `nemo` | `NEMO_API_KEY` (obligatoire dès qu'un rôle choisit `nemo`), `NEMO_URL` | `NEMO_ASR_THREADS`, `NEMO_TTS_NO_CFG`, `NEMO_TTS_THREADS` |
| `crispasr` | `CRISPASR_API_KEY` (obligatoire quand `STT_ENGINE=crispasr`), `CRISPASR_URL`, `DIAR_METHOD` | `ASR_BACKEND`, `ASR_MODEL_URL`, `ASR_MODEL_SHA256`, `DIAR_MODEL_URL`, `DIAR_MODEL_SHA256`, `CRISPASR_API_KEYS`, `CRISPASR_THREADS`, `CRISPASR_WORKERS` |
| `crispasr-stream` | `LIVE_COMMAND`, `LIVE_MODEL_PATH`, `LIVE_VAD_MODEL_PATH`, `LIVE_MAX_SESSIONS`, `LIVE_THREADS`, `LIVE_STREAM_ARGS` | `VAD_MODEL_URL`, `VAD_MODEL_SHA256` |
| `openai` | `TTS_URL`, `TTS_SAMPLE_RATE`, `TTS_MAX_CONCURRENT` | `PTTS_MODEL`, `PTTS_LANG`, `PTTS_QUANT`, `HF_TOKEN` |

Le serveur CrispASR lit la liste `CRISPASR_API_KEYS`, alors que dictee lit la clé unique `CRISPASR_API_KEY` : le `.env` porte la même clé dans les deux. Le téléchargement de phonon-40m-fr demande `HF_TOKEN`, car son dépôt Hugging Face exige d'accepter ses conditions d'accès.

Dans les valeurs par défaut de dictee, les trois moteurs valent encore `nemo`, ce qui permet le retour arrière : remettre les trois variables sur `nemo` et redémarrer dictee suffit à revenir au moteur d'avant. Le compose de test, lui, tourne sur les nouveaux moteurs. En local, `make integration` fabrique un `.env` de banc avec `integration/make-env.sh` s'il manque, et git l'ignore.

## Utiliser l'API

Chaque appel porte une clé dans l'en-tête `Authorization: Bearer <clé>`. Une clé personnelle, qui commence par `dk_`, se crée sur la page de configuration et ne s'affiche qu'une fois. Les exemples ci-dessous écrivent `<adresse>` pour l'adresse de dictee.

### Transcrire un fichier

`POST /v1/audio/transcriptions` suit la route d'OpenAI. Le client envoie le fichier audio dans un formulaire multipart (un formulaire qui transporte des fichiers) et reçoit le texte. Le champ `model` choisit le traitement.

| `model` | Résultat |
|---|---|
| `dictee` | Le texte brut. Tout autre nom de modèle, comme `whisper-1`, donne aussi le texte brut, sauf `gpt-4o-transcribe-diarize`, le nom du modèle d'OpenAI qui sépare les locuteurs : dictee le traite comme `dictee-diarize`. |
| `dictee-clean` | Le texte reformulé par un LLM (un modèle de langage). Si la reformulation échoue, dictee rend le texte brut. |
| `dictee-diarize` | Le texte avec ses locuteurs, nommés A, B, C dans l'ordre de leur première prise de parole. Le format `diarized_json` donne les mêmes locuteurs, quel que soit le modèle. |

Le champ `response_format` vaut `json` par défaut, et accepte `text`, `srt`, `vtt`, `verbose_json` et `diarized_json`. Le champ `language` est facultatif. Une langue envoyée (`fr`, `en`) est respectée, et les réponses détaillées (`verbose_json`) l'annoncent. Sans langue, ou avec `auto`, ces réponses annoncent `und` : avec `crispasr`, le moteur ne détecte pas la langue et dictee ne lit pas celle qu'il annonce, qui n'est pas une détection. Avec `nemo`, le moteur détecte la langue, française ou anglaise ou les deux dans le même audio, et peut perdre les premiers mots d'une langue au moment où l'audio en change.

Le champ `prompt` complète le glossaire de l'utilisateur, un terme par ligne comme sur la page, dans les mêmes bornes (500 lignes et 20 000 caractères). Ses termes s'ajoutent sans doublon à ceux du compte et servent à la correction du glossaire comme à la reformulation. dictee ne transmet jamais le texte brut au moteur et écarte les phrases de contexte (plus de 5 mots ou un point final), car NeMo en ferait des phrases renforcées qui ajoutent des mots que personne n'a dits. En direct, `transcription.prompt` suit la même règle.

CrispASR n'a pas de glossaire qui marche avec Fluid Speech Pro : dictee porte donc le glossaire lui-même. Après la transcription, il remplace dans le texte rendu chaque variante entendue d'un terme (`Terme (souvent entendu : a, b)`) par ce terme, par mot entier et sans tenir compte de la casse. Cette correction s'applique aux fichiers, aux tâches enregistrées et aux textes finals de la dictée en direct, et dictee n'envoie jamais les termes au moteur CrispASR. Un terme sans variante entendue n'a pas d'effet à ce niveau, mais la reformulation le reçoit toujours. Avec `nemo`, `GLOSSARY_BOOST` règle en plus la force avec laquelle NeMo renforce les termes au décodage.

```bash
curl https://<adresse>/v1/audio/transcriptions \
  -H "Authorization: Bearer dk_…" \
  -F file=@enregistrement.m4a \
  -F model=dictee
```

dictee découpe le fichier en morceaux d'environ 30 s, coupés dans les silences, que le moteur transcrit l'un après l'autre. Une réunion avec locuteurs passe en une seule requête, car le moteur marque les personnes pendant qu'il transcrit.

Chaque transcription de fichier est enregistrée comme une tâche dans la base de dictee. La requête attend la fin de la tâche. Si le client raccroche avant, la tâche continue, et le même fichier envoyé de nouveau retrouve la tâche en cours ou son résultat, sans tout recommencer.

Avec `stream=true`, dictee envoie le texte au fur et à mesure, en événements SSE (le serveur envoie ses événements un par un sur la même réponse HTTP). Avec `crispasr`, le moteur ne rend aucun texte partiel : dictee coupe le fichier aux silences en morceaux de 15 s au plus et rend un événement `transcript.text.delta` par morceau fini. Les mesures donnent 3 deltas pour 31 s d'audio et 63 deltas pour 11 minutes, et leur concaténation égale le texte de l'événement `transcript.text.done`. Avec locuteurs, le texte arrive en une fois à la fin, suivi de l'événement final. Avec `nemo`, un delta arrive toutes les 1,12 s d'audio environ. Ce mode n'est pas enregistré : une coupure l'arrête sans reprise, et il ne se combine pas avec `background`.

### Lancer une transcription en arrière-plan

Avec `background=true`, dictee répond tout de suite avec l'objet de la tâche, et le client revient chercher le résultat quand il veut. Un fichier illisible, de plus de 2 heures ou refusé par la file d'attente reçoit sa réponse d'erreur tout de suite, sans tâche.

```bash
# Lancer la tâche : la réponse porte son identifiant, `trn_…`.
curl https://<adresse>/v1/audio/transcriptions \
  -H "Authorization: Bearer dk_…" \
  -F file=@reunion.mp3 -F model=dictee-diarize -F response_format=vtt \
  -F background=true

# Lire son état et, une fois terminée, son résultat VTT, même si la tâche a d'abord été créée en JSON.
curl 'https://<adresse>/v1/audio/transcriptions/trn_…?response_format=vtt' -H "Authorization: Bearer dk_…"

# L'annuler.
curl -X POST https://<adresse>/v1/audio/transcriptions/trn_…/cancel -H "Authorization: Bearer dk_…"
```

L'objet de la tâche donne :

- `status`, qui vaut `queued` (en file), `in_progress` (en cours), `completed` (réussie), `failed` (en échec) ou `cancelled` (annulée) ;
- `progress`, de 0 à 1 : la part des morceaux traités, ou la part de l'audio envoyée au moteur pour une réunion ;
- `output`, une fois la tâche réussie : la réponse dans le format demandé par l'envoi ou le GET, un objet JSON pour `json`, `verbose_json` et `diarized_json`, un texte pour `text`, `srt` et `vtt` ; le VTT avec locuteurs porte des balises comme `<v A>` ;
- `error`, pour une tâche en échec : un `code` et un `message` ;
- `created_at`, `completed_at` et `expires_at`, des dates en secondes Unix, ainsi que `duration`, la durée de l'audio en secondes.

Le POST en arrière-plan rend `output` dans le format demandé par cet envoi, même s'il rejoint une tâche déjà terminée. Le GET accepte `response_format` pour choisir le format de sa réponse ; sans ce paramètre, il garde le format demandé à la création de la tâche. Le changement de format ne modifie pas la tâche et ne relance pas la transcription.

Une lecture ne refait pas le traitement : une tâche brute refuse `diarized_json`, et une tâche reformulée n'accepte que `json` et `text`. Une tâche avec locuteurs accepte tous les formats. Une demande incompatible reçoit une erreur 400 `incompatible_response_format`. Pour `verbose_json`, le GET accepte `timestamp_granularities[]=word` et `timestamp_granularities[]=segment`, seuls ou ensemble. Sans granularité, un format explicitement demandé utilise `segment` ; sans aucune option, les granularités de création restent utilisées. Une granularité inconnue ou demandée pour un autre format est refusée.

L'annulation arrête la tâche partagée par tous les envois qui l'ont rejointe. Elle est sans effet sur une tâche déjà finie, et la répéter ne change rien. Une tâche n'est visible que de son propriétaire : pour tout autre appelant, comme pour un identifiant inconnu ou effacé, la réponse est 404.

Une tâche survit à un redémarrage de dictee. Une tâche en morceaux reprend au premier morceau non terminé, et une réunion repart du début. Quand NeMo ne répond pas pendant une tâche, dictee attend son retour et réessaie trois fois au plus, puis la tâche passe en `failed`. Un fichier renvoyé identique (même compte, mêmes octets, même langue, même mode, même glossaire et même force de glossaire, plus les mêmes consignes de reformulation pour `dictee-clean`) rejoint la tâche en file ou en cours, et reçoit le résultat d'une tâche réussie sans retraitement. C'est le mode qui compte et non le nom du modèle : deux noms de modèle qui donnent le même mode rejoignent la même tâche. Une tâche en échec ou annulée ne se rejoint pas, pas plus qu'une tâche `dictee-clean` dont la reformulation s'est repliée sur le texte brut parce que le LLM n'a pas répondu ou que sa réponse a été écartée ; l'envoi crée alors une nouvelle tâche.

dictee efface l'audio d'une tâche dès sa fin. Son texte reste lisible 24 heures après la fin, puis dictee efface la tâche.

### Dicter en direct

`GET /v1/realtime?intent=transcription` ouvre une connexion WebSocket (une connexion qui reste ouverte pour que le client et le serveur s'envoient des messages dans les deux sens). Elle suit le Realtime d'OpenAI en mode transcription, à une différence près : le client valide lui-même chaque prise de parole.

Le client ouvre la connexion avec sa clé, puis :

- règle la session par `session.update` : audio PCM16 (de l'audio brut sur 16 bits, en mono) à une fréquence de 8000 à 96000 Hz, 24000 par défaut, langue (détectée par défaut, annoncée par une langue nulle dans la session) et `keywords`, des mots à renforcer en plus du glossaire de son compte ;
- envoie l'audio encodé en base64 (une écriture du binaire en texte) par `input_audio_buffer.append` ;
- valide chaque prise de parole par `input_audio_buffer.commit`.

dictee renvoie le texte au fil de la parole dans des événements `conversation.item.input_audio_transcription.delta`, puis le texte final de chaque prise de parole dans un événement `conversation.item.input_audio_transcription.completed`.

Avec `crispasr-stream`, dictee lance un processus CrispASR par session, dans son propre conteneur, et en accepte `LIVE_MAX_SESSIONS` au plus (2 par défaut) : au-delà, la connexion reçoit l'erreur de saturation. Le texte partiel avance d'un pas par seconde, et un mot n'est émis qu'une fois confirmé par deux pas successifs. Le texte final arrive à la validation du client, comme avec NeMo, et dictee y applique le glossaire. Le processus vérifie le modèle de transcription et le modèle de détection de voix (`vad.bin`, téléchargé avec les autres modèles et vérifié par son empreinte) avant de démarrer, car sans le second le binaire retéléchargerait le sien à chaque session. Chaque session lance son processus, et le fichier du modèle est partagé entre eux. La limite de mémoire du conteneur de dictee doit couvrir `LIVE_MAX_SESSIONS` processus : sur une machine de développement x86_64, la mesure donne 913 Mio avec une session ouverte et 1,1 Gio au plus avec deux (environ 570 Mio pour le fichier du modèle, partagé, et environ 220 Mio propres à chaque session). Le compose de test donne 3 Go, et la valeur de production reste à mesurer au déploiement. Quand le moteur prend du retard sur l'audio reçu, la session garde l'audio en attente au lieu de bloquer la connexion, jusqu'à 120 s d'audio, puis la connexion reçoit l'erreur de saturation. Avec le client Python d'OpenAI, une session ressemble à ceci :

```python
from openai import OpenAI

client = OpenAI(api_key="dk_…", base_url="https://<adresse>/v1")
with client.realtime.connect(extra_query={"intent": "transcription"}) as connection:
    connection.send(
        {
            "type": "session.update",
            "session": {
                "type": "transcription",
                "audio": {"input": {"format": {"type": "audio/pcm", "rate": 24000}, "turn_detection": None}},
            },
        }
    )
    connection.input_audio_buffer.append(audio=audio_pcm_en_base64)
    connection.input_audio_buffer.commit()
    event = connection.recv()  # à répéter jusqu'à l'événement `…transcription.completed`
```

La dictée en direct ne rend que le texte brut, sans reformulation ni locuteurs. Elle refuse `turn_detection` de type `server_vad`, parce que dictee ne ferme pas seul une prise de parole : c'est le client qui valide chacune par un commit. Elle n'est pas enregistrée, et une coupure n'est pas reprise.

### Synthétiser une voix

`POST /v1/audio/speech` suit la route d'OpenAI et rend l'audio de la phrase envoyée. Les voix sont celles du serveur de voix : phonon-40m-fr en propose 16, dont `Lucien`, la voix par défaut du déploiement visé. Une voix demandée que le serveur ne propose pas est remplacée par celle du profil de l'utilisateur, puis par `DEFAULT_VOICE`, sans erreur. Le serveur de phonon produit du PCM à 24 kHz (`TTS_SAMPLE_RATE`), et dictee le convertit dans le format demandé. Avec `openai`, dictee ne confie au serveur de voix que `TTS_MAX_CONCURRENT` synthèses à la fois (une par défaut) et met les autres en attente.

### Limites

| Limite | Valeur | Réglage |
|---|---|---|
| Durée d'un audio, tous modes | 2 heures | Constante du code |
| Taille d'un envoi | 300 Mo | `MAX_UPLOAD_MB` |
| Audio long | Plus de 600 s | `LONG_AUDIO_SECONDS` |
| Tâches longues | 1 en cours et 3 en attente, puis 429 `long_audio_busy` | `MAX_QUEUED_LONG` |
| Conservation d'un résultat | 24 heures après la fin de la tâche | `TASK_RETENTION_HOURS` |
| Session de dictée en direct | 2 heures d'audio reçu, ou 30 minutes sans événement du client | Constantes du code |
| Silence de NeMo pendant une réunion | 300 secondes sans événement ni trame d'audio acceptée, puis `upstream_error` | Constantes du code |

Une tâche est longue quand son audio dépasse `LONG_AUDIO_SECONDS`. Une tâche courte part tout de suite, sans attendre les longues, alors que les tâches longues passent une à la fois, dans l'ordre d'arrivée.

## Licences

Les modèles et les moteurs du déploiement visé ont chacun leur licence, que dictee ne modifie pas.

| Composant | Licence | À retenir |
|---|---|---|
| Fluid Speech Pro (transcription) | CC BY 4.0 | Le crédit va à FluidVoice. |
| Nemotron 3 Diarization (locuteurs) | OpenMDW | |
| phonon-40m-fr (voix) | CC BY-NC 4.0 | L'usage commercial est interdit, et le téléchargement exige d'accepter les conditions du dépôt Hugging Face. |
| CrispASR | MIT | |
| ptts, le serveur de voix | MIT ou Apache-2.0 | |
| Silero VAD (détection de voix), distribué par `ggml-org/whisper-vad` | MIT | |
