"""In-app browser: the mint, the reverse proxy and the loop guard.

Every test here drives the real thing — a threaded HTTP server on loopback
standing in for the dev server, Jupyter or intranet box the pane exists to
reach — because the properties worth guarding are all about what crosses the
wire: that the framing headers a target sends never reach the iframe, that its
cookies live in this process instead of the browser, that a 3xx comes back
pointing at the proxy, and that a 9 MiB download is not buffered on its way
through.

The hostnames in comments are 127.0.0.1 or example.net ones on purpose: a real
tailnet name in this repo fails the deploy's leak scan.
"""

import gzip
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

httpx = pytest.importorskip("httpx")

from starlette.testclient import TestClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as A  # noqa: E402

TOKEN = "ABCDEFGHIJ"
HDRS = {A.TOKEN_HEADER: TOKEN}
SHELL_ORIGIN = "https://shell.example.net"
PREFIX = "/pockettui"

# A pattern rather than zeros, so a chunk boundary that dropped or reordered
# bytes would show up in the comparison.
BIG = bytes(range(256)) * (9 * 1024 * 1024 // 256)


# ---------------------------------------------------------------------------
# The target
# ---------------------------------------------------------------------------

PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="30; url=/whoami">
<link rel="stylesheet" href="/style.css">
</head><body>
<a id="root" href="/whoami">who</a>
<a id="abs" href="http://127.0.0.1:{port}/style.css">css</a>
<div style="background: url(/img.png)"></div>
</body></html>
"""

SHEET = """body {{ background: url(/img.png); }}
.x {{ background: url("http://127.0.0.1:{port}/abs.png"); }}
@import "/more.css";
"""


class SiteHandler(BaseHTTPRequestHandler):
    """A small site with one of everything the proxy has to handle."""

    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # noqa: A003 — quiet under pytest
        pass

    @property
    def port(self) -> int:
        return self.server.server_address[1]

    def send_body(self, body: bytes, ctype: str, status: int = 200,
                  extra: tuple = ()) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for name, value in extra:
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):  # noqa: N802
        self.do_GET()

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/":
            self.send_body(
                PAGE.format(port=self.port).encode(), "text/html; charset=utf-8",
                extra=(("X-Frame-Options", "DENY"),
                       ("Content-Security-Policy", "frame-ancestors 'self'"),
                       ("Set-Cookie", "sid=1; Path=/")))
        elif path == "/whoami":
            self.send_body(f"cookie={self.headers.get('Cookie', '')}".encode(),
                           "text/plain; charset=utf-8")
        elif path == "/echo-headers":
            seen = "\n".join(f"{k.lower()}: {v}" for k, v in self.headers.items())
            self.send_body(seen.encode(), "text/plain; charset=utf-8")
        elif path == "/redir":
            self.send_body(b"", "text/plain", status=302, extra=(
                ("Location", f"http://127.0.0.1:{self.port}/"),))
        elif path == "/redir-rel":
            self.send_body(b"", "text/plain", status=302,
                           extra=(("Location", "/whoami"),))
        elif path == "/style.css":
            self.send_body(SHEET.format(port=self.port).encode(), "text/css")
        elif path == "/big.bin":
            self.send_body(BIG, "application/octet-stream")
        elif path == "/gz":
            self.send_body(gzip.compress(PAGE.format(port=self.port).encode()),
                           "text/html; charset=utf-8",
                           extra=(("Content-Encoding", "gzip"),))
        elif path == "/slow":
            # Headers first, then a stall: what the rewrite buffer's own
            # deadline is there for, as opposed to the connect timeout.
            body = b"<html><body>late</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            time.sleep(2)
            self.wfile.write(body)
        else:
            self.send_body(b"no", "text/plain", status=404)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        data = self.rfile.read(length) if length else b""
        self.send_body(b"POST:" + data, "text/plain; charset=utf-8")


@pytest.fixture(scope="module")
def site():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), SiteHandler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=3)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    # Context-managed like the relay tests': the AsyncClient a token owns is
    # created on the portal's loop, so every request in a test has to share one
    # portal or the second would find a client bound to a loop that is gone.
    monkeypatch.setattr(A, "AUTH_TOKEN", TOKEN)
    with TestClient(A.app) as c:
        yield c


@pytest.fixture(autouse=True)
def fresh_limits(monkeypatch):
    monkeypatch.setattr(A, "LIMITER", A.AuthLimiter())
    monkeypatch.setattr(A, "RATE", A.RateLimiter())


@pytest.fixture(autouse=True)
def fresh_browse():
    """No token outlives its test: its client belongs to that test's loop."""
    A.BROWSE.clear()
    yield
    A.BROWSE.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def mint(client, url, prefix=PREFIX, origin=SHELL_ORIGIN):
    return client.post("/api/browse",
                       json={"url": url, "prefix": prefix, "origin": origin},
                       headers=HDRS)


def opened(client, site, path="/", **kw):
    """Mint a token and fetch one path of the fixture site through the proxy."""
    r = mint(client, f"http://127.0.0.1:{site}{path}", **kw)
    assert r.status_code == 200, r.text
    body = r.json()
    # The shell joins the path-only answer to the origin it reaches us on; the
    # TestClient is that origin here, so the path goes straight back.
    return body, client.get(body["url"][len(kw.get("prefix", PREFIX)):],
                            follow_redirects=False)


def base_of(token: str, site: int, prefix: str = PREFIX) -> str:
    return f"{prefix}/b/{token}/h/127.0.0.1:{site}"


def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ---------------------------------------------------------------------------
# Minting
# ---------------------------------------------------------------------------

def test_mint_answers_with_a_token_and_a_path(client, site):
    r = mint(client, f"http://127.0.0.1:{site}/dir/page?q=1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["prefix"] == PREFIX
    assert len(body["token"]) >= 24
    assert body["url"] == f"{base_of(body['token'], site)}/dir/page?q=1"
    # Sliding window, not a fixed lifetime, so the answer is roughly now + 12 h.
    assert A.BROWSE_IDLE - 60 < body["expires"] - time.time() <= A.BROWSE_IDLE


def test_mint_is_idempotent(client, site):
    """The pane persists proxied URLs; a new token per call would 403 them all."""
    first = mint(client, f"http://127.0.0.1:{site}/").json()
    second = mint(client, f"http://127.0.0.1:{site}/other").json()
    assert first["token"] == second["token"]
    assert len(A.BROWSE) == 1


def test_mint_takes_the_prefix_the_shell_reports(client, site):
    body = mint(client, f"http://127.0.0.1:{site}/", prefix="").json()
    assert body["url"] == f"/b/{body['token']}/h/127.0.0.1:{site}/"


@pytest.mark.parametrize("url", ["", "ftp://example.net/x", "http:///nohost",
                                 "not a url", "/relative"])
def test_a_url_that_is_not_one_is_refused(client, url):
    r = mint(client, url)
    assert r.status_code == 400
    assert r.json()["error"] == "bad_url"


@pytest.mark.parametrize("prefix", ["pockettui", "/pocket tui", "/a/", "/a?b"])
def test_a_prefix_that_is_not_a_path_is_refused(client, prefix):
    r = mint(client, "http://127.0.0.1:1/", prefix=prefix)
    assert r.status_code == 400
    assert r.json()["error"] == "bad_prefix"


@pytest.mark.parametrize("origin", ["", "shell.example.net",
                                    "https://shell.example.net/app"])
def test_an_origin_that_is_not_one_is_refused(client, origin):
    r = mint(client, "http://127.0.0.1:1/", origin=origin)
    assert r.status_code == 400
    assert r.json()["error"] == "bad_origin"


def test_this_server_is_refused(client, site, monkeypatch):
    """Proxying ourselves through ourselves is a request that never returns."""
    monkeypatch.setattr(A, "LISTEN_PORT", site)
    r = mint(client, f"http://127.0.0.1:{site}/")
    assert r.status_code == 400
    assert r.json()["error"] == "loop"
    # The same host on any other port is somebody else's server, and allowed.
    assert mint(client, f"http://127.0.0.1:{site + 1}/").status_code == 200


def test_the_mint_is_gated_like_its_neighbours(client, site):
    r = client.post("/api/browse", json={"url": f"http://127.0.0.1:{site}/",
                                         "prefix": PREFIX,
                                         "origin": SHELL_ORIGIN})
    assert r.status_code == 401


def test_the_mint_is_rate_limited(client, site):
    for _ in range(A.RATE_BROWSE):
        assert mint(client, f"http://127.0.0.1:{site}/").status_code == 200
    r = mint(client, f"http://127.0.0.1:{site}/")
    assert r.status_code == 429
    assert r.json()["error"] == "rate_limited"


def test_the_capability_map_says_so():
    assert A.server_capabilities()["browse"] is True


# ---------------------------------------------------------------------------
# Proxying a page
# ---------------------------------------------------------------------------

def test_the_page_comes_back_rewritten_and_unframed(client, site):
    body, r = opened(client, site)
    assert r.status_code == 200
    text = r.text
    base = base_of(body["token"], site)

    # The shim is there, and the links point back through the proxy.
    assert "pockettui-nav" in text
    assert f'href="{base}/whoami"' in text
    assert f'href="{base}/style.css"' in text
    assert f"url({base}/img.png)" in text
    assert f"url={base}/whoami" in text

    # None of what the target sent to keep itself out of a frame.
    assert "x-frame-options" not in r.headers
    assert "frame-ancestors" not in r.headers.get("content-security-policy", "")
    # And the sandbox that puts the page on an opaque origin instead.
    assert r.headers["content-security-policy"] == A.BROWSE_SANDBOX
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["content-type"] == "text/html; charset=utf-8"


def test_the_target_date_and_server_lines_do_not_reach_the_frame(client, site):
    """Whoever answered is this server, and uvicorn says so once already."""
    _, r = opened(client, site)
    assert r.status_code == 200
    assert "date" not in r.headers
    assert "server" not in r.headers


def test_the_proxy_needs_no_header_because_the_token_is_the_credential(client, site):
    """An iframe cannot send one, which is why /b/ is outside the gate."""
    _, r = opened(client, site)
    assert r.status_code == 200
    assert A.TOKEN_HEADER not in {k.lower() for k in r.request.headers}


def test_cookies_live_in_the_jar_and_not_in_the_browser(client, site):
    body, first = opened(client, site)
    assert "set-cookie" not in first.headers
    assert not client.cookies

    second = client.get(f"{base_of(body['token'], site)}/whoami"[len(PREFIX):])
    assert second.status_code == 200
    assert "sid=1" in second.text


def test_a_redirect_comes_back_pointing_at_the_proxy(client, site):
    body, _ = opened(client, site)
    base = base_of(body["token"], site)
    for path, expected in (("/redir", f"{base}/"), ("/redir-rel", f"{base}/whoami")):
        r = client.get(f"{base}{path}"[len(PREFIX):], follow_redirects=False)
        assert r.status_code == 302, path
        assert r.headers["location"] == expected


def test_a_stylesheet_is_rewritten(client, site):
    body, _ = opened(client, site)
    base = base_of(body["token"], site)
    r = client.get(f"{base}/style.css"[len(PREFIX):])
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/css")
    assert f"url({base}/img.png)" in r.text
    assert f'url("{base}/abs.png")' in r.text
    assert f'@import "{base}/more.css"' in r.text


def test_a_gzipped_document_is_inflated_before_rewriting(client, site):
    """Some targets answer from a cache that only holds the compressed copy."""
    body, _ = opened(client, site)
    base = base_of(body["token"], site)
    r = client.get(f"{base}/gz"[len(PREFIX):])
    assert r.status_code == 200
    assert "content-encoding" not in r.headers
    assert f'href="{base}/whoami"' in r.text
    assert "pockettui-nav" in r.text


def test_a_big_download_is_streamed_untouched(client, site):
    body, _ = opened(client, site)
    r = client.get(f"{base_of(body['token'], site)}/big.bin"[len(PREFIX):])
    assert r.status_code == 200
    assert r.content == BIG
    assert r.headers["content-length"] == str(len(BIG))
    assert "pockettui-nav" not in r.text[:4096]


def test_a_document_past_the_buffer_cap_goes_out_unrewritten(client, site,
                                                             monkeypatch):
    """A page nobody would want held in memory still has to render."""
    body, _ = opened(client, site)
    monkeypatch.setattr(A, "BROWSE_REWRITE_CAP", 16)
    r = client.get(f"{base_of(body['token'], site)}/"[len(PREFIX):])
    assert r.status_code == 200
    assert "pockettui-nav" not in r.text
    assert 'href="/whoami"' in r.text


def test_a_post_carries_its_method_and_body(client, site):
    body, _ = opened(client, site)
    r = client.post(f"{base_of(body['token'], site)}/post"[len(PREFIX):],
                    content=b"hello")
    assert r.status_code == 200
    assert r.text == "POST:hello"


def test_the_target_sees_its_own_origin(client, site):
    body, _ = opened(client, site)
    base = base_of(body["token"], site)
    r = client.get(f"{base}/echo-headers"[len(PREFIX):], headers={
        "Origin": SHELL_ORIGIN,
        "Referer": f"http://testserver{base}/dir/page",
        "Cookie": "forged=1",
    })
    assert f"origin: http://127.0.0.1:{site}" in r.text
    assert f"referer: http://127.0.0.1:{site}/dir/page" in r.text
    assert "forged=1" not in r.text
    assert f"host: 127.0.0.1:{site}" in r.text


def test_a_head_request_answers_without_a_body(client, site):
    body, _ = opened(client, site)
    r = client.head(f"{base_of(body['token'], site)}/"[len(PREFIX):])
    assert r.status_code == 200
    assert r.content == b""
    assert "x-frame-options" not in r.headers


# ---------------------------------------------------------------------------
# Refusals and failures
# ---------------------------------------------------------------------------

def test_an_unknown_token_is_an_expired_page(client, site):
    r = client.get(f"/b/nosuchtoken/h/127.0.0.1:{site}/")
    assert r.status_code == 403
    assert '"expired"' in r.text
    assert "pockettui-browse-error" in r.text
    assert r.headers["content-security-policy"] == A.BROWSE_SANDBOX


def test_an_expired_record_is_retired_by_the_request_that_finds_it(client, site):
    body, _ = opened(client, site)
    rec = A.BROWSE[body["token"]]
    rec.created = rec.last_used = time.time() - A.BROWSE_IDLE - 1

    r = client.get(f"{base_of(body['token'], site)}/"[len(PREFIX):])
    assert r.status_code == 403
    assert '"expired"' in r.text
    assert A.BROWSE == {}


@pytest.mark.parametrize("target", ["x/127.0.0.1:80", "h/127.0.0.1", "h/127.0.0.1:x"])
def test_a_target_that_is_not_one_is_refused(client, site, target):
    body, _ = opened(client, site)
    r = client.get(f"/b/{body['token']}/{target}/")
    assert r.status_code == 400


def test_this_server_is_refused_by_the_proxy_too(client, site, monkeypatch):
    body, _ = opened(client, site)
    monkeypatch.setattr(A, "LISTEN_PORT", site)
    r = client.get(f"{base_of(body['token'], site)}/"[len(PREFIX):])
    assert r.status_code == 400
    assert '"loop"' in r.text


def test_nothing_listening_is_a_refused_page(client, site):
    body, _ = opened(client, site)
    dead = free_port()
    r = client.get(f"/b/{body['token']}/h/127.0.0.1:{dead}/")
    assert r.status_code == 502
    assert '"refused"' in r.text
    assert str(dead) in r.text


def test_a_name_that_does_not_resolve_is_an_unknown_host_page(client, site):
    body, _ = opened(client, site)
    r = client.get(f"/b/{body['token']}/h/nothing-resolves-here.invalid:80/")
    assert r.status_code == 502
    assert '"unknown_host"' in r.text


def test_a_target_that_stops_talking_is_a_timeout_page(client, site, monkeypatch):
    body, _ = opened(client, site)
    monkeypatch.setattr(A, "BROWSE_REWRITE_WAIT", 0.3)
    r = client.get(f"{base_of(body['token'], site)}/slow"[len(PREFIX):])
    assert r.status_code == 504
    assert '"timeout"' in r.text


# ---------------------------------------------------------------------------
# The WebSocket target
# ---------------------------------------------------------------------------
# A Vite dev server's HMR channel and a Jupyter kernel are both WebSockets, so
# the pane is worth nothing if the upgrade does not survive the proxy. This
# stands in for one: an echo server on loopback that also records what each
# handshake carried, which is how the header tests below see the target's side.

class Echo:
    """The echo server's port, and the handshakes it has seen."""

    def __init__(self, port: int, seen: list):
        self.port = port
        self.seen = seen


@pytest.fixture(scope="module")
def echo():
    import asyncio

    from websockets.asyncio.server import serve

    seen: list = []
    ready = threading.Event()
    box: dict = {}

    def select(conn, offers):
        # One path that negotiates, so a test can tell a subprotocol carried
        # through the proxy from one silently dropped on the way.
        if conn.request.path.split("?", 1)[0] == "/proto" and "chat" in offers:
            return "chat"
        return None

    async def handler(conn):
        seen.append({
            "path": conn.request.path,
            "origin": conn.request.headers.get("Origin"),
            "cookie": conn.request.headers.get("Cookie"),
            "agent": conn.request.headers.get("User-Agent"),
            "subprotocol": conn.subprotocol,
        })
        async for msg in conn:
            await conn.send(msg)

    async def main():
        async with serve(handler, "127.0.0.1", 0,
                         select_subprotocol=select) as srv:
            box["port"] = srv.sockets[0].getsockname()[1]
            box["loop"] = asyncio.get_running_loop()
            box["stop"] = asyncio.Event()
            ready.set()
            await box["stop"].wait()

    thread = threading.Thread(target=lambda: asyncio.run(main()), daemon=True)
    thread.start()
    assert ready.wait(5), "the echo server never came up"
    yield Echo(box["port"], seen)
    box["loop"].call_soon_threadsafe(box["stop"].set)
    thread.join(timeout=3)


def ws_open(client, port, path="/echo", **kw):
    """Mint a token and upgrade one path of the echo server through the proxy."""
    tok = mint(client, f"http://127.0.0.1:{port}/").json()["token"]
    return client.websocket_connect(f"/b/{tok}/h/127.0.0.1:{port}{path}", **kw)


# ---------------------------------------------------------------------------
# Proxying a socket
# ---------------------------------------------------------------------------

def test_a_socket_carries_text_both_ways(client, echo):
    with ws_open(client, echo.port) as ws:
        ws.send_text("hello")
        assert ws.receive_text() == "hello"


def test_a_socket_carries_binary_frames(client, echo):
    """Kernel protocols are binary, so a text-only pump would be no use."""
    payload = bytes(range(256))
    with ws_open(client, echo.port) as ws:
        ws.send_bytes(payload)
        assert ws.receive_bytes() == payload


def test_a_subprotocol_is_negotiated_end_to_end(client, echo):
    with ws_open(client, echo.port, "/proto",
                 subprotocols=["chat", "other"]) as ws:
        assert ws.accepted_subprotocol == "chat"
        ws.send_text("x")
        assert ws.receive_text() == "x"
    assert echo.seen[-1]["subprotocol"] == "chat"


def test_the_query_string_reaches_the_target(client, echo):
    with ws_open(client, echo.port, "/echo?a=1&b=two") as ws:
        ws.send_text("x")
        assert ws.receive_text() == "x"
    assert echo.seen[-1]["path"] == "/echo?a=1&b=two"


def test_the_target_sees_its_own_origin_and_the_jar(client, site, echo):
    """The handshake carries the session the HTTP side established.

    The cookie was set by the fixture site on another port, and arrives here
    because cookies are scoped to a host and not to a port — which is also why
    one browse token can hold a dev server's session across its whole box.
    """
    body, _ = opened(client, site)
    tok = body["token"]
    with client.websocket_connect(f"/b/{tok}/h/127.0.0.1:{echo.port}/echo") as ws:
        ws.send_text("x")
        assert ws.receive_text() == "x"
    assert echo.seen[-1]["origin"] == f"http://127.0.0.1:{echo.port}"
    assert echo.seen[-1]["cookie"] == "sid=1"
    assert echo.seen[-1]["agent"] == "testclient"


# ---------------------------------------------------------------------------
# Socket refusals
# ---------------------------------------------------------------------------
# Every one of these is accepted and then closed, rather than refused at the
# handshake: a close before the accept reaches a browser as a bare failure,
# and the close code is the only thing the pane can report.

def closed_with(session) -> int:
    """The close code the proxy answered one doomed upgrade with."""
    with pytest.raises(WebSocketDisconnect) as exc:
        with session as ws:
            ws.receive_text()
    return exc.value.code


def test_an_unknown_token_closes_the_socket_unauthorised(client, echo):
    assert closed_with(client.websocket_connect(
        f"/b/nosuchtoken/h/127.0.0.1:{echo.port}/echo")) == 4401


def test_an_expired_record_is_retired_by_the_upgrade_that_finds_it(client, echo):
    tok = mint(client, f"http://127.0.0.1:{echo.port}/").json()["token"]
    rec = A.BROWSE[tok]
    rec.created = rec.last_used = time.time() - A.BROWSE_IDLE - 1
    assert closed_with(client.websocket_connect(
        f"/b/{tok}/h/127.0.0.1:{echo.port}/echo")) == 4401
    assert A.BROWSE == {}


@pytest.mark.parametrize("target", ["x/127.0.0.1:80", "h/127.0.0.1",
                                    "h/127.0.0.1:x"])
def test_a_target_that_is_not_one_closes_the_socket(client, echo, target):
    tok = mint(client, f"http://127.0.0.1:{echo.port}/").json()["token"]
    assert closed_with(client.websocket_connect(f"/b/{tok}/{target}/echo")) == 4400


def test_this_server_is_refused_by_the_socket_route_too(client, echo, monkeypatch):
    tok = mint(client, f"http://127.0.0.1:{echo.port}/").json()["token"]
    monkeypatch.setattr(A, "LISTEN_PORT", echo.port)
    assert closed_with(client.websocket_connect(
        f"/b/{tok}/h/127.0.0.1:{echo.port}/echo")) == 4400


def test_nothing_listening_closes_the_socket_as_a_bad_gateway(client, echo):
    tok = mint(client, f"http://127.0.0.1:{echo.port}/").json()["token"]
    dead = free_port()
    assert closed_with(client.websocket_connect(
        f"/b/{tok}/h/127.0.0.1:{dead}/echo")) == 4502


def test_the_bare_origin_form_upgrades_too(client, echo):
    """A socket opened at the target's root, with no path segment at all."""
    tok = mint(client, f"http://127.0.0.1:{echo.port}/").json()["token"]
    with client.websocket_connect(f"/b/{tok}/h/127.0.0.1:{echo.port}") as ws:
        ws.send_text("x")
        assert ws.receive_text() == "x"
    assert echo.seen[-1]["path"] == "/"
