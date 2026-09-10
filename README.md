# Pinpoint

A browser-based GPS scavenger hunt app. Game masters create routes with hidden waypoints on a map; players follow them in real time using their phone's location.

## How it works

**Game masters** log in and create routes — ordered sequences of GPS waypoints. Each waypoint can advance the player in one of three ways:

- **Button** — player taps a button (with optional descriptive text and custom label)
- **Question / Riddle** — player must answer correctly to proceed
- **Auto-proximity** — player is automatically advanced once they walk within a configured radius

The button or question is only revealed once the player physically arrives at the waypoint. Routes are shared via a URL or QR code. No app install required — it runs entirely in the browser.

**Players** open the link on their phone. The browser asks for location access, then shows the distance in metres to the next hidden waypoint. Once close enough, the advance UI appears. Progress is stored in the session so players can close and reopen the browser without losing their place.

Advancing from one waypoint to the next happens in place, without a page reload — the server returns a rendered fragment for the next waypoint. This keeps the GPS watch alive for the whole route instead of re-acquiring a fix on every waypoint. The plain form POST still works if JavaScript is unavailable.

On arrival the phone plays a short chime and vibrates, so players don't have to watch the screen while walking. A 🔔 toggle in the top-right corner mutes both and is remembered across sessions. Two platform caveats:

- **Vibration is Android-only.** `navigator.vibrate` is not implemented in iOS Safari, so iPhone players get sound only.
- **Sound is on by default**, and the tap on **Start** is what satisfies the browser's requirement for a user gesture before audio may play. The intro and the game are the same document for exactly this reason, so sound works from the very first waypoint. A "Tap to enable sound" hint appears only if audio is somehow still suspended.

## Tech stack

- Python 3.12+ / Django 4.2, SQLite (production container runs Python 3.13)
- Bootstrap 5, Leaflet.js (map), SortableJS (drag-to-reorder waypoints)
- Vanilla JS, no build step — all dependencies loaded from CDN
- i18n: English and Dutch

## Running locally

Requires **Python 3.12 or newer**. On macOS, avoid the system Python (`/usr/bin/python3`) — it is linked against LibreSSL, which makes `urllib3` emit `NotOpenSSLWarning` on every command. Use a Homebrew build instead: `brew install python@3.12`.

```bash
git clone <repo>
cd pinpoint

python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python manage.py migrate
python manage.py createsuperuser   # creates your GM account
python manage.py runserver
```

Open [http://localhost:8000](http://localhost:8000) and log in with the account you just created.

With no configuration the app uses SQLite and stores uploaded images under `media/`, so nothing above needs environment variables.

## Configuration

Everything is driven by environment variables. All are optional; leaving them unset gives the local SQLite + filesystem setup above.

| Variable | Default | Purpose |
| --- | --- | --- |
| `SECRET_KEY` | insecure dev key | **Set this in production.** |
| `DEBUG` | `true` | Set to `false` in production. |
| `ALLOWED_HOSTS` | `localhost 127.0.0.1` | Space-separated hostnames. |
| `CSRF_TRUSTED_ORIGINS` | empty | Space-separated origins, e.g. `https://pinpoint.example.com`. |
| `DB_HOST` | unset → SQLite | Set to an RDS endpoint to switch to Postgres. |
| `DB_NAME` / `DB_USER` / `DB_PASSWORD` | — | Required once `DB_HOST` is set. |
| `DB_PORT` | `5432` | |
| `DB_SSLMODE` | `require` | Use `verify-full` with `sslrootcert` for full certificate validation. |
| `DB_CONN_MAX_AGE` | `600` | Seconds to reuse a connection. `0` disables pooling. |
| `DB_CONNECT_TIMEOUT` | `5` | Seconds before giving up on connecting. |
| `SQLITE_S3_BUCKET` | unset → no sync | Bucket to persist the SQLite database in. See below. |
| `SQLITE_S3_KEY` | `db/db.sqlite3` | Object key for the database. |
| `SQLITE_SYNC_INTERVAL` | `30` | Seconds between upload checks. |
| `SQLITE_DIRTY_MARKER` | `/tmp/pinpoint-db-dirty` | File touched after each write. |
| `USE_S3` | `false` | `true` stores waypoint images in S3 instead of `media/`. |
| `AWS_STORAGE_BUCKET_NAME` | — | Required when `USE_S3=true`. |
| `AWS_S3_REGION_NAME` | unset | |
| `AWS_S3_ENDPOINT_URL` | unset | Only for S3-compatible stores (MinIO); leave unset for real S3. |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | unset | Omit to use the instance's IAM role. |

### Waypoint images in S3

With `USE_S3=true`, images are stored under `waypoints/<route id>/`, so everything belonging to one route shares a prefix. Deleting a waypoint deletes its image, and deleting a route deletes all of its waypoints' images — a `post_delete` signal on `Waypoint` calls `image.delete()`, and cascading a route delete fires it for each waypoint.

That means **`s3:DeleteObject` is required**, not just read and write:

```json
{
  "Effect": "Allow",
  "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
  "Resource": "arn:aws:s3:::YOUR-IMAGE-BUCKET/waypoints/*"
}
```

Without it, route deletion fails outright rather than merely leaving orphaned files: the error propagates out of `route.delete()`, Django rolls the transaction back, and the route stays in the database.

### Persisting SQLite in S3

An alternative to running Postgres. The container has no durable disk, so with `SQLITE_S3_BUCKET` set:

1. On start, `entrypoint.sh` downloads the database from S3. A missing object is fine — `migrate` creates a new one and it is uploaded immediately.
2. A Django signal touches `SQLITE_DIRTY_MARKER` after every write. Since reads vastly outnumber writes, nothing is uploaded when nothing changed.
3. A loop checks the marker every `SQLITE_SYNC_INTERVAL` seconds and uploads when it finds one. The marker is cleared *before* the upload, so a write landing mid-upload is picked up on the next pass rather than lost.
4. On `SIGTERM` (i.e. every deploy) the database is flushed to S3 one last time.

Uploads use SQLite's online-backup API rather than copying the file, which is safe while the app is serving requests.

#### Required IAM permissions

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:ListBucket"],
      "Resource": "arn:aws:s3:::YOUR-BUCKET"
    },
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject"],
      "Resource": "arn:aws:s3:::YOUR-BUCKET/db/db.sqlite3"
    }
  ]
}
```

`s3:ListBucket` is easy to overlook but required, and its absence is confusing: **S3 answers `403 Forbidden` instead of `404 Not Found` for an object that does not exist** when the caller cannot list the bucket, because a 404 would itself disclose whether the object is there. So on a first deploy — when there is legitimately no database yet — a policy without `ListBucket` produces a 403 that looks like a credentials problem.

There is no `s3:HeadObject` action to grant; `HeadObject` is authorised by `s3:GetObject`.

The app deliberately **refuses to start** on a 403 rather than assuming the database is missing. Assuming otherwise would mean starting empty and then uploading that empty database over a real one at the first write. The error message spells out the policy above.

Credentials come from boto3's standard chain — `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` if set, otherwise the instance's IAM role — the same chain used for image storage.

> **This supports exactly one instance.** Two containers would each hold their own copy and the last upload would silently discard the other's writes. Keep the Beanstalk environment at a single instance, and enable **bucket versioning** so a bad overwrite can be recovered.
>
> Anything written since the last upload is lost if the instance dies without a `SIGTERM` (autoscaling replacement, hardware failure, `docker kill`). The exposure is at most `SQLITE_SYNC_INTERVAL` seconds of writes. Use Postgres instead if that is unacceptable.

Setting `DB_HOST` disables all of this — the sync only applies when SQLite is the backend.

### Using Postgres (RDS)

Setting `DB_HOST` switches the app from SQLite to Postgres; `DB_NAME`, `DB_USER` and `DB_PASSWORD` then become mandatory and the app refuses to start without them rather than half-connecting. TLS is required by default.

If you attach an RDS instance to an Elastic Beanstalk environment, Beanstalk injects `RDS_HOSTNAME`, `RDS_PORT`, `RDS_DB_NAME`, `RDS_USERNAME` and `RDS_PASSWORD`. Those are picked up automatically, so no extra configuration is needed in that case. Explicit `DB_*` variables take precedence when both are present.

The container runs `manage.py migrate` on startup, so schema changes are applied on deploy.

## Running tests

```bash
python manage.py test game
```

The suite runs on SQLite by default. To run it against Postgres, export the `DB_*` variables first.

## Deployment

Static files (the admin's CSS and JS) are served by [WhiteNoise](https://whitenoise.readthedocs.io/) straight from gunicorn — there is no nginx or CloudFront in front of the app, and Django itself will not serve static files once `DEBUG=false`. `collectstatic` runs at image build time **with `DEBUG=false`**, which is what produces the hashed filenames and `staticfiles.json`; collecting with `DEBUG=true` would leave no manifest and every `{% static %}` lookup would then fail at runtime.

Anything in `public/` is served from the site root, which is how `/favicon.ico` is delivered.

The app ships as a Docker image, deployed to AWS Elastic Beanstalk as a single-container Docker application via `Dockerrun.aws.json`. Environment variables are configured on the Beanstalk environment and injected into the container — `Dockerrun.aws.json` version 1 has no `environment` section, so the table above is the reference for what to set.

To build and push a new image:

```bash
docker build -t docker.io/rschoof/pinpoint:vX.Y.Z .
docker push docker.io/rschoof/pinpoint:vX.Y.Z
```

Version bumps are managed with [Commitizen](https://commitizen-tools.github.io/commitizen/) (`cz bump`), which updates the image tag in `Dockerrun.aws.json` and generates a changelog.
