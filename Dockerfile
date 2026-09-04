FROM python:3.13-alpine

# gettext is required for compilemessages
RUN apk add --no-cache gettext

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Compile translation catalogues
RUN python manage.py compilemessages -l en -l nl

# Collect static files (served separately in production, but available in the image)
RUN SECRET_KEY=build-only python manage.py collectstatic --noinput

RUN chmod +x entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["./entrypoint.sh"]
