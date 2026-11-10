#!/usr/bin/env python3
"""Plain (decrypted, flattened) download of one restic snapshot.

`restic restore` recreates the absolute paths a snapshot was taken from, so a
profile backup comes back as ``<target>/var/backups/.staging/<profile id>/...``.
The helpers here find the directory that actually holds the backup files and
turn it into a single gzip download with no wrapper folders:

* exactly one gzip archive (``.gz``/``.tgz``; ``*.manifest.json`` sidecars are
  ignored) is served as-is, e.g. ``app_20260101_000000.sql.gz``;
* anything else becomes one ``.tar.gz`` whose entries sit at its top level.
"""

import os
import re
import subprocess
from typing import Iterable, List, Optional, Tuple


SIDECAR_SUFFIX = ".manifest.json"
GZIP_SUFFIXES = (".gz", ".tgz")
TAR_TIMEOUT = 1800


class PlainExportError(Exception):
    """Shown to the operator as the download error."""


def safe_name(value: str, fallback: str = "backup") -> str:
    """ASCII file-name component (profile names may be any language)."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "")).strip("._-")
    return cleaned[:80] or fallback


def snapshot_stamp(snapshot_time: str) -> str:
    """'2026-01-01T02:03:04.5+00:00' -> '20260101_020304'."""
    digits = re.sub(r"\D", "", str(snapshot_time or "")[:19])
    return f"{digits[:8]}_{digits[8:14]}" if len(digits) >= 14 else "snapshot"


def _inside(path: str, root: str) -> bool:
    real_root = os.path.realpath(root)
    real_path = os.path.realpath(path)
    return real_path == real_root or real_path.startswith(real_root + os.sep)


def content_root(restore_dir: str, snapshot_paths: Iterable[str]) -> str:
    """Directory under ``restore_dir`` that holds the snapshot's own files."""
    paths = [p for p in (snapshot_paths or []) if isinstance(p, str) and p.startswith("/")]
    if paths:
        common = paths[0] if len(paths) == 1 else os.path.commonpath(paths)
        candidate = os.path.join(restore_dir, common.lstrip("/"))
        if os.path.isfile(candidate):
            candidate = os.path.dirname(candidate)
        if os.path.isdir(candidate) and _inside(candidate, restore_dir):
            return candidate

    # Unknown layout: skip directories that only wrap a single subdirectory.
    root = restore_dir
    while True:
        try:
            entries = os.listdir(root)
        except OSError:
            return root
        if len(entries) != 1:
            return root
        only = os.path.join(root, entries[0])
        if not os.path.isdir(only) or os.path.islink(only):
            return root
        root = only


def plan(root: str) -> Tuple[str, Optional[str], List[str]]:
    """Return ('file', path, []) to serve one archive directly, or ('tar', None, entries)."""
    try:
        entries = sorted(os.listdir(root))
    except OSError as exc:
        raise PlainExportError(f"Restored snapshot could not be read: {exc}") from exc
    if not entries:
        raise PlainExportError("Snapshot contains no files")
    payload = [name for name in entries if not name.endswith(SIDECAR_SUFFIX)]
    if len(payload) == 1:
        only = os.path.join(root, payload[0])
        if (os.path.isfile(only) and not os.path.islink(only)
                and payload[0].lower().endswith(GZIP_SUFFIXES)):
            return "file", only, []
    return "tar", None, entries


def build(restore_dir: str, snapshot: dict, work_dir: str) -> Tuple[str, str]:
    """Build the download for a restored snapshot. Returns (file_path, download_name)."""
    root = content_root(restore_dir, snapshot.get("paths") or [])
    kind, path, entries = plan(root)
    if kind == "file":
        return path, safe_name(os.path.basename(path))

    download_name = "{}_{}_{}.tar.gz".format(
        safe_name(snapshot.get("profile_name") or "backup"),
        snapshot_stamp(snapshot.get("time", "")),
        str(snapshot.get("id", ""))[:8] or "snapshot",
    )
    archive = os.path.join(work_dir, download_name)
    try:
        result = subprocess.run(
            ["tar", "-czf", archive, "-C", root, "--", *entries],
            capture_output=True,
            text=True,
            timeout=TAR_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise PlainExportError("Packaging the download timed out") from exc
    if result.returncode != 0 or not os.path.isfile(archive):
        raise PlainExportError(result.stderr.strip() or "Failed to package the download")
    return archive, download_name
