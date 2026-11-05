# Tâches de transcription, découpage et dictée en direct : plan d'implémentation

> **Pour les agents :** sous-skill requis : `superpowers:subagent-driven-development` (recommandé) ou `superpowers:executing-plans`, tâche par tâche. Les étapes se cochent (`- [ ]`).

**Ordre d'exécution :** tâches 1 à 14, puis 16, puis 15.

**Objectif :** faire de chaque transcription de fichier sans `stream` une tâche enregistrée qui survit aux coupures, traiter les fichiers par morceaux sur la route REST de NeMo et les réunions de 2 heures sur sa route temps réel, puis ajouter la dictée en direct au format du Realtime d'OpenAI.

**Architecture :** dictee range chaque tâche dans sa base SQLite (schéma 2) et son audio décodé sur le volume `dictee-data`. Une boucle de travail, dans le processus de dictee, fait avancer les tâches (une longue à la fois, les courtes tout de suite) et reprend les inachevées au démarrage. La route de transcription crée ou rejoint une tâche, puis attend sa fin ou rend son identifiant ; deux routes lisent et annulent une tâche, et une route WebSocket traduit le Realtime d'OpenAI vers la route temps réel de NeMo.

**Stack technique :** Python 3.13, Starlette, uvicorn, httpx, websockets, SQLite, ffmpeg (filtre `silencedetect`), pytest, NeMo-Speech.cpp 0.3.0. Aucune dépendance d'exécution nouvelle.

**Spec :** `docs/superpowers/specs/2026-11-01-taches-transcription-design.md`. Le design du 31 octobre (`docs/superpowers/specs/2026-10-31-dictee-server-design.md`) reste la référence pour tout ce que cette spec ne change pas.

## Contraintes globales

- Le travail se fait sur la branche `feat/taches-transcription`, sur le Mac de Geoff. Geoff fusionne lui-même sur `main`. Aucun push, aucune fusion et aucune écriture dans Dokploy, PocketID, Open WebUI, LiteLLM ou GitHub ne se fait sans son accord explicite au moment de l'action.
- Chaque commit passe `-c user.name=supergeoff -c user.email=16121338+supergeoff@users.noreply.github.com` et se termine par les lignes d'attribution de la session. Messages et documentation sont en français, en phrases complètes. Le code et les termes techniques sont en anglais ; docstrings et commentaires restent en français, comme dans le code actuel.
- Chaque tâche de code commence par un test qui échoue. `make test` lance toute la suite dans Docker, car ffmpeg n'est pas installé sur le Mac ; `make test ARGS="tests/<fichier>::<test>"` lance un seul test (cible ajoutée en tâche 3).
- Durée d'un audio : 2 heures (7200 s) pour tous les modes. Taille d'un envoi : `MAX_UPLOAD_MB`, 300. Seuil d'un audio long : `LONG_AUDIO_SECONDS`, 600 s. Tâches longues : 1 en cours et `MAX_QUEUED_LONG` (3) en attente, puis 429 `long_audio_busy`. Conservation d'un résultat : `TASK_RETENTION_HOURS`, 24 heures après la fin.
- Morceaux du fichier entier : 30 s visées, coupe entre 20 et 40 s, `silencedetect` à -35 dB pour un silence d'au moins 0,3 s, sauf réglage changé par la tâche 2.
- Dictée en direct : une session s'arrête après 2 heures d'audio reçu ou 30 minutes sans événement du client ; `rate` va de 8000 à 96000 (24000 par défaut) ; elle rend le texte brut seulement.
- Mémoire : dictee reste borné à 512 Mo et NeMo à 3 Go. Seule la tranche en cours d'un audio passe en mémoire de dictee.
- Compatibilité : Open WebUI et Dictate appellent la même route avec les mêmes champs et reçoivent les mêmes réponses. Le champ `prompt` est accepté et n'est jamais transmis à NeMo.
- Les journaux ne contiennent jamais de texte, d'audio, de glossaire ni de consignes. D'une exception imprévue, seul le nom de la classe est journalisé.
- Rien de personnel dans le dépôt, qui est public. Les enregistrements du dossier Meets, le glossaire de Geoff et les textes qui en sortent restent dans `.integration/verif-taches/` (ignoré par git) et, le temps d'une tâche, sur le VPS. Les relevés commités viennent de phrases de synthèse. Les copies des enregistrements sont effacées à la fin de la tâche 15 ; l'original reste dans le Drive.
- Sur le VPS, les essais tournent dans des conteneurs jetables nommés `verif-*`, lancés avec `--rm`, à mémoire bornée et sans port publié. Les modèles de production sont montés en lecture seule. Tout est nettoyé à la fin de chaque tâche. Aucun secret ni aucune valeur d'environnement n'est affiché. L'accès au VPS est décrit dans `.integration/verif-taches/LISEZMOI.md`, hors du dépôt.
- Quand une vérification de la section 12 de la spec échoue, le travail s'arrête et Geoff est prévenu avant toute suite.

## Points d'attention

Ces cas ne figurent pas dans les tests de la spec, mais un usage normal les rencontrera. Chacun a son test dans la tâche qui possède le code.

1. **Compte par défaut.** La clé de service d'Open WebUI sans email connu désigne le compte par défaut, rangé avec un `owner` vide. Ce compte doit retrouver, lire et annuler ses propres tâches ; une comparaison SQL `owner = ?` avec `NULL` ne trouverait jamais rien. Tests en tâches 5 et 10.
2. **Client de dictée qui disparaît.** Un client coupé au milieu d'une prise de parole, sans commit ni fermeture propre, ne doit laisser aucune session NeMo ouverte, et la ligne de journal de la session doit s'écrire. Test en tâche 12.
3. **Tâche qui fait tomber dictee.** Une réunion qui dépasse la mémoire de dictee le relancerait en boucle à chaque reprise. Au troisième démarrage, elle passe en `failed` avec le code `interrupted`. Test en tâche 9.
4. **Ménage pendant un décodage.** Le dossier d'un envoi en cours de décodage n'a pas encore de ligne en base. Le ménage ne supprime donc un dossier sans tâche qu'après une heure. Test en tâche 9.
5. **Annulation d'une tâche longue en attente.** Sa place dans la file se libère tout de suite, si bien qu'un nouvel envoi long n'est pas refusé à tort, et elle n'atteint jamais NeMo. Test en tâche 9.

## Décisions du plan

La spec laisse ces points au constructeur (section 13) ou ne les tranche pas. Le plan les fixe ainsi.

| Sujet | Décision | Raison |
|---|---|---|
| Modules | `cuts.py` (coupe), `taskstore.py` (registre), `processing.py` (traitement d'une tâche), `runner.py` (file et boucle), `tasks_api.py` (lecture et annulation), `live.py` (dictée en direct) | Un fichier par responsabilité, à plat comme le code actuel |
| Identifiants | `trn_` suivi de 26 caractères Crockford (horodatage de 48 bits en millisecondes, puis 80 bits aléatoires) ; `item_<n>`, `event_<n>` et `sess_` suivi de 24 caractères hexadécimaux pour la dictée en direct | Un identifiant de tâche se trie dans l'ordre de création |
| Réglages figés | Une colonne `params` s'ajoute à celles de la section 9 : glossaire (termes et déformations), force et consignes de `dictee-clean`, figés à la création | Une reprise travaille avec les réglages hachés dans `params_sha256`, et la reformulation a besoin des déformations |
| Dates des tâches | Secondes Unix entières | L'objet tâche les donne sous cette forme |
| Démarrages | `attempts` compte les démarrages d'une tâche. Une tâche `in_progress` trouvée au démarrage après trois démarrages passe en `failed`, code `interrupted` | Point d'attention 3 |
| Reprises auprès de NeMo | Un essai, puis trois reprises au plus par morceau ou par session, chacune après une réponse positive de `/ready` et une pause de 2, 5 puis 10 s | Section 7.1 ; les délais sont laissés au constructeur |
| Avancement d'une réunion | Écrit en base au plus toutes les 5 s | Section 13 |
| Fin d'une tâche | `completed_at` et `expires_at` sont posés à toute fin, échec et annulation compris | L'objet d'une tâche en échec reste lisible 24 heures |
| Codes d'échec | `overloaded` donne 429, `upstream_error` donne 502, `server_error` et `interrupted` donnent 500. Une attente synchrone sur une tâche annulée reçoit 409 `task_cancelled` | Les mêmes erreurs que la requête synchrone d'aujourd'hui |
| Flux d'un audio long | Un `stream=true` sur un audio long prend une place dans la file des tâches longues, sans être enregistré | La porte d'aujourd'hui faisait de même |
| Emplacement des WAV | Le WAV d'une tâche va dans `<dossier de la base>/tasks/<id>/audio.wav`, celui d'un flux dans le dossier temporaire du système | Le ménage ne voit que des dossiers de tâche |
| Ménage | Un dossier de tâche sans ligne en base n'est effacé qu'après une heure | Point d'attention 4 |
| Morceau unique | Un audio de 40 s ou moins forme un seul morceau, sans passe `silencedetect` | Une dictée courte reste rapide |
| Insertions du glossaire | Un terme compte autant d'insertions que d'occurrences en plus de la sortie sans glossaire et de l'écoute | Lecture prudente de la section 7.6, qui attrape aussi un terme dit une fois et rendu dix fois |
| Clé refusée en dictée en direct | La connexion s'ouvre, un événement `error` part, puis la fermeture 1008 | Le client lit l'erreur au format OpenAI |

## Fichiers

| Fichier | Responsabilité |
|---|---|
| `integration/check_nemo.py` | Contrôles `h`, `i` et `j` : route REST, réunion de 2 heures, prises de parole multiples |
| `integration/boost.py` | Mesure de la force du glossaire selon la section 7.6 |
| `integration/run.py` | Intégration de bout en bout, tâches et dictée en direct comprises |
| `Makefile` | `make test ARGS=…` |
| `nemo/pull-models.sh`, `compose.yaml`, `dictee/src/dictee/__main__.py` | Taille des modèles contrôlée hors réseau, arrêt propre de dictee (tâche 16) |
| `dictee/src/dictee/audio.py` | Décodage en WAV sur le disque, tranche et trames d'un WAV |
| `dictee/src/dictee/cuts.py` | Silences repérés par ffmpeg et points de coupe |
| `dictee/src/dictee/db.py` | Schéma 2 et sa migration |
| `dictee/src/dictee/taskstore.py` | Registre des tâches et de leurs morceaux, forme neutre du résultat |
| `dictee/src/dictee/settings.py` | `MAX_QUEUED_LONG`, `TASK_RETENTION_HOURS` et dossier des tâches |
| `dictee/src/dictee/nemo.py` | Temps réel lu sur le disque avec personnes, morceau REST, `/ready`, session de dictée |
| `dictee/src/dictee/speakers.py` | Renommage des personnes en A, B, C et segments |
| `dictee/src/dictee/processing.py` | Traitement d'une tâche : morceaux, réunion, reformulation, reprises |
| `dictee/src/dictee/runner.py` | File des tâches longues, boucle de travail, reprise, ménage, ligne de journal d'une tâche |
| `dictee/src/dictee/tasks_api.py` | Objet tâche, lecture et annulation |
| `dictee/src/dictee/formats.py` | `render_body`, le corps d'une réponse hors de sa réponse HTTP |
| `dictee/src/dictee/upload.py` | Empreinte SHA-256 du fichier reçu |
| `dictee/src/dictee/transcribe.py` | Route de transcription : tâches, arrière-plan, envoi identique, flux |
| `dictee/src/dictee/live.py` | Dictée en direct sur `/v1/realtime?intent=transcription` |
| `dictee/src/dictee/lifecycle.py` | Champ `task` de la ligne de journal d'une requête |
| `dictee/src/dictee/types.py` | `Committed` et `Cleared` |
| `dictee/src/dictee/app.py` | Registre, file, boucle de travail et nouvelles routes |
| `dictee/tests/` | `test_cuts.py`, `test_taskstore.py`, `test_processing.py`, `test_runner.py`, `test_tasks_api.py`, `test_live.py` et les fichiers existants ; `fixtures/nemo/` reçoit quatre relevés |
| `AGENTS.md`, `README.md`, design du 31 octobre | Mis à jour à la livraison (tâche 14) |

---

### Tâche 1 : protocole de NeMo (vérifications 12.1, 12.2 et 12.3)

Cette tâche passe en premier : ses relevés figent les formats que les tâches 6, 7 et 12 lisent, et ses mesures disent si une réunion de 2 heures tient sur la route temps réel. Elle tourne sur le VPS contre un NeMo jetable : l'image `nemo` de production, ses modèles en lecture seule, les réglages de fils de la production relus sans être affichés, et une clé d'API tirée au hasard pour l'occasion.

**Fichiers :**
- Modifier : `integration/check_nemo.py` (contrôles `h`, `i`, `j` et option `--note`), `integration/nemo_rt.py` (clé lue d'abord dans l'environnement)
- Créer, à partir de phrases de synthèse : `dictee/tests/fixtures/nemo/rest_transcription.json`, `realtime_speakers_events.jsonl`, `realtime_multiturn_events.jsonl`, `realtime_endpointing_events.jsonl`
- Modifier : ce plan, section « Résultats » de la tâche

**Interfaces :**
- Produit : les quatre relevés, et dans « Résultats » les valeurs que reprennent les tâches suivantes : `CHUNK_TIMEOUT` (tâche 7), `SPEAKER_KEY` et `SPEAKERS_EVENT_TIMEOUT` (tâche 6), `NEMO_MANY_COMMITS` et `SERVER_VAD` (tâche 12).

- [ ] **Étape 1 : préparer les enregistrements**

Copier le Call et la Note du dossier Meets de Geoff (Google Drive, par le serveur `google` du broker) dans `.integration/verif-taches/`. Les convertir en WAV PCM16 mono 16 kHz (`call.wav`, `note.wav`) dans un conteneur jetable de l'image dictee de production, qui porte ffmpeg. L'original reste dans le Drive.

- [ ] **Étape 2 : écrire le contrôle `h`, un morceau sur la route REST**

`nemo_rt.api_key()` lit d'abord `NEMO_API_KEY` dans l'environnement, puis le `.env` comme aujourd'hui : le conteneur client du VPS n'a pas de `.env`. `check_nemo.py --only h` envoie à `POST /v1/audio/transcriptions` le dialogue de synthèse (`prepare_audio()`) répété jusqu'à 30 s, avec `language=fr-FR`, `response_format=verbose_json`, `timestamp_granularities[]=word` et `speech_contexts=[{"phrases": ["Nemotron", "Sortformer"], "boost": 1.0}]`. Il écrit la réponse dans `rest_transcription.json` et rend `OK` quand `words` est non vide et que chaque mot porte `word`, `start` et `end`. Il envoie ensuite trois fois les 30 premières secondes de `--note` et affiche la durée de chaque requête, sans garder ni afficher le texte. Il lance enfin 16 requêtes simultanées sur ce morceau et affiche les statuts, ainsi que le corps de la première réponse qui n'est pas 200.

- [ ] **Étape 3 : écrire le contrôle `i`, une réunion de 2 heures**

`--only i` répète `--note` jusqu'à 7200 s (une seconde de silence entre deux copies) et l'envoie dans une session temps réel avec `word_timestamps`, `speaker_diarization` et `language=fr-FR`, sans glossaire, par trames d'une seconde, aussi vite que NeMo les accepte. Il affiche le temps total, le temps entre le commit et l'événement final, le plus long silence entre deux événements de NeMo, la taille en octets de l'événement final, le nombre de mots et le nombre de personnes distinctes. Aucun texte n'est gardé. Une session identique sur le dialogue de synthèse écrit `realtime_speakers_events.jsonl`, sous la forme de `realtime_events.jsonl` (`session.created`, `session.updated`, puis les événements qui suivent le commit).

- [ ] **Étape 4 : écrire le contrôle `j`, plusieurs prises de parole**

`--only j` joue deux scénarios et écrit tous les événements reçus. Le premier envoie la phrase 1, un commit, attend `input_audio_buffer.committed`, puis envoie la phrase 2 et un second commit (`realtime_multiturn_events.jsonl`) ; il dit si un second `completed` et un second `committed` arrivent. Le second ouvre une session avec `endpointing_ms` à 500, envoie au rythme réel la phrase 1, 2 s de silence, la phrase 2 et 2 s de silence, attend 10 s, puis envoie le commit (`realtime_endpointing_events.jsonl`) ; il compte les `completed` reçus avant le commit.

- [ ] **Étape 5 : lancer les contrôles sur le VPS**

Sur un réseau jetable `verif-net`, lancer `verif-nemo` (image `nemo` de production, volume des modèles en `:ro`, `--memory 3g`) et un conteneur client `verif-client` (image `ghcr.io/astral-sh/uv:0.13.5-python3.13-trixie-slim`, avec `integration/`, les relevés et les WAV montés). Dans le client : `NEMO_URL=http://verif-nemo:8000 uv run --script integration/check_nemo.py --only h,i,j --note /work/note.wav`. Pendant `i`, relever depuis le VPS `docker stats --no-stream --format '{{.MemUsage}}' verif-nemo` toutes les 5 s.
Attendu : une ligne `OK` pour `h`, `i` et `j`, puis les quatre relevés rapatriés dans `dictee/tests/fixtures/nemo/`.

- [ ] **Étape 6 : juger et consigner**

Le travail s'arrête et Geoff est prévenu si `h` ne rend aucun mot horodaté, si `i` échoue, si la mémoire de `verif-nemo` atteint 3 Go ou si les mots de l'événement final ne portent aucune personne. Sinon, remplir « Résultats » et en tirer les valeurs : `CHUNK_TIMEOUT` vaut le plus grand de 120 s et de dix fois la plus lente des trois requêtes sur la Note ; `SPEAKERS_EVENT_TIMEOUT` vaut le plus grand de 300 s et de deux fois le plus long silence mesuré par `i` (le commit et le final compris) ; `SPEAKER_KEY` est le nom du champ de la personne dans un mot du relevé ; `NEMO_MANY_COMMITS` est vrai si le premier scénario de `j` rend deux `completed` et deux `committed` ; `SERVER_VAD` est vrai si le second rend au moins deux `completed` avant le commit.

- [ ] **Étape 7 : nettoyer et commiter**

Supprimer `verif-nemo`, `verif-client`, `verif-net` et les fichiers copiés sur le VPS ; les copies du Mac restent dans `.integration/verif-taches/` jusqu'à la tâche 15.

```bash
git add integration/check_nemo.py integration/nemo_rt.py dictee/tests/fixtures/nemo docs/superpowers/plans/2026-11-01-taches-transcription.md
git commit -m "Vérifie la route REST de NeMo, une réunion de 2 heures et les prises de parole multiples"
```

**Résultats** (1er novembre 2026, NeMo jetable sur le VPS, aucun texte de la Note gardé) :
- Morceau REST de 30 s : le dialogue de synthèse rend 46 mots horodatés (`word`, `start`, `end`, `confidence`). Les 30 premières secondes de la Note prennent 8,2 s, 8,5 s puis 7,5 s. `CHUNK_TIMEOUT` retenu : 120 s. File pleine : 16 requêtes simultanées reçoivent toutes 200 en 127,7 s, NeMo les met en file sans répondre 429.
- Réunion de 2 heures (Note répétée jusqu'à 7200 s) : 67,4 minutes de traitement ; 16 s entre le commit et l'événement final ; plus long silence entre deux événements de 23 s ; événement final de 1,47 Mo ; 16 758 mots ; 3 personnes ; pic mémoire de NeMo à 2,19 Gio sur 3 Gio. `SPEAKERS_EVENT_TIMEOUT` retenu : 300 s.
- `SPEAKER_KEY` : `speaker`, un entier numéroté à partir de 1.
- `NEMO_MANY_COMMITS` vaut vrai : une session accepte de l'audio après un commit et rend un second `completed` puis un second `committed`. `SERVER_VAD` vaut faux : avec `endpointing_ms` à 500, aucun `completed` n'arrive avant le commit, même après 2 s de silence et 10 s d'attente.

### Tâche 2 : environnement (vérifications 12.4, 12.5 et 12.7)

**Fichiers :**
- Créer, hors du dépôt : `.integration/verif-taches/coupes.py`
- Modifier : ce plan, section « Résultats » de la tâche

**Interfaces :**
- Produit : le seuil et la durée de silence que la tâche 4 met dans `SILENCE_DB` et `MIN_SILENCE_S`.

- [ ] **Étape 1 : `silencedetect` sur le Call et la Note**

Dans un conteneur jetable de l'image dictee de production (son Dockerfile est celui de la branche), lancer `ffmpeg -hide_banner -nostats -i <fichier>.wav -af silencedetect=noise=-35dB:d=0.3 -f null -` sur `call.wav` et `note.wav`. `coupes.py` lit la sortie d'erreur et applique la règle de la section 7.2 : pour chaque morceau, le silence dont le milieu est le plus proche de 30 s après son début, entre 20 et 40 s, sinon une coupe nette à 30 s, jusqu'à ce qu'il reste 40 s ou moins. Il affiche, par fichier, le nombre de silences, de coupes et de coupes nettes.
Attendu : aucune coupe nette. Sinon, essayer -30 dB, puis une durée de 0,2 s, et garder le premier réglage sans coupe nette. Si aucun n'y parvient, le travail s'arrête et Geoff choisit.

- [ ] **Étape 2 : délais de Traefik**

Lire, sans rien modifier, la version de Traefik, sa configuration statique (`respondingTimeouts` des points d'entrée : `readTimeout`, `writeTimeout`, `idleTimeout`) et la configuration dynamique du routeur de dictee (`serversTransport`, `forwardingTimeouts`). Une valeur absente prend le défaut documenté pour cette version.
Le travail s'arrête et Geoff choisit si un délai de moins de 3600 s s'applique à l'attente de la réponse, car changer Traefik est une écriture hors du dépôt. Un `readTimeout` de moins de 120 s est signalé à Geoff sans arrêter le travail : il borne l'envoi d'un gros fichier.

- [ ] **Étape 3 : délais du client Python d'OpenAI**

Run : `docker build -q --target test -t dictee-test dictee && docker run --rm dictee-test uv run --frozen python -c "import openai._constants as c; print(c.DEFAULT_TIMEOUT, c.DEFAULT_MAX_RETRIES)"`
Attendu : un délai de 600 s et 2 reprises. Consigner la conséquence : une requête synchrone de plus de 10 minutes, faite par ce client avec ses réglages par défaut, est renvoyée deux fois et rejoint sa tâche ; le client abandonne après 30 minutes environ, la tâche continue, et un renvoi plus tard reçoit le résultat.

- [ ] **Étape 4 : place libre sur le volume**

Lire le point de montage de `ai-dictee-tltwgv_dictee-data` (`docker volume inspect`) et la place libre de son système de fichiers (`df -h`).
Attendu : au moins 5 Go libres. Sinon, le travail s'arrête et Geoff est prévenu.

- [ ] **Étape 5 : consigner et commiter**

```bash
git add docs/superpowers/plans/2026-11-01-taches-transcription.md
git commit -m "Consigne les vérifications de silencedetect, des délais et de la place libre"
```

**Résultats** (1er novembre 2026) :
- `silencedetect` (ffmpeg 7.1.5 de l'image dictee) à -35 dB et 0,3 s : le Call (43,6 s) a 17 silences et 1 coupe, la Note (385,9 s) 97 silences et 12 coupes, sans aucune coupe nette. Les réglages -30 dB et 0,2 s ne coupent pas plus net. Réglage retenu : celui de la spec, -35 dB et 0,3 s.
- Traefik v3.6.7 : la configuration statique ne fixe aucun `respondingTimeouts`, et le routeur de dictee n'a pas de `serversTransport`. Les défauts s'appliquent : `writeTimeout` à 0 et `responseHeaderTimeout` à 0, donc rien ne coupe l'attente d'une réponse synchrone d'une heure ; `readTimeout` à 60 s, qui borne la lecture de toute la requête, corps compris. Ce dernier point est signalé à Geoff : un envoi de plus de 60 s (un gros fichier sur une liaison lente) serait coupé.
- Client Python d'OpenAI 3.42.0 : délai de 600 s et 2 reprises. Une requête synchrone de plus de 10 minutes est renvoyée deux fois, rejoint sa tâche, puis le client abandonne après 30 minutes environ ; la tâche continue et un renvoi plus tard reçoit le résultat.
- Volume `dictee-data` : 46 Go libres.

### Tâche 3 : audio décodé sur le disque

**Fichiers :**
- Modifier : `Makefile`, `dictee/src/dictee/audio.py`
- Tester : `dictee/tests/test_audio.py`

**Interfaces :**
- Produit :
  - `async def decode_wav16k(src: Path, dst: Path, *, max_seconds: float, timeout_s: float = 600.0, long_seconds: float | None = None, on_long: Callable[[], None] | None = None) -> float` : même contrat que `to_wav16k` (erreurs, appel unique de `on_long`, démuxeurs permis), mais le WAV va dans `dst`, avec l'en-tête canonique de 44 octets, en PCM16 mono 16 kHz, et la fonction rend la durée en secondes. Toute exception efface `dst`. Le dossier parent de `dst` existe déjà.
  - `def read_wav_slice(path: Path, start: float, end: float) -> bytes` : un WAV canonique des échantillons `[round(start * 16000), round(end * 16000))` d'un fichier écrit par `decode_wav16k`, la fin ramenée à celle du fichier.
  - `def pcm_frames(path: Path, frame_bytes: int) -> Iterator[bytes]` : le PCM qui suit l'en-tête de 44 octets, par blocs de `frame_bytes` octets, le dernier plus court.
  - `to_wav16k`, `wav_duration` et `wav_pcm` restent jusqu'à la tâche 6.

- [ ] **Étape 1 : ouvrir la cible `test` aux arguments**

La cible `test` du `Makefile` lance `docker run --rm dictee-test uv run --frozen pytest -q $(ARGS)`. `ARGS` vide garde le comportement actuel, celui que la CI utilise.
Run : `make test`
Attendu : la suite actuelle passe en entier.

- [ ] **Étape 2 : écrire les tests qui échouent**

`numbered_wav(directory, seconds)` écrit un WAV canonique dont l'échantillon `n` vaut `(n % 20000) - 10000`.

```python
async def test_decoding_to_a_file_writes_a_canonical_wav_and_gives_its_duration(tmp_path):
    dst = tmp_path / "audio.wav"
    duration = await decode_wav16k(make_audio(tmp_path, "tone.mp3", seconds=3.0), dst, max_seconds=60)
    assert duration == pytest.approx(3.0, abs=0.1)
    with wave.open(str(dst)) as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 16000)
    assert dst.stat().st_size == 44 + round(duration * 32000)

async def test_decoding_to_a_file_stops_at_the_limit_and_leaves_no_file(tmp_path):
    dst = tmp_path / "audio.wav"
    with pytest.raises(AudioTooLong):
        await decode_wav16k(make_audio(tmp_path, "long.mp3", seconds=5.0), dst, max_seconds=2)
    assert not dst.exists()

async def test_an_unreadable_file_leaves_no_decoded_file(tmp_path):
    src = tmp_path / "junk.mp3"
    src.write_bytes(b"not audio")
    with pytest.raises(AudioDecodeError):
        await decode_wav16k(src, tmp_path / "audio.wav", max_seconds=60)
    assert not (tmp_path / "audio.wav").exists()

async def test_a_refusal_from_on_long_stops_the_decoding_and_leaves_no_file(tmp_path):
    calls = []

    def refuse():
        calls.append(1)
        raise RuntimeError("busy")

    src = make_audio(tmp_path, "five.mp3", seconds=5.0)
    with pytest.raises(RuntimeError):
        await decode_wav16k(src, tmp_path / "audio.wav", max_seconds=60, long_seconds=1, on_long=refuse)
    assert calls == [1] and not (tmp_path / "audio.wav").exists()

async def test_decoding_memory_does_not_grow_with_the_duration(tmp_path):
    src = make_audio(tmp_path, "minute.flac", seconds=60.0)
    tracemalloc.start()
    await decode_wav16k(src, tmp_path / "audio.wav", max_seconds=120)
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert peak < 2 * MIB  # une minute décodée pèse 1,9 Mio

def test_a_slice_is_a_wav_of_the_samples_between_its_bounds(tmp_path):
    piece = read_wav_slice(numbered_wav(tmp_path, 3.0), 1.0, 2.5)
    with wave.open(io.BytesIO(piece)) as wav:
        assert (wav.getframerate(), wav.getnframes()) == (16000, 24000)
        first = struct.unpack("<h", wav.readframes(1))[0]
    assert first == 16000 % 20000 - 10000

def test_a_slice_past_the_end_stops_at_the_end(tmp_path):
    with wave.open(io.BytesIO(read_wav_slice(numbered_wav(tmp_path, 3.0), 2.0, 10.0))) as wav:
        assert wav.getnframes() == 16000

def test_pcm_frames_give_the_samples_after_the_header_in_order(tmp_path):
    path = numbered_wav(tmp_path, 2.5)
    frames = list(pcm_frames(path, 32000))
    assert [len(frame) for frame in frames] == [32000, 32000, 16000]
    assert b"".join(frames) == path.read_bytes()[44:]
```

- [ ] **Étape 3 : vérifier l'échec**

Run : `make test ARGS="tests/test_audio.py"`
Attendu : les nouveaux tests échouent sur `ImportError` (`decode_wav16k`, `read_wav_slice`, `pcm_frames`).

- [ ] **Étape 4 : écrire `decode_wav16k`, `read_wav_slice` et `pcm_frames`**

`decode_wav16k` partage le lancement de ffmpeg avec `to_wav16k` ; il écrit 44 octets réservés, ajoute chaque bloc lu au fichier, puis écrit l'en-tête en place à la fin. Aucun des deux ne garde le PCM en mémoire au-delà du bloc en cours.

- [ ] **Étape 5 : vérifier le succès**

Run : `make test`
Attendu : toute la suite passe.

- [ ] **Étape 6 : commiter**

```bash
git add Makefile dictee/src/dictee/audio.py dictee/tests/test_audio.py
git commit -m "Décode l'audio dans un fichier et lit un WAV par tranches ou par trames"
```

### Tâche 4 : points de coupe

**Fichiers :**
- Créer : `dictee/src/dictee/cuts.py`
- Tester : `dictee/tests/test_cuts.py`

**Interfaces :**
- Consomme : un WAV canonique (tâche 3) ; `AudioDecodeError` (`audio.py`) ; les réglages de la tâche 2.
- Produit :
  - `SILENCE_DB = -35.0`, `MIN_SILENCE_S = 0.3` (ou les valeurs retenues par la tâche 2), `TARGET_CHUNK_S = 30.0`, `MIN_CHUNK_S = 20.0`, `MAX_CHUNK_S = 40.0`.
  - `@dataclass(frozen=True) class Silence` : `start: float`, `end: float`, propriété `middle -> float`.
  - `async def detect_silences(wav: Path, duration: float, *, timeout_s: float = 600.0) -> list[Silence]` : une passe de ffmpeg avec `silencedetect=noise=<SILENCE_DB>dB:d=<MIN_SILENCE_S>`, la sortie d'erreur lue ligne à ligne ; un silence encore ouvert à la fin se ferme à `duration` ; un fichier illisible ou un délai dépassé lève `AudioDecodeError`.
  - `def plan_cuts(duration: float, silences: Sequence[Silence]) -> list[float]` et `def chunk_bounds(duration: float, cuts: Sequence[float]) -> list[tuple[float, float]]`.

La règle de `plan_cuts`, que la signature ne détermine pas :

```text
start = 0
tant que duration - start > MAX_CHUNK_S :
    candidats = silences dont le milieu est dans [start + MIN_CHUNK_S, start + MAX_CHUNK_S]
    coupe = milieu le plus proche de start + TARGET_CHUNK_S (à égalité, le plus tôt),
            ou start + TARGET_CHUNK_S sans candidat
    ajouter coupe ; start = coupe
```

- [ ] **Étape 1 : écrire les tests qui échouent**

`wav_with_silences(directory, seconds, silences)` écrit un WAV canonique : un sinus d'amplitude 8000, et des zéros dans chaque intervalle de `silences`. `random_silences(rng, duration)` tire des silences triés et disjoints de 0,3 à 2 s.

```python
def test_the_cut_falls_in_the_silence_closest_to_30_seconds():
    silences = [Silence(21.0, 21.6), Silence(29.0, 29.4), Silence(33.0, 34.0)]
    assert plan_cuts(65.0, silences) == pytest.approx([29.2])

def test_without_silence_between_20_and_40_seconds_the_cut_is_at_30():
    assert plan_cuts(65.0, [Silence(10.0, 11.0), Silence(45.0, 46.0)]) == [30.0]

def test_a_tie_goes_to_the_earlier_silence():
    assert plan_cuts(70.0, [Silence(27.9, 28.1), Silence(31.9, 32.1)]) == pytest.approx([28.0])

def test_each_chunk_looks_from_its_own_start():
    assert plan_cuts(95.0, [Silence(31.0, 31.2), Silence(58.0, 59.0)]) == pytest.approx([31.1, 58.5])

def test_audio_of_40_seconds_or_less_is_one_chunk():
    assert plan_cuts(40.0, []) == []
    assert chunk_bounds(12.0, []) == [(0.0, 12.0)]

def test_chunks_cover_the_audio_without_gap_or_overlap():
    rng = random.Random(7)
    for _ in range(200):
        duration = rng.uniform(1.0, 7200.0)
        bounds = chunk_bounds(duration, plan_cuts(duration, random_silences(rng, duration)))
        assert bounds[0][0] == 0.0 and bounds[-1][1] == duration
        assert all(left[1] == right[0] for left, right in zip(bounds, bounds[1:]))
        assert all(0 < end - start <= MAX_CHUNK_S for start, end in bounds)

async def test_detect_silences_finds_the_silent_stretches(tmp_path):
    path = wav_with_silences(tmp_path, 30.0, [(10.0, 11.0), (20.0, 20.2)])
    [silence] = await detect_silences(path, 30.0)
    assert silence.start == pytest.approx(10.0, abs=0.05)
    assert silence.end == pytest.approx(11.0, abs=0.05)

async def test_a_silence_that_lasts_until_the_end_closes_at_the_duration(tmp_path):
    path = wav_with_silences(tmp_path, 12.0, [(10.0, 12.0)])
    assert (await detect_silences(path, 12.0))[-1].end == 12.0

async def test_detect_silences_of_an_unreadable_file_raises(tmp_path):
    path = tmp_path / "junk.wav"
    path.write_bytes(b"not audio")
    with pytest.raises(AudioDecodeError):
        await detect_silences(path, 1.0)
```

- [ ] **Étape 2 : vérifier l'échec**

Run : `make test ARGS="tests/test_cuts.py"`
Attendu : échec sur `ModuleNotFoundError: dictee.cuts`.

- [ ] **Étape 3 : écrire `cuts.py`**

ffmpeg est lancé avec `-nostdin -hide_banner -nostats -loglevel info`, `-protocol_whitelist file`, puis `-af silencedetect=…` et `-f null -`. Le processus est tué à la sortie, quelle qu'elle soit, comme dans `audio.py`.

- [ ] **Étape 4 : vérifier le succès**

Run : `make test ARGS="tests/test_cuts.py"`
Attendu : PASS.

- [ ] **Étape 5 : commiter**

```bash
git add dictee/src/dictee/cuts.py dictee/tests/test_cuts.py
git commit -m "Repère les silences et place les coupes des morceaux"
```

### Tâche 5 : base en version 2 et registre des tâches

**Fichiers :**
- Modifier : `dictee/src/dictee/db.py`, `dictee/src/dictee/settings.py`
- Créer : `dictee/src/dictee/taskstore.py`, `dictee/tests/taskdata.py` (données de test partagées par les tâches 5 à 11)
- Tester : `dictee/tests/test_db.py`, `dictee/tests/test_settings.py`, `dictee/tests/test_taskstore.py`

**Interfaces :**
- Consomme : `Mode` (`modes.py`), `GlossaryEntry` (`profile.py`), `Word`, `Segment`, `Transcript` (`types.py`).
- Produit :
  - `db.SCHEMA_VERSION = 2` ; `Database.connect()` devient publique (ancien `_connect`).
  - `Settings.max_queued_long: int = 3` (`MAX_QUEUED_LONG`), `Settings.task_retention_hours: float = 24.0` (`TASK_RETENTION_HOURS`), propriété `Settings.tasks_dir -> Path`, qui vaut `Path(db_path).parent / "tasks"`.
  - `class TaskStatus(StrEnum)` : `QUEUED = "queued"`, `IN_PROGRESS = "in_progress"`, `COMPLETED = "completed"`, `FAILED = "failed"`, `CANCELLED = "cancelled"` ; `TERMINAL_STATUSES` regroupe les trois derniers.
  - `@dataclass(frozen=True) class TaskParams` : `language: str` (forme NeMo, `fr-FR`), `mode: Mode`, `glossary: tuple[GlossaryEntry, ...]`, `boost: float`, `instructions: str | None` (rempli pour `Mode.CLEAN` seulement) ; `digest() -> str`, le SHA-256 hexadécimal du JSON trié de ces cinq champs.
  - `@dataclass(frozen=True) class TaskResult` : `text: str`, `words: tuple[Word, ...]`, `segments: tuple[Segment, ...] | None`, `duration: float`, `language: str`, `cleaned: str | None = None`, `decision: str | None = None` ; `to_json() -> str`, `from_json(raw: str) -> TaskResult`, `transcript() -> Transcript` (le texte reformulé quand il existe, sinon le texte brut).
  - `@dataclass(frozen=True) class ChunkRecord` : `idx: int`, `start: float`, `end: float`, `text: str`, `words: tuple[Word, ...]`.
  - `@dataclass(frozen=True) class TaskRecord` : `id`, `owner: int | None`, `model: str`, `params: TaskParams`, `response_format: str`, `granularities: frozenset[str]`, `upload_sha256: str`, `status: TaskStatus`, `error_code: str | None`, `error_message: str | None`, `duration: float`, `progress: float`, `cuts: tuple[float, ...] | None`, `result: TaskResult | None`, `attempts: int`, `created_at: int`, `started_at: int | None`, `completed_at: int | None`, `expires_at: int | None`.
  - `def new_task_id(now_ms: int | None = None) -> str`.
  - `class TaskStore(db: Database)`, toutes dates en secondes Unix :
    - `create(*, task_id, owner, model, params, response_format, granularities, upload_sha256, duration, now) -> TaskRecord` (statut `queued`) ;
    - `get(task_id) -> TaskRecord | None` ; `get_visible(task_id, owner, now) -> TaskRecord | None` (`None` pour un autre propriétaire ou une tâche dont `expires_at <= now`) ;
    - `find_identical(owner, upload_sha256, params_digest, now) -> TaskRecord | None` : la plus récente des tâches `queued` ou `in_progress`, ou `completed` non expirée ;
    - `start(task_id, now) -> TaskRecord` (`in_progress`, `attempts + 1`, `started_at` posé au premier démarrage seulement) ;
    - `set_cuts(task_id, cuts)`, `save_chunk(task_id, chunk)` (remplace le morceau du même rang), `chunks(task_id) -> list[ChunkRecord]` (par rang), `set_progress(task_id, progress)` ;
    - `complete(task_id, result, now, retention_s) -> TaskRecord` (`progress` à 1) ; `fail(task_id, code, message, now, retention_s) -> TaskRecord` ; `cancel(task_id, now, retention_s) -> TaskRecord | None` (sans effet sur une tâche finie, `None` pour un identifiant inconnu) ; ces trois-là posent `completed_at = now` et `expires_at = now + retention_s` ;
    - `unfinished() -> list[TaskRecord]` (`queued` et `in_progress`, par date de création puis identifiant) ; `delete_expired(now) -> list[str]` (avec leurs morceaux) ; `known_ids() -> set[str]`.

Le schéma 2 ajoute `tasks` avec les colonnes de la section 9 de la spec, plus `params` (JSON du glossaire, de la force et des consignes), et `task_chunks` avec `on delete cascade` vers `tasks`. Un index couvre (`owner`, `upload_sha256`, `params_sha256`). `end` est un mot réservé de SQL et s'écrit entre guillemets. La comparaison des propriétaires utilise `owner is ?`, qui vaut aussi pour `NULL`.

- [ ] **Étape 1 : écrire les tests qui échouent**

`taskdata.py` définit `NOW = 1_793_500_000`, `RAW = TaskParams("fr-FR", Mode.RAW, (GlossaryEntry("Nemotron"),), 1.0, None)`, `RESULT` (un `TaskResult` dont les mots portent des personnes, avec ses segments) et `make(store, **changes)`, qui crée une tâche (propriétaire 1, modèle `dictee`, `RAW`, `json`, granularité `segment`, empreinte `"a" * 64`, 12 s, `NOW`) en remplaçant les valeurs données. La fixture `store` vit dans `conftest.py`.

```python
def test_task_ids_carry_their_prefix_and_sort_by_creation_time():
    first, second = new_task_id(1_700_000_000_000), new_task_id(1_700_000_000_001)
    assert re.fullmatch(r"trn_[0-9A-HJKMNP-TV-Z]{26}", first)
    assert first < second

def test_the_digest_follows_the_glossary_the_boost_and_the_instructions():
    assert RAW.digest() == replace(RAW).digest()
    assert RAW.digest() != replace(RAW, glossary=(GlossaryEntry("Sortformer"),)).digest()
    assert RAW.digest() != replace(RAW, glossary=(GlossaryEntry("Nemotron", ("némo trône",)),)).digest()
    assert RAW.digest() != replace(RAW, boost=1.5).digest()
    assert RAW.digest() != replace(RAW, mode=Mode.CLEAN, instructions="Corrige.").digest()

def test_a_new_task_is_queued_without_progress(store):
    task = make(store)
    assert (task.status, task.progress, task.attempts, task.result) == (TaskStatus.QUEUED, 0.0, 0, None)
    assert store.get(task.id) == task

def test_an_identical_upload_finds_a_queued_running_or_finished_task(store):
    task = make(store)
    assert store.find_identical(1, task.upload_sha256, RAW.digest(), NOW) == task
    store.start(task.id, NOW)
    assert store.find_identical(1, task.upload_sha256, RAW.digest(), NOW).status is TaskStatus.IN_PROGRESS
    store.complete(task.id, RESULT, NOW, 86_400)
    assert store.find_identical(1, task.upload_sha256, RAW.digest(), NOW + 60).status is TaskStatus.COMPLETED

def test_failed_cancelled_and_expired_tasks_are_never_joined(store):
    failed, cancelled, done = make(store), make(store, upload_sha256="b" * 64), make(store, upload_sha256="c" * 64)
    store.fail(failed.id, "overloaded", "busy", NOW, 86_400)
    store.cancel(cancelled.id, NOW, 86_400)
    store.complete(done.id, RESULT, NOW, 60)
    assert store.find_identical(1, "a" * 64, RAW.digest(), NOW) is None
    assert store.find_identical(1, "b" * 64, RAW.digest(), NOW) is None
    assert store.find_identical(1, "c" * 64, RAW.digest(), NOW + 60) is None

def test_the_default_account_finds_and_sees_its_own_tasks(store):
    task = make(store, owner=None)
    assert store.find_identical(None, task.upload_sha256, RAW.digest(), NOW) == task
    assert store.get_visible(task.id, None, NOW) == task
    assert store.get_visible(task.id, 7, NOW) is None

def test_another_owner_and_an_expired_task_are_not_visible(store):
    task = make(store)
    store.complete(task.id, RESULT, NOW, 60)
    assert store.get_visible(task.id, 2, NOW) is None
    assert store.get_visible(task.id, 1, NOW + 59) is not None
    assert store.get_visible(task.id, 1, NOW + 60) is None

def test_start_counts_the_starts_and_keeps_the_first_start_date(store):
    task = make(store)
    store.start(task.id, NOW + 1)
    again = store.start(task.id, NOW + 9)
    assert (again.attempts, again.started_at, again.status) == (2, NOW + 1, TaskStatus.IN_PROGRESS)

def test_chunks_are_kept_once_per_rank_and_read_back_in_order(store):
    task = make(store)
    store.save_chunk(task.id, ChunkRecord(1, 30.0, 61.0, "Salut.", (Word("Salut.", 31.0, 31.5),)))
    store.save_chunk(task.id, ChunkRecord(0, 0.0, 30.0, "Bonjour.", ()))
    store.save_chunk(task.id, ChunkRecord(1, 30.0, 61.0, "Salut !", ()))
    assert [(chunk.idx, chunk.text) for chunk in store.chunks(task.id)] == [(0, "Bonjour."), (1, "Salut !")]

def test_completion_keeps_the_result_for_the_retention(store):
    done = store.complete(make(store).id, RESULT, NOW, 86_400)
    assert (done.status, done.progress, done.completed_at, done.expires_at) == (TaskStatus.COMPLETED, 1.0, NOW, NOW + 86_400)
    assert done.result == RESULT

def test_failure_and_cancellation_record_their_end(store):
    failed = store.fail(make(store).id, "upstream_error", "no answer", NOW, 86_400)
    assert (failed.status, failed.error_code, failed.expires_at) == (TaskStatus.FAILED, "upstream_error", NOW + 86_400)
    assert store.cancel(failed.id, NOW + 5, 86_400) == failed
    assert store.cancel("trn_unknown", NOW, 86_400) is None

def test_unfinished_tasks_come_in_creation_order(store):
    later, earlier = make(store, now=NOW + 10), make(store, now=NOW)
    store.complete(make(store, now=NOW + 5).id, RESULT, NOW, 60)
    assert [task.id for task in store.unfinished()] == [earlier.id, later.id]

def test_expired_tasks_are_deleted_with_their_chunks(store):
    task = make(store)
    store.save_chunk(task.id, ChunkRecord(0, 0.0, 12.0, "Bonjour.", ()))
    store.complete(task.id, RESULT, NOW, 60)
    assert store.delete_expired(NOW + 60) == [task.id]
    assert store.get(task.id) is None and store.chunks(task.id) == []

def test_a_result_survives_its_json_form():
    assert TaskResult.from_json(RESULT.to_json()) == RESULT  # RESULT porte des mots avec personnes et des segments
```

Dans `test_db.py` et `test_settings.py` :

```python
def test_the_schema_is_created_at_version_2(db, db_path):
    assert user_version(db_path) == 2
    assert {"tasks", "task_chunks"} <= table_names(db_path)

def test_a_version_1_database_gains_the_task_tables_and_keeps_its_accounts(tmp_path):
    path = write_version_1_database(tmp_path, email="alice@example.test")  # schéma 1 recopié dans le test
    database = Database(str(path))
    assert user_version(path) == 2 and database.user_by_email("alice@example.test") is not None

def test_task_settings_have_their_defaults(settings):
    assert (settings.max_queued_long, settings.task_retention_hours) == (3, 24.0)
    assert settings.tasks_dir == Path(settings.db_path).parent / "tasks"

def test_task_settings_come_from_the_environment():
    settings = Settings.from_env({**REQUIRED_ENV, "MAX_QUEUED_LONG": "5", "TASK_RETENTION_HOURS": "1.5"})
    assert (settings.max_queued_long, settings.task_retention_hours) == (5, 1.5)
```

Les tests existants qui attendaient la version 1 attendent désormais la version 2.

- [ ] **Étape 2 : vérifier l'échec**

Run : `make test ARGS="tests/test_taskstore.py tests/test_db.py tests/test_settings.py"`
Attendu : échec sur `ModuleNotFoundError: dictee.taskstore` et sur la version du schéma.

- [ ] **Étape 3 : écrire la migration, les réglages et `taskstore.py`**

La migration crée les tables de la version 1 comme aujourd'hui, puis, si `user_version` vaut moins de 2, les tables des tâches, avant de poser `user_version = 2`. Une version plus récente n'est pas touchée.

- [ ] **Étape 4 : vérifier le succès**

Run : `make test`
Attendu : toute la suite passe.

- [ ] **Étape 5 : commiter**

```bash
git add dictee/src/dictee/db.py dictee/src/dictee/settings.py dictee/src/dictee/taskstore.py dictee/tests/conftest.py dictee/tests/taskdata.py dictee/tests/test_db.py dictee/tests/test_settings.py dictee/tests/test_taskstore.py
git commit -m "Ajoute le registre des tâches et passe la base en version 2"
```

### Tâche 6 : temps réel lu sur le disque, personnes marquées par NeMo, `prompt` retiré

À la fin de cette tâche, la route répond encore par la session temps réel, comme aujourd'hui, mais elle décode l'envoi dans un fichier temporaire et prend les personnes dans la session elle-même.

**Fichiers :**
- Modifier : `dictee/src/dictee/nemo.py`, `dictee/src/dictee/speakers.py`, `dictee/src/dictee/transcribe.py`, `dictee/src/dictee/audio.py`, `dictee/tests/fakes.py`
- Tester : `dictee/tests/test_nemo.py`, `dictee/tests/test_speakers.py`, `dictee/tests/test_transcriptions.py`, `dictee/tests/test_audio.py`

**Interfaces :**
- Consomme : `decode_wav16k`, `pcm_frames` (tâche 3) ; `realtime_speakers_events.jsonl`, `SPEAKER_KEY` et `SPEAKERS_EVENT_TIMEOUT` (tâche 1).
- Produit :
  - `NemoClient.transcribe(self, wav: Path, *, language: str, phrases: Sequence[str], boost: float, speakers: bool = False, partials: bool = False, on_progress: Callable[[float], None] | None = None) -> AsyncIterator[Final | Partial]` : `speaker_diarization: true` dans la session quand `speakers` est vrai, jamais de `prompt` ; le WAV est lu par trames d'une seconde (`FRAME_BYTES`) avec `pcm_frames` ; `on_progress` reçoit les secondes déjà envoyées après chaque trame ; chaque `Word.speaker` vaut `str(word[SPEAKER_KEY])` quand NeMo le donne, `None` sinon ; avec `speakers`, l'attente entre deux événements va jusqu'à `SPEAKERS_EVENT_TIMEOUT`.
  - `NemoClient.diarize`, `DIARIZE_TIMEOUT`, `UPLOAD_CHUNK_SIZE` et `_blocks` disparaissent.
  - `speakers.label_speakers(words: Sequence[Word]) -> list[Word]` : les personnes brutes deviennent A, B, C dans l'ordre de leur première apparition ; un mot sans personne prend celle du mot précédent, et les premiers mots sans personne prennent la première qui apparaît ensuite (A si aucune n'apparaît). `assign_speakers` disparaît ; `build_segments` ne change pas.
  - `transcribe.MAX_AUDIO_SECONDS = 7200.0` pour tous les modes, message `The audio is longer than 120 minutes.` ; `MAX_DIARIZE_SECONDS` disparaît ; le champ `prompt` n'est plus lu.
  - `FakeNemo.transcribe(wav, *, language, phrases, boost, speakers=False, partials=False, on_progress=None)` note `language`, `phrases`, `boost`, `speakers`, `partials`, `wav_rate` et `seconds` (lus dans le fichier), appelle `on_progress(seconds)` avant son premier événement et retire les personnes des mots quand `speakers` est faux. Les mots de `FINALS` portent les personnes brutes `"0"` puis `"1"`. `TURNS` et `FakeNemo.diarize` disparaissent.
  - `to_wav16k`, `wav_duration` et `wav_pcm` disparaissent avec leurs tests quand plus rien ne les appelle.

- [ ] **Étape 1 : écrire les tests qui échouent**

`write_wav(directory, seconds)` écrit un WAV canonique (comme `make_wav`, mais dans un fichier). L'aide `transcribe(nemo, path, **options)` de `test_nemo.py` prend un chemin et perd `prompt`. `SPEAKERS_AFTER_COMMIT` (les événements qui suivent le commit) et `SPEAKERS_COMPLETED` (son `completed`) viennent du relevé de la tâche 1 ; le faux serveur `fake_ws` les rejoue quand on les met dans `fake_ws.after_commit`.

```python
async def test_speakers_are_asked_in_the_session_update(fake_ws, nemo, tmp_path):
    await transcribe(nemo, write_wav(tmp_path, 1.0), speakers=True)
    assert fake_ws.session["speaker_diarization"] is True

async def test_no_speakers_and_no_prompt_unless_asked(fake_ws, nemo, tmp_path):
    await transcribe(nemo, write_wav(tmp_path, 1.0))
    assert "speaker_diarization" not in fake_ws.session and "prompt" not in fake_ws.session

async def test_words_carry_the_speaker_that_nemo_gives(fake_ws, nemo, tmp_path):
    fake_ws.after_commit = SPEAKERS_AFTER_COMMIT
    events = await transcribe(nemo, write_wav(tmp_path, 1.0), speakers=True)
    speakers = [word.speaker for event in events if isinstance(event, Final) for word in event.words]
    assert speakers == [str(word[SPEAKER_KEY]) for word in SPEAKERS_COMPLETED["words"]]

async def test_the_audio_is_read_from_the_file_by_one_second_frames(fake_ws, nemo, tmp_path):
    path = write_wav(tmp_path, 2.5)
    await transcribe(nemo, path)
    assert [len(frame) for frame in fake_ws.frames] == [32000, 32000, 16000]
    assert b"".join(fake_ws.frames) == path.read_bytes()[44:]

async def test_progress_gives_the_seconds_already_sent(fake_ws, nemo, tmp_path):
    seen = []
    await transcribe(nemo, write_wav(tmp_path, 2.5), on_progress=seen.append)
    assert seen == [1.0, 2.0, 2.5]
```

```python
def words_of(*speakers):
    return [Word(f"w{i}", float(i), i + 0.5, speaker) for i, speaker in enumerate(speakers)]

def labels(words):
    return [word.speaker for word in label_speakers(words)]

def test_speakers_are_renamed_in_order_of_first_appearance():
    assert labels(words_of("3", "1", "3", "2")) == ["A", "B", "A", "C"]

def test_a_word_without_speaker_takes_the_speaker_of_the_word_before():
    assert labels(words_of("1", None, "2", None)) == ["A", "A", "B", "B"]

def test_leading_words_without_speaker_take_the_first_speaker():
    assert labels(words_of(None, None, "5")) == ["A", "A", "A"]

def test_words_without_any_speaker_all_go_to_a():
    assert labels(words_of(None, None)) == ["A", "A"]

def test_more_than_26_speakers_go_on_with_s27():
    assert labels(words_of(*map(str, range(27))))[-1] == "S27"
```

```python
def test_diarized_json_takes_its_speakers_from_the_realtime_session(ask, fake_nemo):
    body = ask(response_format="diarized_json").json()
    assert [segment["speaker"] for segment in body["segments"]] == ["A", "B"]
    assert fake_nemo.calls[0]["speakers"] is True

def test_prompt_is_accepted_and_never_reaches_nemo(ask, fake_nemo):
    assert ask(prompt="Sortformer").status_code == 200
    assert "prompt" not in fake_nemo.calls[0]

def test_audio_limit_is_two_hours():
    assert MAX_AUDIO_SECONDS == 7200.0
```

`test_each_upload_lives_alone_in_a_directory_removed_afterwards` vérifie aussi que le WAV décodé a disparu après la réponse, en continu comme sans `stream`. Les tests qui citaient `prompt`, `diarize`, `TURNS` ou la limite de 59 minutes suivent le nouveau comportement.

- [ ] **Étape 2 : vérifier l'échec**

Run : `make test ARGS="tests/test_nemo.py tests/test_speakers.py tests/test_transcriptions.py"`
Attendu : échecs sur `label_speakers` introuvable, sur l'argument `speakers` et sur la limite de durée.

- [ ] **Étape 3 : écrire les changements**

Dans `transcribe.py`, l'envoi est décodé par `decode_wav16k` dans un `tempfile.TemporaryDirectory(prefix="dictee-")` qui vit jusqu'à la fin de la reconnaissance, quelle qu'en soit l'issue ; pour un flux, il se supprime dans le rappel de fin de la tâche de reconnaissance. En mode réunion, `recognize` demande `speakers=True` et `finish` applique `label_speakers` puis `build_segments`.

- [ ] **Étape 4 : vérifier le succès**

Run : `make test`
Attendu : toute la suite passe.

- [ ] **Étape 5 : commiter**

```bash
git add dictee/src/dictee dictee/tests
git commit -m "Lit l'audio sur le disque, prend les personnes de la session temps réel et ne transmet plus prompt"
```

### Tâche 7 : morceaux sur la route REST de NeMo

**Fichiers :**
- Modifier : `dictee/src/dictee/nemo.py`, `dictee/tests/fakes.py`
- Tester : `dictee/tests/test_nemo.py`

**Interfaces :**
- Consomme : `rest_transcription.json` et `CHUNK_TIMEOUT` (tâche 1).
- Produit :
  - `NemoClient.transcribe_chunk(self, wav: bytes, *, language: str, phrases: Sequence[str], boost: float) -> Final` : `POST /v1/audio/transcriptions` en multipart, avec `file` (`audio.wav`, `audio/wav`), `language`, `response_format=verbose_json`, `timestamp_granularities[]=word` et, seulement s'il y a des termes, `speech_contexts` en JSON `[{"phrases": [...], "boost": boost}]` ; délai `CHUNK_TIMEOUT` ; erreurs de `_request`. Le `Final` porte le texte de la réponse sans espaces autour et ses mots (`word`, `start`, `end`).
  - `NemoClient.ready(self) -> bool` : `GET /ready` avec `READY_TIMEOUT = 5.0` ; vrai seulement pour une réponse 200 dont `ready` vaut `true` ; toute autre réponse et toute panne de transport donnent faux.
  - `FakeNemo.transcribe_chunk(wav, *, language, phrases, boost)` note `{"chunk": True, "language", "phrases", "boost", "seconds"}` et rend tour à tour les éléments de `chunks` (un `Final` est rendu, une exception est levée, le dernier élément se répète). Par défaut, `chunks` contient un seul `Final("Bonjour à tous. Salut.", …)` avec les quatre mots de `FINALS`, sans personne. `release` retient aussi le premier appel de `transcribe_chunk`. `FakeNemo.ready()` note `{"ready": True}` et rend tour à tour les éléments de `ready_answers` (par défaut `[True]`, le dernier se répète).

- [ ] **Étape 1 : écrire les tests qui échouent**

`parse_form(request)` relit le formulaire multipart d'une requête notée par un `Recorder`, et `REST_RECORDED` vient du relevé de la tâche 1. `ok_response` sert ce relevé pour `/v1/audio/transcriptions` et `{"ready": true}` pour `/ready`.

```python
async def test_a_chunk_goes_as_a_multipart_form_for_word_timestamps(http_nemo):
    recorder = Recorder()
    await http_nemo(recorder).transcribe_chunk(WAV_1S, language="fr-FR", phrases=[], boost=1.0)
    [request] = recorder.requests
    form = parse_form(request)
    assert request.url.path == "/v1/audio/transcriptions"
    assert (form["language"], form["response_format"]) == (["fr-FR"], ["verbose_json"])
    assert form["timestamp_granularities[]"] == ["word"] and form["file"] == [WAV_1S]
    assert "speech_contexts" not in form and "prompt" not in form

async def test_a_chunk_carries_the_glossary_as_speech_contexts(http_nemo):
    recorder = Recorder()
    await http_nemo(recorder).transcribe_chunk(WAV_1S, language="fr-FR", phrases=["Nemotron"], boost=1.0)
    assert json.loads(parse_form(recorder.requests[0])["speech_contexts"][0]) == [{"phrases": ["Nemotron"], "boost": 1.0}]

async def test_a_chunk_answer_gives_the_text_and_the_timed_words(http_nemo):
    final = await http_nemo(Recorder()).transcribe_chunk(WAV_1S, language="fr-FR", phrases=[], boost=1.0)
    assert final.text == REST_RECORDED["text"].strip()
    assert [(w.text, w.start, w.end) for w in final.words] == [(w["word"], w["start"], w["end"]) for w in REST_RECORDED["words"]]

@pytest.mark.parametrize(("status", "body", "expected"), [(200, {"ready": True}, True), (200, {"ready": False}, False), (503, {"ready": False}, False)])
async def test_ready_is_true_only_when_nemo_says_so(http_nemo, status, body, expected):
    assert await http_nemo(Recorder(httpx.Response(status, json=body))).ready() is expected

async def test_ready_is_false_when_nemo_is_unreachable():
    assert await NemoClient("http://127.0.0.1:9", "key", httpx.AsyncClient()).ready() is False
```

`CALLS` perd `diarize` (tâche 6) et gagne `transcribe_chunk` : les tests paramétrés existants sur les erreurs HTTP (429, connexion refusée, délai dépassé, autre statut) et sur les délais propres à chaque appel le couvrent ainsi.

- [ ] **Étape 2 : vérifier l'échec**

Run : `make test ARGS="tests/test_nemo.py"`
Attendu : échec sur `AttributeError: 'NemoClient' object has no attribute 'transcribe_chunk'`.

- [ ] **Étape 3 : écrire `transcribe_chunk`, `ready` et les ajouts du faux NeMo**

Le corps multipart est construit par `httpx` (`files=` et `data=`) : le morceau ne dépasse pas 40 s, soit 1,3 Mo.

- [ ] **Étape 4 : vérifier le succès**

Run : `make test`
Attendu : toute la suite passe.

- [ ] **Étape 5 : commiter**

```bash
git add dictee/src/dictee/nemo.py dictee/tests/fakes.py dictee/tests/test_nemo.py
git commit -m "Transcrit un morceau par la route REST de NeMo et lit sa disponibilité"
```

### Tâche 8 : traitement d'une tâche

**Fichiers :**
- Créer : `dictee/src/dictee/processing.py`
- Modifier : `dictee/tests/fakes.py` (`transcribe_errors`)
- Tester : `dictee/tests/test_processing.py`

**Interfaces :**
- Consomme : `read_wav_slice` (tâche 3) ; `detect_silences`, `plan_cuts`, `chunk_bounds`, `MAX_CHUNK_S` (tâche 4) ; `TaskStore`, `TaskRecord`, `TaskResult`, `ChunkRecord` (tâche 5) ; `NemoClient.transcribe`, `label_speakers`, `build_segments` (tâche 6) ; `transcribe_chunk`, `ready` (tâche 7) ; `clean`, `Profile`, `glossary_terms`.
- Produit :
  - `@dataclass(frozen=True) class Services` : `store: TaskStore`, `nemo: NemoClient`, `settings: Settings`, `http: httpx.AsyncClient`.
  - `@dataclass class RunStats` : `chunks: int | None = None`, `retries: int = 0`, `nemo_ms: int = 0`, `llm_ms: int | None = None`.
  - `def task_dir(settings: Settings, task_id: str) -> Path` (`settings.tasks_dir / task_id`) et `def task_audio(settings: Settings, task_id: str) -> Path` (`task_dir(…) / "audio.wav"`).
  - `async def process(services: Services, task: TaskRecord, stats: RunStats) -> TaskResult` et `async def wait_ready(nemo: NemoClient) -> None`.
  - `MAX_RETRIES = 3`, `RETRY_DELAYS_S = (2.0, 5.0, 10.0)`, `READY_POLL_S = 2.0`, `READY_WAIT_S = 300.0`, `PROGRESS_EVERY_S = 5.0`.
  - `FakeNemo.transcribe_errors: list[Exception | None]`, consommée d'un élément par appel de `transcribe`.

Les règles que les signatures ne fixent pas :

1. Le mode `DIARIZE` passe par la réunion ; les autres passent par les morceaux, et `CLEAN` reformule ensuite.
2. Morceaux. Les coupes sont celles de la tâche ; sinon `[]` pour un audio de `MAX_CHUNK_S` ou moins, ou `plan_cuts(duration, detect_silences(...))`, enregistrées par `set_cuts`. Les morceaux déjà en base ne repartent pas. Chaque morceau manquant passe par `read_wav_slice` puis `transcribe_chunk` (langue, termes du glossaire et force de la tâche), sous les reprises ; ses mots sont décalés du début du morceau, puis `save_chunk` et `set_progress(faits / total)`. Le texte joint par une espace les textes non vides, sans espaces autour ; les mots suivent l'ordre des rangs ; `segments` vaut `None` ; `stats.chunks` reçoit le nombre de morceaux.
3. Reformulation : `clean(text, profile=Profile(params.glossary, params.instructions or "", ""), settings=…, http=…)` ; `cleaned` et `decision` vont dans le résultat, la durée dans `stats.llm_ms`.
4. Réunion : une session `transcribe(task_audio, speakers=True, on_progress=…)` sous les reprises, qui recommence la session entière ; l'avancement vaut secondes envoyées sur durée, écrit au plus toutes les `PROGRESS_EVERY_S` ; le texte joint les phrases finales non vides ; `label_speakers` puis `build_segments` ; sans aucun mot, `segments` vaut `()`. Une réunion reprise repart toujours du début du fichier.
5. Reprises : `NemoUnavailable` et `NemoOverloaded` mènent à `wait_ready`, puis à une pause de `RETRY_DELAYS_S[n]`, puis à un nouvel essai, `MAX_RETRIES` fois au plus par morceau ou par session ; `stats.retries` les compte. `NemoError` remonte tout de suite. `wait_ready` interroge `ready()` toutes les `READY_POLL_S` secondes et lève `NemoUnavailable` après `READY_WAIT_S`.
6. `stats.nemo_ms` additionne le temps passé dans les appels à NeMo.

- [ ] **Étape 1 : écrire les tests qui échouent**

`services` assemble un `TaskStore` sur la base de test, le `FakeNemo`, les réglages et le client du faux LiteLLM. `audio_task(services, seconds, mode=Mode.RAW)` écrit un WAV canonique sans silence à `task_audio(...)` et crée la tâche avec `TaskParams("fr-FR", mode, (GlossaryEntry("Nemotron"),), 1.0, "Corrige." if mode is Mode.CLEAN else None)`. La fixture `no_retry_delays`, rangée dans `conftest.py` pour servir aussi en tâche 11, met `RETRY_DELAYS_S` à `(0, 0, 0)` et `READY_POLL_S` à 0 ; elle s'applique d'office à tout `test_processing.py`.

```python
async def test_a_chunked_task_sends_each_chunk_once_and_joins_the_texts(services, fake_nemo):
    task = audio_task(services, 65.0)
    fake_nemo.chunks = [Final("Bonjour.", (Word("Bonjour.", 0.2, 0.8),)), Final(" Salut. ", (Word("Salut.", 1.0, 1.5),))]
    result = await process(services, task, RunStats())
    assert result.text == "Bonjour. Salut."
    assert [(word.text, word.start) for word in result.words] == [("Bonjour.", 0.2), ("Salut.", 31.0)]
    assert [call["seconds"] for call in fake_nemo.calls if "chunk" in call] == pytest.approx([30.0, 35.0])
    assert services.store.get(task.id).cuts == (30.0,)

async def test_empty_chunks_leave_no_extra_space(services, fake_nemo):
    fake_nemo.chunks = [Final("", ()), Final("Salut.", ())]
    assert (await process(services, audio_task(services, 65.0), RunStats())).text == "Salut."

async def test_a_resumed_task_sends_only_the_chunks_not_done(services, fake_nemo):
    task = audio_task(services, 65.0)
    services.store.set_cuts(task.id, [30.0])
    services.store.save_chunk(task.id, ChunkRecord(0, 0.0, 30.0, "Bonjour.", (Word("Bonjour.", 0.2, 0.8),)))
    fake_nemo.chunks = [Final("Salut.", (Word("Salut.", 1.0, 1.5),))]
    result = await process(services, services.store.get(task.id), RunStats())
    assert result.text == "Bonjour. Salut."
    assert [call["seconds"] for call in fake_nemo.calls if "chunk" in call] == pytest.approx([35.0])

async def test_progress_follows_the_chunks_done(services, fake_nemo):
    task, seen = audio_task(services, 65.0), []
    fake_nemo.on_chunk = lambda: seen.append(services.store.get(task.id).progress)
    await process(services, task, RunStats())
    assert seen == [0.0, 0.5] and services.store.get(task.id).progress == 1.0

async def test_the_chunks_carry_the_glossary_and_the_boost_of_the_task(services, fake_nemo):
    await process(services, audio_task(services, 3.0), RunStats())
    [call] = [call for call in fake_nemo.calls if "chunk" in call]
    assert (call["phrases"], call["boost"], call["language"]) == (["Nemotron"], 1.0, "fr-FR")

async def test_a_clean_task_cleans_the_assembled_text(services, fake_llm):
    result = await process(services, audio_task(services, 3.0, mode=Mode.CLEAN), RunStats())
    assert (result.text, result.cleaned) == ("Bonjour à tous. Salut.", fake_llm.reply)
    assert result.transcript().text == fake_llm.reply

async def test_a_meeting_labels_its_speakers_and_builds_its_segments(services, fake_nemo):
    result = await process(services, audio_task(services, 3.0, mode=Mode.DIARIZE), RunStats())
    assert [segment.speaker for segment in result.segments] == ["A", "B"]
    assert fake_nemo.calls[0]["speakers"] is True and fake_nemo.calls[0]["seconds"] == pytest.approx(3.0)

async def test_a_meeting_starts_its_whole_session_again_after_a_lost_connection(services, fake_nemo):
    fake_nemo.transcribe_errors = [NemoUnavailable("lost"), None]
    stats = RunStats()
    await process(services, audio_task(services, 3.0, mode=Mode.DIARIZE), stats)
    assert len([call for call in fake_nemo.calls if "speakers" in call]) == 2 and stats.retries == 1

async def test_an_unavailable_nemo_is_tried_again_once_ready(services, fake_nemo):
    fake_nemo.chunks = [NemoUnavailable("down"), Final("Bonjour.", ())]
    fake_nemo.ready_answers = [False, True]
    stats = RunStats()
    assert (await process(services, audio_task(services, 3.0), stats)).text == "Bonjour."
    assert stats.retries == 1 and len([call for call in fake_nemo.calls if "ready" in call]) == 2

async def test_after_three_retries_the_failure_goes_up(services, fake_nemo):
    fake_nemo.chunks = [NemoOverloaded("full")]
    with pytest.raises(NemoOverloaded):
        await process(services, audio_task(services, 3.0), RunStats())
    assert len([call for call in fake_nemo.calls if "chunk" in call]) == 4

async def test_an_error_on_the_request_itself_is_not_tried_again(services, fake_nemo):
    fake_nemo.chunks = [NemoError("bad audio")]
    with pytest.raises(NemoError):
        await process(services, audio_task(services, 3.0), RunStats())
    assert len([call for call in fake_nemo.calls if "chunk" in call]) == 1

async def test_waiting_for_nemo_gives_up_after_its_delay(services, fake_nemo, monkeypatch):
    monkeypatch.setattr(processing, "READY_WAIT_S", 0.05)
    fake_nemo.chunks, fake_nemo.ready_answers = [NemoUnavailable("down")], [False]
    with pytest.raises(NemoUnavailable):
        await process(services, audio_task(services, 3.0), RunStats())
```

`FakeNemo.on_chunk`, s'il est donné, est appelé au début de chaque `transcribe_chunk`.

- [ ] **Étape 2 : vérifier l'échec**

Run : `make test ARGS="tests/test_processing.py"`
Attendu : échec sur `ModuleNotFoundError: dictee.processing`.

- [ ] **Étape 3 : écrire `processing.py`**

- [ ] **Étape 4 : vérifier le succès**

Run : `make test`
Attendu : toute la suite passe.

- [ ] **Étape 5 : commiter**

```bash
git add dictee/src/dictee/processing.py dictee/tests/conftest.py dictee/tests/fakes.py dictee/tests/test_processing.py
git commit -m "Traite une tâche par morceaux ou en réunion, avec reprises auprès de NeMo"
```

### Tâche 9 : file des tâches longues et boucle de travail

**Fichiers :**
- Créer : `dictee/src/dictee/runner.py`
- Modifier : `dictee/src/dictee/app.py`, `dictee/src/dictee/transcribe.py` (importe `long_audio_busy` de `runner`)
- Tester : `dictee/tests/test_runner.py`, `dictee/tests/test_app.py`

**Interfaces :**
- Consomme : `TaskStore`, `TaskRecord`, `TaskStatus` (tâche 5) ; `Services`, `RunStats`, `process`, `task_dir` (tâche 8) ; `spawn`, `cancel_once` (`lifecycle.py`) ; `unexpected_error` (`errors.py`).
- Produit :
  - `MAX_TASK_STARTS = 3`, `CLEANUP_EVERY_S = 600.0`, `ORPHAN_AGE_S = 3600.0`.
  - `def long_audio_busy() -> ApiError`, déplacée de `transcribe.py` sans changement.
  - `class LongLane(max_waiting: int)` : propriété `full -> bool` (en cours, en attente et réservées atteignent `1 + max_waiting`) ; `reserve(*, force: bool = False) -> LongTicket` lève `long_audio_busy()` quand la file est pleine, sauf avec `force`.
  - `class LongTicket` : `release() -> None` (sans effet la seconde fois) ; `turn() -> AbstractAsyncContextManager[None]` attend son tour dans l'ordre d'arrivée et rend la place à toute sortie, annulation pendant l'attente comprise.
  - `def failure_of(error: BaseException) -> tuple[str, str]` : `NemoOverloaded` donne `("overloaded", "The speech engine is busy, try again shortly.")` ; `NemoUnavailable` et `NemoError` donnent `("upstream_error", "The speech engine did not answer correctly.")` ; toute autre exception donne `("server_error", "The server failed to process the request.")` et passe par `unexpected_error`, qui ne journalise que le nom de sa classe.
  - `class TaskRunner(services: Services, lane: LongLane, *, process=processing.process, clock=time.time)` : `async start()`, `async stop()`, `is_long(duration: float) -> bool` (plus de `settings.long_audio_seconds`), `submit(task: TaskRecord, ticket: LongTicket | None) -> None`, `async wait(task_id: str) -> TaskRecord`, `async cancel(task_id: str) -> TaskRecord | None`, `async cleanup() -> None`.
  - `def log_task(task: TaskRecord, stats: RunStats) -> None`.
  - `create_app` range `app.state.store` (`TaskStore`) et `app.state.lane` (`LongLane(settings.max_queued_long)`). Le démarrage de l'app crée `settings.tasks_dir`, puis `app.state.runner` sur le client NeMo et le client HTTP, et appelle `start()` ; l'arrêt appelle `stop()`.

Les règles que les signatures ne fixent pas :

1. `submit` lance une tâche courte tout de suite. Une tâche longue attend le tour de son ticket, réservé par l'appelant ou, s'il n'y en a pas (reprise), par le runner avec `force=True`.
2. Un passage relit la tâche et s'arrête si elle est finie ; sinon `store.start`, `process`, puis `complete` ou `fail` (avec `failure_of`) et la durée de conservation `task_retention_hours * 3600`. À chaque fin, sauf l'arrêt de dictee, il efface `task_dir`, écrit la ligne de la tâche et réveille `wait`.
3. `stop` annule les passages et la boucle de ménage. Un passage annulé par l'arrêt laisse sa tâche telle qu'elle est en base (`queued` ou `in_progress`) et son audio en place, pour la reprise. Un passage annulé dont la tâche est `cancelled` en base est une annulation demandée : il efface l'audio et écrit sa ligne.
4. `start` parcourt `store.unfinished()` dans l'ordre de création : une tâche `in_progress` dont `attempts` atteint `MAX_TASK_STARTS` passe en `failed`, code `interrupted`, message `The transcription was interrupted several times.`, et son audio est effacé ; les autres repartent par `submit`. La boucle de ménage tourne ensuite toutes les `CLEANUP_EVERY_S`, en commençant tout de suite.
5. `cleanup` appelle `delete_expired`, puis efface dans `settings.tasks_dir` chaque dossier dont le nom n'est pas un identifiant connu et dont la date de modification a plus de `ORPHAN_AGE_S`.
6. `cancel` passe la tâche en `cancelled` (sans effet sur une tâche finie) et annule son passage s'il tourne ; une tâche en attente d'un tour rend sa place tout de suite.
7. La ligne d'une tâche, au format exact `task=%s user=%s mode=%s audio_s=%.1f chunks=%s starts=%d retries=%d nemo_ms=%d llm_ms=%s status=%s error=%s`, donne `default` pour le compte par défaut et `-` pour un nombre de morceaux, une durée de reformulation ou un code d'erreur absents.

- [ ] **Étape 1 : écrire les tests qui échouent**

`HeldProcess` remplace `process` : il note dans `started` l'ordre des tâches qu'il reçoit, attend `release(task_id)` pour chacune (ou rien si `held.auto` est vrai), puis rend `RESULT` ou lève `held.error`. `runner_for(services, lane, held, start=True)` construit le runner, appelle `start()` si demandé et l'arrête à la fin du test. `long_task(services, now=NOW)` et `short_task(services)` créent une tâche de 900 s ou de 3 s avec son dossier et son WAV. `enter(ticket)` entre dans `ticket.turn()` et y attend sans fin. `make_folder(services, name, age_s)` crée un dossier dans `settings.tasks_dir` et recule sa date de modification de `age_s` secondes, comme `age_folder(path, age_s)` pour un dossier existant.

```python
def test_the_lane_takes_one_running_and_three_waiting_long_tasks():
    lane = LongLane(3)
    tickets = [lane.reserve() for _ in range(4)]
    with pytest.raises(ApiError) as refused:
        lane.reserve()
    assert (refused.value.status, refused.value.code) == (429, "long_audio_busy")
    tickets[0].release()
    tickets[0].release()
    lane.reserve()
    assert lane.full

async def test_a_turn_gives_its_place_back_on_an_error_or_a_cancellation():
    lane = LongLane(1)
    with pytest.raises(RuntimeError):
        async with lane.reserve().turn():
            raise RuntimeError("boom")
    holder = lane.reserve()
    async with holder.turn():
        waiter = asyncio.create_task(enter(lane.reserve()))
        await wait_until(lambda: lane.full)
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
        assert not lane.full

async def test_a_short_task_runs_while_a_long_one_runs(services, held):
    runner = await runner_for(services, LongLane(3), held)
    runner.submit(long_task(services), None)
    short = short_task(services)
    runner.submit(short, None)
    await wait_until(lambda: short.id in held.started)

async def test_long_tasks_run_one_at_a_time_in_order_of_arrival(services, held):
    runner = await runner_for(services, LongLane(3), held)
    first, second = long_task(services), long_task(services)
    runner.submit(first, None)
    runner.submit(second, None)
    await wait_until(lambda: held.started == [first.id])
    held.release(first.id)
    await wait_until(lambda: held.started == [first.id, second.id])

async def test_wait_gives_the_finished_task_even_when_it_finished_before(services, held):
    runner = await runner_for(services, LongLane(3), held)
    task = short_task(services)
    runner.submit(task, None)
    held.release(task.id)
    assert (await runner.wait(task.id)).status is TaskStatus.COMPLETED
    assert (await runner.wait(task.id)).status is TaskStatus.COMPLETED

async def test_cancel_stops_a_running_task_removes_its_audio_and_can_be_repeated(services, held):
    runner = await runner_for(services, LongLane(3), held)
    task = short_task(services)
    runner.submit(task, None)
    await wait_until(lambda: task.id in held.started)
    cancelled = await runner.cancel(task.id)
    assert cancelled.status is TaskStatus.CANCELLED
    await wait_until(lambda: not task_dir(services.settings, task.id).exists())
    assert await runner.cancel(task.id) == services.store.get(task.id)

async def test_cancelling_a_queued_long_task_frees_its_place_and_it_never_runs(services, held):
    lane = LongLane(1)
    runner = await runner_for(services, lane, held)
    running, queued = long_task(services), long_task(services)
    runner.submit(running, lane.reserve())
    runner.submit(queued, lane.reserve())
    assert lane.full
    await runner.cancel(queued.id)
    await wait_until(lambda: not lane.full)
    held.release(running.id)
    await runner.wait(running.id)
    assert held.started == [running.id]

async def test_cancel_leaves_a_finished_task_as_it_is(services, held):
    runner = await runner_for(services, LongLane(3), held)
    done = services.store.complete(short_task(services).id, RESULT, NOW, 86_400)
    assert await runner.cancel(done.id) == done

async def test_unfinished_tasks_resume_at_startup_in_creation_order(services, held):
    first, second = long_task(services, now=NOW), long_task(services, now=NOW + 1)
    services.store.start(first.id, NOW)
    await runner_for(services, LongLane(3), held)
    await wait_until(lambda: held.started == [first.id])

async def test_a_task_started_three_times_fails_as_interrupted_at_startup(services, held):
    task = long_task(services)
    for _ in range(3):
        services.store.start(task.id, NOW)
    await runner_for(services, LongLane(3), held)
    failed = services.store.get(task.id)
    assert (failed.status, failed.error_code) == (TaskStatus.FAILED, "interrupted")
    assert held.started == [] and not task_dir(services.settings, task.id).exists()

async def test_stopping_leaves_a_running_task_in_progress_with_its_audio(services, held):
    runner = await runner_for(services, LongLane(3), held)
    task = short_task(services)
    runner.submit(task, None)
    await wait_until(lambda: task.id in held.started)
    await runner.stop()
    assert services.store.get(task.id).status is TaskStatus.IN_PROGRESS
    assert task_audio(services.settings, task.id).exists()

@pytest.mark.parametrize(("error", "code"), [(None, None), (NemoOverloaded("full"), "overloaded"), (NemoError("bad"), "upstream_error"), (ValueError("secret text"), "server_error")])
async def test_each_end_records_its_code_and_removes_the_audio(services, held, error, code):
    held.auto, held.error = True, error
    runner = await runner_for(services, LongLane(3), held)
    task = short_task(services)
    runner.submit(task, None)
    ended = await runner.wait(task.id)
    assert ended.status is (TaskStatus.COMPLETED if error is None else TaskStatus.FAILED)
    assert ended.error_code == code and not task_dir(services.settings, task.id).exists()

async def test_cleanup_removes_expired_tasks_and_old_orphan_folders_only(services, held):
    expired, queued = short_task(services), long_task(services)
    services.store.complete(expired.id, RESULT, NOW - 100_000, 86_400)
    old_orphan = make_folder(services, "trn_orphan_old", age_s=7200)
    decoding = make_folder(services, "trn_being_decoded", age_s=60)
    age_folder(task_dir(services.settings, queued.id), 7200)
    runner = await runner_for(services, LongLane(3), held, start=False)
    await runner.cleanup()
    assert services.store.get(expired.id) is None
    assert not old_orphan.exists() and decoding.exists() and task_dir(services.settings, queued.id).exists()

def test_the_task_line_says_who_what_and_how_it_ended_without_any_text(caplog):
    log_task(COMPLETED_CLEAN_TASK, RunStats(chunks=2, retries=1, nemo_ms=1200, llm_ms=300))
    line = caplog.records[-1].getMessage()
    assert line == f"task={COMPLETED_CLEAN_TASK.id} user=1 mode=clean audio_s=65.0 chunks=2 starts=1 retries=1 nemo_ms=1200 llm_ms=300 status=completed error=-"
```

Dans `test_app.py`, `test_app_has_one_long_audio_gate_set_from_the_settings` laisse sa place à `test_app_starts_its_task_runner_and_creates_the_task_folder`, qui vérifie, dans le bloc `with TestClient(...)`, `isinstance(app.state.runner, TaskRunner)`, `app.state.lane` et l'existence de `settings.tasks_dir`.

- [ ] **Étape 2 : vérifier l'échec**

Run : `make test ARGS="tests/test_runner.py tests/test_app.py"`
Attendu : échec sur `ModuleNotFoundError: dictee.runner`.

- [ ] **Étape 3 : écrire `runner.py` et le branchement dans `app.py`**

`LongLane` reprend le principe de `LongAudioGate` : un verrou asyncio, servi dans l'ordre d'arrivée, et un compte des places pris sans attente entre le test et l'incrément. `LongAudioGate` reste en place pour les flux jusqu'à la tâche 11.

- [ ] **Étape 4 : vérifier le succès**

Run : `make test`
Attendu : toute la suite passe.

- [ ] **Étape 5 : commiter**

```bash
git add dictee/src/dictee/runner.py dictee/src/dictee/app.py dictee/src/dictee/transcribe.py dictee/tests/test_runner.py dictee/tests/test_app.py
git commit -m "Ajoute la file des tâches longues, la boucle de travail, la reprise et le ménage"
```

### Tâche 10 : lecture et annulation d'une tâche

**Fichiers :**
- Créer : `dictee/src/dictee/tasks_api.py`
- Modifier : `dictee/src/dictee/formats.py`, `dictee/src/dictee/app.py`, `dictee/tests/conftest.py`
- Tester : `dictee/tests/test_tasks_api.py`, `dictee/tests/test_formats.py`

**Interfaces :**
- Consomme : `TaskStore.get_visible`, `TaskRecord`, `TaskResult.transcript` (tâche 5) ; `TaskRunner.cancel` (tâche 9) ; `require_caller`.
- Produit :
  - `formats.render_body(transcript: Transcript, fmt: str, *, granularities: frozenset[str]) -> dict | str` : le corps de la réponse, un objet pour `json`, `verbose_json` et `diarized_json`, une chaîne pour `text`, `srt` et `vtt` ; `render` le construit puis l'enveloppe dans sa réponse, avec les mêmes types de contenu qu'aujourd'hui.
  - `tasks_api.task_object(task: TaskRecord) -> dict` : les champs de la section 6.3 de la spec, `object` à `audio.transcription.task`, `duration` et `progress` arrondis au millième, `output` rendu par `render_body` dans le `response_format` et les granularités de la création quand la tâche est `completed`, `error` à `{"code", "message"}` quand elle est `failed`.
  - `tasks_api.task_error(task: TaskRecord) -> ApiError` : `overloaded` donne 429 `rate_limit_error` ; `upstream_error` donne 502 `server_error` ; `server_error` et `interrupted` donnent 500 `server_error`, chacun avec son code et le message enregistré ; une tâche `cancelled` donne 409 `invalid_request_error`, code `task_cancelled`, message `The transcription task was cancelled.`.
  - `tasks_api.task_not_found(task_id: str) -> ApiError` : 404 `invalid_request_error`, code `not_found`, message `No transcription task found with id '<id>'.`.
  - Routes `GET /v1/audio/transcriptions/{task_id}` (`get_task`) et `POST /v1/audio/transcriptions/{task_id}/cancel` (`cancel_task`) : `require_caller` d'abord, puis `get_visible(task_id, caller.user_id, now)`, et 404 si la tâche n'est pas visible.

- [ ] **Étape 1 : écrire les tests qui échouent**

`personal_headers(db, email)`, rangée dans `conftest.py` pour les tâches 11 et 12, crée un compte et sa clé, puis rend les en-têtes et le numéro du compte. `app_store(client)` rend `client.app.state.store`. Les tâches sont créées directement dans ce registre, avec `make` de `taskdata.py`.

```python
@pytest.mark.parametrize("fmt", RESPONSE_FORMATS)
def test_the_body_is_the_one_of_the_rendered_response(fmt):
    response = render(TRANSCRIPT, fmt, granularities=frozenset({"word", "segment"}))
    body = render_body(TRANSCRIPT, fmt, granularities=frozenset({"word", "segment"}))
    assert body == (json.loads(response.body) if isinstance(body, dict) else response.body.decode())

def test_the_object_of_a_queued_task(store):
    task = make(store, model="dictee-diarize", duration=7180.4)
    assert task_object(task) == {
        "id": task.id, "object": "audio.transcription.task", "status": "queued", "model": "dictee-diarize",
        "created_at": NOW, "completed_at": None, "expires_at": None, "duration": 7180.4,
        "progress": 0.0, "error": None, "output": None,
    }

def test_a_finished_task_carries_its_output_in_the_format_of_its_creation(store):
    srt = store.complete(make(store, response_format="srt").id, RESULT, NOW, 86_400)
    assert task_object(srt)["output"].startswith("1\n00:00:00,000 --> ")
    verbose = store.complete(make(store, response_format="verbose_json", granularities=frozenset({"word"})).id, RESULT, NOW, 86_400)
    assert "words" in task_object(verbose)["output"]

def test_a_failed_task_carries_its_code_and_message(store):
    failed = store.fail(make(store).id, "overloaded", "The speech engine is busy, try again shortly.", NOW, 86_400)
    assert task_object(failed)["error"] == {"code": "overloaded", "message": "The speech engine is busy, try again shortly."}
    assert (task_error(failed).status, task_error(failed).code) == (429, "overloaded")

def test_the_owner_reads_its_task(transcriptions_client, db):
    headers, owner = personal_headers(db, "alice@example.test")
    task = make(app_store(transcriptions_client), owner=owner)
    assert transcriptions_client.get(f"/v1/audio/transcriptions/{task.id}", headers=headers).json()["id"] == task.id

def test_another_caller_gets_the_same_404_as_an_unknown_id(transcriptions_client, db):
    owner_headers, owner = personal_headers(db, "alice@example.test")
    other_headers, _ = personal_headers(db, "bob@example.test")
    task = make(app_store(transcriptions_client), owner=owner)
    hidden = transcriptions_client.get(f"/v1/audio/transcriptions/{task.id}", headers=other_headers)
    unknown = transcriptions_client.get("/v1/audio/transcriptions/trn_unknown", headers=owner_headers)
    assert (hidden.status_code, unknown.status_code) == (404, 404)
    assert hidden.json()["error"]["code"] == unknown.json()["error"]["code"] == "not_found"

def test_the_default_account_reads_its_own_task_with_the_service_key(transcriptions_client, service_headers):
    task = make(app_store(transcriptions_client), owner=None)
    assert transcriptions_client.get(f"/v1/audio/transcriptions/{task.id}", headers=service_headers).status_code == 200

def test_an_expired_task_is_404(transcriptions_client, service_headers):
    store = app_store(transcriptions_client)
    task = store.complete(make(store, owner=None).id, RESULT, NOW, 60)  # expirée depuis longtemps
    assert transcriptions_client.get(f"/v1/audio/transcriptions/{task.id}", headers=service_headers).status_code == 404

def test_cancel_gives_the_cancelled_task_and_can_be_repeated(transcriptions_client, service_headers):
    task = make(app_store(transcriptions_client), owner=None, now=int(time.time()))
    for _ in range(2):
        response = transcriptions_client.post(f"/v1/audio/transcriptions/{task.id}/cancel", headers=service_headers)
        assert (response.status_code, response.json()["status"]) == (200, "cancelled")

def test_cancel_leaves_a_finished_task_as_it_is(transcriptions_client, service_headers):
    store, now = app_store(transcriptions_client), int(time.time())
    done = store.complete(make(store, owner=None, now=now).id, RESULT, now, 86_400)
    response = transcriptions_client.post(f"/v1/audio/transcriptions/{done.id}/cancel", headers=service_headers)
    assert response.json() == task_object(done)

def test_the_task_routes_need_a_key(transcriptions_client):
    assert transcriptions_client.get("/v1/audio/transcriptions/trn_x").status_code == 401
    assert transcriptions_client.post("/v1/audio/transcriptions/trn_x/cancel").status_code == 401
```

- [ ] **Étape 2 : vérifier l'échec**

Run : `make test ARGS="tests/test_tasks_api.py tests/test_formats.py"`
Attendu : échec sur `ModuleNotFoundError: dictee.tasks_api` et `ImportError: render_body`.

- [ ] **Étape 3 : écrire `render_body`, `tasks_api.py` et les routes**

- [ ] **Étape 4 : vérifier le succès**

Run : `make test`
Attendu : toute la suite passe.

- [ ] **Étape 5 : commiter**

```bash
git add dictee/src/dictee/formats.py dictee/src/dictee/tasks_api.py dictee/src/dictee/app.py dictee/tests/conftest.py dictee/tests/test_formats.py dictee/tests/test_tasks_api.py
git commit -m "Ajoute la lecture et l'annulation d'une tâche de transcription"
```

### Tâche 11 : route de transcription sur les tâches

**Fichiers :**
- Modifier : `dictee/src/dictee/transcribe.py`, `dictee/src/dictee/upload.py`, `dictee/src/dictee/lifecycle.py`, `dictee/src/dictee/app.py`, `dictee/tests/fakes.py`
- Tester : `dictee/tests/test_transcriptions.py`, `dictee/tests/test_lifecycle.py`, `dictee/tests/test_app.py`

**Interfaces :**
- Consomme : `decode_wav16k` (tâche 3) ; `TaskParams`, `TaskStatus`, `new_task_id` (tâche 5) ; `task_dir`, `task_audio` (tâche 8) ; `LongLane`, `TaskRunner` (tâche 9) ; `task_object`, `task_error` (tâche 10).
- Produit :
  - `upload.Upload` (`fields: dict[str, list[str]]`, `has_file: bool`, `sha256: str`) ; `read_upload(...) -> Upload`, l'empreinte SHA-256 des octets de la partie `file` étant calculée pendant leur écriture.
  - `_Params` gagne `background: bool`. `stream` et `background` acceptent `true` ou `false` ; les deux à `true` donnent `ApiError(400, "stream and background cannot be used together.", param="background", code="unsupported_combination")`, levée dans `_parse_params`, donc aussi avant la lecture du fichier quand les champs le précèdent.
  - `Trace.task` (`-` par défaut), écrit à la fin de la ligne de la requête sous la forme `task=%s`.
  - `LongAudioGate`, `LongPlace`, `app.state.gate` et leurs tests disparaissent.

Le déroulé de la route, que les signatures ne fixent pas :

1. Sans `stream`, la route forme `TaskParams` : langue au format NeMo, mode, glossaire de l'appelant, `settings.glossary_boost`, et consignes de l'appelant pour `Mode.CLEAN` seulement. Elle cherche `store.find_identical(owner, upload.sha256, params.digest(), now)`. Une tâche trouvée est rejointe, sans rien décoder. Sinon, elle tire `new_task_id()`, crée `task_dir`, décode l'envoi dans `task_audio` avec `max_seconds=MAX_AUDIO_SECONDS`, `long_seconds=settings.long_audio_seconds` et un `on_long` qui réserve une place dans `app.state.lane`, crée la tâche puis appelle `runner.submit(task, ticket)`. Un refus ou une erreur de décodage efface le dossier et rend la place.
2. Avec `background=true`, la réponse est 200 avec `task_object` de la tâche telle qu'elle est en base.
3. Sans `background`, la route attend `run_unless_disconnected(request, runner.wait(task.id))`. Un client parti donne 499 et la tâche continue. Une tâche `completed` est rendue par `render(result.transcript(), params.response_format, granularities=params.granularities)`, donc dans le format de cette requête-ci ; sinon la route lève `task_error(task)`.
4. Avec `stream=true`, l'envoi est décodé dans un dossier temporaire, un audio long réserve une place dans la même file, la reconnaissance tourne dans `ticket.turn()`, et aucune tâche n'est créée.
5. La ligne de la requête porte `task=<id>` ; pour une tâche, `nemo_ms`, `llm_ms` et `decision` restent à `-`, car la ligne de la tâche les donne.

- [ ] **Étape 1 : écrire les tests qui échouent**

`tasks_of(client)` lit toutes les tâches de `app.state.store`, par date de création ; `release(client, fake_nemo)` pose l'événement `release` du faux NeMo depuis le fil du test, par le portail du `TestClient` ; `poll_task(client, headers, task_id)` relit la tâche par `GET` jusqu'à une fin, 5 s au plus ; `post_fields(client, headers, audio, fields, order)` place les champs avant ou après le fichier ; `app_running(app)` démarre et arrête l'app (lifespan) dans un test asynchrone ; `request_line(caplog)` rend la ligne de la requête ; `tone_wav(directory)` écrit un WAV de sinus de 3 s. `post`, `app_with`, `asgi_client` et `call_app` existent déjà dans `test_transcriptions.py`.

```python
def test_a_synchronous_request_leaves_a_finished_task(ask, transcriptions_client):
    assert ask().json() == {"text": "Bonjour à tous. Salut."}
    [task] = tasks_of(transcriptions_client)
    assert task.status is TaskStatus.COMPLETED

def test_background_gives_the_task_object_at_once_then_its_result(ask, transcriptions_client, fake_nemo, service_headers):
    fake_nemo.release = anyio.Event()
    body = ask(background="true").json()
    assert body["object"] == "audio.transcription.task" and body["status"] in ("queued", "in_progress")
    assert body["output"] is None
    release(transcriptions_client, fake_nemo)
    final = poll_task(transcriptions_client, service_headers, body["id"])
    assert (final["status"], final["output"]) == ("completed", {"text": "Bonjour à tous. Salut."})

@pytest.mark.parametrize("field_order", ["before_file", "after_file"])
def test_stream_with_background_is_refused_before_any_task(transcriptions_client, service_headers, tone_mp3, field_order):
    response = post_fields(transcriptions_client, service_headers, tone_mp3, {"stream": "true", "background": "true"}, field_order)
    assert (response.status_code, response.json()["error"]["code"]) == (400, "unsupported_combination")
    assert response.json()["error"]["param"] == "background" and tasks_of(transcriptions_client) == []

def test_background_accepts_only_true_or_false(ask):
    assert ask(background="yes").json()["error"]["param"] == "background"

def test_an_identical_upload_joins_the_running_task(ask, transcriptions_client, fake_nemo):
    fake_nemo.release = anyio.Event()
    first, second = ask(background="true").json(), ask(background="true").json()
    assert first["id"] == second["id"]
    release(transcriptions_client, fake_nemo)
    assert len([call for call in fake_nemo.calls if "chunk" in call]) == 1

def test_an_identical_upload_gets_the_finished_result_without_nemo(ask, fake_nemo):
    ask()
    calls = len(fake_nemo.calls)
    assert ask().json() == {"text": "Bonjour à tous. Salut."}
    assert len(fake_nemo.calls) == calls

def test_an_identical_upload_after_a_failure_makes_a_new_task(ask, transcriptions_client, fake_nemo, no_retry_delays):
    fake_nemo.chunks = [NemoError("bad")]
    assert ask().status_code == 502
    fake_nemo.chunks = [Final("Bonjour.", ())]
    assert ask().json() == {"text": "Bonjour."}
    assert [task.status for task in tasks_of(transcriptions_client)] == [TaskStatus.FAILED, TaskStatus.COMPLETED]

def test_another_glossary_makes_another_task(transcriptions_client, db, tone_mp3):
    headers, owner = personal_headers(db, "alice@example.test")
    post(transcriptions_client, headers, tone_mp3, model="dictee")
    db.save_settings(owner, glossary="Sortformer")
    post(transcriptions_client, headers, tone_mp3, model="dictee")
    assert len(tasks_of(transcriptions_client)) == 2

def test_each_request_gets_its_own_format_from_the_same_task(ask, transcriptions_client):
    assert ask(response_format="json").json() == {"text": "Bonjour à tous. Salut."}
    assert ask(response_format="srt").text.startswith("1\n00:00:00,000 --> ")
    assert len(tasks_of(transcriptions_client)) == 1

async def test_a_client_hanging_up_leaves_the_task_running_to_its_end(settings, fake_nemo, fake_llm, service_headers, tmp_path):
    fake_nemo.release = anyio.Event()
    app = app_with(settings, fake_nemo, fake_llm)
    async with app_running(app):
        status = await call_app(app, service_headers, tone_wav(tmp_path), hang_up_after_upload=True)
        assert status == 499
        [task] = app.state.store.unfinished()
        fake_nemo.release.set()
        assert (await app.state.runner.wait(task.id)).status is TaskStatus.COMPLETED

@pytest.mark.parametrize(("error", "status", "code"), [(NemoOverloaded("full"), 429, "overloaded"), (NemoUnavailable("down"), 502, "upstream_error"), (NemoError("bad"), 502, "upstream_error")])
def test_a_failed_task_gives_the_error_of_a_synchronous_request(ask, fake_nemo, no_retry_delays, error, status, code):
    fake_nemo.chunks = [error]
    response = ask()
    assert (response.status_code, response.json()["error"]["code"]) == (status, code)

def test_audio_over_two_hours_is_refused_before_any_task(ask, transcriptions_client, monkeypatch):
    monkeypatch.setattr(transcribe, "MAX_AUDIO_SECONDS", 1.0)
    response = ask()
    assert (response.status_code, response.json()["error"]["code"]) == (400, "audio_too_long")
    tasks_dir = transcriptions_client.app.state.settings.tasks_dir
    assert tasks_of(transcriptions_client) == [] and not any(tasks_dir.iterdir())

async def test_the_fifth_long_upload_is_429_while_decoding_and_leaves_nothing(settings, fake_nemo, fake_llm, service_headers, tmp_path):
    fake_nemo.release = anyio.Event()
    app = app_with(settings, fake_nemo, fake_llm, long_audio_seconds=1.0)
    async with app_running(app), asgi_client(app) as client:
        for index in range(4):
            response = await client.post("/v1/audio/transcriptions", headers=service_headers, files=upload(tmp_path, f"long{index}.wav", seconds=3.0), data={"model": "dictee", "background": "true"})
            assert response.status_code == 200
        refused = await client.post("/v1/audio/transcriptions", headers=service_headers, files=upload(tmp_path, "long4.wav", seconds=3.0), data={"model": "dictee", "background": "true"})
        assert (refused.status_code, refused.json()["error"]["code"]) == (429, "long_audio_busy")
        assert len(app.state.store.unfinished()) == 4 and len(list(settings.tasks_dir.iterdir())) == 4
        fake_nemo.release.set()

def test_a_stream_makes_no_task(ask, transcriptions_client):
    assert ask(stream="true").status_code == 200
    assert tasks_of(transcriptions_client) == []

def test_the_upload_digest_is_the_sha256_of_the_file_bytes(transcriptions_client, service_headers, tone_mp3):
    post(transcriptions_client, service_headers, tone_mp3, model="dictee")
    [task] = tasks_of(transcriptions_client)
    assert task.upload_sha256 == hashlib.sha256(tone_mp3).hexdigest()

def test_the_request_line_names_its_task(caplog, ask, transcriptions_client):
    ask()
    [task] = tasks_of(transcriptions_client)
    assert request_line(caplog).endswith(f"status=200 task={task.id}")
```

`personal_headers` vient de `conftest.py` (tâche 10), `upload(directory, name, seconds)` fabrique la partie `file` d'un WAV de sinus, et les quatre fichiers longs ont des contenus différents (fréquences différentes), pour ne pas se rejoindre. Les tests existants de formats, de flux, d'erreurs et de journaux, et ceux qui passent par le client Python d'OpenAI (`test_json_with_any_model_name`, `test_stream_through_the_sdk`, `test_diarized_json_through_the_sdk`), passent sans changement de leurs attentes ; ceux de la porte des audios longs disparaissent avec elle.

- [ ] **Étape 2 : vérifier l'échec**

Run : `make test ARGS="tests/test_transcriptions.py"`
Attendu : les nouveaux tests échouent (aucune tâche créée, champ `background` inconnu).

- [ ] **Étape 3 : écrire la route sur les tâches**

- [ ] **Étape 4 : vérifier le succès**

Run : `make test`
Attendu : toute la suite passe.

- [ ] **Étape 5 : commiter**

```bash
git add dictee/src/dictee dictee/tests
git commit -m "Fait passer les transcriptions de fichier par des tâches, en synchrone ou en arrière-plan"
```

### Tâche 12 : dictée en direct

**Fichiers :**
- Créer : `dictee/src/dictee/live.py`
- Modifier : `dictee/src/dictee/nemo.py`, `dictee/src/dictee/types.py`, `dictee/src/dictee/app.py`, `dictee/tests/fakes.py`, `dictee/tests/conftest.py`
- Tester : `dictee/tests/test_live.py`, `dictee/tests/test_nemo.py`

**Interfaces :**
- Consomme : `NEMO_MANY_COMMITS`, `SERVER_VAD`, `realtime_multiturn_events.jsonl` et `realtime_endpointing_events.jsonl` (tâche 1) ; `resolve_caller`, `load_profile`, `glossary_terms`, `to_nemo_language`, `to_iso_language`, `MODELS`.
- Produit :
  - `types.Committed` et `types.Cleared`, sans champ.
  - `NemoClient.live(self, *, sample_rate: int, language: str, phrases: Sequence[str], boost: float, endpointing_ms: int | None = None) -> AbstractAsyncContextManager[LiveSession]` : ouvre une session temps réel avec `sample_rate`, `language`, `speech_contexts` s'il y a des termes et `endpointing_ms` s'il est donné, sans `word_timestamps` ni `prompt`, et la ferme à la sortie.
  - `LiveSession` : `async send_audio(pcm: bytes)`, `async commit()`, `async clear()`, `events() -> AsyncIterator[Partial | Final | Committed | Cleared]`, avec les mêmes exceptions que `transcribe`.
  - `live.realtime(websocket)`, servie par `WebSocketRoute("/v1/realtime", realtime)` ; `MAX_AUDIO_S = 7200.0`, `IDLE_TIMEOUT_S = 1800.0`, `DEFAULT_RATE = 24000`, `MIN_RATE = 8000`, `MAX_RATE = 96000`, `DEFAULT_SILENCE_MS = 500`, et `NEMO_MANY_COMMITS`, `SERVER_VAD` aux valeurs de la tâche 1.
  - `FakeNemo.live(...)` rend une `FakeLiveSession`, ajoutée à `fake_nemo.live_sessions`, qui note ses options dans `options` (un dict des arguments de `live`), l'audio reçu, les commits, les effacements et `closed`. `fake_nemo.live_turns` donne une liste de prises de parole, chacune une liste de `Partial` et de `Final` : les `Partial` d'une prise de parole partent à son premier `send_audio`, son `Final` puis un `Committed` partent à son commit. `fake_nemo.live_error`, s'il est donné, est levée par `events()`.
  - Le faux serveur `FakeNemoRealtime` de `test_nemo.py` gagne `after_commits: list[list[dict]]`, consommée d'une liste par commit (à défaut, `after_commit`), et répond `input_audio_buffer.cleared` à un `clear`.
  - Une fixture `live_server` lance l'app (faux NeMo) dans un `uvicorn.Server` sur un port libre de `127.0.0.1`, dans un fil, et rend son URL de base.

Les règles que les signatures ne fixent pas :

1. **Ouverture.** dictee identifie l'appelant par `resolve_caller` sur les en-têtes de la connexion, puis l'accepte. Une clé refusée donne une `error` (`invalid_request_error`, `invalid_api_key`) puis la fermeture 1008. Un `intent` autre que `transcription` donne le code `unsupported_intent` ; un paramètre `model` qui n'est pas dans `MODELS` et ne contient pas `transcribe` donne `unsupported_model` ; les deux ferment en 1008. Sinon, dictee envoie `session.created` avec la session par défaut.
2. **Session.** `session.update` lit les champs de la section 6.5 de la spec et répond `session.updated` avec la session en vigueur. Un format autre que `audio/pcm`, un `rate` hors de 8000 à 96000 ou une langue inconnue donnent une `error` dont `param` désigne le champ (par exemple `session.audio.input.format.rate`), et la session en vigueur ne change pas. Un `session.update` reçu pendant qu'une session NeMo est ouverte donne le code `session_already_started`.
3. **Prises de parole.** La session NeMo s'ouvre au premier `append` d'une prise de parole, avec les réglages en vigueur, le glossaire de l'appelant suivi des `keywords` absents du glossaire, et `settings.glossary_boost`. L'`item_id` (`item_1`, `item_2`…) naît au même moment ; les textes partiels le portent ; `input_audio_buffer.committed` l'annonce avec `previous_item_id` (l'item validé avant lui, ou `null`). Si `NEMO_MANY_COMMITS` est vrai, une seule session NeMo sert toute la connexion ; sinon, la session NeMo se ferme après son `Committed` et l'`append` suivant en ouvre une nouvelle.
4. **`turn_detection`.** `semantic_vad` est toujours refusé (code `unsupported_turn_detection`, `param` `session.audio.input.turn_detection`). `server_vad` est refusé de la même façon quand `SERVER_VAD` est faux ; sinon, `endpointing_ms` vaut `silence_duration_ms` ou `DEFAULT_SILENCE_MS`, et un `Final` de NeMo qui arrive sans commit du client ferme l'item : `input_audio_buffer.committed`, puis `…completed`.
5. **Pannes et limites.** Une erreur de NeMo donne une `error` (`server_error`, `upstream_error`, ou `rate_limit_error`, `overloaded`) puis la fermeture 1011. Plus de `MAX_AUDIO_S` secondes d'audio reçu donnent le code `audio_too_long`, et `IDLE_TIMEOUT_S` sans événement du client le code `idle_timeout` ; les deux ferment en 1000.
6. **Fin.** À chaque fin, la session NeMo se ferme et une ligne s'écrit, au format exact `route=realtime user=%s audio_s=%.1f turns=%d status=%s`, où `status` vaut `closed`, `idle`, `limit` ou le code de l'erreur.

Les événements envoyés au client, au format du client Python d'OpenAI :

```json
{"type": "session.created", "event_id": "event_1", "session": {"id": "sess_…", "object": "realtime.transcription_session", "type": "transcription", "audio": {"input": {"format": {"type": "audio/pcm", "rate": 24000}, "transcription": {"model": "dictee", "language": "fr"}, "turn_detection": null}}}}
{"type": "input_audio_buffer.committed", "event_id": "event_4", "item_id": "item_1", "previous_item_id": null}
{"type": "input_audio_buffer.cleared", "event_id": "event_5"}
{"type": "conversation.item.input_audio_transcription.delta", "event_id": "event_3", "item_id": "item_1", "content_index": 0, "delta": "Bonjour"}
{"type": "conversation.item.input_audio_transcription.completed", "event_id": "event_6", "item_id": "item_1", "content_index": 0, "transcript": "Bonjour à tous.", "usage": {"type": "duration", "seconds": 1.4}}
{"type": "error", "event_id": "event_7", "error": {"type": "invalid_request_error", "code": "unsupported_turn_detection", "message": "…", "param": "session.audio.input.turn_detection", "event_id": null}}
```

`event_id` dans `error.event_id` reprend celui de l'événement du client qui a causé l'erreur, quand il en a un. `usage.seconds` donne la durée de l'audio de l'item.

- [ ] **Étape 1 : écrire les tests qui échouent**

`live_client` est un `TestClient` de l'app sur le faux NeMo, comme `transcriptions_client` ; `personal_headers` vient de `conftest.py` (tâche 10) ; `wait_for(condition)` attend qu'une condition devienne vraie, 2 s au plus. `open_live(client, headers, query="intent=transcription")` ouvre `client.websocket_connect(f"/v1/realtime?{query}", headers=headers)` ; `pcm_b64(seconds, rate)` encode du silence PCM16 en base64 ; `update(ws, **audio_input)` envoie un `session.update` de type `transcription` ; `until(ws, kind)` lit les événements jusqu'au type donné et les rend.

```python
def test_without_the_transcription_intent_an_error_comes_then_the_socket_closes(live_client, service_headers):
    with open_live(live_client, service_headers, query="model=gpt-realtime") as ws:
        assert ws.receive_json()["error"]["code"] == "unsupported_intent"
        assert ws.receive()["code"] == 1008

def test_a_conversation_model_is_refused(live_client, service_headers):
    with open_live(live_client, service_headers, query="intent=transcription&model=gpt-realtime") as ws:
        assert ws.receive_json()["error"]["code"] == "unsupported_model"

def test_an_unknown_key_gets_an_error_and_the_socket_closes(live_client):
    with open_live(live_client, {"Authorization": "Bearer nope"}) as ws:
        assert ws.receive_json()["error"]["code"] == "invalid_api_key"
        assert ws.receive()["code"] == 1008

def test_the_session_in_force_reaches_nemo(live_client, fake_nemo, db):
    headers, owner = personal_headers(db, "alice@example.test")
    db.save_settings(owner, glossary="Nemotron")
    with open_live(live_client, headers) as ws:
        until(ws, "session.created")
        update(ws, format={"type": "audio/pcm", "rate": 16000}, transcription={"languages": ["en"], "keywords": ["Kubernetes", "Nemotron"], "prompt": "x", "delay": "low"})
        updated = until(ws, "session.updated")[-1]["session"]["audio"]["input"]
        assert (updated["format"]["rate"], updated["transcription"]["language"]) == (16000, "en")
        ws.send_json({"type": "input_audio_buffer.append", "audio": pcm_b64(0.5, 16000)})
        ws.send_json({"type": "input_audio_buffer.commit"})
        until(ws, "conversation.item.input_audio_transcription.completed")
    options = fake_nemo.live_sessions[0].options
    assert (options["sample_rate"], options["language"]) == (16000, "en-US")
    assert options["phrases"] == ["Nemotron", "Kubernetes"] and "prompt" not in options

def test_appended_audio_reaches_nemo_decoded(...)  # les octets reçus par la session NeMo valent le PCM décodé
def test_a_commit_gives_committed_with_its_item_then_completed(...)  # item_1, previous_item_id null, transcript du Final, usage de type duration
def test_partials_carry_the_item_that_committed_announces_later(...)  # delta avant committed, même item_id
def test_each_turn_gets_its_own_item_and_links_to_the_previous_one(...)  # item_2, previous_item_id item_1
def test_clear_is_passed_on_and_acknowledged(...)  # cleared reçu, clear noté par la session NeMo
def test_semantic_vad_is_always_refused(...)
def test_server_vad_follows_what_nemo_can_do(...)  # selon SERVER_VAD : accepté, et un Final sans commit donne committed puis completed ; sinon refusé
def test_nemo_sessions_follow_the_turns_as_nemo_requires(...)  # selon NEMO_MANY_COMMITS : une session pour deux prises de parole, ou deux sessions
def test_a_nemo_error_becomes_an_error_event_and_closes(...)  # live_error = NemoOverloaded : code overloaded, fermeture 1011

def test_a_client_leaving_mid_turn_closes_the_nemo_session_and_writes_its_line(live_client, service_headers, fake_nemo, caplog):
    with open_live(live_client, service_headers) as ws:
        until(ws, "session.created")
        ws.send_json({"type": "input_audio_buffer.append", "audio": pcm_b64(0.5, 24000)})
        wait_for(lambda: fake_nemo.live_sessions)
    wait_for(lambda: fake_nemo.live_sessions[0].closed)
    assert "route=realtime user=default audio_s=0.5 turns=0 status=closed" in caplog.text

def test_an_idle_session_is_closed(...)  # IDLE_TIMEOUT_S à 0,05 : error idle_timeout puis fermeture 1000
def test_audio_over_the_limit_ends_the_session(...)  # MAX_AUDIO_S à 1 : error audio_too_long puis fermeture 1000
def test_the_session_line_never_carries_any_text(...)  # aucun texte du Final, du glossaire ni des keywords dans les journaux

def test_the_openai_sdk_runs_a_transcription_session(live_server, settings, fake_nemo):
    client = OpenAI(api_key=settings.openwebui_service_key, base_url=f"{live_server}/v1", max_retries=0)
    with client.realtime.connect(extra_query={"intent": "transcription"}) as connection:
        connection.send({"type": "session.update", "session": {"type": "transcription", "audio": {"input": {"format": {"type": "audio/pcm", "rate": 24000}, "turn_detection": None}}}})
        connection.input_audio_buffer.append(audio=pcm_b64(0.2, 24000))
        connection.input_audio_buffer.commit()
        events = []
        while not events or events[-1].type != "conversation.item.input_audio_transcription.completed":
            events.append(connection.recv())
    assert events[-1].transcript == "Bonjour à tous."
```

Chaque test noté d'un commentaire s'écrit avec les mêmes aides que ses voisins et assure exactement ce que dit son commentaire. Dans `test_nemo.py`, sur le faux serveur temps réel :

```python
async def test_a_live_session_sends_its_options_then_the_audio_as_binary_frames(fake_ws, nemo):
    async with nemo.live(sample_rate=24000, language="fr-FR", phrases=["Nemotron"], boost=1.0, endpointing_ms=500) as session:
        await session.send_audio(b"\x00\x01" * 100)
        await session.commit()
    assert fake_ws.session == {"sample_rate": 24000, "language": "fr-FR", "speech_contexts": [{"phrases": ["Nemotron"], "boost": 1.0}], "endpointing_ms": 500}
    assert fake_ws.frames == [b"\x00\x01" * 100]

async def test_live_events_follow_the_recorded_multiturn_session(fake_ws, nemo):
    fake_ws.after_commits = MULTITURN_AFTER_COMMITS  # les événements du relevé qui suivent chaque commit
    ...  # deux commits : events() rend, pour chacun, les Partial des deltas non vides, un Final, puis un Committed

async def test_leaving_a_live_session_closes_its_socket(fake_ws, nemo):
    ...  # sortir du bloc `async with nemo.live(...)` sans commit : le faux serveur voit la connexion fermée
```

- [ ] **Étape 2 : vérifier l'échec**

Run : `make test ARGS="tests/test_live.py tests/test_nemo.py"`
Attendu : échec sur `ModuleNotFoundError: dictee.live` et sur `NemoClient.live` introuvable.

- [ ] **Étape 3 : écrire `LiveSession`, `live.py`, le faux NeMo et la route**

`live.py` lit les messages du client et les événements de NeMo dans deux tâches, sous un seul `asyncio.TaskGroup` (ou équivalent) : la fin de l'une annule l'autre, et la session NeMo se ferme dans un `finally`.

- [ ] **Étape 4 : vérifier le succès**

Run : `make test`
Attendu : toute la suite passe.

- [ ] **Étape 5 : commiter**

```bash
git add dictee/src/dictee dictee/tests
git commit -m "Ajoute la dictée en direct au format du Realtime d'OpenAI en mode transcription"
```

### Tâche 13 : force du glossaire (vérification 12.6)

Cette mesure ne fixe qu'une valeur par défaut : elle passe après le code, juste avant la documentation, pour ne pas retenir les tâches précédentes pendant qu'on attend l'accord de Geoff.

**Fichiers :**
- Modifier : `integration/boost.py` (protocole de la section 7.6), `dictee/src/dictee/settings.py` (valeur par défaut de `glossary_boost`), `dictee/tests/test_settings.py`
- Modifier : ce plan, section « Résultats » de la tâche

**Interfaces :**
- Consomme : la route REST et la route temps réel de NeMo (tâche 1) ; `call.wav` et `note.wav` (tâche 1).
- Produit : la force retenue, valeur par défaut de `Settings.glossary_boost`, que la tâche 14 reporte dans le design du 31 octobre et la tâche 15 dans le `.env` de Dokploy si celui-ci la fixe.

- [ ] **Étape 1 : demander l'accord de Geoff pour lire son glossaire**

Le travail attend sa réponse. Avec son accord, un appel en lecture seule dans le conteneur dictee de production écrit les termes de son glossaire (`glossary_terms(parse_glossary(...))`), un par ligne, dans `.integration/verif-taches/glossaire.txt`, sans les afficher. Sans son accord, la mesure se fait avec « Nemotron », « Sortformer » et « Discord » seuls, et le compte rendu le dit.

- [ ] **Étape 2 : demander à Geoff un court enregistrement**

Geoff peut fournir un enregistrement où il prononce au moins cinq termes de son glossaire, avec la liste des termes dits. S'il n'en fournit pas à ce moment-là, des phrases de synthèse (Magpie, sur le NeMo jetable) portent chacune un terme du glossaire, et leur texte sert de référence.

- [ ] **Étape 3 : réécrire `boost.py`**

Options : `--glossary FICHIER`, `--audio WAV[:RÉFÉRENCE]` (répétable), `--forces 0.5,1,1.5,2`, `--engines rest,realtime`, `--out DOSSIER`, `--show-terms` et `--self-test`. Pour chaque audio et chaque moteur, le script fait une passe sans glossaire, puis une passe par force avec le glossaire augmenté de « Nemotron », « Sortformer » et « Discord ». Le moteur `rest` envoie des tranches nettes de 30 s à `/v1/audio/transcriptions` ; le moteur `realtime` ouvre une session par fichier. Les insertions d'un terme dans une passe valent `max(0, occurrences dans la passe - max(occurrences sans glossaire, occurrences dans la référence))`, sur les mots repliés par `fold`. Le script affiche un tableau des insertions par moteur et par force (des nombres seulement ; les termes avec `--show-terms`, pour Geoff), puis `force retenue : <x>` pour la plus haute force sans aucune insertion sur tout le corpus, ou `aucune force sans insertion : 0,5`. Les textes vont dans `--out`, sous `.integration/`.
`--self-test` vérifie le comptage sur trois cas écrits dans le script : un terme absent partout (0), un terme inséré deux fois (2), un terme dit une fois dans la référence et rendu deux fois (1).
Run : `uv run --script integration/boost.py --self-test`
Attendu : `self-test OK`.

- [ ] **Étape 4 : mesurer sur le VPS**

Comme en tâche 1 (`verif-nemo`, `verif-client`, `verif-net`), sur le Call, la Note et l'enregistrement de Geoff ou les phrases de synthèse.
Attendu : le tableau et la ligne de verdict. Si aucune force n'est sans insertion, la valeur est 0,5 et Geoff est prévenu.

- [ ] **Étape 5 : écrire le test de la valeur par défaut, le voir échouer, puis changer la valeur**

```python
def test_glossary_boost_defaults_to_the_measured_force():
    assert Settings.from_env(REQUIRED_ENV).glossary_boost == MEASURED_FORCE  # valeur de l'étape 4
```

Run : `make test ARGS="tests/test_settings.py"` (échec avec 2.0), puis changer la valeur par défaut dans `settings.py`, puis `make test` (succès).

- [ ] **Étape 6 : nettoyer, consigner et commiter**

Supprimer les conteneurs, le réseau et les fichiers copiés sur le VPS. « Résultats » ne reçoit que des nombres : aucun terme du glossaire, aucun texte transcrit.

```bash
git add integration/boost.py dictee/src/dictee/settings.py dictee/tests/test_settings.py docs/superpowers/plans/2026-11-01-taches-transcription.md
git commit -m "Mesure la force du glossaire sans insertion et en fait la valeur par défaut"
```

**Résultats** (4 novembre 2026, NeMo jetable sur le VPS, aucun texte ni aucun terme gardé dans ce plan) :
- Corpus utilisé : sans le glossaire de Geoff, car la base de production ne contient aucun compte ni aucun glossaire. Le glossaire de la mesure se réduit donc aux trois termes fixes. Geoff n'a pas fourni d'enregistrement non plus : le corpus réunit le Call (43,6 s) et la Note (385,9 s), sans référence, et trois phrases de synthèse Magpie de 3 à 4 s (voix Sofia, produites par un script jetable hors dépôt), une par terme fixe, dont le texte sert de référence. Chaque audio passe sur les deux moteurs (`rest` par tranches nettes de 30 s, `realtime` en une session), sans glossaire puis aux forces 0,5, 1, 1,5 et 2, soit 50 passes.
- Insertions (mots du glossaire en trop) par moteur et par force :

| Moteur | Force | Call | Note | Synthèse (3 phrases) | Total |
|---|---|---|---|---|---|
| `rest` | 0,5 | 0 | 0 | 0 | 0 |
| `rest` | 1 | 0 | 0 | 0 | 0 |
| `rest` | 1,5 | 0 | 1 | 0 | 1 |
| `rest` | 2 | 0 | 13 | 0 | 13 |
| `realtime` | 0,5 | 0 | 0 | 0 | 0 |
| `realtime` | 1 | 0 | 0 | 0 | 0 |
| `realtime` | 1,5 | 0 | 0 | 0 | 0 |
| `realtime` | 2 | 3 | 9 | 0 | 12 |

- Le glossaire agit sur ce corpus : le terme dit est rendu dans 2 phrases de synthèse sur 3 dès la force 0,5, et dans l'une des deux il ne l'est que grâce au glossaire. Les insertions n'apparaissent qu'à partir de 1,5. La force 2, ancienne valeur par défaut, en fait 25 au total (13 en `rest`, 12 en `realtime`), et la sortie temps réel du Call passe de 28 à 17 mots.
- Force retenue : 1. Les forces 0,5 et 1 ne font aucune insertion sur les deux moteurs, et 1 est la plus haute. Le repli à 0,5 n'a pas servi. La valeur par défaut de `glossary_boost` passe de 2.0 à 1.0.
- Le script accepte `--glossary FICHIER` pour rejouer la mesure avec les termes de Geoff quand il aura un glossaire.

### Tâche 14 : documentation

**Fichiers :**
- Modifier : `docs/superpowers/specs/2026-10-31-dictee-server-design.md`, `AGENTS.md`, `README.md`

- [ ] **Étape 1 : mettre à jour le design du 31 octobre**

Les sections 3 (la décision « synchrone, sans tâche asynchrone maison » cède la place aux tâches enregistrées), 4.5 (`MAX_QUEUED_LONG`, `TASK_RETENTION_HOURS`, valeur de `GLOSSARY_BOOST`), 4.7 (WAV sur le disque, une tâche longue en cours et trois en attente), 5.1 et 5.2 (routes des tâches, `background`, `prompt` ignoré, 2 heures pour tous les modes), 6.1 (morceaux sur la route REST), 6.3 (réunions sur la route temps réel avec personnes), 6.4 (flux lu sur le disque), 6.6 (lignes de journal d'une tâche et d'une session en direct) et 7.3 (schéma 2) disent ce qui est livré. Le passage qui disait que dictee ne garde ni l'audio ni le texte décrit la conservation de 24 heures. Une ligne en tête renvoie à la spec du 1er novembre pour l'histoire du changement.

- [ ] **Étape 2 : mettre à jour `AGENTS.md` et le `README.md`**

`AGENTS.md` nomme les deux specs sur sa ligne de spec. Le principe sur le stockage devient : dictee garde l'audio d'une tâche jusqu'à sa fin et son texte 24 heures après, et ses journaux n'en contiennent jamais. La liste de ce que dictee ajoute à NeMo remplace « attribution des mots aux locuteurs » par les tâches enregistrées, le découpage en morceaux et la traduction du Realtime d'OpenAI. Le `README.md` décrit les routes des tâches, `background`, la dictée en direct et les limites.

- [ ] **Étape 3 : chercher ce qui serait resté ancien**

Run : `grep -n "ne stocke ni\|90 minutes\|59 minutes\|/v1/audio/diarizations\|assign_speakers\|LongAudioGate" AGENTS.md README.md docs/superpowers/specs/2026-10-31-dictee-server-design.md`
Attendu : aucune ligne, sinon des lignes qui racontent explicitement l'état d'avant.

- [ ] **Étape 4 : commiter**

```bash
git add AGENTS.md README.md docs/superpowers/specs/2026-10-31-dictee-server-design.md
git commit -m "Met la documentation à jour pour les tâches, le découpage et la dictée en direct"
```

### Tâche 15 : intégration et mise en service

**Fichiers :**
- Modifier : `integration/run.py`
- Modifier : ce plan, section « Résultats » de la tâche

- [ ] **Étape 1 : ajouter et adapter les contrôles de `run.py`**

Nouveaux contrôles : `background` (envoi avec `background=true`, puis lecture par `GET` jusqu'à `completed`, et `output` dans le format demandé) ; `rejoin` (requête synchrone coupée après 5 s, puis le même fichier renvoyé : une seule tâche dans les journaux, et la réponse arrive) ; `restart` (tâche longue `dictee` en arrière-plan, `compose restart dictee` après deux morceaux, puis la tâche finit, et sa ligne porte `starts=2` et le nombre de morceaux de la tâche) ; `live` (client Python d'OpenAI, session de transcription sur une phrase Magpie à 24 kHz, le texte contient au moins 60 % de ses mots). Contrôles adaptés : `hangup` (la tâche continue après la coupure et son résultat se relit), `queued` (une tâche longue en cours, trois en attente, la cinquième reçoit 429), `diarize` (personnes données par la session temps réel), `long_stream`.

- [ ] **Étape 2 : lancer l'intégration locale**

Run : `make integration`, puis `make down`
Attendu : chaque contrôle affiche `OK`.

- [ ] **Étape 3 : jouer les scénarios longs sur le VPS**

L'image dictee est construite sur le VPS à partir de l'arbre de travail, envoyé par `tar` sur la connexion SSH et jamais tiré de GitHub. Elle tourne en `verif-dictee` (`--memory 512m`, volume jetable `verif-data`), avec `verif-nemo` comme en tâche 1. Les scénarios, pilotés par un script rangé dans `.integration/verif-taches/` :
1. la Note répétée jusqu'à 2 heures, en mode réunion, avec `background=true`, le client coupé après une minute, puis lue par `GET` jusqu'à `completed` : au moins deux personnes, NeMo sous 3 Go, dictee sous 512 Mo ;
2. le même audio en synchrone, coupé puis renvoyé : une seule tâche, et le résultat arrive ;
3. le même audio en mode `dictee`, en arrière-plan, avec `docker restart verif-dictee` au milieu : la tâche finit, sa ligne porte `starts=2`, et NeMo reçoit autant de requêtes REST que la tâche a de morceaux (à défaut d'un journal de NeMo qui les compte, l'avancement relu juste avant et juste après le redémarrage ne recule pas) ;
4. une dictée en direct par le contrôle `live`.
Tout est supprimé à la fin, conteneurs, réseau, volume et fichiers.

- [ ] **Étape 4 : commiter**

```bash
git add integration/run.py docs/superpowers/plans/2026-11-01-taches-transcription.md
git commit -m "Ajoute les contrôles d'intégration des tâches et de la dictée en direct"
```

- [ ] **Étape 5 : demander à Geoff la suite de la mise en service**

Le travail attend ses réponses, une par action : le push de la branche et la pull request ; sa fusion sur `main` ; le redéploiement dans Dokploy ; `GLOSSARY_BOOST` dans le `.env` de Dokploy, si celui-ci fixe cette variable (vérifié par la présence du nom, sans afficher la valeur). Rien n'est fait sans son accord au moment de l'action.

- [ ] **Étape 6 : vérifier le déploiement, avec l'accord de Geoff**

Rejouer les scénarios 1 et 4 sur la production par le domaine public, puis une requête synchrone courte avec la clé de service, comme Open WebUI, et une comme Dictate : les réponses ont la même forme qu'avant le déploiement.

- [ ] **Étape 7 : effacer les copies**

Effacer `.integration/verif-taches/` sur le Mac (Call, Note, glossaire, enregistrements, textes) et tout ce qui resterait sur le VPS. L'original reste dans le Drive.

**Résultats** (remplis à l'exécution) :
- Contrôles locaux.
- Scénarios longs : temps, pics mémoire de NeMo et de dictee, personnes, `starts`, morceaux. Joués le 5 novembre 2026 sur le VPS, à partir de l'arbre du commit 32dfb59, avec `verif-dictee` (512 Mio) et `verif-nemo` (3 Gio). L'audio est la Note répétée jusqu'à 7199,9 s, et la mémoire est relevée par `docker stats` toutes les 5 à 7 s.
  - Scénario 1 (réunion en arrière-plan, client coupé après une minute, puis lecture par `GET`) : réussi. La réponse arrive en 6,8 s, et la tâche finit `completed` en 3099 s (51,7 min) avec 3 personnes et 497 segments. Les pics sont de 1616 Mio pour NeMo et de 221 Mio pour dictee, qui couvrent aussi le scénario 2.
  - Scénario 2 (même audio en synchrone, coupé à 5 s puis renvoyé pendant la tâche du scénario 1, alors entre 2,5 et 2,7 %) : réussi. Le journal porte trois requêtes (200, 499 puis 200) sur la même tâche et une seule ligne de fin. Le second envoi répond 200 après 3023 s, avec 3 personnes et 497 segments.
  - Scénario 3 (mode `dictee` en arrière-plan, `docker restart verif-dictee` à 2,1 %, après environ cinq morceaux) : réussi. dictee redevient sain 2 s après le redémarrage, l'avancement relu vaut 2,1 % avant comme après et ne recule plus ensuite, et la tâche finit `completed` en 2824 s (47,1 min). Sa ligne porte `chunks=241`, `starts=2` et `retries=0`. NeMo ne journalise pas ses requêtes REST, donc leur nombre n'a pas été compté. Les pics sont de 2350 Mio pour NeMo et de 325 Mio pour dictee.
  - Scénario 4 (dictée en direct par le contrôle `live`) : réussi. 9 mots sur 12 sont retrouvés (75 %), le texte final arrive 2,3 s après le début de l'audio et `cleared` 106 ms après `clear`. Les pics sont de 2385 Mio pour NeMo et de 271 Mio pour dictee.
  - Sur l'ensemble, NeMo culmine à 2385 Mio (sous 3 Gio) et dictee à 325 Mio (sous 512 Mio). Conteneurs, réseau, volume, images et fichiers du VPS ont été supprimés à la fin.
- Vérifications après déploiement.

### Tâche 16 : restes de la revue du premier plan (carte STK-50)

Cette tâche s'exécute après la tâche 14 et avant la tâche 15, pour que la mise en service porte aussi ces corrections. Le premier point de la carte (libérer le WAV avant la reformulation) n'a plus lieu d'être : le WAV est sur le disque depuis la tâche 6.

**Fichiers :**
- Modifier : `nemo/pull-models.sh`, `compose.yaml`, `dictee/src/dictee/__main__.py`, `dictee/src/dictee/lifecycle.py`, `dictee/src/dictee/transcribe.py`, `dictee/src/dictee/speech.py`, `dictee/src/dictee/web.py`, `dictee/src/dictee/upload.py`, `integration/nemo_rt.py`, `integration/run.py`, `integration/voices.py`, `integration/check_nemo.py`, `integration/boost.py`
- Tester : `dictee/tests/test_lifecycle.py`, `dictee/tests/test_main.py`, `dictee/tests/test_transcriptions.py`

**Interfaces :**
- Produit :
  - `lifecycle.abandon_stream(task: asyncio.Task, trace: RequestTrace) -> None` : le filet de fin d'une réponse en continu, qui remplace les deux `_abandon` de `transcribe.py` et de `speech.py`.
  - `lifecycle.RequestTrace` : base des traces de requête, avec `status`, `logged` et `log()`, qui n'écrit la ligne qu'une fois ; `Trace` et la trace de la synthèse en héritent et gardent chacune exactement leur format de ligne.
  - `upload.bounded_request(request: Request, limit_bytes: int, too_big: Callable[[], Exception]) -> Request` : une requête de même corps dont la lecture lève `too_big()` dès que `limit_bytes` octets sont dépassés ; la synthèse (corps JSON), la page (formulaire) et `read_upload` la partagent.
  - `nemo_rt.env_value(name: str) -> str` : la variable d'environnement si elle existe, sinon la valeur du `.env` du dépôt, jamais affichée ; seule lecture du `.env` des scripts d'intégration.
  - `pull-models.sh` écrit la taille de chaque fichier lié dans `pins.txt`, et `models_in_place` compare aussi cette taille à celle du fichier en place.
  - `compose.yaml` donne `stop_grace_period: 30s` à `dictee` ; `__main__.main()` passe `timeout_graceful_shutdown=20` à uvicorn, pour que l'arrêt de la boucle de travail passe avant le SIGKILL de Docker.

- [ ] **Étape 1 : écrire les tests qui échouent**

```python
async def test_a_bounded_request_refuses_a_body_past_its_limit_without_content_length():
    request = request_with_chunks([b"a" * 600, b"b" * 600])  # sans Content-Length
    with pytest.raises(TooBig):
        await bounded_request(request, 1000, TooBig).body()

async def test_abandon_stream_cancels_the_task_and_writes_the_line_once(caplog):
    task, trace = asyncio.create_task(asyncio.sleep(60)), Trace(route="transcriptions")
    await abandon_stream(task, trace)
    await abandon_stream(task, trace)
    assert task.cancelling() and trace.status == CLIENT_CLOSED
    assert len([record for record in caplog.records if "route=transcriptions" in record.getMessage()]) == 1

def test_uvicorn_stops_before_docker_kills_the_container(monkeypatch):
    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: seen.update(kwargs))
    main_with_test_env(monkeypatch)
    assert seen["timeout_graceful_shutdown"] == 20

def test_unreadable_audio_is_422_and_leaves_neither_task_nor_folder(transcriptions_client, service_headers):
    response = post(transcriptions_client, service_headers, b"not audio", model="dictee")
    assert (response.status_code, response.json()["error"]["code"]) == (422, "invalid_audio")
    assert tasks_of(transcriptions_client) == []
    assert not any(transcriptions_client.app.state.settings.tasks_dir.iterdir())

async def test_a_client_hanging_up_during_the_decoding_leaves_neither_task_nor_folder_nor_place(settings, fake_nemo, fake_llm, service_headers, tmp_path, monkeypatch):
    app = app_with(settings, fake_nemo, fake_llm, long_audio_seconds=1.0)
    slow_decoding(monkeypatch)  # le décodage attend un événement avant de finir
    async with app_running(app):
        assert await call_app(app, service_headers, tone_wav(tmp_path), hang_up_during_decoding=True) == 499
        assert app.state.store.unfinished() == [] and not app.state.lane.full
        assert not any(settings.tasks_dir.iterdir())
```

`request_with_chunks`, `main_with_test_env` et `slow_decoding` sont des aides de test écrites dans le fichier qui les utilise. Les tests de journal existants des deux routes passent sans changement : ils figent les formats de ligne.

- [ ] **Étape 2 : vérifier l'échec**

Run : `make test ARGS="tests/test_lifecycle.py tests/test_main.py tests/test_transcriptions.py"`
Attendu : échec sur `abandon_stream`, `bounded_request` et `timeout_graceful_shutdown` introuvables, et sur le dossier laissé par une coupure pendant le décodage s'il en reste un.

- [ ] **Étape 3 : écrire les changements de dictee**

- [ ] **Étape 4 : vérifier le succès**

Run : `make test`
Attendu : toute la suite passe.

- [ ] **Étape 5 : contrôler la taille des modèles hors réseau**

Sur le volume de modèles de `make nemo-up` : lancer le service `models` deux fois, la seconde avec `--network none` (attendu : sortie 0 et la ligne « aucun appel réseau ») ; raccourcir d'un octet une copie de travail du fichier lié par `asr.gguf` (`truncate -s -1`), relancer sans réseau (attendu : sortie non nulle et nom du fichier dans le message) ; relancer avec le réseau (attendu : le fichier est retéléchargé et vérifié, sortie 0).

- [ ] **Étape 6 : vérifier les scripts d'intégration**

Run : `grep -n "\.env" integration/*.py`
Attendu : seule `nemo_rt.env_value` lit le `.env`. Puis `uv run --script integration/boost.py --self-test` et `uv run --script integration/check_nemo.py --help` s'exécutent sans erreur.

- [ ] **Étape 7 : commiter**

```bash
git add nemo/pull-models.sh compose.yaml dictee/src/dictee dictee/tests integration
git commit -m "Traite les restes de la revue du premier plan : arrêt propre, taille des modèles, code partagé"
```
