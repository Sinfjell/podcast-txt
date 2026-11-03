"""Playwright screenshots for Feature Map pages (desktop + mobile)."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from .paths import evidence_dir
from .seed import ADMIN_USER_EMAIL, EMPTY_USER_EMAIL, TEST_USER_EMAIL
from .state import load_state, update_state

# Feature → path + which seeded user to log in as (None = anonymous).
FEATURES: dict[str, dict[str, Any]] = {
    'home': {'path': '/', 'user': None, 'title': 'home'},
    'signup': {'path': '/register', 'user': None, 'title': 'signup'},
    'login': {'path': '/login', 'user': None, 'title': 'login'},
    'transcribe': {'path': '/', 'user': 'user', 'title': 'transcribe-home'},
    'paywall': {'path': '/', 'user': 'empty', 'title': 'paywall-empty',
                'open_buy': True},
    'settings': {'path': '/settings', 'user': 'user', 'title': 'settings'},
    'admin': {'path': '/admin', 'user': 'admin', 'title': 'admin'},
    'mcp-docs': {'path': '/docs/api', 'user': None, 'title': 'mcp-docs'},
    'whats-new': {'path': '/whats-new', 'user': None, 'title': 'whats-new'},
}

VIEWPORTS = {
    'desktop': {'width': 1280, 'height': 800},
    'mobile': {'width': 390, 'height': 844},
}


def _credentials(role: str | None, state: dict) -> tuple[str, str] | None:
    if role is None:
        return None
    if role == 'user':
        return state.get('email') or TEST_USER_EMAIL, state['password']
    if role == 'admin':
        return state.get('admin_email') or ADMIN_USER_EMAIL, state['admin_password']
    if role == 'empty':
        return state.get('empty_email') or EMPTY_USER_EMAIL, state['empty_password']
    raise ValueError(f'unknown user role {role!r}')


def shot_feature(
        base_url: str,
        feature: str,
        *,
        color_schemes: tuple[str, ...] = ('light', 'dark'),
        dry_run: bool = False,
) -> dict[str, Any]:
    if feature not in FEATURES:
        return {
            'ok': False,
            'error': f'unknown feature {feature!r}',
            'known': sorted(FEATURES),
        }
    spec = FEATURES[feature]
    state = load_state()
    if spec['user'] and not state.get('password'):
        return {
            'ok': False,
            'error': 'no seeded passwords in state — run seed + login first',
        }

    out_dir = evidence_dir() / 'shots' / feature
    if dry_run:
        return {
            'ok': True,
            'dry_run': True,
            'feature': feature,
            'path': spec['path'],
            'would_write': str(out_dir),
            'viewports': list(VIEWPORTS),
            'color_schemes': list(color_schemes),
        }

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {
            'ok': False,
            'error': 'playwright not installed — pip install playwright && '
                     'playwright install chromium',
        }

    out_dir.mkdir(parents=True, exist_ok=True)
    files: list[str] = []
    errors: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        for scheme in color_schemes:
            for vp_name, vp in VIEWPORTS.items():
                context = browser.new_context(
                    viewport=vp,
                    color_scheme=scheme,  # type: ignore[arg-type]
                    base_url=base_url,
                )
                page = context.new_page()
                try:
                    creds = _credentials(spec['user'], state)
                    if creds:
                        email, password = creds
                        page.goto('/login', wait_until='domcontentloaded')
                        page.fill('input[name="email"]', email)
                        page.fill('input[name="password"]', password)
                        page.click('button[type="submit"]')
                        page.wait_for_load_state('domcontentloaded')
                    page.goto(spec['path'], wait_until='domcontentloaded')
                    if spec.get('open_buy'):
                        # Prefer the header Buy control when present.
                        btn = page.locator('[data-open-buy-modal]').first
                        if btn.count():
                            btn.click(timeout=3000)
                            page.wait_for_timeout(400)
                    time.sleep(0.4)
                    dest = out_dir / f'{spec["title"]}-{vp_name}-{scheme}.png'
                    page.screenshot(path=str(dest), full_page=True)
                    files.append(str(dest))
                except Exception as exc:  # noqa: BLE001
                    errors.append(f'{vp_name}/{scheme}: {type(exc).__name__}')
                finally:
                    context.close()
        browser.close()

    update_state({'last_shot_feature': feature, 'last_shot_files': files})
    return {
        'ok': not errors,
        'feature': feature,
        'files': files,
        'file_count': len(files),
        'errors': errors or None,
        'evidence_dir': str(out_dir),
    }
