#!/usr/bin/env python3
"""Save the profiles of one repository into it (disaster-recovery config snapshot).

Usage: export-config-snapshot.py <repository_id> [--force]
Prints 'saved' or 'unchanged'; exits 1 on errors. Called by backup-all.sh after
every profile backup; the dashboard does the same after profile changes.
"""

import os
import sys

for path in ("/app/backend", os.path.join(os.path.dirname(__file__), "..", "backend")):
    if path not in sys.path:
        sys.path.insert(0, path)

from config_backup import ConfigBackupError, ConfigSyncState, restic_env, sync_repository  # noqa: E402
from profiles import get_repository_storage, get_storage  # noqa: E402


def main() -> int:
    args = [arg for arg in sys.argv[1:] if arg != "--force"]
    repo_id = args[0] if args and args[0] else "default"
    repo = get_repository_storage().get(repo_id)
    if repo is None:
        print(f"Repository not found: {repo_id}", file=sys.stderr)
        return 1
    state = ConfigSyncState(os.environ.get("BACKUP_BASE", "/var/backups"))
    try:
        keep = int(os.environ.get("CONFIG_SNAPSHOT_KEEP", "20"))
    except ValueError:
        keep = 20
    try:
        result = sync_repository(repo, lambda: get_storage().get_all(), state, restic_env(repo),
                                 force="--force" in sys.argv[1:], keep=keep)
    except (ConfigBackupError, OSError) as exc:
        print(f"Configuration snapshot failed: {exc}", file=sys.stderr)
        return 1
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
