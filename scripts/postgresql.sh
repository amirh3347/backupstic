#!/bin/bash
# PostgreSQL Backup Script
# Uses pg_dump to backup databases

set -euo pipefail

# Configuration
PG_HOST="${PG_HOST:-localhost}"
PG_PORT="${PG_PORT:-5432}"
PG_USER="${PG_USER:-postgres}"
PG_PASSWORD="${PG_PASSWORD:-}"
PG_DATABASE="${PG_DATABASE:-all}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/postgresql}"
DATE=$(date +%Y%m%d_%H%M%S)

# Logging
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1"
}

# Ensure backup directory exists
mkdir -p "$BACKUP_DIR"

log "Starting PostgreSQL backup..."

export PGPASSWORD="$PG_PASSWORD"

if [ "$PG_DATABASE" = "all" ]; then
    # Backup all databases
    BACKUP_FILE="$BACKUP_DIR/pg_dumpall_${DATE}.sql.gz"
    log "Backing up all databases to $BACKUP_FILE"
    pg_dumpall -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" | gzip > "$BACKUP_FILE"
else
    # Backup specific database
    BACKUP_FILE="$BACKUP_DIR/${PG_DATABASE}_${DATE}.sql.gz"
    log "Backing up database $PG_DATABASE to $BACKUP_FILE"
    pg_dump -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" "$PG_DATABASE" | gzip > "$BACKUP_FILE"
fi

log "PostgreSQL backup completed: $BACKUP_FILE"
echo "$BACKUP_FILE"
