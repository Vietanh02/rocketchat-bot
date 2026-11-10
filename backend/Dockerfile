FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first, so code edits do not invalidate the install layer.
COPY requirements.txt ./
# HTTP_PROXY / HTTPS_PROXY / NO_PROXY reach pip as build args (docker compose
# passes them from .env). BUILD_TLS_VERIFY=false trusts PyPI without checking
# its certificate, for a proxy that re-signs it.
ARG BUILD_TLS_VERIFY=true
RUN if [ "$BUILD_TLS_VERIFY" = "false" ]; then \
      export PIP_TRUSTED_HOST="pypi.org files.pythonhosted.org"; \
    fi; \
    pip install --no-cache-dir -r requirements.txt

COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app

# The SQLite file lives here; mount a volume to keep it across rebuilds.
RUN mkdir -p /app/data
VOLUME ["/app/data"]

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
