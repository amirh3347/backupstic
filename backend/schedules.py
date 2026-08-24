#!/usr/bin/env python3
"""Build the cron file derived from Backupstic backup profiles."""

import fcntl
import json
import os
import re
import tempfile
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterable


CRON_FIELD_RE = re.compile(r"[0-9*/,\-]+")
PROFILE_ID_RE = re.compile(r"[A-Za-z0-9-]+")


def valid_cron_expression(expression: str) -> bool:
    """Accept numeric five-field cron expressions without control syntax."""
    fields = str(expression).split()
    return len(fields) == 5 and all(CRON_FIELD_RE.fullmatch(field) for field in fields)


def load_profiles_file(storage_path: str) -> list[dict[str, Any]]:
    """Load the non-secret profile metadata needed to build the crontab."""
    try:
        with open(storage_path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return []

    profiles = []
    for profile_id, stored_profile in data.get("profiles", {}).items():
        if not isinstance(stored_profile, dict):
            continue
        profile = dict(stored_profile)
        profile.setdefault("id", profile_id)
        profiles.append(profile)
    return profiles


def build_profile_schedule_lines(
    profiles: Iterable[dict[str, Any]],
    scripts_dir: str = "/scripts",
    backup_base: str = "/var/backups",
) -> list[str]:
    """Group enabled profiles with identical schedules into sequential batches."""
    groups: dict[str, list[str]] = defaultdict(list)

    for profile in profiles:
        if profile.get("enabled", True) is not True:
            continue

        profile_id = str(profile.get("id", ""))
        schedule = str(profile.get("schedule", "")).strip()
        if not PROFILE_ID_RE.fullmatch(profile_id):
            raise ValueError(f"Invalid profile id in schedule storage: {profile_id!r}")
        if not valid_cron_expression(schedule):
            raise ValueError(f"Invalid schedule for profile {profile_id}: {schedule!r}")
        groups[schedule].append(profile_id)

    lines = []
    for schedule in sorted(groups):
        profile_ids = " ".join(sorted(groups[schedule]))
        lines.append(
            f"{schedule} {scripts_dir}/backup-profile-batch.sh {profile_ids} "
            f">> {backup_base}/cron.log 2>&1"
        )
    return lines


@contextmanager
def _schedule_lock(schedule_file: str):
    path = Path(schedule_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with open(lock_path, "a+", encoding="utf-8") as lock_handle:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        yield path


def _write_schedule_file_unlocked(path: Path, lines: Iterable[str]) -> None:
    descriptor, temp_path = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines).rstrip() + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except Exception:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass
        raise


def write_schedule_file(schedule_file: str, lines: Iterable[str]) -> None:
    """Atomically replace a crontab while serializing concurrent web workers."""
    with _schedule_lock(schedule_file) as path:
        _write_schedule_file_unlocked(path, lines)


def _complete_schedule_lines(
    profiles: Iterable[dict[str, Any]],
    scripts_dir: str,
    backup_base: str,
    fallback_entry: str,
) -> list[str]:
    profile_list = list(profiles)
    lines = ["# Managed by Backupstic; manual edits may be replaced."]

    if profile_list:
        profile_lines = build_profile_schedule_lines(profile_list, scripts_dir, backup_base)
        if profile_lines:
            lines.extend(profile_lines)
        else:
            lines.append("# No enabled backup profiles.")
    elif fallback_entry.strip():
        fallback_entry = fallback_entry.strip()
        if "\n" in fallback_entry or "\r" in fallback_entry:
            raise ValueError("BACKUP_CRON must contain exactly one cron entry")
        lines.append(fallback_entry)
    else:
        lines.append("# No backup profiles configured.")
    return lines


def sync_profile_schedules(
    profiles: Iterable[dict[str, Any]],
    schedule_file: str,
    scripts_dir: str = "/scripts",
    backup_base: str = "/var/backups",
    fallback_entry: str = "",
) -> list[str]:
    """Rebuild the complete cron file from current profile storage."""
    with _schedule_lock(schedule_file) as path:
        lines = _complete_schedule_lines(profiles, scripts_dir, backup_base, fallback_entry)
        _write_schedule_file_unlocked(path, lines)
        return lines


def sync_profile_schedules_from_loader(
    profile_loader: Callable[[], Iterable[dict[str, Any]]],
    schedule_file: str,
    scripts_dir: str = "/scripts",
    backup_base: str = "/var/backups",
    fallback_entry: str = "",
) -> list[str]:
    """Load the latest profiles and publish them under one cross-process lock."""
    with _schedule_lock(schedule_file) as path:
        lines = _complete_schedule_lines(
            profile_loader(), scripts_dir, backup_base, fallback_entry
        )
        _write_schedule_file_unlocked(path, lines)
        return lines
