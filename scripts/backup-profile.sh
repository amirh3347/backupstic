#!/bin/bash
# Profile backup script - called by cron for scheduled profile backups
# Usage: ./backup-profile.sh <profile_id>

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP_BASE="${BACKUP_BASE:-/var/backups}"

if [ $# -ne 1 ]; then
    echo "Usage: $0 <profile_id>"
    exit 1
fi

PROFILE_ID="$1"

# Call the main backup script with profile mode
"$SCRIPT_DIR/backup-all.sh" profile "$PROFILE_ID"