# Pinpoint

A Django web app for running GPS-based scavenger hunts.

## What it does

**Game masters** (GMs) create routes — ordered sequences of GPS waypoints — via a browser-based interface with an interactive map. Each waypoint can advance the player in one of three ways:

- **Button** — player taps a button (optionally with custom text and button label)
- **Question / Riddle** — player must answer correctly (case-insensitive) to proceed
- **Auto-proximity** — player is automatically advanced once within a configured radius

Routes are shared with players via a URL or QR code. **Players** open the link in their browser, which uses the Geolocation API to show the distance in metres to the current (hidden) waypoint. Progress is stored in the Django session.

## Tech stack

- Django 4.2, SQLite, Python venv
- Bootstrap 5, Leaflet.js (map), SortableJS (drag-to-reorder)
- i18n: English and Dutch via Django's translation framework (`locale/`)
- No JavaScript build step — all JS is vanilla, loaded from CDN

## Running locally

```bash
source .venv/bin/activate
python manage.py migrate
python manage.py runserver
```

## Running tests

```bash
python manage.py test game
```

## Development rules

- **Always write tests when making changes.** New views, model changes, and business logic must be covered. Run the full suite before considering work done.
- **Always keep translations in sync.** Any user-facing string added or changed in a template must be wrapped with `{% trans %}` or `{% blocktrans %}`. After that, run `python manage.py makemessages -l en -l nl`, fill in the new `msgstr` entries in both `locale/en/LC_MESSAGES/django.po` and `locale/nl/LC_MESSAGES/django.po`, then run `python manage.py compilemessages -l en -l nl`. Never leave a `msgstr ""` blank.
- **`.mo` files are excluded from git.** Run `compilemessages` locally after editing `.po` files. The Dockerfile also runs it at build time.
- **Document every new environment variable in `README.md`.** Deployment is Elastic Beanstalk single-container Docker via `Dockerrun.aws.json`, which (version 1) has no `environment` section — Beanstalk injects environment properties straight into the container. So the configuration table in `README.md` is the only record of what needs setting; add new variables there, with their default, in the same change. (There is no `docker-compose.yml` any more.)
- **The map tile source is configuration, not code.** `MAP_TILE_URL`, `MAP_TILE_ATTRIBUTION` and `MAP_TILE_MAX_ZOOM` are settings passed into `route_edit`'s context, defaulting to OpenStreetMap's public server — which blocks clients it considers too heavy, so it must stay swappable by environment variable. Render both strings through `|escapejs`, never raw: the URL carries an API key and the attribution carries markup. `escapejs` encodes `<` and `=` as well as quotes, which is harmless — the browser recovers the original once the JS string is parsed, and Leaflet inserts the attribution as HTML. Any future template with a map should take these from settings too.
- **The version lives in `pinpoint/__init__.py` and is bumped by `cz bump`.** `__version__` is exposed to every template by `pinpoint.context_processors.version` (a context processor rather than view context, because Django's own `LoginView` renders the login page) and shown in the footer of the login and route start screens. Every file repeating the version must be listed under `version_files` in `.cz.yaml` — currently `Dockerrun.aws.json` and `pinpoint/__init__.py`. `game/tests.py` asserts the two agree, so drift fails the suite rather than shipping a stale footer.
- **Static files are served by WhiteNoise, and the build must collect them with `DEBUG=false`.** There is no nginx in front of gunicorn, and Django serves nothing static once `DEBUG` is off, so `whitenoise.middleware.WhiteNoiseMiddleware` sits directly after `SecurityMiddleware`. `STATICFILES_STORAGE` is only set to the manifest storage when `DEBUG` is false, so the `collectstatic` line in the `Dockerfile` has to pass `DEBUG=false` too — otherwise no `staticfiles.json` is written and every `{% static %}` lookup fails at runtime (a 500, worse than the missing-CSS 404 it replaced). Files in `public/` are served from the site root; that is where `favicon.ico` lives, and standalone templates link it with `<link rel="icon" href="/favicon.ico">`.
- **SQLite can be persisted to S3 instead of running Postgres.** `game/dbsync.py` plus the `dbsync` management command download the database on start and upload it after writes; `entrypoint.sh` runs the polling loop and flushes on `SIGTERM`. Three things there are load-bearing and easy to break: uploads must use `sqlite3.Connection.backup()` (a plain copy can catch a write mid-transaction), the dirty marker must be cleared *before* the upload (clearing after would drop a write that landed during it), and the signal receivers must be connected with `weak=False` (a receiver connected from `AppConfig.ready()` is otherwise garbage collected the moment `ready()` returns, silently recording nothing). This design only supports a single instance — see `README.md`.
- **The database is SQLite unless a host is configured.** `pinpoint/dbconfig.py` returns Postgres settings as soon as `DB_HOST` (or Beanstalk's `RDS_HOSTNAME`) is set, and raises `ImproperlyConfigured` if the name/user/password are then missing. It takes an explicit `env` mapping so it can be unit-tested without touching `os.environ` — keep it a pure function. Local development and the test suite must keep working with an empty environment.
- **Keep `.dockerignore` in sync.** Any file added to the repo that is not needed at container runtime (dev tooling, docs, config files, CI files) must be added to `.dockerignore` in the same change.
- **Keep `README.md` up to date.** When adding features, changing how the app is run, or modifying deployment, update `README.md` in the same change if the user-facing description or setup instructions are affected.
- **GM views require login; player views are public.** All route/waypoint management views use `@login_required` and filter by `owner=request.user`. Play views (`play_intro`, `play_start`, `play`, `play_advance`) have no auth — players access routes via an unguessable UUID token.
- Routes can only be deleted when inactive (`is_active=False`). The view enforces this server-side.
- **Waypoint images are stored under `waypoints/<route id>/`** via the `waypoint_image_path` callable in `models.py`, so a route's images share one prefix and go away together when the route is deleted. It uses `route_id`, not the waypoint's `pk`, so the image can be attached before the row is written. Deleting a route deletes those objects from S3 via the `post_delete` signal on `Waypoint`, which means the bucket policy needs `s3:DeleteObject` — without it, route deletion fails outright rather than leaving orphans. Never build image URLs by prepending scheme/host: `image.url` is already absolute under S3 storage, and doing so yields `https://app/https://s3…`.
- **Player progress lives on `Participant`, not in the session.** The session only holds the participant's id (`route_{pk}_participant`); `_current_participant` in `views.py` resolves it and carries over the pre-participant `route_{pk}_waypoint` key when it finds one. This exists so a game master can move a stuck team on — a session is unreachable from outside the browser that owns it. The team's page notices a skip by polling `play_state`, which **must stay read-only**: it is hit on a timer by every playing team, and any write there marks the database dirty and triggers an S3 upload. `game/tests.py` asserts that.
- **A question waypoint either gates or collects, per waypoint.** `Waypoint.require_correct_answer` defaults to `True`, which is the original behaviour: a wrong answer re-renders the same waypoint. With it off, `play_advance` stores the answer and advances anyway, and the game master judges the route afterwards on `participant_answers`. Either way the submission is written to an `Answer` (`update_or_create` on participant + waypoint, so a retry overwrites rather than piling up rows) — but only when the waypoint actually has a `question`, because a question waypoint without one renders a plain button. `Answer.is_correct` returns `None` when the waypoint has no expected answer, so a blank-against-blank comparison is never reported as correct; the template must keep distinguishing that from `False`.
- **The intro and the game are one document.** `play_intro` and `play` both render `play.html`, switched by a `started` flag: unstarted shows `#intro-card` and leaves `#waypoint-panel` empty (so the first waypoint's coordinates are not in the page before the player begins), started shows `#play-screen` with the waypoint included. Tapping Start posts to `play_start` with the `X-Requested-With` header and swaps in the first waypoint. This exists so the Start tap is the user gesture that unlocks audio for the whole route — do not split these back into separate pages without moving that unlock somewhere else. `play_start` keeps its plain redirect for non-JS clients.
- **Arrival feedback lives outside `#waypoint-panel`.** The chime (synthesised with the Web Audio API — there is no audio asset) and the 🔔 toggle sit in `#alert-bar` in `play.html`, because anything inside `#waypoint-panel` is destroyed by the fragment swap. `notifyArrival()` fires once per arrival; `arrived` guards against repeats. Audio stays suspended until the player's first gesture, so keep the "Tap to enable sound" hint working. `navigator.vibrate` must stay feature-guarded — it is absent on iOS Safari.
- **The play screen advances in place, without a page reload.** `game/templates/game/_play_waypoint.html` holds every part of the play screen that differs per waypoint; `play.html` includes it on first load and swaps it into `#waypoint-panel` afterwards. `play_advance` content-negotiates on the `X-Requested-With: XMLHttpRequest` header: with it, it returns JSON (`status` of `advanced` / `wrong_answer` / `finished`, plus `html` and the next waypoint's coordinates); without it, it redirects as before, so the plain form POST still works without JS. Keep both paths working, and put per-waypoint markup in the partial rather than `play.html` so the fragment stays in sync. Only the finished screen triggers a real navigation.
