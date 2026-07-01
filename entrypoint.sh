#!/bin/sh
# Entrypoint para Fly.io
# Grava as credenciais GCP em disco antes de iniciar o app,
# pois o Fly.io passa secrets como variáveis de ambiente, não como arquivos.

if [ -n "$GOOGLE_APPLICATION_CREDENTIALS_JSON" ]; then
    echo "$GOOGLE_APPLICATION_CREDENTIALS_JSON" > /tmp/gcp-sa.json
    export GOOGLE_APPLICATION_CREDENTIALS=/tmp/gcp-sa.json
    echo "[entrypoint] Credenciais GCP configuradas."
fi

exec python web_app.py
