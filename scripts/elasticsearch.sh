#!/bin/bash
# Elasticsearch Backup Script

set -euo pipefail

# Configuration
ES_HOST="${ES_HOST:-localhost}"
ES_PORT="${ES_PORT:-9200}"
ES_USER="${ES_USER:-}"
ES_PASSWORD="${ES_PASSWORD:-}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/elasticsearch}"
SNAPSHOT_REPO="${SNAPSHOT_REPO:-backup}"
DATE=$(date +%Y%m%d_%H%M%S)
SNAPSHOT_NAME="snapshot_${DATE}"

# Logging
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1"
}

# Ensure backup directory exists
mkdir -p "$BACKUP_DIR"

# Build authentication options
AUTH_OPTS=()
if [ -n "$ES_USER" ] && [ -n "$ES_PASSWORD" ]; then
    AUTH_OPTS=(--user "${ES_USER}:${ES_PASSWORD}")
fi

log "Starting Elasticsearch backup..."

# Create snapshot repository if not exists
log "Creating snapshot repository..."
curl --silent "${AUTH_OPTS[@]}" -X PUT "http://${ES_HOST}:${ES_PORT}/_snapshot/${SNAPSHOT_REPO}" \
    -H 'Content-Type: application/json' \
    -d "{
        \"type\": \"fs\",
        \"settings\": {
            \"location\": \"${BACKUP_DIR}/es_snapshots\",
            \"compress\": true
        }
    }" > /dev/null 2>&1 || true

# Create snapshot
log "Creating snapshot: $SNAPSHOT_NAME"
HTTP_CODE=$(curl --silent "${AUTH_OPTS[@]}" -o /dev/null -w "%{http_code}" \
    -X PUT "http://${ES_HOST}:${ES_PORT}/_snapshot/${SNAPSHOT_REPO}/${SNAPSHOT_NAME}?wait_for_completion=true" \
    -H 'Content-Type: application/json' \
    -d '{
        "indices": "*",
        "ignore_unavailable": true,
        "include_global_state": false
    }')

if [ "$HTTP_CODE" = "200" ]; then
    log "Elasticsearch backup completed: $SNAPSHOT_NAME"
    # Create a metadata file for restic
    echo "{\"snapshot\": \"${SNAPSHOT_NAME}\", \"date\": \"${DATE}\"}" > "$BACKUP_DIR/backup_${DATE}.json"
    echo "$BACKUP_DIR"
else
    log "ERROR: Elasticsearch backup failed with HTTP code $HTTP_CODE"
    exit 1
fi
