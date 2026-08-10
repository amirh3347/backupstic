#!/bin/bash
# Redis/KeyDB Backup Script

set -euo pipefail

# Configuration
REDIS_HOST="${REDIS_HOST:-localhost}"
REDIS_PORT="${REDIS_PORT:-6379}"
REDIS_PASSWORD="${REDIS_PASSWORD:-}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/redis}"
DATE=$(date +%Y%m%d_%H%M%S)

# Logging
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1"
}

# Ensure backup directory exists
mkdir -p "$BACKUP_DIR"

log "Starting Redis backup..."

# Download RDB directly via SYNC (works for local and remote, no SSH needed)
BACKUP_FILE="$BACKUP_DIR/redis_${DATE}.rdb"

if [ -n "$REDIS_PASSWORD" ]; then
    REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli -h "$REDIS_HOST" -p "$REDIS_PORT" --rdb "$BACKUP_FILE"
else
    redis-cli -h "$REDIS_HOST" -p "$REDIS_PORT" --rdb "$BACKUP_FILE"
fi

# Compress
gzip -f "$BACKUP_FILE"
BACKUP_FILE="${BACKUP_FILE}.gz"

log "Redis backup completed: $BACKUP_FILE"
echo "$BACKUP_FILE"
