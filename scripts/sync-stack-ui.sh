#!/bin/sh
# Refait la copie de Stack UI que dictee embarque, depuis un clone du dépôt stack-ui dont le chemin est l'argument :
#   scripts/sync-stack-ui.sh <chemin du clone de stack-ui>
# La copie tient dans trois endroits, tous réécrits ici et jamais modifiés à la main :
#   - dictee/src/dictee/templates/stack-ui/stack-ui.css : tokens.css puis components/bundle.css, avec l'URL de la police
#     pointée sur la route /fonts/ de l'application ;
#   - dictee/src/dictee/templates/stack-ui/VERSION : le hash court du commit de stack-ui copié ;
#   - dictee/src/dictee/static/fonts/ : la police Figtree et sa licence.
set -eu

if [ "$#" -ne 1 ] || [ ! -f "$1/tokens.css" ]; then
  echo "usage : $0 <chemin du clone de stack-ui>" >&2
  exit 2
fi
source_dir=$1
repo=$(cd "$(dirname "$0")/.." && pwd)
templates=$repo/dictee/src/dictee/templates/stack-ui
fonts=$repo/dictee/src/dictee/static/fonts

# Un arbre modifié ferait mentir VERSION : la copie doit venir d'un commit.
if [ -n "$(git -C "$source_dir" status --porcelain)" ]; then
  echo "le clone de stack-ui a des modifications non validées : la copie ne correspondrait à aucun commit" >&2
  exit 1
fi
version=$(git -C "$source_dir" rev-parse --short HEAD)

mkdir -p "$templates" "$fonts"
{
  echo "/* Stack UI, commit $version : copie de tokens.css puis de components/bundle.css du dépôt stack-ui. Ne pas modifier à la main, scripts/sync-stack-ui.sh la refait. */"
  cat "$source_dir/tokens.css"
  cat "$source_dir/components/bundle.css"
} | sed 's#url("fonts/Figtree-Variable.ttf")#url("/fonts/Figtree-Variable.ttf")#' > "$templates/stack-ui.css"

if ! grep -q 'url("/fonts/Figtree-Variable.ttf")' "$templates/stack-ui.css"; then
  echo "l'URL de la police n'a pas été trouvée dans tokens.css : le @font-face a changé" >&2
  exit 1
fi
cp "$source_dir/fonts/Figtree-Variable.ttf" "$source_dir/fonts/OFL.txt" "$fonts/"
echo "$version" > "$templates/VERSION"
echo "Stack UI $version copié."
