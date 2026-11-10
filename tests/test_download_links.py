import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from backend.download_links import DownloadLinkStore, job_id_for, public_state


class DownloadLinkStoreTests(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.base, True)
        self.store = DownloadLinkStore(self.base)

    def ready_link(self, ttl=60):
        token, state = self.store.create({'format': 'plain', 'snapshot_id': 'ab' * 32})
        prepared = Path(self.base) / 'prepared.gz'
        prepared.write_bytes(b'data')
        self.store.mark_ready(state, str(prepared), 'app.sql.gz', ttl)
        return token, state

    def test_token_is_not_stored_and_maps_to_job(self):
        token, state = self.store.create({'format': 'plain'})
        self.assertEqual(job_id_for(token), state['id'])
        for path in Path(self.base).rglob('*'):
            if path.is_file():
                self.assertNotIn(token, path.read_text())
        self.assertNotIn('token', public_state(state))
        self.assertIsNone(job_id_for('../../etc/passwd'))

    def test_link_can_be_claimed_exactly_once(self):
        token, state = self.ready_link()
        path, claimed, error = self.store.claim(state['id'])
        self.assertEqual(error, '')
        self.assertEqual(Path(path).read_bytes(), b'data')
        self.assertEqual(claimed['status'], 'claimed')
        self.assertEqual(self.store.claim(state['id'])[2], 'used')

    def test_preparing_failed_and_expired_links_cannot_be_claimed(self):
        _, preparing = self.store.create({'format': 'plain'})
        self.assertEqual(self.store.claim(preparing['id'])[2], 'preparing')
        self.store.mark_failed(preparing, 'boom')
        self.assertEqual(self.store.claim(preparing['id'])[2], 'failed')
        _, ready = self.ready_link(ttl=60)
        ready['expires_at'] = time.time() - 1
        self.store.save(ready)
        self.assertEqual(self.store.claim(ready['id'])[2], 'expired')
        self.assertEqual(self.store.claim('0' * 64)[2], 'not_found')

    def test_job_whose_preparing_process_died_is_reported_failed_and_cleaned(self):
        _, state = self.store.create({'format': 'plain'})
        (self.store.job_dir(state['id']) / 'work').mkdir()
        dead = subprocess.Popen(['true'])
        dead.wait()
        state['pid'] = dead.pid
        self.store.save(state)

        loaded = self.store.load(state['id'])
        self.assertEqual(loaded['status'], 'failed')
        self.assertIn('restarted', loaded['error'])
        self.assertEqual(self.store.active_count(), 0)
        self.store.cleanup(max_prepare_seconds=3600)
        self.assertEqual(self.store.all(), [])

    def test_cleanup_removes_expired_and_abandoned_jobs_only(self):
        _, fresh = self.ready_link(ttl=600)
        _, expired = self.ready_link(ttl=600)
        expired['expires_at'] = time.time() - 1
        self.store.save(expired)
        _, abandoned = self.store.create({'format': 'plain'})
        abandoned['created_at'] = time.time() - 7200
        self.store.save(abandoned)
        _, preparing = self.store.create({'format': 'plain'})

        self.store.cleanup(max_prepare_seconds=3600)

        remaining = {state['id'] for state in self.store.all()}
        self.assertEqual(remaining, {fresh['id'], preparing['id']})
        self.assertEqual(self.store.active_count(), 1)


if __name__ == '__main__':
    unittest.main()
