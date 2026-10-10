FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FLASK_APP=wsgi.py \
    BFC_DATABASE_URI=sqlite:////data/controller.db \
    BFC_CAMERA_DIR=/data/camera

WORKDIR /srv/app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Unprivileged service account; /data is the persistent volume (database + camera snapshots).
RUN useradd --system --uid 10001 --home-dir /data --shell /usr/sbin/nologin bambufc \
    && mkdir -p /data/camera \
    && chown -R bambufc:bambufc /data \
    && chmod +x /srv/app/docker-entrypoint.sh

USER bambufc
VOLUME ["/data"]
EXPOSE 8092

ENTRYPOINT ["/srv/app/docker-entrypoint.sh"]
CMD ["web"]
