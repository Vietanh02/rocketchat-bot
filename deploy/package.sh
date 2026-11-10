#!/usr/bin/env bash
# Build a release package of the bot: source, Docker images, checksums.
#
#   deploy/package.sh              # source + bot images (backend, portal, embed)
#   deploy/package.sh --with-deps  # also the Qdrant image
#   deploy/package.sh --no-images  # source only
#
# Output: release/rocketchat-bot-<version>/ and a .tar of that folder.
# The package is made from the committed tree (git archive HEAD), so commit
# first; uncommitted changes are left out. See DEPLOY.md for using it.
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT=$(pwd)

with_images=1
with_deps=0
for arg in "$@"; do
  case "$arg" in
    --with-deps) with_deps=1 ;;
    --no-images) with_images=0 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

VERSION=$(tr -d '[:space:]' < VERSION)
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "VERSION is not x.y.z: $VERSION" >&2; exit 1; }

# The version is written in three places; they must agree.
grep -q "^version = \"$VERSION\"" backend/pyproject.toml || { echo "backend/pyproject.toml is not at $VERSION" >&2; exit 1; }
grep -q "\"version\": \"$VERSION\"" portal/package.json || { echo "portal/package.json is not at $VERSION" >&2; exit 1; }
grep -q "__version__ = \"$VERSION\"" backend/app/__init__.py || { echo "backend/app/__init__.py is not at $VERSION" >&2; exit 1; }

if ! git diff --quiet HEAD -- . ':!release'; then
  echo "warning: uncommitted changes are not in the package (it is built from HEAD)" >&2
fi
COMMIT=$(git rev-parse --short HEAD)

NAME="rocketchat-bot-$VERSION"
OUT="$ROOT/release/$NAME"
rm -rf "$OUT"
mkdir -p "$OUT/images"

echo "==> Source ($COMMIT)"
# Unpacks to rocketchat-bot/, apart from the package folder.
git archive --format=tar.gz --prefix="rocketchat-bot/" -o "$OUT/$NAME-source.tar.gz" HEAD
cp DEPLOY.md "$OUT/"

if [[ $with_images -eq 1 ]]; then
  echo "==> Building the bot images at $VERSION"
  # Built from the committed source, not the working tree.
  build=$(mktemp -d)
  trap 'rm -rf "$build"' EXIT
  tar -xzf "$OUT/$NAME-source.tar.gz" -C "$build"
  # The embedding model is not in git; built in from embed/model/, the
  # image needs no huggingface.co where it is installed.
  if [[ -f embed/model/onnx/model.onnx ]]; then
    cp -r embed/model/. "$build/rocketchat-bot/embed/model/"
  else
    echo "warning: embed/model/ is empty (deploy/fetch-embed-model.sh); the embed image" >&2
    echo "         will download its model from Hugging Face on first start" >&2
  fi
  (cd "$build/rocketchat-bot" && BOT_VERSION="$VERSION" docker compose build backend portal embed)

  save() {  # image, file
    echo "==> Saving $1"
    docker save "$1" | gzip -1 > "$OUT/images/$2.tar.gz"
  }
  save "rocketchat-bot-backend:$VERSION" backend
  save "rocketchat-bot-portal:$VERSION" portal
  save "rocketchat-bot-embed:$VERSION" embed
  if [[ $with_deps -eq 1 ]]; then
    for image in $(cd "$build/rocketchat-bot" && docker compose config --images | grep -v '^rocketchat-bot-'); do
      docker image inspect "$image" >/dev/null 2>&1 || docker pull "$image"
      save "$image" "$(echo "$image" | tr '/:' '__')"
    done
  fi
else
  rmdir "$OUT/images"
fi

cat > "$OUT/RELEASE.txt" <<EOF
rocketchat-bot $VERSION
commit: $COMMIT
built: $(date -u +%Y-%m-%dT%H:%M:%SZ)
images: $([[ $with_images -eq 1 ]] && ls "$OUT/images" | tr '\n' ' ' || echo none)
EOF

echo "==> Checksums"
(cd "$OUT" && find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS)

echo "==> Archive"
tar -C "$ROOT/release" -cf "$ROOT/release/$NAME.tar" "$NAME"

echo
echo "Done: release/$NAME.tar ($(du -h "$ROOT/release/$NAME.tar" | cut -f1))"
cat "$OUT/RELEASE.txt"
