"""TCP_NODELAY on the connections uvicorn accepts (app/core/http_protocol.py).

The server runs the way main.py runs it with workers > 1: uvicorn binds the
listener with Config.bind_socket() and the worker serves the socket it is
handed. No timing is measured: the option itself is read off the accepted
socket on the server side.
"""

import asyncio
import runpy
import socket
from pathlib import Path
from unittest import mock

import uvicorn

from app.core.http_protocol import NoDelayH11Protocol

MAIN_PY = Path(__file__).resolve().parents[2] / "main.py"


async def _app(scope, receive, send):  # pragma: no cover - no request is sent
    raise AssertionError("not called")


async def _until(condition) -> None:
    """Yield to the loop until `condition()` holds; fail after 5 s."""
    for _ in range(500):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached")


async def _nodelay_of_accepted_connection(http) -> int:
    config = uvicorn.Config(_app, host="127.0.0.1", port=0, http=http, lifespan="off", log_level="warning")
    listener = config.bind_socket()  # what the parent process does for its workers
    server = uvicorn.Server(config)
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    writer = None
    try:
        await _until(lambda: server.started)
        _, writer = await asyncio.open_connection(*listener.getsockname()[:2])
        await _until(lambda: server.server_state.connections)
        (connection,) = server.server_state.connections
        accepted = connection.transport.get_extra_info("socket")
        return accepted.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY)
    finally:
        if writer is not None:
            writer.close()
            await writer.wait_closed()
        server.should_exit = True
        await serving
        listener.close()


def test_a_connection_accepted_on_the_shared_listener_has_nodelay():
    assert asyncio.run(_nodelay_of_accepted_connection(NoDelayH11Protocol)) != 0


def test_plain_h11_on_the_shared_listener_leaves_nagle_on():
    # The defect the protocol works around (uvicorn 0.54, CPython 3.13). If
    # this starts failing, uvicorn's bind_socket now makes an IPPROTO_TCP
    # socket and NoDelayH11Protocol could be dropped.
    assert asyncio.run(_nodelay_of_accepted_connection("h11")) == 0


def test_main_runs_uvicorn_with_this_protocol_and_without_its_server_header_and_access_log():
    with mock.patch.object(uvicorn, "run") as run:
        runpy.run_path(str(MAIN_PY), run_name="__main__")
    kwargs = run.call_args.kwargs
    assert kwargs["http"] is NoDelayH11Protocol
    assert kwargs["server_header"] is False
    # The application writes the access line (app/core/access_log.py).
    assert kwargs["access_log"] is False
