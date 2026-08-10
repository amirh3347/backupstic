import gzip
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class BackupScriptIntegrationTests(unittest.TestCase):
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
