#!/usr/bin/env python3
"""Configuration snapshots for disaster recovery.

Profiles and repository definitions live on the dashboard's local volume. If
that server is lost, the snapshots in an off-site repository survive but the
profiles describing them do not. To make a repository self-describing, the
profiles that write into it are stored inside it as a tiny restic snapshot:

    tag  backupstic-config    host  backupstic    file  /backupstic-config.json

The document holds those profiles with their secrets in clear text: it is
protected by the repository's own encryption, so restoring needs nothing but
the repository password (not the old server's PROFILE_SECRET_KEY). Only the
profiles of *this* repository go in, so its password never unlocks the
credentials of other repositories.

A new snapshot is written only when the content changed (hash kept in
``<base>/config-sync/``); the dashboard marks repositories dirty on profile
changes and backup-all.sh pushes after every profile backup.
"""

import datetime
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional


CONFIG_TAG = "backupstic-config"
CONFIG_HOST = "backupstic"
CONFIG_FILENAME = "backupstic-config.json"
DOCUMENT_FORMAT = "backupstic-config"
DOCUMENT_VERSION = 1
STATE_DIR = "config-sync"
REPO_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
PROFILE_ID_RE = re.compile(r"[A-Za-z0-9-]{1,64}")
SNAPSHOT_ID_RE = re.compile(r"[0-9a-f]{8,64}")
REPOSITORY_PUBLIC_FIELDS = (
    "name", "type", "repository", "endpoint", "bucket", "bucket_path", "prefix", "aws_default_region",
)
RESTIC_TIMEOUT = 300
MAX_DOCUMENT_BYTES = 16 * 1024 * 1024
DEFAULT_KEEP = 20


class ConfigBackupError(Exception):
    """Shown to the operator."""


def utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def restic_env(repo: Dict[str, Any], base_env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Environment for running restic against `repo` (mirrors the dashboard's helper)."""
    env = dict(os.environ if base_env is None else base_env)
    env["RESTIC_REPOSITORY"] = repo.get("repository") or env.get("RESTIC_REPO", "/var/backups/restic-repo")
    env["RESTIC_PASSWORD"] = repo.get("password") or env.get("RESTIC_PASSWORD", "")
    for field, var in (("aws_access_key_id", "AWS_ACCESS_KEY_ID"),
                       ("aws_secret_access_key", "AWS_SECRET_ACCESS_KEY"),
                       ("aws_default_region", "AWS_DEFAULT_REGION")):
        if repo.get(field):
            env[var] = repo[field]
    return env


def repo_key(repo_id: Optional[str]) -> str:
    return repo_id or "default"


def repository_profiles(repo_id: str, profiles: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(
        (p for p in profiles if repo_key(p.get("repository_id")) == repo_key(repo_id)),
        key=lambda p: str(p.get("id", "")),
    )


def build_document(repo: Dict[str, Any], profiles: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "format": DOCUMENT_FORMAT,
        "version": DOCUMENT_VERSION,
        "exported_at": utc_now(),
        "repository": {field: repo[field] for field in REPOSITORY_PUBLIC_FIELDS if repo.get(field)},
        "profiles": repository_profiles(repo.get("id"), profiles),
    }


def content_hash(document: Dict[str, Any]) -> str:
    stable = {key: value for key, value in document.items() if key != "exported_at"}
    return hashlib.sha256(json.dumps(stable, sort_keys=True, default=str).encode()).hexdigest()


# ==================== SYNC STATE ====================

class ConfigSyncState:
    """Dirty markers and the last pushed hash per repository (shared by all processes)."""

    def __init__(self, base_dir: str):
        self.root = Path(base_dir) / STATE_DIR

    def _path(self, repo_id: str, suffix: str) -> Path:
        repo_id = repo_key(repo_id)
        if not REPO_ID_RE.fullmatch(repo_id):
            raise ValueError("Invalid repository id")
        return self.root / f"{repo_id}{suffix}"

    def mark_dirty(self, repo_id: Optional[str]) -> None:
        try:
            path = self._path(repo_id, ".dirty")
        except ValueError:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        path.touch()

    def clear_dirty(self, repo_id: Optional[str]) -> None:
        try:
            self._path(repo_id, ".dirty").unlink()
        except (OSError, ValueError):
            pass

    def is_dirty(self, repo_id: Optional[str]) -> bool:
        try:
            return self._path(repo_id, ".dirty").exists()
        except ValueError:
            return False

    def dirty(self) -> List[str]:
        if not self.root.is_dir():
            return []
        return sorted(p.name[:-len(".dirty")] for p in self.root.glob("*.dirty"))

    def last(self, repo_id: Optional[str]) -> Optional[Dict[str, Any]]:
        try:
            with open(self._path(repo_id, ".json"), encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def record(self, repo_id: Optional[str], digest: str, profiles: int) -> None:
        path = self._path(repo_id, ".json")
        self.root.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.root, prefix=".tmp-")
        with os.fdopen(fd, "w") as handle:
            json.dump({"hash": digest, "synced_at": utc_now(), "profiles": profiles}, handle)
        os.replace(tmp, path)

    def forget(self, repo_id: Optional[str]) -> None:
        for suffix in (".dirty", ".json"):
            try:
                self._path(repo_id, suffix).unlink()
            except (OSError, ValueError):
                pass


# ==================== RESTIC OPERATIONS ====================

Runner = Callable[..., subprocess.CompletedProcess]


def _restic(args: List[str], env: Dict[str, str], run: Runner, input_bytes: Optional[bytes] = None,
            timeout: int = RESTIC_TIMEOUT) -> subprocess.CompletedProcess:
    try:
        return run(["restic", "--no-cache", *args], input=input_bytes, capture_output=True,
                   env=env, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise ConfigBackupError("restic timed out") from exc
    except FileNotFoundError as exc:
        raise ConfigBackupError("restic is not installed") from exc


def _stderr(result: subprocess.CompletedProcess) -> str:
    text = result.stderr.decode(errors="replace") if isinstance(result.stderr, bytes) else (result.stderr or "")
    return text.strip().splitlines()[-1] if text.strip() else f"restic exited with {result.returncode}"


def sync_repository(repo: Dict[str, Any], profiles_loader: Callable[[], List[Dict[str, Any]]],
                    state: ConfigSyncState, env: Dict[str, str], force: bool = False,
                    keep: int = DEFAULT_KEEP, run: Runner = subprocess.run) -> str:
    """Save the repository's profiles into it if they changed. Returns 'saved' or 'unchanged'.

    The dirty marker is cleared *before* profiles are read, so a change made
    while this runs marks the repository dirty again instead of being lost.
    """
    repo_id = repo_key(repo.get("id"))
    state.clear_dirty(repo_id)
    try:
        document = build_document(repo, profiles_loader())
        digest = content_hash(document)
        previous = state.last(repo_id) or {}
        if not force and previous.get("hash") == digest:
            return "unchanged"
        payload = json.dumps(document, indent=2, sort_keys=True, default=str).encode()
        result = _restic(
            ["backup", "--stdin", "--stdin-filename", CONFIG_FILENAME,
             "--tag", CONFIG_TAG, "--host", CONFIG_HOST],
            env, run, input_bytes=payload,
        )
        if result.returncode != 0:
            raise ConfigBackupError(_stderr(result))
        state.record(repo_id, digest, len(document["profiles"]))
    except BaseException:
        state.mark_dirty(repo_id)
        raise
    if keep > 0:
        # Old versions are only metadata; a failure here (e.g. a prune holds the
        # exclusive lock) is retried with the next save.
        _restic(["forget", "--tag", CONFIG_TAG, "--host", CONFIG_HOST, "--keep-last", str(keep)], env, run)
    return "saved"


def list_config_snapshots(env: Dict[str, str], run: Runner = subprocess.run) -> List[Dict[str, Any]]:
    result = _restic(["snapshots", "--tag", CONFIG_TAG, "--json"], env, run, timeout=120)
    if result.returncode != 0:
        raise ConfigBackupError(_stderr(result))
    try:
        snapshots = json.loads(result.stdout or b"[]") or []
    except ValueError as exc:
        raise ConfigBackupError("Could not read the repository's snapshot list") from exc
    snapshots = [s for s in snapshots if isinstance(s, dict) and s.get("id")]
    snapshots.sort(key=lambda s: str(s.get("time", "")), reverse=True)
    return [{"id": s["id"], "short_id": s["id"][:8], "time": s.get("time")} for s in snapshots]


def read_document(env: Dict[str, str], snapshot_id: str, run: Runner = subprocess.run) -> Dict[str, Any]:
    if not SNAPSHOT_ID_RE.fullmatch(snapshot_id or ""):
        raise ConfigBackupError("Invalid snapshot id")
    result = _restic(["dump", snapshot_id, f"/{CONFIG_FILENAME}"], env, run, timeout=120)
    if result.returncode != 0:
        raise ConfigBackupError(_stderr(result))
    if len(result.stdout or b"") > MAX_DOCUMENT_BYTES:
        raise ConfigBackupError("Configuration backup is unexpectedly large")
    try:
        document = json.loads(result.stdout)
    except ValueError as exc:
        raise ConfigBackupError("Configuration backup is not valid JSON") from exc
    if not isinstance(document, dict) or document.get("format") != DOCUMENT_FORMAT:
        raise ConfigBackupError("Not a Backupstic configuration backup")
    if int(document.get("version") or 0) > DOCUMENT_VERSION:
        raise ConfigBackupError("Configuration backup was written by a newer Backupstic version")
    if not isinstance(document.get("profiles"), list):
        raise ConfigBackupError("Configuration backup has no profile list")
    return document


def snapshot_counts(env: Dict[str, str], run: Runner = subprocess.run) -> Dict[str, int]:
    """Number of backups per `profile:<id>` tag in the repository (best effort)."""
    result = _restic(["snapshots", "--json"], env, run, timeout=120)
    counts: Dict[str, int] = {}
    if result.returncode != 0:
        return counts
    try:
        snapshots = json.loads(result.stdout or b"[]") or []
    except ValueError:
        return counts
    for snap in snapshots:
        for tag in (snap.get("tags") or []) if isinstance(snap, dict) else []:
            if isinstance(tag, str) and tag.startswith("profile:"):
                counts[tag[len("profile:"):]] = counts.get(tag[len("profile:"):], 0) + 1
    return counts


# ==================== IMPORT ====================

def _candidate(raw: Dict[str, Any], target_repo_id: str) -> Dict[str, Any]:
    profile = dict(raw)
    profile["repository_id"] = repo_key(target_repo_id)
    return profile


def plan_import(document: Dict[str, Any], local_profiles: List[Dict[str, Any]], target_repo_id: str,
                validate: Callable[[Dict[str, Any]], tuple], counts: Optional[Dict[str, int]] = None
                ) -> List[Dict[str, Any]]:
    """Describe what restoring each saved profile would do.

    status: new | exists (same id here) | name_conflict (another profile with the
    same name and type) | invalid
    """
    counts = counts or {}
    local_ids = {p.get("id") for p in local_profiles}
    local_names = {(p.get("name"), p.get("type")): p.get("id") for p in local_profiles}
    items = []
    for raw in document.get("profiles", []):
        if not isinstance(raw, dict):
            continue
        pid = str(raw.get("id", ""))
        item = {
            "id": pid,
            "name": str(raw.get("name", "")),
            "type": str(raw.get("type", "")),
            "description": str(raw.get("description", "") or ""),
            "schedule": str(raw.get("schedule", "")),
            "enabled": raw.get("enabled", True) is not False,
            "auth_method": raw.get("auth_method") if raw.get("type") == "ssh_files" else None,
            "backups": counts.get(pid, 0),
            "status": "new",
            "error": "",
        }
        candidate = _candidate(raw, target_repo_id)
        ok, error = validate(candidate) if PROFILE_ID_RE.fullmatch(pid) else (False, "Invalid profile id")
        if not ok:
            item.update(status="invalid", error=error)
        elif pid in local_ids:
            item["status"] = "exists"
        elif (item["name"], item["type"]) in local_names:
            item["status"] = "name_conflict"
            item["error"] = "Another profile here already has this name and type"
        items.append(item)
    return items


def apply_import(document: Dict[str, Any], storage, target_repo_id: str,
                 validate: Callable[[Dict[str, Any]], tuple], selected_ids: Optional[Iterable[str]] = None,
                 overwrite: bool = False) -> List[Dict[str, Any]]:
    """Restore saved profiles into `storage`, keeping their ids. Returns per-profile results."""
    selected = None if selected_ids is None else {str(pid) for pid in selected_ids}
    plan = {item["id"]: item for item in plan_import(document, storage.get_all(), target_repo_id, validate)}
    results = []
    for raw in document.get("profiles", []):
        if not isinstance(raw, dict):
            continue
        pid = str(raw.get("id", ""))
        if selected is not None and pid not in selected:
            continue
        item = plan.get(pid)
        result = {"id": pid, "name": str(raw.get("name", "")), "result": "skipped", "error": ""}
        if item is None or item["status"] == "invalid":
            result.update(result="failed", error=(item or {}).get("error") or "Invalid profile")
        elif item["status"] == "name_conflict":
            result["error"] = item["error"]
        elif item["status"] == "exists" and not overwrite:
            result["error"] = "Already exists here"
        else:
            candidate = _candidate(raw, target_repo_id)
            validate(candidate)  # normalizes fields
            storage.put(candidate)
            result["result"] = "replaced" if item["status"] == "exists" else "restored"
        results.append(result)
    return results
