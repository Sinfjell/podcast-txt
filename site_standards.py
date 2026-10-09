"""Website-standards plumbing: headers, well-known files, icons, feeds, errors.

Everything here comes out of the 2026-10-09 audit of podskrift.com against
Joost de Valk's Website Specification (specification.website). It lives in its
own module so app.py keeps the product logic and this keeps the web plumbing.

Nothing here touches the database.
"""
import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from urllib.parse import urlparse
from xml.sax.saxutils import escape as xml_escape

from flask import (Blueprint, Response, current_app, jsonify, redirect,
                   render_template, request, send_from_directory, url_for)

bp = Blueprint('site_standards', __name__)

#: Filled by init_site_standards(); avoids importing app.py (circular).
_CFG = {
    'public_base_url': '',
    'changelog_loader': lambda: [],
    'api_markdown_loader': lambda: '',
    'posthog_host': lambda: '',
}

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')

THEME_COLOR = '#FFFFFF'
#: Dark-scheme variant of the browser chrome colour (the design system's dark paper).
THEME_COLOR_DARK = '#0A0F0D'
SITE_NAME = 'Podskrift'
SITE_DESCRIPTION = ('Transcribe any podcast or Spotify episode to text and .srt '
                    'subtitles with OpenAI Whisper, in 28 languages.')
CONTACT_URL = 'https://productivitytech.io/contact/'
SKILL_NAME = 'podskrift-transcript-api'

#: Query parameters that never change what a page renders. Advertised with
#: No-Vary-Search so browsers reuse one cached/prefetched copy for all of them.
_NO_VARY_PARAMS = ('utm_source', 'utm_medium', 'utm_campaign', 'utm_term',
                   'utm_content', 'gclid', 'fbclid', 'msclkid', 'ref')

#: POSTs that legitimately arrive cross-site: server-to-server callbacks and
#: mail-client one-click unsubscribe. Browsers do not send Sec-Fetch-* on
#: server-to-server requests anyway; this is belt and braces.
_CROSS_SITE_POST_EXEMPT = ('/stripe/webhook', '/api/', '/email/unsubscribe')


def _public_base():
    return _CFG['public_base_url'] or request.url_root.rstrip('/')


def _abs(path):
    return _public_base() + path


# ---------------------------------------------------------------------------
# Cache-busted static URLs
# ---------------------------------------------------------------------------
_asset_hashes = {}


def asset_url(filename):
    """url_for('static') plus ?v=<content hash>, so it can be cached for a year."""
    digest = _asset_hashes.get(filename)
    if digest is None:
        try:
            with open(os.path.join(STATIC_DIR, filename), 'rb') as f:
                digest = hashlib.sha256(f.read()).hexdigest()[:10]
        except OSError:
            digest = ''
        _asset_hashes[filename] = digest
    if digest:
        return url_for('static', filename=filename, v=digest)
    return url_for('static', filename=filename)


# ---------------------------------------------------------------------------
# Headers
# ---------------------------------------------------------------------------
def _posthog_origins():
    host = (_CFG['posthog_host']() or '').rstrip('/')
    if not host:
        return []
    origins = [host]
    # The PostHog snippet loads array.js from the -assets twin of the API host.
    assets = host.replace('.i.posthog.com', '-assets.i.posthog.com')
    if assets != host:
        origins.append(assets)
    return origins


def sentry_security_endpoint(dsn=None):
    """Sentry's CSP/Reporting endpoint for the configured DSN, or ''."""
    dsn = (dsn if dsn is not None else os.getenv('SENTRY_DSN', '')).strip()
    if not dsn:
        return ''
    parsed = urlparse(dsn)
    project = parsed.path.strip('/').split('/')[-1] if parsed.path else ''
    if not (parsed.scheme and parsed.hostname and parsed.username and project):
        return ''
    env = os.getenv('SENTRY_ENVIRONMENT', 'production')
    host = parsed.hostname + (f':{parsed.port}' if parsed.port else '')
    return (f'{parsed.scheme}://{host}/api/{project}/security/'
            f'?sentry_key={parsed.username}&sentry_environment={env}')


#: Enforced now: directives that cannot break a working page.
CSP_ENFORCED = ("frame-ancestors 'self' https://eu.posthog.com https://us.posthog.com; "
                "base-uri 'self'; object-src 'none'; upgrade-insecure-requests")


def csp_report_only():
    """The full policy, reported (not enforced) until the reports are clean."""
    ph = ' '.join(_posthog_origins())
    policy = [
        "default-src 'self'",
        f"script-src 'self' 'unsafe-inline' {ph}".strip(),
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data: blob: https:",
        "font-src 'self'",
        f"connect-src 'self' {ph}".strip(),
        "media-src 'self' blob: https:",
        "worker-src 'self' blob:",
        "frame-src 'none'",
        "frame-ancestors 'self' https://eu.posthog.com https://us.posthog.com",
        "base-uri 'self'",
        "object-src 'none'",
        "form-action 'self' https://checkout.stripe.com",
    ]
    endpoint = sentry_security_endpoint()
    if endpoint:
        policy.append(f'report-uri {endpoint}')
        policy.append('report-to csp')
    return '; '.join(policy)


def link_header():
    links = [
        '</llms.txt>; rel="describedby"; type="text/markdown"; title="Site summary for LLMs"',
        '</sitemap.xml>; rel="sitemap"; type="application/xml"',
        '</.well-known/api-catalog>; rel="api-catalog"; type="application/linkset+json"',
        '</docs/api>; rel="service-doc"; type="text/html"',
        '</whats-new/feed.xml>; rel="alternate"; type="application/rss+xml"; title="Podskrift: what\'s new"',
        '</.well-known/agent-skills/index.json>; rel="agent-skills"; type="application/json"',
    ]
    return ', '.join(links)


@bp.before_app_request
def reject_cross_site_writes():
    """Fetch Metadata: refuse state-changing requests a third-party page fires.

    CSRF tokens still guard the billing forms; this adds a cheap outer wall for
    every other POST (feeds, settings, transcription start/cancel).
    """
    if request.method in ('GET', 'HEAD', 'OPTIONS'):
        return None
    if request.headers.get('Sec-Fetch-Site', '') != 'cross-site':
        return None
    if (request.path or '').startswith(_CROSS_SITE_POST_EXEMPT):
        return None
    return Response('Cross-site request refused.', status=403,
                    mimetype='text/plain')


def _prefers_markdown():
    accept = request.accept_mimetypes
    md = accept['text/markdown']
    return md > 0 and md >= accept['text/html']


@bp.before_app_request
def markdown_negotiation():
    """Accept: text/markdown on / and /docs/api returns Markdown, not HTML."""
    if request.method not in ('GET', 'HEAD'):
        return None
    if request.path not in ('/', '/docs/api') or not _prefers_markdown():
        return None
    if request.path == '/':
        view = current_app.view_functions.get('llms_txt')
        if view is None:
            return None
        resp = view()
        resp = current_app.make_response(resp)
        resp.mimetype = 'text/markdown'
        resp.headers['Content-Location'] = '/llms.txt'
    else:
        resp = api_docs_markdown()
        resp.headers['Content-Location'] = '/docs/api.md'
    return resp


@bp.after_app_request
def standard_headers(resp):
    h = resp.headers
    # --- Security --------------------------------------------------------
    if _CFG['public_base_url'].startswith('https://'):
        # No includeSubDomains yet: email.podskrift.com is Mailgun's click-
        # tracking CNAME and is plain HTTP until HTTPS tracking is switched on.
        h.setdefault('Strict-Transport-Security', 'max-age=31536000')
    h.setdefault('X-Content-Type-Options', 'nosniff')
    h.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    h.setdefault('X-Frame-Options', 'SAMEORIGIN')
    h.setdefault('Permissions-Policy',
                 'camera=(), microphone=(), geolocation=(), usb=(), '
                 'payment=(), browsing-topics=()')
    h.setdefault('Cross-Origin-Opener-Policy', 'same-origin')
    if resp.mimetype == 'text/html':
        h.setdefault('Content-Security-Policy', CSP_ENFORCED)
        h.setdefault('Content-Security-Policy-Report-Only', csp_report_only())
        endpoint = sentry_security_endpoint()
        if endpoint:
            h.setdefault('Reporting-Endpoints', f'csp="{endpoint}"')
    # --- Discovery -------------------------------------------------------
    h.setdefault('Link', link_header())
    # --- Redirects -------------------------------------------------------
    if 300 <= resp.status_code < 400:
        h.setdefault('X-Redirect-By', 'Podskrift')
    # --- Caching ---------------------------------------------------------
    if request.endpoint == 'static':
        if request.args.get('v'):
            h['Cache-Control'] = 'public, max-age=31536000, immutable'
        elif (request.view_args or {}).get('filename', '').startswith('fonts/'):
            h['Cache-Control'] = 'public, max-age=2592000'
    elif resp.mimetype == 'text/html':
        h.setdefault('Cache-Control', 'private, no-cache')
        if request.method in ('GET', 'HEAD'):
            h.setdefault('No-Vary-Search',
                         'params=(' + ' '.join(f'"{p}"' for p in _NO_VARY_PARAMS) + ')')
        if request.path in ('/', '/docs/api'):
            resp.vary.add('Accept')
        if (request.method in ('GET', 'HEAD') and resp.status_code == 200
                and not resp.direct_passthrough and not resp.is_streamed
                and 'ETag' not in h):
            resp.add_etag()
            resp.make_conditional(request)
    return resp


# ---------------------------------------------------------------------------
# Icons, manifest
# ---------------------------------------------------------------------------
def _static_file(name, max_age=86400, mimetype=None):
    resp = send_from_directory(STATIC_DIR, name, max_age=max_age, mimetype=mimetype)
    return resp


@bp.route('/favicon.ico')
def favicon_ico():
    return _static_file('favicon.ico', mimetype='image/x-icon')


@bp.route('/apple-touch-icon.png')
@bp.route('/apple-touch-icon-precomposed.png')
def apple_touch_icon():
    return _static_file('apple-touch-icon.png', mimetype='image/png')


@bp.route('/manifest.webmanifest')
def web_manifest():
    data = {
        'name': 'Podskrift: podcast to text',
        'short_name': SITE_NAME,
        'description': SITE_DESCRIPTION,
        'start_url': '/',
        'scope': '/',
        'display': 'standalone',
        'background_color': THEME_COLOR,
        'theme_color': THEME_COLOR,
        'lang': 'en',
        'icons': [
            {'src': asset_url('icon-192.png'), 'sizes': '192x192', 'type': 'image/png', 'purpose': 'any'},
            {'src': asset_url('icon-512.png'), 'sizes': '512x512', 'type': 'image/png', 'purpose': 'any'},
            {'src': asset_url('icon-maskable-512.png'), 'sizes': '512x512', 'type': 'image/png', 'purpose': 'maskable'},
            {'src': asset_url('favicon.svg'), 'sizes': 'any', 'type': 'image/svg+xml'},
        ],
    }
    resp = Response(json.dumps(data, indent=2), mimetype='application/manifest+json')
    resp.headers['Cache-Control'] = 'public, max-age=86400'
    return resp


# ---------------------------------------------------------------------------
# /.well-known
# ---------------------------------------------------------------------------
@bp.route('/.well-known/security.txt')
def security_txt():
    # Rolling expiry: always ~6 months out, so the file can never lapse.
    expires = (datetime.now(timezone.utc) + timedelta(days=180)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    body = (
        f'Contact: {CONTACT_URL}\n'
        f'Expires: {expires.strftime("%Y-%m-%dT%H:%M:%SZ")}\n'
        'Preferred-Languages: en, no\n'
        f'Canonical: {_abs("/.well-known/security.txt")}\n'
        f'Policy: {_abs("/privacy")}\n'
    )
    resp = Response(body, mimetype='text/plain')
    resp.headers['Cache-Control'] = 'public, max-age=86400'
    return resp


@bp.route('/.well-known/change-password')
def change_password():
    """Password managers land here. Password changes live under Settings."""
    return redirect(url_for('settings'), code=302)


@bp.route('/.well-known/api-catalog')
def api_catalog():
    base = _public_base()
    data = {'linkset': [{
        'anchor': f'{base}/api/v1/',
        'service-doc': [
            {'href': f'{base}/docs/api', 'type': 'text/html'},
            {'href': f'{base}/docs/api.md', 'type': 'text/markdown'},
        ],
        'describedby': [{'href': f'{base}/llms.txt', 'type': 'text/markdown'}],
        'status': [{'href': f'{base}/health', 'type': 'application/json'}],
        'terms-of-service': [{'href': f'{base}/terms', 'type': 'text/html'}],
        'privacy-policy': [{'href': f'{base}/privacy', 'type': 'text/html'}],
    }]}
    resp = Response(json.dumps(data, indent=2), mimetype='application/linkset+json')
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Cache-Control'] = 'public, max-age=3600'
    return resp


def skill_markdown():
    base = _public_base()
    return f"""---
name: {SKILL_NAME}
description: Get the full text transcript of a podcast episode (including Spotify episode links) from Podskrift's HTTP API. Use when a user wants a transcript, quotes or a summary of a podcast episode and you can make HTTP requests with their Podskrift API key.
---

# Podskrift transcript API

Podskrift transcribes podcast episodes with OpenAI Whisper (28 languages) and
returns plain text plus .srt subtitles.

## When to use

- The user names a podcast episode, pastes a Spotify / Apple Podcasts / RSS
  link, or asks for a transcript, quote or summary of an episode.
- You can send HTTP requests and the user has given you a Podskrift API key
  (`psk_...`). Without a key, send the user to {base}/ to do it in the browser.

## Steps

1. Authenticate every request with `Authorization: Bearer psk_...` (or
   `X-Api-Key: psk_...`). The user creates the key at {base}/settings.
2. `POST {base}/api/v1/resolve` with the episode URL, or show + date, to get
   the episode.
3. `POST {base}/api/v1/transcriptions` to start the job. It runs server-side.
4. Poll `GET {base}/api/v1/episodes/{{id}}` until the status is `completed`
   (a one-hour episode takes a few minutes). Back off between polls.
5. `GET {base}/api/v1/episodes/{{id}}/transcript` returns the plain text.

Full reference, request bodies and error codes: {base}/docs/api.md

## Rules

- Never print or log the API key.
- Transcription spends the user's minutes. Confirm before starting long
  episodes, and reuse an existing completed transcript
  (`GET {base}/api/v1/episodes`) instead of starting a duplicate.
"""


@bp.route(f'/.well-known/agent-skills/{SKILL_NAME}/SKILL.md')
def agent_skill():
    resp = Response(skill_markdown(), mimetype='text/markdown')
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Cache-Control'] = 'public, max-age=3600'
    return resp


@bp.route('/.well-known/agent-skills/index.json')
def agent_skills_index():
    body = skill_markdown().encode('utf-8')
    data = {
        '$schema': 'https://schemas.agentskills.io/discovery/0.2.0/schema.json',
        'skills': [{
            'name': SKILL_NAME,
            'type': 'skill-md',
            'description': ('Get the transcript of any podcast or Spotify episode '
                            "through Podskrift's HTTP API."),
            'url': f'/.well-known/agent-skills/{SKILL_NAME}/SKILL.md',
            'digest': 'sha256:' + hashlib.sha256(body).hexdigest(),
        }],
    }
    resp = Response(json.dumps(data, indent=2), mimetype='application/json')
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Cache-Control'] = 'public, max-age=3600'
    return resp


# ---------------------------------------------------------------------------
# Markdown + feed
# ---------------------------------------------------------------------------
@bp.route('/docs/api.md')
def api_docs_markdown():
    source = _CFG['api_markdown_loader']()
    front = (
        '---\n'
        'title: "Podcast transcript API"\n'
        f'url: {_abs("/docs/api")}\n'
        'licence: "Documentation for the Podskrift HTTP API"\n'
        '---\n\n'
    )
    body = front + source
    resp = Response(body, mimetype='text/markdown')
    resp.headers['Cache-Control'] = 'public, max-age=3600'
    # Rough estimate (~4 characters per token); good enough for a size hint.
    resp.headers['X-Markdown-Tokens'] = str(max(1, len(body) // 4))
    resp.headers['Link'] = f'<{_abs("/docs/api")}>; rel="canonical"'
    return resp


@bp.route('/whats-new/feed.xml')
def whats_new_feed():
    entries = _CFG['changelog_loader']()[:30]
    base = _public_base()
    page = f'{base}/whats-new'
    self_url = f'{base}/whats-new/feed.xml'

    def rfc822(day):
        try:
            dt = datetime.strptime(day, '%Y-%m-%d').replace(hour=9, tzinfo=timezone.utc)
        except (TypeError, ValueError):
            dt = datetime.now(timezone.utc)
        return format_datetime(dt)

    items = []
    for e in entries:
        link = f'{page}#{e["id"]}'
        items.append(
            '    <item>\n'
            f'      <title>{xml_escape(e["title"])}</title>\n'
            f'      <link>{xml_escape(link)}</link>\n'
            f'      <guid isPermaLink="false">podskrift-whats-new-{xml_escape(e["id"])}</guid>\n'
            f'      <pubDate>{rfc822(e.get("date"))}</pubDate>\n'
            f'      <description>{xml_escape(e["summary"])}</description>\n'
            '    </item>'
        )
    last = rfc822(entries[0].get('date')) if entries else format_datetime(datetime.now(timezone.utc))
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom" '
        'xmlns:sy="http://purl.org/rss/1.0/modules/syndication/">\n'
        '  <channel>\n'
        "    <title>Podskrift: what's new</title>\n"
        f'    <link>{xml_escape(page)}</link>\n'
        f'    <atom:link href="{xml_escape(self_url)}" rel="self" type="application/rss+xml"/>\n'
        '    <description>New Podskrift features, newest first. Built in public.</description>\n'
        '    <language>en</language>\n'
        f'    <lastBuildDate>{last}</lastBuildDate>\n'
        '    <sy:updatePeriod>daily</sy:updatePeriod>\n'
        '    <sy:updateFrequency>1</sy:updateFrequency>\n'
        + '\n'.join(items) + '\n'
        '  </channel>\n'
        '</rss>\n'
    )
    resp = Response(xml, mimetype='application/rss+xml')
    resp.headers['Cache-Control'] = 'public, max-age=3600'
    return resp


# ---------------------------------------------------------------------------
# Error pages
# ---------------------------------------------------------------------------
def _wants_json():
    return (request.path or '').startswith('/api/')


def not_found(error):
    if _wants_json():
        return jsonify({'error': 'not_found', 'message': 'No such endpoint.'}), 404
    return render_template('error.html', code=404), 404


def server_error(error):
    if _wants_json():
        return jsonify({'error': 'server_error',
                        'message': 'Something went wrong on our side.'}), 500
    # error_500.html is standalone (no base.html): if the database is what
    # broke, base.html's context processors would fail again here.
    return render_template('error_500.html'), 500


# ---------------------------------------------------------------------------
# Template helpers
# ---------------------------------------------------------------------------
_NB_WORDS = re.compile(r'\b(hvordan|jeg|norsk|podkast(en)?|hva|kan)\b', re.I)


def text_lang(text):
    """'nb' for the Norwegian FAQ entries on an English page, else ''."""
    return 'nb' if text and len(_NB_WORDS.findall(text)) >= 2 else ''


def inject_template_helpers():
    return {
        'asset_url': asset_url,
        'text_lang': text_lang,
        'theme_color': THEME_COLOR,
        'theme_color_dark': THEME_COLOR_DARK,
    }


def init_site_standards(app, *, public_base_url='', changelog_loader=None,
                        api_markdown_loader=None, posthog_host=None):
    _CFG['public_base_url'] = (public_base_url or '').rstrip('/')
    if changelog_loader:
        _CFG['changelog_loader'] = changelog_loader
    if api_markdown_loader:
        _CFG['api_markdown_loader'] = api_markdown_loader
    if posthog_host:
        _CFG['posthog_host'] = posthog_host
    app.register_blueprint(bp)
    app.register_error_handler(404, not_found)
    app.register_error_handler(500, server_error)
    app.context_processor(inject_template_helpers)
