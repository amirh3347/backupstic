import gzip
import fcntl
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class BackupScriptIntegrationTests(unittest.TestCase):
    def test_startup_schedule_sync_groups_profiles_with_the_same_time(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            backup_base = Path(temp_dir) / 'backups'
            backup_base.mkdir()
            profiles = {
                'profiles': {
                    'profile-a': {
                        'id': 'profile-a',
                        'enabled': True,
                        'schedule': '0 2 * * *',
                    },
                    'profile-b': {
                        'id': 'profile-b',
                        'enabled': True,
                        'schedule': '0 2 * * *',
                    },
                    'profile-disabled': {
                        'id': 'profile-disabled',
                        'enabled': False,
                        'schedule': '0 2 * * *',
                    },
                }
            }
            (backup_base / 'profiles.json').write_text(json.dumps(profiles))
            env = os.environ.copy()
            env.update({
                'BACKUP_BASE': str(backup_base),
                'PROFILES_STORAGE': str(backup_base / 'profiles.json'),
                'SCRIPTS_DIR': '/scripts',
                'BACKUP_CRON': '0 2 * * * /scripts/backup-all.sh full',
            })

            result = subprocess.run(
                [sys.executable, 'scripts/sync-schedules.py'],
                cwd=PROJECT_ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            schedule = (backup_base / 'schedules.cron').read_text()
            job_lines = [line for line in schedule.splitlines() if not line.startswith('#')]
            self.assertEqual(len(job_lines), 1)
            self.assertIn('profile-a profile-b', job_lines[0])
            self.assertNotIn('profile-disabled', schedule)
            self.assertNotIn('backup-all.sh full', schedule)

    def test_busy_backup_lock_returns_failure_instead_of_silent_success(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            backup_base = Path(temp_dir) / 'backups'
            backup_base.mkdir()
            lock_path = backup_base / 'backup.lock'
            with open(lock_path, 'w') as lock_handle:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                env = os.environ.copy()
                env.update({
                    'BACKUP_BASE': str(backup_base),
                    'BACKUP_LOCK_WAIT_SECONDS': '0',
                })
                result = subprocess.run(
                    ['bash', 'scripts/backup-all.sh', 'postgres'],
                    cwd=PROJECT_ROOT,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=20,
                    check=False,
                )
            self.assertEqual(result.returncode, 75, result.stdout + result.stderr)
            self.assertIn('Timed out waiting for the backup lock', result.stdout)

    def test_monitor_handles_missing_backup_directories(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = Path(temp_dir)
            fake_bin = runtime / 'bin'
            fake_bin.mkdir()
            self._executable(
                fake_bin / 'restic',
                '#!/bin/sh\n'
                'case "$*" in\n'
                '  "snapshots --json"|"snapshots --latest 1 --json") printf "[]\\n" ;;\n'
                '  *) exit 0 ;;\n'
                'esac\n',
            )
            self._executable(
                fake_bin / 'jq',
                '#!/bin/sh\n'
                'case "$1" in\n'
                '  length) printf "0\\n" ;;\n'
                '  *) printf "null\\n" ;;\n'
                'esac\n',
            )

            backup_base = runtime / 'backups'
            (backup_base / 'restic-repo').mkdir(parents=True)
            (backup_base / 'restic-repo' / 'config').touch()
            env = os.environ.copy()
            env.update({
                'PATH': f"{fake_bin}:{env['PATH']}",
                'BACKUP_BASE': str(backup_base),
                'RESTIC_REPO': str(backup_base / 'restic-repo'),
                'RESTIC_PASSWORD': 'r' * 48,
            })

            result = subprocess.run(
                ['bash', 'scripts/monitor.sh'],
                cwd=PROJECT_ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            for database in ('postgresql', 'redis', 'mongodb', 'elasticsearch'):
                self.assertIn(f'WARNING: No {database} backups found', result.stdout)
            self.assertIn('Monitoring check completed', result.stdout)
            self.assertTrue(any((backup_base / 'reports').glob('report_*.txt')))

    def test_legacy_postgres_flow_uses_one_lock_and_scoped_storage(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = Path(temp_dir)
            fake_bin = runtime / 'bin'
            fake_bin.mkdir()
            self._executable(fake_bin / 'pg_dumpall', "#!/bin/sh\nprintf '%s\\n' '-- test dump'\n")
            self._executable(fake_bin / 'restic', '#!/bin/sh\nexit 0\n')

            backup_base = runtime / 'backups'
            env = os.environ.copy()
            env.update({
                'PATH': f"{fake_bin}:{Path(sys.executable).parent}:{env['PATH']}",
                'BACKUP_BASE': str(backup_base),
                'RESTIC_REPO': str(backup_base / 'restic-repo'),
                'RESTIC_PASSWORD': 'r' * 48,
                'PROFILE_SECRET_KEY': 'p' * 48,
                'JWT_SECRET_KEY': 'j' * 48,
                'RESTIC_REPOSITORIES_STORAGE': str(backup_base / 'repositories.json'),
                'PG_DATABASE': 'all',
            })

            result = subprocess.run(
                ['bash', 'scripts/backup-all.sh', 'postgres'],
                cwd=PROJECT_ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            dumps = list((backup_base / 'postgresql').glob('*.sql.gz'))
            self.assertEqual(len(dumps), 1)
            with gzip.open(dumps[0], 'rt') as handle:
                self.assertIn('-- test dump', handle.read())
            self.assertFalse((backup_base / 'backup.state').exists())
            self.assertTrue((backup_base / 'repositories.json').exists())

    @staticmethod
    def _executable(path, contents):
        path.write_text(contents)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)


if __name__ == '__main__':
    unittest.main()
