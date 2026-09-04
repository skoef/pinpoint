# Speurtocht

A Django web app for running GPS-based scavenger hunts ("speurtocht" is Dutch for scavenger hunt).

## What it does

**Game masters** create routes — ordered sequences of GPS waypoints — via a browser-based interface with an interactive map. Each waypoint can advance the player in one of three ways:

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
- Routes can only be deleted when inactive (`is_active=False`). The view enforces this server-side.
- Player progress is stored in the Django session keyed as `route_{pk}_waypoint`. The `play_start` view resets it to 0.
