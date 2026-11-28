# dictee-server : règles du dépôt

Ce dépôt porte dictee, le point d'entrée audio de la stack de Geoff, et l'image NeMo-Speech.cpp qui l'accompagne. Le compose qui les fait tourner vit dans Dokploy, seule source des composes : le dépôt n'en garde qu'un compose de test, `compose.integration.yaml`. Les specs en cours sont `docs/superpowers/specs/2026-10-31-dictee-server-design.md`, la référence de ce que dictee fait, `docs/superpowers/specs/2026-11-01-taches-transcription-design.md`, qui garde l'histoire des tâches enregistrées, du découpage en morceaux et de la dictée en direct, et `docs/superpowers/specs/2026-11-28-moteurs-audio-design.md`, qui décrit les moteurs par rôle (transcription, dictée en direct, voix) et leur bascule.

## Principes

- dictee reste compatible avec l'API audio d'OpenAI. Une app standard doit fonctionner sans réglage particulier, et aucune route maison ne remplace une route standard.
- Chaque rôle audio (transcription de fichier, dictée en direct, voix) passe par un moteur que `STT_ENGINE`, `LIVE_ENGINE` et `TTS_ENGINE` choisissent, et les routes ne dépendent d'aucun moteur en particulier. Les fonctions natives du moteur de chaque rôle passent d'abord. dictee n'ajoute que ce que le moteur n'a pas : comptes et clés, réglages de chacun, conversion audio, reformulation, glossaire (CrispASR n'en a pas qui marche), tâches enregistrées, découpage en morceaux et traduction du Realtime d'OpenAI vers la dictée en direct du moteur.
- Rien de personnel dans le dépôt, qui est public : ni glossaire, ni consignes propres à quelqu'un, ni email. Les valeurs par défaut restent neutres, et chacun règle son espace sur la page.
- Aucun secret dans le dépôt. Le `.env` reste dans Dokploy.
- dictee garde l'audio d'une tâche jusqu'à sa fin et son texte 24 heures après, et ses journaux n'en contiennent jamais.
- Les versions sont des tags explicites. La version de CrispASR est un argument de build du compose de Dokploy, et le dépôt ne l'écrit nulle part : `make integration` la reçoit de la variable `CRISPASR_VERSION` (`CRISPASR_VERSION=<version> make integration`). Il en va de même pour la version de NeMo-Speech.cpp tant que NeMo existe : `make nemo-up` la reçoit de `NEMO_VERSION`. L'updater de dokployconfig peut changer ces valeurs dans Dokploy : on relit alors la valeur du compose avant de lancer la commande.

## Méthode

- Le travail suit le plan tâche par tâche, dans l'ordre. Chaque tâche commence par un test qui échoue, puis le code, puis le test qui passe, puis un commit.
- Une tâche ne présuppose jamais le résultat d'une tâche précédente sans l'avoir vérifié. Quand un critère d'abandon est atteint, on s'arrête et on rapporte à Geoff.
- On n'annonce jamais qu'une tâche est terminée sans une preuve observée : test lancé, fichier relu, appel exécuté.
- Le travail se fait sur une branche, et Geoff fusionne lui-même sur `main`. Dokploy construit les images depuis `main`, donc une fusion change ce que le prochain déploiement construit, mais elle ne déploie rien et ne change pas le compose : un changement de compose se fait dans Dokploy.
- Toute écriture dans Dokploy, PocketID, Open WebUI, LiteLLM ou GitHub hors du dépôt attend l'accord explicite de Geoff. Les lectures sont libres.

## Commandes

| Commande | Effet |
|---|---|
| `make test` | Lance `pytest`, avec de faux moteurs (NeMo, CrispASR, serveur de voix) et un faux LiteLLM, sans réseau. |
| `S2S_REF=<ref> make s2s-check` | Construit l'image de `s2s/` (serveur speech-to-speech du mode conversation d'Open WebUI), valide `s2s/conversation.yaml` dans l'image, puis construit le relais `s2s/gate` et lance ses tests dans son image. `s2s/` est la source du montage de `conversation.yaml` du compose de Dokploy, et `s2s/gate` celle de l'image du relais. |
| `make integration` | Demande les variables `CRISPASR_VERSION` (voir Principes), `PTTS_MODEL` (`hf://gradium/phonon-40m-fr@<commit>`) et `HF_TOKEN`, que `integration/make-env.sh` lit pour fabriquer le `.env`, mais plus `NEMO_VERSION`. Lance le compose de test en local avec les vrais modèles sur de courts extraits audio. Elle se joue à la demande, pas à chaque changement, et ne tourne pas dans la CI. |
| `make nemo-up` | Demande la variable `NEMO_VERSION`. Démarre seulement les services `models` et `nemo` du compose de test, sans dictee. |

## Écriture

- Documentation et messages de commit en français, en phrases complètes. Code et termes techniques en anglais.
- Les commits se terminent par les lignes d'attribution demandées par la session.
