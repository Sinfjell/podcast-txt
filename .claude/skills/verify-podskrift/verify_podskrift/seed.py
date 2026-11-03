"""Seed the verify SQLite DB from fixtures only — never a prod DB copy."""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Any


TEST_USER_EMAIL = 'verify-user@podskrift.test'
ADMIN_USER_EMAIL = 'verify-admin@podskrift.test'
EMPTY_USER_EMAIL = 'verify-empty@podskrift.test'
PASSWORD_BYTES = 18


def _random_password() -> str:
    return secrets.token_urlsafe(PASSWORD_BYTES)


def seed_users(app_module, *, dry_run: bool = False) -> dict[str, Any]:
    """Create local test users + customer API key. Returns non-PII counts + secrets
    for the state file (caller writes state; secrets never go to stdout)."""
    from models import db, User

    plan = [
        {'email': TEST_USER_EMAIL, 'admin': False, 'trial_limit': 7200, 'used': 0,
         'paid': 0, 'role': 'user'},
        {'email': ADMIN_USER_EMAIL, 'admin': True, 'trial_limit': 7200, 'used': 0,
         'paid': 0, 'role': 'admin'},
        {'email': EMPTY_USER_EMAIL, 'admin': False, 'trial_limit': 0, 'used': 0,
         'paid': 0, 'role': 'empty-balance'},
    ]
    if dry_run:
        return {
            'ok': True,
            'dry_run': True,
            'would_create': [p['role'] for p in plan],
            'source': 'fixtures-only',
        }

    passwords: dict[str, str] = {}
    api_key = None
    with app_module.app.app_context():
        for spec in plan:
            email = spec['email']
            existing = User.query.filter_by(email=email).first()
            password = _random_password()
            passwords[spec['role']] = password
            if existing:
                user = existing
            else:
                user = User(email=email)
                db.session.add(user)
            user.set_password(password)
            user.trial_seconds_limit = spec['trial_limit']
            user.trial_seconds_used = spec['used']
            user.paid_seconds_balance = spec['paid']
            if spec['trial_limit'] and not user.trial_variant:
                user.trial_variant = '120'
            db.session.flush()
            if spec['role'] == 'user':
                plaintext = app_module.mint_customer_api_key()
                user.api_key_hash = app_module.hash_customer_api_key(plaintext)
                user.api_key_prefix = app_module.customer_api_key_prefix(plaintext)
                user.api_key_created_at = datetime.now(timezone.utc)
                api_key = plaintext
        db.session.commit()

        # Mint a long-lived OAuth access token for MCP checks (hashed in DB).
        # Customer psk_ remains the primary MCP auth; OAuth proves the token path.
        oauth_token = None
        oauth_error = None
        try:
            import json as _json
            import oauth_server as oauth_mod
            from datetime import timedelta
            from models import OAuthAccessToken, OAuthClient
            user = User.query.filter_by(email=TEST_USER_EMAIL).first()
            client = OAuthClient.query.filter_by(
                client_id='poc_verify_podskrift').first()
            if client is None:
                client = OAuthClient(
                    client_id='poc_verify_podskrift',
                    client_secret_hash=None,
                    client_name='verify-podskrift',
                    redirect_uris_json=_json.dumps(
                        ['http://127.0.0.1/callback']),
                    grant_types_json=_json.dumps(
                        ['authorization_code', 'refresh_token']),
                    response_types_json=_json.dumps(['code']),
                    token_endpoint_auth_method='none',
                    token_endpoint_auth_methods_json=_json.dumps(['none']),
                    registration_source='verify',
                )
                db.session.add(client)
                db.session.commit()
            pair = oauth_mod._issue_token_pair(
                user_id=user.id,
                client_id=client.client_id,
                scope='mcp offline_access',
                resource=oauth_mod.mcp_resource_url(),
            )
            oauth_token = pair['access_token']
            row = OAuthAccessToken.query.filter_by(
                token_hash=oauth_mod._hash_token(oauth_token)).first()
            if row is not None:
                row.expires_at = datetime.now(timezone.utc) + timedelta(days=7)
                db.session.commit()
        except Exception as exc:  # noqa: BLE001
            oauth_token = None
            oauth_error = type(exc).__name__

        user_count = User.query.filter(
            User.email.like('%@podskrift.test')).count()

    return {
        'ok': True,
        'dry_run': False,
        'users_seeded': user_count,
        'roles': [p['role'] for p in plan],
        'source': 'fixtures-only',
        'oauth_minted': bool(oauth_token),
        'oauth_error': oauth_error,
        '_secrets': {
            'password': passwords['user'],
            'admin_password': passwords['admin'],
            'empty_password': passwords['empty-balance'],
            'api_key': api_key,
            'oauth_access_token': oauth_token,
            'email': TEST_USER_EMAIL,
            'admin_email': ADMIN_USER_EMAIL,
            'empty_email': EMPTY_USER_EMAIL,
        },
    }
