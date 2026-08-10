import os
import unittest

import bcrypt

os.environ.setdefault('JWT_SECRET_KEY', 'j' * 48)
os.environ.setdefault('ADMIN_USERNAME', 'admin')
os.environ.setdefault('ADMIN_PASSWORD', 'correct horse battery staple')

from backend.auth import AuthManager


class AuthManagerTests(unittest.TestCase):
    def test_plain_password_and_token(self):
        manager = AuthManager()
        self.assertTrue(manager.authenticate_user('admin', 'correct horse battery staple'))
        self.assertFalse(manager.authenticate_user('admin', 'wrong'))
        payload = manager.verify_token(manager.generate_token('admin'))
        self.assertEqual(payload['sub'], 'admin')

    def test_bcrypt_password(self):
        password = b'a different strong password'
        old_plain = os.environ.pop('ADMIN_PASSWORD', None)
        os.environ['ADMIN_PASSWORD_HASH'] = bcrypt.hashpw(password, bcrypt.gensalt()).decode()
        try:
            manager = AuthManager()
            self.assertTrue(manager.authenticate_user('admin', password.decode()))
            self.assertFalse(manager.authenticate_user('admin', 'wrong'))
        finally:
            os.environ.pop('ADMIN_PASSWORD_HASH', None)
            if old_plain is not None:
                os.environ['ADMIN_PASSWORD'] = old_plain

    def test_short_jwt_secret_is_rejected(self):
        old_secret = os.environ['JWT_SECRET_KEY']
        os.environ['JWT_SECRET_KEY'] = 'short'
        try:
            with self.assertRaises(RuntimeError):
                AuthManager()
        finally:
            os.environ['JWT_SECRET_KEY'] = old_secret


if __name__ == '__main__':
    unittest.main()
