# Glossaire complémentaire par le champ `prompt`

## Intention

Geoff veut compléter, depuis l'app dictee sur son téléphone, le glossaire que son compte déclare sur dictee-server. Le complément s'ajoute toujours au glossaire du serveur : l'app n'offre ni remplacement ni effacement. Les deux produits restent indépendants. dictee-server sert tout front qui parle l'API audio d'OpenAI, et l'app dictee fonctionne avec tout back qui la parle. Le complément n'a d'effet complet qu'avec le combo des deux, mais son absence ou un back étranger ne provoque ni refus ni erreur.

## Objectifs et limites

- Objectif : un terme envoyé par l'app renforce la transcription NeMo et figure dans le glossaire de la reformulation, pour une requête directe, un flux, une tâche enregistrée et la dictée en direct.
- Non-objectif : retirer ou remplacer un terme du serveur, stocker le complément côté serveur, ajouter une route ou un champ maison.
- Anti-objectif : une requête standard qui échoue à cause du complément, ou un texte personnel qui apparaît dans les journaux.

## Approches

1. Champ standard `prompt` (retenue). Il existe dans `/v1/audio/transcriptions` et dans `input_audio_transcription.prompt` du Realtime. Un back OpenAI l'accepte et s'en sert déjà comme indice de vocabulaire, donc le réglage garde un effet hors du combo.
2. Champ de formulaire maison `glossary`. Un back strict peut refuser un champ inconnu : rejetée.
3. En-tête `X-Dictee-Glossary`. Il est ignoré sans erreur ailleurs, mais n'y sert à rien, gère mal les accents et passe mal en WebSocket : rejetée.

## Conception

- Lecture : `prompt` se lit avec `parse_glossary`, un terme par ligne, avec la forme `Terme (souvent entendu : a, b)`. Un `prompt` libre venu d'un autre front devient au pire quelques termes sans effet nuisible.
- Bornes : les limites de la page (`MAX_GLOSSARY_CHARS`, `MAX_GLOSSARY_LINES`) s'appliquent au `prompt`. Au-delà, la requête reçoit une erreur 400 au format OpenAI avec `param: "prompt"`.
- Fusion : le glossaire effectif est celui du profil suivi des entrées du `prompt` dont le terme n'y figure pas déjà. Un appelant sans compte reçoit le seul complément.
- Usage : le glossaire fusionné remplace celui du profil partout où il sert aujourd'hui, c'est-à-dire le boost NeMo, le glossaire de la reformulation et les `TaskParams` d'une tâche enregistrée (qui gardent donc le complément jusqu'à la fin de la tâche).
- Direct : `input_audio_transcription.prompt` (et `transcription.prompt` de la session) suit la même lecture. Les termes s'ajoutent à ceux du profil avant les `keywords`, sans doublon.
- Confidentialité : le texte brut du `prompt` ne part jamais vers NeMo et n'entre jamais dans les journaux ni dans la trace.
- App dictee : un champ « Glossaire complémentaire » envoyé tel quel dans `prompt`, hors de ce dépôt.

## Laissé au constructeur

Le découpage du code (fonction de fusion dans `profile.py` par exemple), les tests avec le faux NeMo et le faux LiteLLM, et la mise à jour des docstrings de `transcribe.py` et `live.py` qui disent aujourd'hui que `prompt` est ignoré.
