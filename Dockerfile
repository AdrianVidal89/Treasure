FROM python:3.12-slim

WORKDIR /app

# Cliente de PostgreSQL (pg_dump/pg_restore) para el backup/restauración
# desde el panel admin. Se instala la versión 16 exacta (vía el repositorio
# oficial PGDG) para que coincida con la versión del servidor (postgres:16).
RUN apt-get update && apt-get install -y --no-install-recommends curl gnupg ca-certificates && \
    . /etc/os-release && \
    curl -o /usr/share/keyrings/postgresql.asc --fail https://www.postgresql.org/media/keys/ACCC4CF8.asc && \
    gpg --dearmor -o /usr/share/keyrings/postgresql-archive-keyring.gpg /usr/share/keyrings/postgresql.asc && \
    echo "deb [signed-by=/usr/share/keyrings/postgresql-archive-keyring.gpg] https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" > /etc/apt/sources.list.d/pgdg.list && \
    apt-get update && apt-get install -y --no-install-recommends postgresql-client-16 && \
    apt-get purge -y curl gnupg && apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/* /usr/share/keyrings/postgresql.asc

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2"]
