#!/bin/bash
# MongoDB Backup Script

set -euo pipefail

# Configuration
MONGO_HOST="${MONGO_HOST:-localhost}"
MONGO_PORT="${MONGO_PORT:-27017}"
MONGO_USER="${MONGO_USER:-}"
MONGO_PASSWORD="${MONGO_PASSWORD:-}"
MONGO_DATABASE="${MONGO_DATABASE:-all}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/mongodb}"
DATE=$(date +%Y%m%d_%H%M%S)

# Logging
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1"
}

# Ensure backup directory exists
mkdir -p "$BACKUP_DIR"

log "Starting MongoDB backup..."

# Build authentication options
AUTH_OPTS=()
if [ -n "$MONGO_USER" ] && [ -n "$MONGO_PASSWORD" ]; then
    AUTH_OPTS=(--username "$MONGO_USER" --password "$MONGO_PASSWORD" --authenticationDatabase admin)
fi

BACKUP_FILE="$BACKUP_DIR/mongo_${DATE}.tar.gz"

if [ "$MONGO_DATABASE" = "all" ]; then
    # Backup all databases
    log "Backing up all databases..."
    mongodump --host "$MONGO_HOST" --port "$MONGO_PORT" "${AUTH_OPTS[@]}" --out "$BACKUP_DIR/dump_${DATE}"
else
    # Backup specific database
    log "Backing up database $MONGO_DATABASE..."
    mongodump --host "$MONGO_HOST" --port "$MONGO_PORT" "${AUTH_OPTS[@]}" --db "$MONGO_DATABASE" --out "$BACKUP_DIR/dump_${DATE}"
fi

# Compress
tar -czf "$BACKUP_FILE" -C "$BACKUP_DIR" "dump_${DATE}"
rm -rf "$BACKUP_DIR/dump_${DATE}"

log "MongoDB backup completed: $BACKUP_FILE"
echo "$BACKUP_FILE"
