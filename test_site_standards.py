"""Website Specification items: headers, well-known files, icons, errors, a11y.

From the 2026-10-09 audit against specification.website. Isolation matches
test_app.py (throwaway DB before importing app).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import xml.etree.ElementTree as ET

import pytest

if not os.environ.get('DATABASE_URL'):
    _TEST_DB = os.path.join(tempfile.mkdtemp(prefix='podskrift-std-'), 'test.db')
    os.environ['DATABASE_URL'] = f'sqlite:///{_TEST_DB}'
os.environ.setdefault('SENTRY_DSN', '')
os.environ.setdefault('POSTHOG_KEY', '')
os.environ.setdefault('POSTHOG_HOST', '')
os.environ.setdefault('PODSKRIFT_DISABLE_WATCHDOG', '1')

import app as A  # noqa: E402
import site_standards as S  # noqa: E402


@pytest.fixture
def client():
    A.app.config['TESTING'] = True
    return A.app.test_client()


def test_html_carries_security_headers(client):
    r = client.get('/')
    h = r.headers
    assert h['X-Content-Type-Options'] == 'nosniff'
    assert h['Referrer-Policy'] == 'strict-origin-when-cross-origin'
    assert h['X-Frame-Options'] == 'SAMEORIGIN'
    assert 'camera=()' in h['Permissions-Policy']
    assert h['Cross-Origin-Opener-Policy'] == 'same-origin'
    csp = h['Content-Security-Policy']
    assert "frame-ancestors 'self'" in csp and "object-src 'none'" in csp
    assert 'upgrade-insecure-requests' in csp
    ro = h['Content-Security-Policy-Report-Only']
    assert "default-src 'self'" in ro and "font-src 'self'" in ro
    # No reporting endpoint without a DSN.
    assert 'report-uri' not in ro and 'Reporting-Endpoints' not in h


def test_hsts_only_on_an_https_public_origin(client, monkeypatch):
    assert 'Strict-Transport-Security' not in client.get('/').headers
    monkeypatch.setitem(S._CFG, 'public_base_url', 'https://podskrift.com')
    hsts = client.get('/pricing').headers['Strict-Transport-Security']
    assert hsts == 'max-age=31536000'
    # email.podskrift.com (Mailgun click tracking) is still plain HTTP.
    assert 'includeSubDomains' not in hsts


def test_sentry_security_endpoint_from_dsn(monkeypatch):
    monkeypatch.setenv('SENTRY_ENVIRONMENT', 'production')
    url = S.sentry_security_endpoint('https://abc123@o1.ingest.de.sentry.io/4507')
    assert url == ('https://o1.ingest.de.sentry.io/api/4507/security/'
                   '?sentry_key=abc123&sentry_environment=production')
    assert S.sentry_security_endpoint('not a dsn') == ''
    assert S.sentry_security_endpoint('') == ''


def test_report_only_policy_reports_to_sentry_when_configured(client, monkeypatch):
    monkeypatch.setenv('PODSKRIFT_ENV', 'production')
    monkeypatch.setenv('SENTRY_DSN', 'https://k@o1.ingest.de.sentry.io/42')
    h = client.get('/').headers
    assert 'report-uri https://o1.ingest.de.sentry.io/api/42/security/' in h['Content-Security-Policy-Report-Only']
    assert h['Reporting-Endpoints'].startswith('csp="https://o1.ingest.de.sentry.io/api/42/security/')


def test_link_header_advertises_machine_readable_surfaces(client):
    link = client.head('/').headers['Link']
    for frag in ('</llms.txt>; rel="describedby"', '</sitemap.xml>; rel="sitemap"',
                 '</.well-known/api-catalog>; rel="api-catalog"',
                 '</whats-new/feed.xml>; rel="alternate"'):
        assert frag in link


def test_html_is_revalidated_and_conditional(client):
    r = client.get('/pricing')
    assert r.headers['Cache-Control'] == 'private, no-cache'
    etag = r.headers['ETag']
    again = client.get('/pricing', headers={'If-None-Match': etag})
    assert again.status_code == 304
    assert 'utm_source' in r.headers['No-Vary-Search']


def test_versioned_static_is_immutable(client):
    with A.app.test_request_context():
        url = S.asset_url('favicon.svg')
    assert '?v=' in url
    r = client.get(url)
    assert r.status_code == 200
    assert r.headers['Cache-Control'] == 'public, max-age=31536000, immutable'


def test_redirects_name_the_issuer(client):
    r = client.get('/signup')
    assert r.status_code == 301
    assert r.headers['X-Redirect-By'] == 'Podskrift'


def test_cross_site_post_is_refused_but_webhooks_are_not(client):
    r = client.post('/settings', headers={'Sec-Fetch-Site': 'cross-site'})
    assert r.status_code == 403
    # Same-origin passes the guard (then hits login_required / form handling).
    r = client.post('/settings', headers={'Sec-Fetch-Site': 'same-origin'})
    assert r.status_code != 403
    r = client.post('/stripe/webhook', headers={'Sec-Fetch-Site': 'cross-site'})
    assert r.status_code != 403
    r = client.post('/api/v1/transcriptions', headers={'Sec-Fetch-Site': 'cross-site'})
    assert r.status_code != 403


def test_404_is_a_real_page_with_status_and_noindex(client):
    r = client.get('/definitely-not-a-page')
    assert r.status_code == 404
    body = r.get_data(as_text=True)
    assert 'This page doesn' in body
    assert '<meta name="robots" content="noindex">' in body
    assert 'rel="canonical"' not in body
    assert body.count('name="robots"') == 1


def test_api_404_stays_json(client):
    r = client.get('/api/v1/nope')
    assert r.status_code == 404
    assert r.get_json()['error'] == 'not_found'


def test_500_page_is_standalone():
    with A.app.test_request_context('/boom'):
        body, status = S.server_error(Exception('x'))
    assert status == 500
    assert 'Something went wrong' in body
    assert 'noindex' in body


def test_favicons_and_manifest(client):
    ico = client.get('/favicon.ico')
    assert ico.status_code == 200 and ico.data[:4] == b'\x00\x00\x01\x00'
    touch = client.get('/apple-touch-icon.png')
    assert touch.status_code == 200 and touch.data[:8] == b'\x89PNG\r\n\x1a\n'
    m = client.get('/manifest.webmanifest')
    assert m.mimetype == 'application/manifest+json'
    data = m.get_json(force=True)
    purposes = {i.get('purpose') for i in data['icons']}
    assert 'maskable' in purposes and data['theme_color'] == S.THEME_COLOR
    for icon in data['icons']:
        assert client.get(icon['src']).status_code == 200


def test_head_has_icons_theme_and_og_image(client):
    body = client.get('/').get_data(as_text=True)
    assert '<meta name="color-scheme" content="light dark">' in body
    assert '<meta name="theme-color"' in body
    assert 'rel="apple-touch-icon"' in body and 'rel="manifest"' in body
    assert re.search(r'property="og:image" content="[^"]+og-image\.png\?v=', body)
    assert 'rel="describedby" type="text/markdown" href="/llms.txt"' in body
    assert 'application/rss+xml' in body
    assert 'fonts.googleapis.com' not in body and 'fonts.gstatic.com' not in body
    assert 'type="speculationrules"' in body


def test_security_txt_is_valid(client):
    r = client.get('/.well-known/security.txt')
    assert r.mimetype == 'text/plain'
    body = r.get_data(as_text=True)
    assert re.search(r'^Contact: https://', body, re.M)
    m = re.search(r'^Expires: (\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)$', body, re.M)
    assert m


def test_change_password_points_at_forgot_password(client):
    r = client.get('/.well-known/change-password')
    assert r.status_code == 302 and r.headers['Location'].endswith('/forgot-password')


def test_api_catalog_is_a_linkset(client):
    r = client.get('/.well-known/api-catalog')
    assert r.mimetype == 'application/linkset+json'
    entry = json.loads(r.data)['linkset'][0]
    assert entry['anchor'].endswith('/api/v1/')
    assert any(l['href'].endswith('/docs/api') for l in entry['service-doc'])


def test_agent_skill_digest_matches(client):
    idx = client.get('/.well-known/agent-skills/index.json')
    assert idx.headers['Access-Control-Allow-Origin'] == '*'
    skill = json.loads(idx.data)['skills'][0]
    md = client.get(skill['url'])
    assert md.mimetype == 'text/markdown'
    assert skill['digest'] == 'sha256:' + hashlib.sha256(md.data).hexdigest()
    assert md.get_data(as_text=True).startswith('---\nname: ' + skill['name'])


def test_api_docs_markdown_and_negotiation(client):
    md = client.get('/docs/api.md')
    assert md.mimetype == 'text/markdown'
    text = md.get_data(as_text=True)
    assert text.startswith('---\n') and '# Podcast transcript API' in text
    assert 'AGENT_API_KEY' not in text
    neg = client.get('/docs/api', headers={'Accept': 'text/markdown'})
    assert neg.mimetype == 'text/markdown' and neg.headers['Content-Location'] == '/docs/api.md'
    html = client.get('/docs/api')
    assert html.mimetype == 'text/html' and 'Accept' in html.headers.get('Vary', '')
    assert 'rel="alternate" type="text/markdown" href="/docs/api.md"' in html.get_data(as_text=True)
    # API docs tables have header scope.
    assert '<th scope="col">' in html.get_data(as_text=True)


def test_home_negotiates_to_llms_txt(client):
    r = client.get('/', headers={'Accept': 'text/markdown'})
    assert r.mimetype == 'text/markdown'
    assert r.get_data(as_text=True).startswith('# Podskrift')
    assert client.get('/').mimetype == 'text/html'


def test_whats_new_feed_is_well_formed(client):
    r = client.get('/whats-new/feed.xml')
    assert r.mimetype == 'application/rss+xml'
    root = ET.fromstring(r.data)
    ch = root.find('channel')
    ns = {'atom': 'http://www.w3.org/2005/Atom'}
    assert ch.find('atom:link', ns).get('rel') == 'self'
    items = ch.findall('item')
    assert items, 'changelog.json has entries'
    guids = [i.findtext('guid') for i in items]
    assert len(guids) == len(set(guids))


def test_robots_declares_content_signals_inside_every_group(client):
    body = client.get('/robots.txt').get_data(as_text=True)
    groups = re.split(r'\n(?=User-agent:)', body)
    agent_groups = [g for g in groups if g.startswith('User-agent:')]
    assert agent_groups
    for g in agent_groups:
        assert 'Content-Signal: search=yes, ai-input=yes, ai-train=yes' in g
        # No blank line between User-agent and its rules.
        head = g.split('\n\n')[0]
        assert 'Disallow: /settings' in head


def test_skip_link_and_focusable_main(client):
    body = client.get('/').get_data(as_text=True)
    first_link = re.search(r'<a [^>]*>', body.split('<body', 1)[1]).group(0)
    assert 'skip-link' in first_link and 'href="#content"' in first_link
    assert '<main id="content" tabindex="-1">' in body
    assert ':focus-visible' in body


def test_home_headings_do_not_skip_levels(client):
    body = client.get('/').get_data(as_text=True)
    levels = [int(x) for x in re.findall(r'<h([1-6])[\s>]', body)]
    assert levels[0] == 1
    for a, b in zip(levels, levels[1:]):
        assert b <= a + 1, levels


@pytest.mark.parametrize('path', ['/login', '/register', '/rss-help', '/pricing'])
def test_public_pages_have_one_h1(client, path):
    body = client.get(path).get_data(as_text=True)
    assert len(re.findall(r'<h1[\s>]', body)) == 1


def test_home_inputs_are_labelled(client):
    body = client.get('/').get_data(as_text=True)
    assert re.search(r'id="podcastSearch"[^>]*aria-label=', body, re.S)
    # RSS links go in the main box; there is no separate RSS input any more.
    assert 'id="rss_url"' not in body


def test_norwegian_faq_entry_is_marked(client):
    assert S.text_lang('Hvordan transkriberer jeg en norsk podcast til tekst?') == 'nb'
    assert S.text_lang('How do I transcribe a podcast episode to text?') == ''


def test_llms_txt_points_agents_at_machine_readable_surfaces(client):
    body = client.get('/llms.txt').get_data(as_text=True)
    assert '## For agents' in body
    for frag in ('/docs/api.md', '/.well-known/agent-skills/index.json',
                 '/.well-known/api-catalog', '/whats-new/feed.xml'):
        assert frag in body


def test_episode_selection_is_noindex():
    """POST-only result page (canonical would be /parse_rss): keep it out."""
    src = open(os.path.join(os.path.dirname(__file__), 'templates',
                            'episode_selection.html'), encoding='utf-8').read()
    assert '{% block robots_meta %}<meta name="robots" content="noindex">{% endblock %}' in src


def test_wildcard_or_missing_accept_gets_html_not_markdown(client):
    for headers in ({}, {'Accept': '*/*'}, {'Accept': 'text/*'},
                    {'Accept': 'text/html,application/xhtml+xml,*/*;q=0.8'}):
        resp = client.get('/', headers=headers)
        assert resp.mimetype == 'text/html', headers
        assert 'Accept' in resp.headers.get('Vary', '')


def test_explicit_markdown_accept_gets_markdown_with_vary(client):
    resp = client.get('/', headers={'Accept': 'text/markdown, */*;q=0.5'})
    assert resp.mimetype == 'text/markdown'
    assert 'Accept' in resp.headers.get('Vary', '')
    resp = client.get('/docs/api', headers={'Accept': 'text/markdown'})
    assert resp.mimetype == 'text/markdown'
