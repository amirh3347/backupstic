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

from frontend.app import app, ssh_base_command, valid_cron_expression


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


if __name__ == '__main__':
    unittest.main()
