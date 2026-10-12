"""Shared pytest setup.

Sandboxed agent sessions (Claude Code on the web, some CI runners) route HTTP
through a proxy and have no DNS of their own. `_is_fetchable_url` resolves the
host before every fetch, so without DNS every test that enqueues
`https://example.com/ep.mp3` is refused with "cannot be fetched" -- about sixty
failures on an unchanged main, none of them real.

When, and only when, the machine cannot resolve names at all, public hostnames
resolve to a fixed public address. Reserved names (`localhost`, `.invalid`,
`.test`, `.local`, `.internal`) still fail as they would anywhere, and literal
IPs never touch DNS, so the SSRF tests keep their meaning. With working DNS this
file does nothing.
"""

import socket

import pytest

# example.com's published address: public, so the SSRF guard lets it through.
_PUBLIC_STAND_IN = '93.184.215.14'
_RESERVED_SUFFIXES = ('localhost', '.invalid', '.test', '.local', '.internal')
_real_getaddrinfo = socket.getaddrinfo


def _dns_works():
    try:
        _real_getaddrinfo('example.com', None)
        return True
    except OSError:
        return False


def _offline_getaddrinfo(host, port, *args, **kwargs):
    try:
        return _real_getaddrinfo(host, port, *args, **kwargs)
    except socket.gaierror:
        name = (host or '').rstrip('.').lower()
        if '.' not in name or name.endswith(_RESERVED_SUFFIXES):
            raise
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (_PUBLIC_STAND_IN, port or 0))]


@pytest.fixture(scope='session', autouse=True)
def _resolve_public_hosts_without_dns():
    if _dns_works():
        yield
        return
    socket.getaddrinfo = _offline_getaddrinfo
    try:
        yield
    finally:
        socket.getaddrinfo = _real_getaddrinfo
