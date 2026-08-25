import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNNER = PROJECT_ROOT / 'scripts' / 'run-with-rotating-log.py'


class RotatingLogTests(unittest.TestCase):
    def run_logged(self, log_path, text, max_bytes=128, backup_count=2):
        return subprocess.run(
            [
                sys.executable,
                str(RUNNER),
                '--log-file',
                str(log_path),
                '--max-bytes',
                str(max_bytes),
                '--backup-count',
                str(backup_count),
                '--',
                sys.executable,
                '-c',
                'import sys; sys.stdout.write(sys.argv[1])',
                text,
            ],
            capture_output=True,
            timeout=20,
            check=False,
        )

    def test_rotates_between_runs_and_bounds_every_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / 'manual_backup.log'
            first = self.run_logged(log_path, 'first-' + ('a' * 90))
            second = self.run_logged(log_path, 'second-' + ('b' * 90))

            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertIn(b'second-', log_path.read_bytes())
            self.assertIn(b'first-', Path(f'{log_path}.1').read_bytes())
            for path in log_path.parent.glob('manual_backup.log*'):
                if path.name.endswith('.lock'):
                    continue
                self.assertLessEqual(path.stat().st_size, 128)

    def test_compacts_a_preexisting_oversized_log(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / 'manual_backup.log'
            log_path.write_bytes(b'x' * 10_000)

            result = self.run_logged(log_path, 'done', max_bytes=128)

            self.assertEqual(result.returncode, 0, result.stderr)
            for path in log_path.parent.glob('manual_backup.log*'):
                if path.name.endswith('.lock'):
                    continue
                self.assertLessEqual(path.stat().st_size, 128)

    def test_returns_the_wrapped_command_status(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / 'manual_backup.log'
            result = subprocess.run(
                [
                    sys.executable,
                    str(RUNNER),
                    '--log-file',
                    str(log_path),
                    '--max-bytes',
                    '128',
                    '--backup-count',
                    '2',
                    '--',
                    sys.executable,
                    '-c',
                    'import sys; print("failed"); sys.exit(23)',
                ],
                capture_output=True,
                timeout=20,
                check=False,
            )

            self.assertEqual(result.returncode, 23)
            self.assertIn('failed', log_path.read_text())


if __name__ == '__main__':
    unittest.main()
