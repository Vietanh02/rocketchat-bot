#!/usr/bin/env bash
# Put the embedding model into embed/model/, to be built into the embed image
# (for machines that cannot reach huggingface.co).
#
#   deploy/fetch-embed-model.sh              # from the embed_models volume, else Hugging Face
#   deploy/fetch-embed-model.sh --from-hub   # always download (via HTTPS_PROXY if set)
#
# Then: docker compose build embed && docker compose up -d embed
# A release package (deploy/package.sh) carries the image with the model in it.
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT=$(pwd)
OUT="$ROOT/embed/model"
REPO=intfloat/multilingual-e5-small
# Pinned, so every build has the same vectors.
REVISION=614241f622f53c4eeff9890bdc4f31cfecc418b3
# What text-embeddings-inference reads (the CPU image runs the ONNX model).
FILES="config.json tokenizer.json tokenizer_config.json sentence_bert_config.json 1_Pooling/config.json onnx/model.onnx"
TEI_IMAGE=ghcr.io/huggingface/text-embeddings-inference:cpu-1.8.3

from_hub=0
for arg in "$@"; do
  case "$arg" in
    --from-hub) from_hub=1 ;;
    -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

# The volume docker compose made for embed: <project>_embed_models.
PROJECT=${COMPOSE_PROJECT_NAME:-$(basename "$ROOT" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-')}
VOLUME="${PROJECT}_embed_models"
SNAPSHOT="/data/models--${REPO/\//--}/snapshots/$REVISION"

mkdir -p "$OUT"
if [[ $from_hub -eq 0 ]] && docker volume inspect "$VOLUME" >/dev/null 2>&1 \
  && docker run --rm -v "$VOLUME:/data:ro" --entrypoint sh "$TEI_IMAGE" -c "test -f $SNAPSHOT/onnx/model.onnx"; then
  echo "==> Copying the model from the volume $VOLUME"
  # The snapshot holds links into blobs/; -L copies the files themselves.
  docker run --rm -v "$VOLUME:/data:ro" -v "$OUT:/out" --entrypoint sh "$TEI_IMAGE" \
    -c "cp -rL $SNAPSHOT/. /out/ && chown -R $(id -u):$(id -g) /out"
else
  echo "==> Downloading $REPO@$REVISION from Hugging Face"
  curl_opts=(-fL --retry 3 --create-dirs)
  # A proxy that re-signs certificates: APP_TLS_VERIFY=false in .env.
  if [[ -f .env ]] && grep -qiE '^APP_TLS_VERIFY=false' .env; then curl_opts+=(-k); fi
  for file in $FILES; do
    echo "    $file"
    curl "${curl_opts[@]}" -o "$OUT/$file" "https://huggingface.co/$REPO/resolve/$REVISION/$file"
  done
fi

for file in $FILES; do
  [[ -s "$OUT/$file" ]] || { echo "missing: embed/model/$file" >&2; exit 1; }
done
echo "Done: embed/model/ ($(du -sh "$OUT" | cut -f1)). Next: docker compose build embed"
