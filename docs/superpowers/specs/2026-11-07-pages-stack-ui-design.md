# Pages web : reprise de Stack UI

- **Date** : 7 novembre 2026
- **Statut** : à valider par Geoff
- **Dépôt** : `supergeoff/dictee-server`, branche `feat/design-system-stack-ui`
- **Point de départ** : `main` au commit `016059c`
- **Remplace** : `2026-11-05-ds-broker-pages-design.md` (« Pages web : reprise du design system du MCP broker »), dont l'habillage et la politique de sécurité sont remplacés par ceux de ce document.

## 1. Objet

Les pages web de dictee (la page de configuration, et la page de message des erreurs et de la déconnexion) reprenaient une copie à la main du design system du MCP broker, écrite dans `base.html`. Stack UI est devenu le design system commun des outils web de Geoff : elles s'appuient désormais sur lui, avec ses jetons, ses composants et sa police Figtree, au lieu de leur propre copie. Seul l'habillage change : les textes, les routes, les formulaires et le comportement restent ceux d'avant.

## 2. Objectifs et non-objectifs

**Objectifs**

- Le style vient de Stack UI, copié tel quel dans l'application (`tokens.css` puis `components/bundle.css`), sans règle de dictee qui le double.
- Les pages emploient les classes de la table « Correspondance des anciens noms » du README de Stack UI.
- Le thème suit le système (`prefers-color-scheme`), sans script.
- La police Figtree est servie par dictee.

**Non-objectifs**

- Un sélecteur de thème Clair / Sombre / Système : il demande un script, que la politique de sécurité interdit toujours.
- La barre latérale (`app-shell`) : dictee garde sa colonne unique.
- Tout changement de texte, de route, de formulaire ou de `web.py`, à l'exception de la route de la police et de l'en-tête de sécurité.
- Une dépendance au dépôt stack-ui à l'exécution ou à la construction de l'image : la copie est dans ce dépôt.

## 3. Politique de sécurité

La politique des pages garde son principe (aucun script, aucune ressource d'un autre site) et gagne une seule directive.

| | Valeur |
|---|---|
| Avant | `default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; media-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'` |
| Après | `default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; font-src 'self'; media-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'` |

**Justification.** Figtree est la police de Stack UI. Sans `font-src`, la directive par défaut `default-src 'none'` la bloque et les pages retombent sur la police du système, ce qui défait l'objet du design system. Autoriser `'self'` est le plus petit élargissement possible : seule une police servie par dictee elle-même se charge, ce qui n'ouvre ni un autre site, ni un script, ni une feuille de style externe. Le style reste en ligne (`style-src 'unsafe-inline'` existait déjà), puisque `base.html` inclut la copie de Stack UI dans son élément `<style>`. Le test de la politique l'exige mot pour mot et vérifie en plus qu'elle est l'ancienne plus `font-src 'self'`.

## 4. Design retenu

**Copie du design system.** `dictee/src/dictee/templates/stack-ui/stack-ui.css` concatène `tokens.css` et `components/bundle.css`, avec en première ligne le commit de stack-ui d'où elle vient. `VERSION`, à côté, porte ce hash court. La police `Figtree-Variable.ttf` et sa licence `OFL.txt` sont dans `dictee/src/dictee/static/fonts/`. `scripts/sync-stack-ui.sh <chemin du clone de stack-ui>` refait les trois et corrige l'URL du `@font-face` (`fonts/Figtree-Variable.ttf` devient `/fonts/Figtree-Variable.ttf`) ; il refuse un clone dont l'arbre est modifié, car `VERSION` mentirait. Ces fichiers ne se modifient jamais à la main.

**Route de la police.** `GET /fonts/Figtree-Variable.ttf` rend le fichier en `font/ttf`, sans session (le navigateur le demande aussi sur la page de connexion et les pages de refus), avec `Cache-Control: public, max-age=86400` comme l'icône.

**Gabarits.** `base.html` inclut la copie dans son `<style>` puis n'ajoute qu'une règle propre à dictee, la largeur maximale du lecteur audio (`audio { max-width: 100% }`), que Stack UI ne couvre pas. Le liseré de l'icône en sombre, la marge nulle des enfants d'une `stack` et l'alignement de l'en-tête (`page-header page-header-center`) viennent de Stack UI depuis son commit `44cf377`, et la page ne les redéfinit pas : un test vérifie que la règle locale se limite à ce qui reste. La page est une `column`. La marque est `brand brand-wordmark` (l'icône reste une `<img>`). Chaque bloc (clés, glossaire, consignes, voix) est une `card` avec son `card-title` et sa `card-description`. Les champs sont des `input` (`input-mono` pour le glossaire et les consignes), le tableau des clés un `data-table data-table-compact` dans un `table-scroll`, les encarts des `notice notice-success` et `notice notice-warning` avec leurs `role`, la clé révoquée une `badge`, chaque voix une `list-row` qui garde son `<audio controls>`, le texte discret `.muted` et les extraits de code `.code`.

## 5. Vérification

- `make test` passe, politique de sécurité comprise.
- Les tests nouveaux ont été écrits avant le code : la route de police (type, cache, contenu, sans session), la politique égale à l'ancienne plus `font-src 'self'`, la version de la copie et son URL de police, les deux palettes de `tokens.css` (clair, et sombre par `prefers-color-scheme`) avec les jetons essentiels et le contraste du texte discret, l'absence de script et de ressource externe (la police excepté), les classes des boutons, des champs, des encarts, du tableau, des voix et du code, et la disparition des anciennes classes.
- Les fichiers de la copie sont bien dans l'image d'exécution (cible `runtime` du Dockerfile construite, fichiers lus par `importlib.resources`).
- **Non vérifié ici : le rendu.** Aucun navigateur n'a servi à cette migration. Geoff regarde les pages, en clair et en sombre, avant de fusionner : la page de configuration avec la clé nouvelle, le tableau, les voix, puis sans voix, la page de message, et la police Figtree (onglet réseau : `font/ttf`, aucune violation de politique dans la console).
