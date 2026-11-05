# dictee-server : règles du dépôt

Ce dépôt porte dictee, le point d'entrée audio de la stack de Geoff, et le compose Dokploy qui le fait tourner avec NeMo-Speech.cpp. Les specs en cours sont `docs/superpowers/specs/2026-10-31-dictee-server-design.md`, la référence de ce que dictee fait, et `docs/superpowers/specs/2026-11-01-taches-transcription-design.md`, qui garde l'histoire des tâches enregistrées, du découpage en morceaux et de la dictée en direct.

## Principes

- dictee reste compatible avec l'API audio d'OpenAI. Une app standard doit fonctionner sans réglage particulier, et aucune route maison ne remplace une route standard.
- Les fonctions natives de NeMo-Speech.cpp passent d'abord. dictee n'ajoute que ce que NeMo n'a pas : comptes et clés, réglages de chacun, conversion audio, reformulation, tâches enregistrées, découpage en morceaux et traduction du Realtime d'OpenAI vers la route temps réel de NeMo.
- Rien de personnel dans le dépôt, qui est public : ni glossaire, ni consignes propres à quelqu'un, ni email. Les valeurs par défaut restent neutres, et chacun règle son espace sur la page.
- Aucun secret dans le dépôt. Le `.env` reste dans Dokploy.
- dictee garde l'audio d'une tâche jusqu'à sa fin et son texte 24 heures après, et ses journaux n'en contiennent jamais.
- Les versions sont des tags explicites. La version de NeMo-Speech.cpp est un argument de build du compose, et ce dépôt ne la répète nulle part ailleurs.

## Méthode

- Le travail suit le plan tâche par tâche, dans l'ordre. Chaque tâche commence par un test qui échoue, puis le code, puis le test qui passe, puis un commit.
- Une tâche ne présuppose jamais le résultat d'une tâche précédente sans l'avoir vérifié. Quand un critère d'abandon est atteint, on s'arrête et on rapporte à Geoff.
- On n'annonce jamais qu'une tâche est terminée sans une preuve observée : test lancé, fichier relu, appel exécuté.
- Le travail se fait sur une branche, et Geoff fusionne lui-même sur `main`. Dokploy construit depuis `main`, donc une fusion change ce que le prochain déploiement construit.
- Toute écriture dans Dokploy, PocketID, Open WebUI, LiteLLM ou GitHub hors du dépôt attend l'accord explicite de Geoff. Les lectures sont libres.

## Commandes

| Commande | Effet |
|---|---|
| `make test` | Lance `pytest`, avec un faux NeMo et un faux LiteLLM, sans réseau. |
| `make integration` | Lance le compose en local avec les vrais modèles sur de courts extraits audio. Elle ne tourne pas dans la CI. |

## Écriture

- Documentation et messages de commit en français, en phrases complètes. Code et termes techniques en anglais.
- Les commits se terminent par les lignes d'attribution demandées par la session.
