FROM python:3.14-alpine3.24

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apk add --no-cache \
      bash coreutils curl jq mongodb-tools openssh-client postgresql-client \
      redis restic rsync sshpass tar util-linux \
    && mkdir -p /app/backend /app/static /scripts /var/backups

WORKDIR /app

COPY frontend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --requirement /app/requirements.txt

COPY frontend/app.py /app/app.py
COPY frontend/static/ /app/static/
COPY backend/ /app/backend/
COPY scripts/ /scripts/
RUN chmod 0555 /scripts/*.sh /scripts/*.py

EXPOSE 5000

CMD ["gunicorn", "--bind=0.0.0.0:5000", "--workers=2", "--threads=4", "--timeout=300", "--access-logfile=-", "--error-logfile=-", "app:app"]
