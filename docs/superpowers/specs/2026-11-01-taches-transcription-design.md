# Tâches de transcription, découpage et dictée en direct : design

- **Date** : 1er novembre 2026
- **Statut** : validé par Geoff le 1er novembre 2026
- **Dépôt** : `supergeoff/dictee-server`, branche `feat/taches-transcription`
- **Point de départ** : `2026-10-31-dictee-server-design.md`, déployé le 1er novembre 2026

## 1. Objet

dictee transcrit aujourd'hui tout par la route temps réel de NeMo, même un fichier déjà enregistré, et refuse au-delà de 90 minutes (59 pour les réunions). Une coupure du client ou un redémarrage de dictee fait perdre tout le traitement. Geoff veut trois choses :

- par défaut, le traitement en fichier entier de NeMo, découpé en morceaux, pour tenir la limite d'une heure de ses routes REST et borner la mémoire ;
- le moteur temps réel sur demande, pour le texte progressif d'un fichier et pour une dictée en direct au format du Realtime d'OpenAI ;
- des réunions jusqu'à 2 heures, la durée de son enregistreur, sans perdre le traitement sur une coupure.

Une réunion de 2 heures prend environ 55 minutes sur le VPS. Les réunions arrivent par l'application Android de Geoff, qu'il peut modifier, et par des outils qui ne parlent que le format OpenAI.

La synthèse vocale ne change pas : Geoff garde Magpie, qu'il utilise peu, et attend que les versions de NeMo l'accélèrent. Silero n'est pas ajouté, car le banc du 1er novembre ne lui a trouvé aucun gain dans NeMo.

## 2. Objectifs, non-objectifs, anti-objectifs

**Objectifs**

- Une transcription sans `stream` survit à une coupure du client et au redémarrage de dictee.
- L'application de Geoff lance une transcription en arrière-plan, reçoit un identifiant et vient chercher le résultat.
- Un outil OpenAI qui renvoie le même fichier retrouve le traitement en cours ou son résultat, sans tout recommencer.
- Toutes les transcriptions acceptent 2 heures d'audio, réunions comprises.
- Une dictée en direct est possible par une connexion WebSocket au format du Realtime d'OpenAI en mode transcription.
- Le glossaire n'ajoute plus de mots qui n'ont pas été dits.

**Non-objectifs**

- Reprendre une réunion au milieu de son traitement : elle repart du début après un redémarrage de dictee.
- Reprendre un texte progressif (`stream=true`) ou une dictée en direct après une coupure.
- Combiner `background` et `stream`, et reprendre un flux d'événements à partir d'un curseur.
- La dictée en direct depuis un navigateur : OpenAI la sert par WebRTC, que dictee ne propose pas.
- Le mode conversation du Realtime d'OpenAI, celui qu'utilise le mode d'appel « Realtime » d'Open WebUI.
- Tout changement de la synthèse vocale.

**Anti-objectifs**

- Casser un client actuel : Open WebUI et Dictate continuent d'appeler la même route avec les mêmes champs et reçoivent les mêmes réponses.
- Un glossaire qui invente des mots, même s'il reconnaît mieux les noms rares.
- Écrire le texte ou l'audio dans les journaux.

## 3. Faits mesurés sur lesquels repose la conception

| Fait | Source |
|---|---|
| Une requête REST unique de 6 min 26 dépasse 18 minutes et 6 Go de mémoire : le fichier entier doit être découpé, en tranches de 30 s environ. | Banc du 31 octobre |
| La route REST de transcription de NeMo traite 8,1 s en 2,7 s ; elle accepte `speech_contexts` et rend les mots horodatés en `verbose_json`. Ses routes REST refusent plus de 3600 s. | Banc du 1er novembre, `docs/api.md` de NeMo 0.3.0 |
| La route temps réel de NeMo marque les personnes avec `speaker_diarization` : 6 min 26 en 178 s, 4 personnes comme la passe séparée, 0,93 Go. Elle n'envoie qu'un seul événement final, avec tous les mots, au commit. | Test du 1er novembre sur le VPS |
| Le glossaire à la force 2 insère « Sortformer » dix fois dans une réunion qui ne le contient pas. | Banc du 1er novembre |
| Le champ `prompt` de NeMo renforce une phrase à la force 10. | `docs/api.md` de NeMo 0.3.0 |
| Le Realtime d'OpenAI en transcription : `wss …/v1/realtime?intent=transcription`, `session.update` de type `transcription`, PCM en base64, `input_audio_buffer.commit` par prise de parole, événements `…input_audio_transcription.delta` et `.completed` liés par `item_id`, champ `keywords` pour les mots rares. | Documentation OpenAI, novembre 2026 |
| Le mode background d'OpenAI : `background=true`, statuts `queued`, `in_progress`, `completed`, `failed`, `cancelled`, lecture par `GET` sur l'identifiant, annulation par `POST …/cancel`, idempotente. | Documentation OpenAI, novembre 2026 |

## 4. Approches

| Approche | Principe | Ce qu'elle coûte |
|---|---|---|
| **1. Tâches enregistrées dans dictee** (retenue) | Chaque transcription devient une ligne de la base SQLite de dictee, avec son audio sur le volume. Une boucle de travail dans dictee les traite et reprend les inachevées au redémarrage. | Rien de plus à déployer. Un plantage de dictee suspend la tâche jusqu'au redémarrage. |
| 2. Conteneur de travail séparé | dictee enregistre les tâches, un second conteneur au même code les traite. | Un conteneur de plus, deux processus sur une même base, pour un gain faible : dictee redémarre surtout aux déploiements, et NeMo reste le passage unique. |
| 3. Tâches en mémoire | Le traitement continue sans le client, tant que dictee ne redémarre pas. | Un déploiement ou un plantage perd tout, contre la demande de Geoff. |

Geoff a retenu la première approche le 1er novembre 2026.

## 5. Décisions

| Sujet | Décision | Origine |
|---|---|---|
| Moteur par défaut | Fichier entier de NeMo, par morceaux d'environ 30 s, pour `dictee` et `dictee-clean` sans `stream` | Geoff |
| Réunions | Route temps réel de NeMo avec `speaker_diarization`, en une session, jusqu'à 2 heures | Proposition de Claude, validée par Geoff avec le document |
| Texte progressif | `stream=true` garde la route temps réel, comme aujourd'hui | Geoff |
| Dictée en direct | WebSocket `/v1/realtime?intent=transcription`, au format OpenAI, texte brut seulement | Geoff |
| Coupures | Tâches enregistrées ; identifiant en arrière-plan pour l'application ; envoi identique rattaché à sa tâche pour les outils OpenAI | Geoff |
| Plafond | 2 heures pour tous les modes | Geoff |
| Glossaire | Prudent : la force retenue est la plus haute qui n'insère aucun mot non dit | Geoff |
| Champ `prompt` | Accepté et ignoré, plus transmis à NeMo | Proposition de Claude, validée par Geoff avec le document |
| Conservation | Le résultat de toute transcription enregistrée, dictées courtes comprises, reste 24 heures après la fin de la tâche ; l'audio est effacé dès la fin | Geoff pour la portée et la durée, Claude pour l'effacement de l'audio |
| Coupe des morceaux | Dans les silences repérés par ffmpeg, à défaut coupe nette | Choix de Claude |
| Silero | Non ajouté | Banc du 1er novembre |

Cette conception remplace la décision « API compatible OpenAI et synchrone, sans tâche asynchrone maison » du design du 31 octobre (section 3), ainsi que ses sections 4.7, 6.1, 6.3, 6.4, 6.6 et 7.3 sur les points décrits ici. À la livraison, le design du 31 octobre est mis à jour pour rester la référence unique, et ce document garde l'histoire du changement.

## 6. API

### 6.1 Routes

| Route | Rôle | Nouveau |
|---|---|---|
| `POST /v1/audio/transcriptions` | Toutes les transcriptions de fichier, synchrones ou en arrière-plan | champ `background` |
| `GET /v1/audio/transcriptions/{id}` | État et résultat d'une tâche | oui |
| `POST /v1/audio/transcriptions/{id}/cancel` | Annulation d'une tâche | oui |
| `GET /v1/realtime?intent=transcription` (WebSocket) | Dictée en direct | oui |
| `POST /v1/audio/speech`, `GET /v1/models`, `GET /health`, page et connexion | Inchangées | non |

Les nouvelles routes demandent la même clé que les autres (`Authorization: Bearer`, section 5.4 du design du 31 octobre).

### 6.2 `POST /v1/audio/transcriptions`

Les champs, les modèles et les formats de réponse restent ceux du design du 31 octobre (section 5.2), avec deux changements :

- `background` (`true` ou `false`, défaut `false`) lance la tâche en arrière-plan ;
- `prompt` est accepté et ignoré (section 7.6).

Le moteur dépend du mode et des drapeaux.

| Demande | Moteur | Tâche enregistrée |
|---|---|---|
| `dictee` ou `dictee-clean`, sans `stream` | Fichier entier par morceaux (7.2) | oui |
| `dictee-diarize` (ou `diarized_json`), sans `stream` | Temps réel avec personnes (7.3) | oui |
| Tout mode avec `stream=true` | Temps réel (7.4) | non |
| `stream=true` et `background=true` | Refus 400, code `unsupported_combination` | non |

**Sans `background`.** dictee décode l'envoi, crée la tâche ou rejoint une tâche identique (6.4), puis attend sa fin et répond comme aujourd'hui. Si le client raccroche, la tâche continue. Une tâche en échec donne l'erreur qu'aurait donnée la requête synchrone d'aujourd'hui (429, 502…).

**Avec `background=true`.** dictee décode l'envoi, crée la tâche ou rejoint une tâche identique, puis répond tout de suite 200 avec l'objet tâche (6.3). Un audio illisible ou trop long reste refusé tout de suite, comme aujourd'hui, avant toute tâche.

### 6.3 Objet tâche, lecture et annulation

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

- `status` prend les valeurs d'OpenAI : `queued`, `in_progress`, `completed`, `failed`, `cancelled`.
- `progress` va de 0 à 1 : part des morceaux traités en fichier entier, part de l'audio envoyée à NeMo en temps réel.
- `error` vaut `{"code", "message"}` en échec, avec les codes de l'enveloppe d'erreur actuelle.
- `output` porte, une fois la tâche terminée, la réponse rendue dans le `response_format` de la création : un objet pour `json`, `verbose_json` et `diarized_json`, une chaîne pour `text`, `srt` et `vtt`.
- `expires_at` donne l'heure d'effacement, 24 heures après `completed_at` (variable `TASK_RETENTION_HOURS`).

`GET /v1/audio/transcriptions/{id}` rend cet objet. `POST /v1/audio/transcriptions/{id}/cancel` arrête la tâche et rend l'objet au statut `cancelled` ; l'appel est idempotent et sans effet sur une tâche déjà finie. Une tâche n'est visible que de son propriétaire, le compte retenu par la clé (le compte par défaut pour la clé de service d'Open WebUI sans email connu) : pour tout autre appelant, l'identifiant répond 404, comme un identifiant inconnu ou effacé.

### 6.4 Envoi identique

Deux envois sont identiques quand ils ont le même propriétaire, les mêmes octets de fichier (empreinte SHA-256 calculée pendant la réception), le même mode (deux noms de modèle qui donnent le même mode donnent le même texte, donc se rejoignent), la même langue, le même glossaire et la même force de glossaire, plus les mêmes consignes de reformulation pour `dictee-clean` (choix de Claude : sinon un renvoi rendrait un texte reformulé avec d'anciennes consignes). Le format de réponse et les granularités ne comptent pas : le résultat est gardé sous une forme neutre et rendu à la demande.

Un envoi identique à une tâche `queued` ou `in_progress` la rejoint. Identique à une tâche `completed` non effacée, il reçoit son résultat sans retraitement, sauf quand `dictee-clean` a rendu le texte brut parce que le LLM a échoué (délai dépassé, erreur, réponse vide ou texte écarté pour son écart de longueur) : cette tâche ne se rejoint pas, pour qu'un nouvel envoi redemande la reformulation. Le texte brut d'un audio trop court ou trop long pour le LLM se rejoint comme les autres. Une tâche `failed` ou `cancelled` ne se rejoint pas : l'envoi crée une nouvelle tâche. La règle vaut avec ou sans `background`, de sorte que l'application de Geoff peut renvoyer une demande sans créer de doublon.

### 6.5 Dictée en direct : `/v1/realtime?intent=transcription`

La connexion suit le Realtime d'OpenAI en mode transcription. dictee traduit ses événements vers la route temps réel de NeMo, qui n'utilise pas exactement le même protocole.

| Côté client (OpenAI) | Ce que fait dictee |
|---|---|
| Ouverture avec `Authorization: Bearer` et `intent=transcription` | Identifie l'appelant, ouvre une session NeMo, envoie `session.created`. Sans `intent=transcription`, ou avec un `model` de conversation, il envoie une `error` et ferme. |
| `session.update`, `session.type` à `transcription` | Lit `audio.input.format` (`audio/pcm` seulement, `rate` de 8000 à 96000, 24000 par défaut), la langue (`transcription.languages[0]` ou `transcription.language`, `fr` par défaut), `transcription.keywords` (ajoutés au glossaire de l'appelant) et `turn_detection`. `prompt`, `delay`, `model` et `noise_reduction` sont acceptés et ignorés. Répond `session.updated` avec la session effective. |
| `turn_detection` absent ou `null` | Le client valide chaque prise de parole par `input_audio_buffer.commit`. |
| `turn_detection` de type `server_vad` | Accepté seulement si la vérification 12.3 montre que NeMo finalise seul chaque phrase (`endpointing_ms`) ; sinon refusé par une `error`. `semantic_vad` est toujours refusé. |
| `input_audio_buffer.append` (PCM16 en base64) | Décode et transmet à NeMo en trames binaires. |
| `input_audio_buffer.commit` | Transmet le commit, envoie `input_audio_buffer.committed` avec l'`item_id` de la prise de parole. |
| `input_audio_buffer.clear` | Transmet, puis envoie `input_audio_buffer.cleared`. |
| Événements de NeMo pendant la session | Chaque texte partiel de NeMo devient `conversation.item.input_audio_transcription.delta` (`item_id`, `content_index` à 0, `delta`) ; les textes finals d'une prise de parole, joints par une espace, forment un seul `…completed` (`item_id`, `content_index`, `transcript`), que dictee envoie après `input_audio_buffer.committed`, quand NeMo a rendu tous les textes de la prise de parole. |

L'`item_id` d'une prise de parole (`item_` suivi d'un compteur) naît au premier `append` après le commit précédent, si bien que les textes partiels portent déjà l'identifiant que `committed` annoncera. Si NeMo n'accepte qu'un commit par session (vérification 12.3), dictee ouvre une nouvelle session NeMo pour la prise de parole suivante, sans que le client le voie.

La dictée en direct ne rend que le texte brut : ni reformulation, ni personnes. Une session s'arrête après 2 heures d'audio reçu ou 30 minutes sans événement du client. Les erreurs partent en événements `error` au format OpenAI (`type`, `code`, `message`, `param`, `event_id`). Elle n'est pas enregistrée et ne passe pas par la file des tâches longues.

## 7. Traitement interne

### 7.1 Tâches

**Création.** dictee reçoit l'envoi dans un fichier temporaire en calculant son empreinte, cherche une tâche identique (6.4), puis décode l'audio en WAV PCM16 mono 16 kHz **sur le disque**, dans `/data/tasks/<id>/audio.wav` du volume `dictee-data`, et non plus en mémoire. Un audio illisible, trop long ou trop gros est refusé à ce moment, sans créer de tâche. Le fichier temporaire de l'envoi disparaît dès le décodage fini.

**File.** Une tâche courte (audio de `LONG_AUDIO_SECONDS` ou moins, 600 s par défaut) démarre tout de suite, sans attendre les longues. Les tâches longues passent une à la fois, dans l'ordre d'arrivée. Au plus `MAX_QUEUED_LONG` tâches longues attendent (3 par défaut) ; au-delà, l'envoi reçoit 429 `long_audio_busy`, comme aujourd'hui.

**Boucle de travail.** Une boucle asynchrone de dictee prend les tâches en file et les fait avancer. Elle écrit l'avancement en base après chaque morceau (fichier entier) ou à intervalle régulier (temps réel), et le statut à chaque changement.

**Redémarrage.** Au démarrage, dictee remet en file les tâches `in_progress`. Une tâche en fichier entier reprend au premier morceau non terminé. Une tâche de réunion repart du début, car NeMo ne reprend pas une session temps réel en cours.

**Fin.** À la fin, réussie ou non, dictee efface l'audio de la tâche. Le résultat reste en base jusqu'à `expires_at`. Une boucle de ménage, toutes les 10 minutes, efface les tâches expirées et les dossiers de tâche orphelins.

**Pannes de NeMo pendant une tâche.** Quand NeMo est injoignable ou que sa file est pleine, la tâche attend que `/ready` réponde, puis réessaie le morceau ou la session, trois fois au plus. Au-delà, ou pour une erreur que NeMo signale sur la requête elle-même, la tâche passe en `failed`.

### 7.2 Fichier entier par morceaux (`dictee`, `dictee-clean`)

1. **Points de coupe.** ffmpeg passe une fois sur le WAV avec le filtre `silencedetect` (seuil -35 dB, silence d'au moins 0,3 s). Pour chaque morceau, dictee cherche le silence dont le milieu est le plus proche de 30 s après le début du morceau, entre 20 et 40 s. Sans silence dans cette fenêtre, il coupe net à 30 s. Les points de coupe sont enregistrés avec la tâche, pour qu'une reprise retrouve les mêmes morceaux.
2. **Transcription d'un morceau.** dictee lit la tranche dans le WAV, lui donne un en-tête WAV et l'envoie à `POST /v1/audio/transcriptions` de NeMo avec `language`, `response_format=verbose_json`, `timestamp_granularities[]=word` et `speech_contexts` (glossaire et force retenue, section 7.6).
3. **Assemblage.** Les mots du morceau sont décalés du début du morceau. Le texte et les mots de chaque morceau sont écrits en base dès réception. Le texte final joint les textes non vides des morceaux par une espace.
4. **`dictee-clean`.** La reformulation par le LLM s'applique au texte assemblé, comme aujourd'hui (section 6.2 du design du 31 octobre), puis le résultat est enregistré.

### 7.3 Réunions (`dictee-diarize`)

dictee ouvre une session sur la route temps réel de NeMo avec `word_timestamps`, `speaker_diarization`, la langue et `speech_contexts`. Il lit l'audio depuis le WAV de la tâche et l'envoie par trames d'une seconde, aussi vite que NeMo l'accepte. La part d'audio envoyée donne `progress`.

NeMo rend tous les mots dans l'événement final, chacun avec son numéro de personne. dictee renomme les personnes A, B, C dans l'ordre de leur première prise de parole, et rattache un mot sans personne à la personne du mot qui le précède. Il forme ensuite les segments comme aujourd'hui. La passe séparée `/v1/audio/diarizations` et l'attribution des mots par dictee (`assign_speakers`) disparaissent.

### 7.4 Texte progressif (`stream=true`)

Le comportement reste celui d'aujourd'hui (section 6.4 du design du 31 octobre), avec deux différences. L'audio est lu depuis un fichier temporaire au lieu de la mémoire. En mode réunion, la session temps réel marque les personnes elle-même (7.3), et les segments partent à la fin, avant `transcript.text.done`. Une coupure arrête le traitement, qui n'est pas enregistré.

### 7.5 Dictée en direct

dictee relaie l'audio du client vers une session temps réel de NeMo ouverte avec la langue, `speech_contexts` (glossaire de l'appelant et `keywords`) et, pour `server_vad`, `endpointing_ms`. Il traduit les événements dans les deux sens (6.5) et ferme la session NeMo quand le client part. Une erreur de NeMo devient un événement `error`, et dictee ferme la connexion.

### 7.6 Glossaire prudent

**Mesure de la force.** Le plan mesure les forces 0,5, 1, 1,5 et 2 sur les deux moteurs (fichier entier et temps réel), avec le glossaire réel de Geoff, lu dans son espace avec son accord au moment de la mesure, et les termes « Nemotron », « Sortformer » et « Discord ». Le corpus réunit le Call et la Note du dossier Meets, et un court enregistrement où Geoff prononce plusieurs termes du glossaire (à défaut, des phrases de synthèse qui les contiennent). Un mot du glossaire présent dans une sortie mais absent de la sortie sans glossaire et de l'écoute compte comme une insertion. La force retenue est la plus haute qui ne fait **aucune** insertion sur tout le corpus. Si aucune n'y parvient, c'est 0,5, et le plan le signale à Geoff. La valeur va dans `GLOSSARY_BOOST`, dont la valeur par défaut du code suit.

**Champ `prompt`.** NeMo traite `prompt` comme une phrase renforcée à la force 10, soit cinq fois la force qui insérait déjà des mots. Dictate envoie un prompt fixe. dictee ne transmet donc plus `prompt` à NeMo, ni sur la route de fichier, ni en direct.

## 8. Limites

| Limite | Valeur | Réglage |
|---|---|---|
| Durée d'un audio, tous modes | 2 heures | constante du code |
| Taille d'un envoi | 300 Mo | `MAX_UPLOAD_MB`, inchangé |
| Seuil d'un audio long | 600 s | `LONG_AUDIO_SECONDS`, inchangé |
| Tâches longues | 1 en cours, 3 en attente, puis 429 | `MAX_QUEUED_LONG` |
| Conservation d'un résultat | 24 heures après la fin | `TASK_RETENTION_HOURS` |
| Morceau du fichier entier | 30 s visées, coupe entre 20 et 40 s | constantes du code |
| Session de dictée en direct | 2 heures d'audio, 30 minutes sans événement | constantes du code |

La mémoire de dictee ne dépend plus de la durée de l'audio : le WAV est sur le disque et seule la tranche en cours passe en mémoire. Sa limite de 512 Mo ne change pas. Le disque porte au plus 230 Mo d'audio par tâche longue (2 heures), soit environ 1 Go pour une tâche en cours et trois en attente, effacés à la fin de chaque tâche.

## 9. Base de données

Le schéma passe à la version 2 (`pragma user_version`) et gagne deux tables. La migration part de la version 1 sans toucher aux tables existantes.

| Table | Colonnes |
|---|---|
| `tasks` | `id` (texte `trn_…`), `owner` (numéro du compte, ou vide pour le compte par défaut), `model`, `mode`, `language`, `response_format`, `granularities`, `upload_sha256`, `params_sha256` (langue, mode, glossaire, force, consignes pour `dictee-clean`), `status`, `error_code`, `error_message`, `duration`, `progress`, `cuts` (points de coupe, JSON), `result` (forme neutre, JSON), `attempts`, `created_at`, `started_at`, `completed_at`, `expires_at` |
| `task_chunks` | `task_id`, `idx`, `start`, `end`, `text`, `words` (JSON) ; clé (`task_id`, `idx`) |

Un index sur (`owner`, `upload_sha256`, `params_sha256`) sert la recherche d'envoi identique. La forme neutre du résultat porte le texte, les mots, les segments, la durée et la langue, plus le texte reformulé pour `dictee-clean`.

## 10. Données personnelles et journaux

Le design du 31 octobre disait que dictee ne garde ni le texte ni l'audio. Cela change :

- l'audio décodé reste sur le volume pendant la tâche et disparaît à sa fin ;
- le texte et les mots restent en base 24 heures après la fin, puis sont effacés ;
- les journaux ne contiennent toujours ni texte ni audio.

Chaque tâche écrit une ligne de journal à sa fin, en plus de la ligne de la requête : identifiant de tâche, compte, mode, durée de l'audio, nombre de morceaux, tentatives, temps de NeMo et du LLM, statut. La dictée en direct écrit une ligne par session : compte, secondes d'audio reçues, prises de parole, statut.

## 11. Tests

**Tests automatiques** (`pytest`, faux NeMo HTTP et WebSocket, sans réseau) :

- choix du moteur selon le mode, `stream` et `background`, et refus de `stream` avec `background` ;
- points de coupe : silence le plus proche de 30 s, coupe nette sans silence, morceaux qui couvrent tout l'audio sans trou ni chevauchement ;
- assemblage des morceaux : décalage des mots, texte joint, morceaux vides ;
- tâche synchrone : réponse identique à celle d'aujourd'hui pour chaque format ; la tâche continue quand le client raccroche ;
- arrière-plan : objet tâche, statuts successifs, `progress`, `output` rendu selon le format de création, annulation idempotente, 404 pour un autre propriétaire ;
- envoi identique : rejoint une tâche en cours, reçoit le résultat d'une tâche finie, crée une nouvelle tâche après un échec ou une annulation, distingue deux glossaires différents ;
- redémarrage simulé : une tâche en fichier entier reprend au premier morceau non fait sans renvoyer les autres à NeMo ; une réunion repart du début ;
- pannes de NeMo : attente de `/ready`, trois tentatives, puis `failed` avec le bon code ;
- file : une tâche courte passe pendant une longue, la cinquième tâche longue reçoit 429 ;
- réunions : renommage A, B, C, mot sans personne rattaché au précédent ;
- ménage : effacement à `expires_at`, effacement de l'audio à la fin de la tâche, dossiers orphelins ;
- dictée en direct : traduction de chaque événement dans les deux sens, `item_id` porté par les textes partiels, refus sans `intent=transcription`, refus de `semantic_vad`, session NeMo rouverte après un commit si NeMo l'exige ;
- `prompt` jamais transmis à NeMo ;
- compatibilité avec le client Python officiel d'OpenAI : transcription synchrone, `stream=true`, et session Realtime en transcription.

**Tests d'intégration** sur le VPS, dans des conteneurs jetables puis sur le déploiement après accord de Geoff : une réunion de 2 heures (Note répétée) en arrière-plan avec une coupure du client puis la lecture du résultat, la même en synchrone avec un renvoi du fichier, un redémarrage de dictee au milieu d'une tâche en fichier entier, une dictée en direct par un petit client Python.

## 12. Vérifications à faire en premier dans le plan

Le plan vérifie ces points avant d'écrire le code qui en dépend, et s'arrête pour en parler à Geoff si l'un d'eux échoue.

1. **Route REST de NeMo sur un morceau de 30 s** : mots horodatés en `verbose_json`, effet de `speech_contexts`, temps et mémoire, réponse quand la file de NeMo est pleine.
2. **Réunion de 2 heures en temps réel avec personnes** (Note répétée pour faire 2 heures) : temps, mémoire de `nemo` sous 3 Go, taille de l'événement final, aucun silence de NeMo plus long que le délai d'attente de dictee.
3. **Plusieurs prises de parole dans une session temps réel** : NeMo accepte-t-il de l'audio après un commit, et finalise-t-il seul chaque phrase avec `endpointing_ms` ? La réponse fixe la traduction des commits et l'acceptation de `server_vad`.
4. **`silencedetect`** dans l'image de dictee, et réglages qui placent les coupes dans les silences du Call et de la Note.
5. **Délais de Traefik et des clients** pour une requête synchrone d'une heure, et comportement du client Python d'OpenAI, qui abandonne par défaut après 10 minutes puis réessaie.
6. **Force du glossaire** selon le protocole de la section 7.6.
7. **Place libre** sur le volume `dictee-data`.

## 13. Ce que la conception laisse au constructeur

- Le découpage du code en modules, en suivant la structure actuelle de `dictee/src/dictee`.
- La forme exacte des identifiants (`trn_` suivi d'un identifiant triable dans le temps) et des `item_id`.
- Les délais d'attente précis entre deux tentatives auprès de NeMo, et la fréquence d'écriture de `progress` en temps réel.
- Le nettoyage du code devenu inutile : porte des audios longs en mémoire, passe `/v1/audio/diarizations`, attribution des mots aux personnes, envoi de `prompt` à NeMo.
- La mise à jour du design du 31 octobre et du README à la livraison.

## 14. Hors périmètre

- La reprise d'une réunion au milieu de son traitement.
- La reprise d'un flux d'événements (`stream=true`) après une coupure.
- WebRTC et l'authentification de navigateur du Realtime d'OpenAI.
- Le mode conversation du Realtime d'OpenAI.
- La synthèse vocale.
- Le réglage d'Open WebUI, de Dictate et de l'application Android : chaque changement chez eux attend l'accord de Geoff, au moment de le faire.
