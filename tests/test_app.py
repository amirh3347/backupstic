import os
import re
import tempfile
import unittest
from unittest.mock import patch

_runtime = tempfile.TemporaryDirectory()
os.environ.setdefault('JWT_SECRET_KEY', 'j' * 48)
os.environ.setdefault('PROFILE_SECRET_KEY', 'p' * 48)
os.environ.setdefault('RESTIC_PASSWORD', 'r' * 48)
os.environ.setdefault('ADMIN_USERNAME', 'admin')
os.environ.setdefault('ADMIN_PASSWORD', 'correct horse battery staple')
os.environ['BACKUP_BASE'] = _runtime.name
os.environ['PROFILES_STORAGE'] = os.path.join(_runtime.name, 'profiles.json')
os.environ['RESTIC_REPOSITORIES_STORAGE'] = os.path.join(_runtime.name, 'repositories.json')

from frontend.app import app, ssh_base_command, start_background, valid_cron_expression


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True)
        self.client = app.test_client()

    def test_health_and_security_headers(self):
        response = self.client.get('/api/health')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(response.headers['X-Frame-Options'], 'DENY')

    def test_favicon_is_available(self):
        response = self.client.get('/favicon.ico')
        self.addCleanup(response.close)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, 'image/svg+xml')

    def test_hidden_ssh_path_does_not_block_other_profile_types(self):
        response = self.client.get('/')
        self.addCleanup(response.close)
        html = response.get_data(as_text=True)
        ssh_path_input = re.search(r'<input[^>]+class="ssh-path-value"[^>]*>', html)
        self.assertIsNotNone(ssh_path_input)
        self.assertNotIn('required', ssh_path_input.group(0))

    @patch('frontend.app.shutil.which', return_value='/usr/bin/sshpass')
    def test_new_ssh_hosts_are_automatically_enrolled(self, _which):
        command, _, error = ssh_base_command({
            'ssh_host': 'new-host.example',
            'ssh_port': 22,
            'ssh_user': 'backup',
            'auth_method': 'password',
            'ssh_password': 'secret',
        })
        self.assertEqual(error, '')
        self.assertIn('StrictHostKeyChecking=accept-new', command)

    def test_login(self):
        response = self.client.post('/api/login', json={
            'username': 'admin',
            'password': 'correct horse battery staple',
        })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()['token'])

    def test_cron_validation(self):
        self.assertTrue(valid_cron_expression('0 2 * * *'))
        self.assertFalse(valid_cron_expression('* * * * *\nmalicious'))
        self.assertFalse(valid_cron_expression('@daily'))

    @patch('frontend.app.subprocess.Popen')
    def test_manual_backup_uses_bounded_log_runner(self, popen):
        start_background(['/scripts/backup-all.sh', 'full'])

        command = popen.call_args.args[0]
        self.assertIn('/scripts/run-with-rotating-log.py', command)
        self.assertIn('--max-bytes', command)
        self.assertIn('--backup-count', command)
        self.assertEqual(command[-2:], ['/scripts/backup-all.sh', 'full'])

    def test_profile_mutations_rebuild_grouped_crontab(self):
        login = self.client.post('/api/login', json={
            'username': 'admin',
            'password': 'correct horse battery staple',
        })
        headers = {'Authorization': f"Bearer {login.get_json()['token']}"}
        profile_ids = []

        try:
            for name in ('redis one', 'redis two'):
                response = self.client.post('/api/profiles', headers=headers, json={
                    'name': name,
                    'type': 'redis',
                    'host': 'redis.internal',
                    'port': 6379,
                    'password': '',
                    'enabled': True,
                    'schedule': '0 2 * * *',
                    'repository_id': 'default',
                })
                self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
                profile_ids.append(response.get_json()['profile']['id'])

            schedule_path = os.path.join(_runtime.name, 'schedules.cron')
            with open(schedule_path, encoding='utf-8') as handle:
                schedule = handle.read()
            job_lines = [line for line in schedule.splitlines() if not line.startswith('#')]
            self.assertEqual(len(job_lines), 1)
            self.assertIn('/scripts/backup-profile-batch.sh', job_lines[0])
            self.assertTrue(all(profile_id in job_lines[0] for profile_id in profile_ids))
            self.assertNotIn('backup-all.sh full', schedule)

            response = self.client.put(
                f'/api/profiles/{profile_ids[0]}',
                headers=headers,
                json={'schedule': '30 3 * * *'},
            )
            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
            with open(schedule_path, encoding='utf-8') as handle:
                schedule = handle.read()
            job_lines = [line for line in schedule.splitlines() if not line.startswith('#')]
            self.assertEqual(len(job_lines), 2)
            self.assertIn('30 3 * * *', schedule)
        finally:
            for profile_id in profile_ids:
                self.client.delete(f'/api/profiles/{profile_id}', headers=headers)


if __name__ == '__main__':
    unittest.main()
