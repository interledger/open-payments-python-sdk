"""Block test network access while preserving stdlib event-loop socket pairs."""

import _socket
import socket
import sys

_FALLBACK_SOCKETPAIR = getattr(socket, "_fallback_socketpair", socket.socketpair)
_SOCKETPAIR_CODES = tuple(
    function.__code__
    for function in (socket.socketpair, getattr(socket, "_fallback_socketpair", None))
    if hasattr(function, "__code__")
)
_CONNECT = _socket.socket.connect


def block_network(patch, blocked):
    """Install a fail-closed guard, including for literal-IP and localhost calls.

    Windows implements socketpair() with a private loopback listener. Allow
    only its own connecting socket, called directly from that stdlib function,
    to connect to that listener. No host-wide or thread-wide exception is made.
    """

    def connect(sock, address):
        frame = sys._getframe(1)
        try:
            if any(frame.f_code is code for code in _SOCKETPAIR_CODES):
                local = frame.f_locals
                listener = local.get("lsock")
                if (
                    sock is local.get("csock")
                    and listener is not None
                    and address == listener.getsockname()[:2]
                    and address[0] in ("127.0.0.1", "::1")
                ):
                    return _CONNECT(sock, address)
        finally:
            del frame
        return blocked(sock, address)

    patch(socket, "getaddrinfo", blocked)
    patch(socket, "create_connection", blocked)
    patch(socket.socket, "connect", connect)
    patch(socket.socket, "connect_ex", blocked)
