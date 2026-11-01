"""Offline network guard for tests: block real outbound connections, allow only the local socket-pair setup.

Why this exists. ``asyncio.run()`` builds an event loop, and every loop creates a "self-pipe" with ``socket.socketpair()``.
On Windows (Python 3.10) ``socketpair`` is emulated in Python: it listens on 127.0.0.1 and ``connect()``s to itself. A guard
that refuses *every* ``socket.connect`` therefore broke event-loop creation before any test code ran, even though no
network was used. This guard refuses every connect **except** a loopback connect made from inside ``socket.socketpair``
itself, so real outbound connections (and loopback connections to other local services such as a database) stay blocked.
"""

from __future__ import annotations

import ipaddress
import socket
import threading

_state = threading.local()
NETWORK_ATTEMPT = "network access attempted"


def _loopback(address) -> bool:
    if not isinstance(address, tuple) or not address:
        return not isinstance(address, tuple)  # AF_UNIX paths are local; other shapes are refused below
    try:
        return ipaddress.ip_address(address[0]).is_loopback
    except (ValueError, TypeError):
        return False  # a host NAME is never accepted: it would need DNS


def install(monkeypatch) -> None:
    """Install the guard through ``monkeypatch`` so everything is restored when the test ends."""
    real_socketpair = socket.socketpair
    real_connect = socket.socket.connect

    def socketpair(*args, **kwargs):
        previous = getattr(_state, "in_socketpair", False)
        _state.in_socketpair = True
        try:
            return real_socketpair(*args, **kwargs)
        finally:
            _state.in_socketpair = previous

    def connect(self, address):
        if getattr(_state, "in_socketpair", False) and _loopback(address):
            return real_connect(self, address)
        raise AssertionError(NETWORK_ATTEMPT)

    def connect_ex(self, address):
        raise AssertionError(NETWORK_ATTEMPT)

    monkeypatch.setattr(socket, "socketpair", socketpair)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
