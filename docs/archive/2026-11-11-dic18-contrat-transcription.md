# Plan de correction du contrat de transcription

> Ce plan est conservé comme document historique après la livraison. Les cases reproduisent le plan initial et ne constituent pas un suivi de l'exécution.

**Objectif :** rendre un VTT à dictee-app même lorsque son envoi rejoint une tâche créée dans un autre format, sans retranscrire l'audio.

**Architecture :** la tâche et son résultat restent partagés et inchangés. Le POST en arrière-plan choisit le rendu de sa propre réponse, et le GET accepte un format facultatif. Le GET sans paramètre conserve le rendu enregistré à la création.

**Technologies :** Python 3.13, Starlette, SQLite, pytest, faux NeMo et faux LiteLLM existants.

**Spec :** `docs/superpowers/specs/2026-11-11-dic18-contrat-transcription-design.md`.

## Contraintes générales

- Le travail porte sur `dictee-server` seulement ; DIC-4 adaptera l'application séparément.
- Aucune migration ni modification du format ou des granularités enregistrés dans une tâche existante.
- Une tâche identique reste partagée ; le format demandé ne déclenche pas un nouvel appel à NeMo.
- Les tests et les exemples du dépôt public utilisent des données synthétiques, jamais les MP3 ou transcriptions réels.
- La rétention actuelle reste à 24 heures et la force du glossaire à sa valeur par défaut ; aucune configuration de production ne change.
- Tout déploiement, toute modification de Traefik et toute autre annulation en production attendent un nouvel accord explicite.
- Les commandes de référence sont celles de `AGENTS.md` : `make test` pour la suite. `make integration` nécessite les modèles, `NEMO_VERSION` et l'accord préalable avant de démarrer la stack de test.

## Points de revue couverts par les tests

1. Un POST rejoint une tâche déjà finie : il rend immédiatement le VTT demandé, sans nouvel appel à NeMo.
2. Deux clients relisent une tâche avec des formats différents : leurs réponses ne changent ni les paramètres enregistrés ni la réponse historique sans paramètre.
3. Un appelant sans droit demande un format invalide : il ne doit pas apprendre si la tâche existe.
4. Une tâche brute ou reformulée reçoit une demande de format incompatible avec son traitement : le serveur refuse plutôt que de prétendre avoir calculé les locuteurs ou une transcription brute différente.
5. Une tâche est encore en cours, en échec ou annulée : le choix de rendu ne fabrique aucun résultat et ne modifie ni son état ni son erreur.

## Fichiers et responsabilités

- `dictee/src/dictee/tasks_api.py` : rendu paramétrable de l'objet tâche, lecture des options GET, contrôle du mode et de l'accès.
- `dictee/src/dictee/transcribe.py` : transmission du format et des granularités de la requête au rendu du POST en arrière-plan.
- `dictee/tests/test_tasks_api.py` : GET, validation, compatibilité historique, droits et états.
- `dictee/tests/test_transcriptions.py` : jonction des POST et absence de nouvelle transcription.
- `README.md` et `docs/superpowers/specs/2026-10-31-dictee-server-design.md` : contrat livré et exemple de parcours VTT.

---

### Task 1: Préparer l'exécution et établir la référence

**Fichiers :** aucun fichier de production modifié.

**Entrées :** la spec et ce plan, relus et approuvés. Le clone local est sur `fix/task-output-format`, avec la spec non suivie ; aucun changement de code n'a été observé.

- [ ] Vérifier `git status --short --branch` et la disponibilité de Podman ou Docker. Établir un espace isolé avec le skill `using-git-worktrees`, après accord sur sa création, sans perdre les fichiers de spec et de plan.
- [ ] Lancer `make test` à la racine du dépôt serveur. Attendu : la suite existante réussit. La commande construit une image de test qui contient Python 3.13 et ffmpeg ; ni l'un ni l'autre n'est présent comme outil local.
- [ ] Si la construction ou un test échoue, conserver la sortie, identifier la cause et la rapporter avant toute modification du code. Ne pas attribuer l'échec à la correction qui n'existe pas encore.

### Task 2: Choisir le rendu pour chaque requête

**Fichiers :**
- Modifier : `dictee/src/dictee/tasks_api.py`.
- Modifier : `dictee/src/dictee/transcribe.py`.
- Tester : `dictee/tests/test_tasks_api.py` et `dictee/tests/test_transcriptions.py`.

**Interfaces :**
- Consomme : `TaskRecord`, `TaskResult.transcript()`, `render_body`, `Mode`, `RESPONSE_FORMATS` et les granularités existantes `word` et `segment`.
- Produit : `task_object(task: TaskRecord, *, response_format: str | None = None, granularities: frozenset[str] | None = None) -> dict`. Sans option, cette fonction conserve le comportement actuel.
- Produit : `GET /v1/audio/transcriptions/{id}?response_format=vtt`, qui rend toujours un objet tâche JSON dont `output` est une chaîne VTT une fois la tâche finie.

- [ ] Écrire ce test de régression dans `dictee/tests/test_transcriptions.py` :

```python
def test_background_rejoin_renders_requested_vtt_without_transcribing_again(ask, fake_nemo):
    first = ask(model="dictee-diarize", response_format="json").json()
    assert first["text"]
    calls = len(fake_nemo.calls)
    joined = ask(model="dictee-diarize", response_format="vtt", background="true").json()
    assert joined["status"] == "completed"
    assert joined["output"].startswith("WEBVTT\n")
    assert "<v A>" in joined["output"]
    assert len(fake_nemo.calls) == calls
```

- [ ] Écrire dans `dictee/tests/test_tasks_api.py` un test qui crée une tâche `Mode.DIARIZE` en `json`, la termine avec la fixture synthétique `RESULT`, puis demande `response_format=vtt`. Vérifier HTTP 200, l'en-tête `WEBVTT` et les balises `<v A>` et `<v B>` dans `output`. Demander ensuite le GET sans paramètre et vérifier que son `output` reste un objet JSON et que `store.get(id).response_format` reste `json`.
- [ ] Ajouter les tests suivants avant le code :
  - `test_background_rejoin_vtt_while_running` : une tâche `in_progress` rejointe en VTT conserve son identifiant et `output=null` ; après libération du faux NeMo, GET VTT retourne les balises de locuteur, avec un seul appel à NeMo ;
  - `test_get_alternate_formats_keeps_stored_settings` : JSON, VTT puis JSON pour une même tâche terminée produisent le format demandé sans changer `response_format`, `granularities`, `id` ni `expires_at` ;
  - `test_get_rejects_formats_incompatible_with_processing` : une tâche brute demandée en `diarized_json` et une tâche reformulée demandée en `vtt` reçoivent HTTP 400 ; leurs formats compatibles restent lisibles ;
  - `test_get_verbose_json_granularities` : `verbose_json` rend `words` avec `word`, et `segments` avec `segment` ; la lecture sans options garde les granularités de création ;
  - `test_get_options_validate_without_disclosing_task` : un format inconnu et une granularité invalide reçoivent HTTP 400 ; un identifiant inconnu, expiré ou appartenant à un autre compte reçoit 404 même avec un format invalide ; une requête non authentifiée reçoit 401 ;
  - `test_get_vtt_keeps_unfinished_and_terminal_states` : les tâches `queued`, `failed` et `cancelled` gardent `output=null` et leurs statuts et erreurs ; l'annulation d'une tâche terminée reste inchangée.
- [ ] Exécuter `make test ARGS="tests/test_transcriptions.py::test_background_rejoin_renders_requested_vtt_without_transcribing_again tests/test_tasks_api.py::<nom_du_test_GET>"`. Attendu : les deux tests échouent parce que le serveur rend le format initial, pas à cause d'une erreur de préparation. Remplacer `<nom_du_test_GET>` par le nom choisi au point précédent.
- [ ] Ajouter le rendu optionnel à `task_object` et passer les options de la requête courante dans la réponse du POST en arrière-plan. Garder le traitement synchrone et les valeurs enregistrées inchangés.
- [ ] Dans `get_task`, identifier d'abord le propriétaire et vérifier la visibilité, puis lire les paramètres. `response_format` accepte les six valeurs de `RESPONSE_FORMATS` ; une valeur inconnue reçoit HTTP 400 avec `param=response_format`. Si le paramètre est absent, conserver le format de création. Pour `verbose_json`, accepter les paramètres répétés `timestamp_granularities[]=word` et `timestamp_granularities[]=segment` : sans granularité demandée, utiliser `segment` pour un format explicite, ou les granularités enregistrées quand le format est lui aussi absent. Refuser en HTTP 400 les granularités inconnues ou demandées pour un format autre que `verbose_json`.
- [ ] Valider le format explicitement demandé contre le `task.params.mode` figé : `DIARIZE` accepte les formats disponibles, `RAW` refuse `diarized_json`, et `CLEAN` accepte seulement `json` et `text`. Une combinaison incompatible reçoit HTTP 400, avec `param=response_format` et `code=incompatible_response_format`, sans lancer de traitement. Une réponse sans paramètre garde le comportement existant.
- [ ] Relancer les tests ajoutés, puis `make test`. Attendu : tous les tests passent, y compris ceux des clients synchrones et de l'annulation.
- [ ] Relire le diff. Enregistrer un commit local en français avec les fichiers de code et de tests seulement, selon les règles d'attribution de la session. Ne pas pousser ni fusionner.

### Task 3: Documenter et préparer la vérification réelle

**Fichiers :**
- Modifier : `README.md`.
- Modifier : `docs/superpowers/specs/2026-10-31-dictee-server-design.md`.

**Interfaces :** consomme le contrat et les tests de la tâche 2 ; ne change pas l'API.

- [ ] Mettre à jour les deux documents pour décrire le POST `background=true`, la lecture `GET …/{id}?response_format=vtt`, le GET sans paramètre, la jonction et les restrictions de format. Montrer un exemple `dictee-diarize`/VTT avec une clé factice et un fichier fictif. Préciser qu'une annulation arrête la tâche partagée.
- [ ] Vérifier que `README.md` et la spec décrivent les mêmes formats et que `git diff --check` ne signale aucune erreur. Relancer `make test` si cette tâche a modifié du code ou des tests.
- [ ] Relire l'ensemble des changements par rapport à la spec et au plan. Sans outil de sous-agent disponible, faire la revue dans cette session et indiquer qu'elle n'est pas indépendante.
- [ ] Déplacer le plan terminé dans `docs/archive/`, puis enregistrer un commit local qui comprend la documentation, la spec de cette carte et le plan archivé. Ne pas pousser, fusionner ou déployer.
- [ ] Pour fermer DIC-18, demander séparément l'autorisation de déployer le correctif. Après déploiement autorisé, renvoyer un MP3 de test autorisé d'abord en JSON, puis en VTT, et vérifier : même identifiant, VTT avec balises de voix dans le POST et le GET, état et progression, absence de nouveau traitement. Vérifier également `GET /v1/models` et les résultats de l'annulation déjà observés. Ne jamais afficher le contenu réel de la transcription.
- [ ] Vérifier la configuration Traefik si un accès en lecture à la configuration statique est disponible. Les mesures antérieures ont réussi à 130,9 s et échoué vers 182 s ; elles sont cohérentes avec 180 s mais ne prouvent pas la valeur configurée. Ne pas prétendre l'avoir lue si cet accès manque.
- [ ] Si la correction n'est pas déployée ou qu'un appel réel n'a pas été vérifié, garder DIC-18 ouvert et rapporter précisément ce qui reste.
