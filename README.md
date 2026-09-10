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
| `USE_S3` | `false` | `true` stores waypoint images in S3 instead of `media/`. |
| `AWS_STORAGE_BUCKET_NAME` | — | Required when `USE_S3=true`. |
| `AWS_S3_REGION_NAME` | unset | |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | unset | Omit to use the instance's IAM role. |

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

The app ships as a Docker image, deployed to AWS Elastic Beanstalk as a single-container Docker application via `Dockerrun.aws.json`. Environment variables are configured on the Beanstalk environment and injected into the container — `Dockerrun.aws.json` version 1 has no `environment` section, so the table above is the reference for what to set.

To build and push a new image:

```bash
docker build -t docker.io/rschoof/pinpoint:vX.Y.Z .
docker push docker.io/rschoof/pinpoint:vX.Y.Z
```

Version bumps are managed with [Commitizen](https://commitizen-tools.github.io/commitizen/) (`cz bump`), which updates the image tag in `Dockerrun.aws.json` and generates a changelog.
