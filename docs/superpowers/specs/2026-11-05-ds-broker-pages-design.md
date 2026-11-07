# Pages web : reprise du design system du MCP broker

- **Date** : 5 novembre 2026
- **Statut** : remplacée par `2026-11-07-pages-stack-ui-design.md` (reprise de Stack UI, qui ajoute `font-src 'self'` à la politique de sécurité)
- **Dépôt** : `supergeoff/dictee-server`, branche `feat/ds-broker-pages`
- **Point de départ** : `main` au commit `0c00e00`

## 1. Objet

Les pages web de dictee (la page de configuration, et la page de message qui sert aux erreurs et à la déconnexion) portent un style minimal écrit à la main, qui ne ressemble à aucun autre outil de la stack. Geoff veut qu'elles reprennent le design system du tableau de bord du MCP broker, défini dans `mcp_broker/templates/_shell.html` du dépôt `supergeoff/mcp-broker`, pour que ses outils se ressemblent. Seul l'habillage change : le contenu, les formulaires, les routes et le comportement restent ceux d'aujourd'hui.

## 2. Objectifs, non-objectifs, anti-objectifs

**Objectifs**

- Les couleurs, la typographie, les boutons, les champs, le tableau, les encarts et les pastilles d'état viennent du design system du broker.
- Le thème clair ou sombre suit le réglage du système de la personne.
- Les textes restent en français.

**Non-objectifs**

- La barre latérale, les cartes, la marque et le favicon du broker : dictee garde sa colonne unique.
- Un bouton de choix du thème. Il demande un script, que la politique de sécurité des pages interdit.
- Tout changement de texte, de route, de formulaire ou de `web.py`.
- Un CSS partagé entre les deux dépôts.

**Anti-objectifs**

- Un script, une police, une feuille de style ou une image chargés depuis l'extérieur de la page.
- Une modification de la politique de sécurité des pages ou de son test.
- Un élément personnel dans le dépôt, qui est public.

## 3. Contraintes

- Les pages portent la politique `default-src 'none'; style-src 'unsafe-inline'; media-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'`, que `test_web.py` vérifie mot pour mot. Le style reste donc dans `base.html`, sans script, sans police ni image à charger.
- Le broker nomme la police Inter dans sa pile mais ne la charge pas : l'écran tombe sur la police du système. dictee reprend la même pile et ne charge rien.
- Les tests existants passent sans que leurs assertions changent.

## 4. Approches

- **A. Reprendre tout le gabarit du broker** (barre latérale, cartes). Écartée par Geoff : il veut la version légère.
- **B. Reprendre les couleurs et les composants, en gardant la colonne unique.** Retenue par Geoff le 5 novembre 2026.
- **C. Un CSS partagé entre les deux dépôts.** Écartée : la politique n'autorise que du style intégré à la page, et deux utilisateurs ne justifient pas un paquet commun.

Pour le thème, Geoff a choisi le réglage du système plutôt que le bouton Clair / Sombre / Système du broker, afin de laisser la politique de sécurité intacte.

## 5. Design retenu

**Palette.** Les variables du broker, en clair et en sombre : `--background`, `--foreground`, `--card`, `--muted`, `--muted-foreground`, `--border`, `--input`, `--primary`, `--primary-foreground`, `--accent`, `--accent-foreground`, les couleurs `success`, `warning` et `destructive` avec leur surface, et `--radius` à 10 px. Le sombre s'applique par `@media (prefers-color-scheme: dark)` seulement, et `:root` déclare `color-scheme: light dark` pour que les contrôles natifs (lecteur audio, boutons radio) suivent le thème. Les blocs `data-theme` du broker ne sont pas repris, puisqu'ils servent au bouton de thème.

**Composants.**

- **Page.** Le corps fait 14 px sur le fond `--background`. La colonne reste de 46 rem au plus.
- **Titres.** `h1` à 28 px et graisse 650. Les `h2` gardent leur trait de séparation et prennent la graisse et la taille d'un titre de section du broker.
- **Champs et zones de texte.** Bordure `--input`, rayon de 8 px, hauteur d'au moins 40 px, anneau de focus du broker.
- **Boutons.** Ils prennent les classes du broker : `button button-primary` pour créer et enregistrer, `button button-secondary` pour « Revenir aux consignes par défaut », `button button-danger button-small` pour « Révoquer » et `button button-ghost` pour « Se déconnecter ». Sur la page de message, le lien devient `a.button.button-primary`.
- **Tableau des clés.** Le style `data-table` du broker : en-têtes en petites capitales grises et traits fins, sans carte autour.
- **Encarts.** `notice` (avertissement) pour la clé nouvelle et pour la liste des voix indisponible, `notice notice-success` pour « Modifications enregistrées ».
- **Clé révoquée.** Une pastille `status-badge status-retired`.
- **Voix.** Chaque ligne dans un cadre arrondi, comme `saved-secret-row` du broker, avec le bouton radio en `accent-color: var(--primary)`.
- **Texte discret.** La couleur `--muted-foreground`.
- **Code.** Fond `--muted`, rayon de 6 px, police à chasse fixe. Le broker ne style pas `code` dans son gabarit : c'est un choix de ce design.

Les composants qui viennent du broker gardent ses noms de classes. `voice` et `notice-success`, propres à dictee, gardent les leurs.

## 6. Laissé au constructeur

- Les valeurs exactes et l'ordre des règles, en ne copiant que les règles dont dictee se sert (ni barre latérale, ni cartes de serveur, ni champ de secret masqué).
- Un commentaire en tête du style qui dit que la palette et les composants viennent du design system du MCP broker.
- Les marges de la colonne unique, et le comportement du tableau des clés sur téléphone : il ne doit pas élargir la page.

## 7. Vérification

- `make test` passe, politique de sécurité comprise.
- Deux tests nouveaux, écrits avant le CSS : aucune page ne contient de `<script>` ni de ressource externe, et la page de configuration déclare la palette claire et la palette sombre. Un troisième vérifie que « Révoquer » et « Créer une clé » portent les classes de bouton prévues.
- Le rendu est vu dans un navigateur, en clair et en sombre : la page de configuration avec la clé nouvelle, le tableau, les voix, puis sans voix, et la page de message. On ne dit pas que c'est terminé avant.
