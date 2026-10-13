#!/usr/bin/env python3
"""Run a command while keeping its combined output in bounded log files."""

import argparse
import fcntl
import os
import subprocess
from pathlib import Path


def trim_to_tail(path: Path, max_bytes: int) -> None:
    """Shrink an existing oversized log without making another full-size copy."""
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return
    if size <= max_bytes:
        return

    temporary = path.with_name(f"{path.name}.trim.{os.getpid()}")
    try:
        with path.open("rb") as source:
            source.seek(-max_bytes, os.SEEK_END)
            tail = source.read(max_bytes)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "wb") as target:
            target.write(tail)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


class BoundedLog:
    def __init__(self, path: Path, max_bytes: int, backup_count: int) -> None:
        self.path = path
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = self.path.with_name(f"{self.path.name}.lock")
        self._normalise_existing_logs()

    def _locked(self):
        descriptor = os.open(self.lock, os.O_WRONLY | os.O_CREAT, 0o600)
        handle = os.fdopen(descriptor, "wb")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return handle

    def _normalise_existing_logs(self) -> None:
        with self._locked():
            trim_to_tail(self.path, self.max_bytes)
            for candidate in self.path.parent.glob(f"{self.path.name}.*"):
                suffix = candidate.name.removeprefix(f"{self.path.name}.")
                if not suffix.isdigit():
                    continue
                index = int(suffix)
                if index > self.backup_count:
                    candidate.unlink(missing_ok=True)
                else:
                    trim_to_tail(candidate, self.max_bytes)

    def _rotate(self) -> None:
        if self.backup_count == 0:
            self.path.unlink(missing_ok=True)
            return

        oldest = self.path.with_name(f"{self.path.name}.{self.backup_count}")
        oldest.unlink(missing_ok=True)
        for index in range(self.backup_count - 1, 0, -1):
            source = self.path.with_name(f"{self.path.name}.{index}")
            destination = self.path.with_name(f"{self.path.name}.{index + 1}")
            if source.exists():
                os.replace(source, destination)
        if self.path.exists():
            os.replace(self.path, self.path.with_name(f"{self.path.name}.1"))

    def write(self, data: bytes) -> None:
        if not data:
            return
        # A single pathological write must not escape the configured bound.
        if len(data) > self.max_bytes:
            data = data[-self.max_bytes :]

        with self._locked():
            try:
                current_size = self.path.stat().st_size
            except FileNotFoundError:
                current_size = 0
            if current_size and current_size + len(data) > self.max_bytes:
                self._rotate()

            descriptor = os.open(
                self.path,
                os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                0o600,
            )
            with os.fdopen(descriptor, "ab") as handle:
                handle.write(data)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log-file", required=True, type=Path)
    parser.add_argument("--max-bytes", required=True, type=int)
    parser.add_argument("--backup-count", required=True, type=int)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.command[:1] == ["--"]:
        args.command = args.command[1:]
    if not args.command:
        parser.error("a command is required after --")
    if args.max_bytes < 1:
        parser.error("--max-bytes must be positive")
    if args.backup_count < 0:
        parser.error("--backup-count cannot be negative")
    return args


def main() -> int:
    args = parse_args()
    output = BoundedLog(args.log_file, args.max_bytes, args.backup_count)
    process = subprocess.Popen(
        args.command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        close_fds=True,
    )
    assert process.stdout is not None
    while True:
        chunk = os.read(process.stdout.fileno(), 64 * 1024)
        if not chunk:
            break
        output.write(chunk)
    return process.wait()


if __name__ == "__main__":
    raise SystemExit(main())
