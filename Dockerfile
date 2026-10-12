FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DB_PATH=/data/finance.db

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Version label baked in by CI (git commit), shown in the app footer
ARG APP_VERSION=dev
ENV APP_VERSION=${APP_VERSION}

# Runs as an unprivileged user; the entrypoint fixes ownership of a fresh ./data bind mount first
RUN useradd -u 1000 -m app && mkdir -p /data && chown app /data \
 && printf '#!/bin/sh\nchown -R app:app /data 2>/dev/null || true\nexec setpriv --reuid=app --regid=app --init-groups "$@"\n' > /entrypoint.sh \
 && chmod +x /entrypoint.sh
VOLUME /data
EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/health').status==200 else 1)"

ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
