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
- **Keep `docker-compose.yml` environment list in sync.** Any new environment variable introduced in `settings.py` must be added to the `environment:` section of `docker-compose.yml` so it is forwarded from the Elastic Beanstalk host into the container.
- **Keep `.dockerignore` in sync.** Any file added to the repo that is not needed at container runtime (dev tooling, docs, config files, CI files) must be added to `.dockerignore` in the same change.
- **Keep `README.md` up to date.** When adding features, changing how the app is run, or modifying deployment, update `README.md` in the same change if the user-facing description or setup instructions are affected.
- **GM views require login; player views are public.** All route/waypoint management views use `@login_required` and filter by `owner=request.user`. Play views (`play_intro`, `play_start`, `play`, `play_advance`) have no auth — players access routes via an unguessable UUID token.
- Routes can only be deleted when inactive (`is_active=False`). The view enforces this server-side.
- Player progress is stored in the Django session keyed as `route_{pk}_waypoint`. The `play_start` view resets it to 0.
- **The intro and the game are one document.** `play_intro` and `play` both render `play.html`, switched by a `started` flag: unstarted shows `#intro-card` and leaves `#waypoint-panel` empty (so the first waypoint's coordinates are not in the page before the player begins), started shows `#play-screen` with the waypoint included. Tapping Start posts to `play_start` with the `X-Requested-With` header and swaps in the first waypoint. This exists so the Start tap is the user gesture that unlocks audio for the whole route — do not split these back into separate pages without moving that unlock somewhere else. `play_start` keeps its plain redirect for non-JS clients.
- **Arrival feedback lives outside `#waypoint-panel`.** The chime (synthesised with the Web Audio API — there is no audio asset) and the 🔔 toggle sit in `#alert-bar` in `play.html`, because anything inside `#waypoint-panel` is destroyed by the fragment swap. `notifyArrival()` fires once per arrival; `arrived` guards against repeats. Audio stays suspended until the player's first gesture, so keep the "Tap to enable sound" hint working. `navigator.vibrate` must stay feature-guarded — it is absent on iOS Safari.
- **The play screen advances in place, without a page reload.** `game/templates/game/_play_waypoint.html` holds every part of the play screen that differs per waypoint; `play.html` includes it on first load and swaps it into `#waypoint-panel` afterwards. `play_advance` content-negotiates on the `X-Requested-With: XMLHttpRequest` header: with it, it returns JSON (`status` of `advanced` / `wrong_answer` / `finished`, plus `html` and the next waypoint's coordinates); without it, it redirects as before, so the plain form POST still works without JS. Keep both paths working, and put per-waypoint markup in the partial rather than `play.html` so the fragment stays in sync. Only the finished screen triggers a real navigation.
