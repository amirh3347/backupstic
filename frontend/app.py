#!/usr/bin/env python3
"""Backup Dashboard API - Enhanced Flask backend with profiles and authentication"""

import os
import json
import subprocess
import datetime
import fcntl
import uuid
import shutil
import tempfile
import re
from collections import deque
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
SSH_HOST_KEY_CHECKING = os.environ.get(
    'CONFIG_BACKUP_SSH_HOST_KEY_CHECKING',
    'accept-new',
).strip().lower()
if SSH_HOST_KEY_CHECKING not in {'yes', 'accept-new', 'no'}:
    raise RuntimeError(
        'CONFIG_BACKUP_SSH_HOST_KEY_CHECKING must be yes, accept-new, or no'
    )

# Initialize auth
init_auth(app)

def start_background(args):
    """Start a detached backup process and append its output to the shared log."""
    os.makedirs(BACKUP_BASE, exist_ok=True)
    log_path = os.path.join(BACKUP_BASE, 'manual_backup.log')
    log_handle = open(log_path, 'a', encoding='utf-8')
    try:
        subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
            env=os.environ.copy(),
        )
    finally:
        log_handle.close()


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
    if request.path.startswith('/api/'):
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

def run_restic_command_with_stale_lock_retry(args, repo=None, timeout=300):
    result = run_restic_command(args, repo=repo, timeout=timeout)
    if result['success'] or not restic_lock_error(result):
        return result

    running, _ = app_backup_state()
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

def find_snapshot_repository(snapshot_id):
    for repo in all_repositories():
        res = run_restic_command_with_stale_lock_retry(['snapshots', '--json'], repo=repo, timeout=60)
        if not res['success'] or not res['stdout']:
            continue
        try:
            snapshots = json.loads(res['stdout'])
        except (ValueError, TypeError):
            continue
        for snap in snapshots:
            sid = snap.get('id', '')
            if sid == snapshot_id or sid.startswith(snapshot_id):
                return repo
    return None

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
    return jsonify({'success': True, 'repositories': [sanitize_repository(r) for r in repos]})

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
            if snap_key in seen_snapshots:
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
    repo = find_snapshot_repository(backup_id)
    if repo is None:
        return jsonify({'success': False, 'error': 'Snapshot not found'}), 404
    result = run_restic_command_with_stale_lock_retry(['forget', backup_id, '--prune'], repo=repo, timeout=600)
    if result['success']:
        return jsonify({'success': True, 'message': 'Backup deleted successfully'})
    return jsonify({'success': False, 'error': result['stderr']})

@app.route('/api/backup/<backup_id>/download')
@token_required
def download_backup(backup_id):
    """Return an encrypted restic repository bundle for the requested snapshot."""
    repo = find_snapshot_repository(backup_id)
    if repo is None:
        return jsonify({'success': False, 'error': 'Snapshot not found'}), 404

    work = tempfile.mkdtemp(prefix='restore_', dir=BACKUP_BASE)
    restore_dir = os.path.join(work, 'data')
    encrypted_repo = os.path.join(work, 'encrypted-restic-repo')
    readme = os.path.join(work, 'README_DECRYPT.txt')
    archive = os.path.join(work, f'backup_{backup_id[:8]}.restic.tar.gz')

    def cleanup():
        shutil.rmtree(work, ignore_errors=True)

    res = run_restic_command_with_stale_lock_retry(['restore', backup_id, '--target', restore_dir], repo=repo, timeout=1800)
    if not res['success']:
        cleanup()
        return jsonify({'success': False, 'error': res['stderr'] or 'Restore failed'}), 500

    export_repo = {
        'repository': encrypted_repo,
        'password': repo.get('password', RESTIC_PASSWORD),
    }
    init_res = run_restic_command(['init'], repo=export_repo, timeout=120)
    if not init_res['success']:
        cleanup()
        return jsonify({'success': False, 'error': init_res['stderr'] or 'Failed to initialize encrypted export'}), 500

    backup_res = run_restic_command(
        ['backup', restore_dir, '--tag', 'encrypted-download', '--tag', backup_id[:8]],
        repo=export_repo,
        timeout=1800,
    )
    if not backup_res['success']:
        cleanup()
        return jsonify({'success': False, 'error': backup_res['stderr'] or 'Failed to build encrypted export'}), 500

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
        cleanup()
        return jsonify({'success': False, 'error': tar_result.stderr or 'Failed to package encrypted export'}), 500

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

# ==================== LOGS AND SCHEDULING ENDPOINTS ====================

def valid_cron_expression(expression):
    """Accept numeric five-field cron expressions without control syntax."""
    fields = expression.split()
    return len(fields) == 5 and all(
        re.fullmatch(r'[0-9*/,\-]+', field) for field in fields
    )


def _write_schedule_file(schedule_file, lines):
    """Atomically publish the crontab consumed by the cron container."""
    os.makedirs(os.path.dirname(schedule_file), exist_ok=True)
    temp_path = f'{schedule_file}.{uuid.uuid4().hex}.tmp'
    descriptor = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            handle.write('\n'.join(lines).rstrip() + '\n')
        os.replace(temp_path, schedule_file)
    except Exception:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass
        raise

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
    """Update backup schedule"""
    data = request.get_json() or {}
    cron_expr = data.get('cron', '')
    mode = data.get('mode', 'full')

    if not cron_expr:
        return jsonify({'success': False, 'error': 'Cron expression required'}), 400

    if not valid_cron_expression(cron_expr):
        return jsonify({'success': False, 'error': 'Invalid cron expression'}), 400
    if mode not in ['full', 'db', 'files', 'postgres', 'redis', 'mongo', 'elasticsearch']:
        return jsonify({'success': False, 'error': 'Invalid backup mode'}), 400

    # Build crontab entry for legacy mode (kept for compatibility)
    entry = f"{cron_expr} {SCRIPTS_DIR}/backup-all.sh {mode} >> {BACKUP_BASE}/cron.log 2>&1"

    schedule_file = os.path.join(BACKUP_BASE, 'schedules.cron')
    try:
        with open(schedule_file, encoding='utf-8') as handle:
            lines = handle.read().splitlines()
    except FileNotFoundError:
        lines = []
    lines = [l for l in lines if 'backup-all.sh' not in l and l.strip()]
    lines.append(entry)
    _write_schedule_file(schedule_file, lines)
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

    # Also update the system cron job for this profile
    # Remove existing cron entries for this profile
    schedule_file = os.path.join(BACKUP_BASE, 'schedules.cron')
    try:
        with open(schedule_file, encoding='utf-8') as handle:
            lines = handle.read().splitlines()
    except FileNotFoundError:
        lines = []
    lines = [l for l in lines if f'backup-profile.sh {profile_id}' not in l and l.strip()]

    # Add new cron entry
    entry = f"{cron_expr} {SCRIPTS_DIR}/backup-profile.sh {profile_id} >> {BACKUP_BASE}/cron.log 2>&1"
    lines.append(entry)

    _write_schedule_file(schedule_file, lines)
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
            if snap_key in seen_snapshots:
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
