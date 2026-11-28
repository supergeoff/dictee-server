# Moteurs audio de dictee : couche d'abstraction, CrispASR et Phonon

- **Date** : 28 novembre 2026
- **Statut** : design validé par Geoff le 28 novembre 2026
- **Suivi** : carte Kaneo DIC-29
- **Spec de référence touchée** : `2026-10-31-dictee-server-design.md`, dont les sections sur le moteur seront mises à jour à la fin du chantier

## Intention

Geoff veut que dictee soit le meilleur et le plus rapide possible sur un CPU, avec une voix d'homme, et qu'aucun choix de moteur ou de modèle ne le coince par la suite. Le moteur actuel, NeMo-Speech.cpp, fait presque deux fois plus d'erreurs en français que le meilleur modèle rapide du moment, et sa voix est lente. dictee reçoit donc une couche d'abstraction qui sépare ce que voient les clients de ce qui tourne derrière, puis il change de moteurs derrière cette couche. Les clients (dictee-app, Dictate, Open WebUI) ne voient aucun changement de contrat.

## Objectifs

- La surface externe de dictee reste identique : mêmes routes, mêmes modèles (`dictee`, `dictee-clean`, `dictee-diarize`, `gpt-4o-transcribe-diarize`), mêmes formats, tâches enregistrées, dictée en direct, glossaire, reformulation et synthèse vocale.
- Chaque rôle audio (transcrire un fichier, transcrire en direct, attribuer les locuteurs, synthétiser la voix) passe par un adaptateur choisi dans le `.env`. Changer de moteur ou de modèle revient à changer des valeurs du `.env` et à redéployer, sans toucher au code des routes.
- La transcription passe à CrispASR avec Fluid Speech Pro et Nemotron 3 Diarization. La synthèse vocale passe à phonon-40m-fr avec la voix Lucien.
- NeMo-Speech.cpp sort entièrement à la fin du chantier : service, modèles, code et documentation.
- Les mesures de bout en bout, relevées à travers dictee sur la sidebox, sont inscrites dans DIC-29.

## Non-objectifs

- Aucun changement du service `conversation` ni de son relais. Le mode appel d'Open WebUI continue de fonctionner tel quel, avec ses modèles hébergés.
- Aucune nouvelle route, aucun nouveau champ de requête et aucun changement dans dictee-app.
- Pas de clonage de voix. Le modèle phonon publié ne clone pas, et Pocket TTS, qui clone, est plus lent que le temps réel sur la sidebox.
- Pas de LocalAI ni de parakeet.cpp dans ce chantier. La couche d'abstraction permet de les brancher plus tard.

## Anti-objectifs

- Un glossaire qui passe d'un compte à un autre. Le serveur CrispASR réutilise les hotwords de la requête précédente quand une requête n'en envoie pas : dictee ne lui en envoie donc jamais.
- Un texte personnel (glossaire, transcription, texte à dire) dans les journaux, dans le dépôt ou dans un message d'erreur.
- Une bascule sans retour arrière, ou une suppression de NeMo avant que Geoff ait utilisé le nouveau moteur.
- Un secret dans le compose, dans le dépôt ou dans la conversation.

## Contraintes

- Règles du dépôt (`AGENTS.md`) : compatibilité avec l'API audio d'OpenAI, rien de personnel, aucun secret, versions explicites, travail sur une branche, tests avant le code, Geoff fusionne sur `main`.
- Conventions de `dokployconfig/docs/conventions-compose.md` : un seul `.env`, `env_file: .env`, aucune variable dans `environment`, `expose` et jamais `ports`, `pull_policy: always` sur une image applicative, volumes nommés, toute image et toute version construite inscrites dans `updater/catalog.json`.
- Toute écriture dans Dokploy, LiteLLM, Open WebUI ou GitHub hors du dépôt attend l'accord explicite de Geoff au moment où elle arrive.
- La sidebox a 6 vCPU AMD EPYC-Milan avec AVX2, 12 Go de RAM dont 7 Go disponibles, et pas de `buildx`. Les 7 Go ont été relevés avec NeMo chargé et en service. Un build doit donc venir d'un contexte git, comme celui de `nemo/` aujourd'hui, et non d'un `dockerfile_inline`.
- phonon-40m-fr est sous licence CC BY-NC 4.0, qui interdit l'usage commercial. Son dépôt Hugging Face demande une acceptation, déjà faite sur le compte de Geoff : le téléchargement a besoin d'un jeton.

## Mesures qui fondent les choix

Elles ont été relevées le 28 novembre 2026 sur la sidebox, avec 4 fils, une limite de 3 Go de mémoire et les 5 premières minutes d'une réunion en français à 4 voix. Le détail est dans DIC-29.

| Rôle | Moteur et modèle | Temps | RAM maximale | Résultat |
|---|---|---|---|---|
| Fichier avec locuteurs | CrispASR 0.8.55, Fluid Speech Pro q8_0 et Nemotron 3 Diarization q8_0 | 67 s pour 300 s | 1,5 Go | 4 locuteurs trouvés |
| Fichier sans locuteurs | CrispASR, Fluid Speech Pro | 53 s pour 300 s | 1,3 Go | 738 mots |
| Fichier sans locuteurs | parakeet.cpp, Fluid Speech Pro | 26 s pour 300 s | 1,2 Go | 734 mots |
| Dictée en direct | CrispASR `--stream`, Fluid Speech Pro | 0,8 cœur | 1,7 Go | Texte partiel 0,34 s après la parole à la médiane (1,0 s au 90e centile), texte final à 2,7 s |
| Glossaire | CrispASR `--hotwords`, boost 2 | identique | identique | Texte identique octet pour octet avec et sans |
| Synthèse vocale | phonon-40m-fr, moteur ptts 0.7.1 | 1,9 s pour 20 s de parole | 354 Mo | Voix d'homme du catalogue |
| Synthèse vocale | Pocket TTS FR, voix clonée | 30 s pour 20 s de parole | 0,8 Go | Plus lent que le temps réel |

Sur le leaderboard Open ASR, Fluid Speech Pro fait 5,10 % d'erreur en français et le modèle de production (Nemotron 3.5 streaming) 9,41 %. Sur le leaderboard Open TTS, phonon-40m-fr fait 2,45 % d'erreur en français, Magpie 5,12 % et Pocket TTS 4,40 %.

Une réserve vient du banc. Sur une parole continue, les textes finals du direct de CrispASR ne totalisent que 370 mots, contre 738 hors direct : les longues prises de parole perdent du texte. Ce point est un critère d'acceptation du plan (voir « Dictée en direct »).

## Approches considérées

| Approche | Arbitrage |
|---|---|
| **Retenue : couche d'abstraction dans dictee, CrispASR pour toute la transcription, phonon pour la voix** | Un seul moteur de transcription, mesuré sur les trois rôles, et une voix dix fois plus rapide que le temps réel. Les fichiers sont deux fois plus lents qu'avec parakeet.cpp, ce que Geoff accepte pour n'avoir qu'un moteur. |
| parakeet.cpp pour les fichiers et les locuteurs, CrispASR pour le direct | Le plus rapide par rôle, mais deux moteurs à épingler et Fluid chargé deux fois. Geoff l'a écarté. L'adaptateur reste possible plus tard. |
| LocalAI comme couche moteur | Il combine les modèles par YAML et offre un speech to speech complet, mais c'est une plateforme lourde, sans glossaire, et son texte partiel n'existe qu'avec un modèle en flux moins bon que Fluid. Geoff a préféré une couche propre à dictee. |
| CrispASR sans couche d'abstraction | Le premier design de la journée. Il remplaçait un couplage par un autre, alors que Geoff veut pouvoir changer sans être coincé. |

## Ce que Geoff a décidé

- dictee porte une couche d'abstraction qui branche n'importe quel moteur en direct et y charge n'importe quel modèle.
- La surface externe garde toute la couverture actuelle, direct et glossaire compris.
- Toute la transcription passe par CrispASR, avec Fluid Speech Pro et Nemotron 3 Diarization.
- La synthèse vocale passe par phonon-40m-fr avec la voix Lucien.
- Les conditions d'accès de phonon-40m-fr sont acceptées sur son compte Hugging Face.

## La construction

### La couche d'abstraction

Aujourd'hui, tout le moteur passe par une seule classe, `NemoClient` (`dictee/src/dictee/nemo.py`), que les routes appellent par `state.nemo`. Le chantier la remplace par trois rôles, décrits par des protocoles Python dans un paquet `dictee/engines/`.

| Rôle | Ce que les routes lui demandent | Aujourd'hui dans `NemoClient` |
|---|---|---|
| Transcription de fichier | Un morceau de WAV rend ses mots horodatés. Un fichier entier rend ses mots avec les locuteurs et sa progression, et du texte partiel quand le moteur en produit. Le rôle dit s'il est prêt. | `transcribe_chunk`, `transcribe(speakers=…, partials=…)`, `ready` |
| Dictée en direct | Une session reçoit du PCM, un commit et un vidage, et rend des textes partiels, des textes finals et les accusés du commit et du vidage. | `live` |
| Synthèse vocale | Une phrase et une voix rendent du PCM mono. Le rôle donne sa fréquence, la liste de ses voix et dit s'il est prêt. | `speak`, `voices`, `TTS_SAMPLE_RATE` |

Les types partagés (`Word`, `Final`, `Partial`, `Committed`, `Cleared`) ne changent pas. Les trois exceptions du moteur (indisponible, saturé, erreur) gardent leur sens et perdent le préfixe `Nemo`.

Chaque adaptateur déclare ses capacités, et dictee comble ce qui manque pour que la surface externe ne bouge pas.

| Capacité | Adaptateur qui l'a | Ce que fait dictee sinon |
|---|---|---|
| Renforcer les mots du glossaire au décodage | `nemo` | Il n'envoie pas le glossaire au moteur. La correction du glossaire par dictee s'applique dans tous les cas (voir « Glossaire »). |
| Rendre les locuteurs | `nemo`, `crispasr` | La requête d'un modèle avec locuteurs reçoit une erreur claire au démarrage : un moteur sans locuteurs ne peut pas servir `dictee-diarize`. |
| Rendre la progression d'un fichier | `nemo`, `crispasr` | La progression de la tâche avance par paliers fixes. |
| Rendre du texte partiel pendant un fichier (`stream=true`) | `nemo` | dictee découpe le fichier aux silences et émet un delta par morceau fini (voir « Transcription de fichier »). |

Trois variables du `.env` choisissent les adaptateurs : `STT_ENGINE`, `LIVE_ENGINE` et `TTS_ENGINE`. Les valeurs livrées sont les suivantes.

| Variable | Valeurs | Adaptateur |
|---|---|---|
| `STT_ENGINE` | `nemo`, `crispasr` | `nemo` reprend le code actuel sans changement de comportement. `crispasr` appelle le serveur CrispASR par son API compatible OpenAI. |
| `LIVE_ENGINE` | `nemo`, `crispasr-stream` | `crispasr-stream` lance un `crispasr --stream` par session. |
| `TTS_ENGINE` | `nemo`, `openai` | `openai` appelle n'importe quel serveur qui sert `POST /v1/audio/speech` en PCM et la liste des voix, ici le serveur de phonon. |

L'adaptateur `nemo` ne vit que le temps de la bascule : il sert au retour arrière, puis il part avec NeMo (étape 6).

### Transcription de fichier (adaptateur `crispasr`)

- **Morceau.** dictee garde son découpage aux silences (`cuts.py`, morceaux de 40 s au plus). Chaque morceau part en `POST /v1/audio/transcriptions` avec `response_format=verbose_json` et l'horodatage par mot. La langue demandée est transmise, et `auto` laisse CrispASR détecter la langue.
- **Fichier avec locuteurs.** Le fichier entier part en une requête avec `diarize=true` et `diarize_method=sortformer`. Le serveur est lancé avec le modèle de locuteurs. La progression vient de `GET /progress`, lu toutes les quelques secondes.
- **Fichier en flux (`stream=true`).** Aujourd'hui, NeMo rend un texte partiel toutes les 1,12 s d'audio pendant un fichier. Le serveur CrispASR n'en rend aucun pour ce modèle. dictee émet donc un `transcript.text.delta` par morceau fini, avec des morceaux coupés aux silences et bornés à 15 s dans ce mode, ce qui donnerait un delta toutes les 3 secondes environ d'après la vitesse mesurée sur la sidebox (estimation, à mesurer). Les événements gardent leur forme, et seul leur rythme change. Avec les locuteurs, le texte arrive en une fois à la fin de la requête, suivi de l'événement final (choix de Claude).
- **Aucun hotword.** L'adaptateur n'envoie jamais `hotwords` (voir Anti-objectifs).
- **Pannes.** Un serveur injoignable ou en cours de chargement (`/health` à 503) donne « indisponible », un refus pour cause de charge donne « saturé », et les reprises de `processing.py` restent celles d'aujourd'hui.
- **Requêtes en parallèle.** Le serveur CrispASR traite une requête à la fois par instance de modèle. Il est lancé avec `--server-workers 2` (choix de Claude), et le plan mesure le délai d'une dictée courte pendant une réunion longue.

### Dictée en direct (adaptateur `crispasr-stream`)

Le serveur CrispASR ne rend aucun texte partiel pour ce modèle. L'adaptateur lance donc, dans le conteneur de dictee, un processus `crispasr --stream --stream-json --vad` par session, lui envoie le PCM rééchantillonné à 16 kHz par son entrée standard et lit ses événements JSON.

- **Texte partiel.** CrispASR renvoie à chaque pas le texte entier de la prise de parole, révisé. Les deltas d'OpenAI ne font qu'ajouter du texte. L'adaptateur n'émet donc que le prolongement stable : les mots qui n'ont pas changé entre deux textes partiels successifs. Il ne retire jamais un mot déjà émis (choix de Claude).
- **Texte final.** L'événement `final` de CrispASR devient le texte final de la prise de parole, qui fait foi.
- **Commit du client.** dictee garde le contrat actuel, où le client valide ses prises de parole. Au commit, l'adaptateur envoie assez de silence pour que CrispASR ferme la prise de parole, attend son texte final, puis accuse le commit.
- **Vidage.** L'adaptateur écarte la prise de parole en cours et son texte final à venir.
- **Sessions simultanées.** Chaque session charge sa copie du modèle (1,7 Go). `LIVE_MAX_SESSIONS` borne leur nombre, à 2 par défaut (choix de Claude). Au-delà, la connexion reçoit l'erreur de saturation déjà prévue par le contrat.
- **Critère d'acceptation.** Sur une parole réelle de 3 minutes dite presque d'une traite (le passage de 255 à 431 s de l'enregistrement du banc, où une seule personne parle), la somme des textes finals contient au moins 95 % des mots de la transcription du même audio hors direct. Si le critère échoue après réglage de la fenêtre (`--stream-length`, `--stream-step`), le constructeur s'arrête et rapporte à Geoff. La solution de repli prévue est un adaptateur `window`, qui fait la fenêtre glissante dans dictee au-dessus du rôle de transcription de fichier et marche donc avec n'importe quel moteur. Il n'est construit que si le critère échoue. Ce critère se mesure en première tâche de l'étape 2, avant de construire le reste, pour ne pas découvrir un échec du direct à la bascule (choix de Claude).

### Synthèse vocale (adaptateur `openai`)

- dictee garde son découpage en phrases et son encodage par ffmpeg. Chaque phrase part en `POST /v1/audio/speech` avec `response_format=pcm`, et la fréquence du PCM (24 kHz pour phonon) vient de l'adaptateur et non plus d'une constante.
- La liste des voix vient de `GET /v1/audio/voices`. `DEFAULT_VOICE` vaut `Lucien`.
- Une voix de profil que le moteur ne propose plus (`Leo`, `Sofia` et les autres voix de Magpie) donne la voix par défaut, sans erreur. Aujourd'hui, une voix de profil inconnue part telle quelle au moteur : ce comportement change, parce que le serveur de phonon la refuserait.
- La vitesse reste appliquée par ffmpeg dans dictee. Le serveur de phonon ne la reçoit pas.
- Le serveur de phonon ne traite qu'une synthèse à la fois par défaut et répond 429 au-delà, alors que NeMo met les requêtes en file. L'adaptateur fait donc la file lui-même : il n'envoie qu'autant d'appels simultanés que le serveur en accepte, et les suivants attendent leur tour. Un 429 reçu malgré cela est réessayé pendant quelques secondes, puis il devient « saturé » (choix de Claude).

### Glossaire

Aucun des deux moteurs mesurés n'applique le glossaire. dictee le porte donc lui-même, en deux niveaux.

1. **Correction déterministe.** Les entrées du glossaire ont déjà la forme `Terme (souvent entendu : a, b)`. Après la transcription, dictee remplace chaque variante entendue par son terme, par mot entier et sans tenir compte de la casse. La correction s'applique au texte et aux mots de tous les modèles, des tâches enregistrées et des textes finals du direct. Un terme sans variante n'a pas d'effet à ce niveau.
2. **Reformulation.** Le glossaire continue d'être donné au LLM de `dictee-clean`, comme aujourd'hui.

Le glossaire fusionné (profil puis champ `prompt`) reste calculé comme le décrit la spec du 18 novembre. `GLOSSARY_BOOST` n'a de sens qu'avec l'adaptateur `nemo` et part avec lui.

### Images, modèles et compose

Le dossier `nemo/` du dépôt laisse la place à un dossier `crispasr/`, sur le même schéma : un Dockerfile qui télécharge la release `CRISPASR_VERSION` (argument de build du compose) et vérifie son empreinte, un script qui télécharge les modèles et un script de lancement. L'image de dictee reçoit le même binaire, pour la dictée en direct, avec le même argument de build.

Le compose de Dokploy passe de cinq à six services pendant la bascule, puis revient à cinq.

| Service | Rôle | Image | Mémoire de départ |
|---|---|---|---|
| `models` | Télécharge les modèles dans un volume, puis s'arrête | Build `crispasr/` | 512 Mo |
| `crispasr` | Serveur de transcription, sans domaine | Build `crispasr/` | 3 Go |
| `phonon` | Serveur de voix, sans domaine ni authentification | `ghcr.io/gradium-ai/ptts-openai-server`, tag explicite | 768 Mo |
| `dictee` | Inchangé, plus le binaire du direct et le volume des modèles en lecture seule | Build `dictee/` | 913 Mio mesurés avec une session en direct ; 3 Go dans le compose de test, valeur de production à mesurer au déploiement |
| `conversation`, `conversation-gate` | Inchangés | | |
| `nemo` | Gardé jusqu'à l'étape 6, puis retiré | | |

Les modèles se choisissent dans le `.env`, que lisent le script de téléchargement et les services.

| Variable | Valeur de départ |
|---|---|
| `ASR_BACKEND` | `parakeet` |
| `ASR_MODEL_URL`, `ASR_MODEL_SHA256` | Le fichier `fluid-speech-pro-q8_0.gguf` du dépôt `cstr/fluid-speech-pro-GGUF` et son empreinte |
| `DIAR_METHOD` | `sortformer` |
| `DIAR_MODEL_URL`, `DIAR_MODEL_SHA256` | Le fichier `Nemotron-3-Diarization.q8_0.gguf` du dépôt `nvidia/Nemotron-3-Diarization` et son empreinte |
| `CRISPASR_API_KEYS`, `CRISPASR_THREADS` | Une clé générée au déploiement, et 4 fils |
| `PTTS_MODEL`, `PTTS_LANG`, `PTTS_QUANT` | `hf://gradium/phonon-40m-fr@<commit>`, `fr`, `q8` |
| `VAD_MODEL_URL`, `VAD_MODEL_SHA256` | Le fichier `ggml-silero-v6.2.0.bin` du dépôt `ggml-org/whisper-vad` et son empreinte (constat de construction : sans lui, le binaire du direct retélécharge ce modèle à chaque session ; dictee le lit par `LIVE_VAD_MODEL_PATH`) |
| `HF_TOKEN` | Le jeton Hugging Face de Geoff, écrit dans le `.env` de Dokploy sans passer par la conversation |
| `STT_ENGINE`, `LIVE_ENGINE`, `TTS_ENGINE`, `DEFAULT_VOICE`, `LIVE_MAX_SESSIONS` | `crispasr`, `crispasr-stream`, `openai`, `Lucien`, `2` |

L'updater suit `CRISPASR_VERSION` par les releases GitHub de `CrispStrobe/CrispASR` (section `pins`) et l'image de phonon par son tag (section `images`). La pin `NEMO_VERSION` sort du catalogue à l'étape 6.

### Tests

- `make test` garde son principe : pas de réseau, un faux serveur CrispASR, un faux serveur de voix et un faux LiteLLM. Les tests du faux NeMo restent tant que l'adaptateur `nemo` existe.
- Les tests de contrat des routes ne changent pas : ils prouvent que la surface externe est identique avec chaque adaptateur.
- `make integration` reçoit `CRISPASR_VERSION` à la place de `NEMO_VERSION` et joue les vrais modèles sur de courts extraits.

## Ordre de mise en service

1. **Couche d'abstraction, sans changement de comportement.** `NemoClient` devient l'adaptateur `nemo` derrière les trois rôles. Tous les tests existants passent sans modification de leurs attentes.
2. **Adaptateurs et images.** Les adaptateurs `crispasr`, `crispasr-stream` et `openai`, la correction du glossaire, le dossier `crispasr/` et les tests. Geoff fusionne sur `main`, ce qui ne déploie rien.
3. **Catalogue de l'updater.** Une PR sur `dokployconfig` ajoute la pin et l'image.
4. **Compose.** Avec l'accord de Geoff : ajout de `crispasr` et `phonon`, nouveau `models`, nouvelles variables, `nemo` conservé. dictee démarre encore sur les adaptateurs `nemo`. Le constructeur relève la mémoire disponible avant et après. Si les deux moteurs ne tiennent pas ensemble, il réduit d'abord `LIVE_MAX_SESSIONS` à 1, puis le serveur CrispASR à un seul worker. Le service `nemo` ne s'arrête pas avant l'étape 6, parce qu'il porte le retour arrière (choix de Claude).
5. **Bascule.** Les trois variables passent sur les nouveaux adaptateurs, et dictee redémarre. Le constructeur rejoue les mesures à travers dictee (réunion de 5 minutes, dictée en direct, phrase lue) et les inscrit dans DIC-29. Geoff essaie dictee-app, Dictate et Open WebUI.
6. **Sortie de NeMo.** Après le feu vert de Geoff : retrait du service `nemo` et de son volume, de l'adaptateur `nemo`, du dossier `nemo/`, des variables `NEMO_*` et `GLOSSARY_BOOST`, de la pin de l'updater, puis mise à jour de `AGENTS.md`, du README et de la spec de référence. Le plan est archivé dans `docs/archive/`.
7. **Speech to speech en local, à l'essai.** Le service `conversation` reste sur ses modèles hébergés. Le constructeur mesure ce que donnerait un appel dont la transcription et la voix passent par dictee, et rapporte les délais à Geoff, qui décide (choix de Claude : cette étape ne change rien sans sa décision).

## Retour arrière

Jusqu'à l'étape 6, remettre `STT_ENGINE`, `LIVE_ENGINE` et `TTS_ENGINE` sur `nemo` et redémarrer dictee suffit à revenir au moteur actuel. Chaque rôle peut revenir seul. Après l'étape 6, un retour à NeMo demande de rétablir le service et l'adaptateur depuis git.

## Constats de construction

Cette section note ce que la construction a établi là où la spec laissait le choix au constructeur. Elle ne change aucune décision de Geoff.

- **Réglage de la dictée en direct.** Le réglage retenu est `--stream-step 1000 --stream-final-on-silence-ms 800`. Avec le pas par défaut de 3000 ms, deux textes finals sur cinq perdaient leurs derniers mots, alors qu'ils sont complets avec 1000 ms.
- **Critère d'acceptation du direct.** Mesuré sur une machine de développement x86_64 (4 cœurs) avec les réglages par défaut, le critère donne 489 mots sur 492, puis 492 sur 492 à une seconde mesure. Les délais restent à mesurer sur la sidebox.
- **Validation d'une prise de parole.** À la validation, la session écrit 3 s de silence, attend l'événement `silence` qui prouve la fermeture, puis rend les textes finals et accuse le commit. Les textes finals que CrispASR rend avant le commit sont gardés jusqu'au commit, pour que le client ne voie aucune prise de parole close qu'il n'a pas validée.
- **Fichiers du direct.** Le binaire ne s'arrête pas quand le modèle de détection de voix manque : l'adaptateur vérifie donc les deux fichiers de modèle avant de lancer un processus.
- **Modèle de détection de voix.** Il s'obtient par `VAD_MODEL_URL` et `VAD_MODEL_SHA256` (voir le tableau des variables) et se lit par `LIVE_VAD_MODEL_PATH` côté dictee.
- **Langue.** Sans langue demandée, la réponse porte `und`, avec NeMo comme avec CrispASR. Interrogé avec `language=auto`, CrispASR annonce `en` sur un audio français : ce champ n'est pas une détection, et l'adaptateur ne le lit pas.
- **Port WebSocket de CrispASR.** Il est écarté pour le direct : sa route Realtime ne rend de texte partiel que pour le moteur Nemotron, et elle répond seulement au commit pour parakeet.
- **Intégration.** Tous les contrôles de `integration/run.py` passent sur x86_64 avec les nouveaux moteurs. Le contrôle de langue ne couvre plus le cas anglais, faute de voix anglaise. Sur une machine de développement x86_64, avec les nouveaux moteurs pour les trois rôles, les quatre contrôles de `integration/check_crispasr.py` et les dix-huit contrôles de `integration/run.py` passent, dictée en direct comprise.
- **Mémoire par session du direct.** Les lignes « 1,7 Go » de cette spec sont des mesures d'avant la construction : elles venaient du banc, sur une réunion longue dite d'une traite. Mesurée dans le conteneur de dictee sur des dictées courtes, la mémoire est de 913 Mio avec une session et de 1,1 Gio avec deux, parce que le fichier du modèle (environ 570 Mio) est partagé entre les processus et que chaque session ajoute environ 220 Mio.
- **Moteur en retard.** Quand le moteur de dictée en direct prend du retard sur l'audio reçu, la session garde l'audio en attente au lieu de bloquer la connexion, jusqu'à `MAX_PENDING_S` (120 s d'audio), puis la connexion reçoit l'erreur de saturation. Les silences du commit et du vidage ne sont jamais refusés.
- **Licence du modèle de détection de voix.** Silero VAD (licence MIT, distribué par `ggml-org/whisper-vad`) s'ajoute aux mentions de licence de « Ce qui reste au constructeur », et il figure au README.
- **Unité de la progression.** Le rappel `on_progress` d'un moteur de fichier reçoit les secondes d'audio traitées, et non une fraction, pour tous les moteurs : `nemo` donne les secondes envoyées après chaque trame, et `crispasr` donne la fin de chaque morceau ou, pour une réunion avec locuteurs, le pourcentage de `GET /progress` rapporté à la durée du fichier. dictee rapporte lui-même ces secondes à la durée de la tâche. Les moteurs n'ont plus d'attribut qui dit s'ils rendent une progression.
- **Progression d'une réunion avec CrispASR.** Pour une réunion avec locuteurs, `GET /progress` vaut 0 pendant toute la transcription, puis 100 pendant la séparation des locuteurs : le serveur n'annonce pas de valeur intermédiaire pour ce modèle, et la progression de la tâche n'en a donc pas non plus. Mesurée sur le vrai serveur, une réunion de 307 s terminée en 133 s a gardé une progression de 0 pendant 106 s, puis de 1 jusqu'à la fin, avec 58 segments et deux locuteurs retrouvés. La progression d'une transcription par morceaux, elle, avance morceau par morceau.
- **Délai d'une réunion entière.** Le serveur ne répond qu'à la fin de la transcription d'une réunion avec locuteurs. Le délai de lecture de cette requête vaut donc une seconde par seconde d'audio, avec pour plancher le délai d'un morceau (600 s) : le serveur visé transcrit en un quart du temps de l'audio, ce qui laisse une marge de quatre, file d'attente comprise. Le fichier part depuis le disque, au fil de l'envoi.
- **Workers du serveur.** `CRISPASR_WORKERS` règle le nombre de workers du serveur CrispASR (`--server-workers`), et `crispasr/serve.sh` refuse de démarrer sans elle. Ce constat corrige le passage « Requêtes en parallèle » de la section « Transcription de fichier (adaptateur `crispasr`) », qui écrit `--server-workers 2` en dur : la valeur 2 passe désormais par le `.env`, comme dans le compose de test.
- **Méthode de séparation des locuteurs.** `DIAR_METHOD` (`sortformer` par défaut) est lue par dictee, qui l'envoie au serveur dans le champ `diarize_method` de chaque requête avec locuteurs.
- **Glossaire et élisions.** La correction du glossaire reconnaît aussi une variante dans un mot que le moteur rend avec une élision, un préfixe et son trait d'union ou une ponctuation collée (`d'tarvél`, `sous-tarvél,`), et garde ce qui l'entoure. Les mots (segments, sous-titres, `words`) sont ainsi corrigés comme le texte.
- **Mémoire du serveur CrispASR.** La mémoire du service `crispasr` croît avec la durée de l'audio : le serveur reçoit le fichier entier, et la séparation des locuteurs travaille sur toute la réunion à la fin du traitement. Avec les réglages par défaut et une limite de 3 Go, une réunion de 65 minutes a fait tomber le service sur le serveur visé, et les dictées courtes arrivées pendant ce temps ont reçu un texte vide avec un code 200, parce que le garde-fou de mémoire du serveur refuse le calcul sans signaler d'erreur. Parmi les réglages du serveur, seule `CRISPASR_PARAKEET_LONGFORM_WINDOW` agit sur la mémoire : elle donne la durée des morceaux transcrits dans un fichier de plus de 300 s (90 s par défaut), et la valeur 30 est retenue. `CRISPASR_PARAKEET_MEM_POLICY`, `CRISPASR_PARAKEET_VRAM_BUDGET_MB` et `CRISPASR_PARAKEET_STREAM_THRESHOLD` n'ont rien changé à la mesure. Un seul worker économise environ 770 Mio, mais une dictée attendrait alors la fin d'une réunion, et les 2 workers sont gardés.
- **Dimensionnement mesuré.** Sur le serveur visé, avec des morceaux de 30 s et 2 workers, une réunion de 65 minutes avec locuteurs occupe 2,3 Go pendant la transcription et 3,5 Go au plus pendant la séparation des locuteurs, pour 581 s de traitement. Une réunion de 118 minutes occupe 3,3 Go puis 5,2 Go sur un serveur neuf (1053 s), et 4,4 à 4,9 Go puis 5,4 à 5,8 Go après une autre réunion (1045 à 1063 s). Le serveur occupe 0,7 Go à neuf et garde 2 à 2,9 Go après une réunion longue. La limite de mémoire du service est fixée à 8 Go, ce qui laisse 2,4 Go au-dessus du plus haut pic mesuré. Pendant ces mesures, 196 dictées courtes envoyées toutes les 20 secondes sont toutes revenues complètes, en 2,5 s au lieu de 1,7 s, et deux réunions de 118 minutes à la suite ont rendu le même résultat (1310 segments, 14 410 mots, deux locuteurs).
- **Annulation et moteur.** L'annulation d'une tâche passe la tâche à `cancelled` dans dictee, mais le serveur CrispASR termine la requête qu'il a déjà reçue : une réunion annulée occupe encore un worker et sa mémoire jusqu'à la fin de son traitement. Une réunion lancée juste après une annulation s'ajoute donc à la première dans le serveur.
- **Progression avec des morceaux courts.** Avec des morceaux de 30 s, la progression d'une réunion avec locuteurs passe à 1 dès les premières secondes du traitement (16 à 27 s mesurées), alors que la tâche dure encore dix à dix-sept minutes. Ce constat complète le précédent sur la progression : seul `status` dit qu'une réunion est finie.
- **Progression estimée.** Les deux constats précédents ont conduit à ne plus lire `GET /progress` : pour une réunion avec locuteurs, l'adaptateur `crispasr` estime les secondes traitées d'après le temps écoulé, toutes les 5 secondes, à raison de 0,25 seconde de calcul par seconde d'audio, et il plafonne l'estimation à 95 % du fichier tant que la réponse n'est pas arrivée. Le facteur vient du serveur visé, où un enregistrement réel de 35 minutes a pris 470 s (0,23) et les réunions de synthèse 0,15. Ce constat corrige la phrase « La progression vient de `GET /progress` » de la section « Transcription de fichier (adaptateur `crispasr`) » et la fin du constat « Unité de la progression ». Le facteur est une constante du code : il deviendra un réglage si le serveur change.

## Les enregistrements des mesures

Les mesures et le critère du direct se jouent sur l'enregistrement du banc, un enregistrement du propriétaire du dépôt, gardé hors du dépôt. Il est privé : le constructeur le lit au moment du test, ne le copie ni dans le dépôt ni dans une image, et efface sa copie de travail après la mesure. La phrase lue par la synthèse vocale est un texte neutre écrit dans le script de mesure.

## Ce qui reste au constructeur

- Le découpage exact du paquet `dictee/engines/` et le nom des protocoles.
- La mémoire et le temps d'une réunion de 1 à 2 heures avec locuteurs par le serveur CrispASR. Si la requête unique dépasse la limite du service, le constructeur s'arrête et rapporte, avec la mesure.
- Le réglage de la fenêtre du direct et la quantité de silence qui ferme une prise de parole au commit.
- Le commit de phonon-40m-fr à épingler dans `PTTS_MODEL` et le tag de l'image du serveur de voix.
- Les healthchecks des deux nouveaux services, les limites de mémoire après mesure et l'ordre de démarrage.
- Les mentions de licence à porter au README : Fluid Speech Pro (CC BY 4.0, crédit à FluidVoice), Nemotron 3 Diarization (OpenMDW), phonon-40m-fr (CC BY-NC 4.0, usage non commercial), CrispASR (MIT), ptts (MIT ou Apache-2.0).
