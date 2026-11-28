"""Block test network access while preserving stdlib event-loop socket pairs."""

import _socket
import socket
import sys

_SOCKETPAIR_CODE = getattr(
    getattr(socket, "_fallback_socketpair", None), "__code__", None
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
            if _SOCKETPAIR_CODE is not None and frame.f_code is _SOCKETPAIR_CODE:
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
