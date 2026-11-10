#!/usr/bin/env python3
"""One-time download links.

A link is created by an authenticated user; the server then prepares the file
in the background (restore + package) and the link can be redeemed exactly once
without a dashboard session, e.g. from another machine with curl.

Layout under ``<base>/downloads/<job_id>/``:
  state.json      job metadata (never contains the token)
  payload         the prepared file, while unclaimed
  payload.claimed the file after the single redeem (renamed atomically)
  work/           scratch space used while preparing

``job_id`` is the SHA-256 of the token, so a redeem request maps to its job
directly and the token itself is never written to disk.
"""

import datetime
import hashlib
import json
import os
import re
import secrets
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


ROOT_DIR = "downloads"
JOB_ID_RE = re.compile(r"[0-9a-f]{64}")
TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{32,128}")
PAYLOAD = "payload"
CLAIMED = "payload.claimed"
STATE = "state.json"
ACTIVE = "preparing"


def utc_iso(timestamp: Optional[float]) -> Optional[str]:
    if timestamp is None:
        return None
    return datetime.datetime.fromtimestamp(timestamp, datetime.timezone.utc).isoformat()


def process_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return True  # unknown owner: let the age-based cleanup decide
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def job_id_for(token: str) -> Optional[str]:
    if not TOKEN_RE.fullmatch(token or ""):
        return None
    return hashlib.sha256(token.encode()).hexdigest()


class DownloadLinkStore:
    def __init__(self, base_dir: str):
        self.root = Path(base_dir) / ROOT_DIR

    def job_dir(self, job_id: str) -> Path:
        if not JOB_ID_RE.fullmatch(job_id or ""):
            raise ValueError("Invalid download id")
        return self.root / job_id

    # -- state --
    def create(self, meta: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
        """Create a job; returns (token, state). The token is shown only once."""
        token = secrets.token_urlsafe(32)
        job_id = job_id_for(token)
        directory = self.job_dir(job_id)
        directory.mkdir(parents=True, mode=0o700)
        state = {
            **meta,
            "id": job_id,
            "status": ACTIVE,
            "error": "",
            # The preparing thread lives in this worker; if the worker dies
            # (dashboard restart), the job can never finish.
            "pid": os.getpid(),
            "created_at": time.time(),
            "ready_at": None,
            "expires_at": None,
            "claimed_at": None,
            "filename": "",
            "size": 0,
        }
        self.save(state)
        return token, state

    def save(self, state: Dict[str, Any]) -> None:
        directory = self.job_dir(state["id"])
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".state-", suffix=".json")
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(state, handle)
            os.replace(tmp, directory / STATE)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def load(self, job_id: str) -> Optional[Dict[str, Any]]:
        try:
            with open(self.job_dir(job_id) / STATE, encoding="utf-8") as handle:
                state = json.load(handle)
        except (OSError, ValueError):
            return None
        if not isinstance(state, dict):
            return None
        if state.get("status") == ACTIVE and not process_alive(state.get("pid")):
            state = dict(state, status="failed",
                         error="Interrupted: the dashboard restarted while preparing. Create a new link.")
        return state

    def all(self) -> List[Dict[str, Any]]:
        if not self.root.is_dir():
            return []
        states = []
        for path in self.root.iterdir():
            if JOB_ID_RE.fullmatch(path.name):
                state = self.load(path.name)
                if state:
                    states.append(state)
        return states

    def active_count(self) -> int:
        return sum(1 for state in self.all() if state.get("status") == ACTIVE)

    def mark_ready(self, state: Dict[str, Any], prepared_file: str, filename: str, ttl_seconds: int) -> None:
        directory = self.job_dir(state["id"])
        os.replace(prepared_file, directory / PAYLOAD)
        now = time.time()
        state.update(
            status="ready",
            filename=filename,
            size=(directory / PAYLOAD).stat().st_size,
            ready_at=now,
            expires_at=now + ttl_seconds,
        )
        self.save(state)

    def mark_failed(self, state: Dict[str, Any], error: str) -> None:
        state.update(status="failed", error=error)
        self.save(state)

    def delete(self, job_id: str) -> bool:
        try:
            directory = self.job_dir(job_id)
        except ValueError:
            return False
        if not directory.is_dir():
            return False
        shutil.rmtree(directory, ignore_errors=True)
        return True

    # -- redeem --
    def claim(self, job_id: str) -> tuple[Optional[str], Optional[Dict[str, Any]], str]:
        """Take the file for the single allowed download.

        Returns (path, state, error_code); error_code is '' on success or one of
        'not_found', 'preparing', 'failed', 'expired', 'used'.
        """
        state = self.load(job_id)
        if state is None:
            return None, None, "not_found"
        status = state.get("status")
        if status == ACTIVE:
            return None, state, "preparing"
        if status == "failed":
            return None, state, "failed"
        if status == "claimed":
            return None, state, "used"
        if (state.get("expires_at") or 0) < time.time():
            return None, state, "expired"
        directory = self.job_dir(job_id)
        try:
            # Atomic across worker processes: only one rename can succeed.
            os.rename(directory / PAYLOAD, directory / CLAIMED)
        except FileNotFoundError:
            return None, state, "used"
        state.update(status="claimed", claimed_at=time.time())
        try:
            self.save(state)
        except OSError:
            pass
        return str(directory / CLAIMED), state, ""

    def discard_payload(self, job_id: str) -> None:
        """Delete a redeemed file but keep its state, so the link reports 'used'."""
        try:
            (self.job_dir(job_id) / CLAIMED).unlink()
        except (OSError, ValueError):
            pass

    # -- housekeeping --
    def cleanup(self, max_prepare_seconds: int, now: Optional[float] = None) -> None:
        """Remove expired, used, failed and abandoned jobs."""
        if not self.root.is_dir():
            return
        now = time.time() if now is None else now
        for path in self.root.iterdir():
            if not JOB_ID_RE.fullmatch(path.name) or not path.is_dir():
                continue
            state = self.load(path.name)
            try:
                age = now - path.stat().st_mtime
            except OSError:
                continue
            if state is None:
                remove = age > 3600
            else:
                status = state.get("status")
                created = state.get("created_at") or 0
                if status == ACTIVE:
                    # The preparing worker died (dashboard restart) or hangs.
                    remove = now - created > max_prepare_seconds
                elif status == "failed" and (path / "work").exists():
                    remove = True  # interrupted mid-preparation: free the scratch space now
                elif status == "ready":
                    remove = (state.get("expires_at") or 0) < now
                elif status == "claimed":
                    # The file is gone already; keep the "already used" answer a while.
                    remove = now - (state.get("claimed_at") or created) > 24 * 3600
                else:
                    remove = now - created > 24 * 3600
            if remove:
                shutil.rmtree(path, ignore_errors=True)


def public_state(state: Dict[str, Any]) -> Dict[str, Any]:
    """What the dashboard shows about a link (no token, no paths)."""
    return {
        "id": state.get("id"),
        "status": state.get("status"),
        "error": state.get("error", ""),
        "format": state.get("format"),
        "snapshot_id": state.get("snapshot_id"),
        "short_id": str(state.get("snapshot_id", ""))[:8],
        "profile_name": state.get("profile_name", ""),
        "filename": state.get("filename", ""),
        "size": state.get("size", 0),
        "created_at": utc_iso(state.get("created_at")),
        "ready_at": utc_iso(state.get("ready_at")),
        "expires_at": utc_iso(state.get("expires_at")),
        "claimed_at": utc_iso(state.get("claimed_at")),
        "created_by": state.get("created_by", ""),
    }
