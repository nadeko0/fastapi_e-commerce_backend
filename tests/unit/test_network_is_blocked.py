"""The suite cannot reach the outside world by accident.

pytest-socket is configured in pyproject.toml. These tests are the proof that
the guard is really on, not only installed: if the options are dropped, the
first one stops raising.
"""
import smtplib
import socket

import pytest
from pytest_socket import SocketConnectBlockedError

# The plugin warns about each blocked attempt, and here the attempt is the point.
pytestmark = pytest.mark.filterwarnings("ignore:A test tried to use socket")

# TEST-NET-1 (RFC 5737), reserved for documentation: nothing answers there.
_OUTSIDE = ("192.0.2.1", 80)


def _listener():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    return server


def test_a_connection_to_another_host_is_blocked():
    with socket.socket() as sock, pytest.raises(SocketConnectBlockedError):
        sock.connect(_OUTSIDE)


def test_the_blocked_connection_goes_through_the_stdlib_helper_too():
    with pytest.raises(SocketConnectBlockedError):
        socket.create_connection(_OUTSIDE, timeout=1)


def test_a_mail_client_cannot_reach_a_mail_server():
    with pytest.raises(SocketConnectBlockedError):
        smtplib.SMTP(_OUTSIDE[0], 25, timeout=1)


def test_loopback_by_address_stays_reachable():
    with _listener() as server, socket.socket() as client:
        client.connect(server.getsockname())


def test_localhost_by_name_stays_reachable():
    with _listener() as server, socket.create_connection(("localhost", server.getsockname()[1]), timeout=5):
        pass
