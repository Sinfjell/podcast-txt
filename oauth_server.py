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
from urllib.parse import urlencode, urljoin, urlparse, urlunparse

import requests
from flask import (Response, current_app, flash, g, jsonify, redirect, render_template,
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

# CIMD / JWKS fetch limits (SSRF-hardened outbound HTTP).
CIMD_FETCH_TIMEOUT_SEC = 5
CIMD_FETCH_MAX_BYTES = 64 * 1024
JWKS_CACHE_SECONDS = int(os.getenv('OAUTH_JWKS_CACHE_SECONDS', '300'))
# Clock skew + ChatGPT assertion windows (PODSKRIFT-P).
JWT_ASSERTION_LEEWAY_SEC = 60
JWT_ASSERTION_MAX_LIFETIME_SEC = 3600  # exp may be up to 1h in the future
CLIENT_ASSERTION_TYPE = (
    'urn:ietf:params:oauth:client-assertion-type:jwt-bearer'
)
SUPPORTED_CLIENT_AUTH_METHODS = frozenset({'none', 'private_key_jwt',
                                           'client_secret_post'})
CIMD_CLIENT_AUTH_METHODS = frozenset({'none', 'private_key_jwt'})
# Asymmetric RS256 only (what ChatGPT signs with). Never none / HS*.
JWT_ALLOWED_ALGS = ('RS256',)
# Min seconds between forced JWKS refetches (unknown kid) per jwks_uri.
JWKS_FORCE_REFRESH_MIN_INTERVAL_SEC = 60

_OAUTH_DOC_BEGIN = '<!-- mcp-oauth-section -->'
_OAUTH_DOC_END = '<!-- /mcp-oauth-section -->'

_register_attempts: dict[str, list[float]] = collections.defaultdict(list)
_token_attempts: dict[str, list[float]] = collections.defaultdict(list)
_rate_lock = threading.Lock()
# jwks_uri → (fetched_at_monotonic, jwks_dict)
_jwks_cache: dict[str, tuple[float, dict]] = {}
_jwks_cache_lock = threading.Lock()
_jwks_last_forced: dict[str, float] = {}


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
    if h.startswith('['):  # IPv6 literal in netloc form
        return h in ('[::1]',)
    # urlparse(...).hostname returns ::1 without brackets.
    return h in ('localhost', '127.0.0.1', '::1') or h.endswith('.localhost')


def validate_redirect_uri(uri: str) -> bool:
    """https required, or http://localhost / 127.0.0.1 / [::1] for desktop clients."""
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


def redirect_uri_matches(requested: str, registered: str) -> bool:
    """True when requested matches a registered redirect_uri.

    Exact match always. For http loopback (localhost / 127.0.0.1 / ::1),
    RFC 8252 §7.3: match scheme+host+path and ignore port (Claude Code).
    Non-loopback URIs stay exact-match only.
    """
    if not requested or not registered:
        return False
    req_s = requested.strip()
    reg_s = registered.strip()
    if req_s == reg_s:
        return True
    try:
        req = urlparse(req_s)
        reg = urlparse(reg_s)
    except ValueError:
        return False
    if req.scheme != 'http' or reg.scheme != 'http':
        return False
    req_host = (req.hostname or '').lower()
    reg_host = (reg.hostname or '').lower()
    if not (_is_localhost_host(req_host) and _is_localhost_host(reg_host)):
        return False
    if req_host != reg_host:
        return False
    if (req.path or '') != (reg.path or ''):
        return False
    # Query must still match (port is the only ignored component).
    return (req.query or '') == (reg.query or '')


def redirect_uri_allowed(requested: str, allowed: list[str]) -> bool:
    """True when requested matches any registered redirect_uri (loopback-aware)."""
    return any(redirect_uri_matches(requested, reg) for reg in allowed)


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


def _client_id_host(client_id: str | None) -> str:
    """Hostname for Sentry tags — never the full client_id URL (may be long)."""
    if not client_id:
        return 'unknown'
    if looks_like_cimd_client_id(client_id):
        return (urlparse(client_id).hostname or 'unknown')[:120]
    return 'dcr'


def _is_e2e_test_request() -> bool:
    """True when the caller is our own E2E/negative harness (not real clients)."""
    try:
        ua = request.headers.get('User-Agent') or ''
    except RuntimeError:
        return False
    return ua.startswith('podskrift-e2e/')


def _report_oauth_refusal(*, step: str, reason: str, error: str | None = None,
                          client_id: str | None = None,
                          extra_tags: dict | None = None) -> None:
    """Surface refused authorize/token to Sentry without secrets. Never raises."""
    try:
        import sentry_sdk
        safe_reason = (reason or error or 'refused')[:160]
        # Strip anything that looks like a token/code/assertion fragment.
        if any(p in safe_reason.lower() for p in (
                'poa_', 'por_', 'poc_', 'psk_', 'bearer ', 'eyj')):
            safe_reason = (error or reason or 'refused')[:160]
            if any(p in safe_reason.lower() for p in (
                    'poa_', 'por_', 'poc_', 'psk_', 'bearer ', 'eyj')):
                safe_reason = 'refused'
        tags = {
            'oauth_step': (step or 'unknown')[:64],
            'reason': safe_reason[:120],
            'client_id_host': _client_id_host(client_id),
            'oauth_error': (error or '')[:64],
        }
        is_test = _is_e2e_test_request()
        if is_test:
            tags['test'] = 'true'
        if extra_tags:
            for key, value in extra_tags.items():
                if value is None:
                    continue
                tags[str(key)[:64]] = str(value)[:120]
        sentry_sdk.capture_message(
            f'OAuth {step} refused: {safe_reason}',
            # Self-tests must not page; real client refusals stay warning.
            level='info' if is_test else 'warning',
            tags=tags,
            fingerprint=['oauth-refusal', step or 'unknown', safe_reason[:64]],
        )
    except Exception:  # noqa: BLE001 — reporting must never break OAuth
        pass


def _oauth_error(error: str, description: str | None = None, status: int = 400,
                 *, step: str | None = None, client_id: str | None = None,
                 reason: str | None = None, extra_tags: dict | None = None):
    # Prefer explicit args; fall back to request-scoped context set by handlers.
    step = step or getattr(g, 'oauth_step', None) or 'token'
    client_id = client_id or getattr(g, 'oauth_client_id', None)
    refusal_reason = reason or description or error
    _report_oauth_refusal(
        step=step, reason=refusal_reason, error=error, client_id=client_id,
        extra_tags=extra_tags)
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

def lookup_access_token_row(plaintext: str):
    """Return a valid OAuthAccessToken row, or None."""
    if not plaintext or not plaintext.startswith(ACCESS_TOKEN_PREFIX):
        return None
    from models import OAuthAccessToken
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
    return row


def lookup_access_token_user(plaintext: str):
    """Return User for a valid (non-revoked, non-expired) access token, or None."""
    from models import User, db
    row = lookup_access_token_row(plaintext)
    if row is None:
        return None
    return db.session.get(User, row.user_id)


def lookup_access_token_client_name(plaintext: str) -> str | None:
    """OAuth client_name for a valid access token, when known."""
    from models import OAuthClient
    row = lookup_access_token_row(plaintext)
    if row is None:
        return None
    client = OAuthClient.query.filter_by(client_id=row.client_id).first()
    if client is None:
        return None
    name = (client.client_name or '').strip()
    return name[:64] or None


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
        # ChatGPT CIMD uses none / private_key_jwt; DCR clients registered with
        # client_secret_post keep working (do not drop it from discovery).
        'token_endpoint_auth_methods_supported': [
            'none', 'private_key_jwt', 'client_secret_post'],
        'token_endpoint_auth_signing_alg_values_supported': ['RS256'],
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


def _fetch_https_json(url: str, *, label: str,
                      max_redirects: int = 3) -> tuple[dict | None, str | None]:
    """SSRF-safe HTTPS GET of a small JSON object (CIMD / JWKS).

    Follows a few same-origin HTTPS redirects (ChatGPT/CDN sometimes 302).
    Error strings include ``HTTP <status>`` when applicable for Sentry tags.
    """
    current = url
    for _ in range(max_redirects + 1):
        try:
            parsed = urlparse(current)
        except ValueError:
            return None, f'{label} URL is invalid.'
        if parsed.scheme.lower() != 'https' or not parsed.netloc:
            return None, f'{label} must be an HTTPS URL.'
        if parsed.username or parsed.password or parsed.fragment:
            return None, f'{label} URL is invalid.'
        try:
            port = parsed.port
        except ValueError:
            return None, f'{label} URL is invalid.'
        if port not in (None, 443):
            return None, f'{label} host is not allowed.'
        if not _cimd_host_is_safe(parsed.hostname or ''):
            return None, f'{label} host is not allowed.'
        try:
            resp = requests.get(
                current,
                timeout=CIMD_FETCH_TIMEOUT_SEC,
                allow_redirects=False,
                headers={
                    'Accept': 'application/json',
                    'User-Agent': 'Podskrift-OAuth/1.0',
                },
                stream=True,
            )
        except requests.RequestException:
            return None, f'{label} fetch failed (network).'
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get('Location') or ''
            resp.close()
            if not location:
                return None, f'{label} fetch returned HTTP {resp.status_code}.'
            next_url = urljoin(current, location)
            if not _same_https_origin(next_url, current):
                return None, f'{label} fetch returned HTTP {resp.status_code}.'
            current = next_url
            continue
        if resp.status_code != 200:
            status = resp.status_code
            resp.close()
            return None, f'{label} fetch returned HTTP {status}.'
        chunks = []
        total = 0
        deadline = time.monotonic() + CIMD_FETCH_TIMEOUT_SEC
        try:
            for chunk in resp.iter_content(chunk_size=4096):
                if time.monotonic() > deadline:
                    return None, f'{label} fetch timed out.'
                if not chunk:
                    continue
                total += len(chunk)
                if total > CIMD_FETCH_MAX_BYTES:
                    return None, f'{label} document too large.'
                chunks.append(chunk)
        finally:
            resp.close()
        try:
            doc = json.loads(b''.join(chunks).decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None, f'{label} document is not valid JSON.'
        if not isinstance(doc, dict):
            return None, f'{label} document must be a JSON object.'
        return doc, None
    return None, f'{label} fetch returned HTTP 310.'


def fetch_cimd_document(client_id_url: str) -> tuple[dict | None, str | None]:
    """GET a Client ID Metadata Document. Returns (doc, error_description)."""
    if not looks_like_cimd_client_id(client_id_url):
        return None, 'client_id is not a valid CIMD HTTPS URL.'
    doc, err = _fetch_https_json(client_id_url, label='CIMD')
    if err:
        return None, err
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


def _same_https_origin(url_a: str, url_b: str) -> bool:
    try:
        a = urlparse(url_a)
        b = urlparse(url_b)
    except ValueError:
        return False
    if a.scheme.lower() != 'https' or b.scheme.lower() != 'https':
        return False
    host_a = (a.hostname or '').lower().rstrip('.')
    host_b = (b.hostname or '').lower().rstrip('.')
    if not host_a or host_a != host_b:
        return False
    port_a = a.port or 443
    port_b = b.port or 443
    return port_a == port_b


def _cimd_jwks_uri(doc: dict, client_id_url: str) -> str | None:
    """Return a same-origin HTTPS jwks_uri, or None when absent/invalid."""
    jwks_uri = doc.get('jwks_uri')
    if not isinstance(jwks_uri, str) or not jwks_uri.strip():
        return None
    jwks_uri = jwks_uri.strip()
    if len(jwks_uri) > 512:
        return None
    if not _same_https_origin(jwks_uri, client_id_url):
        return None
    try:
        parsed = urlparse(jwks_uri)
    except ValueError:
        return None
    if parsed.username or parsed.password or parsed.fragment:
        return None
    if (parsed.path or '') in ('', '/'):
        return None
    return jwks_uri


def _cimd_auth_methods(doc: dict) -> tuple[str | None, list[str]]:
    """Return (primary_method, allowed_methods) for none / private_key_jwt."""
    collected: list[str] = []
    for key in ('token_endpoint_auth_methods_supported',
                'token_endpoint_auth_methods'):
        methods = doc.get(key)
        if isinstance(methods, list) and methods:
            for m in methods:
                if isinstance(m, str) and m.strip():
                    collected.append(m.strip())
    single = doc.get('token_endpoint_auth_method')
    if isinstance(single, str) and single.strip():
        primary = single.strip()
    else:
        primary = None
        # OIDC default for public clients when the field is omitted.
        if not collected:
            return 'none', ['none']
    if primary is not None:
        collected.append(primary)
    allowed: list[str] = []
    for m in collected:
        if m in CIMD_CLIENT_AUTH_METHODS and m not in allowed:
            allowed.append(m)
    if not allowed:
        return None, []
    if primary in CIMD_CLIENT_AUTH_METHODS:
        return primary, allowed
    if 'private_key_jwt' in allowed:
        return 'private_key_jwt', allowed
    return 'none', allowed


def _cimd_auth_method(doc: dict) -> str | None:
    """Back-compat: primary CIMD auth method only."""
    primary, _allowed = _cimd_auth_methods(doc)
    return primary


def fetch_jwks(jwks_uri: str, *, force_refresh: bool = False) -> tuple[dict | None, str | None]:
    """Fetch + cache a JWKS document (short TTL)."""
    now = time.monotonic()
    if force_refresh:
        # Unknown-kid refetches are attacker-triggerable: throttle them.
        with _jwks_cache_lock:
            last = _jwks_last_forced.get(jwks_uri)
            if last is not None and now - last < JWKS_FORCE_REFRESH_MIN_INTERVAL_SEC:
                cached = _jwks_cache.get(jwks_uri)
                if cached is not None:
                    return cached[1], None
            _jwks_last_forced[jwks_uri] = now
    else:
        with _jwks_cache_lock:
            cached = _jwks_cache.get(jwks_uri)
            if cached is not None:
                fetched_at, jwks = cached
                if now - fetched_at < JWKS_CACHE_SECONDS:
                    return jwks, None
    jwks, err = _fetch_https_json(jwks_uri, label='JWKS')
    if err:
        return None, err
    keys = jwks.get('keys')
    if not isinstance(keys, list):
        return None, 'JWKS document must contain a keys array.'
    with _jwks_cache_lock:
        _jwks_cache[jwks_uri] = (time.monotonic(), jwks)
    return jwks, None


def _jwk_to_key(jwk: dict):
    """Convert a public RSA JWK dict to a cryptography key (RS256 only)."""
    import jwt
    from jwt.algorithms import RSAAlgorithm

    kty = jwk.get('kty')
    if kty != 'RSA':
        raise jwt.InvalidKeyError(f'Unsupported JWK kty: {kty!r}')
    if jwk.get('use') not in (None, 'sig'):
        raise jwt.InvalidKeyError('JWK is not a signing key.')
    if jwk.get('alg') not in (None, 'RS256'):
        raise jwt.InvalidKeyError('JWK alg is not RS256.')
    if 'd' in jwk:
        # Never accept a published private key as a verification key.
        raise jwt.InvalidKeyError('JWK must be a public key.')
    return RSAAlgorithm.from_jwk(jwk)


def _find_jwk(jwks: dict, kid: str | None) -> dict | None:
    keys = jwks.get('keys') or []
    if not isinstance(keys, list):
        return None
    if kid:
        for key in keys:
            if isinstance(key, dict) and key.get('kid') == kid:
                return key
        return None
    # No kid: only unambiguous when the set has a single signing key.
    usable = [
        k for k in keys
        if isinstance(k, dict) and k.get('kty') == 'RSA'
        and k.get('use', 'sig') in ('sig', None)
    ]
    if len(usable) == 1:
        return usable[0]
    return None


def _consume_jti(jti: str, expires_at: datetime) -> bool:
    """Record jti as used. Returns False if already seen (replay)."""
    from sqlalchemy.exc import IntegrityError
    from models import OAuthJwtJti, db

    digest = _hash_token(jti)
    # Drop expired rows opportunistically (best-effort; not required for correctness).
    now = _utc_now()
    try:
        OAuthJwtJti.query.filter(OAuthJwtJti.expires_at < now).delete(
            synchronize_session=False)
        db.session.commit()
    except Exception:
        db.session.rollback()

    row = OAuthJwtJti(jti_hash=digest, expires_at=expires_at)
    db.session.add(row)
    try:
        db.session.commit()
        return True
    except IntegrityError:
        db.session.rollback()
        return False


def _normalize_audience_value(value: str) -> str:
    """Strip trailing slashes so issuer/token URL variants compare equal."""
    return (value or '').strip().rstrip('/')


def _audience_values(aud) -> list[str]:
    if isinstance(aud, list):
        return [str(a) for a in aud if isinstance(a, (str, int))]
    if isinstance(aud, (str, int)):
        return [str(aud)]
    return []


def _accepted_audiences() -> set[str]:
    """RFC 7523: aud may be the AS issuer or the token endpoint URL."""
    issuer = _normalize_audience_value(oauth_issuer())
    token_endpoint = _normalize_audience_value(f'{oauth_issuer()}/oauth/token')
    return {issuer, token_endpoint}


def verify_private_key_jwt(
        client, assertion: str,
) -> tuple[bool, str | None, dict]:
    """Verify a private_key_jwt client_assertion (RFC 7523 / OIDC core).

    Returns ``(ok, reason_code, extra_tags)``. reason_code is one of the
    PODSKRIFT-P tags (``aud_mismatch``, ``bad_signature``, …) on failure.
    """
    import jwt

    tags: dict = {}
    if not assertion or not isinstance(assertion, str) or len(assertion) > 8192:
        return False, 'bad_signature', tags
    jwks_uri = getattr(client, 'jwks_uri', None)
    if not jwks_uri:
        return False, 'jwks_fetch_failed', tags

    try:
        header = jwt.get_unverified_header(assertion)
    except jwt.PyJWTError:
        return False, 'bad_signature', tags
    alg = header.get('alg')
    kid = header.get('kid') if isinstance(header.get('kid'), str) else None
    tags['jwt_alg'] = alg
    tags['jwt_kid'] = kid

    # Peek at unverified claims for diagnostics (never trusted alone).
    try:
        unverified = jwt.decode(assertion, options={'verify_signature': False})
    except jwt.PyJWTError:
        unverified = {}
    if isinstance(unverified, dict):
        tags['jwt_iss'] = unverified.get('iss')
        aud_peek = unverified.get('aud')
        if isinstance(aud_peek, list):
            tags['jwt_aud'] = ','.join(str(a) for a in aud_peek[:4])
        elif aud_peek is not None:
            tags['jwt_aud'] = aud_peek

    if alg not in JWT_ALLOWED_ALGS:
        return False, 'alg_not_allowed', tags

    jwks, err = fetch_jwks(jwks_uri)
    if err:
        status_m = re.search(r'HTTP (\d+)', err or '')
        if status_m:
            tags['jwks_status'] = status_m.group(1)
        return False, 'jwks_fetch_failed', tags
    jwk = _find_jwk(jwks, kid)
    if jwk is None and kid:
        # Unknown kid → refetch once (key rotation).
        jwks, err = fetch_jwks(jwks_uri, force_refresh=True)
        if err:
            status_m = re.search(r'HTTP (\d+)', err or '')
            if status_m:
                tags['jwks_status'] = status_m.group(1)
            return False, 'jwks_fetch_failed', tags
        jwk = _find_jwk(jwks, kid)
    if jwk is None:
        return False, 'kid_not_found', tags

    try:
        key = _jwk_to_key(jwk)
    except Exception:
        return False, 'bad_signature', tags

    # Verify signature + exp/nbf with leeway; check aud ourselves (slash-tolerant).
    try:
        claims = jwt.decode(
            assertion,
            key=key,
            algorithms=[alg],
            options={
                'require': ['exp', 'iss', 'sub'],
                'verify_aud': False,
            },
            leeway=JWT_ASSERTION_LEEWAY_SEC,
        )
    except jwt.ExpiredSignatureError:
        return False, 'expired', tags
    except jwt.ImmatureSignatureError:
        return False, 'not_yet_valid', tags
    except jwt.InvalidSignatureError:
        return False, 'bad_signature', tags
    except jwt.PyJWTError:
        return False, 'bad_signature', tags

    if (claims.get('iss') != client.client_id
            or claims.get('sub') != client.client_id):
        return False, 'iss_sub_mismatch', tags

    aud_values = _audience_values(claims.get('aud'))
    if not aud_values:
        return False, 'aud_mismatch', tags
    accepted = _accepted_audiences()
    if not any(_normalize_audience_value(a) in accepted for a in aud_values):
        return False, 'aud_mismatch', tags

    now_ts = int(_utc_now().timestamp())
    try:
        exp_ts = int(claims['exp'])
    except (TypeError, ValueError, KeyError):
        return False, 'expired', tags
    # exp may be at most MAX_LIFETIME in the future (generous 1h for ChatGPT).
    if exp_ts - now_ts > JWT_ASSERTION_MAX_LIFETIME_SEC + JWT_ASSERTION_LEEWAY_SEC:
        return False, 'lifetime_too_long', tags

    iat = claims.get('iat')
    if iat is not None:
        try:
            iat_ts = int(iat)
        except (TypeError, ValueError):
            return False, 'not_yet_valid', tags
        if iat_ts > now_ts + JWT_ASSERTION_LEEWAY_SEC:
            return False, 'not_yet_valid', tags

    nbf = claims.get('nbf')
    if nbf is not None:
        try:
            nbf_ts = int(nbf)
        except (TypeError, ValueError):
            return False, 'not_yet_valid', tags
        if nbf_ts > now_ts + JWT_ASSERTION_LEEWAY_SEC:
            return False, 'not_yet_valid', tags

    jti = claims.get('jti')
    if isinstance(jti, str) and jti.strip() and len(jti) <= 256:
        expires_at = datetime.fromtimestamp(
            exp_ts + JWT_ASSERTION_LEEWAY_SEC + 60, tz=timezone.utc)
        if not _consume_jti(f'{client.client_id}\n{jti.strip()}', expires_at):
            return False, 'jti_replay', tags
    else:
        # ChatGPT usually sends jti; allow missing but log for diagnosis.
        try:
            current_app.logger.info(
                'oauth private_key_jwt missing jti: client_host=%s alg=%s kid=%s',
                _client_id_host(client.client_id), alg, kid)
        except Exception:
            pass
    return True, None, tags


def _unverified_assertion_subject(assertion: str) -> str | None:
    """Lookup-only iss/sub from an unverified JWT (never trusted alone)."""
    import jwt
    if not assertion or len(assertion) > 8192:
        return None
    try:
        claims = jwt.decode(assertion, options={'verify_signature': False})
    except jwt.PyJWTError:
        return None
    sub = claims.get('sub')
    iss = claims.get('iss')
    if isinstance(sub, str) and len(sub) <= 512:
        return sub
    if isinstance(iss, str) and len(iss) <= 512:
        return iss
    return None


def _client_allowed_auth_methods(client) -> set[str]:
    """Auth methods this client may use at the token endpoint."""
    raw = getattr(client, 'token_endpoint_auth_methods_json', None)
    if raw:
        listed = _json_list(raw, default=[])
        allowed = {m for m in listed if m in SUPPORTED_CLIENT_AUTH_METHODS}
        if allowed:
            return allowed
    method = (client.token_endpoint_auth_method or 'none').strip()
    if method in SUPPORTED_CLIENT_AUTH_METHODS:
        return {method}
    return {'none'}


def authenticate_oauth_client(
        client, data: dict,
) -> tuple[bool, str | None, dict]:
    """Authenticate the client at the token endpoint.

    Returns ``(ok, reason_or_description, extra_tags)``.
    """
    allowed = _client_allowed_auth_methods(client)
    assertion = (data.get('client_assertion') or '').strip()
    assertion_type = (data.get('client_assertion_type') or '').strip()

    if 'client_secret_post' in allowed and 'private_key_jwt' not in allowed and 'none' not in allowed:
        secret = (data.get('client_secret') or '').strip()
        if not secret or not client.client_secret_hash:
            return False, 'client_secret required.', {}
        if not hmac.compare_digest(_hash_token(secret), client.client_secret_hash):
            return False, 'Invalid client_secret.', {}
        return True, None, {}

    if assertion:
        if 'private_key_jwt' not in allowed:
            return False, 'client_assertion not accepted for this client.', {}
        if assertion_type != CLIENT_ASSERTION_TYPE:
            return False, 'client_assertion_type must be jwt-bearer.', {}
        return verify_private_key_jwt(client, assertion)

    # No assertion: public-client path only when 'none' is an allowed method
    # (ChatGPT advertises both none and private_key_jwt).
    if 'none' in allowed:
        return True, None, {}

    if 'client_secret_post' in allowed:
        secret = (data.get('client_secret') or '').strip()
        if not secret or not client.client_secret_hash:
            return False, 'client_secret required.', {}
        if not hmac.compare_digest(_hash_token(secret), client.client_secret_hash):
            return False, 'Invalid client_secret.', {}
        return True, None, {}

    if 'private_key_jwt' in allowed:
        return False, 'client_assertion required for private_key_jwt.', {}
    return False, 'Unsupported token_endpoint_auth_method.', {}


def upsert_cimd_client(client_id_url: str, doc: dict):
    """Persist / refresh a CIMD client row from a validated metadata document."""
    from models import OAuthClient, db
    auth_method, allowed_methods = _cimd_auth_methods(doc)
    if auth_method is None or not allowed_methods:
        return None, (
            'CIMD token_endpoint_auth_method must include none or private_key_jwt.'
        )
    jwks_uri = _cimd_jwks_uri(doc, client_id_url)
    if 'private_key_jwt' in allowed_methods and not jwks_uri:
        return None, 'CIMD private_key_jwt requires a same-origin https jwks_uri.'
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
    methods_json = json.dumps(allowed_methods)

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
            token_endpoint_auth_methods_json=methods_json,
            jwks_uri=jwks_uri,
            registration_source='cimd',
        )
        db.session.add(row)
    else:
        row.client_name = name
        row.redirect_uris_json = json.dumps(uris)
        row.grant_types_json = json.dumps(grant_types)
        row.response_types_json = json.dumps(response_types)
        row.token_endpoint_auth_method = auth_method
        row.token_endpoint_auth_methods_json = methods_json
        row.jwks_uri = jwks_uri
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
    if (row is not None and not force_refresh and allow_fetch
            and getattr(row, 'registration_source', None) == 'cimd'
            and not getattr(row, 'token_endpoint_auth_methods_json', None)):
        # CIMD rows stored before multi-method support (e.g. ChatGPT, saved as
        # private_key_jwt-only) must re-read the document once so a client that
        # also declares 'none' is not locked to a single method.
        force_refresh = True
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
                              description: str, state: str | None,
                              *, client_id: str | None = None):
    if not redirect_uri or not validate_redirect_uri(redirect_uri):
        return _oauth_error(
            error, description, status=400, step='authorize', client_id=client_id)
    _report_oauth_refusal(
        step='authorize', reason=description or error, error=error,
        client_id=client_id)
    params = {'error': error, 'error_description': description, 'iss': oauth_issuer()}
    if state is not None:
        params['state'] = state
    return _redirect_with_params(redirect_uri, params)


def _https_origin(uri: str) -> str | None:
    """Return scheme://host[:port] for a validated https redirect_uri."""
    try:
        parsed = urlparse(uri)
    except ValueError:
        return None
    if parsed.scheme.lower() != 'https' or not parsed.hostname:
        return None
    host = parsed.hostname.lower()
    try:
        port = parsed.port
    except ValueError:
        return None
    if port and port != 443:
        return f'https://{host}:{port}'
    return f'https://{host}'


def _consent_csp_headers(redirect_uri: str) -> dict[str, str]:
    """CSP for consent: deny framing; allow form-action to redirect origin (PODSKRIFT-M)."""
    from site_standards import csp_report_only

    form_parts = ["'self'", 'https://checkout.stripe.com']
    origin = _https_origin(redirect_uri)
    if origin and origin not in form_parts:
        form_parts.append(origin)
    form_action = 'form-action ' + ' '.join(form_parts)
    enforced = (
        "frame-ancestors 'none'; base-uri 'self'; object-src 'none'; "
        f'{form_action}'
    )
    report_only = csp_report_only()
    if 'form-action' in report_only:
        report_only = re.sub(r'form-action[^;]*', form_action, report_only)
    else:
        report_only = f'{report_only}; {form_action}'
    return {
        'Content-Security-Policy': enforced,
        'Content-Security-Policy-Report-Only': report_only,
    }


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
            token_endpoint_auth_methods_json=json.dumps([auth_method]),
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
        g.oauth_step = 'authorize'
        g.oauth_client_id = client_id or None

        from models import OAuthAuthorizationCode, db

        client, client_err = resolve_oauth_client(client_id)
        if client is None:
            return _oauth_error(
                'invalid_request', client_err or 'Unknown client_id.', 400,
                step='authorize', client_id=client_id or None)

        allowed = _client_redirect_uris(client)
        if (not redirect_uri_allowed(redirect_uri, allowed)
                and looks_like_cimd_client_id(client_id)):
            # Redirect URIs may have rotated in the client's published CIMD.
            client, _ = resolve_oauth_client(client_id, force_refresh=True)
            allowed = _client_redirect_uris(client) if client else []
        if client is None or not redirect_uri_allowed(redirect_uri, allowed):
            return _oauth_error(
                'invalid_request',
                'redirect_uri is not registered for this client.',
                400,
                step='authorize', client_id=client_id or None,
            )

        if response_type != 'code':
            return _authorize_error_redirect(
                redirect_uri, 'unsupported_response_type',
                'Only response_type=code is supported.', state,
                client_id=client_id)

        if code_challenge_method != 'S256' or not code_challenge:
            return _authorize_error_redirect(
                redirect_uri, 'invalid_request',
                'PKCE S256 (code_challenge + code_challenge_method=S256) is required.',
                state, client_id=client_id)

        if not re.fullmatch(r'[A-Za-z0-9_-]{43,128}', code_challenge):
            return _authorize_error_redirect(
                redirect_uri, 'invalid_request',
                'code_challenge must be a valid S256 challenge.', state,
                client_id=client_id)

        if not resource_matches_mcp(resource):
            return _authorize_error_redirect(
                redirect_uri, 'invalid_target',
                f'resource must be {mcp_resource_url()}.', state,
                client_id=client_id)

        scope_str = _scope_string(scope)
        client_label = client.client_name or client.client_id

        if request.method == 'GET':
            # Do NOT pass csrf_token=… into render_template: that shadows the
            # context-processor csrf_token() callable that base.html →
            # _buy_modal.html invokes when Stripe is on (Sentry PODSKRIFT-J).
            # Ensure a token exists; the template calls csrf_token().
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
            # PostHog toolbar origins the site-wide CSP allows. form-action
            # must allow the validated redirect_uri origin (PODSKRIFT-M).
            page.headers['X-Frame-Options'] = 'DENY'
            for header, value in _consent_csp_headers(redirect_uri).items():
                page.headers[header] = value
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
                'The user denied the request.', state, client_id=client_id)

        # Re-check redirect_uri from the form against the registered set.
        form_redirect = (request.form.get('redirect_uri') or '').strip()
        if (form_redirect != redirect_uri
                or not redirect_uri_allowed(form_redirect, allowed)):
            return _oauth_error(
                'invalid_request', 'redirect_uri mismatch.', 400,
                step='authorize', client_id=client_id)

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
        if not client_id and (data.get('client_assertion') or '').strip():
            # RFC 7523 §3.1: client_id may be omitted with an assertion. Use the
            # unverified sub only as a lookup key; verify_private_key_jwt then
            # requires iss == sub == the stored client's client_id + signature.
            client_id = _unverified_assertion_subject(
                (data.get('client_assertion') or '').strip()) or ''
        g.oauth_step = 'token'
        g.oauth_client_id = client_id or None

        from models import (OAuthAccessToken, OAuthAuthorizationCode,
                            OAuthRefreshToken, db)

        # No outbound CIMD fetch from this unauthenticated endpoint: a client
        # that completed /oauth/authorize is already stored. JWKS fetch for
        # private_key_jwt still happens below via the cached jwks_uri.
        client, client_err = resolve_oauth_client(client_id, allow_fetch=False)
        if client is None:
            return _oauth_error(
                'invalid_client', client_err or 'Unknown client_id.', 401)

        g.oauth_client_id = client.client_id
        ok, auth_err, auth_tags = authenticate_oauth_client(client, data)
        if not ok:
            # Non-sensitive reason only (no assertion/secret), so a failed
            # ChatGPT/Claude connect can be diagnosed from server logs + Sentry.
            current_app.logger.warning(
                'oauth token client auth failed: client=%s method=%s grant=%s '
                'reason=%s alg=%s kid=%s aud=%s',
                client.client_id[:120],
                client.token_endpoint_auth_method, grant_type, auth_err,
                (auth_tags or {}).get('jwt_alg'),
                (auth_tags or {}).get('jwt_kid'),
                (auth_tags or {}).get('jwt_aud'))
            return _oauth_error(
                'invalid_client',
                auth_err or 'Client authentication failed.',
                401,
                reason=auth_err or 'Client authentication failed.',
                extra_tags=auth_tags or None,
            )

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
                token_endpoint_auth_methods_json TEXT,
                jwks_uri VARCHAR(512),
                registration_source VARCHAR(16),
                created_at DATETIME
            )
        """,
        """
        CREATE TABLE IF NOT EXISTS oauth_jwt_jtis (
            jti_hash VARCHAR(64) NOT NULL PRIMARY KEY,
            expires_at DATETIME NOT NULL,
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
        ('ix_oauth_jwt_jtis_expires_at',
         'CREATE INDEX IF NOT EXISTS ix_oauth_jwt_jtis_expires_at '
         'ON oauth_jwt_jtis (expires_at)'),
    ]
    for name, ddl in index_specs:
        if 'oauth_authorization_codes' in ddl:
            table_name = 'oauth_authorization_codes'
        elif 'oauth_access_tokens' in ddl:
            table_name = 'oauth_access_tokens'
        elif 'oauth_refresh_tokens' in ddl:
            table_name = 'oauth_refresh_tokens'
        elif 'oauth_jwt_jtis' in ddl:
            table_name = 'oauth_jwt_jtis'
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
