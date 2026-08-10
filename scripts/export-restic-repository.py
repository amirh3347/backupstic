#!/usr/bin/env python3
"""Print a decrypted Restic repository JSON for backup scripts."""

import json
import os
import sys

for path in ("/app/backend", os.path.join(os.path.dirname(__file__), "..", "backend")):
    if path not in sys.path:
        sys.path.insert(0, path)

from profiles import get_repository_storage  # noqa: E402


def main() -> int:
    repo_id = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else "default"
    repo = get_repository_storage().get(repo_id)
    if repo is None:
        print(f"Repository not found: {repo_id}", file=sys.stderr)
        return 1
    print(json.dumps(repo))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
