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

Uploaded images are stored under `media/` by default. To use S3 instead, set `USE_S3=true` and the required `AWS_*` environment variables (see `docker-compose.yml` for the full list).

## Running tests

```bash
python manage.py test game
```

## Deployment

The app ships as a Docker image. `docker-compose.yml` is the entrypoint for production (tested on AWS Elastic Beanstalk single-container Docker). Environment variables are passed in via the host; see `docker-compose.yml` for the full list.

To build and push a new image:

```bash
docker build -t docker.io/rschoof/pinpoint:vX.Y.Z .
docker push docker.io/rschoof/pinpoint:vX.Y.Z
```

Version bumps are managed with [Commitizen](https://commitizen-tools.github.io/commitizen/) (`cz bump`), which updates the version in `docker-compose.yml` and generates a changelog.
