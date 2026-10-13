import gzip
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

_runtime = tempfile.TemporaryDirectory()
os.environ.setdefault('JWT_SECRET_KEY', 'j' * 48)
os.environ.setdefault('PROFILE_SECRET_KEY', 'p' * 48)
os.environ.setdefault('RESTIC_PASSWORD', 'r' * 48)
os.environ.setdefault('ADMIN_USERNAME', 'admin')
os.environ.setdefault('ADMIN_PASSWORD', 'correct horse battery staple')
os.environ.setdefault('BACKUP_BASE', _runtime.name)
os.environ.setdefault('PROFILES_STORAGE', os.path.join(_runtime.name, 'profiles.json'))
os.environ.setdefault('RESTIC_REPOSITORIES_STORAGE', os.path.join(_runtime.name, 'repositories.json'))

import frontend.app as app_module  # noqa: E402
from backend.verification import VerificationStore, inspect_archive  # noqa: E402

PG_DUMP = (
    b'--\n-- PostgreSQL database dump\n--\n\nCREATE TABLE t (id int);\n'
    + b'INSERT INTO t VALUES (1);\n' * 2000
    + b'\n--\n-- PostgreSQL database dump complete\n--\n'
)
# Minimal valid RDB: SELECTDB 0, one string key, EOF, checksum disabled (zeros).
REDIS_RDB = b'REDIS0011' + b'\xfe\x00' + b'\x00\x03foo\x03bar' + b'\xff' + b'\x00' * 8


def write_tar_gz(path, files):
    with tarfile.open(path, 'w:gz') as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))


def write_healthy_backup(directory):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'app_20260101_000000.sql.gz').write_bytes(gzip.compress(PG_DUMP))
    (directory / 'redis_20260101_000000.rdb.gz').write_bytes(gzip.compress(REDIS_RDB))
    write_tar_gz(directory / 'configs_20260101_000000.tar.gz', {
        'etc/nginx/nginx.conf': b'worker_processes 1;\n' * 100,
        'etc/hosts': b'127.0.0.1 localhost\n',
    })
    (directory / 'configs_20260101_000000.manifest.json').write_text(json.dumps({'copied_paths': 2}))


class ArchiveInspectionTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        write_healthy_backup(self.dir)

    def inspect(self, name):
        return inspect_archive(str(self.dir / name), name)

    def test_healthy_files_pass(self):
        for name in os.listdir(self.dir):
            result = self.inspect(name)
            self.assertEqual(result['status'], 'passed', (name, result))

    def test_truncated_gzip_fails(self):
        path = self.dir / 'app_20260101_000000.sql.gz'
        path.write_bytes(path.read_bytes()[:-20])
        self.assertEqual(self.inspect(path.name)['status'], 'failed')

    def test_bit_flip_in_tar_gz_fails(self):
        path = self.dir / 'configs_20260101_000000.tar.gz'
        data = bytearray(path.read_bytes())
        data[len(data) // 2] ^= 0xFF
        path.write_bytes(bytes(data))
        self.assertEqual(self.inspect(path.name)['status'], 'failed')

    def test_postgres_dump_without_completion_marker_fails(self):
        path = self.dir / 'partial.sql.gz'
        path.write_bytes(gzip.compress(PG_DUMP[:len(PG_DUMP) // 2]))
        result = self.inspect(path.name)
        self.assertEqual(result['status'], 'failed')
        self.assertIn('truncated', result['detail'])

    def test_redis_rdb_header_and_trailer(self):
        (self.dir / 'bad.rdb.gz').write_bytes(gzip.compress(b'NOTREDIS' + b'\x00' * 64))
        self.assertEqual(self.inspect('bad.rdb.gz')['status'], 'failed')
        (self.dir / 'cut.rdb').write_bytes(REDIS_RDB[:-3])
        self.assertEqual(self.inspect('cut.rdb')['status'], 'failed')

    def test_empty_compressed_content_warns(self):
        (self.dir / 'empty.sql.gz').write_bytes(gzip.compress(b''))
        self.assertEqual(self.inspect('empty.sql.gz')['status'], 'warning')

    def test_zip_archives(self):
        good = self.dir / 'good.zip'
        with zipfile.ZipFile(good, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('a.txt', 'hello ' * 1000)
        self.assertEqual(self.inspect('good.zip')['status'], 'passed')
        data = bytearray(good.read_bytes())
        data[40] ^= 0xFF
        (self.dir / 'bad.zip').write_bytes(bytes(data))
        self.assertEqual(self.inspect('bad.zip')['status'], 'failed')

    def test_invalid_json_fails_and_unknown_files_are_ignored(self):
        (self.dir / 'broken.json').write_text('{"a": ')
        self.assertEqual(self.inspect('broken.json')['status'], 'failed')
        (self.dir / 'notes.txt').write_text('plain')
        self.assertIsNone(self.inspect('notes.txt'))


class VerificationStoreTests(unittest.TestCase):
    def test_active_job_without_lock_owner_is_reported_interrupted(self):
        with tempfile.TemporaryDirectory() as base:
            store = VerificationStore(base)
            snapshot_id = 'ab' * 32
            store.save({'snapshot_id': snapshot_id, 'status': 'running', 'steps': []})
            self.assertEqual(store.load(snapshot_id)['status'], 'interrupted')

            fd = store.try_acquire()
            self.assertIsNotNone(fd)
            try:
                self.assertIsNone(store.try_acquire())
                self.assertEqual(store.find(snapshot_id[:8])['status'], 'running')
            finally:
                VerificationStore.release(fd)

    def test_rejects_ids_that_are_not_snapshot_hashes(self):
        with tempfile.TemporaryDirectory() as base:
            store = VerificationStore(base)
            self.assertIsNone(store.find('../etc'))
            with self.assertRaises(ValueError):
                store.save({'snapshot_id': '../../x', 'status': 'queued'})


@unittest.skipUnless(shutil.which('restic'), 'restic is not installed')
class EndToEndVerificationTests(unittest.TestCase):
    """Runs the real pipeline: restic restore -> bundle -> extract -> decrypt -> test."""

    password = 'verification-test-password'

    def setUp(self):
        self.base = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.base, True)
        self.repo = {
            'id': 'e2e',
            'name': 'E2E Repository',
            'repository': str(self.base / 'repo'),
            'password': self.password,
        }
        self.restic(['init'])
        for name, value in (('BACKUP_BASE', str(self.base)), ('all_repositories', lambda: [self.repo])):
            patcher = patch.object(app_module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        app_module.app.config.update(TESTING=True)
        self.client = app_module.app.test_client()
        token = self.client.post('/api/login', json={
            'username': os.environ['ADMIN_USERNAME'],
            'password': os.environ['ADMIN_PASSWORD'],
        }).get_json()['token']
        self.headers = {'Authorization': f'Bearer {token}'}

    def restic(self, args):
        env = dict(os.environ, RESTIC_REPOSITORY=self.repo['repository'], RESTIC_PASSWORD=self.password)
        result = subprocess.run(['restic', '--no-cache', *args], env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def snapshot(self, staging):
        self.restic(['backup', str(staging), '--tag', 'profile:e2e', '--tag', 'e2e', '--tag', 'postgresql'])
        return json.loads(self.restic(['snapshots', '--json']))[-1]['id']

    def verify(self, snapshot_id):
        response = self.client.post(f'/api/backup/{snapshot_id[:8]}/verify', headers=self.headers)
        self.assertEqual(response.status_code, 202, response.get_data(as_text=True))
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            state = self.client.get(f'/api/backup/{snapshot_id[:8]}/verify', headers=self.headers).get_json()['verification']
            if state['status'] not in ('queued', 'running'):
                return state
            time.sleep(0.2)
        self.fail('verification did not finish')

    def steps(self, state):
        return {step['key']: step['status'] for step in state['steps']}

    def test_healthy_backup_passes_every_step(self):
        staging = self.base / '.staging' / 'e2e'
        write_healthy_backup(staging)
        snapshot_id = self.snapshot(staging)

        state = self.verify(snapshot_id)

        self.assertEqual(state['status'], 'passed', json.dumps(state, indent=2))
        steps = self.steps(state)
        self.assertEqual(steps.pop('postgres'), 'skipped')
        self.assertTrue(all(status == 'passed' for status in steps.values()), steps)
        self.assertEqual(state['summary']['archives_total'], 4)
        self.assertFalse([name for name in os.listdir(self.base) if name.startswith('verify_')])
        self.assertFalse((self.base / 'backup.state').exists())

        listing = self.client.get('/api/verifications', headers=self.headers).get_json()
        self.assertEqual(listing['verifications'][snapshot_id]['status'], 'passed')

    def test_corrupt_archive_inside_backup_fails_archive_step(self):
        staging = self.base / '.staging' / 'e2e'
        write_healthy_backup(staging)
        dump = staging / 'app_20260101_000000.sql.gz'
        dump.write_bytes(dump.read_bytes()[:-50])
        snapshot_id = self.snapshot(staging)

        state = self.verify(snapshot_id)

        self.assertEqual(state['status'], 'failed')
        steps = self.steps(state)
        self.assertEqual(steps['decrypt'], 'passed')
        self.assertEqual(steps['archives'], 'failed')
        self.assertEqual(state['archives'][0]['path'], 'app_20260101_000000.sql.gz')

    def test_damaged_repository_fails_restore_step(self):
        staging = self.base / '.staging' / 'e2e'
        write_healthy_backup(staging)
        snapshot_id = self.snapshot(staging)
        for pack in (self.base / 'repo' / 'data').rglob('*'):
            if pack.is_file():
                pack.chmod(0o600)
                pack.write_bytes(b'garbage')

        state = self.verify(snapshot_id)

        self.assertEqual(state['status'], 'failed')
        steps = self.steps(state)
        self.assertEqual(steps['restore'], 'failed')
        self.assertEqual(steps['archives'], 'skipped')

    def test_download_still_returns_encrypted_bundle(self):
        staging = self.base / '.staging' / 'e2e'
        write_healthy_backup(staging)
        snapshot_id = self.snapshot(staging)

        response = self.client.get(f'/api/backup/{snapshot_id[:8]}/download', headers=self.headers)
        self.addCleanup(response.close)

        self.assertEqual(response.status_code, 200)
        with tarfile.open(fileobj=io.BytesIO(response.get_data()), mode='r:gz') as bundle:
            names = bundle.getnames()
        self.assertIn('README_DECRYPT.txt', names)
        self.assertIn('encrypted-restic-repo/config', names)

    def test_waits_for_running_backup_and_times_out(self):
        import fcntl

        staging = self.base / '.staging' / 'e2e'
        write_healthy_backup(staging)
        snapshot_id = self.snapshot(staging)
        with open(self.base / 'backup.lock', 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with patch.object(app_module, 'VERIFY_LOCK_WAIT_SECONDS', 1):
                state = self.verify(snapshot_id)

        self.assertEqual(state['status'], 'failed')
        self.assertIn('Timed out waiting', state['steps'][0]['detail'])

    @unittest.skipUnless(os.environ.get('VERIFY_POSTGRES_TEST_URL') and shutil.which('psql'),
                         'test PostgreSQL server not configured')
    def test_postgres_step_restores_dump_and_catches_broken_sql(self):
        staging = self.base / '.staging' / 'e2e'
        staging.mkdir(parents=True)
        dump = staging / 'app_20260101_000000.sql.gz'
        dump.write_bytes(gzip.compress(PG_DUMP))
        good_id = self.snapshot(staging)
        # Valid gzip with a completion marker, but SQL PostgreSQL cannot replay.
        dump.write_bytes(gzip.compress(PG_DUMP.replace(b'CREATE TABLE t', b'CREATE TABEL t')))
        bad_id = self.snapshot(staging)

        with patch.object(app_module, 'VERIFY_POSTGRES_URL', os.environ['VERIFY_POSTGRES_TEST_URL']):
            good = self.verify(good_id)
            bad = self.verify(bad_id)

        self.assertEqual(good['status'], 'passed', json.dumps(good, indent=2))
        self.assertEqual(good['postgres'][0]['tables'], 1)
        self.assertEqual(self.steps(bad)['archives'], 'passed')
        self.assertEqual(self.steps(bad)['postgres'], 'failed')
        self.assertIn('syntax error', bad['postgres'][0]['errors'][0])

    def test_unknown_snapshot_and_bad_ids(self):
        self.assertEqual(self.client.post('/api/backup/deadbeef/verify', headers=self.headers).status_code, 404)
        self.assertEqual(self.client.post('/api/backup/--help/verify', headers=self.headers).status_code, 400)
        self.assertEqual(self.client.get('/api/backup/deadbeef/verify', headers=self.headers).status_code, 404)


if __name__ == '__main__':
    unittest.main()
