#!/bin/bash
# Enhanced Backup Script - Handles both legacy modes and profile-based backups
# Usage: ./backup-all.sh [legacy_mode|profile <profile_id>]

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
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP_BASE="${BACKUP_BASE:-/var/backups}"
RESTIC_REPO="${RESTIC_REPO:-/var/backups/restic-repo}"
RESTIC_PASSWORD="${RESTIC_PASSWORD:-}"
RETENTION_DAYS="${RETENTION_DAYS:-30}"
RETENTION_WEEKLY="${RETENTION_WEEKLY:-12}"
RETENTION_MONTHLY="${RETENTION_MONTHLY:-12}"
BACKUP_LOCK_WAIT_SECONDS="${BACKUP_LOCK_WAIT_SECONDS:-21600}"

# Logging
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1"
}

load_restic_repository() {
    local repo_id="${1:-default}"
    local repo_json
    if [[ -x "$SCRIPT_DIR/export-restic-repository.py" ]]; then
        repo_json=$("$SCRIPT_DIR/export-restic-repository.py" "$repo_id")
        RESTIC_REPO=$(echo "$repo_json" | jq -r '.repository')
        RESTIC_PASSWORD=$(echo "$repo_json" | jq -r '.password')
        export RESTIC_REPOSITORY="$RESTIC_REPO"
        export RESTIC_PASSWORD
        export AWS_ACCESS_KEY_ID="$(echo "$repo_json" | jq -r '.aws_access_key_id // empty')"
        export AWS_SECRET_ACCESS_KEY="$(echo "$repo_json" | jq -r '.aws_secret_access_key // empty')"
        export AWS_DEFAULT_REGION="$(echo "$repo_json" | jq -r '.aws_default_region // empty')"
    else
        export RESTIC_REPOSITORY="$RESTIC_REPO"
        export RESTIC_PASSWORD
    fi
}

ensure_restic_repository() {
    log "Checking restic repository: $RESTIC_REPOSITORY"
    if restic snapshots &>/dev/null; then
        return 0
    fi
    if restic snapshots 2>&1 | grep -qiE 'repository is already locked|unable to create lock'; then
        log "Restic repository has a stale lock; running restic unlock..."
        restic unlock || true
        if restic snapshots &>/dev/null; then
            return 0
        fi
    fi
    log "Restic repository is not initialized or not accessible; trying init..."
    restic init
}

# Ensure backup base dir exists
mkdir -p "$BACKUP_BASE"

# Global lock — prevent concurrent backups
LOCK_FILE="$BACKUP_BASE/backup.lock"
STATE_FILE="$BACKUP_BASE/backup.state"
if [[ ! "$BACKUP_LOCK_WAIT_SECONDS" =~ ^[0-9]+$ ]]; then
    log "Error: BACKUP_LOCK_WAIT_SECONDS must be a non-negative integer"
    exit 64
fi
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    log "Another backup is already running; waiting up to ${BACKUP_LOCK_WAIT_SECONDS}s for the lock..."
    if ! flock -w "$BACKUP_LOCK_WAIT_SECONDS" 9; then
        log "Error: Timed out waiting for the backup lock."
        exit 75
    fi
    log "Backup lock acquired; continuing queued job."
fi

# State file consumed by dashboard "backup in progress" indicator
write_backup_state() {
    printf '{"mode":"%s","started_at":"%s","pid":%s}\n' \
        "$BACKUP_MODE" "$(date -Iseconds)" "$$" > "$STATE_FILE"
}

# Create temp dir; cleanup also clears the state file so dashboard updates promptly.
# STAGE_DIR holds a single profile run's fresh output and is removed on exit too,
# so raw dump files never accumulate in the backup volume.
TMP_BACKUP=$(mktemp -d)
STAGE_DIR=""
trap 'rm -rf "$TMP_BACKUP" ${STAGE_DIR:+"$STAGE_DIR"}; rm -f "$STATE_FILE"' EXIT

# Main execution
MODE="${1:-}"

if [[ "$MODE" == "profile" ]]; then
    # Profile-based backup mode
    PROFILE_ID="${2:-}"
    if [[ -z "$PROFILE_ID" ]]; then
        log "Error: Profile ID required for profile mode"
        exit 1
    fi
    
    # Fetch profile data from storage (JSON file)
    PROFILES_FILE="/var/backups/profiles.json"
    if [[ ! -f "$PROFILES_FILE" ]]; then
        log "Error: Profiles file not found: $PROFILES_FILE"
        exit 1
    fi
    
    # Extract profile using jq (if available) or grep/sed fallback
    if command -v jq >/dev/null 2>&1; then
        if [[ -x "$SCRIPT_DIR/export-profile.py" ]]; then
            PROFILE_JSON=$("$SCRIPT_DIR/export-profile.py" "$PROFILE_ID")
        else
            PROFILE_JSON=$(jq -r ".profiles[\"$PROFILE_ID\"]" "$PROFILES_FILE" 2>/dev/null || echo "null")
        fi
        if [[ "$PROFILE_JSON" == "null" || -z "$PROFILE_JSON" ]]; then
            log "Error: Profile not found: $PROFILE_ID"
            exit 1
        fi
        
        # Extract profile fields
        PROFILE_TYPE=$(echo "$PROFILE_JSON" | jq -r '.type // empty')
        PROFILE_NAME=$(echo "$PROFILE_JSON" | jq -r '.name // empty')
        PROFILE_ENABLED=$(echo "$PROFILE_JSON" | jq -r '.enabled // true')
        PROFILE_REPOSITORY_ID=$(echo "$PROFILE_JSON" | jq -r '.repository_id // "default"')
        PROFILE_MAX_BACKUPS=$(echo "$PROFILE_JSON" | jq -r '.max_backups // 0')
    else
        # Fallback without jq (basic parsing)
        log "Warning: jq not installed, using basic profile parsing"
        # This is a simplified fallback - in production, ensure jq is available
        PROFILE_TYPE="unknown"
        PROFILE_NAME="unknown"
        PROFILE_ENABLED="true"
    fi
    
    # Check if profile is enabled
    if [[ "$PROFILE_ENABLED" != "true" ]]; then
        log "Profile is disabled: $PROFILE_NAME"
        exit 0
    fi
    
    log "Starting profile backup: $PROFILE_NAME (type: $PROFILE_TYPE)"
    load_restic_repository "$PROFILE_REPOSITORY_ID"
    ensure_restic_repository
    BACKUP_MODE="profile:$PROFILE_NAME"
    write_backup_state

    # Per-profile staging dir: holds ONLY this run's fresh output so restic records
    # an accurate size and no raw files are left behind in the volume. Path is stable
    # per profile (keyed by id) so restic can still detect incremental parents.
    STAGE_DIR="$BACKUP_BASE/.staging/$PROFILE_ID"
    rm -rf "$STAGE_DIR"
    mkdir -p "$STAGE_DIR"

    case "$PROFILE_TYPE" in
        postgresql)
            # Handle PostgreSQL profile
            PG_HOST=$(echo "$PROFILE_JSON" | jq -r '.host // empty')
            PG_PORT=$(echo "$PROFILE_JSON" | jq -r '.port // empty')
            PG_USER=$(echo "$PROFILE_JSON" | jq -r '.username // empty')
            PG_PASSWORD=$(echo "$PROFILE_JSON" | jq -r '.password // empty')
            PG_DATABASES=$(echo "$PROFILE_JSON" | jq -r '.databases[] // empty' | tr '\n' ' ')

            export PGPASSWORD="$PG_PASSWORD"
            BACKUP_DIR="$STAGE_DIR"

            for DB in $PG_DATABASES; do
                if [[ -z "$DB" ]]; then continue; fi
                log "Backing up PostgreSQL database: $DB"
                BACKUP_FILE="$BACKUP_DIR/${DB}_$(date +%Y%m%d_%H%M%S).sql.gz"
                if [[ "$DB" == "all" ]]; then
                    pg_dumpall -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" | gzip > "$BACKUP_FILE"
                else
                    pg_dump -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" "$DB" | gzip > "$BACKUP_FILE"
                fi
                log "PostgreSQL backup completed: $BACKUP_FILE"
            done
            ;;

        redis)
            # Handle Redis profile
            REDIS_HOST=$(echo "$PROFILE_JSON" | jq -r '.host // empty')
            REDIS_PORT=$(echo "$PROFILE_JSON" | jq -r '.port // empty')
            REDIS_PASSWORD=$(echo "$PROFILE_JSON" | jq -r '.password // empty')

            BACKUP_DIR="$STAGE_DIR"

            BACKUP_FILE="$BACKUP_DIR/redis_$(date +%Y%m%d_%H%M%S).rdb"
            if [[ -n "$REDIS_PASSWORD" ]]; then
                REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli -h "$REDIS_HOST" -p "$REDIS_PORT" --rdb "$BACKUP_FILE"
            else
                redis-cli -h "$REDIS_HOST" -p "$REDIS_PORT" --rdb "$BACKUP_FILE"
            fi
            gzip -f "$BACKUP_FILE"
            BACKUP_FILE="${BACKUP_FILE}.gz"
            log "Redis backup completed: $BACKUP_FILE"
            ;;

        mongodb)
            # Handle MongoDB profile - delegate to mongodb.sh per database
            MONGO_HOST=$(echo "$PROFILE_JSON" | jq -r '.host // empty')
            MONGO_PORT=$(echo "$PROFILE_JSON" | jq -r '.port // 27017')
            MONGO_USER=$(echo "$PROFILE_JSON" | jq -r '.username // empty')
            MONGO_PASSWORD=$(echo "$PROFILE_JSON" | jq -r '.password // empty')
            MONGO_DATABASES=$(echo "$PROFILE_JSON" | jq -r '.databases[] // empty' | tr '\n' ' ')

            BACKUP_DIR="$STAGE_DIR"

            # Empty list means "all"
            [[ -z "${MONGO_DATABASES// }" ]] && MONGO_DATABASES="all"

            for DB in $MONGO_DATABASES; do
                [[ -z "$DB" ]] && continue
                log "Backing up MongoDB database: $DB"
                MONGO_HOST="$MONGO_HOST" MONGO_PORT="$MONGO_PORT" \
                MONGO_USER="$MONGO_USER" MONGO_PASSWORD="$MONGO_PASSWORD" \
                MONGO_DATABASE="$DB" BACKUP_DIR="$BACKUP_DIR" \
                    "$SCRIPT_DIR/mongodb.sh"
            done
            log "MongoDB backup completed"
            ;;

        ssh_files)
            # Handle Configuration Backup profiles. ssh-files.sh collects remote
            # paths with rsync into a per-job temp dir, archives them, and leaves
            # only the tar/manifest in this staging root for restic.
            PROFILE_TMP=$(mktemp "$TMP_BACKUP/profile.XXXXXX")
            echo "$PROFILE_JSON" > "$PROFILE_TMP"
            BACKUP_DIR="$STAGE_DIR" "$SCRIPT_DIR/ssh-files.sh" "$PROFILE_TMP"
            ;;

        *)
            log "Error: Unsupported profile type: $PROFILE_TYPE"
            exit 1
            ;;
    esac

    # Push this profile's fresh backup into restic so it appears in the dashboard
    # (which lists restic snapshots) and is subject to retention/prune.
    log "Storing profile backup in restic repository..."

    if [[ -d "$STAGE_DIR" && "$(ls -A "$STAGE_DIR" 2>/dev/null)" ]]; then
        # Tag with a stable profile id as well as display name/type. The id tag
        # keeps retention correct even if the user later renames the profile.
        PROFILE_ID_TAG="profile:$PROFILE_ID"
        restic backup "$STAGE_DIR" --tag "$PROFILE_ID_TAG" --tag "$PROFILE_NAME" --tag "$PROFILE_TYPE"
        log "Cleaning old snapshots..."
        if [[ "$PROFILE_MAX_BACKUPS" =~ ^[0-9]+$ && "$PROFILE_MAX_BACKUPS" -gt 0 ]]; then
            restic forget --tag "$PROFILE_ID_TAG" --keep-last "$PROFILE_MAX_BACKUPS" --group-by tags --prune
            # Best-effort cleanup for older snapshots created before the stable
            # profile id tag existed.
            restic forget --tag "$PROFILE_NAME,$PROFILE_TYPE" --keep-last "$PROFILE_MAX_BACKUPS" --group-by tags --prune || true
            if command -v jq >/dev/null 2>&1; then
                LEGACY_PROFILE_SNAPSHOTS=$(
                    restic snapshots --json |
                    jq -r --arg stage "$STAGE_DIR" --arg stable_tag "$PROFILE_ID_TAG" '
                        .[]
                        | select(((.tags // []) | index($stable_tag) | not)
                            and ((.paths // []) | index($stage)))
                        | .id
                    '
                )
                if [[ -n "${LEGACY_PROFILE_SNAPSHOTS// }" ]]; then
                    log "Cleaning legacy snapshots for profile id: $PROFILE_ID"
                    # shellcheck disable=SC2086
                    restic forget $LEGACY_PROFILE_SNAPSHOTS --prune || true
                fi
            fi
        else
            restic forget --keep-daily "$RETENTION_DAYS" --keep-weekly "$RETENTION_WEEKLY" --keep-monthly "$RETENTION_MONTHLY" --prune
        fi
    else
        log "Warning: no backup files produced for profile $PROFILE_NAME; skipping restic push"
    fi

    # Remove the staging output; restic already holds the snapshot. Prevents the
    # raw dumps from piling up in the volume (the delete-doesn't-free-disk issue).
    rm -rf "$STAGE_DIR"
    STAGE_DIR=""

elif [[ -n "$MODE" ]]; then
    # Legacy mode (full, db, files, postgres, redis, mongo, elasticsearch)
    log "Starting legacy backup job: $MODE"
    BACKUP_MODE="$MODE"
    write_backup_state
    
    # Define backup functions for each type
    run_postgres_backup() {
        log "Running PostgreSQL backup..."
        BACKUP_DIR="$BACKUP_BASE/postgresql" \
            bash "$SCRIPT_DIR/postgresql.sh" 2>&1 | tee "$TMP_BACKUP/postgres.log"
        log "PostgreSQL backup completed"
    }
    
    run_redis_backup() {
        log "Running Redis backup..."
        BACKUP_DIR="$BACKUP_BASE/redis" \
            bash "$SCRIPT_DIR/redis.sh" 2>&1 | tee "$TMP_BACKUP/redis.log"
        log "Redis backup completed"
    }
    
    run_mongo_backup() {
        log "Running MongoDB backup..."
        BACKUP_DIR="$BACKUP_BASE/mongodb" \
            bash "$SCRIPT_DIR/mongodb.sh" 2>&1 | tee "$TMP_BACKUP/mongo.log"
        log "MongoDB backup completed"
    }
    
    run_elasticsearch_backup() {
        log "Running Elasticsearch backup..."
        BACKUP_DIR="$BACKUP_BASE/elasticsearch" \
            bash "$SCRIPT_DIR/elasticsearch.sh" 2>&1 | tee "$TMP_BACKUP/elasticsearch.log"
        log "Elasticsearch backup completed"
    }
    
    run_files_backup() {
        log "Running Files backup..."
        BACKUP_DIR="$BACKUP_BASE/files" \
            bash "$SCRIPT_DIR/files.sh" 2>&1 | tee "$TMP_BACKUP/files.log"
        log "Files backup completed"
    }
    
    # Map a backup mode to its on-disk directory + restic tag.
    # Args: <mode>; echoes "<dir> <tag>" or nothing for aggregate modes.
    mode_to_dir() {
        case "$1" in
            postgres)     echo "$BACKUP_BASE/postgresql postgresql" ;;
            redis)        echo "$BACKUP_BASE/redis redis" ;;
            mongo)        echo "$BACKUP_BASE/mongodb mongodb" ;;
            elasticsearch) echo "$BACKUP_BASE/elasticsearch elasticsearch" ;;
            files)        echo "$BACKUP_BASE/files files" ;;
        esac
    }
    
    # Store backup in restic.
    # For a single-service mode, only that service's directory is backed up under
    # its own tag, so single-service backups show up in the dashboard like full/db.
    # For aggregate modes (full/db), all relevant directories are backed up.
    store_in_restic() {
        log "Storing backups in restic repository..."
        
        # Initialize repository if needed
        load_restic_repository "default"
        ensure_restic_repository
        
        # Determine which directories to back up for this mode
        local dirs_tags=()
        case "$MODE" in
            full)
                dirs_tags=(
                    "$BACKUP_BASE/postgresql postgresql"
                    "$BACKUP_BASE/redis redis"
                    "$BACKUP_BASE/mongodb mongodb"
                    "$BACKUP_BASE/elasticsearch elasticsearch"
                    "$BACKUP_BASE/files files"
                )
                ;;
            db)
                dirs_tags=(
                    "$BACKUP_BASE/postgresql postgresql"
                    "$BACKUP_BASE/redis redis"
                    "$BACKUP_BASE/mongodb mongodb"
                    "$BACKUP_BASE/elasticsearch elasticsearch"
                )
                ;;
            postgres|redis|mongo|elasticsearch|files)
                dirs_tags=("$(mode_to_dir "$MODE")")
                ;;
            *)
                log "Unknown backup mode: $MODE"
                return 1
                ;;
        esac
        
        for entry in "${dirs_tags[@]}"; do
            backup_dir="${entry% *}"
            tag="${entry##* }"
            if [ -d "$backup_dir" ] && [ "$(ls -A "$backup_dir" 2>/dev/null)" ]; then
                log "Backing up $backup_dir to restic (tag: $tag)..."
                restic backup "$backup_dir" --tag "$tag"
            fi
        done
        
        # Clean old snapshots (applies for every mode)
        log "Cleaning old snapshots..."
        restic forget --keep-daily "$RETENTION_DAYS" --keep-weekly "$RETENTION_WEEKLY" --keep-monthly "$RETENTION_MONTHLY" --prune
    }
    
    # Cleanup old local backups
    cleanup_local_backups() {
        local local_retention_days="${LOCAL_RETENTION_DAYS:-7}"
        local backup_dir
        [[ "$local_retention_days" =~ ^[0-9]+$ ]] || {
            log "Invalid LOCAL_RETENTION_DAYS: $local_retention_days"
            return 1
        }
        log "Cleaning local backup artifacts older than $local_retention_days days..."
        for backup_dir in postgresql redis mongodb elasticsearch files; do
            [ -d "$BACKUP_BASE/$backup_dir" ] || continue
            find "$BACKUP_BASE/$backup_dir" -type f \
                \( -name '*.gz' -o -name '*.json' -o -name '*.rdb' -o -name '*.tar' \) \
                -mtime "+$local_retention_days" -delete
        done
    }
    
    # Main execution
    case "$MODE" in
        full)
            run_postgres_backup
            run_redis_backup
            run_mongo_backup
            run_elasticsearch_backup
            run_files_backup
            ;;
        db)
            run_postgres_backup
            run_redis_backup
            run_mongo_backup
            run_elasticsearch_backup
            ;;
        files)
            run_files_backup
            ;;
        postgres)
            run_postgres_backup
            ;;
        redis)
            run_redis_backup
            ;;
        mongo)
            run_mongo_backup
            ;;
        elasticsearch)
            run_elasticsearch_backup
            ;;
        *)
            log "Unknown backup mode: $MODE"
            log "Usage: $0 [full|db|files|postgres|redis|mongo|elasticsearch|profile <profile_id>]"
            exit 1
            ;;
    esac
    
    # Store in restic (for every mode, so single-service backups also appear in
    # the dashboard, which reads from restic snapshots)
    store_in_restic
    
    # Cleanup old local backups
    cleanup_local_backups
    
else
    # No mode specified - show usage
    log "Usage: $0 [full|db|files|postgres|redis|mongo|elasticsearch|profile <profile_id>]"
    exit 1
fi

log "=========================================="
log "Backup job completed successfully!"
log "=========================================="

# Generate backup report
REPORT_FILE="$BACKUP_BASE/reports/report_$(date +%Y%m%d_%H%M%S).txt"
mkdir -p "$(dirname "$REPORT_FILE")"

cat > "$REPORT_FILE" << EOF
Backup Report
=============
Date: $(date)
Mode: $MODE
Status: SUCCESS

Backups created:
$(find "$BACKUP_BASE" -name "*.gz" -mtime -1 -exec ls -lh {} \; 2>/dev/null)

Restic snapshots:
$(RESTIC_REPOSITORY="$RESTIC_REPO" RESTIC_PASSWORD="$RESTIC_PASSWORD" restic snapshots 2>/dev/null || echo "N/A")
EOF

log "Report saved to: $REPORT_FILE"
