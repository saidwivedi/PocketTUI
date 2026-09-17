"""Port forwarding: a loopback-only dev server reached at this machine's address.

The point of every test here is the one property the feature exists for — a
server that bound 127.0.0.1 and nothing else answers on an address the phone can
reach, on the same port number, with no step taken by the user. The rest guard
the refusals, because this route binds sockets on the strength of a hostname a
client sent, and the answer it gives ("relayed", "direct", or a refusal) is what
the shell's silent fire-and-forget tap depends on being cheap and repeatable.

The hostnames in the comments are example.net ones on purpose: a real tailnet
name in this repo fails the deploy's leak scan.
"""

import asyncio
import errno
import socket
import sys
import threading
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as A  # noqa: E402

TOKEN = "ABCDEFGHIJ"
HDRS = {A.TOKEN_HEADER: TOKEN}


@pytest.fixture
def client(monkeypatch):
    # Context-managed like the attach tests': the relay's listening sockets are
    # owned by the loop the request ran on, so every request in a test has to
    # share one portal or the first teardown would take the relay with it.
    monkeypatch.setattr(A, "AUTH_TOKEN", TOKEN)
    with TestClient(A.app) as c:
        yield c


@pytest.fixture(autouse=True)
def fresh_limits(monkeypatch):
    monkeypatch.setattr(A, "LIMITER", A.AuthLimiter())
    monkeypatch.setattr(A, "RATE", A.RateLimiter())


@pytest.fixture(autouse=True)
def no_relays():
    """No relay outlives its test, whichever way the test ended."""
    A.RELAYS.clear()
    yield
    for relay in list(A.RELAYS.values()):
        relay.close()
    A.RELAYS.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def reachable_address() -> str:
    """One address of this machine that is not loopback, or "" if it has none.

    The UDP connect names a route without sending anything, which is the only
    way to pick the address a phone would actually reach this box on when the
    hostname resolves to several (or to nothing).
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))
        ip = s.getsockname()[0]
    except OSError:
        return ""
    finally:
        s.close()
    return "" if ip.startswith("127.") else ip


LAN_IP = reachable_address()
needs_address = pytest.mark.skipif(
    not LAN_IP, reason="this machine has no non-loopback address to bind")


class EchoServer:
    """A dev server's stand-in: listens, and sends back what it is sent.

    Bound to 127.0.0.1 only, which is the whole situation being tested — the
    link the phone was handed names an address this listener is not on.
    """

    def __init__(self, host: str = "127.0.0.1") -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, 0))
        self.sock.listen(8)
        # Polled rather than blocking, because close() has to actually stop the
        # listener: closing the fd under a thread parked in accept() leaves the
        # kernel socket listening until that accept returns, and this file's
        # last test turns on the port genuinely refusing connections.
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.closed = False
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while not self.closed:
            try:
                conn, _ = self.sock.accept()
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        with conn:
            while True:
                try:
                    data = conn.recv(4096)
                except OSError:
                    return
                if not data:
                    return
                conn.sendall(b"echo:" + data)

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.thread.join(timeout=3)
            self.sock.close()


@pytest.fixture
def echo():
    servers = []

    def make(host: str = "127.0.0.1") -> EchoServer:
        s = EchoServer(host)
        servers.append(s)
        return s

    yield make
    for s in servers:
        s.close()


def relay(client, host: str, port: int):
    return client.post("/api/relay", json={"host": host, "port": port}, headers=HDRS)


def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


# ---------------------------------------------------------------------------
# The thing it is for
# ---------------------------------------------------------------------------

@needs_address
def test_loopback_server_answers_on_this_machines_address(client, echo):
    """The tap's whole job: 127.0.0.1:N becomes <lan address>:N, bytes both ways."""
    server = echo()
    r = relay(client, LAN_IP, server.port)
    assert r.status_code == 200, r.text
    assert r.json() == {"state": "relayed"}

    # What the phone's browser does with the rewritten link, in miniature: it
    # connects to a host the dev server never bound.
    with socket.create_connection((LAN_IP, server.port), timeout=5) as c:
        c.sendall(b"hello")
        assert c.recv(4096) == b"echo:hello"
        c.sendall(b"again")
        assert c.recv(4096) == b"echo:again"


@needs_address
def test_relay_is_idempotent(client, echo):
    server = echo()
    assert relay(client, LAN_IP, server.port).json() == {"state": "relayed"}
    first = A.RELAYS[server.port]
    assert relay(client, LAN_IP, server.port).json() == {"state": "relayed"}
    # The same relay, not a second one fighting it for the port.
    assert A.RELAYS[server.port] is first


@needs_address
def test_already_bound_there_is_direct(client, echo):
    """A dev server on 0.0.0.0 needs no relay, and must not have one built over it."""
    server = echo("0.0.0.0")
    r = relay(client, LAN_IP, server.port)
    assert r.status_code == 200
    assert r.json() == {"state": "direct"}
    assert server.port not in A.RELAYS
    # And the printed link still works, because it always did.
    with socket.create_connection((LAN_IP, server.port), timeout=5) as c:
        c.sendall(b"hi")
        assert c.recv(4096) == b"echo:hi"


@needs_address
def test_dead_dev_server_takes_the_relay_down(client, echo):
    """A relay outliving what it relays to would sit on a port nobody may have."""
    server = echo()
    port = server.port
    assert relay(client, LAN_IP, port).json() == {"state": "relayed"}
    server.close()

    # The next connection is the evidence: it cannot be served, so the relay
    # retires rather than keeping the port.
    with socket.create_connection((LAN_IP, port), timeout=5) as c:
        c.settimeout(5)
        try:
            assert c.recv(4096) == b""
        except OSError:
            pass
    assert wait_until(lambda: port not in A.RELAYS), "relay still registered"

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # SO_REUSEADDR only for the TIME_WAIT the relayed connection above left
    # behind; a listener still holding the port would refuse this anyway.
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        assert wait_until(lambda: _binds(s, LAN_IP, port)), "port never came free"
    finally:
        s.close()


def _binds(sock: socket.socket, host: str, port: int) -> bool:
    try:
        sock.bind((host, port))
    except OSError as exc:
        if exc.errno in (errno.EADDRINUSE, errno.EADDRNOTAVAIL):
            return False
        raise
    return True


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------

def test_nothing_listening_is_not_found(client):
    r = relay(client, LAN_IP or "192.0.2.1", free_port())
    assert r.status_code == 404
    assert r.json()["error"] == "not_listening"


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1", "[::1]", "0.0.0.0"])
def test_loopback_and_wildcard_hosts_are_refused(client, echo, host):
    """Binding the address the dev server is on, or every address, is the one
    thing this must never do — so it is refused before anything is probed."""
    server = echo()
    r = relay(client, host, server.port)
    assert r.status_code == 400
    assert r.json()["error"] == "bad_host"
    assert server.port not in A.RELAYS


def test_a_hostname_that_resolves_to_nothing_is_refused(client, echo):
    server = echo()
    r = relay(client, "nothing-resolves-here.invalid", server.port)
    assert r.status_code == 400
    assert r.json()["error"] == "unknown_host"


def test_an_address_this_machine_does_not_have_is_refused(client, echo):
    # 192.0.2.0/24 is the documentation range, so no machine holds one.
    server = echo()
    r = relay(client, "192.0.2.77", server.port)
    assert r.status_code == 400
    assert r.json()["error"] == "not_this_machine"


@pytest.mark.parametrize("port", [0, -1, 65536, "http", None])
def test_a_port_that_is_not_one_is_refused(client, port):
    r = relay(client, LAN_IP or "192.0.2.1", port)
    assert r.status_code == 400
    assert r.json()["error"] == "bad_port"


def test_the_route_is_gated_like_its_neighbours(client):
    r = client.post("/api/relay", json={"host": "laptop.example.net", "port": 3000})
    assert r.status_code == 401


def test_the_capability_map_says_so():
    assert A.server_capabilities()["relay"] is True
