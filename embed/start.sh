#!/bin/sh
# The model built into the image if there is one, else the one on Hugging Face.
set -e
LOCAL=/models/multilingual-e5-small
if [ -f "$LOCAL/onnx/model.onnx" ] || [ -f "$LOCAL/model.safetensors" ]; then
  echo "embed: using the model built into the image ($LOCAL)"
  export HF_HUB_OFFLINE=1
  exec text-embeddings-router --model-id "$LOCAL" "$@"
fi
echo "embed: no model in the image; downloading intfloat/multilingual-e5-small"
exec text-embeddings-router --model-id intfloat/multilingual-e5-small "$@"
