import os
import tempfile
import unittest

_runtime = tempfile.TemporaryDirectory()
os.environ.setdefault('JWT_SECRET_KEY', 'j' * 48)
os.environ.setdefault('PROFILE_SECRET_KEY', 'p' * 48)
os.environ.setdefault('RESTIC_PASSWORD', 'r' * 48)
os.environ.setdefault('ADMIN_USERNAME', 'admin')
os.environ.setdefault('ADMIN_PASSWORD', 'correct horse battery staple')
os.environ['BACKUP_BASE'] = _runtime.name
os.environ['PROFILES_STORAGE'] = os.path.join(_runtime.name, 'profiles.json')
os.environ['RESTIC_REPOSITORIES_STORAGE'] = os.path.join(_runtime.name, 'repositories.json')

from frontend.app import app, valid_cron_expression


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True)
        self.client = app.test_client()

    def test_health_and_security_headers(self):
        response = self.client.get('/api/health')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(response.headers['X-Frame-Options'], 'DENY')

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
