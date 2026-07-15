"""Shared network-error detection utilities (used by main.py and worker_main.py)."""
from __future__ import annotations

NETWORK_ERROR_HINTS = (
    "getaddrinfo failed",
    "ConnectError",
    "ConnectTimeout",
    "ConnectionError",
    "ConnectionResetError",
    "ConnectionAbortedError",
    "RemoteProtocolError",
    "ReadError",
    "NetworkError",
    "TimeoutError",
    "TimedOut",           # telegram.error.TimedOut (PTB timeout on HTTP requests)
    "Timed out",          # message text of telegram.error.TimedOut
    "WinError 1231",
    "WinError 1232",
    "WinError 1236",
    "WinError 10022",
    "WinError 10060",
    "WinError 10061",
    "Network is unreachable",
    "Connection refused",
    "Server disconnected",
    "OSError",
)


def is_network_error(exc: BaseException) -> bool:
    """Return True if *exc* looks like a transient network/connectivity error."""
    msg = f"{type(exc).__name__}: {exc}"
    return any(hint in msg for hint in NETWORK_ERROR_HINTS)
