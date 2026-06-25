#!/bin/bash
# Script de deploy para el NAS QNAP.
# Ejecutar desde el directorio del proyecto: sh deploy.sh
set -e

export DOCKER_CONFIG=/tmp/dockercfg
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"

echo "=== [1/7] Backup .env ==="
if [ -f "$APP_DIR/.env" ]; then
    cp "$APP_DIR/.env" "$APP_DIR/.env.bak.$TIMESTAMP"
    echo "  Guardado en .env.bak.$TIMESTAMP"
else
    echo "  ADVERTENCIA: no se encontro .env antes del reset."
fi

echo "=== [2/7] Backup PostgreSQL ==="
DUMP_FILE="$APP_DIR/pg_dump_$TIMESTAMP.sql"
docker compose -f "$APP_DIR/docker-compose.yml" exec -T db \
    pg_dump -U "${POSTGRES_USER:-treasure}" "${POSTGRES_DB:-treasure}" \
    > "$DUMP_FILE"
echo "  Volcado guardado en $DUMP_FILE"

echo "=== [3/7] git fetch + reset ==="
git -C "$APP_DIR" fetch origin main
git -C "$APP_DIR" reset --hard origin/main

echo "=== [4/7] Restaurar .env de produccion ==="
# git reset --hard borra .env si estaba trackeado en el commit anterior.
# Lo restauramos siempre desde el backup.
LATEST_BAK="$(ls -t "$APP_DIR"/.env.bak.* 2>/dev/null | head -1)"
if [ ! -f "$APP_DIR/.env" ]; then
    if [ -n "$LATEST_BAK" ]; then
        cp "$LATEST_BAK" "$APP_DIR/.env"
        echo "  .env restaurado desde $LATEST_BAK"
    else
        echo "ERROR: .env no existe y no hay backup. Abortando." >&2
        exit 1
    fi
else
    echo "  .env presente (no tocado por git reset)."
fi

echo "=== [5/7] Permisos (excluyendo postgres_data) ==="
find "$APP_DIR" -not -path "*/postgres_data*" -not -path "*/.git*" \
    -exec chown root:root {} + 2>/dev/null || true

echo "=== [6/7] Reiniciar contenedores ==="
docker compose -f "$APP_DIR/docker-compose.yml" down
docker compose -f "$APP_DIR/docker-compose.yml" up -d --build

echo "=== [7/7] Deploy completado ==="
echo "  Las migraciones se aplican automaticamente al arrancar el contenedor web."
