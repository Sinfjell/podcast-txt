"""Deploy tracked-file permission fix (prod root git pull → app-user readable)."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OPS = ROOT / 'ops'
DEPLOY_YML = ROOT / '.github' / 'workflows' / 'deploy.yml'
SCRIPT = OPS / 'fix-tracked-perms.sh'


def test_fix_tracked_perms_sh_syntax():
    result = subprocess.run(
        ['bash', '-n', str(SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_fix_tracked_perms_sh_uses_git_ls_files_not_recursive_chown():
    text = SCRIPT.read_text(encoding='utf-8')
    assert 'git ls-files -z' in text
    assert 'chown -h' in text
    assert 'chmod 644' in text
    assert '100755' in text  # mode from git index, not filesystem -x
    # Must never recurse or chmod directories (data/ stays 750 + ACL).
    assert not any(
        line.lstrip().startswith(('chown -R', 'chmod -R'))
        for line in text.splitlines()
        if not line.lstrip().startswith('#')
    )
    # Dir loop must chown only — no chmod of parent paths (comments may say so).
    assert 'Parent dirs of tracked files' in text
    dir_section = text.split('Parent dirs of tracked files', 1)[-1]
    assert 'chown -h' in dir_section
    assert not any(
        line.lstrip().startswith('chmod')
        for line in dir_section.splitlines()
    )


def test_deploy_yml_runs_fix_tracked_perms_and_show_pages_checks():
    text = DEPLOY_YML.read_text(encoding='utf-8')
    assert 'ops/fix-tracked-perms.sh' in text
    assert 'APP_USER=podskrift' in text
    assert 'APP_GROUP=psaserv' in text
    assert 'sudo -u podskrift test -r data/show_pages.json' in text
    assert 'http://127.0.0.1:5002/podcasts' in text
    assert '/podcasts/the-daily' in text


def test_fix_tracked_perms_touches_tracked_only(tmp_path: Path):
    """Tracked files become 644/755 from git mode; dirs/untracked stay put."""
    repo = tmp_path / 'app'
    repo.mkdir()
    subprocess.run(['git', 'init'], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ['git', 'config', 'user.email', 'test@example.com'],
        cwd=repo, check=True, capture_output=True,
    )
    subprocess.run(
        ['git', 'config', 'user.name', 'test'],
        cwd=repo, check=True, capture_output=True,
    )

    data = repo / 'data'
    data.mkdir()
    data.chmod(0o750)
    tracked = data / 'show_pages.json'
    tracked.write_text('{"shows":[]}', encoding='utf-8')
    script_file = repo / 'run.sh'
    script_file.write_text('#!/bin/sh\necho ok\n', encoding='utf-8')
    script_file.chmod(0o755)

    untracked_db = data / 'podcast.db'
    untracked_db.write_bytes(b'sqlite')
    untracked_db.chmod(0o600)
    untracked_env = repo / '.env'
    untracked_env.write_text('SECRET=1\n', encoding='utf-8')
    untracked_env.chmod(0o600)

    # Symlink target must not be chmod'd if we ever listed a symlink (skip path).
    link_target = repo / 'link-target.txt'
    link_target.write_text('target\n', encoding='utf-8')
    link_target.chmod(0o600)
    tracked_link = repo / 'tracked-link.txt'
    tracked_link.symlink_to('link-target.txt')

    subprocess.run(
        ['git', 'add', 'data/show_pages.json', 'run.sh', 'tracked-link.txt'],
        cwd=repo, check=True,
    )
    subprocess.run(['git', 'commit', '-m', 'init'], cwd=repo, check=True, capture_output=True)

    # Simulate a root pull: tracked regular file is 640; strip +x from run.sh
    # so only the git index (100755) should restore 755.
    tracked.chmod(0o640)
    script_file.chmod(0o644)

    dest_script = repo / 'ops' / 'fix-tracked-perms.sh'
    dest_script.parent.mkdir()
    dest_script.write_text(SCRIPT.read_text(encoding='utf-8'), encoding='utf-8')
    dest_script.chmod(0o755)

    env = {
        **os.environ,
        'APP_USER': str(os.getuid()),
        'APP_GROUP': str(os.getgid()),
    }
    result = subprocess.run(
        ['bash', 'ops/fix-tracked-perms.sh'],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout

    tracked_mode = stat.S_IMODE(tracked.stat().st_mode)
    script_mode = stat.S_IMODE(script_file.stat().st_mode)
    db_mode = stat.S_IMODE(untracked_db.stat().st_mode)
    env_mode = stat.S_IMODE(untracked_env.stat().st_mode)
    data_mode = stat.S_IMODE(data.stat().st_mode)
    target_mode = stat.S_IMODE(link_target.stat().st_mode)

    assert tracked_mode == 0o644
    assert script_mode == 0o755  # from git 100755, not filesystem -x
    # data/ keeps 750 — script must not chmod parent dirs.
    assert data_mode == 0o750
    # Untracked runtime secrets must keep their tight modes.
    assert db_mode == 0o600
    assert env_mode == 0o600
    # Symlink chmod must not follow and loosen the target.
    assert target_mode == 0o600
