"""bin/verify-podskrift — JSON on stdout, human hints on stderr."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .env import (
    GuardError,
    assert_sqlite_inside_verify_dir,
    assert_stripe_keys_safe,
    build_verify_environ,
    env_snapshot_for_doctor,
    scrub_secrets,
    sqlite_file_from_url,
)
from .full_stt import (
    assert_full_stt_transcribe_allowed,
    record_full_stt_call,
)
from .paths import (
    PORT_MAX,
    PORT_MIN,
    REPO_ROOT,
    default_port,
    durable_root,
    ensure_dirs,
    evidence_dir,
    log_dir,
    port_is_free,
    port_owner_pid,
    stub_port,
    worktree_id,
)
from .seed import seed_users
from .shots import FEATURES, shot_feature
from .state import load_state, pid_alive, save_state, stop_pid, update_state

SERVER_MODULE = Path(__file__).resolve().parent / 'server.py'
SKILL_DIR = Path(__file__).resolve().parents[1]


def _emit(payload: dict[str, Any], *, ok: bool | None = None) -> int:
    if ok is not None:
        payload.setdefault('ok', ok)
    text = json.dumps(payload, indent=2, sort_keys=True)
    sys.stdout.write(scrub_secrets(text) + '\n')
    return 0 if payload.get('ok', True) else 1


def _err(msg: str) -> None:
    sys.stderr.write(scrub_secrets(msg).rstrip() + '\n')


def _base_url(state: dict | None = None, port: int | None = None) -> str:
    st = state if state is not None else load_state()
    p = port or st.get('port') or default_port()
    return f'http://127.0.0.1:{p}'


def _http_json(url: str, *, method: str = 'GET', data: bytes | None = None,
               headers: dict | None = None, timeout: float = 8.0) -> tuple[int, Any]:
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            ctype = resp.headers.get('Content-Type', '')
            if 'json' in ctype:
                return resp.status, json.loads(body.decode('utf-8'))
            return resp.status, body.decode('utf-8', errors='replace')[:500]
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw.decode('utf-8'))
        except Exception:  # noqa: BLE001
            return exc.code, raw.decode('utf-8', errors='replace')[:500]
    except Exception as exc:  # noqa: BLE001
        return 0, {'error': type(exc).__name__, 'detail': str(exc)[:200]}


def cmd_doctor(args: argparse.Namespace) -> int:
    ensure_dirs()
    state = load_state()
    port = args.port or state.get('port') or default_port()
    issues: list[str] = []
    checks: dict[str, Any] = {}

    try:
        env = build_verify_environ(port=port, stt_mode=args.stt)
        snap = env_snapshot_for_doctor(env)
        checks['env'] = snap
        assert_sqlite_inside_verify_dir(env['DATABASE_URL'])
        checks['sqlite_guard'] = 'pass'
    except GuardError as exc:
        issues.append(str(exc))
        checks['sqlite_guard'] = 'fail'
        env = None

    try:
        assert_stripe_keys_safe(os.environ)
        checks['stripe_guard'] = 'pass'
    except GuardError as exc:
        issues.append(str(exc))
        checks['stripe_guard'] = 'fail'

    pid = state.get('pid')
    checks['state_pid'] = pid
    checks['pid_alive'] = pid_alive(pid)
    checks['port'] = port
    checks['port_in_range'] = PORT_MIN <= port <= PORT_MAX
    checks['port_free'] = port_is_free(port)
    owner = port_owner_pid(port)
    checks['port_owner_pid'] = owner
    if owner and owner != pid:
        issues.append(
            f'port {port} is owned by PID {owner}, not our state pid {pid} — '
            f'run `down` or choose `--port`')

    # Live health if up
    if checks['pid_alive']:
        status, body = _http_json(f'{_base_url(state, port)}/health')
        checks['health_status'] = status
        checks['health'] = body if status == 200 else None
        if status != 200:
            issues.append(f'/health returned {status}')
        # MCP unauthenticated challenge
        st2, body2 = _http_json(
            f'{_base_url(state, port)}/mcp',
            method='POST',
            data=b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}',
            headers={'Content-Type': 'application/json'},
        )
        checks['mcp_unauth_status'] = st2
        www = None
        # Re-fetch headers via a manual request for WWW-Authenticate
        try:
            req = urllib.request.Request(
                f'{_base_url(state, port)}/mcp',
                data=b'{}', method='POST')
            req.add_header('Content-Type', 'application/json')
            urllib.request.urlopen(req, timeout=5)
        except urllib.error.HTTPError as exc:
            www = exc.headers.get('WWW-Authenticate')
            checks['mcp_www_authenticate'] = www
            if 'resource_metadata' not in (www or '') and 'Bearer' not in (www or ''):
                issues.append('MCP 401 missing Bearer/resource_metadata challenge')
        if st2 != 401:
            issues.append(f'MCP expected 401 unauthenticated, got {st2}')
    else:
        checks['health'] = None

    # DB pragma if file exists
    if env:
        db_file = sqlite_file_from_url(env['DATABASE_URL'])
        if db_file and db_file.is_file():
            con = sqlite3.connect(str(db_file))
            rows = con.execute('PRAGMA database_list').fetchall()
            con.close()
            main = next((r for r in rows if r[1] == 'main'), None)
            checks['pragma_database_list'] = [
                {'seq': r[0], 'name': r[1], 'file': r[2]} for r in rows]
            if main and main[2]:
                try:
                    assert_sqlite_inside_verify_dir(
                        f'sqlite:///{Path(main[2]).resolve()}')
                    checks['pragma_path_guard'] = 'pass'
                except GuardError as exc:
                    issues.append(str(exc))
                    checks['pragma_path_guard'] = 'fail'

    checks['worktree_id'] = worktree_id()
    checks['durable_root'] = str(durable_root())
    checks['evidence_dir'] = str(evidence_dir())
    checks['tz_expected'] = 'UTC'
    checks['ffmpeg'] = bool(subprocess.call(
        ['ffmpeg', '-version'], stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL) == 0)

    ok = not issues
    return _emit({
        'ok': ok,
        'command': 'doctor',
        'checks': checks,
        'issues': issues,
    }, ok=ok)


def cmd_up(args: argparse.Namespace) -> int:
    ensure_dirs()
    state = load_state()
    if pid_alive(state.get('pid')):
        return _emit({
            'ok': True,
            'command': 'up',
            'already_running': True,
            'pid': state.get('pid'),
            'port': state.get('port'),
            'base_url': _base_url(state),
        })

    port = args.port or default_port()
    if not (PORT_MIN <= port <= PORT_MAX):
        return _emit({
            'ok': False,
            'error': f'port {port} outside verify range {PORT_MIN}-{PORT_MAX}',
        }, ok=False)

    if not port_is_free(port):
        owner = port_owner_pid(port)
        return _emit({
            'ok': False,
            'error': (
                f'port {port} owned by PID {owner or "unknown"}, not ours — '
                f'run `down` or pass `--port`'),
        }, ok=False)

    try:
        env = build_verify_environ(port=port, stt_mode=args.stt)
    except GuardError as exc:
        return _emit({'ok': False, 'error': str(exc)}, ok=False)

    if args.dry_run:
        return _emit({
            'ok': True,
            'dry_run': True,
            'command': 'up',
            'would_start': {
                'port': port,
                'stub_port': stub_port(port),
                'database': env['DATABASE_URL'],
                'stt': args.stt,
                'tz': env['TZ'],
            },
        })

    log_path = log_dir() / 'up.log'
    log_path.parent.mkdir(parents=True, exist_ok=True)
    child_env = dict(os.environ)
    child_env.update(env)
    # Ensure PYTHONPATH includes repo + skill dir (package parent).
    child_env['PYTHONPATH'] = os.pathsep.join(
        filter(None, [str(REPO_ROOT), str(SKILL_DIR),
                      child_env.get('PYTHONPATH', '')]))
    child_env['PODSKRIFT_VERIFY_PORT'] = str(port)

    python = sys.executable
    log_fh = open(log_path, 'ab')  # noqa: SIM115 — kept for process lifetime
    proc = subprocess.Popen(
        [python, '-m', 'verify_podskrift.server'],
        cwd=str(REPO_ROOT),
        env=child_env,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    update_state({
        'pid': proc.pid,
        'port': port,
        'stub_port': stub_port(port),
        'stt': args.stt,
        'started_at': time.time(),
        'log': str(log_path),
        'database_url': env['DATABASE_URL'],
    })

    # Wait for /health
    deadline = time.time() + 45
    last = None
    while time.time() < deadline:
        if proc.poll() is not None:
            log_fh.close()
            return _emit({
                'ok': False,
                'error': f'server exited early with code {proc.returncode}',
                'log': str(log_path),
            }, ok=False)
        status, body = _http_json(f'http://127.0.0.1:{port}/health', timeout=2)
        last = (status, body)
        if status == 200:
            break
        time.sleep(0.25)
    else:
        stop_pid(proc.pid)
        log_fh.close()
        return _emit({
            'ok': False,
            'error': 'timed out waiting for /health',
            'last': last,
            'log': str(log_path),
        }, ok=False)

    log_fh.close()
    return _emit({
        'ok': True,
        'command': 'up',
        'pid': proc.pid,
        'port': port,
        'stub_port': stub_port(port),
        'base_url': f'http://127.0.0.1:{port}',
        'audio_url': f'http://127.0.0.1:{stub_port(port)}/audio.mp3',
        'stt': args.stt,
        'health': last[1] if last else None,
        'evidence_dir': str(evidence_dir()),
    })


def cmd_down(args: argparse.Namespace) -> int:
    state = load_state()
    pid = state.get('pid')
    if args.dry_run:
        return _emit({
            'ok': True,
            'dry_run': True,
            'command': 'down',
            'would_stop_pid': pid,
            'evidence_kept': str(evidence_dir()),
        })
    stopped = stop_pid(pid)
    # Evidence must survive
    ev = evidence_dir()
    surviving = []
    if ev.is_dir():
        surviving = [str(p) for p in ev.rglob('*') if p.is_file()][:50]
    new_state = {k: v for k, v in state.items()
                 if k not in ('pid',)}
    new_state['pid'] = None
    new_state['stopped_at'] = time.time()
    save_state(new_state)
    return _emit({
        'ok': True,
        'command': 'down',
        'stopped': stopped,
        'was_pid': pid,
        'evidence_surviving': len(surviving),
        'evidence_dir': str(ev),
        'evidence_sample': surviving[:5],
    })


def _import_app_with_verify_env(port: int | None = None):
    """Import ``app`` under verify env for seed/login against a live or offline DB.

    Prefer talking to the running server for HTTP drives; seed uses SQLAlchemy
    against the same DATABASE_URL the server uses.
    """
    port = port or load_state().get('port') or default_port()
    env = build_verify_environ(port=port)
    # Apply into this process so models hit the verify DB.
    for k, v in env.items():
        os.environ[k] = v
    # If app already imported with another DB, force URI.
    if 'app' in sys.modules:
        import app as A
        A.app.config['SQLALCHEMY_DATABASE_URI'] = env['DATABASE_URL']
        return A
    import app as A  # noqa: WPS433
    return A


def cmd_seed(args: argparse.Namespace) -> int:
    ensure_dirs()
    if args.dry_run:
        from .seed import seed_users as su
        # dry_run path without importing app
        return _emit(su(None, dry_run=True) | {'command': 'seed'})

    try:
        A = _import_app_with_verify_env()
    except GuardError as exc:
        return _emit({'ok': False, 'error': str(exc)}, ok=False)

    result = seed_users(A, dry_run=False)
    secrets = result.pop('_secrets', {})
    update_state({
        'email': secrets.get('email'),
        'admin_email': secrets.get('admin_email'),
        'empty_email': secrets.get('empty_email'),
        'password': secrets.get('password'),
        'admin_password': secrets.get('admin_password'),
        'empty_password': secrets.get('empty_password'),
        'api_key': secrets.get('api_key'),
        'oauth_access_token': secrets.get('oauth_access_token'),
        'seeded_at': time.time(),
    })
    result['command'] = 'seed'
    result['secrets_stored_in_state'] = True
    result['state_path'] = str(
        durable_root() / 'worktrees' / worktree_id() / 'state.json')
    # Never print secrets
    return _emit(result)


def cmd_login(args: argparse.Namespace) -> int:
    """Verify credentials work against the running server; save storage state."""
    state = load_state()
    if not state.get('password'):
        return _emit({
            'ok': False,
            'error': 'no seeded user — run `seed` first',
        }, ok=False)
    if not pid_alive(state.get('pid')):
        return _emit({
            'ok': False,
            'error': 'server not running — run `up` first',
        }, ok=False)

    base = _base_url(state)
    role = args.user or 'user'
    if role == 'admin':
        email, password = state['admin_email'], state['admin_password']
    elif role == 'empty':
        email, password = state['empty_email'], state['empty_password']
    else:
        email, password = state['email'], state['password']

    if args.dry_run:
        return _emit({
            'ok': True,
            'dry_run': True,
            'command': 'login',
            'role': role,
            'email_domain': email.split('@')[-1] if email else None,
        })

    # Form login via http cookie jar
    import http.cookiejar
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar))
    # GET login for session cookie
    opener.open(f'{base}/login', timeout=10)
    boundary = '----verifyboundary'
    body = (
        f'--{boundary}\r\n'
        f'Content-Disposition: form-data; name="email"\r\n\r\n{email}\r\n'
        f'--{boundary}\r\n'
        f'Content-Disposition: form-data; name="password"\r\n\r\n{password}\r\n'
        f'--{boundary}--\r\n'
    ).encode()
    req = urllib.request.Request(f'{base}/login', data=body, method='POST')
    req.add_header('Content-Type', f'multipart/form-data; boundary={boundary}')
    try:
        resp = opener.open(req, timeout=10)
        final_url = resp.geturl()
        status = getattr(resp, 'status', 200) or 200
    except urllib.error.HTTPError as exc:
        return _emit({
            'ok': False,
            'error': f'login HTTP {exc.code}',
        }, ok=False)

    # Playwright storage state when available
    storage_path = evidence_dir() / f'storage-{role}.json'
    storage_path.parent.mkdir(parents=True, exist_ok=True)
    playwright_ok = False
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(base_url=base)
            page = context.new_page()
            page.goto('/login', wait_until='domcontentloaded')
            page.fill('input[name="email"]', email)
            page.fill('input[name="password"]', password)
            page.click('button[type="submit"]')
            page.wait_for_load_state('domcontentloaded')
            context.storage_state(path=str(storage_path))
            browser.close()
        playwright_ok = True
        update_state({'storage_state_path': str(storage_path),
                      'logged_in_role': role})
    except Exception as exc:  # noqa: BLE001
        _err(f'playwright storage skipped: {type(exc).__name__}')

    # MCP token check (psk_)
    mcp = None
    api_key = state.get('api_key')
    if api_key and role == 'user':
        st, body = _http_json(
            f'{base}/mcp',
            method='POST',
            data=json.dumps({
                'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                'params': {
                    'protocolVersion': '2025-03-26',
                    'capabilities': {},
                    'clientInfo': {'name': 'verify-podskrift', 'version': '0'},
                },
            }).encode(),
            headers={
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {api_key}',
            },
        )
        mcp = {'status': st, 'server': (body or {}).get('result', {}).get(
            'serverInfo', {}).get('name') if isinstance(body, dict) else None}

    return _emit({
        'ok': True,
        'command': 'login',
        'role': role,
        'email_domain': email.split('@')[-1],
        'redirect': final_url.replace(base, ''),
        'http_status': status,
        'storage_state': str(storage_path) if playwright_ok else None,
        'mcp_initialize': mcp,
        'note': 'password stays in state file only — never type a real password',
    })


def cmd_transcribe(args: argparse.Namespace) -> int:
    state = load_state()
    if not pid_alive(state.get('pid')):
        return _emit({'ok': False, 'error': 'server not up — run `up`'}, ok=False)
    if not state.get('api_key'):
        return _emit({'ok': False, 'error': 'run `seed` first'}, ok=False)

    base = _base_url(state)
    stub_p = state.get('stub_port') or stub_port(state['port'])
    audio_url = args.audio_url or f'http://127.0.0.1:{stub_p}/audio.mp3'
    if args.fail:
        audio_url = f'http://127.0.0.1:{stub_p}/fail.mp3'

    # Unique title so /api/v1/transcriptions does not reuse a prior completed task.
    stamp = int(time.time())
    title = (
        f'Verify Fail Episode {stamp}' if args.fail
        else f'Verify Stub Episode {stamp}'
    )

    if args.dry_run:
        return _emit({
            'ok': True,
            'dry_run': True,
            'command': 'transcribe',
            'audio_url': audio_url,
            'title': title,
            'stt': state.get('stt', 'stub'),
        })

    measured_duration = None
    if state.get('stt') == 'full':
        # Hard stop: measure input, refuse ≥30s / unknown duration, and
        # reserve the single allowed provider call in the verify state dir
        # *before* the API request that would hit Whisper.
        try:
            measured_duration = assert_full_stt_transcribe_allowed(
                audio_url, state=state)
            record_full_stt_call()
        except GuardError as exc:
            return _emit({
                'ok': False,
                'command': 'transcribe',
                'error': str(exc),
                'stt': 'full',
                'audio_url': audio_url,
            }, ok=False)

    body = json.dumps({
        'audio_url': audio_url,
        'title': title,
        'episode_title': title,
        'publisher': 'Verify Stub Show',
        'podcast_name': 'Verify Stub Show',
        'duration_min': (
            round(measured_duration / 60.0, 4)
            if measured_duration is not None else 0.05
        ),
        'language': 'en',
    }).encode()
    st, payload = _http_json(
        f'{base}/api/v1/transcriptions',
        method='POST',
        data=body,
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {state["api_key"]}',
        },
        timeout=30,
    )
    if st not in (200, 201) or not isinstance(payload, dict):
        return _emit({
            'ok': False,
            'command': 'transcribe',
            'http_status': st,
            'response': payload,
        }, ok=False)

    task_id = payload.get('id')
    # Poll via customer API (session /status needs a browser cookie).
    deadline = time.time() + (args.timeout or 120)
    final = None
    status = None
    while time.time() < deadline:
        pst, pbody = _http_json(
            f'{base}/api/v1/episodes/{task_id}',
            headers={'Authorization': f'Bearer {state["api_key"]}'},
            timeout=10,
        )
        final = pbody if isinstance(pbody, dict) else {'raw': pbody}
        status = None
        if isinstance(final, dict):
            status = final.get('transcript_status') or final.get('status')
        if status in ('completed', 'ready', 'error', 'cancelled', 'failed'):
            break
        # Also peek at transcript endpoint — ready is definitive.
        tst, tbody = _http_json(
            f'{base}/api/v1/episodes/{task_id}/transcript',
            headers={'Authorization': f'Bearer {state["api_key"]}'},
            timeout=10,
        )
        if tst == 200 and isinstance(tbody, dict):
            ts = tbody.get('transcript_status')
            if ts in ('ready', 'error', 'failed'):
                status = ts
                final = {**(final or {}), 'transcript_status': ts,
                         'transcript_peek': {
                             'text_len': len(tbody.get('text') or ''),
                             'has_text': bool(tbody.get('text')),
                         }}
                break
        time.sleep(0.4)

    transcript = None
    tst, tbody = _http_json(
        f'{base}/api/v1/episodes/{task_id}/transcript',
        headers={'Authorization': f'Bearer {state["api_key"]}'},
    )
    if tst == 200 and isinstance(tbody, dict):
        transcript = {
            'transcript_status': tbody.get('transcript_status'),
            'text_len': len(tbody.get('text') or ''),
            'has_text': bool(tbody.get('text')),
        }
        if not status:
            status = tbody.get('transcript_status')

    ev = evidence_dir() / 'transcribe'
    ev.mkdir(parents=True, exist_ok=True)
    report = {
        'task_id': task_id,
        'start_status': st,
        'final': {
            'status': status,
            'phase': (final or {}).get('phase') if isinstance(final, dict) else None,
        },
        'transcript': transcript,
        'audio_url_host': '127.0.0.1',
        'fail_path': bool(args.fail),
    }
    (ev / f'{task_id}.json').write_text(
        json.dumps(report, indent=2) + '\n', encoding='utf-8')

    if args.fail:
        ok = status in ('error', 'failed')
    else:
        ok = status in ('completed', 'ready') and bool(
            (transcript or {}).get('has_text'))
    return _emit({
        'ok': ok,
        'command': 'transcribe',
        **report,
        'evidence': str(ev / f'{task_id}.json'),
    }, ok=ok)


def cmd_shot(args: argparse.Namespace) -> int:
    state = load_state()
    if not pid_alive(state.get('pid')):
        return _emit({'ok': False, 'error': 'server not up — run `up`'}, ok=False)
    base = _base_url(state)
    feature = args.feature
    if feature == 'list':
        return _emit({'ok': True, 'features': sorted(FEATURES)})
    result = shot_feature(
        base, feature, dry_run=args.dry_run,
        color_schemes=tuple(args.scheme) if args.scheme else ('light', 'dark'),
    )
    result['command'] = 'shot'
    return _emit(result, ok=result.get('ok'))


def cmd_check(args: argparse.Namespace) -> int:
    """Fast local assertions — not a CI job. Composes doctor + optional MCP/tool."""
    t0 = time.time()
    # Reuse doctor
    doc_ns = argparse.Namespace(port=args.port, stt=args.stt)
    # Capture doctor by calling internals
    from io import StringIO
    old = sys.stdout
    buf = StringIO()
    sys.stdout = buf
    try:
        code = cmd_doctor(doc_ns)
    finally:
        sys.stdout = old
    doctor = json.loads(buf.getvalue())
    results = {'doctor': doctor}

    state = load_state()
    if pid_alive(state.get('pid')) and state.get('api_key'):
        base = _base_url(state)
        st, body = _http_json(
            f'{base}/mcp',
            method='POST',
            data=json.dumps({
                'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {},
            }).encode(),
            headers={
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {state["api_key"]}',
            },
        )
        tool_names = []
        if isinstance(body, dict):
            tools = (body.get('result') or {}).get('tools') or []
            tool_names = [t.get('name') for t in tools if isinstance(t, dict)]
        results['mcp_tools'] = {
            'status': st,
            'tool_count': len(tool_names),
            'tools': tool_names[:20],
        }
        # OAuth token if present
        if state.get('oauth_access_token'):
            st2, _ = _http_json(
                f'{base}/mcp',
                method='POST',
                data=json.dumps({
                    'jsonrpc': '2.0', 'id': 3, 'method': 'ping', 'params': {},
                }).encode(),
                headers={
                    'Content-Type': 'application/json',
                    'Authorization': f'Bearer {state["oauth_access_token"]}',
                },
            )
            results['mcp_oauth'] = {'status': st2}

    elapsed = round(time.time() - t0, 2)
    ok = code == 0 and doctor.get('ok')
    if 'mcp_tools' in results and results['mcp_tools']['status'] != 200:
        ok = False
    return _emit({
        'ok': ok,
        'command': 'check',
        'elapsed_sec': elapsed,
        'results': results,
    }, ok=ok)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog='verify-podskrift',
        description='Local verification harness for Podskrift (Flask + SQLite).',
    )
    p.add_argument('--version', action='store_true')
    sub = p.add_subparsers(dest='cmd')

    def add_dry(sp):
        sp.add_argument('--dry-run', action='store_true',
                        help='print what would happen without writing')

    d = sub.add_parser('doctor', help='read-only health + prod guards')
    d.add_argument('--port', type=int)
    d.add_argument('--stt', choices=('stub', 'full'), default='stub')
    d.set_defaults(func=cmd_doctor)

    up = sub.add_parser('up', help='start verify Flask + fixture HTTP')
    up.add_argument('--port', type=int)
    up.add_argument('--stt', choices=('stub', 'full'), default='stub')
    add_dry(up)
    up.set_defaults(func=cmd_up)

    down = sub.add_parser('down', help='stop only the PID we started')
    add_dry(down)
    down.set_defaults(func=cmd_down)

    seed = sub.add_parser('seed', help='fixture users + MCP tokens (no prod DB)')
    add_dry(seed)
    seed.set_defaults(func=cmd_seed)

    login = sub.add_parser('login', help='login as seeded user; save storage state')
    login.add_argument('--user', choices=('user', 'admin', 'empty'), default='user')
    add_dry(login)
    login.set_defaults(func=cmd_login)

    tr = sub.add_parser('transcribe', help='drive transcription via API (stub STT)')
    tr.add_argument('--audio-url')
    tr.add_argument('--fail', action='store_true',
                    help='exercise stub failure path')
    tr.add_argument('--timeout', type=int, default=120)
    add_dry(tr)
    tr.set_defaults(func=cmd_transcribe)

    sh = sub.add_parser('shot', help='screenshot a Feature Map page')
    sh.add_argument('feature', help='feature id or "list"')
    sh.add_argument('--scheme', nargs='*', choices=('light', 'dark'))
    add_dry(sh)
    sh.set_defaults(func=cmd_shot)

    ch = sub.add_parser('check', help='fast local assertions (not a CI job)')
    ch.add_argument('--port', type=int)
    ch.add_argument('--stt', choices=('stub', 'full'), default='stub')
    ch.set_defaults(func=cmd_check)

    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    if not argv or argv == ['--help']:
        parser.print_help()
        return 0
    if argv == ['--version']:
        from . import __version__
        return _emit({'ok': True, 'version': __version__})
    # Per-subcommand help
    if len(argv) >= 2 and argv[1] in ('--help', '-h'):
        parser.parse_args(argv)
        return 0
    args = parser.parse_args(argv)
    if not getattr(args, 'cmd', None):
        parser.print_help()
        return 0
    try:
        return args.func(args)
    except GuardError as exc:
        return _emit({'ok': False, 'error': str(exc)}, ok=False)
    except BrokenPipeError:
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
