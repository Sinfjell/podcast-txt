"""Multi-key MCP credentials for /connect and customer API auth.

Lookup order: active ``mcp_credentials.key_hash``, then legacy
``users.api_key_hash``. Minting a key for one client never revokes another.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from models import db, McpCredential, User, MCP_CREDENTIAL_COLUMN_MIGRATIONS

CONNECT_CLIENTS = (
    'cursor',
    'vscode',
    'claude_code',
    'claude',
    'chatgpt',
    'other',
)

CLIENT_LABELS = {
    'cursor': 'Cursor',
    'vscode': 'VS Code',
    'claude_code': 'Claude Code',
    'claude': 'Claude',
    'chatgpt': 'ChatGPT',
    'other': 'API key',
}

# Double-click reuse window for an unused key of the same client.
RECENT_UNUSED_MINUTES = 10
# Throttle last_seen_at writes.
LAST_SEEN_THROTTLE_SECONDS = 60
# "Old key in use" window for status.problem.
REVOKED_HIT_WINDOW_MINUTES = 10


def _utcnow():
    return datetime.now(timezone.utc)


def ensure_mcp_credentials_table(app, text, live_columns):
    """Create mcp_credentials if missing. Safe when two gunicorn workers race."""
    from sqlalchemy.exc import OperationalError

    try:
        db.session.execute(text("""
            CREATE TABLE IF NOT EXISTS mcp_credentials (
                id INTEGER NOT NULL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                kind VARCHAR(16) NOT NULL DEFAULT 'key',
                client VARCHAR(32) NOT NULL DEFAULT 'other',
                label VARCHAR(64) NOT NULL DEFAULT 'API key',
                key_hash VARCHAR(64),
                key_prefix VARCHAR(16),
                oauth_client_id VARCHAR(255),
                created_at DATETIME,
                revoked_at DATETIME,
                first_initialize_at DATETIME,
                last_seen_at DATETIME,
                client_name VARCHAR(128),
                client_version VARCHAR(64),
                first_tool_ok_at DATETIME,
                first_transcript_at DATETIME,
                tool_error_count INTEGER NOT NULL DEFAULT 0,
                last_insufficient_balance_at DATETIME,
                last_revoked_hit_at DATETIME,
                FOREIGN KEY(user_id) REFERENCES users (id)
            )
        """))
        db.session.commit()
    except OperationalError as exc:
        db.session.rollback()
        msg = str(exc).lower()
        if 'already exists' not in msg:
            raise
        app.logger.info(
            'mcp_credentials was created by another worker; continuing')

    existing = live_columns('mcp_credentials')
    if not existing:
        return
    for column, ddl_type in MCP_CREDENTIAL_COLUMN_MIGRATIONS.items():
        if column in existing:
            continue
        try:
            db.session.execute(text(
                f'ALTER TABLE mcp_credentials ADD COLUMN {column} {ddl_type}'
            ))
            db.session.commit()
        except OperationalError as exc:
            db.session.rollback()
            if 'duplicate column name' not in str(exc).lower():
                raise
            app.logger.info(
                'mcp_credentials.%s was added by another worker; continuing',
                column)

    existing = live_columns('mcp_credentials')
    index_specs = []
    if 'key_hash' in existing:
        index_specs.append(
            ('ix_mcp_credentials_key_hash',
             'CREATE INDEX IF NOT EXISTS ix_mcp_credentials_key_hash '
             'ON mcp_credentials (key_hash)')
        )
    if 'user_id' in existing:
        index_specs.append(
            ('ix_mcp_credentials_user_id',
             'CREATE INDEX IF NOT EXISTS ix_mcp_credentials_user_id '
             'ON mcp_credentials (user_id)')
        )
    for name, ddl in index_specs:
        try:
            db.session.execute(text(ddl))
            db.session.commit()
        except OperationalError:
            db.session.rollback()
            app.logger.exception('Could not create %s', name)


def migrate_legacy_api_keys(app=None):
    """Copy users.api_key_* into mcp_credentials as 'Legacy key' (idempotent).

    Does not clear the legacy columns — dual lookup keeps old keys working.
    """
    users = User.query.filter(User.api_key_hash.isnot(None)).all()
    migrated = 0
    for user in users:
        exists = McpCredential.query.filter_by(
            user_id=user.id, key_hash=user.api_key_hash).first()
        if exists:
            continue
        row = McpCredential(
            user_id=user.id,
            kind='key',
            client='other',
            label='Legacy key',
            key_hash=user.api_key_hash,
            key_prefix=user.api_key_prefix,
            created_at=user.api_key_created_at or _utcnow(),
        )
        db.session.add(row)
        migrated += 1
    if migrated:
        db.session.commit()
        if app is not None:
            app.logger.info('Migrated %s legacy API key(s) into mcp_credentials',
                            migrated)
    return migrated


def lookup_credential_by_key(plaintext, *, hash_fn):
    """Return (user, credential_or_None, revoked_credential_or_None).

    Active mcp_credentials win; then legacy users.api_key_hash (credential
    None). If the hash matches only a revoked row, user is None and the
    revoked credential is returned for old-key detection.
    """
    if not plaintext:
        return None, None, None
    digest = hash_fn(plaintext)
    active = (
        McpCredential.query
        .filter_by(key_hash=digest, kind='key')
        .filter(McpCredential.revoked_at.is_(None))
        .first()
    )
    if active is not None:
        user = db.session.get(User, active.user_id)
        return user, active, None

    user = User.query.filter_by(api_key_hash=digest).first()
    if user is not None:
        # Lazy-migrate so /connect tracking works even before boot migration.
        legacy = McpCredential.query.filter_by(
            user_id=user.id, key_hash=digest).first()
        if legacy is None:
            legacy = McpCredential(
                user_id=user.id,
                kind='key',
                client='other',
                label='Legacy key',
                key_hash=user.api_key_hash,
                key_prefix=user.api_key_prefix,
                created_at=user.api_key_created_at or _utcnow(),
            )
            db.session.add(legacy)
            db.session.commit()
        if legacy.revoked_at is not None:
            return None, None, legacy
        return user, legacy, None

    revoked = (
        McpCredential.query
        .filter_by(key_hash=digest, kind='key')
        .filter(McpCredential.revoked_at.isnot(None))
        .order_by(McpCredential.revoked_at.desc())
        .first()
    )
    if revoked is not None:
        return None, None, revoked
    return None, None, None


def note_revoked_key_hit(credential):
    """Record that a revoked key was presented (drives status.problem)."""
    if credential is None:
        return
    credential.last_revoked_hit_at = _utcnow()
    db.session.commit()


def touch_credential_seen(credential):
    """Update last_seen_at at most once per LAST_SEEN_THROTTLE_SECONDS."""
    if credential is None:
        return
    now = _utcnow()
    last = credential.last_seen_at
    if last is not None:
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if (now - last).total_seconds() < LAST_SEEN_THROTTLE_SECONDS:
            return
    credential.last_seen_at = now
    db.session.commit()


def mark_initialize(credential, client_info):
    """Set first_initialize_at + clientInfo when still null. Returns True if new."""
    if credential is None:
        return False
    touch_credential_seen(credential)
    if credential.first_initialize_at is not None:
        return False
    info = client_info if isinstance(client_info, dict) else {}
    name = info.get('name')
    version = info.get('version')
    credential.first_initialize_at = _utcnow()
    if isinstance(name, str) and name.strip():
        credential.client_name = name.strip()[:128]
    if isinstance(version, str) and version.strip():
        credential.client_version = version.strip()[:64]
    db.session.commit()
    return True


def mark_tool_result(credential, *, ok, insufficient_balance=False):
    """Record first successful tool call or accumulate errors / balance hits."""
    if credential is None:
        return
    touch_credential_seen(credential)
    changed = False
    if insufficient_balance:
        credential.last_insufficient_balance_at = _utcnow()
        # Spec: out-of-minutes still greens step 4 (the tool path worked).
        if credential.first_tool_ok_at is None:
            credential.first_tool_ok_at = _utcnow()
        changed = True
    elif ok:
        if credential.first_tool_ok_at is None:
            credential.first_tool_ok_at = _utcnow()
            changed = True
    else:
        credential.tool_error_count = int(credential.tool_error_count or 0) + 1
        changed = True
    if changed:
        db.session.commit()


def mark_first_transcript(credential):
    """Set first_transcript_at once when a transcript is ready for this key."""
    if credential is None or credential.first_transcript_at is not None:
        return False
    credential.first_transcript_at = _utcnow()
    db.session.commit()
    return True


def mark_first_transcript_for_task(task):
    """If task has credential_id and is ready, stamp the credential."""
    cred_id = getattr(task, 'credential_id', None)
    if not cred_id:
        return False
    cred = db.session.get(McpCredential, cred_id)
    return mark_first_transcript(cred)


def newest_active_credential(user_id, client):
    """Newest non-revoked credential for this user+client (key or oauth)."""
    return (
        McpCredential.query
        .filter_by(user_id=user_id, client=client)
        .filter(McpCredential.revoked_at.is_(None))
        .order_by(McpCredential.created_at.desc(), McpCredential.id.desc())
        .first()
    )


def list_active_credentials(user_id):
    return (
        McpCredential.query
        .filter_by(user_id=user_id)
        .filter(McpCredential.revoked_at.is_(None))
        .order_by(McpCredential.created_at.desc(), McpCredential.id.desc())
        .all()
    )


def mint_credential(user_id, client, *, mint_fn, hash_fn, prefix_fn, label=None):
    """Create or rotate a key credential for client. Returns (row, plaintext).

    If an unused key for the same client was minted in the last 10 minutes,
    rotate that row instead of inserting another.
    """
    if client not in CONNECT_CLIENTS:
        client = 'other'
    label = label or CLIENT_LABELS.get(client, 'API key')
    cutoff = _utcnow() - timedelta(minutes=RECENT_UNUSED_MINUTES)
    recent = (
        McpCredential.query
        .filter_by(user_id=user_id, client=client, kind='key')
        .filter(McpCredential.revoked_at.is_(None))
        .filter(McpCredential.first_initialize_at.is_(None))
        .filter(McpCredential.created_at >= cutoff)
        .order_by(McpCredential.created_at.desc())
        .first()
    )
    plaintext = mint_fn()
    digest = hash_fn(plaintext)
    prefix = prefix_fn(plaintext)
    now = _utcnow()
    if recent is not None:
        recent.key_hash = digest
        recent.key_prefix = prefix
        recent.label = label
        recent.created_at = now
        db.session.commit()
        return recent, plaintext

    row = McpCredential(
        user_id=user_id,
        kind='key',
        client=client,
        label=label,
        key_hash=digest,
        key_prefix=prefix,
        created_at=now,
    )
    db.session.add(row)
    db.session.commit()
    return row, plaintext


def revoke_credential(user_id, credential_id, *, clear_legacy_user=None):
    """Revoke a credential owned by user_id. Returns the row or None.

    Keeps key_hash so revoked-key 401s can be detected. Clears matching
    legacy users.api_key_* when the hash matches.
    """
    row = (
        McpCredential.query
        .filter_by(id=credential_id, user_id=user_id)
        .filter(McpCredential.revoked_at.is_(None))
        .first()
    )
    if row is None:
        return None
    row.revoked_at = _utcnow()
    if clear_legacy_user is not None and row.key_hash:
        user = clear_legacy_user
        if user is not None and user.api_key_hash == row.key_hash:
            user.api_key_hash = None
            user.api_key_prefix = None
            user.api_key_created_at = None
    db.session.commit()
    return row


def connect_status_payload(user, client):
    """JSON-serialisable status for GET /connect/status."""
    if client not in CONNECT_CLIENTS:
        client = 'cursor'
    logged_in = user is not None and getattr(user, 'is_authenticated', False)
    if not logged_in:
        return {
            'logged_in': False,
            'credential': None,
            'steps': {
                'logged_in': {'done': False},
                'authorized': {'done': False},
                'connected': {'done': False},
                'first_tool': {'done': False},
                'first_transcript': {'done': False},
            },
            'problem': None,
        }

    cred = newest_active_credential(user.id, client)
    steps = {
        'logged_in': {'done': True, 'email': user.email},
        'authorized': {
            'done': cred is not None,
            'at': _iso(cred.created_at) if cred else None,
            'label': cred.label if cred else None,
            'prefix': cred.key_prefix if cred else None,
        },
        'connected': {
            'done': bool(cred and cred.first_initialize_at),
            'at': _iso(cred.first_initialize_at) if cred else None,
            'client_name': _client_display(cred) if cred else None,
        },
        'first_tool': {
            'done': bool(cred and cred.first_tool_ok_at),
            'at': _iso(cred.first_tool_ok_at) if cred else None,
        },
        'first_transcript': {
            'done': bool(cred and cred.first_transcript_at),
            'at': _iso(cred.first_transcript_at) if cred else None,
        },
    }
    problem = None
    if cred and cred.last_insufficient_balance_at and not steps['first_transcript']['done']:
        hit = cred.last_insufficient_balance_at
        if hit.tzinfo is None:
            hit = hit.replace(tzinfo=timezone.utc)
        if (_utcnow() - hit).total_seconds() < 3600:
            problem = 'no_minutes'
    if cred and not steps['first_tool']['done'] and int(cred.tool_error_count or 0) >= 3:
        problem = 'tool_errors'
    # Revoked key in use for this client (or any of this user's revoked keys)
    cutoff = _utcnow() - timedelta(minutes=REVOKED_HIT_WINDOW_MINUTES)
    revoked_hit = (
        McpCredential.query
        .filter_by(user_id=user.id, client=client)
        .filter(McpCredential.revoked_at.isnot(None))
        .filter(McpCredential.last_revoked_hit_at.isnot(None))
        .filter(McpCredential.last_revoked_hit_at >= cutoff)
        .first()
    )
    if revoked_hit is not None:
        problem = 'revoked_key_in_use'

    cred_payload = None
    if cred is not None:
        cred_payload = {
            'id': cred.id,
            'kind': cred.kind,
            'client': cred.client,
            'label': cred.label,
            'prefix': cred.key_prefix,
            'created_at': _iso(cred.created_at),
            'last_seen_at': _iso(cred.last_seen_at),
            'first_initialize_at': _iso(cred.first_initialize_at),
        }
    return {
        'logged_in': True,
        'credential': cred_payload,
        'steps': steps,
        'problem': problem,
        'connections': [
            {
                'id': c.id,
                'client': c.client,
                'label': c.label,
                'prefix': c.key_prefix,
                'created_at': _iso(c.created_at),
                'last_seen_at': _iso(c.last_seen_at),
                'connected': bool(c.first_initialize_at),
            }
            for c in list_active_credentials(user.id)
            if c.client in ('cursor', 'vscode', 'claude_code', 'claude', 'chatgpt')
               or c.first_initialize_at
        ],
    }


def _iso(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _client_display(cred):
    if not cred:
        return None
    name = (cred.client_name or '').strip()
    ver = (cred.client_version or '').strip()
    if name and ver:
        return f'{name} {ver}'
    return name or None
