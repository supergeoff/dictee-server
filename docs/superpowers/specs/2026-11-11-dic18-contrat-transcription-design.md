# Contrat de transcription pour dictee-app

## Intention

DIC-18 vérifie le contrat de dictee-server avant l'implémentation de DIC-4. Un client doit récupérer un VTT avec locuteurs sans perdre le bénéfice de la jonction d'un fichier déjà envoyé. Cette modification ne construit pas la fonctionnalité Android.

## Constat

Une transcription est conservée sous forme neutre et les envois identiques rejoignent la même tâche. Pourtant, l'objet tâche utilise toujours le format enregistré à sa création. En production, un MP3 envoyé en JSON, puis renvoyé avec `background=true` et `response_format=vtt`, rejoint la tâche mais reçoit un objet JSON. Le GET de la tâche ignore actuellement `response_format`.

## Approches

- Une tâche par format simplifierait la lecture, mais pourrait retranscrire le même MP3. Cette approche est écartée.
- Modifier le format stocké lorsqu'un envoi rejoint une tâche changerait les réponses des autres demandes sur cette tâche. Cette approche est écartée.
- Garder une seule tâche et choisir le rendu pour chaque requête conserve la jonction et ne change pas le format enregistré. Cette approche est proposée.

## Contrat proposé

- Un POST avec `background=true` rend son objet tâche dans le format et les granularités de la requête courante. Si la tâche rejointe est déjà terminée, `output` utilise ce format immédiatement. Si elle n'est pas terminée, `output` reste `null`.
- `GET /v1/audio/transcriptions/{id}?response_format=vtt` rend l'objet tâche dont `output` est un texte VTT quand la tâche est terminée. Les autres formats acceptés par le serveur sont disponibles sous les restrictions de traitement ci-dessous. Une requête GET peut préciser les granularités si elle demande `verbose_json`.
- Sans paramètre de format ni granularité, GET conserve le comportement actuel : l'objet est rendu selon les réglages enregistrés à la création de la tâche. Les tâches existantes restent lisibles, sans migration de base.
- Le format de chaque requête est utilisé pour son seul rendu. Il ne modifie ni les champs enregistrés de la tâche, ni le traitement, ni la durée de conservation. L'annulation reste liée à la tâche partagée et conserve son contrat actuel.
- Une lecture choisit la présentation d'un résultat déjà calculé ; elle ne lance ni diarisation ni reformulation. Une tâche sans locuteurs ne peut pas produire un `diarized_json` qui prétend les contenir. Une tâche reformulée ne peut pas être présentée comme la transcription brute horodatée d'un traitement non reformulé. Ces demandes incompatibles reçoivent une erreur explicite, plutôt qu'un résultat trompeur.
- Pour une tâche avec locuteurs, les formats JSON et VTT utilisent le même résultat, sans nouvel appel de transcription. Pour une tâche brute ou reformulée, le rendu respecte les données effectivement calculées.
- Les formats inconnus reçoivent une erreur 400. La vérification du propriétaire et de l'expiration de la tâche précède la production de tout résultat ; une tâche inconnue, expirée ou appartenant à un autre compte reste invisible.

## Vérifications

Les tests couvrent le POST qui rejoint une tâche en cours puis une tâche terminée, les GET alternant JSON et VTT sur le même identifiant, l'absence de nouvel appel de transcription, les anciennes lectures sans paramètre, les granularités et les formats incompatibles. Ils couvrent aussi l'authentification, l'expiration, les états d'échec et l'annulation, sans modifier les données partagées.

Le README décrit les champs et les exemples du parcours VTT. Après les tests locaux et un déploiement séparément autorisé, les appels réels en production sont rejoués avec les MP3 de test autorisés. Le résultat doit montrer la même tâche et un VTT avec locuteurs, y compris après une première demande en JSON.

## Hors du changement de code

Le glossaire conserve la valeur par défaut du serveur, conformément à la demande de Geoff. La conservation actuellement observée est de 24 heures ; DIC-4 prévoit de renvoyer le MP3 si le résultat a expiré. La valeur de rétention ne change pas sans décision.

Le délai Traefik et l'annulation d'une tâche en cours sont vérifiés séparément. Toute modification de configuration, tout redémarrage ou déploiement en production et toute annulation d'une tâche de test en cours attendent un accord explicite.
