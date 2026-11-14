# Plan : langue automatique et début sans parole

> **Pour l'exécution :** utiliser le skill `subagent-driven-development` (recommandé) ou `executing-plans`, tâche par tâche. Les cases suivent l'avancement de l'exécution.

**Objectif :** transcrire un audio en français, en anglais ou dans les deux langues sans langue choisie à l'avance, et mener à son terme un audio qui commence sans parole reconnue.

**Architecture :** `modes.py` porte la règle de langue : une langue absente ou `auto` part en détection chez NeMo, et le résultat annonce alors `und`. Les routes n'ont rien à décider : elles passent par `to_nemo_language` et `to_iso_language`. Dans `nemo.py`, le délai sans événement d'une session temps réel repart à chaque trame que NeMo accepte.

**Technologies :** Python 3.13, Starlette, websockets, pytest, faux NeMo du dépôt, NeMo-Speech.cpp 0.3.0 pour la tâche 1.

**Spec :** `docs/superpowers/specs/2026-11-14-langue-auto-design.md`. L'exécutant lit la spec et ce plan.

## Contraintes générales

- Le travail se fait sur la branche `fix/langue-auto`. Geoff fusionne lui-même sur `main`, et rien n'est poussé sans son accord.
- Chaque tâche suit l'ordre test qui échoue, code, test qui passe, commit. Les messages de commit sont en français, en phrases complètes.
- `language` reste le champ d'OpenAI. Aucune route ni aucun champ maison n'est ajouté.
- Une langue envoyée par l'app est respectée comme aujourd'hui : `fr` donne `fr-FR`, `en` donne `en-US`.
- La synthèse vocale garde le français (`speech.LANGUAGE`).
- Aucune migration de base : une tâche créée avant le changement garde sa langue `fr-FR` et reste lisible.
- Les audios des tests et des contrôles sont de synthèse. Aucun enregistrement réel ni aucun texte transcrit n'entre dans le dépôt, qui est public.
- Les journaux ne portent jamais de contenu : ni texte, ni audio, ni glossaire.
- La version de NeMo-Speech.cpp vient du compose de Dokploy, relu le 14 novembre 2026 : `0.3.0`. L'exécutant la relit avant la tâche 1.
- Démarrer la stack de test (`make nemo-up`, `make integration`) et Docker sur le Mac attend l'accord de Geoff.
- `make test` lance la suite dans Docker. Sans Docker, `cd dictee && uv run --frozen pytest -q <fichiers>` lance les fichiers nommés ; les tests qui appellent ffmpeg échouent alors, faute de ffmpeg sur le Mac.

## Points de revue couverts par les tests

1. Une app envoie `AUTO` ou `Auto` : elle obtient la détection, pas une erreur 400 (tâche 2).
2. Le même fichier est envoyé une fois sans langue et une fois avec `language=fr` : ce sont deux tâches, et la seconde n'hérite pas du résultat détecté (tâche 2).
3. Une réponse `verbose_json` ou `diarized_json` d'une tâche détectée porte `und` et ne lève pas d'erreur de langue au rendu (tâche 2).
4. Un client de la dictée en direct envoie `languages: ["auto"]` ou `language: null` : la session détecte et annonce une langue nulle (tâche 3).
5. Le rappel d'avancement de l'appelant lève une exception pendant l'envoi : la session s'arrête tout de suite, comme aujourd'hui, malgré le rappel ajouté par `transcribe` (tâche 4).

---

### Tâche 1 : vérifier `auto` sur un vrai NeMo

**Fichiers :**
- Modifier : `integration/check_nemo.py` (nouveau contrôle `k`)
- Modifier : ce plan, section « Résultats de la tâche 1 »

**Interfaces :**
- Utilise : `nemo_rt.synth_wav(text, voice, rate, language)`, `nemo_rt.realtime(pcm, rate, phrases, boost, **options)`, `rest_form`, `rest_files`, `report`, `note`, `silence`, `duration` de `check_nemo.py`.
- Produit : la décision qui ouvre ou arrête les tâches 2 à 5.

- [ ] **Étape 1 : écrire le contrôle `k`**

Ajouter à `check_nemo.py` les constantes et la fonction suivantes, puis l'entrée `"k": lambda audio: check_language()` dans `CHECKS`.

```python
EN_PHRASE_1 = ("Good morning, this is a test of the automatic language detection.", "John")
EN_PHRASE_2 = ("Thank you, the meeting will continue in English this afternoon.", "Aria")
FR_KEYWORDS = ["bonjour", "transcription", "synthèse", "français"]
EN_KEYWORDS = ["morning", "language", "meeting", "afternoon"]
```

`check_language() -> None` synthétise les deux phrases anglaises avec `language="en-US"`, puis forme trois audios séparés par une seconde de silence : `french` (les deux phrases françaises), `english` (les deux phrases anglaises) et `mixed` (français 1, anglais 1, français 2, anglais 2). Pour chaque audio et chaque langue parmi `auto`, `fr-FR` et `en-US`, elle transcrit par la route REST (`verbose_json`) et par une session temps réel avec `speaker_diarization`. Elle n'affiche que des comptes : mots-clés retrouvés par langue, nombre de mots, statut HTTP ou type du dernier événement. Elle rapporte quatre lignes :

| Ligne | Réussite |
|---|---|
| `k1` auto accepté | la route REST répond 200 et la session rend `session.updated` puis `input_audio_buffer.committed`, avec `auto` |
| `k2` deux langues | `mixed` avec `auto` retrouve les quatre mots de `FR_KEYWORDS` et les quatre de `EN_KEYWORDS`, par les deux routes |
| `k3` départ en anglais | `english` avec `auto` retrouve `morning`, le mot-clé de sa première phrase, par les deux routes |
| `k4` dictées françaises | chaque phrase française seule rend avec `auto` le même texte qu'avec `fr-FR`, par la route REST |

Avec `--note`, la fonction compare aussi les 30 premières secondes de l'enregistrement réel en `auto` et en `fr-FR`, et n'en affiche que les nombres de mots.

- [ ] **Étape 2 : lancer NeMo et le contrôle, avec l'accord de Geoff**

```bash
NEMO_VERSION=0.3.0 make nemo-up
uv run --script integration/check_nemo.py --only k
```

Attendu : les quatre lignes `k1` à `k4` en `OK`.

- [ ] **Étape 3 : consigner les résultats et décider**

Écrire les comptes obtenus dans « Résultats de la tâche 1 », sans aucun texte transcrit.

| Résultat | Suite |
|---|---|
| `k1`, `k2` ou `k3` en échec | Le travail s'arrête. L'exécutant rapporte les comptes à Geoff. |
| `k4` en échec, le reste en `OK` | Le travail s'arrête avant la tâche 2. La tâche 2 est réécrite pour limiter la détection par défaut au modèle `dictee-diarize`, comme la spec le prévoit, et Geoff relit ce changement. |
| Tout en `OK` | Les tâches 2 à 5 suivent, telles qu'écrites. |

- [ ] **Étape 4 : commit**

```bash
git add integration/check_nemo.py docs/superpowers/plans/2026-11-14-langue-auto.md
git commit -m "Vérifier la détection de langue sur NeMo"
```

#### Résultats de la tâche 1

À remplir par l'exécutant.

---

### Tâche 2 : règle de langue des transcriptions

**Fichiers :**
- Modifier : `dictee/src/dictee/modes.py`
- Modifier : `dictee/tests/test_modes.py`, `dictee/tests/test_transcriptions.py`
- Modifier au besoin : les tests qui attendent encore `fr-FR` ou `fr` pour une requête sans langue

**Interfaces :**
- Produit, dans `modes.py` :
  - `AUTO_LANGUAGE = "auto"` et `UNDETERMINED_LANGUAGE = "und"` ;
  - `is_auto_language(code: str | None) -> bool` : vrai pour `None`, une chaîne vide et `auto` dans n'importe quelle casse ;
  - `to_nemo_language(code: str | None) -> str` : rend `auto` quand `is_auto_language(code)`, sinon la conversion d'aujourd'hui ;
  - `to_iso_language(code: str | None) -> str` : rend `und` quand `is_auto_language(code)` ou quand le code vaut `und` dans n'importe quelle casse, sinon la conversion d'aujourd'hui.
- `DEFAULT_LANGUAGE` reste en place jusqu'à la tâche 3, qui retire son dernier usage.

- [ ] **Étape 1 : écrire les tests qui échouent**

Dans `test_modes.py`, remplacer les cas d'une langue absente et ajouter ceux de `auto` :

```python
@pytest.mark.parametrize("code", [None, "", "auto", "AUTO", "Auto"])
def test_a_missing_or_auto_language_asks_nemo_to_detect(code):
    assert is_auto_language(code)
    assert to_nemo_language(code) == "auto"
    assert to_iso_language(code) == "und"


@pytest.mark.parametrize("code", ["und", "UND"])
def test_an_undetermined_language_stays_undetermined(code):
    assert to_iso_language(code) == "und"


@pytest.mark.parametrize("code", ["fr", "en-US", "und"])
def test_a_named_language_is_not_auto(code):
    assert not is_auto_language(code)


@pytest.mark.parametrize("code", [" auto", "auto ", "automatic", "aut"])
def test_what_only_looks_like_auto_is_refused(code):
    with pytest.raises(ApiError) as e:
        to_nemo_language(code)
    assert e.value.status == 400 and e.value.param == "language"
```

Dans `test_transcriptions.py`, avec les aides `ask` et `fake_nemo` du fichier :

```python
@pytest.mark.parametrize("fields", [{}, {"language": "auto"}, {"language": "AUTO"}])
def test_without_a_language_nemo_detects_it(ask, fake_nemo, fields):
    assert ask(**fields).status_code == 200
    assert fake_nemo.calls[0]["language"] == "auto"


@pytest.mark.parametrize("model", ["dictee", "dictee-clean", "dictee-diarize"])
def test_every_model_detects_the_language_by_default(ask, fake_nemo, model):
    assert ask(model=model).status_code == 200
    assert fake_nemo.calls[0]["language"] == "auto"


def test_a_stream_without_a_language_detects_it(ask, fake_nemo):
    assert ask(stream="true").status_code == 200
    assert fake_nemo.transcribe_calls[0]["language"] == "auto"


@pytest.mark.parametrize("response_format", ["verbose_json", "diarized_json"])
def test_a_detected_language_is_reported_as_undetermined(ask, response_format):
    answer = ask(response_format=response_format)
    assert answer.status_code == 200
    assert answer.json().get("language", "und") == "und"


def test_the_same_file_with_and_without_a_language_makes_two_tasks(ask):
    detected = ask(background="true").json()
    french = ask(background="true", language="fr").json()
    assert detected["id"] != french["id"]
```

Garder `test_language_reaches_nemo_in_its_own_form` et `test_verbose_json_reports_the_iso_language` : une langue envoyée ne change pas.

- [ ] **Étape 2 : voir les tests échouer**

```bash
cd dictee && uv run --frozen pytest -q tests/test_modes.py tests/test_transcriptions.py
```

Attendu : les nouveaux tests échouent sur `fr-FR` ou `fr`, ou sur l'import de `is_auto_language`.

- [ ] **Étape 3 : écrire la règle dans `modes.py`**

Écrire les trois fonctions de « Interfaces ». `_split_language` ne reçoit plus de langue absente : ce cas est traité avant lui. Le message de l'erreur 400 cite `auto` parmi les valeurs acceptées.

- [ ] **Étape 4 : mettre à jour les tests qui portaient l'ancien défaut**

Les tests qui attendent `fr-FR` chez NeMo ou `fr` dans la réponse pour une requête sans langue attendent `auto` et `und`. Un test qui dépend du français envoie `language="fr"`.

- [ ] **Étape 5 : voir la suite passer**

```bash
make test
```

Attendu : toute la suite passe. Sans Docker, lancer les fichiers de `dictee/tests` un par un et noter ceux qui échouent faute de ffmpeg.

- [ ] **Étape 6 : commit**

```bash
git add dictee/src/dictee/modes.py dictee/tests
git commit -m "Détecter la langue quand la requête n'en donne pas"
```

---

### Tâche 3 : dictée en direct

**Fichiers :**
- Modifier : `dictee/src/dictee/live.py`, `dictee/src/dictee/modes.py` (retrait de `DEFAULT_LANGUAGE`)
- Modifier : `dictee/tests/test_live.py`

**Interfaces :**
- Utilise : `is_auto_language`, `to_iso_language`, `to_nemo_language` de la tâche 2.
- Produit : `_Config.language: str | None = None`, où `None` vaut détection ; `_read_language(code: object, param: str) -> str | None`.

- [ ] **Étape 1 : écrire les tests qui échouent**

Dans `test_live.py`, avec les aides `open_live`, `update` et `until` du fichier :

```python
def test_a_live_session_detects_the_language_by_default(live_client, service_headers, fake_nemo):
    with open_live(live_client, service_headers) as ws:
        until(ws, "session.created")
        update(ws)
        session = until(ws, "session.updated")[-1]["session"]["audio"]["input"]
        ws.send_json({"type": "input_audio_buffer.append", "audio": pcm_b64(0.5, 24000)})
        ws.send_json({"type": "input_audio_buffer.commit"})
        until(ws, COMPLETED)
    assert session["transcription"]["language"] is None
    assert fake_nemo.live_sessions[0].options["language"] == "auto"


@pytest.mark.parametrize("transcription", [{"languages": ["auto"]}, {"language": "auto"}, {"language": None}])
def test_a_live_session_asked_for_auto_detects_the_language(live_client, service_headers, fake_nemo, transcription):
    with open_live(live_client, service_headers) as ws:
        until(ws, "session.created")
        update(ws, transcription=transcription)
        session = until(ws, "session.updated")[-1]["session"]["audio"]["input"]
    assert session["transcription"]["language"] is None
```

Le test existant qui attend `"fr"` après un `session.update` sans langue attend `None`.

- [ ] **Étape 2 : voir les tests échouer**

```bash
cd dictee && uv run --frozen pytest -q tests/test_live.py
```

Attendu : échec sur `"fr"` au lieu de `None`, et sur `fr-FR` au lieu de `auto`.

- [ ] **Étape 3 : écrire le code**

`_read_language` rend `None` quand `is_auto_language(code)`, sinon le code ISO. `_Config.language` vaut `None` par défaut. La session annonce `config.language` tel quel, et `to_nemo_language(config.language)` part chez NeMo. Retirer `DEFAULT_LANGUAGE` de `modes.py` et son import dans `live.py`.

- [ ] **Étape 4 : voir les tests passer**

```bash
cd dictee && uv run --frozen pytest -q tests/test_live.py tests/test_modes.py
```

Attendu : tout passe.

- [ ] **Étape 5 : commit**

```bash
git add dictee/src/dictee/live.py dictee/src/dictee/modes.py dictee/tests/test_live.py
git commit -m "Détecter la langue de la dictée en direct par défaut"
```

---

### Tâche 4 : délai sans événement d'une session temps réel

**Fichiers :**
- Modifier : `dictee/src/dictee/nemo.py`
- Modifier : `dictee/tests/test_nemo.py`

**Interfaces :**
- `NemoClient.transcribe` garde sa signature. Il appelle toujours `_send_audio(ws, wav, on_progress)` avec un rappel à lui, jamais `None` : ce rappel note l'instant de la trame acceptée, puis appelle le rappel de l'appelant s'il y en a un.
- Le délai lu à l'appel (`EVENT_TIMEOUT`, ou `SPEAKERS_EVENT_TIMEOUT` avec les personnes) compte depuis le plus récent de la dernière trame acceptée et du dernier événement reçu. Le message de `NemoError` ne change pas : `no event from NeMo for {timeout:g} seconds`.
- `LiveSession` et les appels HTTP ne changent pas.

- [ ] **Étape 1 : écrire les tests qui échouent**

Dans `test_nemo.py`, avec les aides `fake_ws`, `nemo`, `wav_1s` et `transcribe` du fichier :

```python
async def test_a_session_that_takes_audio_without_any_event_outlives_the_timeout(fake_ws, nemo, wav_1s, monkeypatch):
    # NeMo n'émet rien tant qu'il n'a reconnu aucun mot : l'audio qu'il accepte prouve qu'il travaille.
    monkeypatch.setattr(nemo_module, "EVENT_TIMEOUT", 0.2)

    async def slow_upload(ws, wav, on_progress):
        for second in range(1, 5):
            await asyncio.sleep(0.15)
            on_progress(float(second))
        await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))

    monkeypatch.setattr(nemo_module, "_send_audio", slow_upload)
    items = await transcribe(nemo, wav_1s)
    assert [type(item) for item in items] == [Final]


async def test_a_session_that_stops_taking_audio_and_stays_silent_fails_after_the_timeout(
    fake_ws, nemo, wav_1s, monkeypatch
):
    monkeypatch.setattr(nemo_module, "EVENT_TIMEOUT", 0.2)

    async def stuck_upload(ws, wav, on_progress):
        on_progress(1.0)
        await asyncio.Event().wait()

    monkeypatch.setattr(nemo_module, "_send_audio", stuck_upload)
    started = time.monotonic()
    with pytest.raises(NemoError, match="no event from NeMo for 0.2 seconds"):
        await transcribe(nemo, wav_1s)
    assert 0.2 <= time.monotonic() - started < 1.0
    await asyncio.wait_for(fake_ws.closed.wait(), 1)


async def test_the_progress_of_the_caller_still_gets_every_frame(fake_ws, nemo, tmp_path):
    seen: list[float] = []
    await transcribe(nemo, write_wav(tmp_path, 3.0), on_progress=seen.append)
    assert seen == [1.0, 2.0, 3.0]
```

- [ ] **Étape 2 : voir les tests échouer**

```bash
cd dictee && uv run --frozen pytest -q tests/test_nemo.py
```

Attendu : les deux premiers tests échouent, parce que `transcribe` passe aujourd'hui `None` à `_send_audio` quand l'appelant ne donne pas de rappel (`TypeError`), ou sur `NemoError: no event from NeMo for 0.2 seconds`. Le troisième passe déjà : il garde le comportement actuel.

- [ ] **Étape 3 : écrire le code dans `nemo.py`**

Faire repartir le délai à chaque trame acceptée, selon « Interfaces ». Mettre à jour la docstring de `transcribe` et le commentaire de `EVENT_TIMEOUT`.

- [ ] **Étape 4 : voir les tests passer, anciens compris**

```bash
cd dictee && uv run --frozen pytest -q tests/test_nemo.py
```

Attendu : tout passe, dont `test_silence_from_nemo_raises_nemo_error`, `test_with_speakers_silence_from_nemo_still_ends_after_the_speakers_timeout`, `test_cancelling_during_the_audio_upload_closes_the_session` et `test_an_error_of_the_progress_callback_ends_the_session_at_once`.

Un audio sans aucun mot reconnu rend déjà un texte vide : `test_silence_gives_empty_text` de `test_transcriptions.py` le garde, et aucun test nouveau n'est nécessaire pour ce point de la spec.

- [ ] **Étape 5 : commit**

```bash
git add dictee/src/dictee/nemo.py dictee/tests/test_nemo.py
git commit -m "Ne plus prendre un début sans parole pour une panne de NeMo"
```

---

### Tâche 5 : documentation et contrôle d'intégration

**Fichiers :**
- Modifier : `README.md` (champ `language`, dictée en direct, délais)
- Modifier : `docs/superpowers/specs/2026-10-31-dictee-server-design.md` (sections 5 sur `language`, la dictée en direct, les délais de la session temps réel)
- Modifier : `integration/run.py` (contrôle `language`)

**Interfaces :**
- Utilise : la règle de la tâche 2 et le délai de la tâche 4.

- [ ] **Étape 1 : écrire le contrôle d'intégration `language`**

Dans `integration/run.py`, ajouter un contrôle `language` à la liste des contrôles. Il synthétise la phrase `EN_PHRASE_1` de la tâche 1 par dictee, l'envoie à `POST /v1/audio/transcriptions` sans champ `language` avec le modèle `dictee`, et réussit quand la réponse contient `morning` et `language`. Il renvoie le même audio avec `language=en` et attend les deux mêmes mots.

- [ ] **Étape 2 : mettre la documentation à jour**

Le README et la spec de référence disent : sans `language` ou avec `auto`, NeMo détecte la langue et la réponse détaillée annonce `und` ; la dictée en direct suit la même règle et annonce une langue nulle ; le délai de 300 secondes d'une session temps réel repart à chaque trame que NeMo accepte, et un audio sans mot reconnu rend un texte vide.

- [ ] **Étape 3 : vérifier la suite et le contrôle**

```bash
make test
NEMO_VERSION=0.3.0 make integration
uv run --script integration/run.py --only language
```

Attendu : la suite passe, et le contrôle `language` est `OK`. `make integration` attend l'accord de Geoff.

- [ ] **Étape 4 : commit**

```bash
git add README.md docs/superpowers/specs/2026-10-31-dictee-server-design.md integration/run.py
git commit -m "Documenter la détection de langue et le délai de la session"
```

---

## Après le plan, hors du dépôt

Ces étapes attendent chacune l'accord de Geoff.

1. Geoff relit la branche, la fusionne sur `main` et déploie depuis Dokploy.
2. Dans dictee-app, le réglage de langue de la transcription passe de `fr` à `auto`.
3. L'enregistrement en anglais de 27 minutes est renvoyé depuis l'app. La tâche doit finir `completed`, avec un texte en anglais dès le début.
