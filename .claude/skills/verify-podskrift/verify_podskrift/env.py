"""Environment isolation for verify-podskrift.

The CLI sets process env *before* starting Flask. ``python-dotenv``'s
``load_dotenv()`` uses ``override=False``, so empty-string values already in
``os.environ`` win over ``.env``. Tests assert this against the real app
loader, not only a helper.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, unquote

from .paths import (
    REPO_ROOT, db_path, default_port, durable_root, ensure_dirs, stub_port,
)

# Keys that must be empty (or absent) so Mailgun/PostHog/Sentry/STT vendors
# never fire from a copied ``.env`` during verify.
BLANK_KEYS = (
    'MAILGUN_API_KEY',
    'POSTHOG_KEY',
    'POSTHOG_API_KEY',
    'SENTRY_DSN',
    'TOGETHER_API_KEY',
    'SONIOX_API_KEY',
    'GROQ_API_KEY',
    'ELEVENLABS_API_KEY',
    'AGENT_API_KEY',
)

# Never let verify enable shadow comparison against prod vendors.
SHADOW_OFF = {
    'STT_SHADOW_RATE': '0',
    'STT_TOGETHER_SHADOW_RATE': '0',
    'SCRIBE_SHADOW_PCT': '0',
    'STT_SONIOX_LANGUAGES': '',
    'STT_PROVIDER': 'whisper-1',
}

LIVE_STRIPE_PREFIXES = ('sk_live_', 'rk_live_')
TEST_STRIPE_PREFIXES = ('sk_test_', 'rk_test_')


class GuardError(RuntimeError):
    """Raised when verify would touch prod or an unowned resource."""


def sqlite_file_from_url(database_url: str) -> Path | None:
    """Return the filesystem path for a sqlite DATABASE_URL, else None."""
    raw = (database_url or '').strip()
    if not raw.startswith('sqlite:'):
        return None
    # sqlite:////abs/path or sqlite:///rel/path
    if raw in ('sqlite:///:memory:', 'sqlite://', 'sqlite:///:memory'):
        return None
    if raw.startswith('sqlite:////'):
        return Path('/' + raw[len('sqlite:////'):]).resolve()
    if raw.startswith('sqlite:///'):
        rest = raw[len('sqlite:///'):]
        p = Path(rest)
        if not p.is_absolute():
            p = (REPO_ROOT / p).resolve()
        else:
            p = p.resolve()
        return p
    parsed = urlparse(raw)
    if parsed.path:
        return Path(unquote(parsed.path)).resolve()
    return None


def assert_sqlite_inside_verify_dir(
        database_url: str, *, verify_root: Path | None = None) -> Path:
    """Refuse any DB path outside the durable verify directory."""
    root = (verify_root or durable_root()).resolve()
    path = sqlite_file_from_url(database_url)
    if path is None:
        raise GuardError(
            f'DATABASE_URL must be a sqlite file under {root}, got {database_url!r}')
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise GuardError(
            f'SQLite path {path} is outside verify dir {root} — refuse. '
            'Never point verify at data/podcast.db or a prod copy.'
        ) from exc
    return path


def assert_stripe_keys_safe(environ: dict[str, str] | None = None) -> dict[str, str]:
    """Allow absent or test-mode Stripe keys; refuse live keys."""
    env = environ if environ is not None else os.environ
    out: dict[str, str] = {}
    for key in ('STRIPE_SECRET_KEY', 'STRIPE_WEBHOOK_SECRET'):
        val = (env.get(key) or '').strip()
        if not val:
            out[key] = ''
            continue
        if any(val.startswith(p) for p in LIVE_STRIPE_PREFIXES):
            raise GuardError(
                f'{key} looks like a live Stripe key ({val[:8]}…) — refuse. '
                'Use sk_test_/rk_test_ or leave unset.')
        if key == 'STRIPE_SECRET_KEY' and not any(
                val.startswith(p) for p in TEST_STRIPE_PREFIXES):
            # whsec_ is fine for webhook; secret key should be test-mode.
            if not val.startswith('whsec_'):
                raise GuardError(
                    f'{key} is set but is not sk_test_/rk_test_ — refuse.')
        out[key] = val
    return out


def scrub_secrets(text: str) -> str:
    """Redact key-shaped tokens from verify stdout/logs."""
    patterns = [
        r'sk-[A-Za-z0-9_-]{8,}',
        r'rk_(?:live|test)_[A-Za-z0-9]+',
        r'psk_[A-Za-z0-9_-]{8,}',
        r'poa_[A-Za-z0-9_-]{8,}',
        r'por_[A-Za-z0-9_-]{8,}',
        r'poc_[A-Za-z0-9_-]{8,}',
        r'phc_[A-Za-z0-9]+',
        r'whsec_[A-Za-z0-9]+',
        r'key-[A-Za-z0-9]{8,}',
    ]
    out = text
    for pat in patterns:
        out = re.sub(pat, '[redacted]', out)
    return out


def build_verify_environ(
        *,
        port: int | None = None,
        stt_mode: str = 'stub',
        real_openai_key: str | None = None,
        include_stripe_test: bool = True,
        base: dict[str, str] | None = None,
) -> dict[str, str]:
    """Return the env mapping the verify server process must start with.

    ``stt_mode``: ``stub`` (default) or ``full`` (one real Whisper call).
    """
    ensure_dirs()
    port = port or default_port()
    db = db_path()
    env = dict(base or os.environ)

    # Prod / analytics isolation — empty string must be present so dotenv loses.
    for key in BLANK_KEYS:
        env[key] = ''
    env.update(SHADOW_OFF)

    env['PODSKRIFT_ENV'] = 'dev'
    env['PODSKRIFT_VERIFY'] = '1'
    env['PODSKRIFT_VERIFY_STT'] = stt_mode
    env['TZ'] = 'Europe/Oslo'
    env['DATABASE_URL'] = f'sqlite:///{db}'
    env['SECRET_KEY'] = 'podskrift-verify-local-not-for-prod'
    env['PUBLIC_BASE_URL'] = f'http://127.0.0.1:{port}'
    env['EMAIL_ENABLED'] = '0'
    env['MCP_ENABLED'] = '1'
    env['MCP_OAUTH_ENABLED'] = '1'
    env['TRIAL_ENABLED'] = '1'
    env['TRIAL_SPLIT_ENABLED'] = '1'
    env['TRIAL_AB_ENABLED'] = '0'
    env['NEW_USER_TRIAL_MINUTES'] = '120'
    env['TRIAL_DAILY_MINUTES'] = '2000'
    env['ADMIN_EMAILS'] = 'verify-admin@podskrift.test'
    # Disable volume pricing noise unless a feature drive needs it.
    env.setdefault('VOLUME_PRICING_ENABLED', '0')
    env.setdefault('CHECKOUT_RECOVERY_ENABLED', '0')

    # Stub key satisfies trial_available(); real network is never used in stub.
    if stt_mode == 'full':
        key = (real_openai_key or env.get('OPENAI_API_KEY') or '').strip()
        if not key:
            raise GuardError(
                'full STT needs OPENAI_API_KEY in the process env '
                '(not from .env). Export it, then re-run with --stt full.')
        if os.environ.get('CI') or os.environ.get('GITHUB_ACTIONS'):
            raise GuardError('full STT is refused in CI.')
        env['OPENAI_API_KEY'] = key
    else:
        env['OPENAI_API_KEY'] = 'sk-verify-stub-not-a-real-key'

    env['PODSKRIFT_VERIFY_PORT'] = str(port)
    env['PODSKRIFT_VERIFY_STUB_PORT'] = str(stub_port(port))

    # Stripe: only keep test-mode keys already in the process env.
    if include_stripe_test:
        try:
            safe = assert_stripe_keys_safe(env)
            for k, v in safe.items():
                env[k] = v
        except GuardError:
            env['STRIPE_SECRET_KEY'] = ''
            env['STRIPE_WEBHOOK_SECRET'] = ''
            env['STRIPE_PRICE_ID'] = ''
    else:
        env['STRIPE_SECRET_KEY'] = ''
        env['STRIPE_WEBHOOK_SECRET'] = ''
        env['STRIPE_PRICE_ID'] = ''

    assert_sqlite_inside_verify_dir(env['DATABASE_URL'])
    return env


def apply_environ(env: dict[str, str]) -> None:
    """Write *env* into ``os.environ`` (used by the server child)."""
    # Clear then set so we do not leave stale live keys from the parent.
    for key in BLANK_KEYS:
        os.environ[key] = env.get(key, '')
    for key, value in env.items():
        os.environ[key] = value


def env_snapshot_for_doctor(env: dict[str, str] | None = None) -> dict[str, Any]:
    """Non-secret summary of the verify environment."""
    e = env or os.environ
    db = e.get('DATABASE_URL', '')
    path = sqlite_file_from_url(db)
    stripe = (e.get('STRIPE_SECRET_KEY') or '')
    stripe_mode = (
        'test' if any(stripe.startswith(p) for p in TEST_STRIPE_PREFIXES)
        else ('live-blocked' if any(stripe.startswith(p) for p in LIVE_STRIPE_PREFIXES)
              else 'unset')
    )
    return {
        'podskrift_env': e.get('PODSKRIFT_ENV'),
        'tz': e.get('TZ'),
        'database_url_kind': 'sqlite' if db.startswith('sqlite:') else 'other',
        'database_path': str(path) if path else None,
        'stt_mode': e.get('PODSKRIFT_VERIFY_STT', 'stub'),
        'openai_key_set': bool((e.get('OPENAI_API_KEY') or '').strip()),
        'openai_key_is_stub': (e.get('OPENAI_API_KEY') or '').startswith('sk-verify-stub'),
        'mailgun_blank': e.get('MAILGUN_API_KEY', None) == '',
        'posthog_blank': e.get('POSTHOG_KEY', None) == '',
        'sentry_blank': e.get('SENTRY_DSN', None) == '',
        'together_blank': e.get('TOGETHER_API_KEY', None) == '',
        'soniox_blank': e.get('SONIOX_API_KEY', None) == '',
        'groq_blank': e.get('GROQ_API_KEY', None) == '',
        'stripe_mode': stripe_mode,
        'mcp_enabled': e.get('MCP_ENABLED'),
        'shadow_rate': e.get('STT_SHADOW_RATE'),
        'public_base_url': e.get('PUBLIC_BASE_URL'),
    }
