"""Child process entry: fixture HTTP + Flask with verify env and STT stub.

Started only by ``bin/verify-podskrift up``. Sets env before importing ``app``
so empty-string keys beat ``.env`` (dotenv override=False).
"""

from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def _skill_dir() -> Path:
    return Path(__file__).resolve().parents[1]


def _prepare_sys_path() -> None:
    skill = str(_skill_dir())
    if skill not in sys.path:
        sys.path.insert(0, skill)
    repo = str(_skill_dir().parents[2])  # skill → skills → .claude → wait
    # skill_dir = .claude/skills/verify-podskrift
    # parents[0]=skills, [1]=.claude, [2]=repo
    repo = str(Path(__file__).resolve().parents[4])
    if repo not in sys.path:
        sys.path.insert(0, repo)


def start_fixture_http(port: int, audio_path: Path) -> ThreadingHTTPServer:
    """Serve fixture audio and a tiny RSS feed on loopback."""
    audio_bytes = audio_path.read_bytes()
    fail_name = 'verify-fail-2s.mp3'
    from verify_podskrift.fixture_audio import ensure_fail_mp3
    fail_bytes = ensure_fail_mp3().read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # noqa: N802 - stdlib API
            return

        def do_GET(self):  # noqa: N802
            path = self.path.split('?', 1)[0]
            if path in ('/audio.mp3', '/short.mp3'):
                body, ctype = audio_bytes, 'audio/mpeg'
            elif path.endswith(fail_name) or path == '/fail.mp3':
                body, ctype = fail_bytes, 'audio/mpeg'
            elif path in ('/feed.xml', '/rss.xml'):
                body = (
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<rss version="2.0"><channel>'
                    '<title>Verify Stub Show</title>'
                    '<item><title>Verify Stub Episode</title>'
                    f'<enclosure url="http://127.0.0.1:{port}/audio.mp3" '
                    'type="audio/mpeg" length="1"/>'
                    '<itunes:duration>3</itunes:duration>'
                    '</item></channel></rss>'
                ).encode('utf-8')
                ctype = 'application/rss+xml'
            elif path == '/health':
                body, ctype = b'{"ok":true}', 'application/json'
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd


def main() -> int:
    _prepare_sys_path()
    from verify_podskrift.env import (
        GuardError, apply_environ, build_verify_environ,
    )
    from verify_podskrift.fixture_audio import ensure_short_mp3
    from verify_podskrift.paths import log_dir
    from verify_podskrift.stt_stub import install_stubs

    port = int(os.environ.get('PODSKRIFT_VERIFY_PORT') or '5502')
    stt_mode = (os.environ.get('PODSKRIFT_VERIFY_STT') or 'stub').strip().lower()
    try:
        env = build_verify_environ(port=port, stt_mode=stt_mode)
    except GuardError as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}), flush=True)
        return 2

    apply_environ(env)

    audio = ensure_short_mp3()
    stub_p = int(env['PODSKRIFT_VERIFY_STUB_PORT'])
    start_fixture_http(stub_p, audio)

    # Import app only after env is set.
    import app as A  # noqa: WPS433 — intentional late import
    installed = install_stubs(A)

    log_path = log_dir() / 'server.log'
    log_path.parent.mkdir(parents=True, exist_ok=True)

    ready = {
        'ok': True,
        'event': 'verify-server-ready',
        'port': port,
        'stub_port': stub_p,
        'audio_url': f'http://127.0.0.1:{stub_p}/audio.mp3',
        'stt': installed,
        'pid': os.getpid(),
        'database': env['DATABASE_URL'],
    }
    print(json.dumps(ready), flush=True)
    with open(log_path, 'a', encoding='utf-8') as fh:
        fh.write(json.dumps(ready) + '\n')

    # Werkzeug threaded so status polls work while a job runs.
    A.app.run(host='127.0.0.1', port=port, threaded=True, use_reloader=False)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
