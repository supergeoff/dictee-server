# Langue automatique et début sans parole

## Intention

Les réunions enregistrées sont en français, en anglais ou dans les deux langues. dictee doit les transcrire sans que personne choisisse la langue avant l'envoi, et un enregistrement qui commence sans parole reconnue ne doit plus échouer.

## Constat

Les mesures datent du 14 novembre 2026, sur dictee en production avec NeMo-Speech.cpp 0.3.0, et sur un Mac avec le même modèle Nemotron 3.5.

- Sans champ `language`, dictee demande à NeMo la langue `fr-FR`. Nemotron 3.5 est conditionné par la langue demandée.
- Un enregistrement en anglais de 27 minutes, envoyé avec la langue française, ne rend aucun mot. Le même fichier envoyé avec `language=en` rend 2776 mots.
- Réglé sur le français, NeMo reste muet sur de la parole anglaise tant qu'il n'a émis aucun mot. Une fois lancé, il continue en anglais, avec des pertes.
- Sur un audio qui alterne une minute de français et une minute d'anglais, une langue fixe perd environ la moitié du texte de l'autre langue. La détection automatique du même modèle retrouve les deux langues (2220 caractères, contre 1377 en français fixe et 1427 en anglais fixe).
- Tant que NeMo n'a reconnu aucun mot, sa session temps réel n'envoie aucun événement. dictee abandonne après 300 secondes sans événement et rend `upstream_error`. Un audio fait de 15 minutes de silence suivies de parole échoue de cette façon.
- La documentation de NeMo 0.3.0 (`docs/asr/models.md`) donne la valeur `auto` pour la détection de la langue par le modèle. Cette valeur n'a pas encore été essayée sur NeMo-Speech.cpp : dictee la refuse aujourd'hui.
- dictee-app envoie toujours le champ `language`, lu dans un réglage libre qui vaut `fr` par défaut.

## Objectifs

- Une requête sans langue est transcrite dans la ou les langues parlées.
- Une app peut demander la détection en envoyant `language=auto`.
- Une langue envoyée par l'app reste respectée, comme aujourd'hui.
- Un audio sans parole reconnue au début va à son terme. Sans aucun mot, il rend un texte vide.

## Hors périmètre

- Le réglage de langue par personne sur la page de dictee.
- La langue de la synthèse vocale, qui reste le français.
- Le changement de la valeur par défaut dans dictee-app, qui relève de son dépôt.

## À éviter

- Une dictée courte en français qui sort dans une autre langue ou perd des mots à cause de la détection.
- Une route ou un champ maison : `language` reste le champ d'OpenAI, et son absence vaut détection comme chez OpenAI.

## Approches

- La détection automatique quand la requête ne donne pas de langue ne demande rien aux apps et suit le comportement d'OpenAI. Geoff a retenu cette approche le 14 novembre 2026.
- Un réglage par personne sur la page de dictee donnerait la main sur chaque usage, mais il ajoute une page, une colonne en base et leurs tests. Cette approche est écartée.
- Le choix de la langue dans dictee-app ne sert pas les autres outils au format OpenAI et ajoute un choix à chaque enregistrement. Cette approche est écartée, sauf la valeur `auto`, que dictee doit accepter pour que dictee-app puisse la demander.

## Conception

### Règle de langue

- Sans champ `language`, ou avec `language=auto` dans n'importe quelle casse, dictee envoie `auto` à NeMo.
- Avec un code ISO-639-1 ou régional, dictee convertit et envoie la langue comme aujourd'hui.
- La règle vaut pour les tâches, pour `stream=true` et pour la dictée en direct (`/v1/realtime`), où elle remplace le français par défaut.
- Une tâche enregistre `auto` comme langue. Deux envois identiques dont l'un demande `auto` et l'autre `fr` restent deux tâches distinctes, parce que la langue fait déjà partie de leur identité.

### Champ `language` des réponses

Ce point est un choix de conception, non une décision de Geoff. Quand la langue est détectée, le champ `language` de `verbose_json` et de `diarized_json` vaut `und`, le code ISO d'une langue indéterminée. La session temps réel de NeMo 0.3.0 ne rapporte pas la langue qu'elle détecte, et garder celle que la route REST rapporte demanderait une colonne de plus par morceau en base : dictee ne la rend donc pas. La dictée en direct annonce une langue nulle dans sa session quand elle détecte.

### Délai sans événement

Pendant l'envoi de l'audio, une session temps réel est tenue pour vivante tant que NeMo accepte des trames. Le délai de 300 secondes ne court donc que depuis la dernière trame acceptée ou le dernier événement reçu, selon le plus récent. Après le commit, le délai entre deux événements s'applique comme aujourd'hui. Un NeMo bloqué, qui ne lit plus et n'émet plus, donne toujours `upstream_error` après 300 secondes.

### Documentation

Le README et la spec de référence (`2026-10-31-dictee-server-design.md`, sections sur `language`, la dictée en direct et les délais) décrivent la nouvelle règle.

## Vérifications avant le code

La première tâche du plan lance NeMo seul (`make nemo-up`, avec la version du compose de Dokploy) et consigne ses résultats. Les audios sont de synthèse, produits par les voix de NeMo, sans enregistrement réel.

| Vérification | Suite |
|---|---|
| NeMo accepte `auto` sur la route REST et sur la session temps réel, avec les locuteurs. | Sinon, le travail s'arrête et Geoff est prévenu. |
| Un audio qui alterne français et anglais rend du texte dans chaque langue avec `auto`. Le texte obtenu avec la bonne langue fixe sert de référence. | Sinon, le travail s'arrête et Geoff est prévenu. |
| Un audio en anglais rend ses premiers mots avec `auto`, sans départ muet. | Sinon, le travail s'arrête et Geoff est prévenu. |
| Des dictées courtes en français rendent avec `auto` le même texte qu'avec `fr`. | Sinon, `auto` par défaut se limite au modèle `dictee-diarize`, et les autres modèles gardent le français par défaut. |

## Tests

Chaque comportement naît d'un test qui échoue, avec le faux NeMo du dépôt.

- Une requête sans langue, puis une requête `language=auto`, envoient `auto` à NeMo, par les morceaux, par la session et par la dictée en direct.
- Une requête `language=en` envoie `en-US`, comme aujourd'hui.
- Une session qui accepte l'audio pendant plus que le délai sans émettre d'événement va à son terme.
- Une session qui cesse de lire l'audio et d'émettre échoue après le délai.
- Un audio sans aucun mot reconnu rend un texte vide, et sa tâche est `completed`.

## Laissé au réalisateur

- La forme interne de la valeur `auto` dans `modes.py` et dans le registre des tâches.
- La façon de mesurer la dernière trame acceptée dans `NemoClient.transcribe`.
- Le choix d'un contrôle d'intégration pour l'anglais dans `integration/run.py`.

## Suite hors du dépôt

Après le déploiement, le réglage « langue » de la transcription dans dictee-app passe de `fr` à `auto`. Ce réglage est un champ libre de l'app : aucun changement de code n'est nécessaire pour l'utiliser.
