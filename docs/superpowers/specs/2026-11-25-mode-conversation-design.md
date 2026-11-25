# Mode conversation d'Open WebUI : serveur speech-to-speech dans le compose dictee

Spec du 25 novembre 2026.

## Intention

Geoff veut que le mode appel d'Open WebUI fonctionne en temps réel. dictee (NeMo, avec Nemotron et Magpie) lui convient pour la dictée et la lecture vocale, mais il est trop lent pour un appel, et l'inférence vocale sur ses machines n'est pas envisageable. La voie retenue fait tourner un pont léger sur la sidebox. Ce pont ne garde en local que la détection de parole et appelle des modèles hébergés, par LiteLLM. Geoff veut un service conforme à ses conventions de compose, deux alias LiteLLM sans nom de modèle, et un Open WebUI réglé pour que l'appel fonctionne de bout en bout.

## Objectifs

- Un appel vocal depuis Open WebUI (web et PWA) où l'assistant répond à voix haute en français. La délégation des vraies tâches au modèle du chat, que fait déjà le bridge d'Open WebUI, continue de fonctionner.
- Le serveur [speech-to-speech](https://github.com/huggingface/speech-to-speech) de Hugging Face, qui parle le protocole Realtime d'OpenAI, est ajouté au compose Dokploy `dictee` (serveur sideboxnode) avec son propre domaine.
- Deux nouveaux alias LiteLLM, `transcribe` et `speech`, s'ajoutent à l'alias de conversation existant `medium`.
- Toutes les requêtes de modèle passent par LiteLLM, et leurs traces arrivent donc dans Phoenix.

## Non-objectifs

- dictee, `nemo` et `models` ne changent pas, ni dans le compose ni dans leur code.
- Aucun modèle vocal de bout en bout (gpt-realtime et équivalents) et aucune inférence de modèle sur la sidebox.
- La dictée, la lecture vocale et le mode d'appel standard d'Open WebUI ne changent pas. Le mode standard reste le retour arrière.
- Aucun patch du code de speech-to-speech ni de celui d'Open WebUI.

## Anti-objectifs

- Un domaine public qui laisse n'importe qui consommer les crédits OpenRouter. Le serveur speech-to-speech n'a aucune authentification, et sa documentation le dit.
- Une image de plusieurs Go construite pour CUDA sur une machine sans GPU.
- Un secret écrit dans le compose, dans un fichier monté ou dans le dépôt, qui est public.

## Contraintes

- Conventions de `dokployconfig/docs/conventions-compose.md` : un seul `.env`, `env_file: .env`, aucune variable dans `environment`, `expose` et jamais `ports`, ordre des clés, `pull_policy: always` sur une image applicative, `restart: unless-stopped`, volumes nommés en bas, fichiers de configuration en montages de fichiers Dokploy référencés par `../files/<fichier>`, domaine créé par `domain.create`.
- Toute image ou version construite entre dans `dokployconfig/updater/catalog.json`.
- Règles du dépôt `dictee-server` (AGENTS.md) : branche, Geoff fusionne sur `main`, rien de personnel ni de secret, documentation en français.
- Toute écriture dans Dokploy, LiteLLM, Open WebUI ou GitHub hors dépôt attend l'accord explicite de Geoff au moment où elle arrive.
- Le joker DNS `*.supergeoff.top` pointe sur le VPS principal. Le domaine du service a besoin d'un enregistrement A vers la sidebox (146.59.63.80), que Geoff crée lui-même.
- dictee sert déjà `/v1/realtime` (dictée en direct) sur `dictee.supergeoff.top` : le nouveau service ne peut pas partager ce domaine.

## Approches considérées

| Approche | Arbitrage |
|---|---|
| **Retenue : speech-to-speech dans le compose dictee, derrière un gardien Caddy qui vérifie une clé** | Une brique Apache-2.0 largement adoptée, qui garde en local la détection de parole en CPU et délègue le reste à LiteLLM. Le gardien ajoute un petit service, mais c'est la seule protection compatible avec la clé `Bearer` qu'envoie Open WebUI. |
| gpt-realtime-2.1-mini par LiteLLM | Compatibilité garantie et coût d'environ 0,20 $ par appel de référence, mais un modèle fermé. Geoff a choisi la voie open-weight. |
| Un chemin sur `dictee.supergeoff.top` | Impossible : `/v1/realtime` y est déjà pris. |
| Basicauth Traefik ou filtrage par IP | Open WebUI envoie une clé `Bearer`, que le basicauth refuse. Le filtrage par IP dépend d'un chemin réseau non vérifié entre hostbox et sidebox. |
| Image officielle du projet | Elle est construite sur `nvidia/cuda` et pèse plusieurs Go. |

## Ce que Geoff a décidé

- Le service vit dans le compose `dictee`, avec un domaine dédié.
- Les alias s'appellent `transcribe` et `speech`. Ils pointent sur les modèles Mistral d'OpenRouter : `mistralai/voxtral-mini-transcribe-3-0` (0,30 s médian, 0,000058 $ par seconde) et `mistralai/voxtral-mini-tts-3-0` (0,46 s avant le premier son, 13 $ par million de caractères, voix françaises `fr_marie_*`).
- Le modèle de conversation est l'alias LiteLLM `medium`.

## La construction

### Le service `conversation`

- **Image.** Un Dockerfile dans `dictee-server/s2s/`, à côté de `nemo/` et `dictee/`. Base `python:3.13-slim` (le paquet supporte 3.11 à 3.14), `libsndfile1` et `libportaudio2`, puis `pip install speech-to-speech==${S2S_VERSION}` avec la roue CPU de PyTorch (`--extra-index-url https://download.pytorch.org/whl/cpu`). La version vient d'un argument de build du compose (`S2S_VERSION: "1.1.0"`, dernière release PyPI du 20 octobre 2026), comme `NEMO_VERSION`. Le build se fait depuis le dépôt (`https://github.com/supergeoff/dictee-server.git#main:s2s`). Le dépôt n'écrit la version nulle part ailleurs.
- **Configuration.** Un fichier YAML de speech-to-speech (`schema_version: 1`), monté par Dokploy (`../files/conversation.yaml`), lancé par `speech-to-speech serve -f /config/conversation.yaml`. Les clés passent par des références `{env: NOM}`, résolues depuis le `.env` du compose. Le fichier ne contient aucun secret.
  - VAD : `silero`, avec Smart Turn actif (réglage par défaut).
  - STT : backend `openai`, `openai_stt_base_url` vers LiteLLM, `openai_stt_model: transcribe`, `openai_stt_api_key: {env: CONVERSATION_LITELLM_KEY}`.
  - LLM : backend `chat-completions`, `model_name: medium`, URL et clé LiteLLM. Les instructions viennent d'Open WebUI par `session.update`.
  - TTS : backend `openai`, `openai_tts_model: speech`, `openai_tts_voice_override: fr_marie_neutral`, `openai_tts_response_format: pcm` et le taux d'échantillonnage du PCM que renvoie Voxtral, à mesurer. Le réglage `voice_override` rend sans effet la voix choisie dans Open WebUI.
  - Serveur : `host: 0.0.0.0`, port `8765`.
- **Adresse de LiteLLM.** L'adresse publique `https://litellm.supergeoff.top/v1`, comme le fait dictee depuis la sidebox. Le builder relit la valeur de `LITELLM_URL` dans le `.env` de dictee et prend la même base.
- **Clé LiteLLM.** Une nouvelle clé nommée `conversation`, limitée aux trois alias, pour attribuer ses appels dans Phoenix. L'identité de l'utilisateur d'Open WebUI ne descend pas jusqu'à LiteLLM : l'attribution se fait par clé.
- **Caches.** Silero et Smart Turn se téléchargent au premier lancement. Un volume nommé `conversation-cache` monté sur le cache de l'utilisateur du conteneur évite de les retélécharger.
- **Ressources.** Limite mémoire de départ : 1,5 Go, ajustée après mesure. Le builder vérifie la mémoire libre de sideboxnode avant le premier déploiement.
- **Réseau.** Aucun domaine : `conversation-gate` le joint par son nom dans le réseau du compose.

### Le service `conversation-gate`

- Image `caddy` officielle, tag explicite, inscrite dans `images` du catalogue de l'updater.
- Un Caddyfile monté (`../files/conversation.Caddyfile`). Il écoute en HTTP sur le port 8080, refuse avec 401 toute requête dont l'en-tête `Authorization` ne vaut pas `Bearer {env.CONVERSATION_GATE_KEY}`, puis transmet tout le reste, WebSocket compris, à `conversation:8765`. La clé vit dans le `.env`.
- Le domaine `conversation.supergeoff.top` (HTTPS, letsencrypt) pointe sur ce service, port 8080, sans middleware.

### Le `.env` du compose

Trois variables s'ajoutent : `CONVERSATION_LITELLM_KEY` (la clé LiteLLM `conversation`), `CONVERSATION_GATE_KEY` (une valeur aléatoire générée au déploiement) et l'adresse LiteLLM si elle n'est pas déjà dans le `.env`. Le `.env` réécrit reste trié par nom. Seules ces variables changent, et on le vérifie par empreintes.

### LiteLLM (`litellmconfig`)

Deux entrées dans `mounts/config.yaml`, par une PR que Geoff fusionne, avec les tests du dépôt.

- `transcribe` : `model: openai/mistralai/voxtral-mini-transcribe-3-0`, `api_base` d'OpenRouter, clé OpenRouter du compte pro comme les autres alias, `model_info.mode: audio_transcription`.
- `speech` : `model: openai/mistralai/voxtral-mini-tts-3-0`, même `api_base` et même clé, `model_info.mode: audio_speech`.
- Chaque alias déclare son prix, pour que LiteLLM facture le bon coût. Le builder suit la façon de faire des alias d'image.

Le builder vérifie par un vrai appel que LiteLLM transmet `response_format: pcm` et le champ `stream_format` à OpenRouter, et que l'audio de `speech` arrive en flux et non d'un bloc. S'il arrive d'un bloc, il mesure le délai ajouté et le rapporte à Geoff avant de continuer.

### Open WebUI

Dans Admin > Audio > Appels vocaux, en mode Realtime :

- base URL `https://conversation.supergeoff.top/v1` et la clé du gardien ;
- modèle vocal `medium` (le serveur ignore ce champ, mais Open WebUI l'exige) ;
- voix `fr_marie_neutral` ;
- transcription `transcribe`.

Le builder relève la configuration audio actuelle avant de l'écrire, pour pouvoir y revenir.

## Le test de compatibilité

Avant de brancher Open WebUI, un script (`s2s/compat_test.py`) se connecte au serveur déployé à travers le gardien et rejoue les événements du bridge d'Open WebUI, lus dans `backend/open_webui/routers/audio/realtime.py` et `src/lib/utils/realtime.ts`. La production tourne en 0.13.4 et l'updater passera en 0.13.5 : le builder compare ces deux fichiers entre les deux versions et couvre les deux s'ils diffèrent.


1. Une connexion sans clé est refusée par le gardien.
2. `session.update` avec `type: realtime`, sortie audio PCM 24 kHz, `turn_detection` `server_vad` avec `create_response: false` et `interrupt_response: false`, les outils `generate_chat_completion` et `play_animation` : le serveur répond `session.updated` sans erreur.
3. Un fichier audio français est envoyé en `input_audio_buffer.append` : le serveur renvoie `conversation.item.input_audio_transcription.completed` avec un texte en français, et ne lance aucune réponse de lui-même.
4. `response.create` : le serveur renvoie des `response.output_audio.delta`. Le script vérifie que l'audio est bien du PCM 24 kHz, en mesurant sa durée par rapport à celle du texte prononcé.
5. Un instantané de contexte comme celui du bridge (`conversation.item.delete` puis `conversation.item.create`) est accepté.
6. Une réponse hors conversation (`conversation: none`, `input: []`, `tools: []`) est acceptée.
7. Une question posée pendant un appel d'outil en cours ne ferme pas la session.
8. Un arrêt de l'envoi d'audio pendant 30 secondes, comme un micro coupé, ne ferme pas la session.
9. Le script mesure le délai entre la fin de l'audio envoyé et le premier `response.output_audio.delta`, sur cinq essais.

Un point qui échoue arrête le chantier : le builder rapporte l'événement exact et la réponse du serveur à Geoff, sans patcher le code. Le point 9 ne bloque rien : la mesure est rapportée telle quelle, et c'est le vrai appel de Geoff à l'étape 7 qui juge la réactivité. Le builder ne cherche pas à optimiser avant ce retour. Si Geoff trouve l'appel trop lent, les premiers réglages à essayer sont `speculative_reopen_ms` et Smart Turn, que le bridge d'Open WebUI attend avant de demander la réponse (choix de Claude).

## Accès du builder

Les lectures sont libres, et chaque écriture attend l'accord de Geoff. Les secrets se lisent par le skill bitwarden, avec `run` et jamais `get` quand c'est possible.

| Cible | Accès |
|---|---|
| Dokploy (compose, `.env`, montages, domaine) | API directe avec le secret `DOKPLOY` et `dokployconfig/lib/dokploy.py`, selon `conventions-compose.md` (empreintes seulement, jamais un `.env` affiché). |
| Clés LiteLLM | API d'administration avec la master key (secret `LITELLM`). |
| Open WebUI | API d'administration avec le secret `OPENWEBUI`. |
| Dépôts | La devbox, une branche par dépôt et une PR ; Geoff fusionne. |
| DNS | Geoff seul. |

## Ordre de mise en service

1. PR `litellmconfig` avec les deux alias, puis la clé `conversation`, et un essai de chaque alias par un vrai appel.
2. PR `dictee-server` avec `s2s/` (Dockerfile, script de test, README court), sur la branche `feat/mode-conversation`. Elle doit être fusionnée sur `main` avant l'étape 5, puisque Dokploy construit l'image depuis `#main:s2s`.
3. PR `dokployconfig` : version et image au catalogue de l'updater.
4. Enregistrement DNS par Geoff.
5. Compose `dictee` : deux services, deux montages de fichiers, trois variables du `.env`, le domaine. Un seul redéploiement, puis vérification que `nemo` et `dictee` restent sains et inchangés. Le redéploiement peut relancer `nemo` et couper la dictée quelques minutes : c'est accepté sur cette stack personnelle, sans fenêtre de déploiement (choix de Claude). Si la mémoire libre de sideboxnode ne couvre pas la limite du service, le builder s'arrête et rapporte, sans toucher à un autre service (choix de Claude).
6. Test de compatibilité.
7. Réglages d'Open WebUI, puis un vrai appel de Geoff depuis son téléphone.

## Retour arrière

Remettre Open WebUI en mode d'appel standard suffit à revenir à l'état actuel. Pour retirer le service, on supprime les deux services, leurs montages, leurs variables et le domaine, et dictee n'a jamais changé.

## Ce qui reste au builder

- La syntaxe exacte du Caddyfile et le chemin du cache dans le conteneur.
- Le healthcheck des deux services (le serveur speech-to-speech n'annonce pas de route de santé : une connexion TCP sur 8765 suffit).
- Le taux d'échantillonnage du PCM de Voxtral TTS chez OpenRouter, à mesurer par un appel.
- Le prix déclaré des deux alias, relevé sur les pages d'OpenRouter.
- La forme de la section `pins` du catalogue pour la version de speech-to-speech (releases GitHub `huggingface/speech-to-speech`).
