# Glossaire complémentaire par `prompt` : plan d'implémentation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal :** le champ standard `prompt` complète le glossaire du compte, en transcription comme en direct.

**Architecture :** `profile.py` porte la lecture, la borne et la fusion. `transcribe.py` fusionne le `prompt` dans le `Profile` avant tout usage, ce qui couvre NeMo, la reformulation et les `TaskParams`. `live.py` lit `transcription.prompt` dans `_Config` et l'ajoute aux phrases de NeMo.

**Tech Stack :** Python, FastAPI, pytest (faux NeMo, faux LiteLLM).

**Spec :** `docs/superpowers/specs/2026-11-18-glossaire-prompt-design.md`

## Global Constraints

- Bornes du complément : 20 000 caractères et 500 lignes, les mêmes constantes que la page.
- Erreur de dépassement : 400 au format OpenAI, `code="invalid_value"`, `param="prompt"` (en direct : `param="session.audio.input.transcription.prompt"`).
- Fusion : glossaire du profil d'abord, puis les entrées du `prompt` dont le terme n'y est pas, dans leur ordre, sans doublon.
- Le texte du `prompt` n'entre jamais dans les journaux, la trace ni les options NeMo autres que `phrases`.
- `make test` reste vert à chaque commit. Commits en français avec les lignes d'attribution de la session.

## Review Focus

- `prompt` vide ou fait d'espaces : rien ne change, et une tâche identique sans `prompt` est retrouvée (Task 2, `test_blank_prompt_joins_the_same_task`).
- Phrase libre d'un autre front (« Bonjour, voici une réunion. ») : acceptée, 200, elle devient un terme (Task 2, `test_free_prompt_is_accepted`).
- Terme déjà dans le glossaire du compte : pas de doublon dans `phrases` (Task 1 et Task 3).
- Appelant sans compte (clé de service) : reçoit le seul complément (Task 2).
- `prompt` non textuel en direct (nombre, liste) : 400 sur le bon `param`, la session reste ouverte avec ses réglages (Task 3).

---

### Task 1 : lecture, borne et fusion dans `profile.py`

**Files :**
- Modify : `dictee/src/dictee/profile.py`, `dictee/src/dictee/web.py:48-49,227-232`
- Test : `dictee/tests/test_profile.py`

**Interfaces :**
- Produces : `MAX_GLOSSARY_CHARS = 20_000`, `MAX_GLOSSARY_LINES = 500`, `glossary_too_large(text: str) -> bool`, `merge_glossary(base: tuple[GlossaryEntry, ...], extra: str) -> tuple[GlossaryEntry, ...]`, `with_extra_glossary(profile: Profile, extra: str) -> Profile`.

- [ ] **Step 1 : tests qui échouent**
  - `test_merge_glossary_appends_new_terms` : `merge_glossary((GlossaryEntry("Nemotron"),), "Magpie\nNemotron\nK8s (souvent entendu : cassis)")` vaut `(GlossaryEntry("Nemotron"), GlossaryEntry("Magpie"), GlossaryEntry("K8s", ("cassis",)))`.
  - `test_merge_glossary_blank_keeps_base` : `merge_glossary(base, "  \n")` est `base`.
  - `test_glossary_too_large` : vrai pour `"a" * 20_001` et pour `"a\n" * 501`, faux pour `"a\n" * 500`.
  - `test_with_extra_glossary_keeps_instructions_and_voice`.
- [ ] **Step 2 :** `cd dictee && pytest tests/test_profile.py -v` échoue sur l'import.
- [ ] **Step 3 :** implémenter dans `profile.py` ; déplacer les deux constantes depuis `web.py`, qui les importe et utilise `glossary_too_large` dans `_checked_glossary` (message inchangé). Les doublons se comparent sur `term` exact.
- [ ] **Step 4 :** `make test` passe.
- [ ] **Step 5 :** commit « Ajoute la fusion du glossaire complémentaire ».

### Task 2 : `prompt` dans `/v1/audio/transcriptions`

**Files :**
- Modify : `dictee/src/dictee/transcribe.py` (`_Params`, `_parse_params` et sa docstring, `_handle`)
- Test : `dictee/tests/test_transcriptions.py`

**Interfaces :**
- Consumes : `glossary_too_large`, `with_extra_glossary` (Task 1).
- Produces : `_Params.prompt: str` (vide sans champ).

- [ ] **Step 1 : tests qui échouent**, sur le modèle de `test_user_glossary_becomes_boosted_phrases` :
  - `test_prompt_completes_the_user_glossary` : glossaire `"Nemotron\nMagpie"`, `extra_body={"prompt": "Kubernetes\nMagpie"}` donne `phrases == ["Nemotron", "Magpie", "Kubernetes"]`.
  - `test_prompt_alone_without_account` : clé de service, `prompt="Kubernetes"` donne `["Kubernetes"]`.
  - `test_prompt_reaches_the_cleanup_glossary` : modèle `dictee-clean`, le message système reçu par le faux LiteLLM contient `- Kubernetes`.
  - `test_too_large_prompt_is_a_400` : `"a" * 20_001` donne 400, `param == "prompt"`, `code == "invalid_value"`, et NeMo n'est pas appelé.
  - `test_another_prompt_makes_another_task` et `test_blank_prompt_joins_the_same_task`, sur le modèle de `test_another_glossary_makes_another_task`.
  - `test_free_prompt_is_accepted` : 200 avec `"Bonjour, voici une réunion."`.
  - `test_prompt_never_reaches_the_logs` : avec `caplog` au niveau DEBUG, `"Kubernetes"` est absent de `caplog.text`.
- [ ] **Step 2 :** `pytest tests/test_transcriptions.py -k prompt -v` échoue.
- [ ] **Step 3 :** `_parse_params` lit `_first(fields, "prompt") or ""` et lève `ApiError(400, "prompt is limited to 20000 characters and 500 lines.", param="prompt", code="invalid_value")` si `glossary_too_large`. `_handle` remplace `profile` par `with_extra_glossary(profile, params.prompt)` juste après le second `_parse_params`. La docstring ne dit plus que `prompt` est ignoré.
- [ ] **Step 4 :** `make test` passe.
- [ ] **Step 5 :** commit « Complète le glossaire par le champ prompt des transcriptions ».

### Task 3 : `prompt` en direct

**Files :**
- Modify : `dictee/src/dictee/live.py` (`_Config`, `_read_transcription` et sa docstring, `_open_session`)
- Test : `dictee/tests/test_live.py`

**Interfaces :**
- Consumes : `glossary_too_large`, `parse_glossary`, `glossary_terms`.
- Produces : `_Config.prompt_terms: tuple[str, ...] = ()`.

- [ ] **Step 1 : tests qui échouent**
  - Mettre à jour `test_the_session_in_force_reaches_nemo` : avec `"prompt": "x"`, `phrases == ["Nemotron", "x", "Kubernetes"]`.
  - `test_prompt_term_already_known_is_not_repeated` : glossaire `Nemotron`, `prompt="Nemotron\nMagpie"`, keywords `["Magpie"]` donne `["Nemotron", "Magpie"]`.
  - `test_invalid_prompt_is_refused` : `prompt=3` puis `"a" * 20_001` donnent une erreur `invalid_value` au `param` `session.audio.input.transcription.prompt`, et la session suivante garde les réglages précédents.
  - Ajouter `"prompt": "Magpie"` à `test_the_session_line_never_carries_any_text` et `"Magpie"` à la liste des textes absents.
- [ ] **Step 2 :** `pytest tests/test_live.py -v` échoue.
- [ ] **Step 3 :** `_read_transcription` lit `prompt` s'il est présent (`None` vaut vide ; non-`str` ou trop grand : `_invalid`). `_open_session` construit `phrases` dans l'ordre profil, `prompt_terms`, `keywords`, en retirant les doublons (`dict.fromkeys`).
- [ ] **Step 4 :** `make test` passe.
- [ ] **Step 5 :** commit « Complète le glossaire par le prompt de la dictée en direct ».
