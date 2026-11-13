"""uvicorn's h11 protocol with TCP_NODELAY set on every accepted connection.

Why: with workers > 1 (main.py) uvicorn 0.54 binds the shared listener in the
parent with Config.bind_socket(), which creates it as
socket.socket(family=...), so its `proto` is 0. Accepted sockets take the
listener's proto, and CPython 3.13's asyncio sets TCP_NODELAY on an accepted
socket only when proto == IPPROTO_TCP (asyncio.base_events._set_nodelay).
Nagle's algorithm then stayed on in every worker: h11 writes the status line
and headers, then the body, as two writes, and the body waited for the
client's delayed ACK, about 40 ms on every reused keep-alive connection
(Caddy -> api). A single worker never calls bind_socket and was not affected.

Setting the option per connection here does not depend on how the listener
was made, so it holds for the single-worker, multi-worker and reload paths
alike, and survives a uvicorn upgrade that fixes or changes bind_socket.
Re-check this class when uvicorn is upgraded: it overrides
H11Protocol.connection_made (uvicorn/protocols/http/h11_impl.py).
"""

import asyncio
import socket

from uvicorn.protocols.http.h11_impl import H11Protocol


class NoDelayH11Protocol(H11Protocol):
    def connection_made(self, transport: asyncio.Transport) -> None:  # type: ignore[override]  # as H11Protocol's
        sock = transport.get_extra_info("socket")
        if sock is not None and sock.family in (socket.AF_INET, socket.AF_INET6):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        super().connection_made(transport)
