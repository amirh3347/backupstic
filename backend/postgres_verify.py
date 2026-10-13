#!/usr/bin/env python3
"""Prove a PostgreSQL SQL dump is restorable by loading it into a scratch server.

Plain-format dumps (``pg_dump | gzip`` and ``pg_dumpall``) can only be fully
validated by PostgreSQL itself, by replaying them. Each database in a dump is
restored into its own temporary ``backupstic_verify_*`` database on a dedicated
verification server (VERIFY_POSTGRES_URL) and dropped afterwards.

Safety rules, because dumps contain cluster-wide statements:
  * statements touching the connecting superuser (e.g. ``ALTER ROLE postgres
    ... PASSWORD``) are filtered out so the server's own login keeps working;
  * cluster dumps create roles, so they only run on a server that holds
    nothing but Backupstic scratch objects;
  * each database is replayed by its own psql process connected to its scratch
    database, and ``\\connect`` lines are dropped, so data can never land in a
    different database.
"""

import json
import os
import re
import subprocess
import tempfile
import uuid
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional
from urllib.parse import parse_qs, unquote, urlparse


SCRATCH_PREFIX = "backupstic_verify_"
PENDING_ROLES_FILE = "verify-postgres-roles.json"
MAX_REPORTED_ERRORS = 5

CLUSTER_HEADER = b"PostgreSQL database cluster dump"
DATABASE_HEADER_RE = re.compile(rb'^-- Database "(.*)" dump\s*$')
COPY_START_RE = re.compile(rb"^COPY .* FROM stdin;\s*$")
CREATE_ROLE_RE = re.compile(rb'^CREATE ROLE ("(?:[^"]|"")+"|[^\s;]+);')
ALTER_ROLE_RE = re.compile(rb'^ALTER ROLE ("(?:[^"]|"")+"|[^\s;]+)[\s;]')
CREATE_DATABASE_RE = re.compile(rb"^CREATE DATABASE ")
ENCODING_RE = re.compile(rb"ENCODING = '([A-Za-z0-9_]+)'")
CLIENT_ENCODING_RE = re.compile(rb"^SET client_encoding = '([A-Za-z0-9_]+)';")
DATABASE_REF_LINE_RE = re.compile(rb"^(ALTER DATABASE|COMMENT ON DATABASE|GRANT |REVOKE |SECURITY LABEL )")
ERROR_LINE_RE = re.compile(r"\bERROR:\s+(.*)$")

# Errors that say nothing about the dump itself.
IGNORED_ERRORS = [re.compile(r'^role ".*" already exists$')]
MISSING_ROLE_ERRORS = [re.compile(r'^role ".*" does not exist$')]
ENVIRONMENT_ERRORS = [
    re.compile(p) for p in (
        r'^extension ".*" is not available',
        r"could not open extension control file",
        r'could not access file "\$libdir',
        r'^tablespace ".*" does not exist',
        r'^directory ".*" does not exist',
        r'invalid value for parameter "default_tablespace"',
        r'^collation ".*" for encoding ".*" does not exist',
        r"^could not create locale",
        r"No space left on device",
    )
]


class PostgresVerifyError(Exception):
    """The verification server itself is unusable (not a dump problem)."""


def postgres_env(url: str) -> Dict[str, str]:
    """Translate a postgresql:// URL into libpq environment variables."""
    parsed = urlparse(url)
    if parsed.scheme not in ("postgres", "postgresql") or not parsed.hostname:
        raise PostgresVerifyError("VERIFY_POSTGRES_URL must look like postgresql://user:password@host:5432/postgres")
    env = {
        "PGHOST": parsed.hostname,
        "PGPORT": str(parsed.port or 5432),
        "PGUSER": unquote(parsed.username or "postgres"),
        "PGDATABASE": unquote(parsed.path.lstrip("/")) or "postgres",
        "PGCONNECT_TIMEOUT": "10",
        "PGAPPNAME": "backupstic-verify",
    }
    if parsed.password is not None:
        env["PGPASSWORD"] = unquote(parsed.password)
    sslmode = parse_qs(parsed.query).get("sslmode")
    if sslmode:
        env["PGSSLMODE"] = sslmode[0]
    return env


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def unquote_ident(token: bytes) -> str:
    text = token.decode("utf-8", "replace")
    if text.startswith('"') and text.endswith('"'):
        return text[1:-1].replace('""', '"')
    return text


def classify_errors(messages: List[str]) -> tuple[str, str, List[str]]:
    """Return (status, detail, reported_errors) for psql ERROR messages."""
    relevant = [m for m in messages if not any(p.search(m) for p in IGNORED_ERRORS)]
    missing_roles = [m for m in relevant if any(p.search(m) for p in MISSING_ROLE_ERRORS)]
    environment = [m for m in relevant if any(p.search(m) for p in ENVIRONMENT_ERRORS)]
    other = [m for m in relevant if m not in missing_roles and m not in environment]
    if other and not environment:
        return "failed", f"{len(other)} SQL errors while restoring: {other[0]}", other[:MAX_REPORTED_ERRORS]
    if environment:
        detail = f"Restored, but the verification server lacks something the dump needs: {environment[0]}"
        if other:
            detail += f" ({len(other)} follow-up errors)"
        return "warning", detail, (environment + other)[:MAX_REPORTED_ERRORS]
    note = f"; ownership/grants skipped for {len(missing_roles)} statements (roles not on verification server)" if missing_roles else ""
    return "passed", note, []


def rewrite_database_refs(line: bytes, name: str, scratch: str) -> bytes:
    """Point ALTER/COMMENT/GRANT ... DATABASE <name> at the scratch database."""
    for ident in {name, quote_ident(name)}:
        pattern = re.compile(rb"(\bDATABASE\s+)" + re.escape(ident.encode()) + rb"(?=[\s;])")
        if pattern.search(line):
            return pattern.sub(lambda m: m.group(1) + quote_ident(scratch).encode(), line, count=1)
    return line


class Segment:
    """One database's portion of a dump (or the globals part of a cluster dump)."""

    def __init__(self, name: Optional[str], is_globals: bool = False):
        self.name = name
        self.is_globals = is_globals


def split_dump(lines: Iterable[bytes], cluster: bool) -> Iterator[tuple[Segment, bytes]]:
    """Yield (segment, line) pairs; COPY data lines are never inspected."""
    segment = Segment(None, is_globals=cluster)
    in_copy = False
    for line in lines:
        if in_copy:
            if line.rstrip(b"\r\n") == b"\\.":
                in_copy = False
            yield segment, line
            continue
        if cluster:
            header = DATABASE_HEADER_RE.match(line)
            if header:
                segment = Segment(header.group(1).decode("utf-8", "replace"))
                continue
        if COPY_START_RE.match(line):
            in_copy = True
        yield segment, line


class PostgresRestoreTester:
    def __init__(self, url: str, state_dir: str, timeout: int = 7200,
                 popen: Callable = subprocess.Popen, run: Callable = subprocess.run):
        self.env = {**os.environ, **postgres_env(url)}
        self.user = self.env["PGUSER"]
        self.maintenance_db = self.env["PGDATABASE"]
        self.state_dir = state_dir
        self.timeout = timeout
        self.popen = popen
        self.run_cmd = run
        self.token = uuid.uuid4().hex[:8]
        self.scratch_dbs: List[str] = []
        self._created_roles: set = set()

    # ---------- server helpers ----------
    def sql(self, statement: str, database: Optional[str] = None, timeout: int = 120) -> str:
        result = self.run_cmd(
            ["psql", "-X", "-At", "-v", "ON_ERROR_STOP=1", "-d", database or self.maintenance_db, "-c", statement],
            capture_output=True, text=True, timeout=timeout, env=self.env,
        )
        if result.returncode != 0:
            raise PostgresVerifyError((result.stderr or result.stdout or "psql failed").strip().splitlines()[-1])
        return result.stdout.strip()

    def server_version(self) -> str:
        return self.sql("SHOW server_version")

    def _pending_path(self) -> str:
        return os.path.join(self.state_dir, PENDING_ROLES_FILE)

    def cleanup_leftovers(self) -> None:
        """Drop scratch databases/roles left by an earlier, interrupted run."""
        for name in self.sql(f"SELECT datname FROM pg_database WHERE datname LIKE '{SCRATCH_PREFIX}%'").splitlines():
            self.drop_database(name)
        try:
            with open(self._pending_path()) as handle:
                roles = json.load(handle)
        except (OSError, ValueError):
            roles = []
        self.drop_roles(roles)

    def drop_database(self, name: str) -> None:
        ident = quote_ident(name)
        try:
            self.sql(f"ALTER DATABASE {ident} IS_TEMPLATE false")
        except PostgresVerifyError:
            pass
        self.sql(f"DROP DATABASE IF EXISTS {ident} WITH (FORCE)", timeout=600)

    def drop_roles(self, roles: List[str]) -> List[str]:
        failed = []
        for role in roles:
            if role == self.user:
                continue
            try:
                self.sql(f"DROP ROLE IF EXISTS {quote_ident(role)}")
            except PostgresVerifyError:
                failed.append(role)
        if failed:
            self._write_pending(failed)
        else:
            try:
                os.unlink(self._pending_path())
            except OSError:
                pass
        return failed

    def _write_pending(self, roles: List[str]) -> None:
        os.makedirs(self.state_dir, exist_ok=True)
        with open(self._pending_path(), "w") as handle:
            json.dump(sorted(set(roles)), handle)

    def foreign_objects(self) -> List[str]:
        """Anything on the server that Backupstic did not create."""
        databases = self.sql(
            "SELECT datname FROM pg_database WHERE datname NOT IN ('template0', 'template1', 'postgres') "
            f"AND datname NOT LIKE '{SCRATCH_PREFIX}%'"
        ).splitlines()
        databases = [d for d in databases if d != self.maintenance_db]
        roles = self.sql("SELECT rolname FROM pg_roles WHERE rolname NOT LIKE 'pg\\_%'").splitlines()
        roles = [r for r in roles if r != self.user]
        return [f"database {d}" for d in databases] + [f"role {r}" for r in roles]

    def create_scratch_database(self, encoding: str) -> str:
        name = f"{SCRATCH_PREFIX}{self.token}_{len(self.scratch_dbs)}"
        try:
            self.sql(f"CREATE DATABASE {quote_ident(name)} TEMPLATE template0 ENCODING '{encoding}' LC_COLLATE 'C' LC_CTYPE 'C'")
        except PostgresVerifyError:
            # Unknown/unsupported encoding names fall back to UTF8.
            self.sql(f"CREATE DATABASE {quote_ident(name)} TEMPLATE template0 ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C'")
        self.scratch_dbs.append(name)
        return name

    def database_stats(self, name: str) -> tuple[int, int]:
        out = self.sql(
            "ANALYZE; SELECT count(*), coalesce(sum(greatest(c.reltuples, 0)), 0)::bigint "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.relkind IN ('r', 'p') AND n.nspname NOT IN ('pg_catalog', 'information_schema') "
            "AND n.nspname NOT LIKE 'pg\\_toast%'",
            database=name, timeout=self.timeout,
        )
        tables, rows = (out.splitlines()[-1].split("|") + ["0", "0"])[:2]
        return int(tables or 0), int(rows or 0)

    # ---------- restore ----------
    def _start_psql(self, database: str, errors_file):
        return self.popen(
            ["psql", "-X", "-q", "-v", "ON_ERROR_STOP=0", "-d", database],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=errors_file, env=self.env,
        )

    def _finish_psql(self, proc) -> None:
        try:
            proc.stdin.close()
        except BrokenPipeError:
            pass
        try:
            code = proc.wait(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise PostgresVerifyError(f"Restore did not finish within {self.timeout}s")
        if code not in (0, 1):
            raise PostgresVerifyError(f"psql exited with code {code} (connection lost?)")

    def restore(self, open_stream: Callable[[], Any], cluster: bool) -> Dict[str, Any]:
        """Replay one dump. ``open_stream`` returns a fresh binary reader of the SQL."""
        if cluster:
            foreign = self.foreign_objects()
            if foreign:
                return {
                    "status": "warning",
                    "detail": "Cluster dump not restored: the verification server is not dedicated "
                              f"(it contains {', '.join(foreign[:3])}). Point VERIFY_POSTGRES_URL at an empty server.",
                    "errors": [], "databases": 0, "tables": 0, "rows": 0,
                }
            # Record the roles this dump creates before running it, so an
            # interrupted run can still be cleaned up next time.
            with open_stream() as stream:
                created_roles = {
                    unquote_ident(m.group(1))
                    for m in (CREATE_ROLE_RE.match(line) for line in stream)
                    if m
                } - {self.user}
            self._write_pending(list(created_roles))
        else:
            created_roles = set()

        self._created_roles = created_roles
        totals = {"databases": 0, "tables": 0, "rows": 0}
        with tempfile.TemporaryFile() as errors_file, open_stream() as stream:
            runner = _SegmentRunner(self, errors_file, totals)
            try:
                for segment, line in split_dump(stream, cluster):
                    runner.feed(segment, line)
                runner.finish()
            except BaseException:
                runner.abort()
                raise
            errors_file.seek(0)
            messages = [
                m.group(1).strip()
                for m in (ERROR_LINE_RE.search(raw) for raw in errors_file.read().decode("utf-8", "replace").splitlines())
                if m
            ]

        status, note, reported = classify_errors(messages)
        summary = (
            f"{totals['databases']} database{'s' if totals['databases'] != 1 else ''}, "
            f"{totals['tables']} table{'s' if totals['tables'] != 1 else ''}, ~{totals['rows']:,} rows"
        )
        detail = f"Restored {summary}{note}" if status == "passed" else f"{note} ({summary})"
        return {"status": status, "detail": detail, "errors": reported, **totals}

    def cleanup(self) -> List[str]:
        """Drop everything this run created; returns problems (never raises)."""
        problems = []
        for name in self.scratch_dbs:
            try:
                self.drop_database(name)
            except (PostgresVerifyError, subprocess.TimeoutExpired, OSError) as exc:
                problems.append(f"could not drop {name}: {exc}")
        self.scratch_dbs = []
        roles, self._created_roles = self._created_roles, set()
        if roles:
            try:
                failed = self.drop_roles(list(roles))
                if failed:
                    problems.append(f"could not drop roles: {', '.join(failed)}")
            except (PostgresVerifyError, subprocess.TimeoutExpired, OSError) as exc:
                problems.append(str(exc))
        return problems


class _SegmentRunner:
    """Feeds dump lines to one psql process per database segment."""

    def __init__(self, tester: "PostgresRestoreTester", errors_file, totals: Dict[str, int]):
        self.tester = tester
        self.errors_file = errors_file
        self.totals = totals
        self.segment: Optional[Segment] = None
        self.proc = None
        self.scratch: Optional[str] = None
        self.buffered: List[bytes] = []
        self.encoding = "UTF8"

    def feed(self, segment: Segment, line: bytes) -> None:
        if segment is not self.segment:
            self.finish()
            self.segment = segment
            self.encoding = "UTF8"
        stripped = line.lstrip()
        if self.proc is None and not segment.is_globals:
            m = ENCODING_RE.search(line) if CREATE_DATABASE_RE.match(line) else CLIENT_ENCODING_RE.match(line)
            if m:
                self.encoding = m.group(1).decode()
        # Database switching and creation are handled by us; tablespace paths
        # from the source host do not exist here.
        if (stripped.startswith(b"\\connect") or CREATE_DATABASE_RE.match(line)
                or stripped.startswith(b"CREATE TABLESPACE ")):
            return
        # Never let the dump alter the login the verifier itself uses.
        for regex in (CREATE_ROLE_RE, ALTER_ROLE_RE):
            m = regex.match(line)
            if m and unquote_ident(m.group(1)) == self.tester.user:
                return
        if self.proc is None:
            # Hold the prologue until the first real statement so the scratch
            # database can be created with the dump's encoding.
            if not segment.is_globals and _is_prologue(stripped):
                self.buffered.append(line)
                return
            self._start()
        self._write(line)

    def _start(self) -> None:
        if self.segment.is_globals:
            target = self.tester.maintenance_db
        else:
            self.scratch = self.tester.create_scratch_database(self.encoding)
            self.totals["databases"] += 1
            target = self.scratch
        self.proc = self.tester._start_psql(target, self.errors_file)
        pending, self.buffered = self.buffered, []
        for line in pending:
            self._write(line)

    def _write(self, line: bytes) -> None:
        if self.segment.name and self.scratch and DATABASE_REF_LINE_RE.match(line):
            line = rewrite_database_refs(line, self.segment.name, self.scratch)
        try:
            self.proc.stdin.write(line)
        except BrokenPipeError:
            raise PostgresVerifyError("psql stopped reading the dump (connection lost?)")

    def abort(self) -> None:
        if self.proc is not None:
            self.proc.kill()
            self.proc.wait()
            self.proc = None

    def finish(self) -> None:
        # A segment with only prologue lines (e.g. an empty template1) has
        # nothing to restore.
        if self.proc is not None:
            self.tester._finish_psql(self.proc)
            if self.scratch:
                tables, rows = self.tester.database_stats(self.scratch)
                self.totals["tables"] += tables
                self.totals["rows"] += rows
        self.proc = None
        self.scratch = None
        self.buffered = []


def _is_prologue(stripped: bytes) -> bool:
    return (
        not stripped
        or stripped.startswith((b"--", b"SET ", b"\\restrict", b"\\unrestrict"))
        or stripped.startswith(b"SELECT pg_catalog.set_config(")
        or DATABASE_REF_LINE_RE.match(stripped) is not None
    )
