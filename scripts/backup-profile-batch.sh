#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "$#" -lt 1 ]; then
    echo "Usage: $0 <profile_id> [profile_id ...]" >&2
    exit 64
fi

status=0
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting scheduled batch with $# profile(s)"
for profile_id in "$@"; do
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Running scheduled profile: $profile_id"
    if ! "$SCRIPT_DIR/backup-profile.sh" "$profile_id"; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] Scheduled profile failed: $profile_id" >&2
        status=1
    fi
done

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Scheduled batch finished with status: $status"

exit "$status"
