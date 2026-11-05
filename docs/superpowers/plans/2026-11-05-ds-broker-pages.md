# Pages web : reprise du design system du MCP broker, plan d'implémentation

> **Pour les agents qui exécutent ce plan :** utiliser `subagent-driven-development` (recommandé) ou `executing-plans`, tâche par tâche. Les étapes se cochent avec `- [ ]`.

**Objectif :** habiller la page de configuration et la page de message de dictee avec les couleurs et les composants du design system du MCP broker, sans changer leur contenu ni leur comportement.

**Architecture :** tout le style reste dans le bloc `<style>` de `base.html`, que la politique de sécurité autorise et que les deux pages partagent. Les templates `index.html` et `message.html` reçoivent seulement des classes. Le thème clair ou sombre suit le réglage du système par `prefers-color-scheme`, sans script.

**Stack :** Jinja2 (templates de Starlette), pytest avec le client de test de Starlette, CSS seul.

**Spec :** `docs/superpowers/specs/2026-11-05-ds-broker-pages-design.md`. Source du design system : `mcp_broker/templates/_shell.html` du dépôt `supergeoff/mcp-broker`, lisible avec l'outil GitHub (`github-get_file_contents`, propriétaire `supergeoff`).

## Contraintes globales

- La politique des pages reste `default-src 'none'; style-src 'unsafe-inline'; media-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'`, que `test_web.py` vérifie mot pour mot.
- Le style est dans `base.html`, sans script, sans police, sans image ni feuille de style chargées depuis l'extérieur.
- La pile de polices est celle du broker : `Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif`. Rien n'est chargé.
- Les textes restent en français. `web.py` et les routes ne changent pas. Les tests existants passent sans que leurs assertions changent.
- Le dépôt est public : rien de personnel dedans.
- Les tests se lancent par `make test ARGS="tests/test_web.py -k design_system"` (Docker doit tourner). Les chemins de `ARGS` sont relatifs à `dictee/`.
- Les messages de commit sont en français, en phrases complètes, et se terminent par la ligne `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.
- Aucun push ni aucune PR sans l'accord de Geoff, qui fusionne lui-même sur `main`.

## Points à relire en priorité

- **Téléphone de 375 px, libellé de clé de 100 caractères sans espace.** La page ne défile pas en largeur, seul le tableau défile dans son bloc. Test : tâche 3 (libellé de 100 caractères, bloc `table-scroll`) et contrôle visuel de la tâche 4.
- **Mode sombre.** Les encarts d'avertissement et de succès, sur surface translucide, restent lisibles. Contrôle visuel de la tâche 4, variantes testées en tâche 3.
- **Clé nouvelle de plus de 40 caractères.** Son `code` passe à la ligne au lieu de déborder. Test : tâche 1 (`overflow-wrap: anywhere`) et contrôle visuel à 375 px.
- **Liste des voix indisponible.** Un encart d'avertissement et jamais de succès, sans cadre de voix. Test : tâche 3.
- **Page de message après une erreur (403 sans jeton, déconnexion).** Le lien est un bouton lisible. Test : tâche 2.

## Structure des fichiers

- Modifier `dictee/src/dictee/templates/base.html` : le bloc `<style>` (tâches 1 à 3).
- Modifier `dictee/src/dictee/templates/index.html` : classes des boutons, du tableau, des encarts et de la pastille (tâches 2 et 3).
- Modifier `dictee/src/dictee/templates/message.html` : le lien devient un bouton (tâche 2).
- Modifier `dictee/tests/test_web.py` : une section `# --- Habillage ---`, ajoutée juste après `test_html_responses_are_not_cached_and_carry_a_content_security_policy`.

---

### Tâche 1 : palette et éléments de base

**Fichiers :**
- Modifier : `dictee/src/dictee/templates/base.html:8-29`
- Tester : `dictee/tests/test_web.py`

**Interfaces :**
- Produit : les variables CSS `--background`, `--foreground`, `--card`, `--muted`, `--muted-foreground`, `--border`, `--input`, `--primary`, `--primary-foreground`, `--accent`, `--accent-foreground`, `--success`, `--success-surface`, `--warning`, `--warning-surface`, `--destructive`, `--destructive-surface` et `--radius`, que les tâches 2 et 3 utilisent.

- [ ] **Étape 1 : écrire les tests**

```python
# --- Habillage ---

TOKENS = (
    "--background", "--foreground", "--card", "--muted", "--muted-foreground", "--border", "--input",
    "--primary", "--primary-foreground", "--accent", "--accent-foreground", "--success", "--success-surface",
    "--warning", "--warning-surface", "--destructive", "--destructive-surface",
)  # fmt: skip


def test_design_system_palette_is_declared_for_light_and_dark(logged_in):
    for page in (logged_in.client.get("/").text, logged_in.post("/logout").text):
        for token in TOKENS:
            assert page.count(f"{token}:") == 2, token  # une déclaration en clair, une en sombre
        assert "color-scheme: light dark" in page and "prefers-color-scheme: dark" in page


def test_design_system_pages_load_nothing_from_outside(logged_in):
    pages = [
        logged_in.client.get("/").text,
        logged_in.post("/keys", {"label": "x"}).text,
        logged_in.post("/logout").text,
    ]
    for page in pages:
        assert "<script" not in page
        assert not re.search(r"<(link|img|iframe|object|embed)\b", page)
        assert "@import" not in page and "url(" not in page


def test_design_system_code_wraps_long_keys(logged_in):
    assert "overflow-wrap: anywhere" in logged_in.client.get("/").text
```

- [ ] **Étape 2 : voir les tests échouer**

Lancer `make test ARGS="tests/test_web.py -k design_system"`. Attendu : `test_design_system_palette_is_declared_for_light_and_dark` échoue (`--background` apparaît 0 fois). Les deux autres passent déjà : ils fixent les contraintes de la spec et ne doivent plus casser.

- [ ] **Étape 3 : remplacer le bloc `<style>` de `base.html`**

Le bloc commence par un commentaire qui dit que la palette et les composants viennent du design system du MCP broker, et que le thème suit le système sans script à cause de la politique de sécurité. Les variables sont celles du broker, copiées telles quelles :

```css
:root {
  color-scheme: light dark;
  --background: #f5f5f5; --foreground: #0a0a0a; --card: #ffffff; --muted: #f1f1f1; --muted-foreground: #737373;
  --border: #d7d7d7; --input: #e5e5e5; --primary: #171717; --primary-foreground: #fafafa;
  --accent: #ededed; --accent-foreground: #171717;
  --success: #047857; --success-surface: #d1fae5; --warning: #b45309; --warning-surface: #fef3c7;
  --destructive: #b91c1c; --destructive-surface: #fee2e2; --radius: 10px;
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
@media (prefers-color-scheme: dark) {
  :root {
    --background: #0d0d0d; --foreground: #fafafa; --card: #171717; --muted: #212121; --muted-foreground: #b3b3b3;
    --border: #ffffff1a; --input: #ffffff26; --primary: #e5e5e5; --primary-foreground: #171717;
    --accent: #262626; --accent-foreground: #fafafa;
    --success: #34d399; --success-surface: #064e3b66; --warning: #fbbf24; --warning-surface: #78350f66;
    --destructive: #f87171; --destructive-surface: #7f1d1d66;
  }
}
```

Puis les règles de base, sur le modèle de `_shell.html` : `body` (marge 0, fond et texte du thème, 14 px), `main` (46 rem au plus, centré, marges de 16 px), `header` (inchangé), `h1` (28 px, graisse 650), `h2` (garde son trait du haut, prend la taille 16 px et la graisse 650 d'un titre de section), `a` (couleur héritée, sans soulignement), `label` (13 px, graisse 600), `textarea` et `input[type="text"]` (règles de champ du broker : bordure `--input`, rayon de 8 px, hauteur d'au moins 40 px, anneau de focus), `code` (fond `--muted`, rayon de 6 px, `ui-monospace`, `overflow-wrap: anywhere`), `.discret` (couleur `--muted-foreground`). Les règles de `table`, `button`, `.notice` et `.voice` restent pour l'instant telles qu'elles sont, en lisant leurs couleurs dans les nouvelles variables.

- [ ] **Étape 4 : voir les tests passer**

Lancer `make test ARGS="tests/test_web.py"`. Attendu : tous les tests passent, y compris celui de la politique de sécurité.

- [ ] **Étape 5 : commit**

```bash
git add dictee/src/dictee/templates/base.html dictee/tests/test_web.py
git commit -m "Reprend la palette et les champs du design system du MCP broker dans base.html" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Tâche 2 : boutons

**Fichiers :**
- Modifier : `dictee/src/dictee/templates/base.html` (règles `.button*`, suppression de la règle `button`)
- Modifier : `dictee/src/dictee/templates/index.html`, `dictee/src/dictee/templates/message.html`
- Tester : `dictee/tests/test_web.py`

**Interfaces :**
- Consomme : `--primary`, `--primary-foreground`, `--card`, `--border`, `--accent`, `--accent-foreground`, `--destructive`, `--destructive-surface`, `--muted-foreground`, `--radius` (tâche 1).
- Produit : les classes `button`, `button-primary`, `button-secondary`, `button-danger`, `button-ghost`, `button-small`.

- [ ] **Étape 1 : écrire les tests**

```python
BUTTON_CLASSES = {
    "Se déconnecter": "button button-ghost",
    "Créer une clé": "button button-primary",
    "Révoquer": "button button-danger button-small",
    "Enregistrer le glossaire": "button button-primary",
    "Enregistrer les consignes": "button button-primary",
    "Revenir aux consignes par défaut": "button button-secondary",
    "Enregistrer la voix": "button button-primary",
}


def button_classes(page: str, label: str) -> str | None:
    found = re.search(rf'<button[^>]*class="([^"]+)"[^>]*>\s*{re.escape(label)}\s*</button>', page)
    return found.group(1) if found else None


def test_design_system_buttons_carry_the_broker_classes(logged_in):
    page = logged_in.post("/keys", {"label": "Android"}).text  # une clé existe, donc « Révoquer » est affiché
    for label, classes in BUTTON_CLASSES.items():
        assert button_classes(page, label) == classes, label


def test_design_system_message_pages_link_with_a_primary_button(logged_in):
    refused = logged_in.client.post("/keys", data={"label": "x"})  # 403 : pas de jeton
    logged_out = logged_in.post("/logout").text
    for page, label in ((logged_out, "Se connecter"), (refused.text, "Retour à la page de configuration")):
        assert re.search(rf'<a [^>]*class="button button-primary"[^>]*>\s*{label}\s*</a>', page), label
```

- [ ] **Étape 2 : voir les tests échouer**

Lancer `make test ARGS="tests/test_web.py -k design_system_b"` puis `-k design_system_message`. Attendu : les deux échouent, car les boutons n'ont pas encore de classe.

- [ ] **Étape 3 : ajouter les règles de bouton et poser les classes**

Dans `base.html`, supprimer la règle `button { font: inherit; padding: ... }` et copier de `_shell.html` les règles `button, input, select { font: inherit; }`, `.button`, `.button-primary`, `.button-secondary`, `.button-danger`, `.button-ghost`, `.button-small`, avec leurs états `:hover` et `:active`, et ajouter un anneau `:focus-visible` de 2 px en `--primary`. Dans `index.html`, poser les classes du tableau `BUTTON_CLASSES` sur les boutons de même libellé, sans toucher à leur `type` ni à leur formulaire. Dans `message.html`, remplacer le lien par `<a class="button button-primary" href="{{ link_url }}">{{ link_text }}</a>`.

- [ ] **Étape 4 : voir les tests passer**

Lancer `make test ARGS="tests/test_web.py"`. Attendu : tous les tests passent.

- [ ] **Étape 5 : commit**

```bash
git add dictee/src/dictee/templates dictee/tests/test_web.py
git commit -m "Habille les boutons des deux pages avec les classes du design system du broker" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Tâche 3 : tableau, encarts, pastille et voix

**Fichiers :**
- Modifier : `dictee/src/dictee/templates/base.html` (règles du tableau, des encarts, de la pastille et des voix)
- Modifier : `dictee/src/dictee/templates/index.html`
- Tester : `dictee/tests/test_web.py`

**Interfaces :**
- Consomme : les variables de la tâche 1.
- Produit : les classes `table-scroll`, `data-table`, `notice`, `notice-success`, `status-badge`, `status-retired`, et le cadre de `.voice`.

- [ ] **Étape 1 : écrire les tests**

```python
def test_design_system_notices_use_their_variants(logged_in, fake_nemo):
    saved = logged_in.client.get("/?saved=1").text
    assert re.search(r'class="notice notice-success"[^>]*>\s*Modifications enregistrées', saved)
    created = logged_in.post("/keys", {"label": "Android"}).text
    assert 'class="notice" role="alert"' in created and "notice-success" not in created
    logged_in.client.app.state.voices = None
    fake_nemo.error = NemoUnavailable("down")
    down = logged_in.client.get("/").text
    assert re.search(r'class="notice"[^>]*>\s*La liste des voix', down) and "notice-success" not in down


def test_design_system_key_table_scrolls_on_its_own_and_marks_revoked_keys(logged_in):
    page = logged_in.post("/keys", {"label": "x" * 100}).text  # 100 caractères : la limite des libellés
    assert re.search(r'<div class="table-scroll">\s*<table class="data-table">', page)
    key_id = re.search(r'action="/keys/(\d+)/revoke"', page).group(1)
    page = logged_in.post(f"/keys/{key_id}/revoke").text
    assert re.search(r'class="status-badge status-retired"[^>]*>\s*révoquée le', page)
    assert "Révoquer" not in page
```

- [ ] **Étape 2 : voir les tests échouer**

Lancer `make test ARGS="tests/test_web.py -k design_system_notices"` puis `-k design_system_key_table`. Attendu : les deux échouent (`notice-success` et `table-scroll` n'existent pas).

- [ ] **Étape 3 : ajouter les règles et poser les classes**

Dans `index.html` : `Modifications enregistrées.` reçoit `class="notice notice-success"`, les deux autres encarts gardent `class="notice"` ; le tableau des clés est enveloppé dans `<div class="table-scroll">` et reçoit `class="data-table"` ; « révoquée le … » devient `<span class="status-badge status-retired">révoquée le {{ key.revoked_at[:10] }}</span>`. Dans `base.html` :

- `.table-scroll` : `overflow-x: auto`.
- `.data-table` : règles `.data-table` de `_shell.html` (en-têtes de 12 px, graisse 650, majuscules, couleur `--muted-foreground`, traits `--border`). Les marges de cellule sont au choix du constructeur, dans la limite d'un tableau lisible à 46 rem.
- `.notice` : fond `--warning-surface`, bordure de 1 px en `--warning`, texte en `--foreground`, rayon `--radius`, marge intérieure de 12 px sur 14 px. `.notice-success` : les mêmes règles avec `--success-surface` et `--success`.
- `.status-badge` et `.status-retired` : copiés de `_shell.html`.
- `.voice` : cadre de 1 px en `--border`, rayon `--radius`, espace de 8 px entre deux voix, `.voice label` en graisse 500. `input[type="radio"]` : `accent-color: var(--primary)`.

- [ ] **Étape 4 : voir les tests passer**

Lancer `make test ARGS="tests/test_web.py"`. Attendu : tous les tests passent.

- [ ] **Étape 5 : commit**

```bash
git add dictee/src/dictee/templates dictee/tests/test_web.py
git commit -m "Habille le tableau des clés, les encarts, la pastille de révocation et les voix" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Tâche 4 : suite complète et rendu dans un navigateur

**Fichiers :** aucun fichier du dépôt ne change. Le script de rendu vit hors du dépôt, dans le dossier temporaire de la session.

- [ ] **Étape 1 : lancer toute la suite**

Lancer `make test`. Attendu : aucun échec.

- [ ] **Étape 2 : rendre les pages en HTML statique**

Écrire hors du dépôt un script qui charge `dictee.web._templates.env`, rend `index.html` et `message.html` et écrit les résultats dans un dossier. Le contexte de `index.html` est strict (une clé manquante lève une erreur) : `csrf`, `api_url`, `keys` (éléments avec `id`, `label`, `prefix`, `created_at`, `last_used_at`, `revoked_at`), `new_key`, `glossary`, `instructions`, `voices`, `current_voice`, `saved`. Celui de `message.html` : `message`, `link_url`, `link_text`. Trois rendus : la page complète (clé nouvelle de 48 caractères, une clé active, une clé révoquée, libellé de 100 caractères sans espace, `saved` vrai, cinq voix), la page sans clé ni voix, et la page de message. Lancer le script par `cd dictee && uv run --frozen python <script>`. Attendu : les trois fichiers existent.

- [ ] **Étape 3 : les voir en clair et en sombre**

Servir le dossier avec `python3 -m http.server` en arrière-plan, l'ouvrir avec `preview_start` (paramètre `url`), puis `resize_window` avec `colorScheme` à `light` puis `dark`. Contrôler à la largeur du bureau et au préréglage mobile (375 px) : `document.documentElement.scrollWidth <= window.innerWidth` doit être vrai, la clé nouvelle passe à la ligne, les encarts sont lisibles dans les deux thèmes, les boutons ont les bons styles, le lecteur audio et les boutons radio suivent le thème. Si l'émulation du thème sombre ne s'applique pas à cette page, le dire et le contrôler autrement, sans l'annoncer comme vérifié.

- [ ] **Étape 4 : remettre le navigateur et le dépôt en ordre**

Remettre le préréglage `desktop`, arrêter le serveur, puis lancer `git status -sb` et `git diff --stat main`. Attendu : seuls `base.html`, `index.html`, `message.html`, `test_web.py`, la spec et ce plan diffèrent de `main`.

- [ ] **Étape 5 : rapporter à Geoff**

Dire ce qui a été vu (pages, thèmes, largeurs), ce qui ne l'a pas été, et que la branche `feat/ds-broker-pages` attend sa fusion. Ne rien pousser sans son accord.
