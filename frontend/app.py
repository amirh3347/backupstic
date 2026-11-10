#!/usr/bin/env python3
"""Backup Dashboard API - Enhanced Flask backend with profiles and authentication"""

import os
import json
import subprocess
import datetime
import fcntl
import hmac
import html
import shutil
import tempfile
import threading
import time
from collections import deque
from contextlib import contextmanager
from pathlib import Path
from flask import Flask, jsonify, request, send_from_directory, send_file, after_this_request

# Import our new modules
import sys
for backend_dir in (
    os.environ.get('BACKEND_DIR', ''),
    '/app/backend',
    str(Path(__file__).resolve().parents[1] / 'backend'),
):
    if backend_dir and os.path.isdir(backend_dir):
        sys.path.insert(0, backend_dir)
        break
from profiles import (
    get_storage,
    validate_profile,
    sanitize_profile,
    get_repository_storage,
    validate_repository,
    sanitize_repository,
)
from auth import AuthManager, token_required, init_auth
from schedules import (
    sync_profile_schedules_from_loader,
    valid_cron_expression,
    write_schedule_file,
)
from postgres_verify import PostgresRestoreTester
import plain_export
import config_backup
from download_links import (
    DownloadLinkStore,
    job_id_for as download_job_id_for,
    public_state as public_download_state,
)
from verification import (
    SNAPSHOT_ID_RE,
    Verification,
    VerificationStore,
    VerifyQueue,
    cleanup_stale_work_dirs,
    new_state as new_verification_state,
    summarize as summarize_verification,
)

app = Flask(__name__, static_folder='static')
app.config['MAX_CONTENT_LENGTH'] = int(os.environ.get('MAX_REQUEST_BYTES', 1048576))

# Configuration
RESTIC_REPO = os.environ.get('RESTIC_REPO', '/var/backups/restic-repo')
RESTIC_PASSWORD = os.environ.get('RESTIC_PASSWORD', '')
BACKUP_BASE = os.environ.get('BACKUP_BASE', '/var/backups')
SCRIPTS_DIR = os.environ.get('SCRIPTS_DIR', '/scripts')
BACKUP_RETENTION_DAYS = int(os.environ.get('RETENTION_DAYS', 30))
BACKUP_RETENTION_WEEKLY = int(os.environ.get('RETENTION_WEEKLY', 12))
BACKUP_RETENTION_MONTHLY = int(os.environ.get('RETENTION_MONTHLY', 12))
CONFIG_BACKUP_SSH_PRIVATE_KEY = os.environ.get(
    'CONFIG_BACKUP_SSH_PRIVATE_KEY',
    '/run/secrets/config-backup/id_ed25519'
)
CONFIG_BACKUP_SSH_PUBLIC_KEY_FILE = os.environ.get(
    'CONFIG_BACKUP_SSH_PUBLIC_KEY_FILE',
    f'{CONFIG_BACKUP_SSH_PRIVATE_KEY}.pub'
)
CONFIG_BACKUP_SSH_PUBLIC_KEY = os.environ.get('CONFIG_BACKUP_SSH_PUBLIC_KEY', '')
SSH_CONNECT_TIMEOUT = int(os.environ.get('CONFIG_BACKUP_SSH_CONNECT_TIMEOUT', 10))
VERIFY_LOCK_WAIT_SECONDS = max(0, int(os.environ.get('VERIFY_LOCK_WAIT_SECONDS', 3600)))
# Optional dedicated PostgreSQL server used to test-restore SQL dumps.
VERIFY_POSTGRES_URL = os.environ.get('VERIFY_POSTGRES_URL', '').strip()
VERIFY_POSTGRES_TIMEOUT = max(60, int(os.environ.get('VERIFY_POSTGRES_TIMEOUT', 7200)))
# How often the dashboard looks for snapshots queued by profiles with
# "verify after backup" enabled. 0 disables automatic health checks.
AUTO_VERIFY_POLL_SECONDS = max(0, int(os.environ.get('AUTO_VERIFY_POLL_SECONDS', 15)))
# Wrong repository passwords allowed per user and window for plain downloads.
PLAIN_DOWNLOAD_MAX_FAILURES = max(1, int(os.environ.get('PLAIN_DOWNLOAD_MAX_FAILURES', 5)))
PLAIN_DOWNLOAD_FAILURE_WINDOW = max(1, int(os.environ.get('PLAIN_DOWNLOAD_FAILURE_WINDOW', 300)))
# Disaster recovery: each repository keeps a copy of its own profiles.
CONFIG_SNAPSHOT_ENABLED = os.environ.get('CONFIG_SNAPSHOT_ENABLED', 'true').strip().lower() not in ('0', 'false', 'no', 'off')
CONFIG_SNAPSHOT_KEEP = max(0, int(os.environ.get('CONFIG_SNAPSHOT_KEEP', 20)))
# Background housekeeping cadence (post-backup checks, config snapshots, link cleanup).
MAINTENANCE_INTERVAL_SECONDS = AUTO_VERIFY_POLL_SECONDS or 15
# One-time download links: lifetime once the file is ready, how many may be
# prepared at once (each needs 2-3x the backup size on disk), and when a job
# whose worker died is given up.
DOWNLOAD_LINK_TTL_SECONDS = max(60, int(os.environ.get('DOWNLOAD_LINK_TTL_SECONDS', 3600)))
DOWNLOAD_LINK_MAX_PREPARING = max(1, int(os.environ.get('DOWNLOAD_LINK_MAX_PREPARING', 2)))
DOWNLOAD_LINK_PREPARE_TIMEOUT = max(600, int(os.environ.get('DOWNLOAD_LINK_PREPARE_TIMEOUT', 6 * 3600)))
MANUAL_BACKUP_LOG_MAX_BYTES = max(
    1,
    int(os.environ.get('MANUAL_BACKUP_LOG_MAX_BYTES', 10 * 1024 * 1024)),
)
MANUAL_BACKUP_LOG_BACKUP_COUNT = max(
    0,
    int(os.environ.get('MANUAL_BACKUP_LOG_BACKUP_COUNT', 3)),
)
SSH_HOST_KEY_CHECKING = os.environ.get(
    'CONFIG_BACKUP_SSH_HOST_KEY_CHECKING',
    'accept-new',
).strip().lower()
if SSH_HOST_KEY_CHECKING not in {'yes', 'accept-new', 'no'}:
    raise RuntimeError(
        'CONFIG_BACKUP_SSH_HOST_KEY_CHECKING must be yes, accept-new, or no'
    )


def sync_profile_crontab():
    """Publish a complete schedule derived from the latest profile storage."""
    return sync_profile_schedules_from_loader(
        lambda: get_storage().get_all(),
        os.path.join(BACKUP_BASE, 'schedules.cron'),
        scripts_dir=SCRIPTS_DIR,
        backup_base=BACKUP_BASE,
        fallback_entry=os.environ.get('BACKUP_CRON', ''),
    )

# Initialize auth
init_auth(app)

def start_background(args):
    """Start a detached backup process with size-bounded rotating output."""
    os.makedirs(BACKUP_BASE, exist_ok=True)
    log_path = os.path.join(BACKUP_BASE, 'manual_backup.log')
    subprocess.Popen(
        [
            sys.executable,
            os.path.join(SCRIPTS_DIR, 'run-with-rotating-log.py'),
            '--log-file',
            log_path,
            '--max-bytes',
            str(MANUAL_BACKUP_LOG_MAX_BYTES),
            '--backup-count',
            str(MANUAL_BACKUP_LOG_BACKUP_COUNT),
            '--',
            *args,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
        env=os.environ.copy(),
    )


@app.after_request
def security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=()'
    response.headers['Content-Security-Policy'] = (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'"
    )
    if request.path.startswith(('/api/', '/dl/')):
        response.headers['Cache-Control'] = 'no-store'
    return response

def restic_env_for_repository(repo=None):
    env = os.environ.copy()
    if repo is None:
        env['RESTIC_REPOSITORY'] = RESTIC_REPO
        env['RESTIC_PASSWORD'] = RESTIC_PASSWORD
        return env
    env['RESTIC_REPOSITORY'] = repo.get('repository', RESTIC_REPO)
    env['RESTIC_PASSWORD'] = repo.get('password', RESTIC_PASSWORD)
    if repo.get('aws_access_key_id'):
        env['AWS_ACCESS_KEY_ID'] = repo.get('aws_access_key_id', '')
    if repo.get('aws_secret_access_key'):
        env['AWS_SECRET_ACCESS_KEY'] = repo.get('aws_secret_access_key', '')
    if repo.get('aws_default_region'):
        env['AWS_DEFAULT_REGION'] = repo.get('aws_default_region', '')
    return env

def run_restic_command(args, repo=None, timeout=300):
    try:
        result = subprocess.run(
            ['restic', *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=restic_env_for_repository(repo),
        )
        return {
            'success': result.returncode == 0,
            'stdout': result.stdout,
            'stderr': result.stderr,
            'returncode': result.returncode,
        }
    except subprocess.TimeoutExpired:
        return {'success': False, 'stdout': '', 'stderr': 'Command timed out', 'returncode': -1}
    except Exception as e:
        return {'success': False, 'stdout': '', 'stderr': str(e), 'returncode': -1}

def app_backup_state():
    """Return whether the dashboard/cron backup lock is currently held."""
    lock_file = os.path.join(BACKUP_BASE, 'backup.lock')
    state_file = os.path.join(BACKUP_BASE, 'backup.state')
    running = False
    state = None
    try:
        os.makedirs(BACKUP_BASE, exist_ok=True)
        fd = os.open(lock_file, os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
        except BlockingIOError:
            running = True
            if os.path.exists(state_file):
                try:
                    with open(state_file) as f:
                        state = json.load(f)
                except Exception:
                    state = None
        finally:
            os.close(fd)
    except Exception:
        running = False
    return running, state

def restic_lock_error(result):
    text = f"{result.get('stderr', '')}\n{result.get('stdout', '')}".lower()
    return 'repository is already locked' in text or 'unable to create lock in backend' in text

def unlock_restic_repository(repo):
    return run_restic_command(['unlock'], repo=repo, timeout=120)

def run_restic_command_with_stale_lock_retry(args, repo=None, timeout=300, backup_lock_held=False):
    result = run_restic_command(args, repo=repo, timeout=timeout)
    if result['success'] or not restic_lock_error(result):
        return result

    # Holding the backup lock ourselves proves no backup owns the restic lock.
    running = False if backup_lock_held else app_backup_state()[0]
    if running:
        result['stderr'] = 'Restic repository is locked because a backup is currently running'
        return result

    unlock = unlock_restic_repository(repo)
    if not unlock['success']:
        result['stderr'] = unlock['stderr'] or result['stderr'] or 'Failed to remove stale restic lock'
        return result

    retry = run_restic_command(args, repo=repo, timeout=timeout)
    if not retry['success'] and restic_lock_error(retry):
        retry['stderr'] = retry['stderr'] or 'Restic repository is still locked after unlock'
    return retry

def check_restic_repository(repo):
    res = run_restic_command_with_stale_lock_retry(['snapshots', '--json'], repo=repo, timeout=60)
    if res['success']:
        return True, ''
    return False, res['stderr'] or 'Cannot access restic repository'

def init_restic_repository(repo):
    ok, _ = check_restic_repository(repo)
    if ok:
        return True, 'Repository already initialized'
    res = run_restic_command(['init'], repo=repo, timeout=120)
    if res['success']:
        return True, 'Repository initialized'
    return False, res['stderr'] or 'Failed to initialize repository'

def all_repositories():
    return get_repository_storage().get_all()

def find_snapshot_in_repository(snapshot_id, repo):
    """Return the snapshot with a full or short id from one repository, or None."""
    res = run_restic_command_with_stale_lock_retry(['snapshots', '--json'], repo=repo, timeout=60)
    if not res['success'] or not res['stdout']:
        return None
    try:
        snapshots = json.loads(res['stdout'])
    except (ValueError, TypeError):
        return None
    for snap in snapshots:
        sid = snap.get('id', '')
        if sid == snapshot_id or sid.startswith(snapshot_id):
            return snap
    return None

def find_snapshot(snapshot_id):
    """Return (repository, snapshot) for a full or short snapshot id."""
    for repo in all_repositories():
        snap = find_snapshot_in_repository(snapshot_id, repo)
        if snap is not None:
            return repo, snap
    return None, None

def find_snapshot_repository(snapshot_id):
    return find_snapshot(snapshot_id)[0]

@contextmanager
def backup_lock(mode, wait_seconds, on_wait=None):
    """Hold the global backup lock so scheduled backups/prunes queue behind us.

    Publishes backup.state like backup-all.sh does, so the dashboard banner and
    /api/backup-status explain why the lock is busy.
    """
    os.makedirs(BACKUP_BASE, exist_ok=True)
    lock_file = os.path.join(BACKUP_BASE, 'backup.lock')
    state_file = os.path.join(BACKUP_BASE, 'backup.state')
    fd = os.open(lock_file, os.O_CREAT | os.O_RDWR)
    locked = False
    try:
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except BlockingIOError:
                if on_wait:
                    on_wait()
                    on_wait = None
                if time.monotonic() >= deadline:
                    raise TimeoutError('Timed out waiting for the running backup to finish')
                time.sleep(2)
        with open(state_file, 'w') as f:
            json.dump({
                'mode': mode,
                'kind': 'verify',
                'started_at': datetime.datetime.now().astimezone().isoformat(),
                'pid': os.getpid(),
            }, f)
        yield
    finally:
        if locked:
            try:
                os.unlink(state_file)
            except OSError:
                pass
        os.close(fd)

def get_profile_repository(profile):
    repo_id = profile.get('repository_id') or 'default'
    repo = get_repository_storage().get(repo_id)
    if repo is None:
        return None, f'Restic repository not found: {repo_id}'
    return repo, ''

BACKUP_TYPES = {'postgresql', 'mongodb', 'redis', 'ssh_files', 'ssh-files'}

def snapshot_profile_name(snap):
    tags = snap.get('tags') or []
    for tag in tags:
        if tag in BACKUP_TYPES or tag == 'files' or str(tag).startswith('profile:'):
            continue
        return tag
    return ''

def snapshot_type(snap):
    tags = snap.get('tags') or []
    for tag in tags:
        if tag in BACKUP_TYPES:
            return 'ssh_files' if tag == 'ssh-files' else tag
    return ''

def snapshot_profile_id(snap, profiles):
    tags = snap.get('tags') or []
    for tag in tags:
        if isinstance(tag, str) and tag.startswith('profile:'):
            pid = tag.split(':', 1)[1]
            if pid in profiles:
                return pid

    paths = snap.get('paths') or []
    for path in paths:
        for pid in profiles:
            if isinstance(path, str) and path.rstrip('/').endswith(f'/.staging/{pid}'):
                return pid

    name = snapshot_profile_name(snap)
    stype = snapshot_type(snap)
    for pid, profile in profiles.items():
        if profile.get('name') == name and profile.get('type') == stype:
            return pid
    return None

def apply_profile_snapshot_policy(snaps, active_profiles_only=False, respect_retention=False):
    profiles = {p.get('id'): p for p in get_storage().get_all() if p.get('id')}
    enriched = []
    for snap in snaps:
        pid = snapshot_profile_id(snap, profiles)
        if active_profiles_only and not pid:
            continue
        if pid:
            snap['profile_id'] = pid
            snap['profile_name'] = profiles[pid].get('name')
            snap['profile_type'] = profiles[pid].get('type')
            snap['max_backups'] = profiles[pid].get('max_backups', 0)
        enriched.append(snap)

    if not respect_retention:
        return enriched

    by_profile = {}
    passthrough = []
    for snap in enriched:
        pid = snap.get('profile_id')
        if not pid:
            passthrough.append(snap)
            continue
        by_profile.setdefault(pid, []).append(snap)

    limited = list(passthrough)
    for pid, items in by_profile.items():
        max_backups = int(profiles.get(pid, {}).get('max_backups') or 0)
        items.sort(key=lambda s: s.get('time', ''), reverse=True)
        limited.extend(items[:max_backups] if max_backups > 0 else items)
    limited.sort(key=lambda s: s.get('time', ''), reverse=True)
    return limited

def app_public_key():
    """Return the configured public key shown to users for key auth."""
    if CONFIG_BACKUP_SSH_PUBLIC_KEY.strip():
        return CONFIG_BACKUP_SSH_PUBLIC_KEY.strip()
    try:
        with open(CONFIG_BACKUP_SSH_PUBLIC_KEY_FILE) as f:
            return f.read().strip()
    except OSError:
        return ''

def ssh_base_command(profile):
    """Build an SSH command list without leaking passwords in argv."""
    host = profile.get('ssh_host')
    port = str(profile.get('ssh_port') or 22)
    user = profile.get('ssh_user')
    auth_method = profile.get('auth_method') or ('password' if profile.get('ssh_password') else 'key')

    common = [
        '-p', port,
        '-o', f'ConnectTimeout={SSH_CONNECT_TIMEOUT}',
        '-o', f'StrictHostKeyChecking={SSH_HOST_KEY_CHECKING}',
        '-o', f'UserKnownHostsFile={os.environ.get("CONFIG_BACKUP_SSH_KNOWN_HOSTS", "/run/secrets/config-backup/known_hosts")}',
        '-o', 'LogLevel=ERROR',
    ]
    if auth_method == 'key':
        if not os.path.exists(CONFIG_BACKUP_SSH_PRIVATE_KEY):
            return None, {}, f'Configured private key not found: {CONFIG_BACKUP_SSH_PRIVATE_KEY}'
        return [
            'ssh',
            '-i', CONFIG_BACKUP_SSH_PRIVATE_KEY,
            '-o', 'BatchMode=yes',
            *common,
            f'{user}@{host}',
        ], {}, ''

    if shutil.which('sshpass') is None:
        return None, {}, 'sshpass is required for password authentication'
    env = os.environ.copy()
    env['SSHPASS'] = profile.get('ssh_password', '')
    return [
        'sshpass', '-e',
        'ssh',
        '-o', 'PreferredAuthentications=password',
        '-o', 'PubkeyAuthentication=no',
        *common,
        f'{user}@{host}',
    ], env, ''

def check_ssh_profile_connection(profile):
    """Validate SSH connectivity before saving an ssh_files profile."""
    cmd, env, error = ssh_base_command(profile)
    if error:
        return False, error
    try:
        result = subprocess.run(
            [*cmd, 'true'],
            capture_output=True,
            text=True,
            timeout=SSH_CONNECT_TIMEOUT + 5,
            env=env or os.environ.copy(),
        )
    except subprocess.TimeoutExpired:
        return False, 'SSH connection timed out'
    except FileNotFoundError as exc:
        return False, f'Missing command: {exc.filename}'
    except Exception as exc:
        return False, str(exc)

    stderr = (result.stderr or '').strip()
    lowered = stderr.lower()
    if result.returncode != 0:
        if 'permission denied' in lowered or 'authentication failed' in lowered:
            return False, 'SSH authentication failed'
        if 'connection timed out' in lowered or 'operation timed out' in lowered:
            return False, 'SSH connection timed out'
        return False, stderr or f'SSH connection failed with exit code {result.returncode}'

    try:
        rsync_check = subprocess.run(
            [*cmd, 'command -v rsync >/dev/null 2>&1'],
            capture_output=True,
            text=True,
            timeout=SSH_CONNECT_TIMEOUT + 5,
            env=env or os.environ.copy(),
        )
    except subprocess.TimeoutExpired:
        return False, 'SSH connection timed out while checking remote rsync'
    except Exception as exc:
        return False, str(exc)

    if rsync_check.returncode != 0:
        return False, 'rsync is not installed on the remote server'

    return True, ''

def format_bytes(num_bytes):
    """Human-readable size, e.g. 1536 -> '1.5 KB'"""
    try:
        num_bytes = float(num_bytes)
    except (TypeError, ValueError):
        return '0 B'
    for unit in ['B', 'KB', 'MB', 'GB', 'TB', 'PB']:
        if abs(num_bytes) < 1024.0:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.1f} EB"

# Serve static files
@app.route('/')
def index():
    return send_from_directory('static', 'index.html')

@app.route('/favicon.ico')
def favicon():
    return send_from_directory('static', 'favicon.svg', mimetype='image/svg+xml')

@app.route('/<path:path>')
def static_files(path):
    return send_from_directory('static', path)

# ==================== AUTHENTICATION ENDPOINTS ====================

@app.route('/api/login', methods=['POST'])
def login():
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': 'No JSON data provided'}), 400
    
    username = data.get('username')
    password = data.get('password')
    
    if not username or not password:
        return jsonify({'success': False, 'error': 'Username and password required'}), 400
    
    if app.auth_manager.authenticate_user(username, password):
        token = app.auth_manager.generate_token(username)
        return jsonify({
            'success': True,
            'token': token,
            'user': username
        })
    else:
        return jsonify({'success': False, 'error': 'Invalid credentials'}), 401

@app.route('/api/logout', methods=['POST'])
def logout():
    return jsonify({'success': True, 'message': 'Logged out successfully'})

@app.route('/api/validate-token', methods=['POST'])
@token_required
def validate_token():
    return jsonify({'success': True, 'user': request.current_user})

# ==================== PROFILE MANAGEMENT ENDPOINTS ====================

@app.route('/api/profiles', methods=['GET'])
@token_required
def get_profiles():
    """Get all backup profiles"""
    storage = get_storage()
    profiles = storage.get_all()
    return jsonify({'success': True, 'profiles': [sanitize_profile(p) for p in profiles]})

@app.route('/api/profiles', methods=['POST'])
@token_required
def create_profile():
    """Create a new backup profile"""
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': 'No JSON data provided'}), 400
    for immutable_field in ('id', 'created_at', 'updated_at'):
        data.pop(immutable_field, None)
    
    # Validate profile
    is_valid, error_msg = validate_profile(data)
    if not is_valid:
        return jsonify({'success': False, 'error': error_msg}), 400

    _, repo_error = get_profile_repository(data)
    if repo_error:
        return jsonify({'success': False, 'error': repo_error}), 400

    if data.get('type') == 'ssh_files':
        ok, error_msg = check_ssh_profile_connection(data)
        if not ok:
            return jsonify({'success': False, 'error': error_msg}), 400
    
    storage = get_storage()
    profile = storage.create(data)
    sync_profile_crontab()
    mark_config_dirty(profile.get('repository_id'))
    return jsonify({'success': True, 'profile': sanitize_profile(profile)})

@app.route('/api/profiles/<profile_id>', methods=['GET'])
@token_required
def get_profile(profile_id):
    """Get a specific backup profile"""
    storage = get_storage()
    profile = storage.get(profile_id)
    if profile is None:
        return jsonify({'success': False, 'error': 'Profile not found'}), 404
    return jsonify({'success': True, 'profile': sanitize_profile(profile)})

@app.route('/api/profiles/<profile_id>', methods=['PUT'])
@token_required
def update_profile(profile_id):
    """Update a backup profile"""
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': 'No JSON data provided'}), 400
    for immutable_field in ('id', 'created_at', 'updated_at'):
        data.pop(immutable_field, None)

    storage = get_storage()
    existing = storage.get(profile_id)
    if existing is None:
        return jsonify({'success': False, 'error': 'Profile not found'}), 404

    for secret_field in ('password', 'ssh_password'):
        if secret_field in data and data.get(secret_field) == '' and existing.get(secret_field):
            data.pop(secret_field)

    # Validate the merged profile so partial updates keep existing fields and
    # type-specific normalizers can persist their derived fields.
    merged = existing.copy()
    merged.update(data)
    is_valid, error_msg = validate_profile(merged)
    
    if not is_valid:
        return jsonify({'success': False, 'error': error_msg}), 400

    _, repo_error = get_profile_repository(merged)
    if repo_error:
        return jsonify({'success': False, 'error': repo_error}), 400

    if merged.get('type') == 'ssh_files':
        ok, error_msg = check_ssh_profile_connection(merged)
        if not ok:
            return jsonify({'success': False, 'error': error_msg}), 400
    
    profile = storage.update(profile_id, merged)
    if profile is None:
        return jsonify({'success': False, 'error': 'Profile not found'}), 404
    sync_profile_crontab()
    mark_config_dirty(existing.get('repository_id'), profile.get('repository_id'))
    return jsonify({'success': True, 'profile': sanitize_profile(profile)})

@app.route('/api/profiles/<profile_id>', methods=['DELETE'])
@token_required
def delete_profile(profile_id):
    """Delete profile metadata while preserving its backup snapshots."""
    storage = get_storage()
    profile = storage.get(profile_id)
    if profile is None:
        return jsonify({'success': False, 'error': 'Profile not found'}), 404

    success = storage.delete(profile_id)
    if not success:
        return jsonify({'success': False, 'error': 'Profile not found'}), 404
    sync_profile_crontab()
    mark_config_dirty(profile.get('repository_id'))
    return jsonify({
        'success': True,
        'message': 'Profile deleted; existing snapshots were preserved',
        'snapshots_preserved': True,
    })

@app.route('/api/config-backup/public-key')
@token_required
def get_config_backup_public_key():
    """Return the app-level SSH public key used by Configuration Backup."""
    public_key = app_public_key()
    if not public_key:
        return jsonify({
            'success': False,
            'error': 'Configuration Backup public key is not configured'
        }), 404
    return jsonify({'success': True, 'public_key': public_key})

# ==================== RESTIC REPOSITORY MANAGEMENT ====================

@app.route('/api/restic-repositories', methods=['GET'])
@token_required
def list_restic_repositories():
    storage = get_repository_storage()
    repos = storage.get_all()
    state = config_sync_state()
    items = []
    for repo in repos:
        item = sanitize_repository(repo)
        last = state.last(repo.get('id')) or {}
        item['config_backup'] = {
            'enabled': CONFIG_SNAPSHOT_ENABLED,
            'synced_at': last.get('synced_at'),
            'profiles': last.get('profiles'),
            'pending': state.is_dirty(repo.get('id')),
        }
        items.append(item)
    return jsonify({'success': True, 'repositories': items})

@app.route('/api/restic-repositories', methods=['POST'])
@token_required
def create_restic_repository():
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': 'No JSON data provided'}), 400
    for immutable_field in ('id', 'created_at', 'updated_at'):
        data.pop(immutable_field, None)
    is_valid, error_msg = validate_repository(data)
    if not is_valid:
        return jsonify({'success': False, 'error': error_msg}), 400
    storage = get_repository_storage()
    repo = storage.create(data)
    return jsonify({'success': True, 'repository': sanitize_repository(repo)})

@app.route('/api/restic-repositories/<repo_id>', methods=['GET'])
@token_required
def get_restic_repository(repo_id):
    storage = get_repository_storage()
    repo = storage.get(repo_id)
    if repo is None:
        return jsonify({'success': False, 'error': 'Repository not found'}), 404
    return jsonify({'success': True, 'repository': sanitize_repository(repo)})

@app.route('/api/restic-repositories/<repo_id>', methods=['PUT'])
@token_required
def update_restic_repository(repo_id):
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': 'No JSON data provided'}), 400
    for immutable_field in ('id', 'created_at', 'updated_at'):
        data.pop(immutable_field, None)
    storage = get_repository_storage()
    existing = storage.get(repo_id)
    if existing is None:
        return jsonify({'success': False, 'error': 'Repository not found'}), 404
    for secret_field in ('password', 'aws_access_key_id', 'aws_secret_access_key'):
        if secret_field in data and data.get(secret_field) == '' and existing.get(secret_field):
            data.pop(secret_field)
    merged = existing.copy()
    merged.update(data)
    is_valid, error_msg = validate_repository(merged)
    if not is_valid:
        return jsonify({'success': False, 'error': error_msg}), 400
    repo = storage.update(repo_id, merged)
    # A new location/password needs a fresh copy of the configuration.
    config_sync_state().forget(repo_id)
    mark_config_dirty(repo_id)
    return jsonify({'success': True, 'repository': sanitize_repository(repo)})

@app.route('/api/restic-repositories/<repo_id>', methods=['DELETE'])
@token_required
def delete_restic_repository(repo_id):
    storage = get_repository_storage()
    referencing_profiles = [
        profile.get('name', profile.get('id'))
        for profile in get_storage().get_all()
        if (profile.get('repository_id') or 'default') == repo_id
    ]
    if referencing_profiles:
        return jsonify({
            'success': False,
            'error': 'Repository is still referenced by backup profiles',
            'profiles': referencing_profiles,
        }), 409
    if not storage.delete(repo_id):
        return jsonify({'success': False, 'error': 'Repository not found or default repository cannot be deleted'}), 404
    config_sync_state().forget(repo_id)
    return jsonify({'success': True, 'message': 'Repository deleted successfully'})

@app.route('/api/restic-repositories/<repo_id>/check', methods=['POST'])
@token_required
def check_restic_repository_endpoint(repo_id):
    storage = get_repository_storage()
    repo = storage.get(repo_id)
    if repo is None:
        return jsonify({'success': False, 'error': 'Repository not found'}), 404
    ok, error_msg = check_restic_repository(repo)
    if not ok:
        return jsonify({'success': False, 'error': error_msg}), 400
    return jsonify({'success': True, 'message': 'Repository is accessible'})

@app.route('/api/restic-repositories/<repo_id>/init', methods=['POST'])
@token_required
def init_restic_repository_endpoint(repo_id):
    storage = get_repository_storage()
    repo = storage.get(repo_id)
    if repo is None:
        return jsonify({'success': False, 'error': 'Repository not found'}), 404
    ok, message = init_restic_repository(repo)
    if not ok:
        return jsonify({'success': False, 'error': message}), 400
    return jsonify({'success': True, 'message': message})

# ==================== BACKUP OPERATION ENDPOINTS ====================

@app.route('/api/backup', methods=['POST'])
@token_required
def trigger_backup():
    """Trigger a manual backup for specific profile(s)"""
    data = request.get_json() or {}
    profile_ids = data.get('profile_ids', [])  # List of profile IDs to backup
    mode = data.get('mode', 'profile')  # 'profile' for specific profiles, or legacy modes
    
    if mode == 'profile':
        storage = get_storage()
        if not profile_ids:
            profiles = [p for p in storage.get_all() if p.get('enabled', True)]
            if not profiles:
                return jsonify({'success': False, 'error': 'No enabled profiles found'}), 400
        else:
            profiles = []
            for pid in profile_ids:
                profile = storage.get(pid)
                if profile is None:
                    return jsonify({'success': False, 'error': f'Profile not found: {pid}'}), 404
                if not profile.get('enabled', True):
                    return jsonify({'success': False, 'error': f'Profile is disabled: {profile["name"]}'}), 400
                profiles.append(profile)

        checked_repositories = set()
        for profile in profiles:
            repo, repo_error = get_profile_repository(profile)
            if repo_error:
                return jsonify({'success': False, 'error': repo_error}), 400
            repo_id = repo.get('id') or 'default'
            if repo_id in checked_repositories:
                continue
            ok, message = init_restic_repository(repo)
            if not ok:
                return jsonify({
                    'success': False,
                    'error': f'Restic repository check failed for {repo.get("name", repo_id)}: {message}'
                }), 400
            checked_repositories.add(repo_id)
        
        # Run the selected profiles SEQUENTIALLY in a single detached job.
        # backup-all.sh takes a global flock, so launching one nohup per profile
        # in parallel made all-but-the-first exit with "already running" and only
        # the first profile got backed up. A sequential loop releases the lock
        # between profiles so every one runs.
        start_background([f'{SCRIPTS_DIR}/backup-profile-batch.sh', *[p['id'] for p in profiles]])

        profile_names = [p['name'] for p in profiles]
        return jsonify({
            'success': True,
            'message': f'Backup started for profiles: {", ".join(profile_names)}'
        })
    else:
        # Legacy backup modes (full, db, files, etc.) - kept for compatibility
        if mode not in ['full', 'db', 'files', 'postgres', 'redis', 'mongo', 'elasticsearch']:
            return jsonify({'success': False, 'error': 'Invalid backup mode'}), 400
        
        # Run backup in background
        start_background([f'{SCRIPTS_DIR}/backup-all.sh', mode])
        
        return jsonify({'success': True, 'message': f'Legacy backup started in background: {mode}'})

@app.route('/api/backup-status')
@token_required
def backup_status():
    """Check whether a backup is currently running."""
    running, state = app_backup_state()

    resp = {'running': running}
    if state:
        resp.update(state)
    return jsonify(resp)

# ==================== SNAPSHOT AND BACKUP MANAGEMENT ENDPOINTS ====================

def snapshot_size(snap):
    """Best-effort logical size (bytes) for a restic snapshot.

    restic >= 0.17 embeds a `summary` in `snapshots --json` with
    `total_bytes_processed`; older versions don't, so fall back to
    `restic stats <id>` (restore-size mode)."""
    summary = snap.get('summary') or {}
    size = summary.get('total_bytes_processed') or summary.get('data_added')
    if size:
        return int(size)
    sid = snap.get('id')
    if not sid:
        return 0
    repo = get_repository_storage().get(snap.get('repository_id') or 'default')
    res = run_restic_command_with_stale_lock_retry(['stats', sid, '--json', '--mode', 'restore-size'], repo=repo, timeout=60)
    if res['success'] and res['stdout']:
        try:
            return int(json.loads(res['stdout']).get('total_size', 0))
        except (ValueError, TypeError):
            return 0
    return 0

@app.route('/api/snapshots')
@token_required
def get_snapshots():
    """Get all snapshots, each enriched with a logical `size` in bytes."""
    active_profiles_only = request.args.get('active_profiles_only') in ('1', 'true', 'yes')
    respect_retention = request.args.get('respect_retention') in ('1', 'true', 'yes')
    repository_filter = request.args.get('repository_id') or ''
    all_snaps = []
    errors = []
    seen_repositories = set()
    seen_snapshots = set()
    for repo in all_repositories():
        if repository_filter and repo.get('id') != repository_filter:
            continue
        repo_key = repo.get('repository') or repo.get('id') or 'default'
        if repo_key in seen_repositories:
            continue
        seen_repositories.add(repo_key)
        result = run_restic_command_with_stale_lock_retry(['snapshots', '--json'], repo=repo, timeout=60)
        if not result['success']:
            errors.append(f"{repo.get('name')}: {result['stderr']}")
            continue
        if not result['stdout']:
            continue
        try:
            snapshots = json.loads(result['stdout'])
        except (ValueError, TypeError):
            errors.append(f"{repo.get('name')}: Failed to parse snapshots")
            continue
        for snap in snapshots:
            snap_key = (repo_key, snap.get('id'))
            if snap_key in seen_snapshots or is_config_snapshot(snap):
                continue
            seen_snapshots.add(snap_key)
            snap['repository_id'] = repo.get('id')
            snap['repository_name'] = repo.get('name')
            snap['size'] = snapshot_size(snap)
            all_snaps.append(snap)
    all_snaps = apply_profile_snapshot_policy(
        all_snaps,
        active_profiles_only=active_profiles_only,
        respect_retention=respect_retention,
    )
    if all_snaps or not errors:
        response = jsonify({'success': True, 'snapshots': all_snaps, 'errors': errors})
    else:
        response = jsonify({'success': False, 'error': '; '.join(errors)})
    response.headers['Cache-Control'] = 'no-store'
    return response

@app.route('/api/snapshot/<snapshot_id>/files')
@token_required
def get_snapshot_files(snapshot_id):
    """Get files in a snapshot"""
    repo = find_snapshot_repository(snapshot_id)
    if repo is None:
        return jsonify({'success': False, 'error': 'Snapshot not found'}), 404
    result = run_restic_command_with_stale_lock_retry(['ls', snapshot_id, '--json'], repo=repo, timeout=120)
    if result['success']:
        return jsonify({'success': True, 'output': '\n'.join(result['stdout'].splitlines()[:1000])})
    return jsonify({'success': False, 'error': result['stderr']})

@app.route('/api/backups', methods=['GET'])
@token_required
def list_backups():
    """List backup files available for download/delete"""
    # This would list actual backup files from restic or local storage
    # For now, we'll return restic snapshots as backup representations
    snapshots_resp = get_snapshots().get_json()
    if snapshots_resp.get('success'):
        backups = []
        for snap in snapshots_resp.get('snapshots', []):
            backups.append({
                'id': snap['id'],
                'short_id': snap['id'][:8],
                'time': snap['time'],
                'hostname': snap.get('hostname', 'unknown'),
                'tags': snap.get('tags', []),
                'paths': snap.get('paths', []),
                'size': snap.get('size', 0),
                'repository_id': snap.get('repository_id'),
                'repository_name': snap.get('repository_name'),
            })
        return jsonify({'success': True, 'backups': backups})
    return jsonify({'success': False, 'error': snapshots_resp.get('error', 'Failed to list backups')})

@app.route('/api/backup/<backup_id>', methods=['DELETE'])
@token_required
def delete_backup(backup_id):
    """Delete a backup snapshot"""
    repo, snap = find_snapshot(backup_id)
    if repo is None:
        return jsonify({'success': False, 'error': 'Snapshot not found'}), 404
    result = run_restic_command_with_stale_lock_retry(['forget', backup_id, '--prune'], repo=repo, timeout=600)
    if result['success']:
        verification_store().delete(snap.get('id', ''))
        VerifyQueue(BACKUP_BASE).remove(snap.get('id', ''))
        return jsonify({'success': True, 'message': 'Backup deleted successfully'})
    return jsonify({'success': False, 'error': result['stderr']})

def restore_snapshot(backup_id, repo, target, backup_lock_held=False):
    return run_restic_command_with_stale_lock_retry(
        ['restore', backup_id, '--target', target],
        repo=repo,
        timeout=1800,
        backup_lock_held=backup_lock_held,
    )

def package_encrypted_bundle(backup_id, repo, work, restore_dir):
    """Wrap restored files in a password-protected restic repository tarball.

    This is the exact artifact users download, so backup verification builds
    it through this function too. Returns (archive_path, error).
    """
    encrypted_repo = os.path.join(work, 'encrypted-restic-repo')
    readme = os.path.join(work, 'README_DECRYPT.txt')
    archive = os.path.join(work, f'backup_{backup_id[:8]}.restic.tar.gz')
    export_repo = {
        'repository': encrypted_repo,
        'password': repo.get('password', RESTIC_PASSWORD),
    }
    init_res = run_restic_command(['init', '--no-cache'], repo=export_repo, timeout=120)
    if not init_res['success']:
        return None, init_res['stderr'] or 'Failed to initialize encrypted export'

    backup_res = run_restic_command(
        ['backup', '--no-cache', restore_dir, '--tag', 'encrypted-download', '--tag', backup_id[:8]],
        repo=export_repo,
        timeout=1800,
    )
    if not backup_res['success']:
        return None, backup_res['stderr'] or 'Failed to build encrypted export'

    with open(readme, 'w') as f:
        f.write(
            "This archive contains a Restic encrypted repository.\n"
            "To decrypt/restore:\n"
            "  tar -xzf backup.restic.tar.gz\n"
            "  export RESTIC_REPOSITORY=$PWD/encrypted-restic-repo\n"
            "  export RESTIC_PASSWORD='<repository password>'\n"
            "  restic snapshots\n"
            "  restic restore latest --target ./restore-output\n"
        )

    try:
        tar_result = subprocess.run(
            ['tar', '-czf', archive, '-C', work, 'encrypted-restic-repo', 'README_DECRYPT.txt'],
            capture_output=True,
            text=True,
            timeout=1800,
        )
    except subprocess.TimeoutExpired:
        tar_result = subprocess.CompletedProcess(args=[], returncode=-1, stdout='', stderr='Command timed out')

    if tar_result.returncode != 0 or not os.path.exists(archive):
        return None, tar_result.stderr or 'Failed to package encrypted export'
    # The tarball now holds the repository; free the disk space early.
    shutil.rmtree(encrypted_repo, ignore_errors=True)
    return archive, ''

@app.route('/api/backup/<backup_id>/download')
@token_required
def download_backup(backup_id):
    """Return an encrypted restic repository bundle for the requested snapshot."""
    repo = find_snapshot_repository(backup_id)
    if repo is None:
        return jsonify({'success': False, 'error': 'Snapshot not found'}), 404

    work = tempfile.mkdtemp(prefix='restore_', dir=BACKUP_BASE)
    restore_dir = os.path.join(work, 'data')

    def cleanup():
        shutil.rmtree(work, ignore_errors=True)

    res = restore_snapshot(backup_id, repo, restore_dir)
    if not res['success']:
        cleanup()
        return jsonify({'success': False, 'error': res['stderr'] or 'Restore failed'}), 500

    archive, error = package_encrypted_bundle(backup_id, repo, work, restore_dir)
    if error:
        cleanup()
        return jsonify({'success': False, 'error': error}), 500

    @after_this_request
    def _remove(response):
        cleanup()
        return response

    return send_file(
        archive,
        as_attachment=True,
        download_name=f'backup_{backup_id[:8]}.restic.tar.gz',
        mimetype='application/gzip'
    )

_plain_download_failures = {}
_plain_download_failures_lock = threading.Lock()

class DownloadError(Exception):
    """Preparing a download failed; `status` is the HTTP status to report."""

    def __init__(self, message, status=500):
        super().__init__(message)
        self.status = status

def _plain_download_recent_failures(key, now):
    """Drop expired failures for `key`; caller holds the lock."""
    recent = [t for t in _plain_download_failures.get(key, []) if now - t < PLAIN_DOWNLOAD_FAILURE_WINDOW]
    if recent:
        _plain_download_failures[key] = recent
    else:
        _plain_download_failures.pop(key, None)
    return recent

def password_lockout_response(user):
    """429 response while `user` has too many recent wrong passwords, else None."""
    now = time.monotonic()
    with _plain_download_failures_lock:
        recent = _plain_download_recent_failures(user, now)
        if len(recent) < PLAIN_DOWNLOAD_MAX_FAILURES:
            return None
        retry_after = int(PLAIN_DOWNLOAD_FAILURE_WINDOW - (now - recent[0])) + 1
    response = jsonify({
        'success': False,
        'error': f'Too many wrong passwords. Try again in {retry_after} seconds.',
    })
    response.headers['Retry-After'] = str(retry_after)
    return response, 429

def repository_password_error(repo, password, user, snapshot_id):
    """403 response when `password` is not the repository's password, else None.

    Unencrypted exports leave restic's protection, so a dashboard session alone
    is not enough to take data out in the clear.
    """
    now = time.monotonic()
    expected = repo.get('password', RESTIC_PASSWORD) or ''
    if expected and hmac.compare_digest(password.encode(), expected.encode()):
        with _plain_download_failures_lock:
            _plain_download_failures.pop(user, None)
        return None
    with _plain_download_failures_lock:
        recent = _plain_download_recent_failures(user, now)
        recent.append(now)
        _plain_download_failures[user] = recent
        attempts_left = max(0, PLAIN_DOWNLOAD_MAX_FAILURES - len(recent))
    app.logger.warning('Rejected plain download of %s: wrong repository password', snapshot_id[:8])
    return jsonify({
        'success': False,
        'error': 'Wrong repository password',
        'attempts_left': attempts_left,
    }), 403

def download_disk_space_error(snap, factor):
    size = snapshot_size(snap)
    if not size:
        return ''
    needed = size * factor + 64 * 1024 * 1024
    free = shutil.disk_usage(BACKUP_BASE).free
    if free >= needed:
        return ''
    return (f'Not enough free disk space: needs about {format_bytes(needed)}, '
            f'only {format_bytes(free)} is available')

def build_download(fmt, snap, repo, work):
    """Restore `snap` into `work` and package it. Returns (file_path, download_name)."""
    os.makedirs(work, exist_ok=True)
    restore_dir = os.path.join(work, 'data')
    res = restore_snapshot(snap['id'], repo, restore_dir)
    if not res['success']:
        raise DownloadError(res['stderr'] or 'Restore failed')
    if fmt == 'plain':
        try:
            return plain_export.build(restore_dir, snap, work)
        except plain_export.PlainExportError as exc:
            raise DownloadError(str(exc)) from exc
    archive, error = package_encrypted_bundle(snap['id'], repo, work, restore_dir)
    if error:
        raise DownloadError(error)
    return archive, f'backup_{snap["id"][:8]}.restic.tar.gz'

def resolve_download_request(backup_id, data, fmt):
    """Validate a download request. Returns (repo, snap, None) or (None, None, error_response)."""
    if not SNAPSHOT_ID_RE.fullmatch(backup_id):
        return None, None, (jsonify({'success': False, 'error': 'Invalid snapshot id'}), 400)
    if fmt not in ('plain', 'encrypted'):
        return None, None, (jsonify({'success': False, 'error': 'format must be plain or encrypted'}), 400)
    password = data.get('password')
    user = getattr(request, 'current_user', '') or ''
    if fmt == 'plain':
        if not isinstance(password, str) or not password:
            return None, None, (jsonify({'success': False, 'error': 'Repository password is required'}), 400)
        locked = password_lockout_response(user)
        if locked:
            return None, None, locked
    repo, snap = find_snapshot(backup_id)
    if repo is None:
        return None, None, (jsonify({'success': False, 'error': 'Snapshot not found'}), 404)
    if fmt == 'plain':
        wrong = repository_password_error(repo, password, user, snap['id'])
        if wrong:
            return None, None, wrong
    snap['repository_id'] = repo.get('id')
    snap = (apply_profile_snapshot_policy([snap]) or [snap])[0]
    space_error = download_disk_space_error(snap, 2 if fmt == 'plain' else 3)
    if space_error:
        return None, None, (jsonify({'success': False, 'error': space_error}), 507)
    return repo, snap, None

@app.route('/api/backup/<backup_id>/download-plain', methods=['POST'])
@token_required
def download_plain_backup(backup_id):
    """Return the snapshot's own files as one unencrypted gzip download.

    Requires the password of the repository that encrypts the snapshot.
    """
    repo, snap, error = resolve_download_request(backup_id, request.get_json(silent=True) or {}, 'plain')
    if error:
        return error

    work = tempfile.mkdtemp(prefix='download_', dir=BACKUP_BASE)

    def cleanup():
        shutil.rmtree(work, ignore_errors=True)

    try:
        path, download_name = build_download('plain', snap, repo, work)
    except DownloadError as exc:
        cleanup()
        return jsonify({'success': False, 'error': str(exc)}), exc.status
    except Exception:
        cleanup()
        raise

    @after_this_request
    def _remove(response):
        cleanup()
        return response

    return send_file(path, as_attachment=True, download_name=download_name, mimetype='application/gzip')

# ==================== ONE-TIME DOWNLOAD LINKS ====================

def download_link_store():
    return DownloadLinkStore(BACKUP_BASE)

def cleanup_download_links():
    try:
        download_link_store().cleanup(DOWNLOAD_LINK_PREPARE_TIMEOUT)
    except Exception:
        app.logger.exception('Download link cleanup failed')

def prepare_download_link(store, state, snap, repo):
    """Background worker: restore + package, then make the link redeemable."""
    directory = store.job_dir(state['id'])
    work = str(directory / 'work')
    try:
        path, name = build_download(state['format'], snap, repo, work)
        if not directory.is_dir():
            return  # revoked while preparing
        store.mark_ready(state, path, name, DOWNLOAD_LINK_TTL_SECONDS)
    except Exception as exc:
        if not isinstance(exc, DownloadError):
            app.logger.exception('Preparing download link failed')
        try:
            store.mark_failed(state, str(exc) or exc.__class__.__name__)
        except OSError:
            pass  # revoked while preparing
    finally:
        shutil.rmtree(work, ignore_errors=True)

@app.route('/api/backup/<backup_id>/download-link', methods=['POST'])
@token_required
def create_download_link(backup_id):
    """Prepare a download in the background and return a one-time link to it.

    The link works without a dashboard session (e.g. curl on another server),
    can be redeemed exactly once, and expires DOWNLOAD_LINK_TTL_SECONDS after
    the file is ready. The token is returned only in this response.
    """
    data = request.get_json(silent=True) or {}
    fmt = data.get('format', 'plain')
    repo, snap, error = resolve_download_request(backup_id, data, fmt)
    if error:
        return error

    store = download_link_store()
    store.cleanup(DOWNLOAD_LINK_PREPARE_TIMEOUT)
    if store.active_count() >= DOWNLOAD_LINK_MAX_PREPARING:
        return jsonify({
            'success': False,
            'error': 'Other downloads are still being prepared; try again when they are ready',
        }), 429

    token, state = store.create({
        'format': fmt,
        'snapshot_id': snap['id'],
        'repository_id': repo.get('id') or 'default',
        'profile_name': snap.get('profile_name') or '',
        'created_by': getattr(request, 'current_user', '') or '',
    })
    threading.Thread(
        target=prepare_download_link,
        args=(store, state, snap, repo),
        name=f'download-{state["id"][:8]}',
        daemon=True,
    ).start()
    return jsonify({
        'success': True,
        'link': public_download_state(state),
        'url': f'/dl/{token}',
        'ttl_seconds': DOWNLOAD_LINK_TTL_SECONDS,
    }), 202

@app.route('/api/download-links/<job_id>', methods=['GET'])
@token_required
def get_download_link(job_id):
    try:
        state = download_link_store().load(job_id)
    except ValueError:
        state = None
    if state is None:
        return jsonify({'success': False, 'error': 'Download link not found'}), 404
    return jsonify({'success': True, 'link': public_download_state(state)})

@app.route('/api/download-links/<job_id>', methods=['DELETE'])
@token_required
def revoke_download_link(job_id):
    if not download_link_store().delete(job_id):
        return jsonify({'success': False, 'error': 'Download link not found'}), 404
    return jsonify({'success': True, 'message': 'Download link revoked'})

LINK_PAGE_MESSAGES = {
    'not_found': (404, 'Link not found', 'This download link does not exist or was revoked.'),
    'preparing': (409, 'Still preparing', 'The backup is still being prepared. Reload this page in a moment.'),
    'failed': (410, 'Download failed', 'The backup could not be prepared. Create a new link in the dashboard.'),
    'expired': (410, 'Link expired', 'This download link has expired. Create a new one in the dashboard.'),
    'used': (410, 'Link already used', 'One-time links can be downloaded only once. Create a new one in the dashboard.'),
}

def download_link_page(title, text, status=200, button=False):
    body = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="robots" content="noindex">'
        f'<title>{html.escape(title)} · Backupstic</title><style>'
        ':root{color-scheme:light dark;--bg:#f4f6fa;--card:#fff;--text:#132033;--muted:#5b6b7f;--line:#dfe5ee;--blue:#2563eb}'
        '@media (prefers-color-scheme:dark){:root{--bg:#0d1420;--card:#152031;--text:#e6edf6;--muted:#9aa9bb;--line:#26354a;--blue:#4f8cff}}'
        'body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--text);'
        'font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;padding:16px;box-sizing:border-box}'
        '.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:28px;max-width:440px;width:100%;box-sizing:border-box}'
        'h1{font-size:19px;margin:0 0 8px}p{margin:0 0 6px;color:var(--muted)}.file{color:var(--text);font-weight:600;word-break:break-all}'
        'button{margin-top:18px;width:100%;padding:11px;border:0;border-radius:10px;background:var(--blue);color:#fff;font:inherit;font-weight:600;cursor:pointer}'
        '</style></head><body><main class="card">'
        f'<h1>{html.escape(title)}</h1>{text}'
        + ('<form method="post"><button type="submit">Download</button></form>' if button else '')
        + '</main></body></html>'
    )
    response = app.response_class(body, status=status, mimetype='text/html')
    response.headers['Cache-Control'] = 'no-store'
    return response

@app.route('/dl/<token>', methods=['GET'])
def download_link_landing(token):
    """Landing page; does NOT consume the link.

    Chat apps and mail scanners fetch links with GET to build previews, so only
    the POST below hands out (and burns) the file.
    """
    job_id = download_job_id_for(token)
    state = download_link_store().load(job_id) if job_id else None
    if state is None:
        code = 'not_found'
    elif state.get('status') == 'ready' and (state.get('expires_at') or 0) < time.time():
        code = 'expired'
    else:
        code = {'preparing': 'preparing', 'failed': 'failed', 'claimed': 'used'}.get(state.get('status'), '')
    if code:
        status, title, text = LINK_PAGE_MESSAGES[code]
        return download_link_page(title, f'<p>{html.escape(text)}</p>', status)
    expires = datetime.datetime.fromtimestamp(state['expires_at'], datetime.timezone.utc)
    text = (
        f'<p class="file">{html.escape(state.get("filename", ""))}</p>'
        f'<p>{html.escape(format_bytes(state.get("size", 0)))}'
        f'{" · not encrypted" if state.get("format") == "plain" else " · encrypted restic bundle"}</p>'
        f'<p>This link works once and expires {html.escape(expires.strftime("%Y-%m-%d %H:%M UTC"))}.</p>'
    )
    return download_link_page('Backup download', text, button=True)

@app.route('/dl/<token>', methods=['POST'])
def redeem_download_link(token):
    job_id = download_job_id_for(token)
    store = download_link_store()
    path, state, code = store.claim(job_id) if job_id else (None, None, 'not_found')
    if code:
        status, title, text = LINK_PAGE_MESSAGES[code]
        return download_link_page(title, f'<p>{html.escape(text)}</p>', status)
    app.logger.info('Download link %s redeemed', job_id[:8])

    @after_this_request
    def _remove(response):
        # The open file keeps streaming; only the state stays, to answer "already used".
        store.discard_payload(job_id)
        return response

    return send_file(path, as_attachment=True, download_name=state['filename'], mimetype='application/gzip')

# ==================== BACKUP VERIFICATION ENDPOINTS ====================

def verification_store():
    return VerificationStore(BACKUP_BASE)

def open_bundle_repository(args, repo_path, timeout, password, wrong_password=False):
    bundle_repo = {
        'repository': repo_path,
        'password': os.urandom(16).hex() if wrong_password else password,
    }
    return run_restic_command(['--no-cache', *args], repo=bundle_repo, timeout=timeout)

def prepare_verification(snap, repo, requested_by='', trigger='manual'):
    """Enrich a snapshot for verification and build its initial job state."""
    snap['repository_id'] = repo.get('id')
    snap['size'] = snapshot_size(snap)
    snap = (apply_profile_snapshot_policy([snap]) or [snap])[0]
    return snap, new_verification_state(snap, repo, requested_by=requested_by, trigger=trigger)

def run_verification_job(store, lock_fd, state, snap, repo):
    try:
        snapshot_id = state['snapshot_id']
        cleanup_stale_work_dirs(BACKUP_BASE)
        store.prune_history()
        password = repo.get('password', RESTIC_PASSWORD)
        Verification(
            store,
            state,
            work_base=BACKUP_BASE,
            expected_size=snap.get('size', 0),
            restore=lambda target: restore_snapshot(snapshot_id, repo, target, backup_lock_held=True),
            package=lambda work, restore_dir: package_encrypted_bundle(snapshot_id, repo, work, restore_dir),
            open_repo=lambda args, path, timeout, wrong_password=False: open_bundle_repository(
                args, path, timeout, password, wrong_password=wrong_password,
            ),
            backup_lock=lambda on_wait: backup_lock(
                f'verify:{state["short_id"]}', VERIFY_LOCK_WAIT_SECONDS, on_wait=on_wait,
            ),
            postgres_tester=(
                (lambda: PostgresRestoreTester(VERIFY_POSTGRES_URL, BACKUP_BASE, timeout=VERIFY_POSTGRES_TIMEOUT))
                if VERIFY_POSTGRES_URL else None
            ),
        ).run()
    except Exception as exc:
        app.logger.exception('Backup verification crashed')
        state.update(status='failed', error=f'Unexpected error: {exc}', finished_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
        try:
            store.save(state)
        except Exception:
            pass
    finally:
        VerificationStore.release(lock_fd)

@app.route('/api/backup/<backup_id>/verify', methods=['POST'])
@token_required
def start_backup_verification(backup_id):
    """Start a background health check of one snapshot (restore -> bundle -> extract -> decrypt -> test)."""
    if not SNAPSHOT_ID_RE.fullmatch(backup_id):
        return jsonify({'success': False, 'error': 'Invalid snapshot id'}), 400
    repo, snap = find_snapshot(backup_id)
    if repo is None:
        return jsonify({'success': False, 'error': 'Snapshot not found'}), 404

    store = verification_store()
    lock_fd = store.try_acquire()
    if lock_fd is None:
        running = store.running()
        return jsonify({
            'success': False,
            'error': 'Another backup verification is already running',
            'verification': running,
        }), 409

    try:
        snap, state = prepare_verification(snap, repo, requested_by=getattr(request, 'current_user', ''))
        store.save(state)
        # Serialize before the job thread starts mutating state.
        response = jsonify({'success': True, 'verification': state})
        threading.Thread(
            target=run_verification_job,
            args=(store, lock_fd, state, snap, repo),
            name=f'verify-{state["short_id"]}',
            daemon=True,
        ).start()
    except Exception:
        VerificationStore.release(lock_fd)
        raise
    return response, 202

def process_verify_queue_once():
    """Run the oldest queued post-backup health check, if nothing else is busy.

    Returns the finished verification state, or None when there was nothing to
    do (empty queue, a backup or another check is running, snapshot gone).
    Every dashboard worker polls; the verification lock lets only one run.
    """
    queue = VerifyQueue(BACKUP_BASE)
    items = queue.pending()
    if not items:
        return None
    # A batch backs up profiles one after another; start once the batch is idle
    # so the restore step does not sit waiting for the next profile's backup.
    if app_backup_state()[0]:
        return None
    store = verification_store()
    lock_fd = store.try_acquire()
    if lock_fd is None:
        return None
    try:
        item = items[0]
        snapshot_id = item['snapshot_id']
        queue.remove(snapshot_id)
        repo = get_repository_storage().get(item.get('repository_id') or 'default')
        snap = find_snapshot_in_repository(snapshot_id, repo) if repo else None
        if snap is None:
            app.logger.warning('Skipping post-backup health check of %s: snapshot not found', snapshot_id[:8])
            VerificationStore.release(lock_fd)
            return None
        snap, state = prepare_verification(snap, repo, requested_by='after-backup', trigger='after_backup')
        store.save(state)
    except Exception:
        VerificationStore.release(lock_fd)
        raise
    app.logger.info('Running post-backup health check of %s', state['short_id'])
    run_verification_job(store, lock_fd, state, snap, repo)
    return state

def maintenance_loop():
    try:
        mark_unsynced_repositories()
    except Exception:
        app.logger.exception('Configuration snapshot startup check failed')
    while True:
        if AUTO_VERIFY_POLL_SECONDS > 0:
            try:
                process_verify_queue_once()
            except Exception:
                app.logger.exception('Post-backup health check queue failed')
        try:
            sync_dirty_configs()
        except Exception:
            app.logger.exception('Configuration snapshot sync failed')
        cleanup_download_links()
        time.sleep(MAINTENANCE_INTERVAL_SECONDS)

_auto_verify_started = False
_auto_verify_start_lock = threading.Lock()

@app.before_request
def ensure_auto_verify_worker():
    """Start the housekeeping thread in each worker process on its first request.

    Started lazily (not at import) so it lives in the gunicorn worker rather
    than a pre-fork parent; the container health check makes the first request.
    """
    global _auto_verify_started
    if _auto_verify_started or app.testing:
        return
    with _auto_verify_start_lock:
        if _auto_verify_started:
            return
        _auto_verify_started = True
        threading.Thread(target=maintenance_loop, name='maintenance', daemon=True).start()

@app.route('/api/backup/<backup_id>/verify', methods=['GET'])
@token_required
def get_backup_verification(backup_id):
    """Latest verification result for a snapshot (full or short id)."""
    state = verification_store().find(backup_id)
    if state is None:
        return jsonify({'success': False, 'error': 'No verification found for this backup'}), 404
    return jsonify({'success': True, 'verification': state})

@app.route('/api/verifications')
@token_required
def list_backup_verifications():
    """Latest verification summary for every snapshot that has been checked."""
    states = verification_store().all()
    verifications = {s['snapshot_id']: summarize_verification(s) for s in states}
    # Snapshots waiting for their automatic post-backup check.
    for item in VerifyQueue(BACKUP_BASE).pending():
        sid = item['snapshot_id']
        existing = verifications.get(sid)
        if existing and str(existing.get('started_at') or '') >= str(item.get('queued_at') or ''):
            continue
        verifications[sid] = {
            'snapshot_id': sid,
            'short_id': sid[:8],
            'status': 'scheduled',
            'trigger': 'after_backup',
            'started_at': item.get('queued_at'),
            'finished_at': None,
            'summary': {},
        }
    return jsonify({'success': True, 'verifications': verifications})

# ==================== CONFIGURATION BACKUP (DISASTER RECOVERY) ====================

def is_config_snapshot(snap):
    return config_backup.CONFIG_TAG in (snap.get('tags') or [])

def config_sync_state():
    return config_backup.ConfigSyncState(BACKUP_BASE)

def mark_config_dirty(*repo_ids):
    if not CONFIG_SNAPSHOT_ENABLED:
        return
    state = config_sync_state()
    for repo_id in {config_backup.repo_key(r) for r in repo_ids}:
        state.mark_dirty(repo_id)

def mark_unsynced_repositories():
    """After an upgrade (or on a fresh volume) save every used repository's config once."""
    if not CONFIG_SNAPSHOT_ENABLED:
        return
    state = config_sync_state()
    used = {config_backup.repo_key(p.get('repository_id')) for p in get_storage().get_all()}
    for repo_id in used:
        if state.last(repo_id) is None:
            state.mark_dirty(repo_id)

def save_repository_config(repo, force=False):
    return config_backup.sync_repository(
        repo,
        lambda: get_storage().get_all(),
        config_sync_state(),
        restic_env_for_repository(repo),
        force=force,
        keep=CONFIG_SNAPSHOT_KEEP,
    )

def sync_dirty_configs():
    """Push configuration snapshots for repositories whose profiles changed."""
    if not CONFIG_SNAPSHOT_ENABLED:
        return []
    state = config_sync_state()
    done = []
    for repo_id in state.dirty():
        # Stay out of the way of backups and prunes; retried next tick.
        if app_backup_state()[0]:
            break
        repo = get_repository_storage().get(repo_id)
        if repo is None:
            state.forget(repo_id)
            continue
        try:
            done.append((repo_id, save_repository_config(repo)))
        except config_backup.ConfigBackupError as exc:
            app.logger.warning('Configuration snapshot for repository %s failed: %s', repo_id, exc)
    return done

def config_backup_error(exc, status=400):
    return jsonify({'success': False, 'error': str(exc)}), status

@app.route('/api/restic-repositories/<repo_id>/config-backups', methods=['GET'])
@token_required
def list_config_backups(repo_id):
    """Saved configurations in a repository, plus a restore preview of one of them."""
    repo = get_repository_storage().get(repo_id)
    if repo is None:
        return jsonify({'success': False, 'error': 'Repository not found'}), 404
    env = restic_env_for_repository(repo)
    try:
        snapshots = config_backup.list_config_snapshots(env)
    except config_backup.ConfigBackupError as exc:
        return config_backup_error(exc)
    response = {'success': True, 'snapshots': snapshots, 'selected': None, 'profiles': []}
    if not snapshots:
        return jsonify(response)

    wanted = request.args.get('snapshot_id') or snapshots[0]['id']
    selected = next((s for s in snapshots if s['id'] == wanted or s['id'].startswith(wanted)), None)
    if selected is None:
        return jsonify({'success': False, 'error': 'Configuration backup not found'}), 404
    try:
        document = config_backup.read_document(env, selected['id'])
    except config_backup.ConfigBackupError as exc:
        return config_backup_error(exc)
    counts = config_backup.snapshot_counts(env)
    response['selected'] = {
        **selected,
        'exported_at': document.get('exported_at'),
        'repository_name': (document.get('repository') or {}).get('name', ''),
    }
    response['profiles'] = config_backup.plan_import(
        document, get_storage().get_all(), repo.get('id'), validate_profile, counts,
    )
    return jsonify(response)

@app.route('/api/restic-repositories/<repo_id>/config-backups', methods=['POST'])
@token_required
def save_config_backup(repo_id):
    """Save this repository's profiles into it now."""
    repo = get_repository_storage().get(repo_id)
    if repo is None:
        return jsonify({'success': False, 'error': 'Repository not found'}), 404
    try:
        result = save_repository_config(repo, force=True)
    except config_backup.ConfigBackupError as exc:
        return config_backup_error(exc)
    return jsonify({'success': True, 'result': result, 'config_backup': config_sync_state().last(repo_id)})

@app.route('/api/restic-repositories/<repo_id>/config-backups/restore', methods=['POST'])
@token_required
def restore_config_backup(repo_id):
    """Recreate profiles saved in this repository, keeping their ids.

    Kept ids make the existing snapshots, retention and health history line
    up with the restored profiles again.
    """
    repo = get_repository_storage().get(repo_id)
    if repo is None:
        return jsonify({'success': False, 'error': 'Repository not found'}), 404
    data = request.get_json(silent=True) or {}
    snapshot_id = data.get('snapshot_id')
    profile_ids = data.get('profile_ids')
    if profile_ids is not None and (not isinstance(profile_ids, list) or not all(isinstance(p, str) for p in profile_ids)):
        return jsonify({'success': False, 'error': 'profile_ids must be a list of ids'}), 400
    env = restic_env_for_repository(repo)
    try:
        if not snapshot_id:
            snapshots = config_backup.list_config_snapshots(env)
            if not snapshots:
                return jsonify({'success': False, 'error': 'This repository has no configuration backup'}), 404
            snapshot_id = snapshots[0]['id']
        document = config_backup.read_document(env, str(snapshot_id))
    except config_backup.ConfigBackupError as exc:
        return config_backup_error(exc)

    results = config_backup.apply_import(
        document, get_storage(), repo.get('id'), validate_profile,
        selected_ids=profile_ids, overwrite=bool(data.get('overwrite')),
    )
    changed = [r for r in results if r['result'] in ('restored', 'replaced')]
    if changed:
        sync_profile_crontab()
        mark_config_dirty(repo.get('id'))
    return jsonify({
        'success': True,
        'restored': sum(1 for r in results if r['result'] == 'restored'),
        'replaced': sum(1 for r in results if r['result'] == 'replaced'),
        'skipped': sum(1 for r in results if r['result'] == 'skipped'),
        'failed': sum(1 for r in results if r['result'] == 'failed'),
        'results': results,
    })

# ==================== LOGS AND SCHEDULING ENDPOINTS ====================

@app.route('/api/logs')
@token_required
def get_logs():
    """Get recent backup logs"""
    log_file = f"{BACKUP_BASE}/cron.log"
    if os.path.exists(log_file):
        with open(log_file, encoding='utf-8', errors='replace') as handle:
            logs = ''.join(deque(handle, maxlen=100))
        return jsonify({'success': True, 'logs': logs})
    return jsonify({'success': True, 'logs': 'No logs found'})

@app.route('/api/schedule')
@token_required
def get_schedule():
    """Get current backup schedule"""
    schedule_file = os.path.join(BACKUP_BASE, 'schedules.cron')
    try:
        with open(schedule_file, encoding='utf-8') as handle:
            schedule = ''.join(line for line in handle if 'backup' in line)
    except FileNotFoundError:
        schedule = ''
    return jsonify({'success': True, 'schedule': schedule})

@app.route('/api/schedule', methods=['POST'])
@token_required
def update_schedule():
    """Update the legacy schedule when profile scheduling is not in use."""
    data = request.get_json() or {}
    cron_expr = data.get('cron', '')
    mode = data.get('mode', 'full')

    if not cron_expr:
        return jsonify({'success': False, 'error': 'Cron expression required'}), 400

    if not valid_cron_expression(cron_expr):
        return jsonify({'success': False, 'error': 'Invalid cron expression'}), 400
    if mode not in ['full', 'db', 'files', 'postgres', 'redis', 'mongo', 'elasticsearch']:
        return jsonify({'success': False, 'error': 'Invalid backup mode'}), 400

    if get_storage().get_all():
        return jsonify({
            'success': False,
            'error': 'Global legacy schedules cannot be combined with profile schedules'
        }), 409

    # Build crontab entry for legacy mode (kept for compatibility)
    entry = f"{cron_expr} {SCRIPTS_DIR}/backup-all.sh {mode} >> {BACKUP_BASE}/cron.log 2>&1"

    write_schedule_file(
        os.path.join(BACKUP_BASE, 'schedules.cron'),
        ['# Managed by Backupstic; manual edits may be replaced.', entry],
    )
    return jsonify({'success': True, 'message': 'Schedule updated'})

@app.route('/api/profile-schedule', methods=['POST'])
@token_required
def update_profile_schedule():
    """Update schedule for a specific profile"""
    data = request.get_json() or {}
    profile_id = data.get('profile_id')
    cron_expr = data.get('cron', '')

    if not profile_id or not cron_expr:
        return jsonify({'success': False, 'error': 'Profile ID and cron expression required'}), 400

    # Validate cron expression (basic check)
    if not valid_cron_expression(cron_expr):
        return jsonify({'success': False, 'error': 'Invalid cron expression'}), 400

    storage = get_storage()
    profile = storage.get(profile_id)
    if profile is None:
        return jsonify({'success': False, 'error': 'Profile not found'}), 404

    # Update profile schedule
    updated_profile = storage.update(profile_id, {
        'schedule': cron_expr,
        'updated_at': datetime.datetime.now(datetime.timezone.utc).isoformat()
    })

    if updated_profile is None:
        return jsonify({'success': False, 'error': 'Failed to update profile'}), 500

    sync_profile_crontab()
    mark_config_dirty(updated_profile.get('repository_id'))
    return jsonify({'success': True, 'message': 'Profile schedule updated'})

@app.route('/api/retention')
@token_required
def get_retention():
    """Get retention policy from config"""
    # Try well-known paths for the env file (in order of preference)
    env_file_candidates = [
        "/etc/backup.env",
        f"{BACKUP_BASE}/../configs/backup.env",
        f"{BACKUP_BASE}/../backup/configs/backup.env",
    ]
    retention = {}
    for env_file in env_file_candidates:
        expanded = os.path.abspath(env_file)
        if os.path.exists(expanded):
            with open(expanded) as f:
                for line in f:
                    if 'RETENTION' in line and '=' in line:
                        key, val = line.strip().split('=', 1)
                        retention[key] = val
            break
    return jsonify({'success': True, 'retention': retention})

@app.route('/api/status')
@token_required
def get_status():
    """Aggregate dashboard status: snapshot count, last backup time, disk usage."""
    # --- Snapshots (from restic repositories, matching dashboard policy) ---
    snapshot_count = 0
    last_backup = None
    snapshots = []
    seen_repositories = set()
    seen_snapshots = set()
    for repo in all_repositories():
        repo_key = repo.get('repository') or repo.get('id') or 'default'
        if repo_key in seen_repositories:
            continue
        seen_repositories.add(repo_key)
        result = run_restic_command_with_stale_lock_retry(['snapshots', '--json'], repo=repo, timeout=60)
        if not result['success'] or not result['stdout']:
            continue
        try:
            repo_snapshots = json.loads(result['stdout'])
        except (ValueError, TypeError):
            continue
        for snap in repo_snapshots:
            snap_key = (repo_key, snap.get('id'))
            if snap_key in seen_snapshots or is_config_snapshot(snap):
                continue
            seen_snapshots.add(snap_key)
            snap['repository_id'] = repo.get('id')
            snap['repository_name'] = repo.get('name')
            snapshots.append(snap)

    snapshots = apply_profile_snapshot_policy(snapshots, active_profiles_only=True, respect_retention=True)
    snapshot_count = len(snapshots)
    times = [s.get('time') for s in snapshots if s.get('time')]
    if times:
        last_backup = max(times)

    # --- Disk usage of the backup volume ---
    disk = {'used': '-', 'available': '-', 'total': '-', 'percent': '0'}
    try:
        target = BACKUP_BASE if os.path.isdir(BACKUP_BASE) else '/'
        usage = shutil.disk_usage(target)
        percent = int(round(usage.used / usage.total * 100)) if usage.total else 0
        disk = {
            'used': format_bytes(usage.used),
            'available': format_bytes(usage.free),
            'total': format_bytes(usage.total),
            'percent': str(percent),
        }
    except Exception:
        pass

    return jsonify({
        'success': True,
        'snapshot_count': snapshot_count,
        'last_backup': last_backup,
        'disk': disk,
    })

@app.route('/api/health')
def health():
    """Health check endpoint"""
    return jsonify({'status': 'healthy', 'timestamp': datetime.datetime.now().isoformat()})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
