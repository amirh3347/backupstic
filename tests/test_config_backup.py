import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
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
from backend import config_backup  # noqa: E402
from backend.profiles import ProfileStorage, validate_profile  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def redis_profile(pid, name, repo='offsite', **extra):
    return {
        'id': pid, 'name': name, 'type': 'redis', 'host': 'redis.internal', 'port': 6379,
        'password': f'secret-{name}', 'schedule': '0 2 * * *', 'repository_id': repo, **extra,
    }


class ImportPlanTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.storage = ProfileStorage(os.path.join(self.dir, 'profiles.json'))
        self.document = {
            'format': 'backupstic-config', 'version': 1,
            'profiles': [
                redis_profile('aaaa-1', 'cache'),
                redis_profile('bbbb-2', 'sessions'),
                redis_profile('cccc-3', 'queue'),
                {'id': '../evil', 'name': 'x', 'type': 'redis'},
            ],
        }

    def test_plan_reports_new_existing_conflicting_and_invalid(self):
        self.storage.put(redis_profile('aaaa-1', 'cache', repo='local'))
        self.storage.put(redis_profile('zzzz-9', 'sessions', repo='local'))

        plan = config_backup.plan_import(self.document, self.storage.get_all(), 'new-repo',
                                         validate_profile, {'cccc-3': 4})

        status = {item['id']: item['status'] for item in plan}
        self.assertEqual(status, {'aaaa-1': 'exists', 'bbbb-2': 'name_conflict',
                                  'cccc-3': 'new', '../evil': 'invalid'})
        self.assertEqual(next(i for i in plan if i['id'] == 'cccc-3')['backups'], 4)
        self.assertNotIn('password', json.dumps(plan))

    def test_apply_keeps_ids_secrets_and_points_profiles_at_the_new_repository(self):
        self.storage.put(redis_profile('aaaa-1', 'cache', repo='local', port=1111))

        results = config_backup.apply_import(self.document, self.storage, 'new-repo', validate_profile)

        by_id = {r['id']: r['result'] for r in results}
        self.assertEqual(by_id, {'aaaa-1': 'skipped', 'bbbb-2': 'restored', 'cccc-3': 'restored', '../evil': 'failed'})
        restored = self.storage.get('cccc-3')
        self.assertEqual(restored['repository_id'], 'new-repo')
        self.assertEqual(restored['password'], 'secret-queue')
        self.assertEqual(self.storage.get('aaaa-1')['port'], 1111)

        config_backup.apply_import(self.document, self.storage, 'new-repo', validate_profile,
                                   selected_ids=['aaaa-1'], overwrite=True)
        self.assertEqual(self.storage.get('aaaa-1')['port'], 6379)
        self.assertEqual(self.storage.get('aaaa-1')['repository_id'], 'new-repo')

    def test_selected_ids_limit_the_restore(self):
        results = config_backup.apply_import(self.document, self.storage, 'r', validate_profile,
                                             selected_ids=['cccc-3'])
        self.assertEqual([r['id'] for r in results], ['cccc-3'])
        self.assertIsNone(self.storage.get('bbbb-2'))


@unittest.skipUnless(shutil.which('restic'), 'restic is not installed')
class ConfigSnapshotResticTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.repo = {'id': 'offsite', 'repository': str(self.dir / 'repo'), 'password': 'offsite-password-123'}
        self.env = config_backup.restic_env(self.repo)
        subprocess.run(['restic', 'init'], env=self.env, check=True, capture_output=True)
        self.state = config_backup.ConfigSyncState(str(self.dir))
        self.profiles = [redis_profile('aaaa-1', 'cache'), redis_profile('bbbb-2', 'other', repo='default')]

    def sync(self, **kwargs):
        return config_backup.sync_repository(self.repo, lambda: self.profiles, self.state, self.env, **kwargs)

    def test_saves_only_on_change_and_round_trips_with_secrets(self):
        self.state.mark_dirty('offsite')
        self.assertEqual(self.sync(), 'saved')
        self.assertFalse(self.state.is_dirty('offsite'))
        self.assertEqual(self.sync(), 'unchanged')
        self.assertEqual(len(config_backup.list_config_snapshots(self.env)), 1)

        self.profiles[0]['port'] = 7000
        self.assertEqual(self.sync(), 'saved')
        snapshots = config_backup.list_config_snapshots(self.env)
        self.assertEqual(len(snapshots), 2)

        latest = config_backup.read_document(self.env, snapshots[0]['id'])
        self.assertEqual([p['id'] for p in latest['profiles']], ['aaaa-1'])  # only this repository's
        self.assertEqual(latest['profiles'][0]['port'], 7000)
        self.assertEqual(latest['profiles'][0]['password'], 'secret-cache')
        self.assertNotIn('password', latest['repository'])
        self.assertEqual(config_backup.read_document(self.env, snapshots[1]['id'])['profiles'][0]['port'], 6379)

    def test_keep_limits_old_versions(self):
        for port in range(3):
            self.profiles[0]['port'] = 7000 + port
            self.sync(keep=2)
        self.assertEqual(len(config_backup.list_config_snapshots(self.env)), 2)

    def test_failure_leaves_repository_dirty_for_retry(self):
        broken = dict(self.env, RESTIC_PASSWORD='wrong-password')
        with self.assertRaises(config_backup.ConfigBackupError):
            config_backup.sync_repository(self.repo, lambda: self.profiles, self.state, broken)
        self.assertTrue(self.state.is_dirty('offsite'))
        self.assertIsNone(self.state.last('offsite'))

    def test_wrong_password_cannot_read_configuration(self):
        self.sync()
        with self.assertRaises(config_backup.ConfigBackupError):
            config_backup.list_config_snapshots(dict(self.env, RESTIC_PASSWORD='nope'))


@unittest.skipUnless(shutil.which('restic') and shutil.which('jq'), 'restic and jq are required')
class DisasterRecoveryTests(unittest.TestCase):
    """Server A backs up into an off-site repository and is lost; server B restores from it."""

    offsite_password = 'offsite-repository-password'

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.offsite = self.dir / 'offsite-repo'
        self.server_b = self.dir / 'server-b'
        self.server_b.mkdir()
        patcher = patch.object(app_module, 'BACKUP_BASE', str(self.server_b))
        patcher.start()
        self.addCleanup(patcher.stop)
        app_module.app.config.update(TESTING=True)
        self.client = app_module.app.test_client()
        token = self.client.post('/api/login', json={
            'username': os.environ['ADMIN_USERNAME'], 'password': os.environ['ADMIN_PASSWORD'],
        }).get_json()['token']
        self.headers = {'Authorization': f'Bearer {token}'}

    def run_server_a(self):
        """Real backup-all.sh run on 'server A' with its own volume and secret key."""
        base = self.dir / 'server-a'
        fake_bin = self.dir / 'bin'
        fake_bin.mkdir()
        redis_cli = fake_bin / 'redis-cli'
        redis_cli.write_text('#!/bin/sh\nfor last; do :; done\nprintf "REDIS0011\\376\\000\\377" > "$last"\n')
        redis_cli.chmod(redis_cli.stat().st_mode | stat.S_IXUSR)
        env = dict(
            os.environ,
            PATH=f"{fake_bin}:{Path(sys.executable).parent}:{os.environ['PATH']}",
            BACKUP_BASE=str(base),
            PROFILES_STORAGE=str(base / 'profiles.json'),
            RESTIC_REPOSITORIES_STORAGE=str(base / 'repositories.json'),
            RESTIC_REPO=str(base / 'local-repo'),
            RESTIC_PASSWORD='a' * 48,
            PROFILE_SECRET_KEY='server-a-secret-key-that-is-long-enough!!',
            JWT_SECRET_KEY='k' * 48,
        )
        setup = (
            'import sys; sys.path.insert(0, "backend")\n'
            'from profiles import get_storage, get_repository_storage\n'
            'repo = get_repository_storage().create({"name": "Offsite S3", "type": "local",'
            f' "repository": "{self.offsite}", "password": "{self.offsite_password}"}})\n'
            'for name in ("cache", "sessions"):\n'
            '    p = get_storage().create({"name": name, "type": "redis", "host": "redis.internal",'
            ' "port": 6379, "password": "pw-" + name, "schedule": "15 3 * * *",'
            ' "repository_id": repo["id"], "verify_after_backup": True})\n'
            '    print(p["id"])\n'
        )
        created = subprocess.run([sys.executable, '-c', setup], cwd=PROJECT_ROOT, env=env,
                                 capture_output=True, text=True, check=True)
        profile_ids = created.stdout.split()
        outputs = []
        for profile_id in profile_ids:
            result = subprocess.run(['bash', 'scripts/backup-all.sh', 'profile', profile_id],
                                    cwd=PROJECT_ROOT, env=env, capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            outputs.append(result.stdout)
        # The first backup saves the configuration; the second finds it unchanged.
        self.assertIn('Saved configuration snapshot to the repository', outputs[0])
        self.assertNotIn('configuration snapshot', outputs[1])
        return profile_ids

    def test_new_server_restores_profiles_and_sees_old_backups(self):
        profile_ids = self.run_server_a()
        shutil.rmtree(self.dir / 'server-a')  # the server is gone; only the off-site repository is left

        created = self.client.post('/api/restic-repositories', headers=self.headers, json={
            'name': 'Offsite again', 'type': 'local', 'repository': str(self.offsite),
            'password': self.offsite_password,
        })
        self.assertEqual(created.status_code, 200, created.get_data(as_text=True))
        repo_id = created.get_json()['repository']['id']
        storage = app_module.get_storage()
        self.addCleanup(lambda: [storage.delete(pid) for pid in profile_ids])
        self.addCleanup(app_module.get_repository_storage().delete, repo_id)

        def visible_backups():
            listing = self.client.get(f'/api/snapshots?active_profiles_only=1&repository_id={repo_id}',
                                      headers=self.headers).get_json()
            return listing['snapshots']

        self.assertEqual(visible_backups(), [])  # without profiles the dashboard shows nothing

        preview = self.client.get(f'/api/restic-repositories/{repo_id}/config-backups',
                                  headers=self.headers).get_json()
        self.assertTrue(preview['success'], preview)
        self.assertEqual(len(preview['snapshots']), 1)
        plan = {item['id']: item for item in preview['profiles']}
        self.assertEqual(set(plan), set(profile_ids))
        self.assertTrue(all(item['status'] == 'new' and item['backups'] == 1 for item in plan.values()), plan)

        restored = self.client.post(f'/api/restic-repositories/{repo_id}/config-backups/restore',
                                    headers=self.headers, json={}).get_json()
        self.assertEqual(restored['restored'], 2, restored)

        for pid in profile_ids:
            profile = storage.get(pid)  # same id, decrypted with server B's key
            self.assertEqual(profile['repository_id'], repo_id)
            self.assertEqual(profile['password'], f"pw-{profile['name']}")
            self.assertTrue(profile['verify_after_backup'])
        backups = visible_backups()
        self.assertEqual(sorted(s['profile_id'] for s in backups), sorted(profile_ids))
        self.assertFalse([s for s in backups if 'backupstic-config' in (s.get('tags') or [])])
        schedule = (self.server_b / 'schedules.cron').read_text()
        self.assertTrue(all(pid in schedule for pid in profile_ids))

        again = self.client.post(f'/api/restic-repositories/{repo_id}/config-backups/restore',
                                 headers=self.headers, json={}).get_json()
        self.assertEqual((again['restored'], again['skipped']), (0, 2))

        # Server B now keeps the configuration in the repository itself.
        self.assertEqual(app_module.sync_dirty_configs(), [(repo_id, 'saved')])
        repos = self.client.get('/api/restic-repositories', headers=self.headers).get_json()['repositories']
        info = next(r for r in repos if r['id'] == repo_id)['config_backup']
        self.assertEqual(info['profiles'], 2)
        self.assertFalse(info['pending'])

    def test_profile_changes_mark_repository_for_saving(self):
        response = self.client.post('/api/profiles', headers=self.headers, json={
            'name': 'dr-mark', 'type': 'redis', 'host': 'r', 'port': 6379, 'schedule': '0 2 * * *',
            'repository_id': 'default',
        })
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        pid = response.get_json()['profile']['id']
        self.addCleanup(app_module.get_storage().delete, pid)
        self.assertIn('default', app_module.config_sync_state().dirty())


if __name__ == '__main__':
    unittest.main()
