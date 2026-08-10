#!/bin/bash
# Configuration Backup over SSH with rsync collection and local tar archive.
# Usage: ./ssh-files.sh <profile_json_file>

set -euo pipefail

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1"
}

fail() {
    log "Error: $1"
    exit 1
}

remote_quote() {
    printf "%q" "$1"
}

is_absolute_path() {
    local path="$1"
    [[ -n "$path" && "$path" == /* && "$path" != *$'\n'* && "$path" != *$'\r'* ]]
}

sanitize_name() {
    printf "%s" "$1" | tr -cs 'A-Za-z0-9_.-' '_' | sed 's/^_//; s/_$//'
}

run_ssh() {
    "${SSH_CMD[@]}" "$@"
}

classify_ssh_error() {
    local err
    err="$(printf "%s" "$1" | tr '[:upper:]' '[:lower:]')"
    if [[ "$err" == *"permission denied"* || "$err" == *"authentication failed"* ]]; then
        echo "SSH authentication failed"
    elif [[ "$err" == *"timed out"* || "$err" == *"connection timeout"* ]]; then
        echo "SSH connection timed out"
    else
        echo "${1:-SSH connection failed}"
    fi
}

if [ $# -ne 1 ]; then
    echo "Usage: $0 <profile_json_file>"
    exit 1
fi

PROFILE_FILE="$1"
[ -f "$PROFILE_FILE" ] || fail "Profile file not found: $PROFILE_FILE"
command -v jq >/dev/null 2>&1 || fail "jq is required but not installed"
command -v rsync >/dev/null 2>&1 || fail "rsync is required but not installed"
command -v tar >/dev/null 2>&1 || fail "tar is required but not installed"

PROFILE_ID=$(jq -r '.id // "manual"' "$PROFILE_FILE")
PROFILE_NAME=$(jq -r '.name // "configuration-backup"' "$PROFILE_FILE")
SSH_HOST=$(jq -r '.ssh_host // empty' "$PROFILE_FILE")
SSH_PORT=$(jq -r '.ssh_port // 22' "$PROFILE_FILE")
SSH_USER=$(jq -r '.ssh_user // empty' "$PROFILE_FILE")
AUTH_METHOD=$(jq -r '.auth_method // (if (.ssh_password // "") != "" then "password" else "key" end)' "$PROFILE_FILE")
SSH_PASSWORD=$(jq -r '.ssh_password // empty' "$PROFILE_FILE")
PRESERVE_METADATA=$(jq -r '.preserve_metadata // false' "$PROFILE_FILE")
LOG_RSYNC_OUTPUT=$(jq -r '.log_rsync_output // false' "$PROFILE_FILE")
DATE=$(date +%Y%m%d_%H%M%S)

[ -n "$SSH_HOST" ] || fail "SSH host is required"
[ -n "$SSH_USER" ] || fail "SSH user is required"
[[ "$SSH_PORT" =~ ^[0-9]+$ ]] || fail "SSH port must be numeric"
if [ "$SSH_PORT" -lt 1 ] || [ "$SSH_PORT" -gt 65535 ]; then
    fail "SSH port must be between 1 and 65535"
fi

BACKUP_DIR="${BACKUP_DIR:-/var/backups/ssh_files}"
mkdir -p "$BACKUP_DIR"

SAFE_PROFILE=$(sanitize_name "$PROFILE_NAME")
[ -n "$SAFE_PROFILE" ] || SAFE_PROFILE="configuration_backup"
WORK_DIR=$(mktemp -d "$BACKUP_DIR/job-${PROFILE_ID}-${DATE}.XXXXXX")
ARCHIVE_PATH="$BACKUP_DIR/${SAFE_PROFILE}_${DATE}.tar.gz"
MANIFEST_PATH="$BACKUP_DIR/${SAFE_PROFILE}_${DATE}.manifest.json"

cleanup() {
    local status=$?
    rm -rf "$WORK_DIR"
    if [ "$status" -ne 0 ]; then
        rm -f "$ARCHIVE_PATH" "$MANIFEST_PATH"
    fi
}
trap cleanup EXIT

SSH_COMMON=(
    -p "$SSH_PORT"
    -o "ConnectTimeout=${CONFIG_BACKUP_SSH_CONNECT_TIMEOUT:-10}"
    -o StrictHostKeyChecking=yes
    -o "UserKnownHostsFile=${CONFIG_BACKUP_SSH_KNOWN_HOSTS:-/run/secrets/config-backup/known_hosts}"
    -o LogLevel=ERROR
)

if [ "$AUTH_METHOD" = "key" ]; then
    SSH_PRIVATE_KEY="${CONFIG_BACKUP_SSH_PRIVATE_KEY:-/run/secrets/config-backup/id_ed25519}"
    [ -f "$SSH_PRIVATE_KEY" ] || fail "Configured private key not found: $SSH_PRIVATE_KEY"
    SSH_CMD=(ssh -i "$SSH_PRIVATE_KEY" -o BatchMode=yes "${SSH_COMMON[@]}" "$SSH_USER@$SSH_HOST")
    RSYNC_RUNNER=(rsync)
    RSYNC_SSH="ssh -i $(remote_quote "$SSH_PRIVATE_KEY") -o BatchMode=yes -p $SSH_PORT -o ConnectTimeout=${CONFIG_BACKUP_SSH_CONNECT_TIMEOUT:-10} -o StrictHostKeyChecking=yes -o UserKnownHostsFile=$(remote_quote "${CONFIG_BACKUP_SSH_KNOWN_HOSTS:-/run/secrets/config-backup/known_hosts}") -o LogLevel=ERROR"
elif [ "$AUTH_METHOD" = "password" ]; then
    [ -n "$SSH_PASSWORD" ] || fail "SSH password is required for password authentication"
    command -v sshpass >/dev/null 2>&1 || fail "sshpass is required for password authentication but not installed"
    export SSHPASS="$SSH_PASSWORD"
    SSH_CMD=(sshpass -e ssh -o PreferredAuthentications=password -o PubkeyAuthentication=no "${SSH_COMMON[@]}" "$SSH_USER@$SSH_HOST")
    RSYNC_RUNNER=(sshpass -e rsync)
    RSYNC_SSH="ssh -o PreferredAuthentications=password -o PubkeyAuthentication=no -p $SSH_PORT -o ConnectTimeout=${CONFIG_BACKUP_SSH_CONNECT_TIMEOUT:-10} -o StrictHostKeyChecking=yes -o UserKnownHostsFile=$(remote_quote "${CONFIG_BACKUP_SSH_KNOWN_HOSTS:-/run/secrets/config-backup/known_hosts}") -o LogLevel=ERROR"
else
    fail "Unsupported auth_method: $AUTH_METHOD"
fi

log "Starting Configuration Backup for profile: $PROFILE_NAME"
log "Host: $SSH_HOST:$SSH_PORT"
log "User: $SSH_USER"
log "Authentication: $AUTH_METHOD"
log "Preserve metadata: $PRESERVE_METADATA"

if ! SSH_TEST_OUTPUT=$(run_ssh true 2>&1); then
    fail "$(classify_ssh_error "$SSH_TEST_OUTPUT")"
fi

if ! REMOTE_RSYNC_OUTPUT=$(run_ssh "command -v rsync >/dev/null 2>&1" 2>&1); then
    fail "rsync is not installed on remote host $SSH_HOST. Install it on the target server first. Debian/Ubuntu: apt install rsync. RHEL/CentOS/Rocky: dnf install rsync or yum install rsync."
fi

mapfile -t PATH_ENTRIES < <(
    jq -c '
      (.exclude_patterns // []) as $global |
      if ((.path_configs // []) | length) > 0 then
        .path_configs[] | {path: .path, exclude_patterns: (.exclude_patterns // $global)}
      else
        .paths[]? | {path: ., exclude_patterns: $global}
      end
    ' "$PROFILE_FILE"
)

[ "${#PATH_ENTRIES[@]}" -gt 0 ] || fail "At least one remote path is required"

COPIED_PATHS=0
for entry in "${PATH_ENTRIES[@]}"; do
    REMOTE_PATH=$(jq -r '.path // empty' <<<"$entry")
    [ -n "$REMOTE_PATH" ] || continue
    is_absolute_path "$REMOTE_PATH" || fail "Only absolute remote paths are allowed: $REMOTE_PATH"

    log "Checking remote path: $REMOTE_PATH"
    QUOTED_PATH=$(remote_quote "$REMOTE_PATH")
    if ! REMOTE_KIND=$(run_ssh "if [ -d $QUOTED_PATH ]; then echo dir; elif [ -e $QUOTED_PATH ]; then echo file; else echo missing; fi" 2>&1); then
        fail "Failed to inspect remote path $REMOTE_PATH: $REMOTE_KIND"
    fi
    if [ "$REMOTE_KIND" = "missing" ]; then
        fail "Remote path does not exist: $REMOTE_PATH"
    fi

    RELATIVE_PATH="${REMOTE_PATH#/}"
    if [ "$REMOTE_KIND" = "dir" ]; then
        DEST_PATH="$WORK_DIR/$RELATIVE_PATH"
        mkdir -p "$DEST_PATH"
        REMOTE_SOURCE="$SSH_USER@$SSH_HOST:$REMOTE_PATH/"
    else
        DEST_PATH="$WORK_DIR/$(dirname "$RELATIVE_PATH")/"
        mkdir -p "$DEST_PATH"
        REMOTE_SOURCE="$SSH_USER@$SSH_HOST:$REMOTE_PATH"
    fi

    RSYNC_CMD=("${RSYNC_RUNNER[@]}" --protect-args --human-readable --stats)
    if [ "$PRESERVE_METADATA" = "true" ]; then
        RSYNC_CMD+=(-a)
    else
        RSYNC_CMD+=(-rL --no-perms --no-owner --no-group --omit-dir-times)
    fi

    mapfile -t EXCLUDES < <(jq -r '.exclude_patterns[]? // empty' <<<"$entry")
    for pattern in "${EXCLUDES[@]}"; do
        [ -n "$pattern" ] || continue
        if [[ "$pattern" == *$'\n'* || "$pattern" == *$'\r'* ]]; then
            fail "Invalid exclude pattern for $REMOTE_PATH"
        fi
        RSYNC_CMD+=(--exclude "$pattern")
    done

    RSYNC_CMD+=(-e "$RSYNC_SSH" "$REMOTE_SOURCE" "$DEST_PATH")

    log "Running rsync for: $REMOTE_PATH"
    ATTEMPT=1
    until "${RSYNC_CMD[@]}" >"$WORK_DIR/rsync_${COPIED_PATHS}.log" 2>&1; do
        STATUS=$?
        if [ "$LOG_RSYNC_OUTPUT" = "true" ]; then
            sed 's/^/[rsync] /' "$WORK_DIR/rsync_${COPIED_PATHS}.log"
        fi
        if [ "$ATTEMPT" -ge 3 ]; then
            fail "rsync failed for $REMOTE_PATH after 3 attempts with exit code $STATUS"
        fi
        log "rsync failed for $REMOTE_PATH with exit code $STATUS; retrying ($ATTEMPT/2)"
        ATTEMPT=$((ATTEMPT + 1))
        sleep 5
        if ! SSH_RETRY_OUTPUT=$(run_ssh true 2>&1); then
            fail "$(classify_ssh_error "$SSH_RETRY_OUTPUT")"
        fi
    done

    if [ "$LOG_RSYNC_OUTPUT" = "true" ]; then
        sed 's/^/[rsync] /' "$WORK_DIR/rsync_${COPIED_PATHS}.log"
    fi
    COPIED_PATHS=$((COPIED_PATHS + 1))
done

[ "$COPIED_PATHS" -gt 0 ] || fail "No remote paths were copied"

log "Creating archive: $ARCHIVE_PATH"
if ! tar -czf "$ARCHIVE_PATH" -C "$WORK_DIR" .; then
    fail "Failed to create archive"
fi

jq -n \
    --arg profile "$PROFILE_NAME" \
    --arg timestamp "$(date -Iseconds)" \
    --arg host "$SSH_HOST" \
    --arg archive "$(basename "$ARCHIVE_PATH")" \
    --argjson copied "$COPIED_PATHS" \
    '{profile:$profile,timestamp:$timestamp,host:$host,archive:$archive,copied_paths:$copied}' \
    > "$MANIFEST_PATH"

log "Configuration Backup completed: $ARCHIVE_PATH"
echo "$ARCHIVE_PATH"
