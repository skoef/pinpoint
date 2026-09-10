#!/bin/sh
set -e

# Restore the SQLite database from S3, if that is how this deployment persists
# data. No-ops when SQLITE_S3_BUCKET is unset or Postgres is configured.
python manage.py dbsync --download

python manage.py migrate --noinput

SYNC_INTERVAL="${SQLITE_SYNC_INTERVAL:-30}"
SYNC_PID=""

if [ -n "${SQLITE_S3_BUCKET:-}" ]; then
    # migrate itself writes django_migrations, so on a first deploy this stores
    # the freshly created database straight away instead of waiting an interval.
    python manage.py dbsync --upload --if-dirty || true

    # Writes are rare compared to reads, so rather than uploading on every
    # change a signal touches a marker file and this loop picks it up.
    while true; do
        sleep "$SYNC_INTERVAL"
        python manage.py dbsync --upload --if-dirty || true
    done &
    SYNC_PID=$!
    echo "sqlite->s3 sync every ${SYNC_INTERVAL}s (pid ${SYNC_PID})"
fi

# Not exec'd: the shell has to stay alive to hold the trap below, which is what
# gives us one last upload when Beanstalk stops the container on a deploy.
gunicorn pinpoint.wsgi:application \
    --bind 0.0.0.0:8000 \
    --workers 2 \
    --timeout 60 &
APP_PID=$!

shutdown() {
    trap - TERM INT
    [ -n "$SYNC_PID" ] && kill "$SYNC_PID" 2>/dev/null || true
    kill -TERM "$APP_PID" 2>/dev/null || true
    wait "$APP_PID" 2>/dev/null || true
    if [ -n "${SQLITE_S3_BUCKET:-}" ]; then
        echo "flushing database to S3 before exit"
        # Unconditional: this is the last chance to persist anything.
        python manage.py dbsync --upload || true
    fi
    exit 0
}
trap shutdown TERM INT

wait "$APP_PID"
