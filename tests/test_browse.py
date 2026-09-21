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
import json
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

httpx = pytest.importorskip("httpx")

from starlette.requests import Request  # noqa: E402
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
    """No token outlives its test: its client belongs to that test's loop.

    The client goes with them, since both flavours share one (BROWSE_CLIENT)
    and a pool made on the last test's portal is bound to a loop that is gone.
    """
    A.BROWSE.clear()
    A.BROWSE_CLIENT = None
    yield
    A.BROWSE.clear()
    A.BROWSE_CLIENT = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def mint(client, url, prefix=PREFIX, origin=SHELL_ORIGIN):
    return client.post("/api/browse",
                       json={"url": url, "prefix": prefix, "origin": origin},
                       headers=HDRS)


def mint_tab(client, url, origin=SHELL_ORIGIN, prefix=PREFIX):
    """The other flavour's mint, with the Origin header a browser puts on a POST."""
    headers = dict(HDRS)
    if origin is not None:
        headers["Origin"] = origin
    return client.post("/api/browse",
                       json={"url": url, "prefix": prefix,
                             "origin": SHELL_ORIGIN, "mode": "tab"},
                       headers=headers)


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
    caps = A.server_capabilities()
    assert caps["browse"] is True
    # Independent of the same-origin gate, which is per request: the shell asks
    # for the mode and is refused for its own address, not for its age.
    assert caps["browse_tab"] is True


# ---------------------------------------------------------------------------
# The tab flavour
# ---------------------------------------------------------------------------
# The pane's frame is sandboxed and stays that way. A page opened as a tab in
# the laptop's own browser is not, which is the only way an app written to be
# the top window runs at all — top is unforgeable and an opaque origin makes
# even same-host frames strangers. Two tokens, one cookie jar, and the
# unsandboxed one only for a shell this server did not serve itself.

def test_the_tab_mint_is_a_second_token(client, site):
    pane = mint(client, f"http://127.0.0.1:{site}/").json()
    tab = mint_tab(client, f"http://127.0.0.1:{site}/").json()
    assert tab["token"] != pane["token"]
    assert tab["url"] == f"{base_of(tab['token'], site)}/"
    assert len(A.BROWSE) == 2
    assert {r.sandbox for r in A.BROWSE.values()} == {True, False}


def test_the_tab_mint_is_idempotent_within_its_flavour(client, site):
    first = mint_tab(client, f"http://127.0.0.1:{site}/").json()
    second = mint_tab(client, f"http://127.0.0.1:{site}/other").json()
    assert first["token"] == second["token"]
    assert len(A.BROWSE) == 1


@pytest.mark.parametrize("origin", ["http://testserver", "https://testserver",
                                    "http://testserver:80", None,
                                    "testserver", "about:blank"])
def test_a_shell_this_server_serves_itself_gets_no_tab_token(client, site, origin):
    """An unsandboxed page would land on the origin holding the pairing token.

    The TestClient's own address is the Host here, so an Origin naming it is
    the laptop-only install: shell and backend on one origin. The https
    spelling is refused with it, because `tailscale serve` terminates TLS and
    forwards in the clear — the scheme this server sees is not the browser's,
    so a host that matches with a port missing on either side is one origin.
    A request with no Origin at all is not a browser's, and there is nothing
    to compare.
    """
    r = mint_tab(client, f"http://127.0.0.1:{site}/", origin=origin)
    assert r.status_code == 403
    assert r.json()["error"] == "same_origin"
    assert not A.BROWSE


def test_a_shell_on_another_port_of_this_host_is_another_origin(client, site):
    """Two ports, both written down, are two origins and storage agrees."""
    r = client.post("/api/browse",
                    json={"url": f"http://127.0.0.1:{site}/", "prefix": PREFIX,
                          "origin": SHELL_ORIGIN, "mode": "tab"},
                    headers={**HDRS, "Host": "testserver:5560",
                             "Origin": "http://testserver:8080"})
    assert r.status_code == 200


def test_the_gate_compares_against_the_address_the_browser_used(client, site):
    """Behind a proxy that rewrites Host, only X-Forwarded-Host still names it.

    Host here is the backend address the front end forwarded to; the browser
    asked for box.example.net, which is also where the shell came from. Reading
    Host would compare the shell's origin against an address nobody typed and
    hand out the flavour exactly where the two really are one origin.
    """
    r = client.post("/api/browse",
                    json={"url": f"http://127.0.0.1:{site}/", "prefix": PREFIX,
                          "origin": SHELL_ORIGIN, "mode": "tab"},
                    headers={**HDRS, "Host": "127.0.0.1:5599",
                             "X-Forwarded-Host": "box.example.net, edge.example.net",
                             "Origin": "https://box.example.net"})
    assert r.status_code == 403
    assert r.json()["error"] == "same_origin"
    # A forwarded host that is not the shell's is another origin, as before.
    r = client.post("/api/browse",
                    json={"url": f"http://127.0.0.1:{site}/", "prefix": PREFIX,
                          "origin": SHELL_ORIGIN, "mode": "tab"},
                    headers={**HDRS, "Host": "127.0.0.1:5599",
                             "X-Forwarded-Host": "box.example.net",
                             "Origin": SHELL_ORIGIN})
    assert r.status_code == 200


def test_a_port_is_the_number_it_spells_not_the_way_it_is_written(client, site):
    """:0443 and :443 are one port, so the two sides are one origin."""
    r = client.post("/api/browse",
                    json={"url": f"http://127.0.0.1:{site}/", "prefix": PREFIX,
                          "origin": SHELL_ORIGIN, "mode": "tab"},
                    headers={**HDRS, "Host": "box.example.net:0443",
                             "Origin": "https://box.example.net:443"})
    assert r.status_code == 403
    assert r.json()["error"] == "same_origin"


def test_the_pane_flavour_is_never_gated(client, site):
    """It keeps its sandbox on every install, so it has nothing to be gated on."""
    r = client.post("/api/browse",
                    json={"url": f"http://127.0.0.1:{site}/", "prefix": PREFIX,
                          "origin": SHELL_ORIGIN},
                    headers={**HDRS, "Origin": "http://testserver"})
    assert r.status_code == 200


def test_a_tab_page_is_served_without_the_sandbox(client, site):
    tok = mint_tab(client, f"http://127.0.0.1:{site}/").json()["token"]
    r = client.get(f"{base_of(tok, site)}/"[len(PREFIX):])
    assert r.status_code == 200
    # The one header that goes, and it is the one the flavour exists for.
    assert "content-security-policy" not in r.headers
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "no-store"
    assert "x-frame-options" not in r.headers
    # Same rewriter, same shim, told which kind of document it is in.
    assert f'href="{base_of(tok, site)}/whoami"' in r.text
    assert '"sandbox": false' in r.text

    pane = mint(client, f"http://127.0.0.1:{site}/").json()["token"]
    p = client.get(f"{base_of(pane, site)}/"[len(PREFIX):])
    assert p.headers["content-security-policy"] == A.BROWSE_SANDBOX
    assert '"sandbox": true' in p.text


def test_both_flavours_carry_the_same_session(client, site):
    """One jar: a login done in the pane is one the tab already has."""
    pane = mint(client, f"http://127.0.0.1:{site}/").json()["token"]
    tab = mint_tab(client, f"http://127.0.0.1:{site}/").json()["token"]
    # The fixture site sets its cookie on "/", and only there.
    assert client.get(f"{base_of(pane, site)}/"[len(PREFIX):]).status_code == 200
    assert "sid=1" in client.get(f"{base_of(tab, site)}/whoami"[len(PREFIX):]).text
    assert A.BROWSE[pane].client is A.BROWSE[tab].client


def test_retiring_one_flavour_leaves_the_other_its_client(client, site):
    pane = mint(client, f"http://127.0.0.1:{site}/").json()["token"]
    tab = mint_tab(client, f"http://127.0.0.1:{site}/").json()["token"]
    A.BROWSE[tab].last_used = 0          # idled out, as a long-closed pane would
    assert client.get(f"{base_of(tab, site)}/"[len(PREFIX):],
                      follow_redirects=False).status_code == 302
    assert tab not in A.BROWSE
    # The pane's requests still go through, which they would not on a closed client.
    assert client.get(f"{base_of(pane, site)}/whoami"[len(PREFIX):]).status_code == 200


def test_a_dead_token_heals_to_the_pane_flavour(client, site):
    """A restart forgets which flavour an address was for, so it heals to the
    safe one: the sandboxed page is the one that can be served to anybody."""
    pane = mint(client, f"http://127.0.0.1:{site}/", prefix="").json()["token"]
    tab = mint_tab(client, f"http://127.0.0.1:{site}/", prefix="").json()["token"]
    r = client.get(f"/b/deadtoken/h/127.0.0.1:{site}/whoami",
                   follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == f"/b/{pane}/h/127.0.0.1:{site}/whoami"
    assert tab not in r.headers["location"]


def test_a_dead_token_is_never_healed_into_the_tab_flavour(client, site):
    """With no sandboxed token live there is nothing safe to point at.

    An address whose token is gone says nothing about the flavour it was minted
    for, and the pane is where a healed one is likeliest to land — so healing
    to the tab token would serve a page into an iframe without the sandbox, on
    this server's own origin, on the strength of an address nobody can vouch
    for. The expired page instead, which the pane answers by re-minting.
    """
    tab = mint_tab(client, f"http://127.0.0.1:{site}/", prefix="").json()["token"]
    r = client.get(f"/b/deadtoken/h/127.0.0.1:{site}/whoami",
                   follow_redirects=False)
    assert r.status_code == 403
    assert "location" not in r.headers
    assert tab not in r.text


# ---------------------------------------------------------------------------
# The hop a tab starts on
# ---------------------------------------------------------------------------
# A tab-flavour page runs on this server's real origin, and a browser that once
# opened the shell straight from this address left the pairing token in that
# origin's localStorage. So no tab starts on a proxied page: it starts on
# /enter, which wipes the storage and redirects onward.

def enter_url(tok: str, to: str, prefix: str = PREFIX) -> str:
    return f"{prefix}/b/{tok}/enter?to={urllib.parse.quote(to, safe='')}"


def test_the_enter_hop_wipes_this_origins_storage_and_redirects(client, site):
    tok = mint_tab(client, f"http://127.0.0.1:{site}/").json()["token"]
    to = f"/h/127.0.0.1:{site}/dir/page?q=1#frag"
    r = client.get(enter_url(tok, to)[len(PREFIX):], follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == f"{PREFIX}/b/{tok}{to}"
    # Applied before the redirect is followed, which is the whole point of the
    # hop: nothing of the shell's is left on this origin by the time the
    # unsandboxed document runs. Chromium and Firefox; Safari ignores it.
    assert r.headers["clear-site-data"] == '"storage"'
    assert r.headers["cache-control"] == "no-store"


def test_the_enter_hop_is_refused_to_the_pane_token(client, site):
    """The pane has no use for it, and a sandboxed page reached through it
    would be one this route treats as a tab about to start."""
    tok = mint(client, f"http://127.0.0.1:{site}/").json()["token"]
    r = client.get(enter_url(tok, f"/h/127.0.0.1:{site}/")[len(PREFIX):],
                   follow_redirects=False)
    assert r.status_code == 403
    assert "clear-site-data" not in r.headers


def test_the_enter_hop_is_refused_once_its_token_has_gone(client, site):
    tok = mint_tab(client, f"http://127.0.0.1:{site}/").json()["token"]
    A.BROWSE[tok].last_used = 0
    r = client.get(enter_url(tok, f"/h/127.0.0.1:{site}/")[len(PREFIX):],
                   follow_redirects=False)
    assert r.status_code == 403


@pytest.mark.parametrize("to", [
    "",                                    # nowhere at all
    "/whoami",                             # not this proxy's shape
    "/x/127.0.0.1:8080/",                  # no such scheme segment
    "/h/127.0.0.1/",                       # no port written
    "/h/127.0.0.1:notaport/",              # nor a number
    "//evil.example.net/",                 # a host where the scheme goes
    "http://evil.example.net/",            # an address of its own
    "/h/127.0.0.1:8080/a b",               # a space the Location cannot carry
    "/h/127.0.0.1:8080/x\r\nX-Evil: 1",    # a second header
])
def test_the_enter_hop_goes_nowhere_but_this_proxy(client, site, to):
    tok = mint_tab(client, f"http://127.0.0.1:{site}/").json()["token"]
    r = client.get(f"/b/{tok}/enter?to={urllib.parse.quote(to, safe='')}",
                   follow_redirects=False)
    assert r.status_code == 400
    assert "location" not in r.headers


# ---------------------------------------------------------------------------
# The page a tab with nothing in it starts on
# ---------------------------------------------------------------------------
# A tab has no chrome of ours, and the browser's own address bar types into the
# laptop rather than into the computer. /start is the one address bar a tab has
# that goes through the proxy — this server's own page, on this server's
# origin, carrying the same wipe /enter does because it is the entry itself.

def start_url(tok: str, prefix: str = PREFIX) -> str:
    return f"{prefix}/b/{tok}/start"


def test_the_start_page_is_this_servers_own_page_unsandboxed(client, site):
    tok = mint_tab(client, f"http://127.0.0.1:{site}/").json()["token"]
    r = client.get(start_url(tok)[len(PREFIX):])
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert r.headers["clear-site-data"] == '"storage"'
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-content-type-options"] == "nosniff"
    # The tab flavour's whole point: no opaque origin, so the page it sends the
    # browser to is the top window and its frames are its own to script.
    assert "content-security-policy" not in r.headers
    assert 'id="addr"' in r.text and '<form id="go">' in r.text
    assert socket.gethostname() in r.text


def test_the_start_page_is_refused_to_the_pane_token(client, site):
    """The pane has an address bar of its own, and a sandboxed tab is the one
    thing the flavour exists to avoid."""
    tok = mint(client, f"http://127.0.0.1:{site}/").json()["token"]
    r = client.get(start_url(tok)[len(PREFIX):])
    assert r.status_code == 403
    assert "clear-site-data" not in r.headers


def test_the_start_page_is_refused_once_its_token_has_gone(client, site):
    tok = mint_tab(client, f"http://127.0.0.1:{site}/").json()["token"]
    A.BROWSE[tok].last_used = 0
    assert client.get(start_url(tok)[len(PREFIX):]).status_code == 403
    assert client.get(start_url("nosuchtoken")[len(PREFIX):]).status_code == 403


def test_the_start_page_lists_the_bookmarks_as_proxied_links(client, site, marks):
    """Written by this server, not built by its script: a start page with
    JavaScript off still reaches everything the computer has kept."""
    client.put("/api/browse/bookmarks", json={"bookmarks": [ONE]}, headers=HDRS)
    tok = mint_tab(client, f"http://127.0.0.1:{site}/").json()["token"]
    r = client.get(start_url(tok)[len(PREFIX):])
    assert f'href="{PREFIX}/b/{tok}/s/wiki.example.net:443/start"' in r.text
    assert ">Start<" in r.text
    assert ">wiki.example.net<" in r.text


def test_a_bookmark_title_cannot_write_the_start_page(client, site, marks):
    client.put("/api/browse/bookmarks", headers=HDRS, json={"bookmarks": [
        dict(ONE, title='</a><img src=x onerror=alert(1)>')]})
    tok = mint_tab(client, f"http://127.0.0.1:{site}/").json()["token"]
    r = client.get(start_url(tok)[len(PREFIX):])
    assert "<img src=x" not in r.text
    assert "&lt;/a&gt;&lt;img src=x onerror=alert(1)&gt;" in r.text


# What the field does with an address, run as the page runs it. The guess is a
# port of the shell's (isPrivateHost in 08-links.js), and it is the whole of
# what makes a scheme-less address work in a tab, so it is exercised rather
# than read: a private host is http, everything else https, and the rest of the
# URL survives either way.
START_CASES = [
    ("localhost:3000", "/h/localhost:3000/"),
    ("10.0.0.5", "/h/10.0.0.5:80/"),
    ("box.local", "/h/box.local:80/"),
    ("portal.example.net", "/s/portal.example.net:443/"),
    ("https://x.example.net/a?b", "/s/x.example.net:443/a?b"),
    ("x.example.net:8443", "/s/x.example.net:8443/"),
    ("   ", ""),
]


def test_the_start_pages_field_guesses_a_scheme_the_way_the_pane_does(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH")
    harness = """
const typed = [];
let where = "";
globalThis.window = globalThis;
globalThis.location = { get href() { return where },
                        set href(v) { where = v } };
const input = { value: "" };
const on = {};
globalThis.document = {
  currentScript: { dataset: { cfg: CFG } },
  getElementById(id) {
    return id === "addr" ? input
                         : { addEventListener(t, fn) { on[t] = fn } };
  },
};
SCRIPT
for (const c of CASES) {
  where = "";
  input.value = c;
  on.submit({ preventDefault() {} });
  typed.push(where);
}
console.log(JSON.stringify(typed));
"""
    f = tmp_path / "start.mjs"
    # the script goes in last, for the reason the shim harnesses give: its own
    # text must not be searched for the placeholders after it.
    f.write_text(harness
                 .replace("CFG", json.dumps(json.dumps({"prefix": PREFIX,
                                                        "tok": "TOKEN123"})))
                 .replace("CASES", json.dumps([c for c, _ in START_CASES]))
                 .replace("SCRIPT", A.BROWSE_START_JS), encoding="utf-8")
    out = subprocess.run([node, str(f)], check=True, capture_output=True)
    got = json.loads(out.stdout.decode("utf-8"))
    want = [f"{PREFIX}/b/TOKEN123{tail}" if tail else "" for _, tail in START_CASES]
    assert got == want


# ---------------------------------------------------------------------------
# What a proxied page may ask this server for
# ---------------------------------------------------------------------------
# Nothing. A tab-flavour page runs on this origin, so a call from it would
# carry whatever the browser attaches to a same-origin request — but the
# browser also writes the Referer, and a page cannot forge that.

def test_an_api_call_from_a_proxied_page_is_refused(client):
    r = client.get("/api/version", headers={
        **HDRS,
        "Referer": "https://box.example.net/b/sometoken/s/intranet.example.net:443/app"})
    assert r.status_code == 403
    assert r.json()["error"] == "proxied_origin"


def test_the_gate_reads_the_path_whatever_prefix_it_sits_under(client):
    """The proxy is mounted under /pockettui behind `tailscale serve`, and the
    token segment is the page's, not one this server can enumerate."""
    r = client.get("/api/version", headers={
        **HDRS, "Referer": "https://box.example.net/pockettui/b/xyz/h/127.0.0.1:3000/"})
    assert r.status_code == 403


def test_the_shells_own_calls_are_untouched(client):
    """Its Referer is its own path, or — cross-origin — nothing but the origin.
    A WebSocket handshake sends none at all, which is why absence has to pass.
    """
    assert client.get("/api/version",
                      headers={**HDRS, "Referer": SHELL_ORIGIN + "/"}).status_code == 200
    assert client.get("/api/version", headers=HDRS).status_code == 200


def test_the_socket_route_takes_the_tab_token_too(client, echo):
    """Both flavours are good on every /b/ route: a tab runs the same apps."""
    tok = mint_tab(client, f"http://127.0.0.1:{echo.port}/").json()["token"]
    with client.websocket_connect(f"/b/{tok}/h/127.0.0.1:{echo.port}/echo") as ws:
        ws.send_text("hi")
        assert ws.receive_text() == "hi"


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


# ---------------------------------------------------------------------------
# The authority the target is addressed by
# ---------------------------------------------------------------------------
# The path segment always spells the port out, so that one target has one
# path. What goes upstream must not: a real browser sends `Host: h.example.net`
# for an https URL on 443, and a server that routes on the Host line answers
# `h.example.net:443` with a different page than the one the user asked for.

@pytest.mark.parametrize("sch,host,port,expected", [
    ("s", "h.example.net", 443, "h.example.net"),
    ("s", "h.example.net", "443", "h.example.net"),
    ("s", "h.example.net", 8443, "h.example.net:8443"),
    ("h", "h.example.net", 80, "h.example.net"),
    ("h", "h.example.net", 8080, "h.example.net:8080"),
    # 443 is not http's default, and a port is only dropped when it is the
    # scheme's own — otherwise the address would name a different server.
    ("h", "h.example.net", 443, "h.example.net:443"),
    ("s", "h.example.net", 80, "h.example.net:80"),
    # The long spellings, for the callers that hold a scheme rather than the
    # path's one letter, and the socket ones the WebSocket half addresses.
    ("https", "h.example.net", 443, "h.example.net"),
    ("http", "h.example.net", 80, "h.example.net"),
    ("wss", "h.example.net", 443, "h.example.net"),
    ("ws", "h.example.net", 8080, "h.example.net:8080"),
    # A v6 literal keeps its brackets either way round.
    ("s", "[::1]", 443, "[::1]"),
    ("h", "[2001:db8::1]", 8080, "[2001:db8::1]:8080"),
])
def test_a_default_port_is_left_out_of_the_authority(sch, host, port, expected):
    assert A.browse_authority(sch, host, port) == expected


def headers_for(target_origin: str, authority: str, sent: dict) -> dict:
    """browse_upstream_headers over one made-up request, as a dict."""
    scope = {
        "type": "http", "method": "GET", "path": "/", "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in sent.items()],
    }
    pairs = A.browse_upstream_headers(Request(scope), authority, target_origin,
                                      PREFIX, False)
    return dict(pairs)


def test_a_default_port_target_is_addressed_without_one():
    """The headers a 443 target sees, with no 443 anywhere in them."""
    tok = "sometoken"
    authority = A.browse_authority("s", "portal.example.net", 443)
    out = headers_for(f"https://{authority}", authority, {
        "Host": "testserver",
        "Origin": SHELL_ORIGIN,
        # As the browser sends it: the path segment it is on carries the port.
        "Referer": f"{SHELL_ORIGIN}{PREFIX}/b/{tok}/s/portal.example.net:443/dir/page",
    })
    assert out["host"] == "portal.example.net"
    assert out["origin"] == "https://portal.example.net"
    assert out["referer"] == "https://portal.example.net/dir/page"


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
    """With nothing live to point it at — the case below is the other one."""
    r = client.get(f"/b/nosuchtoken/h/127.0.0.1:{site}/")
    assert r.status_code == 403
    assert '"expired"' in r.text
    assert "pockettui-browse-error" in r.text
    assert r.headers["content-security-policy"] == A.BROWSE_SANDBOX


def test_an_unknown_token_is_re_pointed_at_the_live_one(client, site):
    """A restart mints a new token; the addresses the pane holds carry the old.

    Answering those with the live token is what keeps a restart from costing
    the pane a re-mint and a retry — and the retry was the loop: it was aimed
    at the error page's own address, which is this proxy's.
    """
    tok = mint(client, f"http://127.0.0.1:{site}/", prefix="").json()["token"]
    r = client.get(f"/b/deadtoken/h/127.0.0.1:{site}/whoami",
                   follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == f"/b/{tok}/h/127.0.0.1:{site}/whoami"
    assert client.get(r.headers["location"]).text.startswith("cookie=")


def test_an_unknown_token_keeps_the_query_it_arrived_with(client, site):
    tok = mint(client, f"http://127.0.0.1:{site}/", prefix="").json()["token"]
    r = client.get(f"/b/deadtoken/h/127.0.0.1:{site}/whoami?q=1&r=2",
                   follow_redirects=False)
    assert r.headers["location"] == f"/b/{tok}/h/127.0.0.1:{site}/whoami?q=1&r=2"


def test_a_stale_token_under_the_public_prefix_is_re_pointed_too(client, site):
    tok = mint(client, f"http://127.0.0.1:{site}/").json()["token"]
    r = client.get(f"/b/deadtoken/h/127.0.0.1:{site}/whoami",
                   follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == f"{PREFIX}/b/{tok}/h/127.0.0.1:{site}/whoami"


# ---------------------------------------------------------------------------
# The proxy reached through itself
# ---------------------------------------------------------------------------
# The founder's runaway: a page left open under a token a restart had replaced
# reported its own proxied address as where it was, the pane wrapped that as a
# target, and the backend proxied its own public name — which the loop guard
# cannot see, because `tailscale serve` fronts it on 443 and the guard knows
# only LISTEN_PORT. Twenty layers inside a second, and one more per reload.
#
# The fixture site stands in for that public name here: a target whose path is
# itself a proxy path is the shape, whoever is serving it.

def test_a_target_that_is_this_proxy_is_peeled_to_the_real_one(client, site):
    tok = mint(client, f"http://127.0.0.1:{site}/", prefix="").json()["token"]
    inner = f"/b/{tok}/h/127.0.0.1:{site}/whoami"
    r = client.get(f"/b/{tok}/h/127.0.0.1:{site}{inner}", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == inner
    assert client.get(r.headers["location"]).text.startswith("cookie=")


def test_three_layers_are_peeled_in_one_answer(client, site):
    """Not one layer per round trip: the pane would spend the round trips."""
    tok = mint(client, f"http://127.0.0.1:{site}/", prefix="").json()["token"]
    hop = f"/b/{tok}/h/127.0.0.1:{site}"
    r = client.get(f"{hop}{hop}{hop}/whoami", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == f"{hop}/whoami"


def test_a_nested_layer_carrying_the_public_prefix_is_peeled(client, site):
    """The shape the founder's URL actually had: the prefix is inside it.

    `tailscale serve` strips /pockettui before this server sees the request,
    so the outer layer arrives bare — but the layer that was wrapped around
    the public name kept the prefix the browser saw.
    """
    tok = mint(client, f"http://127.0.0.1:{site}/").json()["token"]
    r = client.get(f"/b/{tok}/h/127.0.0.1:{site}{PREFIX}/b/{tok}"
                   f"/h/127.0.0.1:{site}/whoami", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == f"{PREFIX}/b/{tok}/h/127.0.0.1:{site}/whoami"


def test_a_nested_layer_under_a_dead_token_lands_on_the_live_one(client, site):
    """Both halves at once: the outer token is gone and the path is wrapped."""
    tok = mint(client, f"http://127.0.0.1:{site}/", prefix="").json()["token"]
    r = client.get(f"/b/deadtoken/h/127.0.0.1:{site}/b/oldertoken"
                   f"/h/127.0.0.1:{site}/whoami", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == f"/b/{tok}/h/127.0.0.1:{site}/whoami"


def test_an_ordinary_path_that_merely_starts_with_b_is_not_peeled(client, site):
    """/b/ on the target is only this proxy's when the rest of it matches.

    The 404 is the fixture site's own, which is the assertion: the request
    went upstream rather than being answered here with a redirect.
    """
    tok = mint(client, f"http://127.0.0.1:{site}/", prefix="").json()["token"]
    r = client.get(f"/b/{tok}/h/127.0.0.1:{site}/b/thing/echo-headers",
                   follow_redirects=False)
    assert r.status_code == 404


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


# A scheme is the one thing an address typed without one was guessed at, so a
# failure that a scheme could be the whole of offers the other one. It matters
# in a tab above all: there is no pane there to retry on the user's behalf.

def token_for(prefix=PREFIX):
    return A.BrowseToken(token="TOK", prefix=prefix, origin=SHELL_ORIGIN,
                         created=time.time(), last_used=time.time(), client=None)


@pytest.mark.parametrize("code", ["tls", "refused", "timeout", "unknown_host"])
def test_a_secure_target_that_will_not_talk_offers_http(code):
    rec = token_for()
    alt = A.browse_other_scheme(rec, "s", "box.example.net:443", "/app", "q=1")
    assert alt == f"{PREFIX}/b/TOK/h/box.example.net:80/app?q=1"
    page = A.browse_error_page(code, "box.example.net:443", SHELL_ORIGIN, alt)
    assert f'href="{alt}"' in page
    assert "Try http:// instead" in page
    # The retry the pane has always had stays where it was.
    assert 'onclick="location.reload()"' in page


def test_a_plain_target_that_will_not_talk_offers_https():
    rec = token_for()
    alt = A.browse_other_scheme(rec, "h", "box.example.net:80", "/", "")
    assert alt == f"{PREFIX}/b/TOK/s/box.example.net:443/"
    page = A.browse_error_page("refused", "box.example.net:80", SHELL_ORIGIN, alt)
    assert f'href="{alt}"' in page
    assert "Try https:// instead" in page


def test_a_port_somebody_wrote_out_is_not_guessed_at():
    """Only a default port means "no port was meant"; :8080 names one server."""
    rec = token_for()
    assert A.browse_other_scheme(rec, "h", "box.example.net:8080", "/", "") is None
    assert A.browse_other_scheme(rec, "s", "box.example.net:8443", "/", "") is None


@pytest.mark.parametrize("code", ["loop", "bad_target", "upstream", "expired"])
def test_a_failure_a_scheme_cannot_explain_offers_nothing(code):
    """A 502 from a target that did answer, or a token that has gone, says
    nothing about the scheme, and a link there would be noise."""
    page = A.browse_error_page(code, "box.example.net:443", SHELL_ORIGIN,
                               f"{PREFIX}/b/TOK/h/box.example.net:80/")
    assert "Try http" not in page


def test_the_proxy_puts_the_other_scheme_on_the_page_it_serves(client, site):
    """End to end, on the one failure a test can have without a network: a
    name that cannot resolve, addressed each way round."""
    body, _ = opened(client, site)
    tok = body["token"]
    r = client.get(f"/b/{tok}/h/nothing-resolves-here.invalid:80/app")
    assert f'href="{PREFIX}/b/{tok}/s/nothing-resolves-here.invalid:443/app"' in r.text
    r = client.get(f"/b/{tok}/s/nothing-resolves-here.invalid:443/")
    assert f'href="{PREFIX}/b/{tok}/h/nothing-resolves-here.invalid:80/"' in r.text
    # And nothing to offer where the port was written out.
    r = client.get(f"/b/{tok}/h/nothing-resolves-here.invalid:8080/")
    assert "Try http" not in r.text and "Try https" not in r.text


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
            "host": conn.request.headers.get("Host"),
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
    # A port that is not the scheme's default stays on both lines.
    assert echo.seen[-1]["host"] == f"127.0.0.1:{echo.port}"


def test_a_default_port_socket_target_is_addressed_without_one(client,
                                                               monkeypatch):
    """No fixture can bind 443, so the handshake is caught on its way out.

    The connect is replaced by a recorder that refuses, which is enough: the
    URI and the Origin are decided before it is called, and the URI is where
    the handshake's own Host line comes from.
    """
    seen: dict = {}

    async def refuse(uri, **kw):
        seen["uri"] = uri
        seen["headers"] = kw.get("additional_headers") or {}
        raise OSError("not a real target")

    monkeypatch.setattr(A, "_ws_connect", lambda: refuse)
    tok = mint(client, "https://portal.example.net/").json()["token"]
    assert closed_with(client.websocket_connect(
        f"/b/{tok}/s/portal.example.net:443/socket")) == 4502
    assert seen["uri"] == "wss://portal.example.net/socket"
    assert seen["headers"]["Origin"] == "https://portal.example.net"


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


def test_a_socket_at_this_proxy_is_refused_rather_than_carried(client, echo):
    """A socket cannot be redirected, so the nested shape is simply not one."""
    tok = mint(client, f"http://127.0.0.1:{echo.port}/",
               prefix="").json()["token"]
    assert closed_with(client.websocket_connect(
        f"/b/{tok}/h/127.0.0.1:{echo.port}/b/{tok}"
        f"/h/127.0.0.1:{echo.port}/echo")) == 4400


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


# ---------------------------------------------------------------------------
# Bookmarks
# ---------------------------------------------------------------------------

@pytest.fixture
def marks(tmp_path, monkeypatch):
    """The list on a computer nobody has bookmarked anything on yet."""
    p = tmp_path / ".pockettui" / "browser_bookmarks.json"
    monkeypatch.setattr(A, "BOOKMARKS_PATH", p)
    return p


ONE = {"url": "https://wiki.example.net/start", "title": "Start", "added": 1700000000}


def test_a_computer_with_no_file_has_no_bookmarks(client, marks):
    r = client.get("/api/browse/bookmarks", headers=HDRS)
    assert r.status_code == 200
    assert r.json() == {"bookmarks": []}


def test_a_file_that_is_not_a_list_reads_as_none(client, marks):
    """A hand-edited or half-written file costs the pane its bar, not a 500."""
    marks.parent.mkdir(parents=True)
    marks.write_text("{oh no", encoding="utf-8")
    assert client.get("/api/browse/bookmarks", headers=HDRS).json() == {"bookmarks": []}
    marks.write_text('{"url": "https://wiki.example.net/"}', encoding="utf-8")
    assert client.get("/api/browse/bookmarks", headers=HDRS).json() == {"bookmarks": []}


def test_a_saved_list_is_written_and_read_back(client, marks):
    r = client.put("/api/browse/bookmarks", json={"bookmarks": [ONE]}, headers=HDRS)
    assert r.status_code == 200
    assert r.json() == {"bookmarks": [ONE]}
    assert json.loads(marks.read_text(encoding="utf-8")) == [ONE]
    assert client.get("/api/browse/bookmarks", headers=HDRS).json() == {"bookmarks": [ONE]}


def test_a_save_replaces_the_whole_list(client, marks):
    client.put("/api/browse/bookmarks", json={"bookmarks": [ONE]}, headers=HDRS)
    r = client.put("/api/browse/bookmarks", json={"bookmarks": []}, headers=HDRS)
    assert r.json() == {"bookmarks": []}
    assert client.get("/api/browse/bookmarks", headers=HDRS).json() == {"bookmarks": []}


def test_a_missing_timestamp_is_filled_rather_than_refused(client, marks):
    """The only caller that omits it is one bookmarking a page this second."""
    before = int(time.time())
    r = client.put("/api/browse/bookmarks",
                   json={"bookmarks": [{"url": "https://wiki.example.net/", "title": "w"}]},
                   headers=HDRS)
    assert r.status_code == 200
    assert before <= r.json()["bookmarks"][0]["added"] <= int(time.time())


def test_a_long_title_is_cut_rather_than_refused(client, marks):
    r = client.put("/api/browse/bookmarks",
                   json={"bookmarks": [dict(ONE, title="t" * 500)]}, headers=HDRS)
    assert r.status_code == 200
    assert r.json()["bookmarks"][0]["title"] == "t" * A.BOOKMARK_TITLE_MAX


@pytest.mark.parametrize("body", [
    {"bookmarks": "nope"},                                   # not a list
    {},                                                      # no list at all
    {"bookmarks": ["https://wiki.example.net/"]},            # not an entry
    {"bookmarks": [{"title": "no url"}]},
    {"bookmarks": [dict(ONE, url="ftp://wiki.example.net/")]},
    {"bookmarks": [dict(ONE, url="javascript:alert(1)")]},
    {"bookmarks": [dict(ONE, url="https://" + "a" * 2100)]},
    {"bookmarks": [dict(ONE, title=7)]},
    {"bookmarks": [dict(ONE, added="yesterday")]},
    {"bookmarks": [ONE] * 501},
])
def test_a_list_this_server_will_not_keep_is_refused(client, marks, body):
    r = client.put("/api/browse/bookmarks", json=body, headers=HDRS)
    assert r.status_code == 400
    assert r.json()["error"] == "bad_bookmarks"
    assert not marks.exists()


def test_the_bookmark_routes_are_gated_like_their_neighbours(client, marks):
    assert client.get("/api/browse/bookmarks").status_code == 401
    assert client.put("/api/browse/bookmarks", json={"bookmarks": []}).status_code == 401
    assert not marks.exists()


def test_the_capability_map_says_bookmarks_too():
    """Independent of httpx: storing a list needs nothing to fetch pages with."""
    assert A.server_capabilities()["bookmarks"] is True


def test_a_put_can_be_preflighted_from_the_hosted_shell(client):
    """The shell is cross-origin, and a method CORS omits never leaves it.

    The save is this server's first route that is neither GET nor POST, so the
    preflight is the thing to pin: without PUT on the allow list the browser
    refuses the request before it is ever sent, and the pane would look as if
    the computer had said no.
    """
    r = client.options("/api/browse/bookmarks", headers={
        "Origin": SHELL_ORIGIN,
        "Access-Control-Request-Method": "PUT",
        "Access-Control-Request-Headers": A.TOKEN_HEADER,
    })
    assert r.status_code == 200
    assert "PUT" in r.headers["access-control-allow-methods"]
