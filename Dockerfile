FROM python:3.13-alpine

# gettext for compilemessages; libpq is the Postgres client library psycopg
# links against at runtime.
RUN apk add --no-cache gettext libpq

WORKDIR /app

COPY requirements.txt .
# psycopg[c] has no musl wheels, so it is compiled here. The toolchain is
# installed as a virtual package and removed again, keeping it out of the image.
RUN apk add --no-cache --virtual .build-deps gcc musl-dev postgresql-dev \
 && pip install --no-cache-dir -r requirements.txt \
 && apk del .build-deps

COPY . .

# Compile translation catalogues
RUN python manage.py compilemessages -l en -l nl

# Collect static files, served at runtime by WhiteNoise.
# DEBUG=false matters: it is what selects the manifest storage, so the hashed
# names and staticfiles.json are built here. Collecting with DEBUG=true would
# leave no manifest, and the runtime (DEBUG=false) would then fail every
# {% static %} lookup instead of merely 404ing.
RUN SECRET_KEY=build-only DEBUG=false python manage.py collectstatic --noinput

RUN chmod +x entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["./entrypoint.sh"]
