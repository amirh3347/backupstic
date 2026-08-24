#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$PROJECT_DIR/configs/backup.env"
SSH_DIR="$PROJECT_DIR/data/config-backup"

for command_name in docker openssl ssh-keygen; do
    command -v "$command_name" >/dev/null 2>&1 || {
        echo "Missing required command: $command_name" >&2
        exit 1
    }
done
docker compose version >/dev/null 2>&1 || {
    echo "Docker Compose v2 is required (docker compose)." >&2
    exit 1
}

umask 077
mkdir -p "$SSH_DIR"

generated_password=""
if [ ! -f "$ENV_FILE" ]; then
    restic_password="$(openssl rand -base64 48 | tr -d '\n')"
    profile_secret="$(openssl rand -base64 48 | tr -d '\n')"
    jwt_secret="$(openssl rand -base64 48 | tr -d '\n')"
    generated_password="$(openssl rand -base64 24 | tr -d '\n')"
    {
        printf 'RESTIC_PASSWORD=%s\n' "$restic_password"
        printf 'PROFILE_SECRET_KEY=%s\n' "$profile_secret"
        printf 'JWT_SECRET_KEY=%s\n' "$jwt_secret"
        printf 'ADMIN_USERNAME=admin\n'
        printf 'ADMIN_PASSWORD=%s\n' "$generated_password"
        printf 'JWT_ACCESS_TOKEN_EXPIRES=3600\n'
        printf 'RESTIC_REPO=/var/backups/restic-repo\n'
        printf 'PROFILES_STORAGE=/var/backups/profiles.json\n'
        printf 'RESTIC_REPOSITORIES_STORAGE=/var/backups/restic_repositories.json\n'
        printf 'RETENTION_DAYS=30\nRETENTION_WEEKLY=12\nRETENTION_MONTHLY=12\nLOCAL_RETENTION_DAYS=7\n'
        printf 'CONFIG_BACKUP_SSH_PRIVATE_KEY=/run/secrets/config-backup/id_ed25519\n'
        printf 'CONFIG_BACKUP_SSH_PUBLIC_KEY_FILE=/run/secrets/config-backup/id_ed25519.pub\n'
        printf 'CONFIG_BACKUP_SSH_KNOWN_HOSTS=/run/secrets/config-backup/known_hosts\n'
        printf 'CONFIG_BACKUP_SSH_CONNECT_TIMEOUT=10\n'
        printf 'CONFIG_BACKUP_SSH_HOST_KEY_CHECKING=accept-new\n'
        printf 'BACKUP_CRON=0 2 * * * /scripts/backup-all.sh full >> /var/backups/cron.log 2>&1\n'
        printf 'BACKUP_LOCK_WAIT_SECONDS=21600\n'
        printf 'MONITOR_INTERVAL_SECONDS=3600\nMAX_REQUEST_BYTES=1048576\n'
    } > "$ENV_FILE"
    chmod 0600 "$ENV_FILE"
    echo "Created configs/backup.env with random secrets."
else
    echo "Keeping existing configs/backup.env."
fi

if [ ! -f "$SSH_DIR/id_ed25519" ]; then
    ssh-keygen -q -t ed25519 -N '' -C 'backupstic' -f "$SSH_DIR/id_ed25519"
    echo "Created the application SSH keypair in data/config-backup/."
fi
touch "$SSH_DIR/known_hosts"
chmod 0600 "$SSH_DIR/id_ed25519" "$SSH_DIR/known_hosts"
chmod 0644 "$SSH_DIR/id_ed25519.pub"
chmod 0755 "$PROJECT_DIR/scripts" "$PROJECT_DIR/scripts"/*.sh "$PROJECT_DIR/scripts"/*.py

(cd "$PROJECT_DIR" && docker compose config >/dev/null)

echo "Initialization complete."
if [ -n "$generated_password" ]; then
    printf 'Initial admin password (store it now): %s\n' "$generated_password"
fi
echo "Next: docker compose up -d --build"
