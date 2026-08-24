#!/usr/bin/env python3
"""Reconcile the shared crontab with the profiles stored in the backup volume."""

import os
import sys
from pathlib import Path


for backend_dir in (
    os.environ.get("BACKEND_DIR", ""),
    "/app/backend",
    str(Path(__file__).resolve().parents[1] / "backend"),
):
    if backend_dir and os.path.isdir(backend_dir):
        sys.path.insert(0, backend_dir)
        break

from schedules import load_profiles_file, sync_profile_schedules_from_loader


def main() -> int:
    backup_base = os.environ.get("BACKUP_BASE", "/var/backups")
    profiles_path = os.environ.get(
        "PROFILES_STORAGE",
        os.path.join(backup_base, "profiles.json"),
    )
    lines = sync_profile_schedules_from_loader(
        lambda: load_profiles_file(profiles_path),
        os.path.join(backup_base, "schedules.cron"),
        scripts_dir=os.environ.get("SCRIPTS_DIR", "/scripts"),
        backup_base=backup_base,
        fallback_entry=os.environ.get("BACKUP_CRON", ""),
    )
    jobs = sum(1 for line in lines if line and not line.startswith("#"))
    print(f"Schedule reconciliation complete: {jobs} cron job(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
