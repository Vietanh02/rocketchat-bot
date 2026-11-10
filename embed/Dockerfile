# The embedding server (text-embeddings-inference) with its model inside,
# for machines that cannot reach huggingface.co.
#
#   deploy/fetch-embed-model.sh      # fills embed/model/ (once)
#   docker compose build embed
#
# With embed/model/ empty the image is the plain server: it downloads the
# model from Hugging Face on first start, into the embed_models volume.
ARG TEI_IMAGE=ghcr.io/huggingface/text-embeddings-inference:cpu-1.8.3
FROM ${TEI_IMAGE}

COPY model/ /models/multilingual-e5-small/
COPY start.sh /usr/local/bin/start-embed
RUN chmod 755 /usr/local/bin/start-embed && rm -f /models/multilingual-e5-small/README.md

ENTRYPOINT ["start-embed"]
CMD ["--json-output"]
