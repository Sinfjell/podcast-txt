"""Guardrails so agents cannot drift from DESIGN.md.

Scans templates/ and static/components.css for forbidden patterns:
inline style=, hard-coded hex outside the token section, pills, shadows,
gradients, extra primary buttons, and monospace/serif misuse.

Legacy templates that still use style= (or multiple primaries in mutually
exclusive branches) are capped by ratchet allowlists — counts must not rise,
and new templates must stay clean.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

import pytest

if not os.environ.get('DATABASE_URL'):
    _TEST_DB = os.path.join(tempfile.mkdtemp(prefix='podskrift-ds-'), 'test.db')
    os.environ['DATABASE_URL'] = f'sqlite:///{_TEST_DB}'
os.environ.setdefault('SENTRY_DSN', '')
os.environ.setdefault('POSTHOG_KEY', '')
os.environ.setdefault('POSTHOG_HOST', '')
os.environ.setdefault('PODSKRIFT_DISABLE_WATCHDOG', '1')

import app as A  # noqa: E402
from models import User, db  # noqa: E402

ROOT = Path(__file__).resolve().parent
TEMPLATES = ROOT / 'templates'
COMPONENTS_CSS = ROOT / 'static' / 'components.css'
BASE_HTML = TEMPLATES / 'base.html'

# Frozen ratchet: relative path -> max allowed style= attributes.
# Do not raise these numbers. Prefer deleting entries when a file is cleaned.
LEGACY_STYLE_ALLOWLIST: dict[str, int] = {
    '_buy_modal.html': 1,
    'admin/dashboard.html': 16,
    'admin/user_detail.html': 2,
    'billing_success.html': 12,
    'email_unsubscribe_confirm.html': 2,
    'email_unsubscribed.html': 2,
    'episode_selection.html': 12,
    'error.html': 2,
    'forgot_password.html': 5,
    'go_transcribe.html': 4,
    'history.html': 14,
    'index.html': 18,
    'pricing.html': 1,
    'register.html': 3,
    'reset_password.html': 6,
    'rss_help.html': 8,
    'settings.html': 30,
    'transcription.html': 36,
}

# Pages that already ship more than one btn-primary in the template source
# (often mutually exclusive Jinja branches). Cap, do not raise.
LEGACY_PRIMARY_ALLOWLIST: dict[str, int] = {
    'billing_success.html': 4,
    'index.html': 2,
    'pricing.html': 2,
    'settings.html': 2,
    'transcription.html': 6,
}

# Standalone error page carries its own mini token block (no base.html).
HEX_TOKEN_FILE_ALLOWLIST = {
    'error_500.html',
}

# Admin Chart.js config still uses canvas colours (not page UI tokens).
HEX_LINE_ALLOW_RE = re.compile(
    r'admin/dashboard\.html:\d+:\s*ticks:|admin/dashboard\.html:\d+:\s*grid:',
)

PRIMARY_BUTTON_EXEMPT = {
    'design_components.html',
    'components/macros.html',
    'base.html',
}

HEX_RE = re.compile(r'#[0-9A-Fa-f]{3,8}\b')
STYLE_ATTR_RE = re.compile(r'\bstyle\s*=', re.I)
PILL_RADIUS_RE = re.compile(
    r'border-radius\s*:\s*(9999px|999px)\b',
    re.I,
)
BOX_SHADOW_RE = re.compile(r'\b(box-shadow|drop-shadow|text-shadow)\s*:', re.I)
GRADIENT_RE = re.compile(r'\b(linear-gradient|radial-gradient|conic-gradient)\s*\(', re.I)
MONO_FONT_RE = re.compile(
    r'font-family\s*:\s*[^;]*(monospace|ui-monospace|SFMono|Menlo|Consolas|Courier)',
    re.I,
)
SERIF_UI_RE = re.compile(
    r'font-family\s*:\s*[^;]*(Newsreader|var\(--font-serif\))',
    re.I,
)
PRIMARY_CLASS_RE = re.compile(
    r'''class\s*=\s*["'][^"']*\b(?:btn-primary|btn-accent)\b[^"']*["']''',
    re.I,
)
PRIMARY_VARIANT_RE = re.compile(
    r'''variant\s*=\s*["']primary["']''',
    re.I,
)


def _rel(path: Path) -> str:
    return str(path.relative_to(TEMPLATES)).replace('\\', '/')


def _strip_jinja_comments(text: str) -> str:
    return re.sub(r'\{#.*?#\}', '', text, flags=re.S)


def _strip_html_comments(text: str) -> str:
    return re.sub(r'<!--.*?-->', '', text, flags=re.S)


def _strip_style_and_script_blocks(text: str) -> str:
    text = re.sub(r'<style\b[^>]*>.*?</style>', '', text, flags=re.I | re.S)
    text = re.sub(r'<script\b[^>]*>.*?</script>', '', text, flags=re.I | re.S)
    return text


def _iter_templates():
    for path in sorted(TEMPLATES.rglob('*.html')):
        yield path


def _count_style_attrs(text: str) -> int:
    return len(STYLE_ATTR_RE.findall(text))


def _count_primaries(text: str) -> int:
    html = _strip_style_and_script_blocks(
        _strip_html_comments(_strip_jinja_comments(text))
    )
    without_jinja = re.sub(r'\{%.*?%\}', '\n', html, flags=re.S)
    without_jinja = re.sub(r'\{\{.*?\}\}', '', without_jinja, flags=re.S)
    return len(PRIMARY_CLASS_RE.findall(without_jinja)) + len(
        PRIMARY_VARIANT_RE.findall(without_jinja)
    )


def _hex_outside_token_section_base(text: str) -> list[str]:
    start = text.find('/* === Design tokens')
    if start < 0:
        start = text.find('/* Design tokens')
    if start < 0:
        start = text.find('Design tokens')
    end = text.find('* { margin: 0;')
    if start < 0 or end < 0:
        return ['base.html: could not locate token section']
    remainder = text[:start] + text[end:]
    hits = []
    for line in remainder.splitlines():
        if HEX_RE.search(line) and 'theme_color' not in line:
            hits.append(line.strip()[:120])
    allowed = text[start:end]
    if '#0A7A55' not in allowed:
        hits.append('token section missing accent #0A7A55')
    return hits


def test_components_css_exists_and_uses_tokens_only():
    assert COMPONENTS_CSS.is_file()
    text = COMPONENTS_CSS.read_text(encoding='utf-8')
    assert HEX_RE.search(text) is None, 'components.css must use var(--token), not hex'
    assert BOX_SHADOW_RE.search(text) is None
    assert GRADIENT_RE.search(text) is None
    assert PILL_RADIUS_RE.search(text) is None
    for m in MONO_FONT_RE.finditer(text):
        start = text.rfind('{', 0, m.start())
        rule_start = text.rfind('}', 0, start)
        header = text[rule_start + 1:start]
        assert re.search(r'(code|pre|snippet|secret)', header, re.I), (
            f'monospace outside code/snippet/secret: {header.strip()[:80]}'
        )
    assert SERIF_UI_RE.search(text) is None


def test_component_macros_exist():
    macros = TEMPLATES / 'components' / 'macros.html'
    assert macros.is_file()
    text = macros.read_text(encoding='utf-8')
    for name in (
        'button', 'link_button', 'section', 'page_header', 'tabs', 'tab_panel',
        'checklist_row', 'checklist', 'snippet', 'secret_field', 'notice',
        'empty_state', 'form_field', 'chip',
    ):
        assert f'macro {name}' in text, f'missing macro {name}'
    assert _count_style_attrs(text) == 0
    assert HEX_RE.search(text) is None


def test_no_forbidden_css_in_templates():
    failures = []
    for path in _iter_templates():
        text = _strip_jinja_comments(path.read_text(encoding='utf-8'))
        for i, line in enumerate(text.splitlines(), 1):
            if BOX_SHADOW_RE.search(line):
                value = line.split(':', 1)[-1].strip().lower()
                if value.startswith('none'):
                    continue
                failures.append(f'{_rel(path)}:{i}: shadow forbidden')
            if GRADIENT_RE.search(line):
                failures.append(f'{_rel(path)}:{i}: gradient forbidden')
            if PILL_RADIUS_RE.search(line):
                failures.append(
                    f'{_rel(path)}:{i}: pill radius forbidden ({line.strip()[:60]})'
                )
    assert not failures, 'DESIGN.md violations:\n' + '\n'.join(failures)


def test_hardcoded_hex_only_in_token_section():
    failures = []
    for path in _iter_templates():
        rel = _rel(path)
        text = path.read_text(encoding='utf-8')
        if path == BASE_HTML:
            failures.extend(_hex_outside_token_section_base(text))
            continue
        if rel in HEX_TOKEN_FILE_ALLOWLIST:
            # Must still only assign known design tokens, not random hex.
            continue
        body = _strip_jinja_comments(text)
        # Ignore Chart.js colour literals in admin scripts.
        body_no_script = re.sub(
            r'<script\b[^>]*>.*?</script>', '', body, flags=re.I | re.S
        )
        for i, line in enumerate(body_no_script.splitlines(), 1):
            if HEX_RE.search(line):
                failures.append(f'{rel}:{i}: {line.strip()[:100]}')
    assert not failures, 'hard-coded hex outside tokens:\n' + '\n'.join(failures)


def test_inline_style_ratchet():
    failures = []
    for path in _iter_templates():
        rel = _rel(path)
        text = path.read_text(encoding='utf-8')
        count = _count_style_attrs(text)
        if rel.startswith('components/') or rel == 'design_components.html':
            if count:
                failures.append(
                    f'{rel}: component/gallery templates must not use style= ({count})'
                )
            continue
        if count == 0:
            continue
        allowed = LEGACY_STYLE_ALLOWLIST.get(rel)
        if allowed is None:
            failures.append(
                f'{rel}: has {count} style= attribute(s); new templates must use '
                f'components macros / CSS classes (not on the legacy allowlist)'
            )
        elif count > allowed:
            failures.append(
                f'{rel}: style= count {count} exceeds allowlist cap {allowed}'
            )
    for rel in LEGACY_STYLE_ALLOWLIST:
        if not (TEMPLATES / rel).is_file():
            failures.append(f'allowlist entry missing on disk: {rel}')
    assert not failures, 'inline style guardrail:\n' + '\n'.join(failures)


def test_at_most_one_primary_button_per_template():
    failures = []
    for path in _iter_templates():
        rel = _rel(path)
        if rel in PRIMARY_BUTTON_EXEMPT:
            continue
        text = path.read_text(encoding='utf-8')
        if 'data-ds-gallery' in text:
            continue
        total = _count_primaries(text)
        if total <= 1:
            continue
        allowed = LEGACY_PRIMARY_ALLOWLIST.get(rel)
        if allowed is None:
            failures.append(
                f'{rel}: {total} primary buttons; new screens must have at most one '
                f'accent-filled button (DESIGN.md)'
            )
        elif total > allowed:
            failures.append(
                f'{rel}: {total} primary buttons exceeds allowlist cap {allowed}'
            )
    for rel in LEGACY_PRIMARY_ALLOWLIST:
        if not (TEMPLATES / rel).is_file():
            failures.append(f'primary allowlist entry missing: {rel}')
    assert not failures, (
        'One accent-filled button per screen (DESIGN.md):\n' + '\n'.join(failures)
    )


def test_monospace_only_in_code_contexts():
    failures = []
    for path in _iter_templates():
        text = path.read_text(encoding='utf-8')
        for style in re.findall(r'<style\b[^>]*>(.*?)</style>', text, flags=re.I | re.S):
            for m in MONO_FONT_RE.finditer(style):
                start = style.rfind('{', 0, m.start())
                rule_start = style.rfind('}', 0, start)
                header = style[rule_start + 1:start]
                if not re.search(r'(^|[\s,])(code|pre|kbd|samp)\b', header, re.I):
                    if not re.search(r'(snippet|secret|api-docs|settings code)', header, re.I):
                        failures.append(
                            f'{_rel(path)}: monospace on `{header.strip()[:60]}`'
                        )
        for m in SERIF_UI_RE.finditer(text):
            line = text[max(0, m.start() - 80):m.start() + 80]
            if '@font-face' in line or '--font-serif' in line:
                continue
            if "font-family: 'Newsreader'" in line or 'font-family: "Newsreader"' in line:
                # @font-face src block
                if '@font-face' in text[max(0, m.start() - 200):m.start()]:
                    continue
            start = text.rfind('{', 0, m.start())
            if start < 0:
                continue
            rule_start = text.rfind('}', 0, start)
            header = text[rule_start + 1:start]
            if not re.search(r'transcript', header, re.I):
                failures.append(
                    f'{_rel(path)}: Newsreader/serif outside transcript '
                    f'(`{header.strip()[:60]}`)'
                )
    assert not failures, 'typography guardrail:\n' + '\n'.join(failures)


@pytest.fixture
def client():
    A.app.config['TESTING'] = True
    A.app.config['DEBUG'] = False
    return A.app.test_client()


def test_gallery_404_when_not_debug_and_not_admin(client):
    A.app.config['DEBUG'] = False
    r = client.get('/design/components')
    assert r.status_code == 404


def test_gallery_ok_when_debug(client):
    A.app.config['DEBUG'] = True
    try:
        r = client.get('/design/components')
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        assert 'Component gallery' in body
        assert 'noindex' in body.lower()
        assert r.headers.get('X-Robots-Tag', '').startswith('noindex')
        assert 'ds-checklist' in body
        assert 'ds-notice' in body
        assert 'btn-primary' in body
    finally:
        A.app.config['DEBUG'] = False


def test_gallery_ok_for_admin_user(client, monkeypatch):
    A.app.config['DEBUG'] = False
    with A.app.app_context():
        user = User(email='admin-ds@example.com', password_hash='x')
        db.session.add(user)
        db.session.commit()
        uid = user.id
    monkeypatch.setenv('ADMIN_EMAILS', 'admin-ds@example.com')
    with client.session_transaction() as sess:
        sess['_user_id'] = str(uid)
        sess['_fresh'] = True
    r = client.get('/design/components')
    assert r.status_code == 200
    assert b'Component gallery' in r.data


def test_base_links_components_css(client):
    r = client.get('/')
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert 'components.css' in html
