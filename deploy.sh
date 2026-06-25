#!/bin/bash
# Script de deploy para el NAS QNAP.
# Ejecutar desde el directorio del proyecto: sh deploy.sh
set -e

export DOCKER_CONFIG=/tmp/dockercfg
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"

echo "=== [1/6] Backup .env ==="
if [ -f "$APP_DIR/.env" ]; then
    cp "$APP_DIR/.env" "$APP_DIR/.env.bak.$TIMESTAMP"
    echo "  Guardado en .env.bak.$TIMESTAMP"
else
    echo "  ADVERTENCIA: no se encontro .env antes del reset."
fi

echo "=== [2/6] Backup PostgreSQL ==="
DUMP_FILE="$APP_DIR/pg_dump_$TIMESTAMP.sql"
docker compose -f "$APP_DIR/docker-compose.yml" exec -T db \
    sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' \
    > "$DUMP_FILE"
echo "  Volcado guardado en $DUMP_FILE"

echo "=== [3/6] git fetch + reset ==="
git -C "$APP_DIR" fetch origin main
git -C "$APP_DIR" reset --hard origin/main

echo "=== [4/6] Restaurar .env de produccion ==="
# En la primera ejecucion tras este commit, git reset --hard eliminara .env
# (estaba trackeado en el commit anterior). Lo restauramos desde el backup.
if [ ! -f "$APP_DIR/.env" ]; then
    LATEST_BAK="$(ls -t "$APP_DIR"/.env.bak.* 2>/dev/null | head -1)"
    if [ -n "$LATEST_BAK" ]; then
        cp "$LATEST_BAK" "$APP_DIR/.env"
        echo "  .env restaurado desde $LATEST_BAK"
    else
        echo "ERROR: .env no existe y no hay backup. Abortando." >&2
        exit 1
    fi
else
    echo "  .env presente (no afectado por git reset)."
fi

echo "=== [5/6] Reiniciar contenedores ==="
docker compose -f "$APP_DIR/docker-compose.yml" down
docker compose -f "$APP_DIR/docker-compose.yml" up -d

echo "=== [6/6] Deploy completado ==="
echo "  Las migraciones se aplican automaticamente al arrancar el contenedor web."
