#!/usr/bin/env python3
"""Print a decrypted backup profile JSON for backup scripts."""

import json
import os
import sys

for path in ("/app/backend", os.path.join(os.path.dirname(__file__), "..", "backend")):
    if path not in sys.path:
        sys.path.insert(0, path)

from profiles import get_storage  # noqa: E402


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: export-profile.py <profile_id>", file=sys.stderr)
        return 2
    profile = get_storage().get(sys.argv[1])
    if profile is None:
        print(f"Profile not found: {sys.argv[1]}", file=sys.stderr)
        return 1
    print(json.dumps(profile))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
