"""OAuth 2.1 authorization server for Podskrift MCP (feature-flagged).

Off unless ``MCP_OAUTH_ENABLED`` is truthy (default off). Implements the MCP
authorization spec (2025-06-18) so ChatGPT connectors and Claude.ai custom
connectors can complete OAuth without a third-party IdP:

- Protected Resource Metadata (RFC 9728)
- Authorization Server Metadata (RFC 8414)
- Dynamic Client Registration (RFC 7591)
- Authorization code + PKCE S256
- Token endpoint with refresh-token rotation
- Resource indicators (RFC 8707) audience-bound to the MCP URL

API-key auth (``psk_…``) on ``/mcp`` is unchanged and stays available.
"""

from __future__ import annotations

import base64
import collections
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import socket
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode, urlparse, urlunparse

import requests
from flask import (Response, flash, g, jsonify, redirect, render_template,
                   request, session, url_for)
from flask_login import current_user

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ACCESS_TOKEN_LIFETIME = timedelta(hours=1)
REFRESH_TOKEN_LIFETIME = timedelta(days=30)
AUTH_CODE_LIFETIME = timedelta(minutes=10)

# Rate limits (in-process; same pattern as MCP / password-reset).
OAUTH_REGISTER_MAX_PER_WINDOW = int(os.getenv('OAUTH_REGISTER_MAX_PER_WINDOW', '20'))
OAUTH_REGISTER_WINDOW_SECONDS = int(os.getenv('OAUTH_REGISTER_WINDOW_SECONDS', '3600'))
OAUTH_TOKEN_MAX_PER_WINDOW = int(os.getenv('OAUTH_TOKEN_MAX_PER_WINDOW', '60'))
OAUTH_TOKEN_WINDOW_SECONDS = int(os.getenv('OAUTH_TOKEN_WINDOW_SECONDS', '60'))

# Token prefixes — distinct from customer API keys (psk_).
ACCESS_TOKEN_PREFIX = 'poa_'
REFRESH_TOKEN_PREFIX = 'por_'
CLIENT_ID_PREFIX = 'poc_'

DEFAULT_SCOPE = 'mcp'
SUPPORTED_SCOPES = frozenset({'mcp', 'offline_access'})

# Claude.ai hosted MCP OAuth callback (must pass redirect_uri validation).
CLAUDE_AI_CALLBACK = 'https://claude.ai/api/mcp/auth_callback'

# CIMD fetch limits (SSRF-hardened outbound HTTP).
CIMD_FETCH_TIMEOUT_SEC = 5
CIMD_FETCH_MAX_BYTES = 64 * 1024

_OAUTH_DOC_BEGIN = '<!-- mcp-oauth-section -->'
_OAUTH_DOC_END = '<!-- /mcp-oauth-section -->'

_register_attempts: dict[str, list[float]] = collections.defaultdict(list)
_token_attempts: dict[str, list[float]] = collections.defaultdict(list)
_rate_lock = threading.Lock()


def mcp_oauth_enabled() -> bool:
    """True when OAuth discovery + /oauth/* endpoints should be served."""
    return (os.getenv('MCP_OAUTH_ENABLED', '0') or '').strip().lower() in (
        '1', 'true', 'yes', 'on',
    )


def filter_mcp_oauth_docs_section(markdown: str, *, enabled: bool | None = None) -> str:
    """Include or strip the gated MCP OAuth section from customer-api.md."""
    if enabled is None:
        enabled = mcp_oauth_enabled()
    begin = _OAUTH_DOC_BEGIN
    end = _OAUTH_DOC_END
    if begin not in markdown:
        return markdown
    if enabled:
        return markdown.replace(begin, '').replace(end, '')
    pattern = re.compile(
        re.escape(begin) + r'.*?' + re.escape(end),
        flags=re.DOTALL,
    )
    return pattern.sub('', markdown)


def _app():
    import app as app_mod
    return app_mod


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def oauth_issuer() -> str:
    """Canonical authorization-server issuer (no trailing slash)."""
    return (os.getenv('PUBLIC_BASE_URL') or 'https://podskrift.com').rstrip('/')


def mcp_resource_url() -> str:
    """Canonical MCP resource identifier (RFC 8707 audience)."""
    return f'{oauth_issuer()}/mcp'


def protected_resource_metadata_url() -> str:
    return f'{oauth_issuer()}/.well-known/oauth-protected-resource'


def _hash_token(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode('utf-8')).hexdigest()


def _mint_opaque(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(32)


def _json_list(value: Any, *, default: list | None = None) -> list:
    if isinstance(value, list):
        return [str(x) for x in value if isinstance(x, (str, int))]
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return list(default or [])
        if isinstance(parsed, list):
            return [str(x) for x in parsed if isinstance(x, (str, int))]
    return list(default or [])


def _client_redirect_uris(client) -> list[str]:
    return _json_list(client.redirect_uris_json, default=[])


def _is_localhost_host(host: str) -> bool:
    h = (host or '').lower()
    if h.startswith('['):  # IPv6 literal
        return h in ('[::1]',)
    return h in ('localhost', '127.0.0.1') or h.endswith('.localhost')


def validate_redirect_uri(uri: str) -> bool:
    """https required, or http://localhost / 127.0.0.1 for desktop clients."""
    if not uri or not isinstance(uri, str) or len(uri) > 1024:
        return False
    try:
        parsed = urlparse(uri.strip())
    except ValueError:
        return False
    if parsed.fragment:
        return False
    if not parsed.scheme or not parsed.netloc:
        return False
    host = parsed.hostname or ''
    if parsed.scheme == 'https':
        return bool(host)
    if parsed.scheme == 'http':
        return _is_localhost_host(host)
    return False


def normalize_resource(resource: str | None) -> str | None:
    """Canonical resource URL or None if missing/invalid."""
    if not resource or not isinstance(resource, str):
        return None
    raw = resource.strip()
    if not raw:
        return None
    try:
        parsed = urlparse(raw)
    except ValueError:
        return None
    if parsed.scheme.lower() != 'https' or not parsed.netloc:
        # Tests / local may use http via PUBLIC_BASE_URL
        if parsed.scheme.lower() != 'http' or not parsed.netloc:
            return None
    # Lowercase scheme+host; drop fragment and default ports; no trailing slash
    # on path except when path is empty → we keep /mcp as-is.
    host = (parsed.hostname or '').lower()
    if not host:
        return None
    port = parsed.port
    netloc = host
    if port and not (
        (parsed.scheme == 'https' and port == 443)
        or (parsed.scheme == 'http' and port == 80)
    ):
        netloc = f'{host}:{port}'
    path = parsed.path or ''
    if path != '/' and path.endswith('/'):
        path = path.rstrip('/')
    return urlunparse((parsed.scheme.lower(), netloc, path, '', '', ''))


def resource_matches_mcp(resource: str | None) -> bool:
    expected = normalize_resource(mcp_resource_url())
    got = normalize_resource(resource)
    return bool(expected and got and expected == got)


def verify_pkce_s256(code_verifier: str, code_challenge: str) -> bool:
    if not code_verifier or not code_challenge:
        return False
    if len(code_verifier) < 43 or len(code_verifier) > 128:
        return False
    if not re.fullmatch(r'[A-Za-z0-9._~-]+', code_verifier):
        return False
    digest = hashlib.sha256(code_verifier.encode('ascii')).digest()
    computed = base64.urlsafe_b64encode(digest).rstrip(b'=').decode('ascii')
    return hmac.compare_digest(computed, code_challenge)


def _rate_limit(bucket_map: dict, key: str, *, max_n: int, window: int) -> bool:
    """Reserve one slot. True when under the limit (and recorded)."""
    now = time.time()
    with _rate_lock:
        seen = [t for t in bucket_map.get(key, ()) if now - t < window]
        if len(seen) >= max_n:
            bucket_map[key] = seen
            return False
        seen.append(now)
        bucket_map[key] = seen
        if len(bucket_map) > 10000:
            stale = [k for k, v in list(bucket_map.items())
                     if not v or now - v[-1] > window]
            for k in stale:
                bucket_map.pop(k, None)
        return True


def _client_ip() -> str:
    A = _app()
    return A._client_ip()


def _oauth_error(error: str, description: str | None = None, status: int = 400):
    body = {'error': error}
    if description:
        body['error_description'] = description
    resp = jsonify(body)
    resp.status_code = status
    resp.headers['Cache-Control'] = 'no-store'
    return resp


def _www_authenticate_header() -> str:
    meta = protected_resource_metadata_url()
    return (
        f'Bearer resource_metadata="{meta}", '
        f'realm="podskrift", scope="{DEFAULT_SCOPE}"'
    )


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------

def lookup_access_token_user(plaintext: str):
    """Return User for a valid (non-revoked, non-expired) access token, or None."""
    if not plaintext or not plaintext.startswith(ACCESS_TOKEN_PREFIX):
        return None
    from models import OAuthAccessToken, User, db
    digest = _hash_token(plaintext)
    row = OAuthAccessToken.query.filter_by(token_hash=digest).first()
    if row is None:
        return None
    if row.revoked_at is not None:
        return None
    if _as_utc(row.expires_at) <= _utc_now():
        return None
    if not resource_matches_mcp(row.resource):
        return None
    return db.session.get(User, row.user_id)


def list_connected_apps(user_id: int) -> list[dict]:
    """Active OAuth connections for Settings (by client, newest first)."""
    from models import OAuthClient, OAuthRefreshToken
    now = _utc_now()
    rows = (
        OAuthRefreshToken.query
        .filter_by(user_id=user_id)
        .filter(OAuthRefreshToken.revoked_at.is_(None))
        .order_by(OAuthRefreshToken.created_at.desc())
        .all()
    )
    seen: set[str] = set()
    out = []
    for tok in rows:
        if tok.client_id in seen:
            continue
        if _as_utc(tok.expires_at) <= now:
            continue
        seen.add(tok.client_id)
        client = OAuthClient.query.filter_by(client_id=tok.client_id).first()
        out.append({
            'client_id': tok.client_id,
            'client_name': (client.client_name if client else None) or tok.client_id,
            'connected_at': tok.created_at,
            'scope': tok.scope or DEFAULT_SCOPE,
        })
    return out


def revoke_user_client(user_id: int, client_id: str) -> bool:
    """Revoke all access + refresh tokens for user×client. True if any revoked."""
    from models import OAuthAccessToken, OAuthRefreshToken, db
    now = _utc_now()
    changed = False
    for model in (OAuthAccessToken, OAuthRefreshToken):
        rows = (
            model.query
            .filter_by(user_id=user_id, client_id=client_id)
            .filter(model.revoked_at.is_(None))
            .all()
        )
        for row in rows:
            row.revoked_at = now
            changed = True
    if changed:
        db.session.commit()
    return changed


def _issue_token_pair(*, user_id: int, client_id: str, scope: str,
                      resource: str) -> dict:
    from models import OAuthAccessToken, OAuthRefreshToken, db
    now = _utc_now()
    access_plain = _mint_opaque(ACCESS_TOKEN_PREFIX)
    refresh_plain = _mint_opaque(REFRESH_TOKEN_PREFIX)
    access = OAuthAccessToken(
        token_hash=_hash_token(access_plain),
        client_id=client_id,
        user_id=user_id,
        scope=scope,
        resource=resource,
        expires_at=now + ACCESS_TOKEN_LIFETIME,
    )
    refresh = OAuthRefreshToken(
        token_hash=_hash_token(refresh_plain),
        client_id=client_id,
        user_id=user_id,
        scope=scope,
        resource=resource,
        expires_at=now + REFRESH_TOKEN_LIFETIME,
    )
    db.session.add(access)
    db.session.add(refresh)
    db.session.commit()
    return {
        'access_token': access_plain,
        'token_type': 'Bearer',
        'expires_in': int(ACCESS_TOKEN_LIFETIME.total_seconds()),
        'refresh_token': refresh_plain,
        'scope': scope,
    }


def _scope_string(requested: str | None) -> str:
    parts = []
    for part in (requested or DEFAULT_SCOPE).split():
        if part in SUPPORTED_SCOPES and part not in parts:
            parts.append(part)
    if DEFAULT_SCOPE not in parts:
        parts.insert(0, DEFAULT_SCOPE)
    if 'offline_access' not in parts:
        parts.append('offline_access')
    return ' '.join(parts)


# ---------------------------------------------------------------------------
# Metadata + routes
# ---------------------------------------------------------------------------

def protected_resource_metadata() -> dict:
    issuer = oauth_issuer()
    return {
        'resource': mcp_resource_url(),
        'authorization_servers': [issuer],
        'scopes_supported': sorted(SUPPORTED_SCOPES),
        'bearer_methods_supported': ['header'],
        'resource_documentation': f'{issuer}/docs/api',
    }


def authorization_server_metadata() -> dict:
    issuer = oauth_issuer()
    return {
        'issuer': issuer,
        'authorization_endpoint': f'{issuer}/oauth/authorize',
        'token_endpoint': f'{issuer}/oauth/token',
        'registration_endpoint': f'{issuer}/oauth/register',
        # ChatGPT prefers CIMD when advertised; DCR remains via registration_endpoint.
        'client_id_metadata_document_supported': True,
        'scopes_supported': sorted(SUPPORTED_SCOPES),
        'response_types_supported': ['code'],
        'grant_types_supported': ['authorization_code', 'refresh_token'],
        'code_challenge_methods_supported': ['S256'],
        # CIMD public clients use none; DCR may use client_secret_post.
        'token_endpoint_auth_methods_supported': ['none', 'client_secret_post'],
        'authorization_response_iss_parameter_supported': True,
    }


def looks_like_cimd_client_id(client_id: str) -> bool:
    """True when client_id is an HTTPS URL with a path (CIMD identifier)."""
    if not client_id or not isinstance(client_id, str) or len(client_id) > 512:
        return False
    try:
        parsed = urlparse(client_id.strip())
    except ValueError:
        return False
    if parsed.scheme.lower() != 'https' or not parsed.netloc:
        return False
    if parsed.username or parsed.password or parsed.fragment:
        return False
    try:
        port = parsed.port
    except ValueError:
        return False
    if port not in (None, 443):
        return False
    path = parsed.path or ''
    return bool(path) and path != '/'


def _ip_is_public(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address((ip_str or '').split('%', 1)[0])
    except ValueError:
        return False
    if ip.version == 6:
        embedded = ip.ipv4_mapped or ip.sixtofour
        if embedded is None and ip in ipaddress.ip_network('64:ff9b::/96'):
            embedded = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        if embedded is not None and not embedded.is_global:
            return False
    return bool(ip.is_global)


def _cimd_host_is_safe(hostname: str) -> bool:
    """Reject localhost / private / link-local hosts before fetching CIMD."""
    host = (hostname or '').lower().rstrip('.')
    if not host or _is_localhost_host(host):
        return False
    if host.endswith('.local') or host.endswith('.internal'):
        return False
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return False
    if not infos:
        return False
    for info in infos:
        addr = info[4][0]
        if not _ip_is_public(addr):
            return False
    return True


def fetch_cimd_document(client_id_url: str) -> tuple[dict | None, str | None]:
    """GET a Client ID Metadata Document. Returns (doc, error_description)."""
    if not looks_like_cimd_client_id(client_id_url):
        return None, 'client_id is not a valid CIMD HTTPS URL.'
    parsed = urlparse(client_id_url)
    if not _cimd_host_is_safe(parsed.hostname or ''):
        return None, 'CIMD host is not allowed.'
    try:
        resp = requests.get(
            client_id_url,
            timeout=CIMD_FETCH_TIMEOUT_SEC,
            allow_redirects=False,
            headers={
                'Accept': 'application/json',
                'User-Agent': 'Podskrift-OAuth/1.0',
            },
            stream=True,
        )
    except requests.RequestException:
        return None, 'Could not fetch client metadata document.'
    if resp.status_code != 200:
        resp.close()
        return None, f'CIMD fetch returned HTTP {resp.status_code}.'
    # Cap body size before json parse.
    chunks = []
    total = 0
    deadline = time.monotonic() + CIMD_FETCH_TIMEOUT_SEC
    try:
        for chunk in resp.iter_content(chunk_size=4096):
            if time.monotonic() > deadline:
                return None, 'CIMD fetch timed out.'
            if not chunk:
                continue
            total += len(chunk)
            if total > CIMD_FETCH_MAX_BYTES:
                return None, 'CIMD document too large.'
            chunks.append(chunk)
    finally:
        resp.close()
    try:
        doc = json.loads(b''.join(chunks).decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, 'CIMD document is not valid JSON.'
    if not isinstance(doc, dict):
        return None, 'CIMD document must be a JSON object.'
    doc_client_id = doc.get('client_id')
    if not isinstance(doc_client_id, str) or doc_client_id != client_id_url:
        return None, 'CIMD client_id must exactly match the document URL.'
    name = doc.get('client_name')
    if not isinstance(name, str) or not name.strip():
        return None, 'CIMD document requires client_name.'
    uris = doc.get('redirect_uris')
    if not isinstance(uris, list) or not uris:
        return None, 'CIMD document requires redirect_uris.'
    for uri in uris:
        if not isinstance(uri, str) or not validate_redirect_uri(uri):
            return None, 'CIMD redirect_uris failed validation.'
    return doc, None


def _cimd_auth_method(doc: dict) -> str | None:
    """Prefer public-client none; reject unsupported methods."""
    methods = doc.get('token_endpoint_auth_methods')
    if isinstance(methods, list) and methods:
        str_methods = [str(m) for m in methods]
        if 'none' in str_methods:
            return 'none'
    single = (doc.get('token_endpoint_auth_method') or 'none')
    if isinstance(single, str) and single.strip() == 'none':
        return 'none'
    # private_key_jwt is advertised by ChatGPT but not implemented here yet;
    # accept the client when none is also listed (handled above).
    return None


def upsert_cimd_client(client_id_url: str, doc: dict):
    """Persist / refresh a CIMD client row from a validated metadata document."""
    from models import OAuthClient, db
    auth_method = _cimd_auth_method(doc)
    if auth_method is None:
        return None, 'CIMD token_endpoint_auth_method must include none.'
    uris = [u.strip() for u in doc['redirect_uris'] if isinstance(u, str)]
    grant_types = _json_list(
        doc.get('grant_types'),
        default=['authorization_code', 'refresh_token'],
    )
    if 'authorization_code' not in grant_types:
        grant_types.append('authorization_code')
    if 'refresh_token' not in grant_types:
        grant_types.append('refresh_token')
    response_types = _json_list(doc.get('response_types'), default=['code'])
    if 'code' not in response_types:
        response_types = ['code']
    name = (doc.get('client_name') or '').strip()[:255]

    row = OAuthClient.query.filter_by(client_id=client_id_url).first()
    if row is None:
        row = OAuthClient(
            client_id=client_id_url,
            client_secret_hash=None,
            client_name=name,
            redirect_uris_json=json.dumps(uris),
            grant_types_json=json.dumps(grant_types),
            response_types_json=json.dumps(response_types),
            token_endpoint_auth_method=auth_method,
            registration_source='cimd',
        )
        db.session.add(row)
    else:
        row.client_name = name
        row.redirect_uris_json = json.dumps(uris)
        row.grant_types_json = json.dumps(grant_types)
        row.response_types_json = json.dumps(response_types)
        row.token_endpoint_auth_method = auth_method
        if not row.registration_source:
            row.registration_source = 'cimd'
    db.session.commit()
    return row, None


def resolve_oauth_client(client_id: str, *, allow_fetch: bool = True,
                         force_refresh: bool = False):
    """Load a DCR or CIMD client. Fetches CIMD metadata when unknown (or forced)."""
    from models import OAuthClient
    client_id = (client_id or '').strip()
    if not client_id:
        return None, 'Missing client_id.'
    row = OAuthClient.query.filter_by(client_id=client_id).first()
    if row is not None and not force_refresh:
        return row, None
    if not allow_fetch or not looks_like_cimd_client_id(client_id):
        if row is not None:
            return row, None
        return None, 'Unknown client_id.'
    doc, err = fetch_cimd_document(client_id)
    if err:
        if row is not None:
            return row, None
        return None, err
    return upsert_cimd_client(client_id, doc)


def _redirect_with_params(redirect_uri: str, params: dict):
    parsed = urlparse(redirect_uri)
    q = []
    if parsed.query:
        q.append(parsed.query)
    q.append(urlencode(params))
    new_query = '&'.join(q)
    target = urlunparse((
        parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, '',
    ))
    return redirect(target)


def _authorize_error_redirect(redirect_uri: str | None, error: str,
                              description: str, state: str | None):
    if not redirect_uri or not validate_redirect_uri(redirect_uri):
        return _oauth_error(error, description, status=400)
    params = {'error': error, 'error_description': description, 'iss': oauth_issuer()}
    if state is not None:
        params['state'] = state
    return _redirect_with_params(redirect_uri, params)


def register_oauth(app_flask):
    """Attach OAuth + well-known routes. Always registered; 404 when flag off."""

    def _require_oauth_flag():
        if not mcp_oauth_enabled():
            return jsonify({'error': 'Not found'}), 404
        return None

    @app_flask.get('/.well-known/oauth-protected-resource')
    @app_flask.get('/.well-known/oauth-protected-resource/mcp')
    def oauth_protected_resource_metadata():
        denied = _require_oauth_flag()
        if denied:
            return denied
        resp = jsonify(protected_resource_metadata())
        resp.headers['Cache-Control'] = 'public, max-age=300'
        return resp

    @app_flask.get('/.well-known/oauth-authorization-server')
    def oauth_authorization_server_metadata():
        denied = _require_oauth_flag()
        if denied:
            return denied
        resp = jsonify(authorization_server_metadata())
        resp.headers['Cache-Control'] = 'public, max-age=300'
        return resp

    @app_flask.post('/oauth/register')
    def oauth_register():
        denied = _require_oauth_flag()
        if denied:
            return denied
        ip = _client_ip()
        if not _rate_limit(
                _register_attempts, f'ip:{ip}',
                max_n=OAUTH_REGISTER_MAX_PER_WINDOW,
                window=OAUTH_REGISTER_WINDOW_SECONDS):
            return _oauth_error(
                'too_many_requests', 'Slow down and try again later.', 429)

        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            # RFC 7591 also allows application/x-www-form-urlencoded
            data = request.form.to_dict(flat=True) if request.form else {}
            if 'redirect_uris' in data and isinstance(data['redirect_uris'], str):
                try:
                    data['redirect_uris'] = json.loads(data['redirect_uris'])
                except json.JSONDecodeError:
                    data['redirect_uris'] = [data['redirect_uris']]

        redirect_uris = data.get('redirect_uris')
        if not isinstance(redirect_uris, list) or not redirect_uris:
            return _oauth_error(
                'invalid_client_metadata',
                'redirect_uris must be a non-empty array.',
            )
        cleaned_uris = []
        for uri in redirect_uris:
            if not isinstance(uri, str) or not validate_redirect_uri(uri):
                return _oauth_error(
                    'invalid_redirect_uri',
                    'Each redirect_uri must be https (or http://localhost / '
                    'http://127.0.0.1 for desktop clients).',
                )
            cleaned_uris.append(uri.strip())

        grant_types = _json_list(
            data.get('grant_types'),
            default=['authorization_code', 'refresh_token'],
        )
        if 'authorization_code' not in grant_types:
            grant_types.append('authorization_code')
        if 'refresh_token' not in grant_types:
            grant_types.append('refresh_token')
        response_types = _json_list(data.get('response_types'), default=['code'])
        if 'code' not in response_types:
            response_types = ['code']

        auth_method = (data.get('token_endpoint_auth_method') or 'none').strip()
        if auth_method not in ('none', 'client_secret_post'):
            return _oauth_error(
                'invalid_client_metadata',
                'token_endpoint_auth_method must be none or client_secret_post.',
            )

        client_name = (data.get('client_name') or '').strip()[:255] or None
        client_id = _mint_opaque(CLIENT_ID_PREFIX)
        client_secret = None
        secret_hash = None
        if auth_method == 'client_secret_post':
            client_secret = secrets.token_urlsafe(32)
            secret_hash = _hash_token(client_secret)

        from models import OAuthClient, db
        row = OAuthClient(
            client_id=client_id,
            client_secret_hash=secret_hash,
            client_name=client_name,
            redirect_uris_json=json.dumps(cleaned_uris),
            grant_types_json=json.dumps(grant_types),
            response_types_json=json.dumps(response_types),
            token_endpoint_auth_method=auth_method,
            registration_source='dcr',
        )
        db.session.add(row)
        db.session.commit()

        issued_at = int((_as_utc(row.created_at) or _utc_now()).timestamp())
        payload = {
            'client_id': client_id,
            'client_id_issued_at': issued_at,
            'client_name': client_name,
            'redirect_uris': cleaned_uris,
            'grant_types': grant_types,
            'response_types': response_types,
            'token_endpoint_auth_method': auth_method,
        }
        if client_secret is not None:
            payload['client_secret'] = client_secret
            payload['client_secret_expires_at'] = 0
        resp = jsonify(payload)
        resp.status_code = 201
        resp.headers['Cache-Control'] = 'no-store'
        return resp

    @app_flask.route('/oauth/authorize', methods=['GET', 'POST'])
    def oauth_authorize():
        denied = _require_oauth_flag()
        if denied:
            return denied

        A = _app()
        # Relative next= so safe_next_url accepts the return path with query.
        if not current_user.is_authenticated:
            nxt = request.full_path
            if nxt.endswith('?'):
                nxt = nxt[:-1]
            return redirect(url_for('login', next=nxt))

        src = request.values
        client_id = (src.get('client_id') or '').strip()
        redirect_uri = (src.get('redirect_uri') or '').strip()
        response_type = (src.get('response_type') or '').strip()
        state = src.get('state')
        scope = src.get('scope')
        code_challenge = (src.get('code_challenge') or '').strip()
        code_challenge_method = (src.get('code_challenge_method') or '').strip()
        resource = (src.get('resource') or '').strip() or mcp_resource_url()

        from models import OAuthAuthorizationCode, db

        client, client_err = resolve_oauth_client(client_id)
        if client is None:
            return _oauth_error(
                'invalid_request', client_err or 'Unknown client_id.', 400)

        allowed = _client_redirect_uris(client)
        if redirect_uri not in allowed and looks_like_cimd_client_id(client_id):
            # Redirect URIs may have rotated in the client's published CIMD.
            client, _ = resolve_oauth_client(client_id, force_refresh=True)
            allowed = _client_redirect_uris(client) if client else []
        if client is None or redirect_uri not in allowed:
            return _oauth_error(
                'invalid_request',
                'redirect_uri is not registered for this client.',
                400,
            )

        if response_type != 'code':
            return _authorize_error_redirect(
                redirect_uri, 'unsupported_response_type',
                'Only response_type=code is supported.', state)

        if code_challenge_method != 'S256' or not code_challenge:
            return _authorize_error_redirect(
                redirect_uri, 'invalid_request',
                'PKCE S256 (code_challenge + code_challenge_method=S256) is required.',
                state)

        if not re.fullmatch(r'[A-Za-z0-9_-]{43,128}', code_challenge):
            return _authorize_error_redirect(
                redirect_uri, 'invalid_request',
                'code_challenge must be a valid S256 challenge.', state)

        if not resource_matches_mcp(resource):
            return _authorize_error_redirect(
                redirect_uri, 'invalid_target',
                f'resource must be {mcp_resource_url()}.', state)

        scope_str = _scope_string(scope)
        client_label = client.client_name or client.client_id

        if request.method == 'GET':
            # Do not pass csrf_token=... here: that shadows the context-processor
            # callable csrf_token() that base.html → _buy_modal.html invokes.
            # Mint into the session; the consent form calls csrf_token() itself.
            A.generate_csrf_token()
            page = Response(render_template(
                'oauth_consent.html',
                client_name=client_label,
                client_id=client.client_id,
                redirect_host=(urlparse(redirect_uri).hostname or ''),
                client_host=(urlparse(client.client_id).hostname
                             if looks_like_cimd_client_id(client.client_id) else None),
                redirect_uri=redirect_uri,
                response_type=response_type,
                state=state or '',
                scope=scope_str,
                code_challenge=code_challenge,
                code_challenge_method=code_challenge_method,
                resource=normalize_resource(resource) or mcp_resource_url(),
            ), mimetype='text/html')
            # Consent must never be framed (clickjacking), not even by the
            # PostHog toolbar origins the site-wide CSP allows.
            page.headers['X-Frame-Options'] = 'DENY'
            page.headers['Content-Security-Policy'] = (
                "frame-ancestors 'none'; base-uri 'self'; object-src 'none'")
            page.headers['Cache-Control'] = 'no-store'
            return page

        if not A.validate_csrf_token():
            flash('That form expired. Please try again.', 'error')
            return redirect(request.full_path if not request.full_path.endswith('?')
                            else request.path)

        decision = (request.form.get('decision') or '').strip()
        if decision != 'approve':
            return _authorize_error_redirect(
                redirect_uri, 'access_denied',
                'The user denied the request.', state)

        # Re-check redirect_uri from the form against the registered set.
        form_redirect = (request.form.get('redirect_uri') or '').strip()
        if form_redirect != redirect_uri or form_redirect not in allowed:
            return _oauth_error('invalid_request', 'redirect_uri mismatch.', 400)

        code_plain = secrets.token_urlsafe(32)
        now = _utc_now()
        row = OAuthAuthorizationCode(
            code_hash=_hash_token(code_plain),
            client_id=client.client_id,
            user_id=current_user.id,
            redirect_uri=form_redirect,
            code_challenge=code_challenge,
            code_challenge_method='S256',
            scope=scope_str,
            resource=normalize_resource(resource) or mcp_resource_url(),
            expires_at=now + AUTH_CODE_LIFETIME,
        )
        db.session.add(row)
        db.session.commit()

        params = {
            'code': code_plain,
            'iss': oauth_issuer(),
        }
        if state is not None and state != '':
            params['state'] = state
        return _redirect_with_params(form_redirect, params)

    @app_flask.post('/oauth/token')
    def oauth_token():
        denied = _require_oauth_flag()
        if denied:
            return denied

        ip = _client_ip()
        if not _rate_limit(
                _token_attempts, f'ip:{ip}',
                max_n=OAUTH_TOKEN_MAX_PER_WINDOW,
                window=OAUTH_TOKEN_WINDOW_SECONDS):
            return _oauth_error(
                'too_many_requests', 'Slow down and try again later.', 429)

        # application/x-www-form-urlencoded (RFC 6749) or JSON
        if request.is_json:
            data = request.get_json(silent=True) or {}
        else:
            data = request.form.to_dict(flat=True)

        grant_type = (data.get('grant_type') or '').strip()
        client_id = (data.get('client_id') or '').strip()

        from models import (OAuthAccessToken, OAuthAuthorizationCode,
                            OAuthRefreshToken, db)

        # No outbound CIMD fetch from this unauthenticated endpoint: a client
        # that completed /oauth/authorize is already stored.
        client, client_err = resolve_oauth_client(client_id, allow_fetch=False)
        if client is None:
            return _oauth_error(
                'invalid_client', client_err or 'Unknown client_id.', 401)

        if client.token_endpoint_auth_method == 'client_secret_post':
            secret = (data.get('client_secret') or '').strip()
            if not secret or not client.client_secret_hash:
                return _oauth_error('invalid_client', 'client_secret required.', 401)
            if not hmac.compare_digest(
                    _hash_token(secret), client.client_secret_hash):
                return _oauth_error('invalid_client', 'Invalid client_secret.', 401)

        if not _rate_limit(
                _token_attempts, f'client:{client_id}',
                max_n=OAUTH_TOKEN_MAX_PER_WINDOW,
                window=OAUTH_TOKEN_WINDOW_SECONDS):
            return _oauth_error(
                'too_many_requests', 'Slow down and try again later.', 429)

        if grant_type == 'authorization_code':
            return _token_authorization_code(data, client)
        if grant_type == 'refresh_token':
            return _token_refresh(data, client)
        return _oauth_error(
            'unsupported_grant_type',
            'Only authorization_code and refresh_token are supported.',
        )

    def _token_authorization_code(data: dict, client) -> Response:
        from models import OAuthAuthorizationCode, db

        code = (data.get('code') or '').strip()
        redirect_uri = (data.get('redirect_uri') or '').strip()
        code_verifier = (data.get('code_verifier') or '').strip()
        resource = (data.get('resource') or '').strip() or None

        if not code or not redirect_uri or not code_verifier:
            return _oauth_error(
                'invalid_request',
                'code, redirect_uri, and code_verifier are required.',
            )

        row = OAuthAuthorizationCode.query.filter_by(
            code_hash=_hash_token(code)).first()
        if row is None:
            return _oauth_error('invalid_grant', 'Invalid authorization code.')

        # Reuse / already used → refuse, and revoke tokens issued to this
        # user×client: a replayed code means it leaked (RFC 6749 §4.1.2).
        if row.used_at is not None:
            if row.client_id == client.client_id:
                revoke_user_client(row.user_id, row.client_id)
            return _oauth_error(
                'invalid_grant', 'Authorization code has already been used.')

        if _as_utc(row.expires_at) <= _utc_now():
            return _oauth_error('invalid_grant', 'Authorization code expired.')

        if row.client_id != client.client_id:
            return _oauth_error('invalid_grant', 'Code was issued to another client.')

        if row.redirect_uri != redirect_uri:
            return _oauth_error('invalid_grant', 'redirect_uri mismatch.')

        if row.code_challenge_method != 'S256' or not verify_pkce_s256(
                code_verifier, row.code_challenge):
            return _oauth_error('invalid_grant', 'PKCE verification failed.')

        if resource is not None and not resource_matches_mcp(resource):
            return _oauth_error(
                'invalid_target',
                f'resource must be {mcp_resource_url()}.',
            )
        if not resource_matches_mcp(row.resource):
            return _oauth_error('invalid_grant', 'Code audience mismatch.')

        # Atomic claim: conditional UPDATE so two workers cannot both redeem.
        now = _utc_now()
        claimed = (
            OAuthAuthorizationCode.query
            .filter_by(id=row.id, used_at=None)
            .update({'used_at': now}, synchronize_session=False)
        )
        db.session.commit()
        if not claimed:
            return _oauth_error(
                'invalid_grant', 'Authorization code has already been used.')

        tokens = _issue_token_pair(
            user_id=row.user_id,
            client_id=client.client_id,
            scope=row.scope or DEFAULT_SCOPE,
            resource=row.resource,
        )
        resp = jsonify(tokens)
        resp.headers['Cache-Control'] = 'no-store'
        return resp

    def _token_refresh(data: dict, client) -> Response:
        from models import OAuthAccessToken, OAuthRefreshToken, db

        refresh_plain = (data.get('refresh_token') or '').strip()
        resource = (data.get('resource') or '').strip() or None
        if not refresh_plain:
            return _oauth_error('invalid_request', 'refresh_token is required.')

        digest = _hash_token(refresh_plain)
        row = OAuthRefreshToken.query.filter_by(token_hash=digest).first()
        if row is None:
            return _oauth_error('invalid_grant', 'Invalid refresh token.')
        if row.revoked_at is not None:
            # Replay of an already-rotated token: assume theft and revoke the
            # whole user×client family (OAuth 2.1 §4.3.1 / BCP 4.14.2).
            if row.replaced_by_hash and row.client_id == client.client_id:
                revoke_user_client(row.user_id, row.client_id)
            return _oauth_error('invalid_grant', 'Refresh token revoked.')
        if _as_utc(row.expires_at) <= _utc_now():
            return _oauth_error('invalid_grant', 'Refresh token expired.')
        if row.client_id != client.client_id:
            return _oauth_error('invalid_grant', 'Token was issued to another client.')
        if resource is not None and not resource_matches_mcp(resource):
            return _oauth_error(
                'invalid_target',
                f'resource must be {mcp_resource_url()}.',
            )
        if not resource_matches_mcp(row.resource):
            return _oauth_error('invalid_grant', 'Token audience mismatch.')

        now = _utc_now()
        # Rotate: claim old refresh, then issue a new pair.
        claimed = (
            OAuthRefreshToken.query
            .filter_by(id=row.id, revoked_at=None)
            .update({'revoked_at': now}, synchronize_session=False)
        )
        db.session.commit()
        if not claimed:
            return _oauth_error('invalid_grant', 'Refresh token revoked.')

        # Also revoke outstanding access tokens for this client×user.
        (
            OAuthAccessToken.query
            .filter_by(user_id=row.user_id, client_id=client.client_id)
            .filter(OAuthAccessToken.revoked_at.is_(None))
            .update({'revoked_at': now}, synchronize_session=False)
        )
        db.session.commit()

        tokens = _issue_token_pair(
            user_id=row.user_id,
            client_id=client.client_id,
            scope=row.scope or DEFAULT_SCOPE,
            resource=row.resource,
        )
        # Record rotation link on the old refresh row.
        new_refresh_hash = _hash_token(tokens['refresh_token'])
        stale = OAuthRefreshToken.query.filter_by(token_hash=digest).first()
        if stale is not None:
            stale.replaced_by_hash = new_refresh_hash
            db.session.commit()

        resp = jsonify(tokens)
        resp.headers['Cache-Control'] = 'no-store'
        return resp

    @app_flask.post('/settings/oauth/revoke')
    def settings_oauth_revoke():
        """Revoke a connected OAuth app from Settings (CSRF-protected)."""
        A = _app()
        if not current_user.is_authenticated:
            return redirect(url_for('login', next=url_for('settings')))
        if not mcp_oauth_enabled():
            flash('Connected apps are not available.', 'error')
            return redirect(url_for('settings'))
        if not A.validate_csrf_token():
            flash('That form expired. Please try again.', 'error')
            return redirect(url_for('settings') + '#connected-apps')
        client_id = (request.form.get('client_id') or '').strip()
        if not client_id:
            flash('Missing app.', 'error')
            return redirect(url_for('settings') + '#connected-apps')
        if revoke_user_client(current_user.id, client_id):
            flash('Access revoked. That app can no longer use your account.', 'success')
        else:
            flash('That app was already disconnected.', 'success')
        return redirect(url_for('settings') + '#connected-apps')


def ensure_oauth_tables():
    """Create oauth_* tables if missing. Safe when two gunicorn workers race."""
    from sqlalchemy.exc import OperationalError
    from sqlalchemy import text

    from models import (
        OAUTH_ACCESS_TOKEN_COLUMN_MIGRATIONS,
        OAUTH_AUTHORIZATION_CODE_COLUMN_MIGRATIONS,
        OAUTH_CLIENT_COLUMN_MIGRATIONS,
        OAUTH_REFRESH_TOKEN_COLUMN_MIGRATIONS,
        db,
    )

    A = _app()
    statements = [
        """
            CREATE TABLE IF NOT EXISTS oauth_clients (
                id INTEGER NOT NULL PRIMARY KEY,
                client_id VARCHAR(512) NOT NULL UNIQUE,
                client_secret_hash VARCHAR(64),
                client_name VARCHAR(255),
                redirect_uris_json TEXT NOT NULL,
                grant_types_json TEXT NOT NULL,
                response_types_json TEXT NOT NULL,
                token_endpoint_auth_method VARCHAR(64) NOT NULL DEFAULT 'none',
                registration_source VARCHAR(16),
                created_at DATETIME
            )
        """,
        """
        CREATE TABLE IF NOT EXISTS oauth_authorization_codes (
            id INTEGER NOT NULL PRIMARY KEY,
            code_hash VARCHAR(64) NOT NULL UNIQUE,
            client_id VARCHAR(512) NOT NULL,
            user_id INTEGER NOT NULL,
            redirect_uri VARCHAR(1024) NOT NULL,
            code_challenge VARCHAR(128) NOT NULL,
            code_challenge_method VARCHAR(16) NOT NULL,
            scope VARCHAR(255),
            resource VARCHAR(512) NOT NULL,
            expires_at DATETIME NOT NULL,
            used_at DATETIME,
            created_at DATETIME,
            FOREIGN KEY(user_id) REFERENCES users (id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS oauth_access_tokens (
            id INTEGER NOT NULL PRIMARY KEY,
            token_hash VARCHAR(64) NOT NULL UNIQUE,
            client_id VARCHAR(512) NOT NULL,
            user_id INTEGER NOT NULL,
            scope VARCHAR(255),
            resource VARCHAR(512) NOT NULL,
            expires_at DATETIME NOT NULL,
            revoked_at DATETIME,
            created_at DATETIME,
            FOREIGN KEY(user_id) REFERENCES users (id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS oauth_refresh_tokens (
            id INTEGER NOT NULL PRIMARY KEY,
            token_hash VARCHAR(64) NOT NULL UNIQUE,
            client_id VARCHAR(512) NOT NULL,
            user_id INTEGER NOT NULL,
            scope VARCHAR(255),
            resource VARCHAR(512) NOT NULL,
            expires_at DATETIME NOT NULL,
            revoked_at DATETIME,
            replaced_by_hash VARCHAR(64),
            created_at DATETIME,
            FOREIGN KEY(user_id) REFERENCES users (id)
        )
        """,
    ]
    for ddl in statements:
        try:
            db.session.execute(text(ddl))
            db.session.commit()
        except OperationalError as exc:
            db.session.rollback()
            if 'already exists' not in str(exc).lower():
                raise
            A.app.logger.info('oauth table create raced another worker; continuing')

    migrations = (
        ('oauth_clients', OAUTH_CLIENT_COLUMN_MIGRATIONS),
        ('oauth_authorization_codes', OAUTH_AUTHORIZATION_CODE_COLUMN_MIGRATIONS),
        ('oauth_access_tokens', OAUTH_ACCESS_TOKEN_COLUMN_MIGRATIONS),
        ('oauth_refresh_tokens', OAUTH_REFRESH_TOKEN_COLUMN_MIGRATIONS),
    )
    for table, cols in migrations:
        existing = A._live_columns(table)
        if not existing:
            continue
        for column, ddl_type in cols.items():
            if column in existing:
                continue
            try:
                db.session.execute(text(
                    f'ALTER TABLE {table} ADD COLUMN {column} {ddl_type}'
                ))
                db.session.commit()
            except OperationalError as exc:
                db.session.rollback()
                if 'duplicate column name' not in str(exc).lower():
                    raise
                A.app.logger.info(
                    '%s.%s was added by another worker; continuing', table, column)

    index_specs = [
        ('ix_oauth_clients_client_id',
         'CREATE UNIQUE INDEX IF NOT EXISTS ix_oauth_clients_client_id '
         'ON oauth_clients (client_id)'),
        ('ix_oauth_authorization_codes_code_hash',
         'CREATE UNIQUE INDEX IF NOT EXISTS ix_oauth_authorization_codes_code_hash '
         'ON oauth_authorization_codes (code_hash)'),
        ('ix_oauth_authorization_codes_client_id',
         'CREATE INDEX IF NOT EXISTS ix_oauth_authorization_codes_client_id '
         'ON oauth_authorization_codes (client_id)'),
        ('ix_oauth_authorization_codes_user_id',
         'CREATE INDEX IF NOT EXISTS ix_oauth_authorization_codes_user_id '
         'ON oauth_authorization_codes (user_id)'),
        ('ix_oauth_access_tokens_token_hash',
         'CREATE UNIQUE INDEX IF NOT EXISTS ix_oauth_access_tokens_token_hash '
         'ON oauth_access_tokens (token_hash)'),
        ('ix_oauth_access_tokens_client_id',
         'CREATE INDEX IF NOT EXISTS ix_oauth_access_tokens_client_id '
         'ON oauth_access_tokens (client_id)'),
        ('ix_oauth_access_tokens_user_id',
         'CREATE INDEX IF NOT EXISTS ix_oauth_access_tokens_user_id '
         'ON oauth_access_tokens (user_id)'),
        ('ix_oauth_refresh_tokens_token_hash',
         'CREATE UNIQUE INDEX IF NOT EXISTS ix_oauth_refresh_tokens_token_hash '
         'ON oauth_refresh_tokens (token_hash)'),
        ('ix_oauth_refresh_tokens_client_id',
         'CREATE INDEX IF NOT EXISTS ix_oauth_refresh_tokens_client_id '
         'ON oauth_refresh_tokens (client_id)'),
        ('ix_oauth_refresh_tokens_user_id',
         'CREATE INDEX IF NOT EXISTS ix_oauth_refresh_tokens_user_id '
         'ON oauth_refresh_tokens (user_id)'),
    ]
    for name, ddl in index_specs:
        if 'oauth_authorization_codes' in ddl:
            table_name = 'oauth_authorization_codes'
        elif 'oauth_access_tokens' in ddl:
            table_name = 'oauth_access_tokens'
        elif 'oauth_refresh_tokens' in ddl:
            table_name = 'oauth_refresh_tokens'
        else:
            table_name = 'oauth_clients'
        if not A._live_columns(table_name):
            continue
        try:
            db.session.execute(text(ddl))
            db.session.commit()
        except OperationalError:
            db.session.rollback()
            A.app.logger.exception('Could not create %s', name)
