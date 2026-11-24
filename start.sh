#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

echo "============================================================"
echo "  PodcastForge Studio - Iniciando aplicación local"
echo "============================================================"

if [ ! -f ".env" ] && [ -f ".env.example" ]; then
  cp .env.example .env
  echo "[AVISO] Se creó .env desde .env.example. Añade tus claves antes de generar vídeos."
fi

if [ ! -d ".venv" ]; then
  echo "[INFO] Creando entorno virtual Python..."
  if command -v uv >/dev/null 2>&1; then
    uv venv .venv --python 3.12
    uv pip install --python .venv/bin/python -r requirements.txt
  else
    python3 -m venv .venv
    .venv/bin/pip install --upgrade pip
    .venv/bin/pip install -r requirements.txt
  fi
fi

if [ ! -f "frontend/dist/index.html" ] && [ -f "frontend/package.json" ]; then
  echo "[INFO] Compilando interfaz web..."
  (cd frontend && npm install && npm run build)
fi

APP_HOST="${HOST:-127.0.0.1}"
APP_PORT="${PORT:-8000}"
if [ -f ".env" ]; then
  ENV_HOST=$(grep -E '^HOST=' .env | head -n1 | cut -d'=' -f2- | cut -d'#' -f1 | tr -d '[:space:]' || true)
  ENV_PORT=$(grep -E '^PORT=' .env | head -n1 | cut -d'=' -f2- | cut -d'#' -f1 | tr -d '[:space:]' || true)
  [ -n "$ENV_HOST" ] && APP_HOST="$ENV_HOST"
  [ -n "$ENV_PORT" ] && APP_PORT="$ENV_PORT"
fi

.venv/bin/python cli.py doctor --skip-api || true

URL="http://${APP_HOST}:${APP_PORT}"
echo "[INFO] Abriendo ${URL}..."
(sleep 2 && (command -v open >/dev/null 2>&1 && open "$URL" || (command -v xdg-open >/dev/null 2>&1 && xdg-open "$URL" || true))) &

exec .venv/bin/python -m uvicorn backend.main:app --host "$APP_HOST" --port "$APP_PORT"
