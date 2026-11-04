# dictee-server

dictee est le point d'entrée audio de la stack supergeoff. Il sert la transcription (Nemotron 3.5 ASR), la séparation des locuteurs (Nemotron 3 Diarization) et la synthèse vocale (Magpie TTS) de NVIDIA, exécutées sur CPU par [NeMo-Speech.cpp](https://github.com/NVIDIA/NeMo-Speech.cpp), derrière une API compatible avec l'API audio d'OpenAI.

Chaque utilisateur se connecte avec PocketID sur une petite page de configuration, où il gère ses clés, son glossaire, ses consignes de reformulation et sa voix.

La spec est dans [`docs/superpowers/specs/2026-10-31-dictee-server-design.md`](docs/superpowers/specs/2026-10-31-dictee-server-design.md), mise à jour pour décrire les tâches, le découpage en morceaux et la dictée en direct. L'histoire de ce changement est dans [`docs/superpowers/specs/2026-11-01-taches-transcription-design.md`](docs/superpowers/specs/2026-11-01-taches-transcription-design.md), et les règles du dépôt pour les agents sont dans [`AGENTS.md`](AGENTS.md).

## Mise en route

Il faut Docker et `make`. Le déploiement en production passe par Dokploy, qui construit le compose depuis `main` et garde le `.env` : la spec le décrit (section 4).

Le `.env` porte les variables suivantes, dont la spec donne le rôle (section 4.5) : `CLEANUP_MODEL`, `DEFAULT_VOICE`, `GLOSSARY_BOOST`, `LITELLM_API_KEY`, `LITELLM_URL`, `LONG_AUDIO_SECONDS`, `MAX_QUEUED_LONG`, `MAX_UPLOAD_MB`, `NEMO_API_KEY`, `NEMO_ASR_THREADS`, `NEMO_TTS_NO_CFG`, `NEMO_TTS_THREADS`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`, `OIDC_ISSUER`, `OPENWEBUI_SERVICE_KEY`, `PUBLIC_URL`, `SESSION_SECRET` et `TASK_RETENTION_HOURS`. En local, `make integration` en fabrique un de banc avec `integration/make-env.sh` s'il manque, et git l'ignore.

| Commande | Effet |
|---|---|
| `make test` | Lance `pytest` dans une image Docker, avec un faux NeMo et un faux LiteLLM, sans réseau. |
| `make integration` | Construit et lance la stack complète avec les vrais modèles (le premier lancement les télécharge), attend `/health`, puis joue `integration/run.py`. Elle demande `uv`, laisse la stack démarrée et ne tourne pas dans la CI. |
| `make down` | Arrête la stack ; les volumes restent. |

## Utiliser l'API

Chaque appel porte une clé dans l'en-tête `Authorization: Bearer <clé>`. Une clé personnelle, qui commence par `dk_`, se crée sur la page de configuration et ne s'affiche qu'une fois. Les exemples ci-dessous écrivent `<adresse>` pour l'adresse de dictee.

### Transcrire un fichier

`POST /v1/audio/transcriptions` suit la route d'OpenAI. Le client envoie le fichier audio dans un formulaire multipart (un formulaire qui transporte des fichiers) et reçoit le texte. Le champ `model` choisit le traitement.

| `model` | Résultat |
|---|---|
| `dictee` | Le texte brut. Tout autre nom de modèle, comme `whisper-1`, donne aussi le texte brut. |
| `dictee-clean` | Le texte reformulé par un LLM (un modèle de langage). Si la reformulation échoue, dictee rend le texte brut. |
| `dictee-diarize` | Le texte avec ses locuteurs, nommés A, B, C dans l'ordre de leur première prise de parole. Le format `diarized_json` donne les mêmes locuteurs, quel que soit le modèle. |

Le champ `response_format` vaut `json` par défaut, et accepte `text`, `srt`, `vtt`, `verbose_json` et `diarized_json`. Le champ `language` vaut `fr` par défaut. Le champ `prompt` est accepté et ignoré : dictee ne le transmet jamais à NeMo, car NeMo en ferait une phrase renforcée qui ajoute des mots que personne n'a dits. Le glossaire de l'utilisateur tient ce rôle, avec une force prudente.

```bash
curl https://<adresse>/v1/audio/transcriptions \
  -H "Authorization: Bearer dk_…" \
  -F file=@enregistrement.m4a \
  -F model=dictee
```

dictee découpe le fichier en morceaux d'environ 30 s, coupés dans les silences, que NeMo transcrit l'un après l'autre. Une réunion avec locuteurs passe en une seule session, car NeMo marque les personnes pendant qu'il transcrit.

Chaque transcription de fichier est enregistrée comme une tâche dans la base de dictee. La requête attend la fin de la tâche. Si le client raccroche avant, la tâche continue, et le même fichier envoyé de nouveau retrouve la tâche en cours ou son résultat, sans tout recommencer.

Avec `stream=true`, dictee envoie le texte au fur et à mesure, en événements SSE (le serveur envoie ses événements un par un sur la même réponse HTTP). Ce mode n'est pas enregistré : une coupure l'arrête sans reprise, et il ne se combine pas avec `background`.

### Lancer une transcription en arrière-plan

Avec `background=true`, dictee répond tout de suite avec l'objet de la tâche, et le client revient chercher le résultat quand il veut. Un fichier illisible, de plus de 2 heures ou refusé par la file d'attente reçoit sa réponse d'erreur tout de suite, sans tâche.

```bash
# Lancer la tâche : la réponse porte son identifiant, `trn_…`.
curl https://<adresse>/v1/audio/transcriptions \
  -H "Authorization: Bearer dk_…" \
  -F file=@reunion.m4a -F model=dictee-diarize -F response_format=diarized_json \
  -F background=true

# Lire son état et, une fois terminée, son résultat.
curl https://<adresse>/v1/audio/transcriptions/trn_… -H "Authorization: Bearer dk_…"

# L'annuler.
curl -X POST https://<adresse>/v1/audio/transcriptions/trn_…/cancel -H "Authorization: Bearer dk_…"
```

L'objet de la tâche donne :

- `status`, qui vaut `queued` (en file), `in_progress` (en cours), `completed` (réussie), `failed` (en échec) ou `cancelled` (annulée) ;
- `progress`, de 0 à 1 : la part des morceaux traités, ou la part de l'audio envoyée à NeMo pour une réunion ;
- `output`, une fois la tâche réussie : la réponse dans le `response_format` demandé à la création, un objet JSON pour `json`, `verbose_json` et `diarized_json`, un texte pour `text`, `srt` et `vtt` ;
- `error`, pour une tâche en échec : un `code` et un `message` ;
- `created_at`, `completed_at` et `expires_at`, des dates en secondes Unix, ainsi que `duration`, la durée de l'audio en secondes.

L'annulation est sans effet sur une tâche déjà finie, et la répéter ne change rien. Une tâche n'est visible que de son propriétaire : pour tout autre appelant, comme pour un identifiant inconnu ou effacé, la réponse est 404.

Une tâche survit à un redémarrage de dictee. Une tâche en morceaux reprend au premier morceau non terminé, et une réunion repart du début. Quand NeMo ne répond pas pendant une tâche, dictee attend son retour et réessaie trois fois au plus, puis la tâche passe en `failed`. Un fichier renvoyé identique (mêmes octets, même langue, même traitement, même glossaire) rejoint la tâche en file ou en cours, et reçoit le résultat d'une tâche réussie sans retraitement ; une tâche en échec ou annulée ne se rejoint pas, et l'envoi en crée une nouvelle.

dictee efface l'audio d'une tâche dès sa fin. Son texte reste lisible 24 heures après la fin, puis dictee efface la tâche.

### Dicter en direct

`GET /v1/realtime?intent=transcription` ouvre une connexion WebSocket (une connexion qui reste ouverte pour que le client et le serveur s'envoient des messages dans les deux sens). Elle suit le Realtime d'OpenAI en mode transcription, à une différence près : le client valide lui-même chaque prise de parole.

Le client ouvre la connexion avec sa clé, puis :

- règle la session par `session.update` : audio PCM16 (de l'audio brut sur 16 bits, en mono) à une fréquence de 8000 à 96000 Hz, 24000 par défaut, langue (`fr` par défaut) et `keywords`, des mots à renforcer en plus du glossaire de son compte ;
- envoie l'audio encodé en base64 (une écriture du binaire en texte) par `input_audio_buffer.append` ;
- valide chaque prise de parole par `input_audio_buffer.commit`.

dictee renvoie le texte au fil de la parole dans des événements `conversation.item.input_audio_transcription.delta`, puis le texte final de chaque prise de parole dans un événement `conversation.item.input_audio_transcription.completed`. Avec le client Python d'OpenAI, une session ressemble à ceci :

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

La dictée en direct ne rend que le texte brut, sans reformulation ni locuteurs. Elle refuse `turn_detection` de type `server_vad`, parce que NeMo ne ferme pas seul une prise de parole : c'est le client qui valide chacune par un commit. Elle n'est pas enregistrée, et une coupure n'est pas reprise.

### Limites

| Limite | Valeur | Réglage |
|---|---|---|
| Durée d'un audio, tous modes | 2 heures | Constante du code |
| Taille d'un envoi | 300 Mo | `MAX_UPLOAD_MB` |
| Audio long | Plus de 600 s | `LONG_AUDIO_SECONDS` |
| Tâches longues | 1 en cours et 3 en attente, puis 429 `long_audio_busy` | `MAX_QUEUED_LONG` |
| Conservation d'un résultat | 24 heures après la fin de la tâche | `TASK_RETENTION_HOURS` |
| Session de dictée en direct | 2 heures d'audio reçu, ou 30 minutes sans événement du client | Constantes du code |

Une tâche est longue quand son audio dépasse `LONG_AUDIO_SECONDS`. Une tâche courte part tout de suite, sans attendre les longues, alors que les tâches longues passent une à la fois, dans l'ordre d'arrivée.
