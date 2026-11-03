"""Durable paths and per-worktree ports for verify-podskrift.

Port range 5502–5599 is reserved for this skill (other verify skills use
8890–8988, 3310–3399, and Postgres 54339).
"""

from __future__ import annotations

import hashlib
import os
import socket
from pathlib import Path

PORT_MIN = 5502
PORT_MAX = 5599
PORT_SPAN = PORT_MAX - PORT_MIN + 1  # 98

# Stub OpenAI / fixture HTTP lives on port + 1000 within 6502–6599.
STUB_PORT_OFFSET = 1000

# verify_podskrift/ → skill dir → skills → .claude → repo
REPO_ROOT = Path(__file__).resolve().parents[4]
SKILL_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = SKILL_ROOT / 'fixtures'
VERIFY_DIR = REPO_ROOT / '.verify'


def durable_root() -> Path:
    return Path(
        os.environ.get('PODSKRIFT_VERIFY_HOME')
        or (Path.home() / '.local' / 'share' / 'podskrift-verify')
    ).expanduser().resolve()


# Back-compat for imports/tests that patch DURABLE_ROOT.
DURABLE_ROOT = durable_root()


def worktree_id(root: Path | None = None) -> str:
    path = str((root or REPO_ROOT).resolve())
    return hashlib.sha256(path.encode()).hexdigest()[:12]


def worktree_dir(root: Path | None = None) -> Path:
    return durable_root() / 'worktrees' / worktree_id(root)


def db_path(root: Path | None = None) -> Path:
    return worktree_dir(root) / 'podcast.db'


def state_path(root: Path | None = None) -> Path:
    return worktree_dir(root) / 'state.json'


def evidence_dir(root: Path | None = None) -> Path:
    """Evidence survives ``down`` — lives under the repo's ``.verify/``."""
    return VERIFY_DIR / 'evidence' / worktree_id(root)


def log_dir(root: Path | None = None) -> Path:
    return VERIFY_DIR / 'logs' / worktree_id(root)


def default_port(root: Path | None = None) -> int:
    wid = worktree_id(root)
    n = int(wid[:8], 16) % PORT_SPAN
    return PORT_MIN + n


def stub_port(app_port: int) -> int:
    return app_port + STUB_PORT_OFFSET


def port_in_range(port: int) -> bool:
    return PORT_MIN <= port <= PORT_MAX


def ensure_dirs(root: Path | None = None) -> None:
    worktree_dir(root).mkdir(parents=True, exist_ok=True)
    evidence_dir(root).mkdir(parents=True, exist_ok=True)
    log_dir(root).mkdir(parents=True, exist_ok=True)
    FIXTURES.mkdir(parents=True, exist_ok=True)


def port_owner_pid(port: int) -> int | None:
    """Best-effort PID listening on TCP port (Linux). None if free/unknown."""
    try:
        import subprocess
        out = subprocess.check_output(
            ['ss', '-ltnp', f'sport = :{port}'],
            text=True, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        if f':{port}' not in line:
            continue
        # users:(("python",pid=1234,fd=3))
        marker = 'pid='
        if marker in line:
            try:
                return int(line.split(marker, 1)[1].split(',', 1)[0])
            except ValueError:
                return None
    return None


def port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(('127.0.0.1', port))
            return True
        except OSError:
            return False
