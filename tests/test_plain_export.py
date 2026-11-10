import gzip
import os
import shutil
import tarfile
import tempfile
import unittest
from pathlib import Path

from backend import plain_export


STAGE = 'var/backups/.staging/profile-1'


class PlainExportTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.base, True)
        self.restore = self.base / 'data'
        self.stage = self.restore / STAGE
        self.stage.mkdir(parents=True)
        self.snapshot = {
            'id': 'ab12cd34' + '0' * 56,
            'time': '2026-03-04T05:06:07.123456789+00:00',
            'paths': [f'/{STAGE}'],
            'profile_name': 'پایگاه داده main',
        }

    def build(self):
        return plain_export.build(str(self.restore), self.snapshot, str(self.base))

    def test_single_gzip_dump_is_served_as_is(self):
        dump = self.stage / 'app_20260304_050607.sql.gz'
        dump.write_bytes(gzip.compress(b'-- dump\n'))
        path, name = self.build()
        self.assertEqual(Path(path), dump)
        self.assertEqual(name, 'app_20260304_050607.sql.gz')

    def test_manifest_sidecar_does_not_hide_the_single_archive(self):
        archive = self.stage / 'configs_20260304.tar.gz'
        archive.write_bytes(gzip.compress(b'x'))
        (self.stage / 'configs_20260304.manifest.json').write_text('{}')
        path, _ = self.build()
        self.assertEqual(Path(path), archive)

    def test_several_files_become_one_flat_tar_gz(self):
        (self.stage / 'a.sql.gz').write_bytes(gzip.compress(b'a'))
        (self.stage / 'b.sql.gz').write_bytes(gzip.compress(b'b'))
        (self.stage / 'nested').mkdir()
        (self.stage / 'nested' / 'c.txt').write_text('c')

        path, name = self.build()

        self.assertEqual(name, 'main_20260304_050607_ab12cd34.tar.gz')
        with tarfile.open(path, 'r:gz') as archive:
            names = sorted(archive.getnames())
        self.assertEqual(names, ['a.sql.gz', 'b.sql.gz', 'nested', 'nested/c.txt'])

    def test_multiple_snapshot_paths_use_their_common_parent(self):
        for sub in ('postgresql', 'redis'):
            (self.restore / 'var/backups' / sub).mkdir(parents=True)
            (self.restore / 'var/backups' / sub / f'{sub}.gz').write_bytes(gzip.compress(b'x'))
        root = plain_export.content_root(str(self.restore), ['/var/backups/postgresql', '/var/backups/redis'])
        self.assertEqual(Path(root), self.restore / 'var/backups')

    def test_unknown_layout_skips_single_wrapper_directories(self):
        (self.stage / 'only.txt').write_text('x')
        root = plain_export.content_root(str(self.restore), [])
        self.assertEqual(Path(root), self.stage)

    def test_paths_outside_restore_dir_are_ignored(self):
        (self.stage / 'only.txt').write_text('x')
        root = plain_export.content_root(str(self.restore), ['/../../etc'])
        self.assertTrue(os.path.realpath(root).startswith(os.path.realpath(self.restore)))

    def test_empty_snapshot_fails(self):
        with self.assertRaises(plain_export.PlainExportError):
            self.build()


if __name__ == '__main__':
    unittest.main()
