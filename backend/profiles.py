#!/usr/bin/env python3
"""Backup Profiles Data Model and Storage"""

import json
import os
import re
import base64
import hashlib
import uuid
import fcntl
from contextlib import contextmanager
from datetime import datetime
from pathlib import PurePosixPath, Path
from typing import Any, Dict, List, Optional
import threading
from cryptography.fernet import Fernet, InvalidToken


ENCRYPTED_PREFIX = "enc:v1:"
PROFILE_SECRET_FIELDS = {"password", "ssh_password"}
REPOSITORY_SECRET_FIELDS = {"password", "aws_access_key_id", "aws_secret_access_key"}


def _fernet() -> Fernet:
    secret = (
        os.environ.get("PROFILE_SECRET_KEY")
        or os.environ.get("JWT_SECRET_KEY")
        or os.environ.get("RESTIC_PASSWORD")
    )
    if not secret or len(secret) < 32:
        raise RuntimeError(
            "PROFILE_SECRET_KEY, JWT_SECRET_KEY, or RESTIC_PASSWORD must contain at least 32 characters"
        )
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    return Fernet(key)


def encrypt_value(value: Any) -> Any:
    if not isinstance(value, str) or not value or value.startswith(ENCRYPTED_PREFIX):
        return value
    return ENCRYPTED_PREFIX + _fernet().encrypt(value.encode()).decode()


def decrypt_value(value: Any) -> Any:
    if not isinstance(value, str) or not value.startswith(ENCRYPTED_PREFIX):
        return value
    token = value[len(ENCRYPTED_PREFIX):].encode()
    try:
        return _fernet().decrypt(token).decode()
    except InvalidToken:
        return ""


def encrypt_secrets(data: Dict[str, Any], fields: set[str]) -> Dict[str, Any]:
    item = dict(data)
    for field in fields:
        if field in item:
            item[field] = encrypt_value(item[field])
    return item


def decrypt_secrets(data: Dict[str, Any], fields: set[str]) -> Dict[str, Any]:
    item = dict(data)
    for field in fields:
        if field in item:
            item[field] = decrypt_value(item[field])
    return item


def sanitize_secrets(data: Dict[str, Any], fields: set[str]) -> Dict[str, Any]:
    item = dict(data)
    for field in fields:
        item.pop(field, None)
    return item


def contains_unencrypted_secret(data: Dict[str, Any], fields: set[str]) -> bool:
    for field in fields:
        value = data.get(field)
        if isinstance(value, str) and value and not value.startswith(ENCRYPTED_PREFIX):
            return True
    return False


@contextmanager
def _file_lock(lock_path: Path, exclusive: bool = False):
    with open(lock_path, "a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


class ProfileStorage:
    """Thread-safe JSON file storage for backup profiles"""

    def __init__(self, storage_path: str = "/var/backups/profiles.json"):
        self.storage_path = Path(storage_path)
        self.lock_path = self.storage_path.with_suffix(self.storage_path.suffix + ".lock")
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with _file_lock(self.lock_path, exclusive=True):
            self._ensure_storage_exists()
            self._migrate_plaintext_secrets()

    def _ensure_storage_exists(self):
        if not self.storage_path.exists():
            self._write({"profiles": {}, "version": 1})

    def _read(self) -> Dict[str, Any]:
        with self._lock:
            with open(self.storage_path, "r") as f:
                return json.load(f)

    def _write(self, data: Dict[str, Any]):
        with self._lock:
            tmp_path = self.storage_path.with_suffix(".tmp")
            descriptor = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "w") as f:
                json.dump(data, f, indent=2, default=str)
                f.flush()
                os.fsync(f.fileno())
            tmp_path.replace(self.storage_path)

    def _migrate_plaintext_secrets(self):
        data = self._read()
        changed = False
        for profile_id, profile in data.get("profiles", {}).items():
            if contains_unencrypted_secret(profile, PROFILE_SECRET_FIELDS):
                data["profiles"][profile_id] = encrypt_secrets(profile, PROFILE_SECRET_FIELDS)
                changed = True
        if changed:
            self._write(data)

    def get_all(self) -> List[Dict[str, Any]]:
        with _file_lock(self.lock_path):
            data = self._read()
        return [decrypt_secrets(profile, PROFILE_SECRET_FIELDS) for profile in data.get("profiles", {}).values()]

    def get(self, profile_id: str) -> Optional[Dict[str, Any]]:
        with _file_lock(self.lock_path):
            data = self._read()
        profile = data.get("profiles", {}).get(profile_id)
        if profile is None:
            return None
        return decrypt_secrets(profile, PROFILE_SECRET_FIELDS)

    def create(self, profile: Dict[str, Any]) -> Dict[str, Any]:
        profile = dict(profile)
        profile_id = str(uuid.uuid4())
        now = datetime.now().astimezone().isoformat()
        profile["id"] = profile_id
        profile["created_at"] = now
        profile["updated_at"] = now
        with _file_lock(self.lock_path, exclusive=True):
            data = self._read()
            data["profiles"][profile_id] = encrypt_secrets(profile, PROFILE_SECRET_FIELDS)
            self._write(data)
        return profile

    def update(self, profile_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        with _file_lock(self.lock_path, exclusive=True):
            data = self._read()
            if profile_id not in data.get("profiles", {}):
                return None
            profile = data["profiles"][profile_id]
            profile = decrypt_secrets(profile, PROFILE_SECRET_FIELDS)
            profile.update(updates)
            profile["updated_at"] = datetime.now().astimezone().isoformat()
            data["profiles"][profile_id] = encrypt_secrets(profile, PROFILE_SECRET_FIELDS)
            self._write(data)
        return profile

    def delete(self, profile_id: str) -> bool:
        with _file_lock(self.lock_path, exclusive=True):
            data = self._read()
            if profile_id not in data.get("profiles", {}):
                return False
            del data["profiles"][profile_id]
            self._write(data)
        return True


# Profile validation schemas
PROFILE_TYPES = {
    "postgresql": {
        "required": ["name", "type", "host", "port", "username", "password", "databases", "schedule"],
        "optional": ["description", "enabled", "max_backups", "repository_id"]
    },
    "redis": {
        "required": ["name", "type", "host", "port", "schedule"],
        "optional": ["description", "enabled", "databases", "password", "max_backups", "repository_id"]
    },
    "mongodb": {
        "required": ["name", "type", "host", "port", "databases", "schedule"],
        "optional": ["description", "enabled", "username", "password", "max_backups", "repository_id"]
    },
    "ssh_files": {
        "required": ["name", "type", "ssh_host", "ssh_port", "ssh_user", "schedule"],
        "optional": ["description", "enabled", "auth_method", "ssh_password", "paths", "exclude_patterns", "path_configs", "preserve_metadata", "log_rsync_output", "max_backups", "repository_id"]
    }
}

DEFAULT_PROFILE = {
    "name": "",
    "type": "postgresql",
    "description": "",
    "enabled": True,
    "schedule": "0 2 * * *",
}


def validate_profile(profile: Dict[str, Any]) -> tuple[bool, str]:
    """Validate profile data. Returns (is_valid, error_message)"""
    profile_type = profile.get("type")
    if profile_type not in PROFILE_TYPES:
        return False, f"Invalid profile type: {profile_type}"

    schema = PROFILE_TYPES[profile_type]
    allowed_fields = set(schema["required"] + schema["optional"] + ["id", "created_at", "updated_at"])
    unknown_fields = sorted(set(profile) - allowed_fields)
    if unknown_fields:
        return False, f"Unsupported fields: {', '.join(unknown_fields)}"
    name = str(profile.get("name", "")).strip()
    if not name or len(name) > 100 or any(char in name for char in "\r\n"):
        return False, "Profile name must be 1-100 characters on one line"
    profile["name"] = name
    for field in schema["required"]:
        if field not in profile or not profile[field]:
            return False, f"Missing required field: {field}"

    # Validate schedule (cron expression basic check)
    schedule = str(profile.get("schedule", "")).strip()
    if len(schedule.split()) != 5 or not all(
        re.fullmatch(r"[0-9*/,\-]+", field) for field in schedule.split()
    ):
        return False, "Invalid numeric five-field cron expression"
    profile["schedule"] = schedule

    try:
        max_backups = int(profile.get("max_backups", 0) or 0)
    except (TypeError, ValueError):
        return False, "max_backups must be a number"
    if max_backups < 0:
        return False, "max_backups cannot be negative"
    profile["max_backups"] = max_backups

    # Type-specific validations
    if profile_type == "postgresql":
        if not isinstance(profile.get("databases"), list) or len(profile["databases"]) == 0:
            return False, "PostgreSQL profile requires at least one database"
        if not all(re.fullmatch(r"[A-Za-z0-9_.-]+|all", str(db)) for db in profile["databases"]):
            return False, "PostgreSQL database names contain invalid characters"
    elif profile_type == "mongodb":
        if not isinstance(profile.get("databases"), list) or len(profile["databases"]) == 0:
            return False, "MongoDB profile requires at least one database"
        if not all(re.fullmatch(r"[A-Za-z0-9_.-]+|all", str(db)) for db in profile["databases"]):
            return False, "MongoDB database names contain invalid characters"
    elif profile_type == "ssh_files":
        ok, error = validate_ssh_files_profile(profile)
        if not ok:
            return False, error

    if profile_type in {"postgresql", "mongodb", "redis"}:
        host = str(profile.get("host", "")).strip()
        if not host or len(host) > 253 or any(char in host for char in "\r\n"):
            return False, "Host must be 1-253 characters on one line"
        try:
            port = int(profile.get("port"))
        except (TypeError, ValueError):
            return False, "Port must be a number"
        if port < 1 or port > 65535:
            return False, "Port must be between 1 and 65535"
        profile["host"] = host
        profile["port"] = port

    return True, ""


def validate_ssh_files_profile(profile: Dict[str, Any]) -> tuple[bool, str]:
    """Normalize and validate Configuration Backup profile fields."""
    host = str(profile.get("ssh_host", "")).strip()
    user = str(profile.get("ssh_user", "")).strip()
    if not host:
        return False, "Configuration Backup profile requires SSH host"
    if not re.fullmatch(r"[A-Za-z0-9_.:-]+", host):
        return False, "SSH host contains invalid characters"
    if not user:
        return False, "Configuration Backup profile requires SSH username"
    if not re.fullmatch(r"[A-Za-z0-9_.@-]+", user):
        return False, "SSH username contains invalid characters"

    try:
        port = int(profile.get("ssh_port", 22))
    except (TypeError, ValueError):
        return False, "SSH port must be a number"
    if port < 1 or port > 65535:
        return False, "SSH port must be between 1 and 65535"
    profile["ssh_port"] = port

    auth_method = profile.get("auth_method") or ("password" if profile.get("ssh_password") else "key")
    if auth_method not in ("password", "key"):
        return False, "Authentication method must be password or key"
    if auth_method == "password" and not profile.get("ssh_password"):
        return False, "SSH password is required when password authentication is selected"
    profile["auth_method"] = auth_method

    path_configs = normalize_path_configs(profile)
    if not path_configs:
        return False, "Configuration Backup profile requires at least one path"
    for entry in path_configs:
        path = entry["path"]
        if not is_valid_absolute_path(path):
            return False, f"Invalid absolute path: {path}"
        for pattern in entry.get("exclude_patterns", []):
            if not is_valid_exclude_pattern(pattern):
                return False, f"Invalid exclude pattern for {path}: {pattern}"

    profile["path_configs"] = path_configs
    profile["paths"] = [entry["path"] for entry in path_configs]
    profile["exclude_patterns"] = [
        pattern
        for pattern in profile.get("exclude_patterns", [])
        if isinstance(pattern, str) and pattern.strip()
    ]
    profile["preserve_metadata"] = bool(profile.get("preserve_metadata", False))
    profile["log_rsync_output"] = bool(profile.get("log_rsync_output", False))
    return True, ""


def normalize_path_configs(profile: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Accept both the new per-path form and the previous flat paths form."""
    configs = profile.get("path_configs")
    if isinstance(configs, list) and configs:
        normalized = []
        for item in configs:
            if isinstance(item, str):
                path = item.strip()
                excludes = []
            elif isinstance(item, dict):
                path = str(item.get("path", "")).strip()
                excludes = item.get("exclude_patterns", [])
            else:
                continue
            normalized.append({
                "path": path,
                "exclude_patterns": normalize_patterns(excludes),
            })
        return normalized

    global_excludes = normalize_patterns(profile.get("exclude_patterns", []))
    paths = profile.get("paths", [])
    if not isinstance(paths, list):
        return []
    return [
        {"path": str(path).strip(), "exclude_patterns": list(global_excludes)}
        for path in paths
        if isinstance(path, str) and path.strip()
    ]


def normalize_patterns(patterns: Any) -> List[str]:
    if not isinstance(patterns, list):
        return []
    return [str(pattern).strip() for pattern in patterns if isinstance(pattern, str) and pattern.strip()]


def is_valid_absolute_path(path: str) -> bool:
    if not path or not path.startswith("/") or "\x00" in path or "\n" in path or "\r" in path:
        return False
    return PurePosixPath(path).is_absolute()


def is_valid_exclude_pattern(pattern: str) -> bool:
    return bool(pattern) and "\x00" not in pattern and "\n" not in pattern and "\r" not in pattern


# Global storage instance
_storage: Optional[ProfileStorage] = None


def get_storage() -> ProfileStorage:
    global _storage
    if _storage is None:
        storage_path = os.environ.get("PROFILES_STORAGE", "/var/backups/profiles.json")
        _storage = ProfileStorage(storage_path)
    return _storage


class ResticRepositoryStorage:
    """Thread-safe JSON storage for Restic repositories."""

    def __init__(self, storage_path: str = "/var/backups/restic_repositories.json"):
        self.storage_path = Path(storage_path)
        self.lock_path = self.storage_path.with_suffix(self.storage_path.suffix + ".lock")
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with _file_lock(self.lock_path, exclusive=True):
            self._ensure_storage_exists()
            self._migrate_plaintext_secrets()

    def _default_repository(self) -> Dict[str, Any]:
        return {
            "id": "default",
            "name": "Default Local Repository",
            "type": "local",
            "repository": os.environ.get("RESTIC_REPO", "/var/backups/restic-repo"),
            "password": os.environ.get("RESTIC_PASSWORD", ""),
            "created_at": datetime.now().astimezone().isoformat(),
            "updated_at": datetime.now().astimezone().isoformat(),
        }

    def _ensure_storage_exists(self):
        if not self.storage_path.exists():
            default = self._default_repository()
            self._write({"repositories": {"default": encrypt_secrets(default, REPOSITORY_SECRET_FIELDS)}, "version": 1})

    def _read(self) -> Dict[str, Any]:
        with self._lock:
            with open(self.storage_path, "r") as f:
                return json.load(f)

    def _write(self, data: Dict[str, Any]):
        with self._lock:
            tmp_path = self.storage_path.with_suffix(".tmp")
            descriptor = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "w") as f:
                json.dump(data, f, indent=2, default=str)
                f.flush()
                os.fsync(f.fileno())
            tmp_path.replace(self.storage_path)

    def _migrate_plaintext_secrets(self):
        data = self._read()
        changed = False
        for repo_id, repo in data.get("repositories", {}).items():
            if contains_unencrypted_secret(repo, REPOSITORY_SECRET_FIELDS):
                data["repositories"][repo_id] = encrypt_secrets(repo, REPOSITORY_SECRET_FIELDS)
                changed = True
        if changed:
            self._write(data)

    def get_all(self) -> List[Dict[str, Any]]:
        with _file_lock(self.lock_path):
            data = self._read()
        return [decrypt_secrets(repo, REPOSITORY_SECRET_FIELDS) for repo in data.get("repositories", {}).values()]

    def get(self, repo_id: str) -> Optional[Dict[str, Any]]:
        with _file_lock(self.lock_path):
            data = self._read()
        repo = data.get("repositories", {}).get(repo_id)
        if repo is None:
            return None
        return decrypt_secrets(repo, REPOSITORY_SECRET_FIELDS)

    def create(self, repo: Dict[str, Any]) -> Dict[str, Any]:
        repo = dict(repo)
        repo_id = repo.get("id") or str(uuid.uuid4())
        now = datetime.now().astimezone().isoformat()
        repo["id"] = repo_id
        repo["created_at"] = now
        repo["updated_at"] = now
        with _file_lock(self.lock_path, exclusive=True):
            data = self._read()
            data.setdefault("repositories", {})[repo_id] = encrypt_secrets(repo, REPOSITORY_SECRET_FIELDS)
            self._write(data)
        return repo

    def update(self, repo_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        with _file_lock(self.lock_path, exclusive=True):
            data = self._read()
            if repo_id not in data.get("repositories", {}):
                return None
            repo = decrypt_secrets(data["repositories"][repo_id], REPOSITORY_SECRET_FIELDS)
            repo.update(updates)
            repo["id"] = repo_id
            repo["updated_at"] = datetime.now().astimezone().isoformat()
            data["repositories"][repo_id] = encrypt_secrets(repo, REPOSITORY_SECRET_FIELDS)
            self._write(data)
        return repo

    def delete(self, repo_id: str) -> bool:
        if repo_id == "default":
            return False
        with _file_lock(self.lock_path, exclusive=True):
            data = self._read()
            if repo_id not in data.get("repositories", {}):
                return False
            del data["repositories"][repo_id]
            self._write(data)
        return True


def validate_repository(repo: Dict[str, Any]) -> tuple[bool, str]:
    allowed_fields = {
        "id", "name", "type", "repository", "password", "endpoint", "bucket",
        "bucket_path", "prefix", "aws_access_key_id", "aws_secret_access_key",
        "aws_default_region", "created_at", "updated_at",
    }
    unknown_fields = sorted(set(repo) - allowed_fields)
    if unknown_fields:
        return False, f"Unsupported fields: {', '.join(unknown_fields)}"
    repo_type = repo.get("type", "local")
    if repo_type not in {"local", "s3", "minio"}:
        return False, "Repository type must be local, s3, or minio"
    name = str(repo.get("name", "")).strip()
    if not name or len(name) > 100 or any(char in name for char in "\r\n"):
        return False, "Repository name must be 1-100 characters on one line"
    repo["name"] = name
    if repo_type in {"s3", "minio"}:
        endpoint = str(repo.get("endpoint", "")).strip().rstrip("/")
        bucket = str(repo.get("bucket", "") or repo.get("bucket_path", "")).strip().strip("/")
        prefix = str(repo.get("prefix", "")).strip().strip("/")
        if endpoint and bucket:
            repo["repository"] = f"s3:{endpoint}/{bucket}{('/' + prefix) if prefix else ''}"
    if not repo.get("repository"):
        return False, "Restic repository path/URL is required"
    repository = str(repo["repository"]).strip()
    if any(char in repository for char in "\r\n\x00"):
        return False, "Restic repository contains invalid characters"
    if repo_type == "local" and not repository.startswith("/"):
        return False, "Local repository path must be absolute"
    repo["repository"] = repository
    if not repo.get("password"):
        return False, "Restic repository password is required"
    if repo_type in {"s3", "minio"}:
        if not str(repo.get("repository", "")).startswith("s3:"):
            return False, "S3/MinIO repository must use restic s3: repository format"
        if not repo.get("aws_access_key_id") or not repo.get("aws_secret_access_key"):
            return False, "S3/MinIO repositories require access key and secret key"
    return True, ""


_repo_storage: Optional[ResticRepositoryStorage] = None


def get_repository_storage() -> ResticRepositoryStorage:
    global _repo_storage
    if _repo_storage is None:
        storage_path = os.environ.get("RESTIC_REPOSITORIES_STORAGE", "/var/backups/restic_repositories.json")
        _repo_storage = ResticRepositoryStorage(storage_path)
    return _repo_storage


def sanitize_profile(profile: Dict[str, Any]) -> Dict[str, Any]:
    return sanitize_secrets(profile, PROFILE_SECRET_FIELDS)


def sanitize_repository(repo: Dict[str, Any]) -> Dict[str, Any]:
    return sanitize_secrets(repo, REPOSITORY_SECRET_FIELDS)
