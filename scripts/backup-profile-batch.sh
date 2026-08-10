#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "$#" -lt 1 ]; then
    echo "Usage: $0 <profile_id> [profile_id ...]" >&2
    exit 64
fi

status=0
for profile_id in "$@"; do
    if ! "$SCRIPT_DIR/backup-profile.sh" "$profile_id"; then
        status=1
    fi
done

exit "$status"
