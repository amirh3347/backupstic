#!/bin/bash
# Backup Monitor Script - Checks backup status and generates reports

set -euo pipefail

# Load configuration from env file safely (for standalone use outside Docker)
# Handles lines with spaces like BACKUP_CRON that break 'source'
if [ -f /etc/backup.env ]; then
    while IFS= read -r line; do
        [[ "$line" =~ ^[[:space:]]*# ]] && continue
        [[ -z "$line" ]] && continue
        if [[ "$line" =~ ^[a-zA-Z_][a-zA-Z0-9_]*= ]]; then
            key="${line%%=*}"
            val="${line#*=}"
            export "$key=$val"
        fi
    done < /etc/backup.env
fi

# Configuration
BACKUP_BASE="${BACKUP_BASE:-/var/backups}"
RESTIC_REPO="${RESTIC_REPO:-/var/backups/restic-repo}"
RESTIC_PASSWORD="${RESTIC_PASSWORD:-}"
REPORT_DIR="$BACKUP_BASE/reports"
LOG_FILE="$BACKUP_BASE/monitor.log"

# Logging
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

# Ensure directories exist
mkdir -p "$REPORT_DIR"

log "Starting backup monitoring check..."

# Check restic repository
check_restic_repo() {
    log "Checking restic repository..."

    export RESTIC_REPOSITORY="$RESTIC_REPO"
    export RESTIC_PASSWORD

    if [ ! -f "$RESTIC_REPO/config" ]; then
        log "Restic repository not initialized — initializing now..."
        if ! restic init; then
            log "ERROR: Failed to initialize restic repository at $RESTIC_REPO"
            return 1
        fi
        log "Restic repository initialized successfully"
    fi

    if ! restic snapshots &>/dev/null; then
        log "ERROR: Cannot access restic repository at $RESTIC_REPO (wrong password or corrupt)"
        restic snapshots 2>&1 | head -5 | while IFS= read -r errline; do log "  restic: $errline"; done
        return 1
    fi

    SNAPSHOT_COUNT=$(restic snapshots --json | jq 'length')
    LAST_SNAPSHOT=$(restic snapshots --latest 1 --json | jq -r '.[0].time')
    log "Repository OK: $SNAPSHOT_COUNT snapshots, last: $LAST_SNAPSHOT"
    return 0
}

# Check disk space
check_disk_space() {
    log "Checking disk space..."

    DISK_USAGE=$(df -h "$BACKUP_BASE" | awk 'NR==2 {print $5}' | tr -d '%')
    DISK_AVAIL=$(df -h "$BACKUP_BASE" | awk 'NR==2 {print $4}')

    log "Disk usage: ${DISK_USAGE}%, Available: ${DISK_AVAIL}"

    if [ "$DISK_USAGE" -gt 90 ]; then
        log "WARNING: Disk usage is above 90%!"
        return 1
    fi
    return 0
}

# Check backup freshness
check_backup_freshness() {
    log "Checking backup freshness..."

    for db in postgresql redis mongodb elasticsearch; do
        BACKUP_DIR="$BACKUP_BASE/$db"
        if [ ! -d "$BACKUP_DIR" ]; then
            log "WARNING: No $db backups found"
            continue
        fi

        LATEST=$(find "$BACKUP_DIR" -type f \( -name "*.gz" -o -name "*.json" \) -print -quit 2>/dev/null)
        if [ -n "$LATEST" ]; then
            AGE=$(( ($(date +%s) - $(stat -c %Y "$LATEST")) / 86400 ))
            if [ "$AGE" -gt 2 ]; then
                log "WARNING: $db backup is $AGE days old"
            else
                log "$db backup: $AGE days old (OK)"
            fi
        else
            log "WARNING: No $db backups found"
        fi
    done
}

# Generate daily report
generate_report() {
    log "Generating daily report..."

    REPORT_FILE="$REPORT_DIR/report_$(date +%Y%m%d).txt"

    cat > "$REPORT_FILE" << EOF
========================================
Backup Monitoring Report
Date: $(date)
========================================

1. Repository Status:
$(restic snapshots 2>/dev/null | tail -n +2 || echo "N/A")

2. Disk Usage:
$(df -h "$BACKUP_BASE")

3. Recent Backups:
$(find "$BACKUP_BASE" -name "*.gz" -mtime -1 -exec ls -lh {} \; 2>/dev/null || echo "No recent backups")

4. Backup Sizes by Type:
PostgreSQL: $(du -sh "$BACKUP_BASE/postgresql" 2>/dev/null | cut -f1 || echo "N/A")
Redis: $(du -sh "$BACKUP_BASE/redis" 2>/dev/null | cut -f1 || echo "N/A")
MongoDB: $(du -sh "$BACKUP_BASE/mongodb" 2>/dev/null | cut -f1 || echo "N/A")
Elasticsearch: $(du -sh "$BACKUP_BASE/elasticsearch" 2>/dev/null | cut -f1 || echo "N/A")
Files: $(du -sh "$BACKUP_BASE/files" 2>/dev/null | cut -f1 || echo "N/A")

5. Restic Statistics:
$(restic stats 2>/dev/null || echo "N/A")

========================================
EOF

    log "Report saved to: $REPORT_FILE"
}

# Main execution
check_restic_repo
check_disk_space
check_backup_freshness
generate_report

log "Monitoring check completed"
