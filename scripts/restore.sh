#!/bin/bash
# Restore Script - Restores from restic repository or local backups
# Usage: ./restore.sh [postgres|redis|mongo|elasticsearch|files|all]

set -euo pipefail

# Configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP_BASE="${BACKUP_BASE:-/var/backups}"
RESTIC_REPO="${RESTIC_REPO:-/var/backups/restic-repo}"
RESTIC_PASSWORD="${RESTIC_PASSWORD:-}"
RESTORE_TARGET="${RESTORE_TARGET:-/tmp/restore}"
RESTORE_MODE="${1:-all}"

# Logging
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1"
}

# Ensure restore directory exists
mkdir -p "$RESTORE_TARGET"

log "=========================================="
log "Starting restore operation: $RESTORE_MODE"
log "=========================================="

# Restore from restic
restore_from_restic() {
    local tag="$1"
    local dest="$2"

    log "Restoring $tag from restic repository..."

    export RESTIC_REPOSITORY="$RESTIC_REPO"
    export RESTIC_PASSWORD

    # Find latest snapshot for this tag
    SNAPSHOT_ID=$(restic snapshots --tag "$tag" --latest 1 --json | jq -r '.[0].id' 2>/dev/null)

    if [ -n "$SNAPSHOT_ID" ] && [ "$SNAPSHOT_ID" != "null" ]; then
        log "Restoring snapshot $SNAPSHOT_ID"
        restic restore "$SNAPSHOT_ID" --target "$dest"
        log "Restored to $dest"
    else
        log "No snapshot found for tag: $tag"
        return 1
    fi
}

# Restore PostgreSQL
restore_postgres() {
    log "Restoring PostgreSQL..."
    restore_from_restic "postgresql" "$RESTORE_TARGET"

    # Find the backup file
    BACKUP_FILE=$(find "$RESTORE_TARGET/var/backups/postgresql" -name "pg_dumpall_*.sql.gz" -o -name "*.sql.gz" | head -1)

    if [ -n "$BACKUP_FILE" ]; then
        log "Found backup file: $BACKUP_FILE"
        log "To restore, run:"
        log "  gunzip -c $BACKUP_FILE | psql -h localhost -U postgres"
    else
        log "No PostgreSQL backup file found"
    fi
}

# Restore Redis
restore_redis() {
    log "Restoring Redis..."
    restore_from_restic "redis" "$RESTORE_TARGET"

    # Find the backup file
    BACKUP_FILE=$(find "$RESTORE_TARGET/var/backups/redis" -name "redis_*.tar.gz" | head -1)

    if [ -n "$BACKUP_FILE" ]; then
        log "Found backup file: $BACKUP_FILE"
        log "To restore, run:"
        log "  tar -xzf $BACKUP_FILE -C /var/lib/redis"
        log "  systemctl restart redis"
    else
        log "No Redis backup file found"
    fi
}

# Restore MongoDB
restore_mongo() {
    log "Restoring MongoDB..."
    restore_from_restic "mongodb" "$RESTORE_TARGET"

    # Find the backup file
    BACKUP_FILE=$(find "$RESTORE_TARGET/var/backups/mongodb" -name "mongo_*.tar.gz" | head -1)

    if [ -n "$BACKUP_FILE" ]; then
        log "Found backup file: $BACKUP_FILE"
        log "To restore, run:"
        log "  tar -xzf $BACKUP_FILE -C /tmp/mongo_restore"
        log "  mongorestore --host localhost --port 27017 /tmp/mongo_restore/dump_*"
    else
        log "No MongoDB backup file found"
    fi
}

# Restore Elasticsearch
restore_elasticsearch() {
    log "Restoring Elasticsearch..."
    restore_from_restic "elasticsearch" "$RESTORE_TARGET"

    # Find the snapshot metadata
    BACKUP_FILE=$(find "$RESTORE_TARGET/var/backups/elasticsearch" -name "backup_*.json" | head -1)

    if [ -n "$BACKUP_FILE" ]; then
        SNAPSHOT_NAME=$(jq -r '.snapshot' "$BACKUP_FILE")
        log "Found snapshot: $SNAPSHOT_NAME"
        log "To restore, run:"
        log "  curl -X POST 'http://localhost:9200/_snapshot/backup/${SNAPSHOT_NAME}/_restore'"
    else
        log "No Elasticsearch backup found"
    fi
}

# Restore Files
restore_files() {
    log "Restoring files..."
    restore_from_restic "files" "$RESTORE_TARGET"

    log "Files restored to: $RESTORE_TARGET"
    log "Check the contents and copy to desired location"
}

# Main execution
case "$RESTORE_MODE" in
    all)
        restore_postgres
        restore_redis
        restore_mongo
        restore_elasticsearch
        restore_files
        ;;
    postgres)
        restore_postgres
        ;;
    redis)
        restore_redis
        ;;
    mongo)
        restore_mongo
        ;;
    elasticsearch)
        restore_elasticsearch
        ;;
    files)
        restore_files
        ;;
    *)
        log "Unknown restore mode: $RESTORE_MODE"
        log "Usage: $0 [postgres|redis|mongo|elasticsearch|files|all]"
        exit 1
        ;;
esac

log "=========================================="
log "Restore operation completed!"
log "=========================================="
log "Restored files are in: $RESTORE_TARGET"
