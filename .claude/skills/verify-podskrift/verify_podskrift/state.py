"""PID / credential state for one worktree. Never commit; lives under durable dir."""

from __future__ import annotations

import json
import os
import signal
import time
from pathlib import Path
from typing import Any

from .paths import ensure_dirs, state_path


def load_state(root: Path | None = None) -> dict[str, Any]:
    path = state_path(root)
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(data: dict[str, Any], root: Path | None = None) -> Path:
    ensure_dirs(root)
    path = state_path(root)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + '\n',
                   encoding='utf-8')
    tmp.replace(path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def update_state(updates: dict[str, Any], root: Path | None = None) -> dict[str, Any]:
    data = load_state(root)
    data.update(updates)
    data['updated_at'] = time.time()
    save_state(data, root)
    return data


def clear_secrets(root: Path | None = None) -> None:
    """Drop passwords/tokens from state; keep port/pid history metadata."""
    data = load_state(root)
    for key in ('password', 'api_key', 'oauth_access_token', 'oauth_refresh_token',
                'storage_state_path'):
        data.pop(key, None)
    save_state(data, root)


def pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def stop_pid(pid: int | None, *, grace_sec: float = 3.0) -> bool:
    """SIGTERM then SIGKILL only the process group we started. Never pkill.

    ``up`` uses ``start_new_session=True``, so the child is its own session
    leader and ``killpg(pid, …)`` stays inside that tree.
    """
    if not pid_alive(pid):
        return False
    assert pid is not None

    def _signal(sig: int) -> None:
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            try:
                os.kill(pid, sig)
            except OSError:
                pass
        except OSError:
            try:
                os.kill(pid, sig)
            except OSError:
                pass

    _signal(signal.SIGTERM)
    deadline = time.time() + grace_sec
    while time.time() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.1)
    _signal(signal.SIGKILL)
    time.sleep(0.2)
    return not pid_alive(pid)
