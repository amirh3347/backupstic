import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault('PROFILE_SECRET_KEY', 'p' * 48)

from backend.profiles import ProfileStorage, sanitize_profile, validate_profile


class ProfileStorageTests(unittest.TestCase):
    def test_secrets_are_encrypted_and_sanitized(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / 'profiles.json'
            storage = ProfileStorage(str(path))
            profile = storage.create({
                'name': 'main-db',
                'type': 'postgresql',
                'host': 'db.internal',
                'port': 5432,
                'username': 'backup',
                'password': 'database-secret',
                'databases': ['app'],
                'schedule': '0 2 * * *',
            })
            self.assertNotIn('database-secret', path.read_text())
            self.assertEqual(storage.get(profile['id'])['password'], 'database-secret')
            self.assertNotIn('password', sanitize_profile(storage.get(profile['id'])))

    def test_database_path_traversal_is_rejected(self):
        candidate = {
            'name': 'unsafe',
            'type': 'postgresql',
            'host': 'db.internal',
            'port': 5432,
            'username': 'backup',
            'password': 'secret',
            'databases': ['../../escape'],
            'schedule': '0 2 * * *',
        }
        valid, _ = validate_profile(candidate)
        self.assertFalse(valid)


if __name__ == '__main__':
    unittest.main()
