#!/bin/bash
# Files Backup Script (GlusterFS, configs, media)

set -euo pipefail

# Configuration
GLUSTER_MOUNT="${GLUSTER_MOUNT:-/mnt/gluster}"
CONFIG_DIRS="${CONFIG_DIRS:-/etc/haproxy /etc/keepalived /root/cluster}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/files}"
DATE=$(date +%Y%m%d_%H%M%S)

# Logging
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1"
}

# Ensure backup directory exists
mkdir -p "$BACKUP_DIR"

log "Starting files backup..."

BACKUP_FILE="$BACKUP_DIR/files_${DATE}.tar.gz"

# Create tarball of specified directories
TAR_DIRS=()
for dir in $CONFIG_DIRS; do
    if [ -d "$dir" ]; then
        TAR_DIRS+=("$dir")
    fi
done

# Add gluster mount if exists
if [ -d "$GLUSTER_MOUNT" ]; then
    TAR_DIRS+=("$GLUSTER_MOUNT")
fi

if [ ${#TAR_DIRS[@]} -gt 0 ]; then
    log "Backing up directories: ${TAR_DIRS[*]}"
    tar -czf "$BACKUP_FILE" -- "${TAR_DIRS[@]}" 2>/dev/null || log "Warning: Some files may not be accessible"
else
    log "No directories found to backup"
    exit 1
fi

log "Files backup completed: $BACKUP_FILE"
echo "$BACKUP_FILE"
