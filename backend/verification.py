#!/usr/bin/env python3
"""Backup health verification.

A verification replays exactly what an operator does with a downloaded backup:
restore the snapshot from restic, wrap it in the encrypted download bundle,
extract that bundle, open it with the repository password, and finally test
every archive inside (gzip/tar/zip/...). Job state is persisted as JSON under
BACKUP_BASE so every dashboard worker process can report progress.
"""

import bz2
import datetime
import fcntl
import gzip
import hashlib
import json
import lzma
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
import uuid
import zipfile
import zlib
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from postgres_verify import CLUSTER_HEADER, PostgresVerifyError


STEP_DEFINITIONS = [
    ("restore", "Restore snapshot from Restic"),
    ("package", "Build encrypted download bundle"),
    ("extract", "Extract the bundle"),
    ("decrypt", "Open bundle with repository password"),
    ("compare", "Compare with original snapshot"),
    ("archives", "Test archives inside the backup"),
    ("postgres", "Restore PostgreSQL dumps into a test server"),
]

ACTIVE_STATUSES = {"queued", "running"}
SNAPSHOT_ID_RE = re.compile(r"[0-9a-f]{4,64}")
WORK_DIR_PREFIX = "verify_"
BUNDLE_REPO_DIR = "encrypted-restic-repo"
BUNDLE_README = "README_DECRYPT.txt"
CHUNK_SIZE = 1024 * 1024
SAMPLE_SIZE = 4096
MAX_JSON_BYTES = 64 * 1024 * 1024
MAX_REPORTED_ARCHIVES = 200
MAX_REPORTED_MISMATCHES = 20
CODEC_SUFFIX = {"gzip": "gz", "bzip2": "bz2", "xz": "xz"}
REDIS_CHECK_TIMEOUT = 1800
HISTORY_MAX_AGE_SECONDS = 365 * 24 * 3600
DISK_HEADROOM_BYTES = 64 * 1024 * 1024


def utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def format_bytes(num_bytes: float) -> str:
    num_bytes = float(num_bytes or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num_bytes) < 1024.0:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.1f} PB"


class VerificationError(Exception):
    """A step failed; the message is shown to the operator."""


# ==================== STATE STORAGE ====================

class VerificationStore:
    """Latest verification result per snapshot, plus a global single-job lock."""

    def __init__(self, base_dir: str):
        self.base_dir = Path(base_dir)
        self.root = self.base_dir / "verifications"
        self.lock_path = self.base_dir / "verify.lock"

    def _path(self, snapshot_id: str) -> Path:
        if not SNAPSHOT_ID_RE.fullmatch(snapshot_id or ""):
            raise ValueError("Invalid snapshot id")
        return self.root / f"{snapshot_id}.json"

    def try_acquire(self) -> Optional[int]:
        """Take the single-verification lock; returns its fd, or None if busy."""
        self.base_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return None
        return fd

    @staticmethod
    def release(fd: int) -> None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def is_busy(self) -> bool:
        try:
            fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        except OSError:
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        except BlockingIOError:
            return True
        finally:
            os.close(fd)

    def save(self, state: Dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(state["snapshot_id"])
        state["updated_at"] = utc_now()
        fd, tmp = tempfile.mkstemp(dir=self.root, prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(state, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _read(self, path: Path) -> Optional[Dict[str, Any]]:
        try:
            with open(path, encoding="utf-8") as handle:
                state = json.load(handle)
        except (OSError, ValueError):
            return None
        return state if isinstance(state, dict) else None

    def _finalize_orphan(self, state: Dict[str, Any], busy: bool) -> Dict[str, Any]:
        """A job marked active while nobody holds the lock died with its worker."""
        if state.get("status") in ACTIVE_STATUSES and not busy:
            state = dict(state)
            state["status"] = "interrupted"
            state["error"] = "Verification was interrupted before it finished (the dashboard restarted). Run it again."
        return state

    def load(self, snapshot_id: str) -> Optional[Dict[str, Any]]:
        try:
            path = self._path(snapshot_id)
        except ValueError:
            return None
        state = self._read(path)
        return self._finalize_orphan(state, self.is_busy()) if state else None

    def find(self, snapshot_prefix: str) -> Optional[Dict[str, Any]]:
        """Resolve a short or full snapshot id to its latest verification."""
        if not SNAPSHOT_ID_RE.fullmatch(snapshot_prefix or ""):
            return None
        if not self.root.is_dir():
            return None
        matches = [p for p in self.root.glob(f"{snapshot_prefix}*.json")]
        if len(matches) != 1:
            return None
        return self.load(matches[0].stem)

    def all(self) -> List[Dict[str, Any]]:
        if not self.root.is_dir():
            return []
        busy = self.is_busy()
        states = []
        for path in self.root.glob("*.json"):
            state = self._read(path)
            if state and state.get("snapshot_id"):
                states.append(self._finalize_orphan(state, busy))
        return states

    def running(self) -> Optional[Dict[str, Any]]:
        for state in self.all():
            if state.get("status") in ACTIVE_STATUSES:
                return state
        return None

    def delete(self, snapshot_id: str) -> None:
        try:
            self._path(snapshot_id).unlink()
        except (OSError, ValueError):
            pass

    def prune_history(self, max_age_seconds: int = HISTORY_MAX_AGE_SECONDS) -> None:
        if not self.root.is_dir():
            return
        cutoff = time.time() - max_age_seconds
        for path in self.root.glob("*.json"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                pass


def summarize(state: Dict[str, Any]) -> Dict[str, Any]:
    """Compact form for list views."""
    return {
        "id": state.get("id"),
        "snapshot_id": state.get("snapshot_id"),
        "short_id": state.get("short_id"),
        "status": state.get("status"),
        "current_step": state.get("current_step"),
        "started_at": state.get("started_at"),
        "finished_at": state.get("finished_at"),
        "error": state.get("error", ""),
        "summary": state.get("summary", {}),
    }


def new_state(snapshot: Dict[str, Any], repo: Dict[str, Any], requested_by: str = "") -> Dict[str, Any]:
    snapshot_id = snapshot["id"]
    return {
        "id": str(uuid.uuid4()),
        "snapshot_id": snapshot_id,
        "short_id": snapshot_id[:8],
        "snapshot_time": snapshot.get("time"),
        "repository_id": repo.get("id") or "default",
        "repository_name": repo.get("name") or "",
        "profile_name": snapshot.get("profile_name") or "",
        "requested_by": requested_by,
        "status": "queued",
        "current_step": None,
        "started_at": utc_now(),
        "finished_at": None,
        "error": "",
        "steps": [
            {"key": key, "label": label, "status": "pending", "detail": "", "duration_ms": None}
            for key, label in STEP_DEFINITIONS
        ],
        "archives": [],
        "summary": {},
    }


# ==================== ARCHIVE INSPECTION ====================

def _drain(stream) -> tuple[int, bytes, bytes]:
    """Read a stream to EOF (so codec checksums are verified) keeping head/tail samples."""
    total = 0
    head = b""
    tail = b""
    while True:
        chunk = stream.read(CHUNK_SIZE)
        if not chunk:
            break
        total += len(chunk)
        if len(head) < SAMPLE_SIZE:
            head += chunk[:SAMPLE_SIZE - len(head)]
        tail = (tail + chunk)[-SAMPLE_SIZE:]
    return total, head, tail


def _content_checks(inner_name: str, total: int, head: bytes, tail: bytes) -> tuple[str, str]:
    """Format-aware sanity checks on decompressed content."""
    if total == 0:
        return "warning", "Content is empty"
    if inner_name.endswith(".sql") and b"PostgreSQL database" in head:
        if b"dump complete" not in tail:
            return "failed", "PostgreSQL dump is truncated (completion marker missing)"
        kind = "cluster dump" if CLUSTER_HEADER in head else "dump"
        return "passed", f"PostgreSQL {kind} complete, {format_bytes(total)}"
    if inner_name.endswith(".rdb"):
        if not head.startswith(b"REDIS"):
            return "failed", "Not a Redis RDB file (REDIS header missing)"
        if not (tail[-1:] == b"\xff" or tail[-9:-8] == b"\xff"):
            return "failed", "Redis RDB is truncated (EOF marker missing)"
        return "passed", f"Redis RDB {head[5:9].decode('ascii', 'replace')}, {format_bytes(total)}"
    return "passed", f"{format_bytes(total)} uncompressed"


def _open_compressed(path: str, codec: str):
    if codec == "gzip":
        return gzip.open(path, "rb")
    if codec == "bzip2":
        return bz2.open(path, "rb")
    if codec == "xz":
        return lzma.open(path, "rb")
    return open(path, "rb")


def _redis_check_rdb(path: str, codec: str) -> Optional[tuple[str, str]]:
    """Let Redis' own redis-check-rdb validate the file (structure + checksum)."""
    tool = shutil.which("redis-check-rdb")
    if not tool:
        return None
    rdb_path = path
    temp = None
    try:
        if codec != "none":
            fd, temp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".rdb")
            with os.fdopen(fd, "wb") as out, _open_compressed(path, codec) as stream:
                shutil.copyfileobj(stream, out, CHUNK_SIZE)
            rdb_path = temp
        result = subprocess.run([tool, rdb_path], capture_output=True, text=True, timeout=REDIS_CHECK_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "warning", f"redis-check-rdb could not run: {exc}"
    finally:
        if temp:
            try:
                os.unlink(temp)
            except OSError:
                pass
    output = (result.stdout or "") + (result.stderr or "")
    keys = re.search(r"\[info\] (\d+) keys read", output)
    keys_text = f", {keys.group(1)} keys" if keys else ""
    if result.returncode == 0 and "RDB looks OK" in output:
        return "passed", f"redis-check-rdb: RDB looks OK{keys_text}"
    error_lines = output.split("--- RDB ERROR DETECTED ---", 1)[-1].splitlines()
    reason = next(
        (re.sub(r"^\[offset \d+\] ", "", line) for line in error_lines if line.startswith("[offset")),
        "redis-check-rdb reported an error",
    )
    if "Can't handle RDB format version" in reason:
        return "warning", f"{reason} (newer than this redis-check-rdb; header and EOF checks passed)"
    return "failed", f"redis-check-rdb: {reason}"


def classify_archive(name: str) -> Optional[tuple[str, str, str]]:
    """Return (kind, codec, inner_name) for files worth testing, else None."""
    lower = name.lower()
    for suffixes, codec in (
        ((".tar.gz", ".tgz"), "gzip"),
        ((".tar.bz2", ".tbz2", ".tbz"), "bzip2"),
        ((".tar.xz", ".txz"), "xz"),
        ((".tar",), "none"),
    ):
        if lower.endswith(suffixes):
            return "tar", codec, lower
    if lower.endswith(".zip"):
        return "zip", "none", lower
    for suffix, codec in ((".gz", "gzip"), (".bz2", "bzip2"), (".xz", "xz")):
        if lower.endswith(suffix):
            return "compressed", codec, lower[: -len(suffix)]
    if lower.endswith((".json", ".rdb", ".sql")):
        return "plain", "none", lower
    return None


def inspect_archive(path: str, name: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Fully read one archive and report whether it opens cleanly."""
    name = name or os.path.basename(path)
    classified = classify_archive(name)
    if classified is None:
        return None
    kind, codec, inner = classified
    if kind == "tar":
        label = "tar" if codec == "none" else f"tar.{CODEC_SUFFIX[codec]}"
    elif kind == "compressed":
        label = CODEC_SUFFIX[codec]
    else:
        label = inner.rsplit(".", 1)[-1]
    result = {"path": name, "format": label, "size": 0, "status": "passed", "detail": ""}
    try:
        result["size"] = os.path.getsize(path)
        if kind == "tar":
            with _open_compressed(path, codec) as raw:
                members = files = content = 0
                with tarfile.open(fileobj=raw, mode="r|") as archive:
                    for member in archive:
                        members += 1
                        if member.isfile():
                            files += 1
                            extracted = archive.extractfile(member)
                            if extracted is not None:
                                content += _drain(extracted)[0]
                # Reach the end of the compressed stream so its CRC is verified.
                _drain(raw)
            if members == 0:
                result.update(status="warning", detail="Archive is empty")
            else:
                result["detail"] = f"{plural(files, 'file')}, {format_bytes(content)} uncompressed"
        elif kind == "zip":
            with zipfile.ZipFile(path) as archive:
                infos = archive.infolist()
                if any(info.flag_bits & 0x1 for info in infos):
                    result.update(status="warning", detail="ZIP is password-protected; contents not tested")
                else:
                    bad = archive.testzip()
                    if bad is not None:
                        result.update(status="failed", detail=f"Corrupt entry: {bad}")
                    elif not infos:
                        result.update(status="warning", detail="Archive is empty")
                    else:
                        total = sum(info.file_size for info in infos)
                        result["detail"] = f"{len(infos)} {'entry' if len(infos) == 1 else 'entries'}, {format_bytes(total)} uncompressed"
        elif kind == "plain" and inner.endswith(".json"):
            if result["size"] > MAX_JSON_BYTES:
                result["detail"] = "Too large to parse; skipped JSON validation"
            else:
                with open(path, "rb") as handle:
                    json.loads(handle.read() or b"null")
                result["detail"] = "Valid JSON"
        else:
            with _open_compressed(path, codec) as stream:
                total, head, tail = _drain(stream)
            status, detail = _content_checks(inner, total, head, tail)
            if status == "passed" and inner.endswith(".rdb"):
                status, detail = _redis_check_rdb(path, codec) or (status, detail)
            result.update(status=status, detail=detail)
            if status == "passed" and inner.endswith(".sql") and b"PostgreSQL database" in head:
                # Internal hints for the PostgreSQL restore step; popped before reporting.
                result["postgres_dump"] = {"codec": codec, "cluster": CLUSTER_HEADER in head}
    except (OSError, EOFError, ValueError, tarfile.TarError, zipfile.BadZipFile,
            zlib.error, lzma.LZMAError, RuntimeError, NotImplementedError) as exc:
        result.update(status="failed", detail=str(exc) or exc.__class__.__name__)
    return result


# ==================== FILE TREE HELPERS ====================

def tree_digest(root: str) -> Dict[str, tuple]:
    """Map relative path -> (type, size, sha256 | link target) for a directory tree."""
    entries: Dict[str, tuple] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in filenames + [d for d in dirnames if os.path.islink(os.path.join(dirpath, d))]:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root)
            if os.path.islink(full):
                entries[rel] = ("link", 0, os.readlink(full))
            elif os.path.isfile(full):
                digest = hashlib.sha256()
                size = 0
                with open(full, "rb") as handle:
                    while True:
                        chunk = handle.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        size += len(chunk)
                        digest.update(chunk)
                entries[rel] = ("file", size, digest.hexdigest())
    return entries


def compare_trees(original: Dict[str, tuple], copy: Dict[str, tuple]) -> List[str]:
    problems = []
    for rel in sorted(set(original) | set(copy)):
        if rel not in copy:
            problems.append(f"missing: {rel}")
        elif rel not in original:
            problems.append(f"unexpected: {rel}")
        elif original[rel] != copy[rel]:
            problems.append(f"content differs: {rel}")
    return problems


# ==================== VERIFICATION RUN ====================

class Verification:
    """Runs the six steps for one snapshot, persisting progress after each change.

    Restic/bundle operations are injected so the dashboard keeps one implementation
    of restore/packaging shared with the download endpoint:
      restore(target_dir) -> result dict       (runs under the global backup lock)
      package(work_dir, restore_dir) -> (archive_path, error)
      open_repo(args, repo_path, timeout, wrong_password=False) -> result dict
      backup_lock(on_wait) -> context manager guarding the production repository
      postgres_tester() -> PostgresRestoreTester; None when VERIFY_POSTGRES_URL is unset
    """

    def __init__(self, store: VerificationStore, state: Dict[str, Any], *,
                 work_base: str, expected_size: int,
                 restore: Callable, package: Callable, open_repo: Callable,
                 backup_lock: Callable, postgres_tester: Optional[Callable] = None):
        self.store = store
        self.state = state
        self.work_base = work_base
        self.expected_size = int(expected_size or 0)
        self.restore = restore
        self.package = package
        self.open_repo = open_repo
        self.backup_lock = backup_lock
        self.postgres_tester = postgres_tester
        self.pg_dumps: List[tuple] = []
        self.work = ""

    # -- state helpers --
    def _step(self, key: str) -> Dict[str, Any]:
        return next(step for step in self.state["steps"] if step["key"] == key)

    def _save(self) -> None:
        self.store.save(self.state)

    def _detail(self, key: str, detail: str) -> None:
        self._step(key)["detail"] = detail
        self._save()

    def _run_step(self, key: str, func: Callable[[], str]) -> None:
        step = self._step(key)
        step.update(status="running", detail="", started_at=utc_now())
        self.state["current_step"] = key
        self._save()
        started = time.monotonic()
        try:
            outcome = func()
        except VerificationError as exc:
            step.update(status="failed", detail=str(exc))
            raise
        except Exception as exc:  # unexpected: still report which step broke
            step.update(status="failed", detail=f"Unexpected error: {exc}")
            raise VerificationError(str(exc)) from exc
        finally:
            step["duration_ms"] = int((time.monotonic() - started) * 1000)
            step["finished_at"] = utc_now()
        status, detail = outcome if isinstance(outcome, tuple) else ("passed", outcome)
        step.update(status=status, detail=detail)
        self._save()

    # -- steps --
    def _restore(self) -> str:
        if self.expected_size:
            needed = self.expected_size * 3 + DISK_HEADROOM_BYTES
            free = shutil.disk_usage(self.work_base).free
            if free < needed:
                raise VerificationError(
                    f"Not enough free disk space: verification needs about {format_bytes(needed)}, "
                    f"only {format_bytes(free)} is available"
                )
        self.restore_dir = os.path.join(self.work, "data")
        try:
            with self.backup_lock(lambda: self._detail("restore", "Waiting for the running backup to finish…")):
                self._detail("restore", "Restoring files from the repository…")
                result = self.restore(self.restore_dir)
        except TimeoutError as exc:
            raise VerificationError(str(exc)) from exc
        if not result.get("success"):
            raise VerificationError(result.get("stderr") or "restic restore failed")
        self.original = tree_digest(self.restore_dir)
        if not self.original:
            raise VerificationError("Snapshot restored but contains no files")
        total = sum(entry[1] for entry in self.original.values())
        return f"{plural(len(self.original), 'file')}, {format_bytes(total)} restored and decrypted by restic"

    def _package(self) -> str:
        archive, error = self.package(self.work, self.restore_dir)
        if error or not archive or not os.path.isfile(archive):
            raise VerificationError(error or "Download bundle was not created")
        self.archive = archive
        return f"{os.path.basename(archive)} ({format_bytes(os.path.getsize(archive))})"

    def _extract(self) -> str:
        self.extract_dir = os.path.join(self.work, "extracted")
        os.makedirs(self.extract_dir)
        try:
            with tarfile.open(self.archive, "r:gz") as bundle:
                members = bundle.getmembers()
                bundle.extractall(self.extract_dir, filter="data")
        except (tarfile.TarError, OSError, EOFError, zlib.error) as exc:
            raise VerificationError(f"Bundle could not be extracted: {exc}") from exc
        os.unlink(self.archive)
        self.bundle_repo = os.path.join(self.extract_dir, BUNDLE_REPO_DIR)
        if not os.path.isfile(os.path.join(self.bundle_repo, "config")):
            raise VerificationError("Extracted bundle does not contain an encrypted restic repository")
        if not os.path.isfile(os.path.join(self.extract_dir, BUNDLE_README)):
            return "warning", f"{len(members)} bundle entries extracted; {BUNDLE_README} is missing"
        return f"{len(members)} bundle entries extracted"

    def _decrypt(self) -> str:
        wrong = self.open_repo(["snapshots", "--json"], self.bundle_repo, 120, wrong_password=True)
        if wrong.get("success"):
            raise VerificationError("Bundle opened with a wrong password; it is not protected")
        listed = self.open_repo(["snapshots", "--json"], self.bundle_repo, 300)
        if not listed.get("success"):
            raise VerificationError(listed.get("stderr") or "Repository password was rejected")
        try:
            snapshots = json.loads(listed.get("stdout") or "[]")
        except ValueError:
            snapshots = []
        if len(snapshots) != 1:
            raise VerificationError(f"Expected one snapshot in the bundle, found {len(snapshots)}")
        self.decrypted_dir = os.path.join(self.work, "decrypted")
        restored = self.open_repo(["restore", "latest", "--target", self.decrypted_dir], self.bundle_repo, 3600)
        if not restored.get("success"):
            raise VerificationError(restored.get("stderr") or "Restore from the bundle failed")
        shutil.rmtree(self.extract_dir, ignore_errors=True)
        return "Password accepted (a wrong password is rejected) and contents restored"

    def _compare(self) -> str:
        # The bundle stores the absolute path of the first restore, so its files
        # come back nested under that path inside the decrypted target.
        self.decrypted_data = os.path.join(self.decrypted_dir, self.restore_dir.lstrip("/"))
        copy = tree_digest(self.decrypted_data) if os.path.isdir(self.decrypted_data) else {}
        problems = compare_trees(self.original, copy)
        if problems:
            shown = "; ".join(problems[:MAX_REPORTED_MISMATCHES])
            more = len(problems) - MAX_REPORTED_MISMATCHES
            raise VerificationError(
                f"{len(problems)} differences: {shown}" + (f" (+{more} more)" if more > 0 else "")
            )
        shutil.rmtree(self.restore_dir, ignore_errors=True)
        return f"{plural(len(self.original), 'file')} identical after the round trip (SHA-256)"

    def _archives(self):
        files = sorted(rel for rel, entry in self.original.items() if entry[0] == "file")
        # Snapshots store absolute staging paths (var/backups/.staging/<id>/...);
        # report names relative to their shared directory instead.
        common = os.path.commonpath([os.path.dirname(rel) for rel in files]) if files else ""
        results = []
        for rel in files:
            full_path = os.path.join(self.decrypted_data, rel)
            inspected = inspect_archive(full_path, os.path.relpath(rel, common or "."))
            if inspected is None:
                continue
            hint = inspected.pop("postgres_dump", None)
            if hint:
                self.pg_dumps.append((full_path, hint["codec"], inspected["path"], hint["cluster"]))
            results.append(inspected)
        counts = {status: sum(1 for r in results if r["status"] == status) for status in ("passed", "warning", "failed")}
        # Problems first so they survive the report cap.
        order = {"failed": 0, "warning": 1, "passed": 2}
        results.sort(key=lambda r: (order[r["status"]], r["path"]))
        self.state["archives"] = results[:MAX_REPORTED_ARCHIVES]
        self.state["summary"].update(
            archives_total=len(results),
            archives_passed=counts["passed"],
            archives_warning=counts["warning"],
            archives_failed=counts["failed"],
        )
        if not results:
            return "warning", "No archive or dump files found to test"
        detail = f"{counts['passed']} of {len(results)} opened cleanly"
        if counts["failed"]:
            raise VerificationError(f"{counts['failed']} of {plural(len(results), 'archive')} corrupt")
        if counts["warning"]:
            return "warning", f"{detail}; {counts['warning']} with warnings"
        return detail

    def _postgres(self):
        if not self.pg_dumps:
            return "skipped", "No PostgreSQL dumps in this backup"
        if self.postgres_tester is None:
            return "skipped", "Not configured: set VERIFY_POSTGRES_URL to test-restore PostgreSQL dumps"
        try:
            tester = self.postgres_tester()
            version = tester.server_version()
            tester.cleanup_leftovers()
        except (PostgresVerifyError, subprocess.TimeoutExpired, OSError) as exc:
            return "warning", f"Verification PostgreSQL server is unavailable: {exc}"

        results = []
        for full_path, codec, display, cluster in self.pg_dumps:
            self._detail("postgres", f"Restoring {display} into PostgreSQL {version}…")
            try:
                result = tester.restore(lambda: _open_compressed(full_path, codec), cluster)
            except (PostgresVerifyError, subprocess.TimeoutExpired, OSError) as exc:
                result = {"status": "warning", "detail": f"Verification server problem: {exc}", "errors": []}
            finally:
                problems = tester.cleanup()
            if problems:
                result["detail"] += f" (cleanup: {'; '.join(problems)})"
            result["path"] = display
            results.append(result)
        self.state["postgres"] = results
        self.state["summary"]["postgres_version"] = version

        failed = [r for r in results if r["status"] == "failed"]
        if failed:
            raise VerificationError(
                f"{len(failed)} of {plural(len(results), 'PostgreSQL dump')} did not restore cleanly: {failed[0]['detail']}"
            )
        totals = {key: sum(r.get(key, 0) for r in results) for key in ("databases", "tables", "rows")}
        detail = (
            f"PostgreSQL {version} restored {plural(len(results), 'dump')}: "
            f"{plural(totals['databases'], 'database')}, {plural(totals['tables'], 'table')}, ~{totals['rows']:,} rows"
        )
        if any(r["status"] == "warning" for r in results):
            return "warning", f"{detail}; see warnings below"
        return detail

    def run(self) -> Dict[str, Any]:
        self.state["status"] = "running"
        self._save()
        failed = False
        try:
            self.work = tempfile.mkdtemp(prefix=WORK_DIR_PREFIX, dir=self.work_base)
            for key, func in (
                ("restore", self._restore),
                ("package", self._package),
                ("extract", self._extract),
                ("decrypt", self._decrypt),
                ("compare", self._compare),
                ("archives", self._archives),
                ("postgres", self._postgres),
            ):
                self._run_step(key, func)
            self.state["summary"].update(
                files=len(self.original),
                bytes=sum(entry[1] for entry in self.original.values()),
            )
        except VerificationError as exc:
            failed = True
            self.state["error"] = str(exc)
        except Exception as exc:
            failed = True
            self.state["error"] = f"Unexpected error: {exc}"
        finally:
            if self.work:
                shutil.rmtree(self.work, ignore_errors=True)
        for step in self.state["steps"]:
            if step["status"] in ("pending", "running"):
                step["status"] = "skipped" if failed else step["status"]
        if failed:
            self.state["status"] = "failed"
        elif any(step["status"] == "warning" for step in self.state["steps"]):
            self.state["status"] = "warning"
        else:
            self.state["status"] = "passed"
        self.state["current_step"] = None
        self.state["finished_at"] = utc_now()
        self._save()
        return self.state


def cleanup_stale_work_dirs(work_base: str) -> None:
    """Remove scratch dirs left by a verification whose worker died.

    Only call while holding the verification lock, so no live job owns them.
    """
    try:
        names = os.listdir(work_base)
    except OSError:
        return
    for name in names:
        path = os.path.join(work_base, name)
        if name.startswith(WORK_DIR_PREFIX) and os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path, ignore_errors=True)
