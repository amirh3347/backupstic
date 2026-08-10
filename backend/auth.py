#!/usr/bin/env python3
"""Authentication module for backup dashboard"""

import datetime
import hmac
import os

import bcrypt
import jwt
from functools import wraps
from flask import request, jsonify, current_app

class AuthManager:
    def __init__(self):
        self.secret_key = os.environ.get('JWT_SECRET_KEY', '')
        if len(self.secret_key) < 32:
            raise RuntimeError('JWT_SECRET_KEY must contain at least 32 characters')
        self.algorithm = 'HS256'
        self.access_token_expires = int(os.environ.get('JWT_ACCESS_TOKEN_EXPIRES', 3600))  # 1 hour
        self.username = os.environ.get('ADMIN_USERNAME', '').strip()
        self.password = os.environ.get('ADMIN_PASSWORD', '')
        self.password_hash = os.environ.get('ADMIN_PASSWORD_HASH', '')
        if not self.username:
            raise RuntimeError('ADMIN_USERNAME is required')
        if not self.password and not self.password_hash:
            raise RuntimeError('ADMIN_PASSWORD or ADMIN_PASSWORD_HASH is required')

    def authenticate_user(self, username, password):
        """Authenticate a user with username and password"""
        if not hmac.compare_digest(str(username), self.username):
            return False
        if self.password_hash:
            try:
                return bcrypt.checkpw(password.encode(), self.password_hash.encode())
            except (ValueError, TypeError):
                return False
        return hmac.compare_digest(str(password), self.password)

    def generate_token(self, username):
        """Generate a JWT token for the user"""
        payload = {
            'username': username,
            'sub': username,
            'exp': datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=self.access_token_expires),
            'iat': datetime.datetime.now(datetime.timezone.utc),
            'iss': 'backupstic',
        }
        return jwt.encode(payload, self.secret_key, algorithm=self.algorithm)

    def verify_token(self, token):
        """Verify and decode a JWT token"""
        try:
            payload = jwt.decode(
                token,
                self.secret_key,
                algorithms=[self.algorithm],
                issuer='backupstic',
                options={'require': ['exp', 'iat', 'iss', 'sub']},
            )
            return payload
        except jwt.ExpiredSignatureError:
            raise jwt.ExpiredSignatureError('Token has expired')
        except jwt.InvalidTokenError:
            raise jwt.InvalidTokenError('Invalid token')

def token_required(f):
    """Decorator to require a valid JWT token for endpoint access"""
    @wraps(f)
    def decorated(*args, **kwargs):
        token = None
        
        # Check for token in Authorization header
        auth_header = request.headers.get('Authorization', '')
        scheme, _, candidate = auth_header.partition(' ')
        if scheme.lower() == 'bearer' and candidate and ' ' not in candidate:
            token = candidate
        elif auth_header:
            return jsonify({'success': False, 'error': 'Invalid token format'}), 401
        
        if not token:
            return jsonify({'success': False, 'error': 'Token is missing'}), 401
        
        try:
            # Get auth manager from app context
            auth_manager = current_app.auth_manager
            payload = auth_manager.verify_token(token)
            # Attach user info to request context
            request.current_user = payload['sub']
        except jwt.ExpiredSignatureError:
            return jsonify({'success': False, 'error': 'Token has expired'}), 401
        except jwt.InvalidTokenError:
            return jsonify({'success': False, 'error': 'Invalid token'}), 401
        
        return f(*args, **kwargs)
    
    return decorated

def init_auth(app):
    """Initialize authentication for the Flask app"""
    app.auth_manager = AuthManager()
