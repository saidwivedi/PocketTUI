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

import asyncio
import gzip
import html
import json
import re
import socket
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
import chromium as CH  # noqa: E402

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

# A PDF small enough to read in an assertion and real enough to be sniffed:
# what settles it either way is the five bytes at the front.
PDF = (b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n"
       b"trailer\n<< /Root 1 0 R >>\n%%EOF\n")

FRAME = """<!doctype html>
<html><body><embed src="/doc.pdf" type="application/pdf"></body></html>
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
        rng = re.match(r"bytes=(\d+)-(\d*)$",
                       self.headers.get("Range", "")) if status == 200 else None
        if rng:
            # What Chrome's PDF viewer asks a large document for, so the proxy
            # has to carry the header up and the 206 back down.
            start = int(rng.group(1))
            end = min(int(rng.group(2)) if rng.group(2) else len(body) - 1,
                      len(body) - 1)
            piece = body[start:end + 1]
            self.send_response(206)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(piece)))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Range",
                             f"bytes {start}-{end}/{len(body)}")
            for name, value in extra:
                self.send_header(name, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(piece)
            return
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
        elif path == "/lib.js":
            self.send_body(b"if(top===self){go()}window.parent.x=1;",
                           "application/javascript")
        elif path == "/strict.js":
            self.send_body(b'"use strict";var a=top;', "application/javascript")
        elif path == "/data.json":
            self.send_body(b'{"top": 1, "parent": 2}', "application/json")
        elif path == "/setcookie":
            self.send_body(b"ok", "text/plain", extra=(
                ("Set-Cookie", "keep=yes; Path=/; Max-Age=3600"),))
        elif path == "/setcookies":
            # One value made of the three characters the shim's configuration
            # attribute has to escape, and one the page is not meant to read.
            self.send_body(b"ok", "text/plain", extra=(
                ("Set-Cookie", "tricky=a'b<c&d; Path=/; Max-Age=3600"),
                ("Set-Cookie", "hidden=nope; Path=/; Max-Age=3600; HttpOnly"),))
        elif path == "/enablejs":
            # What Google answers a search with when the address this computer
            # speaks from is rate-limited: a plain 200 on the page's own
            # address, which only the body gives away.
            self.send_body(
                b"<html><body><p>We're sorry, but your computer or network "
                b"may be sending automated queries. To protect our users, we "
                b"can't process your request right now. If you're having "
                b"trouble accessing Google Search, please "
                b'<a href="/httpservice/retry/enablejs">try again</a>.'
                b"</body></html>", "text/html; charset=utf-8")
        elif path == "/wall":
            # A bot wall: the status a site answers a visitor it does not trust
            # with, and a body that says which kind of wall it is. Both ride
            # into the shim's configuration for the pane to read (browse_wall).
            self.send_body(
                b"<html><head><title>Attention Required! | Cloudflare</title>"
                b'</head><body><div id="challenge-platform">checking</div>'
                b"</body></html>",
                "text/html; charset=utf-8", status=403)
        elif path == "/forbidden":
            # The same status with nothing to say for itself.
            self.send_body(b"<html><body>no</body></html>",
                           "text/html; charset=utf-8", status=403)
        elif path == "/style.css":
            self.send_body(SHEET.format(port=self.port).encode(), "text/css")
        elif path == "/doc.pdf":
            self.send_body(PDF, "application/pdf")
        elif path == "/bin.pdf":
            # A PDF handed out as a generic stream, which is what makes the
            # name the only thing the headers say about it.
            self.send_body(PDF, "application/octet-stream", extra=(
                ("Content-Disposition", 'attachment; filename="report.pdf"'),))
        elif path == "/sniff":
            # Nothing but the bytes: no type worth reading and no name.
            self.send_body(PDF, "application/octet-stream")
        elif path == "/frame.html":
            self.send_body(FRAME.encode(), "text/html; charset=utf-8")
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
    A._BROWSE_COOKIE_SAVE = None
    yield
    A.BROWSE.clear()
    A.BROWSE_CLIENT = None
    A._BROWSE_COOKIE_SAVE = None


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


def shim_cfg(page: str) -> dict:
    """The configuration the shim was served with, read back off the page."""
    raw = re.search(r"<script data-cfg='([^']*)'", page).group(1)
    return json.loads(html.unescape(raw))


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


def test_a_mint_from_another_origin_is_another_token(client, site):
    """The record is keyed by the mount: flavour, prefix and origin together."""
    first = mint(client, f"http://127.0.0.1:{site}/").json()
    again = mint(client, f"http://127.0.0.1:{site}/other").json()
    assert again["token"] == first["token"]
    other = mint(client, f"http://127.0.0.1:{site}/",
                 origin="https://other.example.net").json()
    assert other["token"] != first["token"]
    assert len(A.BROWSE) == 2


def test_a_second_mount_gets_its_own_token(client, site):
    """One shell through `tailscale serve`, one reaching this server directly.

    The prefix cannot be moved onto the existing record: it is in the shim's
    configuration and in every URL of every page already served under that
    token, so the other shell would be left holding links to a mount its
    browser cannot reach.
    """
    tailnet = mint(client, f"http://127.0.0.1:{site}/").json()
    local = mint(client, f"http://127.0.0.1:{site}/", prefix="").json()
    assert local["token"] != tailnet["token"]
    assert local["prefix"] == ""
    assert local["url"] == f"/b/{local['token']}/h/127.0.0.1:{site}/"
    assert len(A.BROWSE) == 2
    assert A.BROWSE[tailnet["token"]].prefix == PREFIX
    assert A.BROWSE[local["token"]].prefix == ""
    # One jar and one pool between them: two shells reaching one machine are
    # two views of the same browsing session.
    assert A.BROWSE[local["token"]].client is A.BROWSE[tailnet["token"]].client


def test_a_second_mount_leaves_the_first_shells_pages_alone(client, site):
    """The founder's breakage: a local mint with no prefix moved the record the
    live pane's pages were built from, and every link in them lost /pockettui.
    """
    tailnet = mint(client, f"http://127.0.0.1:{site}/").json()["token"]
    local = mint(client, f"http://127.0.0.1:{site}/", prefix="").json()["token"]

    page = client.get(f"{base_of(tailnet, site)}/"[len(PREFIX):])
    assert page.status_code == 200
    assert f'"prefix": "{PREFIX}"' in page.text
    assert f'href="{base_of(tailnet, site)}/whoami"' in page.text
    assert 'href="/b/' not in page.text

    # And the shell that minted second gets its own mount's spelling.
    bare = client.get(f"{base_of(local, site, prefix='')}/")
    assert bare.status_code == 200
    assert '"prefix": ""' in bare.text
    assert f'href="{base_of(local, site, prefix="")}/whoami"' in bare.text


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


def test_the_tab_mint_is_keyed_by_mount_like_the_pane(client, site):
    tailnet = mint_tab(client, f"http://127.0.0.1:{site}/").json()["token"]
    local = mint_tab(client, f"http://127.0.0.1:{site}/", prefix="").json()["token"]
    assert local != tailnet
    assert len(A.BROWSE) == 2
    assert {r.sandbox for r in A.BROWSE.values()} == {False}
    page = client.get(f"{base_of(tailnet, site)}/"[len(PREFIX):])
    assert f'"prefix": "{PREFIX}"' in page.text
    assert f'href="{base_of(tailnet, site)}/whoami"' in page.text


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


def test_a_dead_token_heals_to_the_mount_that_minted_last(client, site):
    """Every sandboxed record carries its own mount's prefix, and the address
    being healed arrives without one whatever the browser saw (`tailscale
    serve` strips it), so the shell that asked for something last is the best
    guess there is."""
    first = mint(client, f"http://127.0.0.1:{site}/").json()["token"]
    later = mint(client, f"http://127.0.0.1:{site}/", prefix="").json()["token"]
    A.BROWSE[first].last_used -= 1
    r = client.get(f"/b/deadtoken/h/127.0.0.1:{site}/whoami",
                   follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == f"/b/{later}/h/127.0.0.1:{site}/whoami"
    A.BROWSE[later].last_used -= 2       # now the other one is the recent mount
    r = client.get(f"/b/deadtoken/h/127.0.0.1:{site}/whoami",
                   follow_redirects=False)
    assert r.headers["location"] == f"{PREFIX}/b/{first}/h/127.0.0.1:{site}/whoami"


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


def test_the_shim_is_seeded_with_the_cookies_the_page_may_read(client, site):
    """Sandboxed, the page's own document.cookie throws, so the shim keeps a jar
    of its own — and it has to start with what the session already holds, or a
    script that reads back what the hop before set finds nothing. HttpOnly ones
    stay out of it: the page was never meant to see them, and they go to the
    target from here regardless."""
    tok = mint(client, f"http://127.0.0.1:{site}/").json()["token"]
    base = base_of(tok, site)
    assert client.get(f"{base}/"[len(PREFIX):]).status_code == 200
    assert client.get(f"{base}/setcookies"[len(PREFIX):]).status_code == 200

    page = client.get(f"{base}/"[len(PREFIX):]).text
    raw = re.search(r"<script data-cfg='([^']*)'", page).group(1)
    # A single-quoted attribute the page must not be able to end: the three
    # characters a cookie value may carry come through as entities.
    assert "&#39;" in raw and "&lt;" in raw and "&amp;" in raw
    cfg = json.loads(html.unescape(raw))
    pairs = dict(p.split("=", 1) for p in cfg["cookies"].split("; "))
    assert pairs["sid"] == "1"
    assert pairs["tricky"] == "a'b<c&d"
    assert "hidden" not in pairs
    # And the value the page is not shown is still the one the target is sent.
    assert "hidden=nope" in client.get(f"{base}/whoami"[len(PREFIX):]).text


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


# ---------------------------------------------------------------------------
# A script a page on the computer's own network loads
# ---------------------------------------------------------------------------
# The inline half is tested on strings (tests/test_browse_rewrite.py); this is
# the half that only exists over the wire, where three things have to line up:
# the flavour of the token, what the browser said it was fetching, and what the
# target said it sent back.

def _script(client, tok, site, path, dest="script"):
    # The prefix is what a `tailscale serve` front end adds; this app serves
    # the routes bare, which is what every other test here strips too.
    return client.get((base_of(tok, site) + path)[len(PREFIX):],
                      headers={"Sec-Fetch-Dest": dest} if dest else {})


def test_a_script_on_the_computers_network_reads_the_pages_own_top(client, site):
    r = mint_tab(client, f"http://127.0.0.1:{site}/")
    assert r.status_code == 200, r.text
    got = _script(client, r.json()["token"], site, "/lib.js")
    assert got.status_code == 200
    body = got.text
    assert body.startswith("with(window.__pt||{}){")
    # The bare word is left for `with` to resolve; the member spelling cannot
    # be reached that way and is rewritten.
    assert "if(top===self){go()}" in body and "__pt.parent.x=1;" in body


def test_the_panes_own_scripts_are_passed_through_as_they_came(client, site):
    r = mint(client, f"http://127.0.0.1:{site}/")
    got = _script(client, r.json()["token"], site, "/lib.js")
    assert got.text == "if(top===self){go()}window.parent.x=1;"
    assert "__pt" not in got.text


def test_a_strict_script_is_never_wrapped(client, site):
    # `with` is a SyntaxError in strict code: wrapping one would take the
    # library away rather than fix it.
    r = mint_tab(client, f"http://127.0.0.1:{site}/")
    got = _script(client, r.json()["token"], site, "/strict.js")
    assert got.text == '"use strict";var a=top;'


def test_json_is_never_wrapped_however_it_is_fetched(client, site):
    # Both gates, one at a time: the wrong content type with the right
    # destination, and the right type with the destination an XHR sends.
    r = mint_tab(client, f"http://127.0.0.1:{site}/")
    tok = r.json()["token"]
    assert _script(client, tok, site, "/data.json").text == '{"top": 1, "parent": 2}'
    assert _script(client, tok, site, "/lib.js", dest="empty").text == (
        "if(top===self){go()}window.parent.x=1;")


def test_a_wrapped_script_keeps_its_own_content_type(client, site):
    r = mint_tab(client, f"http://127.0.0.1:{site}/")
    got = _script(client, r.json()["token"], site, "/lib.js")
    assert got.headers["content-type"] == "application/javascript; charset=utf-8"
    assert int(got.headers["content-length"]) == len(got.content)
    # Not a document: the sandbox header belongs to the page that loads it.
    assert "content-security-policy" not in got.headers


# ---------------------------------------------------------------------------
# The cookie jar between runs
# ---------------------------------------------------------------------------
# A browser does not ask its user to log in again because it was restarted, and
# this server had been doing exactly that: every `pockettui update` threw away
# every session the pane held.

def test_the_jar_is_written_to_disk_and_read_back(tmp_path, monkeypatch):
    path = tmp_path / "state" / "browser_cookies.json"
    monkeypatch.setattr(A, "BROWSE_COOKIES_PATH", path)
    jar = httpx.Cookies().jar
    jar.set_cookie(A.browse_cookie_from_row(
        {"name": "sid", "value": "abc", "domain": "box.example.net",
         "domain_specified": False, "path": "/",
         "secure": False, "expires": int(time.time()) + 3600}))
    A.browse_save_cookies(jar)
    assert path.exists()
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    back = httpx.Cookies().jar
    assert A.browse_load_cookies(back) == 1
    assert [c.name for c in back] == ["sid"]


def test_an_expired_cookie_is_dropped_on_the_way_back_in(tmp_path, monkeypatch):
    path = tmp_path / "browser_cookies.json"
    monkeypatch.setattr(A, "BROWSE_COOKIES_PATH", path)
    path.write_text(json.dumps([
        {"name": "old", "value": "1", "domain": "box.example.net",
         "path": "/", "secure": False, "expires": int(time.time()) - 10},
        {"name": "new", "value": "2", "domain": "box.example.net",
         "path": "/", "secure": False, "expires": int(time.time()) + 3600},
    ]), encoding="utf-8")
    jar = httpx.Cookies().jar
    assert A.browse_load_cookies(jar) == 1
    assert [c.name for c in jar] == ["new"]


def test_a_session_cookie_is_not_kept_across_a_restart(tmp_path):
    # One with no expiry is one the device in front of the user would drop when
    # it closed, and this file is what outlives a restart.
    jar = httpx.Cookies().jar
    jar.set_cookie(A.browse_cookie_from_row(
        {"name": "keep", "value": "1", "domain": "box.example.net", "path": "/",
         "secure": False, "expires": int(time.time()) + 60}))
    rows = A.browse_cookie_rows(jar)
    assert [r["name"] for r in rows] == ["keep"]
    for c in jar:
        c.expires = None
    assert A.browse_cookie_rows(jar) == []


def test_a_missing_or_broken_file_leaves_the_jar_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "BROWSE_COOKIES_PATH", tmp_path / "nope.json")
    assert A.browse_load_cookies(httpx.Cookies().jar) == 0
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(A, "BROWSE_COOKIES_PATH", bad)
    assert A.browse_load_cookies(httpx.Cookies().jar) == 0


def test_a_set_cookie_through_the_proxy_reaches_the_file(client, site, tmp_path,
                                                         monkeypatch):
    path = tmp_path / "browser_cookies.json"
    monkeypatch.setattr(A, "BROWSE_COOKIES_PATH", path)
    monkeypatch.setattr(A, "BROWSE_COOKIE_SAVE_AFTER", 0.05)
    r = mint(client, f"http://127.0.0.1:{site}/")
    tok = r.json()["token"]
    assert client.get(
        (base_of(tok, site) + "/setcookie")[len(PREFIX):]).status_code == 200
    for _ in range(100):
        if path.exists():
            break
        time.sleep(0.05)
    rows = json.loads(path.read_text(encoding="utf-8"))
    assert [row["name"] for row in rows] == ["keep"]


def test_a_restored_jar_is_sent_to_the_target(client, site, tmp_path,
                                              monkeypatch):
    # The whole point of the file: the next run is still logged in.
    path = tmp_path / "browser_cookies.json"
    monkeypatch.setattr(A, "BROWSE_COOKIES_PATH", path)
    path.write_text(json.dumps([
        {"name": "sid", "value": "restored", "domain": "127.0.0.1", "path": "/",
         "secure": False, "expires": int(time.time()) + 3600},
    ]), encoding="utf-8")
    r = mint(client, f"http://127.0.0.1:{site}/")
    got = client.get(
        (base_of(r.json()["token"], site) + "/whoami")[len(PREFIX):])
    assert "sid=restored" in got.text


# ---------------------------------------------------------------------------
# HTTP Basic sign-ins the frame cannot send
# ---------------------------------------------------------------------------
# The pane's frame is on an opaque origin: the browser sends the credentials
# it prompted for with the navigation and with no-cors loads, and with nothing
# else. A font and a DataTables POST arrived without them and were answered
# 401, so the proxy remembers the Basic value and hands it on (browse_auth_*).

GOOD = "Basic " + "dXNlcjpwYXNz"            # user:pass
OTHER = "Basic " + "dXNlcjpvdGhlcg=="       # user:other


class AuthHandler(BaseHTTPRequestHandler):
    """An intranet page behind Basic auth, recording what each request carried."""

    protocol_version = "HTTP/1.1"
    accepted = GOOD
    seen: list = []

    def log_message(self, fmt, *args):  # noqa: A003, quiet under pytest
        pass

    def answer(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        if length:
            self.rfile.read(length)
        sent = self.headers.get("Authorization")
        type(self).seen.append((self.command, self.path, sent))
        ctype = "text/html; charset=utf-8"
        if sent != type(self).accepted:
            body = b"<html><body>sign in</body></html>"
            self.send_response(401)
            self.send_header("WWW-Authenticate", "Negotiate")
            self.send_header("WWW-Authenticate", 'Basic realm="Campus Login"')
        elif self.path.endswith(".woff"):
            body, ctype = b"wOFF", "font/woff"
            self.send_response(200)
        else:
            body = b"<html><body>ok</body></html>"
            self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = answer  # noqa: N815
    do_POST = answer  # noqa: N815


@pytest.fixture
def auth_site():
    AuthHandler.accepted = GOOD
    AuthHandler.seen = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), AuthHandler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=3)


def auth_base(client, port):
    r = mint(client, f"http://127.0.0.1:{port}/app/")
    assert r.status_code == 200, r.text
    tok = r.json()["token"]
    return tok, base_of(tok, port)[len(PREFIX):]


def last_auth():
    return AuthHandler.seen[-1][2]


def test_the_first_navigation_still_gets_the_prompt(client, auth_site):
    _, base = auth_base(client, auth_site)
    r = client.get(f"{base}/app/")
    assert r.status_code == 401
    assert r.headers.get_list("www-authenticate") == [
        "Negotiate", 'Basic realm="Campus Login"']


def test_a_basic_sign_in_is_remembered(client, auth_site):
    tok, base = auth_base(client, auth_site)
    r = client.get(f"{base}/app/", headers={"Authorization": GOOD})
    assert r.status_code == 200
    assert last_auth() == GOOD
    assert A.BROWSE[tok].auth == {("http", "127.0.0.1", auth_site): GOOD}
    assert GOOD not in repr(A.BROWSE[tok])


def test_a_font_without_it_is_sent_with_it(client, auth_site):
    _, base = auth_base(client, auth_site)
    client.get(f"{base}/app/", headers={"Authorization": GOOD})
    r = client.get(f"{base}/app/fonts/glyphicons-halflings-regular.woff")
    assert r.status_code == 200 and r.content == b"wOFF"
    assert last_auth() == GOOD


def test_a_post_without_it_is_sent_with_it(client, auth_site):
    _, base = auth_base(client, auth_site)
    client.get(f"{base}/app/", headers={"Authorization": GOOD})
    r = client.post(f"{base}/app/API/LastMovements", content=b"draw=1")
    assert r.status_code == 200
    assert AuthHandler.seen[-1][:1] == ("POST",)
    assert last_auth() == GOOD


def test_a_refused_remembered_value_is_relayed_and_forgotten(client, auth_site):
    tok, base = auth_base(client, auth_site)
    client.get(f"{base}/app/", headers={"Authorization": GOOD})
    # The password changed on the target's side since the prompt.
    AuthHandler.accepted = OTHER
    r = client.get(f"{base}/app/fonts/fontawesome-webfont.woff")
    assert r.status_code == 401
    assert last_auth() == GOOD
    assert 'Basic realm="Campus Login"' in r.headers.get_list("www-authenticate")
    assert A.BROWSE[tok].auth == {}
    client.get(f"{base}/app/fonts/fontawesome-webfont.woff")
    assert last_auth() is None


def test_a_refused_value_from_the_browser_is_not_remembered(client, auth_site):
    tok, base = auth_base(client, auth_site)
    r = client.get(f"{base}/app/", headers={"Authorization": OTHER})
    assert r.status_code == 401
    assert A.BROWSE[tok].auth == {}


def test_another_host_port_under_the_same_token_gets_nothing(client, auth_site,
                                                              site):
    tok, base = auth_base(client, auth_site)
    client.get(f"{base}/app/", headers={"Authorization": GOOD})
    r = client.get(f"{base_of(tok, site)}/echo-headers"[len(PREFIX):])
    assert r.status_code == 200
    assert "authorization" not in r.text


def test_another_scheme_on_the_same_host_port_gets_nothing(client, auth_site):
    tok, base = auth_base(client, auth_site)
    client.get(f"{base}/app/", headers={"Authorization": GOOD})
    rec = A.BROWSE[tok]
    headers: list = []

    class Req:
        headers = {}

    used = A.browse_auth_attach(Req, rec, ("https", "127.0.0.1", auth_site),
                                headers)
    assert used is None and headers == []


def test_a_digest_header_is_forwarded_and_not_remembered(client, auth_site):
    tok, base = auth_base(client, auth_site)
    digest = 'Digest username="user", realm="x", nonce="n1", response="r"'
    client.get(f"{base}/app/", headers={"Authorization": digest})
    assert last_auth() == digest
    assert A.BROWSE[tok].auth == {}


def test_the_tab_flavour_remembers_nothing(client, auth_site):
    r = mint_tab(client, f"http://127.0.0.1:{auth_site}/app/")
    assert r.status_code == 200, r.text
    tok = r.json()["token"]
    base = base_of(tok, auth_site)[len(PREFIX):]
    client.get(f"{base}/app/", headers={"Authorization": GOOD})
    assert A.BROWSE[tok].auth == {}
    client.get(f"{base}/app/fonts/x.woff")
    assert last_auth() is None


def test_clear_browsing_data_forgets_every_sign_in(client, auth_site,
                                                   monkeypatch, tmp_path,
                                                   no_browser_singleton):
    monkeypatch.setenv("HOME", str(tmp_path))
    tok, base = auth_base(client, auth_site)
    client.get(f"{base}/app/", headers={"Authorization": GOOD})
    assert A.BROWSE[tok].auth

    async def shutdown(timeout=6.0):
        pass

    monkeypatch.setattr(CH.FullBrowser.get(), "shutdown", shutdown)
    r = client.post("/api/browser/reset", headers=HDRS)
    assert r.status_code == 200
    assert A.BROWSE[tok].auth == {}
    client.get(f"{base}/app/fonts/x.woff")
    assert last_auth() is None


# ---------------------------------------------------------------------------
# Full browser: the pane's socket
# ---------------------------------------------------------------------------

A_BROWSER = CH.Found(path="/usr/bin/chromium", version="151.0.7000.1", major=151,
                     source="path")


@pytest.fixture
def no_browser_singleton(monkeypatch):
    """No FullBrowser outlives its test, and none of them starts a Chromium."""
    monkeypatch.setattr(CH.FullBrowser, "_instance", None)
    monkeypatch.setattr(A, "_CHROMIUM", CH)

    async def refuse(self):
        raise AssertionError("no test here may start a browser")

    monkeypatch.setattr(CH.FullBrowser, "ensure", refuse)
    yield
    CH.FullBrowser._instance = None


def browser_socket(client, pane="p1"):
    return client.websocket_connect(f"/ws/browser/{pane}")


def test_ws_browser_refuses_bad_token(client, no_browser_singleton):
    # CORS does not apply to WebSockets, so this handshake is the only thing
    # between the open port and a browser holding the user's logins.
    with pytest.raises(WebSocketDisconnect) as exc:
        with browser_socket(client) as ws:
            ws.send_text(json.dumps({"token": "WRONGCODE9", "dev": "phone"}))
            ws.receive_text()
    assert exc.value.code == 4401


def test_ws_browser_ready_after_auth(client, monkeypatch, no_browser_singleton):
    monkeypatch.setattr(CH, "find_chromium", lambda *a, **kw: A_BROWSER)
    with browser_socket(client) as ws:
        ws.send_text(json.dumps({"token": TOKEN, "dev": "phone"}))
        ready = json.loads(ws.receive_text())
        # A connect starts nothing: the browser is launched by the first tab
        # that is opened, not by a pane that happens to be on screen.
        assert ready["type"] == "ready"
        assert ready["version"] == "151.0.7000.1"
        assert ready["capMb"] >= 128

        # A message about a tab that is not open is an `error` reply, not a
        # closed socket: the pane has to stay up to show what went wrong.
        ws.send_text(json.dumps({"type": "show", "tab": "t1", "cssW": 400,
                                 "cssH": 300, "dpr": 1, "zoom": 1}))
        err = json.loads(ws.receive_text())
        assert err == {"type": "error", "tab": "t1", "code": "no_tab",
                       "message": "that tab is not open"}

        # And an unknown type is ignored rather than answered, so a newer
        # shell talking to an older server is not cut off mid-session.
        ws.send_text(json.dumps({"type": "somethingNew", "tab": "t1"}))
        ws.send_text(json.dumps({"type": "hide", "tab": "t1"}))
        assert json.loads(ws.receive_text())["code"] == "no_tab"


def test_cursor_probes_are_single_flight_and_never_awaited():
    """One probe in the air at a time, and no op waits behind it.

    Every message on the pane's socket is dispatched in turn, so a probe
    awaited here stands between the user's click and the mouse event; and a
    probe still running when the next mousemove arrives is an answer about a
    point the pointer has already left.
    """
    class Probe:
        def __init__(self):
            self.gate = asyncio.Event()
            self.calls = 0

        async def send(self, method, params=None, timeout=15):
            self.calls += 1
            await self.gate.wait()
            return {"result": {"value": {"cursor": "pointer", "title": ""}}}

    class Tab:
        pane, tab, zoom, cursor_sent, probe_busy = "p1", "t1", 1.0, None, False

    class Fb:
        def __init__(self):
            self.msgs = []

        def tab(self, pane, tab):
            return PT

        def to_pane(self, pane, msg):
            self.msgs.append(msg)

    async def main():
        fb = Fb()
        msg = {"type": "cursor", "tab": "t1", "x": 10, "y": 20}
        await A.browser_op_cursor(fb, "p1", msg)
        assert PT.probe_busy is True          # dispatched, not awaited
        await asyncio.sleep(0)
        assert PT.session.calls == 1
        # Twenty more moves while the page has not answered the first: dropped.
        for _ in range(20):
            await A.browser_op_cursor(fb, "p1", msg)
        assert PT.session.calls == 1
        assert fb.msgs == []

        PT.session.gate.set()
        for _ in range(50):
            if not PT.probe_busy:
                break
            await asyncio.sleep(0.01)
        assert PT.probe_busy is False
        assert fb.msgs == [{"type": "cursor", "tab": "t1", "cursor": "pointer",
                            "title": ""}]

        # The next move is asked again, and an unchanged answer is not resent.
        PT.session.gate.set()
        await A.browser_op_cursor(fb, "p1", msg)
        for _ in range(50):
            if not PT.probe_busy:
                break
            await asyncio.sleep(0.01)
        assert PT.session.calls == 2 and len(fb.msgs) == 1

    PT = Tab()
    PT.session = Probe()
    asyncio.run(main())


def test_ws_browser_refuses_a_proxied_page(client, no_browser_singleton):
    # The same gate require_token puts on the HTTP routes, which a WebSocket
    # handshake skips: a page this server's own proxy served has no business
    # driving the browser whatever credential it managed to put on the call.
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(
                "/ws/browser/p1",
                headers={"Referer": "https://shell.example.net/b/tok/s/"}) as ws:
            ws.send_text(json.dumps({"token": TOKEN}))
            ws.receive_text()
    assert exc.value.code == 4403


def test_capabilities_browser_full_follows_find(monkeypatch, no_browser_singleton):
    monkeypatch.setattr(CH, "find_chromium", lambda *a, **kw: None)
    assert A.server_capabilities()["browser_full"] is False

    monkeypatch.setattr(CH, "find_chromium", lambda *a, **kw: A_BROWSER)
    assert A.server_capabilities()["browser_full"] is True

    def blows_up(*a, **kw):
        raise OSError("the probe fell over")

    # A probe that throws is a browser this machine does not have, not a
    # capability map that cannot be built.
    monkeypatch.setattr(CH, "find_chromium", blows_up)
    assert A.server_capabilities()["browser_full"] is False


def test_browser_status_reports_the_browser_it_would_use(client, monkeypatch,
                                                         no_browser_singleton):
    monkeypatch.setattr(CH, "find_chromium", lambda *a, **kw: A_BROWSER)
    body = client.get("/api/browser/status", headers=HDRS).json()
    assert body["running"] is False and body["pid"] is None
    assert body["tabs"] == [] and body["memMb"] is None
    assert body["found"] == {"path": "/usr/bin/chromium", "version": "151.0.7000.1"}
    assert body["capMb"] >= 128
    assert client.get("/api/browser/status").status_code == 401


def test_ws_browser_mouse_before_open_is_no_tab_error(client, monkeypatch,
                                                      no_browser_singleton):
    # A pointer that lands on a pane whose tab has gone — closed under it, lost
    # with the browser — is a fact the pane has to hear about and recover from,
    # not a socket that drops. The page it was showing is on screen either way.
    monkeypatch.setattr(CH, "find_chromium", lambda *a, **kw: A_BROWSER)
    with browser_socket(client) as ws:
        ws.send_text(json.dumps({"token": TOKEN, "dev": "phone"}))
        assert json.loads(ws.receive_text())["type"] == "ready"

        ws.send_text(json.dumps({"type": "mouse", "tab": "t1", "kind": "down",
                                 "x": 10, "y": 20, "button": "left",
                                 "buttons": 1, "clicks": 1, "mods": 0}))
        err = json.loads(ws.receive_text())
        assert err == {"type": "error", "tab": "t1", "code": "no_tab",
                       "message": "that tab is not open"}

        # The rest of the input half answers the same way, and the socket is
        # still there to take the next message after every one of them.
        for msg in ({"type": "key", "tab": "t1", "kind": "down", "key": "a",
                     "text": "a"},
                    {"type": "text", "tab": "t1", "text": "hello"},
                    {"type": "link", "tab": "t1", "x": 1, "y": 2},
                    {"type": "hit", "tab": "t1", "x": 1, "y": 2},
                    {"type": "cursor", "tab": "t1", "x": 1, "y": 2}):
            ws.send_text(json.dumps(msg))
            assert json.loads(ws.receive_text())["code"] == "no_tab", msg["type"]

        ws.send_text(json.dumps({"type": "hide", "tab": "t1"}))
        assert json.loads(ws.receive_text())["code"] == "no_tab"


# ---------------------------------------------------------------------------
# Full browser: the reset and the file a page's input is given
# ---------------------------------------------------------------------------

class OpenTab:
    """A record in the tab table, which is all `has_tabs` looks at."""

    dead = False


def test_browser_reset_refused_with_open_tab(client, monkeypatch, tmp_path,
                                             no_browser_singleton):
    monkeypatch.setenv("HOME", str(tmp_path))
    profile = tmp_path / ".pockettui" / "chromium-profile"
    profile.mkdir(parents=True)

    fb = CH.FullBrowser.get()
    fb._tabs[("p1", "t1")] = OpenTab()

    # Somebody is looking at a page. Pulling the profile out from under it
    # would be indistinguishable from a crash, so the answer is no.
    r = client.post("/api/browser/reset", headers=HDRS)
    assert r.status_code == 409
    assert r.json() == {"error": "browser tabs are open"}
    assert profile.is_dir()

    assert client.post("/api/browser/reset").status_code == 401


def test_browser_reset_removes_profile(client, monkeypatch, tmp_path,
                                       no_browser_singleton):
    monkeypatch.setenv("HOME", str(tmp_path))
    profile = tmp_path / ".pockettui" / "chromium-profile" / "Default"
    profile.mkdir(parents=True)
    (profile / "Cookies").write_bytes(b"every login the phone has made")
    downloads = tmp_path / ".pockettui" / "downloads"
    downloads.mkdir(parents=True)
    (downloads / "report.pdf").write_bytes(b"the user's own file")

    fb = CH.FullBrowser.get()
    stopped = []

    async def shutdown(timeout=6.0):
        stopped.append(True)

    monkeypatch.setattr(fb, "shutdown", shutdown)
    r = client.post("/api/browser/reset", headers=HDRS)
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert stopped == [True]              # the browser is never left holding it
    assert not (tmp_path / ".pockettui" / "chromium-profile").exists()
    # The downloads are the user's files and not part of the session.
    assert (downloads / "report.pdf").read_bytes() == b"the user's own file"


def test_browser_upload_writes_under_uploads(client, monkeypatch, tmp_path):
    root = tmp_path / "uploads" / "browser"
    monkeypatch.setattr(A, "BROWSER_UPLOAD_DIR", root)

    # The name the page's file input will show is kept as the user sees it —
    # spaces and all — and is one path component whatever the phone sent.
    r = client.post("/api/browser/upload?name=../../notes%20v2.txt",
                    content=b"hello", headers=HDRS)
    body = r.json()
    assert body["name"] == "notes v2.txt" and body["bytes"] == 5
    staged = Path(body["path"])
    assert staged.read_bytes() == b"hello"
    assert staged.parent.parent == root
    assert staged.name == "notes v2.txt"

    # Each one lands in a directory of its own, so the same name twice is two
    # files rather than one overwritten.
    second = client.post("/api/browser/upload?name=notes v2.txt",
                         content=b"other", headers=HDRS).json()
    assert second["path"] != body["path"]
    assert Path(second["path"]).read_bytes() == b"other"

    assert client.post("/api/browser/upload", content=b"",
                       headers=HDRS).status_code == 400
    assert client.post("/api/browser/upload", content=b"x").status_code == 401


# ---------------------------------------------------------------------------
# Handing a page to the computer's own browser
# ---------------------------------------------------------------------------
# The proxy is what a tab uses by default, so the pages it cannot serve have to
# leave it rather than fail in it: a sign-in run by somebody else's identity
# provider, and Google refusing the address this computer speaks from. Both
# come back as one page, which asks the pane to move the tab to the browser
# running on the computer.

# What a browser sends when the frame is being sent to a page, as opposed to
# the page fetching something for itself.
NAV = {"sec-fetch-mode": "navigate", "sec-fetch-dest": "iframe",
       "accept": "text/html,application/xhtml+xml"}


def handoff_of(body: str) -> dict:
    """The message the handoff page posts, read back out of it."""
    m = re.search(r"var M = (\{.*?\});", body)
    assert m, body[:400]
    return json.loads(m.group(1).replace('type: "', '"type": "')
                      .replace(", url: ", ', "url": ')
                      .replace(", reason: ", ', "reason": '))


def test_a_sign_in_host_is_handed_to_the_computers_browser(client):
    # No site fixture and no network: the address alone settles it, so the
    # answer comes back before anything is fetched.
    body = mint(client, "https://accounts.google.com/signin/v2/identifier"
                        "?service=youtube").json()
    r = client.get(body["url"][len(PREFIX):], headers=NAV,
                   follow_redirects=False)
    assert r.status_code == 200
    assert "This page needs the computer's own browser" in r.text
    assert handoff_of(r.text) == {
        "type": "pockettui-stream",
        "url": "https://accounts.google.com/signin/v2/identifier?service=youtube",
        "reason": "login"}
    # Addressed to the shell that minted the token, and served on an origin of
    # its own like every other page this proxy prints.
    assert json.dumps(SHELL_ORIGIN) in r.text
    assert r.headers["content-security-policy"] == A.BROWSE_SANDBOX


def test_googles_rate_limit_page_is_handed_off(client):
    body = mint(client, "https://www.google.com/sorry/index?continue=x").json()
    r = client.get(body["url"][len(PREFIX):], headers=NAV,
                   follow_redirects=False)
    assert r.status_code == 200
    msg = handoff_of(r.text)
    assert msg["reason"] == "google_sorry"
    assert msg["url"] == "https://www.google.com/sorry/index?continue=x"


def test_the_scriptless_google_fallback_is_handed_off(client, site,
                                                      monkeypatch):
    # The fallback is a 200 on the address that was asked for, so it is the
    # body that says what it is — read here off a local stand-in, with the
    # host check answered for it.
    monkeypatch.setattr(A, "browse_is_google", lambda host: True)
    body, _ = opened(client, site)
    r = client.get(f"{base_of(body['token'], site)}/enablejs"[len(PREFIX):],
                   headers=NAV, follow_redirects=False)
    msg = handoff_of(r.text)
    assert msg["reason"] == "google_sorry"
    assert msg["url"] == f"http://127.0.0.1:{site}/enablejs"


def test_an_ordinary_page_on_a_google_host_is_served_as_it_was(client, site,
                                                               monkeypatch):
    monkeypatch.setattr(A, "browse_is_google", lambda host: True)
    body, r = opened(client, site)
    assert r.status_code == 200 and "pockettui-stream" not in r.text
    assert "<a id=\"root\"" in r.text


def test_only_a_navigation_is_handed_off(client, site, monkeypatch):
    # A page fetching something for itself gets the thing, not a page telling
    # the pane to move: a handoff in place of a script would break the page it
    # belongs to.
    monkeypatch.setitem(A.BROWSE_HANDOFF_HOSTS, "127.0.0.1", ())
    body, _ = opened(client, site)
    r = client.get(f"{base_of(body['token'], site)}/lib.js"[len(PREFIX):],
                   headers={"sec-fetch-mode": "no-cors",
                            "sec-fetch-dest": "script"})
    assert r.status_code == 200 and "pockettui-stream" not in r.text
    # And the navigation on the same host is handed off, so the difference is
    # the request and nothing else.
    nav = client.get(f"{base_of(body['token'], site)}/whoami"[len(PREFIX):],
                     headers=NAV)
    assert handoff_of(nav.text)["reason"] == "login"


def test_the_tab_flavour_is_never_handed_off(client, site, monkeypatch):
    # The unsandboxed flavour is already a page in a browser of the device's
    # own, which is where a handoff would send it.
    monkeypatch.setitem(A.BROWSE_HANDOFF_HOSTS, "127.0.0.1", ())
    body = mint_tab(client, f"http://127.0.0.1:{site}/").json()
    r = client.get(body["url"][len(PREFIX):], headers=NAV,
                   follow_redirects=False)
    assert r.status_code == 200 and "pockettui-stream" not in r.text


@pytest.mark.parametrize("host,path,handed", [
    ("accounts.google.com", "/", True),
    ("login.microsoftonline.com", "/common/oauth2/authorize", True),
    ("github.com", "/login", True),
    ("github.com", "/login/oauth/authorize", True),
    ("github.com", "/sessions/two-factor", True),
    # A whole site is not a sign-in because two of its paths are.
    ("github.com", "/anthropics/claude-code", False),
    ("github.com", "/loginhelp", False),
    # A dotted entry is the name and everything under it, and nothing else.
    ("acme.okta.com", "/oauth2/v1/authorize", True),
    ("okta.com.example.net", "/", False),
    ("news.example.net", "/login", False),
])
def test_which_addresses_are_the_computers_browsers(host, path, handed):
    assert A.browse_handoff_host(host, path) is handed


# ---------------------------------------------------------------------------
# A PDF, which the sandbox cannot draw
# ---------------------------------------------------------------------------
# Chrome's PDF viewer is a plugin and a sandboxed frame runs none, so a PDF
# opened in the pane's own flavour is answered with a card naming the document
# instead of bytes no frame will draw. The unsandboxed flavour, which is where
# the viewer runs, gets the bytes — typed and inline, whatever the target said.


def pdf_of(body: str) -> dict:
    """The message the PDF card posts, read back out of it."""
    m = re.search(r"var M = (\{.*?\});", body)
    assert m, body[:400]
    return json.loads(m.group(1))


def test_a_pdf_navigation_in_the_sandbox_is_a_card(client, site):
    body, _ = opened(client, site)
    r = client.get(f"{base_of(body['token'], site)}/doc.pdf"[len(PREFIX):],
                   headers=NAV, follow_redirects=False)
    assert r.status_code == 200
    assert "This PDF needs a real viewer" in r.text
    assert "Chrome cannot show PDFs inside the pane's sandbox" in r.text
    assert ">Open the PDF<" in r.text
    # The bytes stay behind: what goes out is a page, not a document no frame
    # in this flavour can draw.
    assert "%PDF-" not in r.text
    assert pdf_of(r.text) == {"type": "pockettui-pdf",
                              "url": f"http://127.0.0.1:{site}/doc.pdf",
                              "name": "doc.pdf"}
    # Addressed to the shell that minted the token, and served on an origin of
    # its own like every other page this proxy prints.
    assert json.dumps(SHELL_ORIGIN) in r.text
    assert r.headers["content-security-policy"] == A.BROWSE_SANDBOX
    assert r.headers["x-content-type-options"] == "nosniff"


def test_the_card_names_the_address_the_query_and_all(client, site):
    body, _ = opened(client, site)
    r = client.get(f"{base_of(body['token'], site)}/doc.pdf?page=3"[len(PREFIX):],
                   headers=NAV)
    assert pdf_of(r.text)["url"] == f"http://127.0.0.1:{site}/doc.pdf?page=3"


def test_a_pdf_sent_as_a_stream_is_named_by_its_disposition(client, site):
    """The headers say "bytes"; the name is the only thing that says PDF."""
    body, _ = opened(client, site)
    r = client.get(f"{base_of(body['token'], site)}/bin.pdf"[len(PREFIX):],
                   headers=NAV)
    assert r.status_code == 200
    assert pdf_of(r.text) == {"type": "pockettui-pdf",
                              "url": f"http://127.0.0.1:{site}/bin.pdf",
                              "name": "report.pdf"}


def test_a_pdf_with_nothing_but_its_bytes_is_recognised(client, site):
    """No usable type and no name, so the signature is what is left."""
    body, _ = opened(client, site)
    r = client.get(f"{base_of(body['token'], site)}/sniff"[len(PREFIX):],
                   headers=NAV)
    assert "This PDF needs a real viewer" in r.text
    assert pdf_of(r.text)["name"] == "sniff"


def test_a_binary_that_is_not_a_pdf_is_streamed_as_it_was(client, site):
    """The peek settles it either way: nine mebibytes still go through whole."""
    body, _ = opened(client, site)
    r = client.get(f"{base_of(body['token'], site)}/big.bin"[len(PREFIX):],
                   headers=NAV)
    assert r.status_code == 200
    assert r.content == BIG


def test_a_pdf_the_page_fetched_for_itself_is_bytes(client, site):
    """An <embed> is the page asking for a document to draw inside itself.

    A card in its place would put this proxy's own page inside somebody's
    layout, so only the frame's own navigation is answered with one.
    """
    body, _ = opened(client, site)
    r = client.get(f"{base_of(body['token'], site)}/doc.pdf"[len(PREFIX):],
                   headers={"sec-fetch-mode": "no-cors",
                            "sec-fetch-dest": "embed",
                            "accept": "*/*"})
    assert r.status_code == 200
    assert r.content == PDF
    assert r.headers["content-type"] == "application/pdf"


def test_a_page_embedding_a_pdf_is_served_as_a_page(client, site):
    body, _ = opened(client, site)
    base = base_of(body["token"], site)
    page = client.get(f"{base}/frame.html"[len(PREFIX):], headers=NAV)
    assert page.status_code == 200
    assert "pockettui-pdf" not in page.text
    assert f'src="{base}/doc.pdf"' in page.text
    # And the address it was rewritten to answers with the document itself.
    sub = client.get(f"{base}/doc.pdf"[len(PREFIX):],
                     headers={"sec-fetch-mode": "no-cors",
                              "sec-fetch-dest": "object", "accept": "*/*"})
    assert sub.content == PDF


def test_the_tab_flavour_gets_the_pdf_typed_and_inline(client, site):
    """The flavour whose frame is allowed the viewer, which is the whole point.

    The target sent a generic stream marked as an attachment; both would have
    the browser save the file rather than show it.
    """
    body = mint_tab(client, f"http://127.0.0.1:{site}/bin.pdf").json()
    r = client.get(body["url"][len(PREFIX):], headers=NAV,
                   follow_redirects=False)
    assert r.status_code == 200
    assert r.content == PDF
    assert r.headers["content-type"] == "application/pdf"
    assert r.headers["content-disposition"] == 'inline; filename="report.pdf"'
    assert "content-security-policy" not in r.headers


def test_the_tab_flavour_carries_a_range_request_through(client, site):
    """What Chrome's viewer does to a large document, hop by hop."""
    body = mint_tab(client, f"http://127.0.0.1:{site}/doc.pdf").json()
    path = body["url"][len(PREFIX):]
    head = client.get(path, headers=dict(NAV, Range="bytes=0-7"),
                      follow_redirects=False)
    assert head.status_code == 206
    assert head.content == PDF[:8]
    assert head.headers["content-range"] == f"bytes 0-7/{len(PDF)}"
    assert head.headers["accept-ranges"] == "bytes"
    assert head.headers["content-type"] == "application/pdf"
    # The tail, asked for the way a viewer asks for the trailer.
    tail = client.get(path, headers=dict(NAV, Range=f"bytes={len(PDF) - 6}-"),
                      follow_redirects=False)
    assert tail.status_code == 206
    assert tail.content == PDF[-6:]


def test_a_head_of_a_pdf_answers_with_the_targets_own_headers(client, site):
    """HEAD is answered before any of this: there is no body to read."""
    body = mint_tab(client, f"http://127.0.0.1:{site}/doc.pdf").json()
    r = client.head(body["url"][len(PREFIX):], follow_redirects=False)
    assert r.status_code == 200
    assert r.content == b""
    assert r.headers["content-length"] == str(len(PDF))


@pytest.mark.parametrize("disposition,name", [
    ('attachment; filename="report.pdf"', "report.pdf"),
    ("attachment; filename=report.pdf", "report.pdf"),
    ("inline; filename=report.pdf; size=1", "report.pdf"),
    ("attachment; filename*=UTF-8''r%C3%A9sum%C3%A9.pdf", "résumé.pdf"),
    # A target does not get to name a path, only a file.
    ('attachment; filename="../../etc/passwd"', "passwd"),
    ('attachment; filename="C:\\\\docs\\\\report.pdf"', "report.pdf"),
    ("attachment", ""),
    ("", ""),
])
def test_what_a_disposition_names(disposition, name):
    assert A.browse_disposition_name(disposition) == name


@pytest.mark.parametrize("base,disposition,is_pdf", [
    ("application/pdf", "", True),
    ("application/x-pdf", "", True),
    ("application/octet-stream", 'attachment; filename="a.pdf"', True),
    ("application/octet-stream", 'attachment; filename="a.zip"', False),
    ("application/octet-stream", "", False),
    # A type that says what it is is believed, name or no name.
    ("image/png", 'attachment; filename="a.pdf"', False),
    ("text/html", "", False),
])
def test_which_responses_read_as_a_pdf(base, disposition, is_pdf):
    assert A.browse_is_pdf(base, disposition) is is_pdf


@pytest.mark.parametrize("path,name", [
    ("/dir/resume.pdf", "resume.pdf"),
    ("/dir/my%20resume.pdf", "my resume.pdf"),
    ("/", "document.pdf"),
    ("", "document.pdf"),
])
def test_what_a_pdf_is_called_when_only_the_path_says(path, name):
    assert A.browse_pdf_name("", path) == name

# ---------------------------------------------------------------------------
# What the pane's hint bar is drawn from
# ---------------------------------------------------------------------------
# A page the proxy served but that plainly did not work is worth naming the key
# that would fix it (browserHint, 42-browser.js). Two of the four things that
# raise that bar are read here: the status the document was answered with, and
# whether the body reads as a wall. Both ride into the shim's configuration and
# go out with its landing report.

def test_a_wall_reaches_the_shim_as_a_status_and_a_kind(client, site):
    body, _ = opened(client, site)
    r = client.get(f"{base_of(body['token'], site)}/wall"[len(PREFIX):],
                   headers=NAV)
    assert r.status_code == 403
    cfg = shim_cfg(r.text)
    assert cfg["status"] == 403
    assert cfg["wall"] == "captcha"


def test_a_refusal_with_nothing_to_say_is_a_status_and_no_kind(client, site):
    # The status is the whole of what is known, and the bar reads the same off
    # it: what the kind adds is a line in the log.
    body, _ = opened(client, site)
    r = client.get(f"{base_of(body['token'], site)}/forbidden"[len(PREFIX):],
                   headers=NAV)
    assert r.status_code == 403
    assert shim_cfg(r.text)["status"] == 403
    assert shim_cfg(r.text)["wall"] is None


def test_an_ordinary_page_carries_its_own_status_and_no_wall(client, site):
    # And is never scanned for one: "enable JavaScript" sits in a <noscript> on
    # half the web, and a 200 that mentions it is not a wall.
    body, _ = opened(client, site)
    r = client.get(f"{base_of(body['token'], site)}/"[len(PREFIX):],
                   headers=NAV)
    assert r.status_code == 200
    cfg = shim_cfg(r.text)
    assert cfg["status"] == 200
    assert cfg["wall"] is None


def test_a_document_fetched_by_the_page_is_never_scanned(client, site):
    """A subresource is not what the user is looking at, so it says nothing.

    The same address, asked for the two ways: as the frame's own page, and as
    something the page fetched for itself.
    """
    body, _ = opened(client, site)
    base = base_of(body["token"], site)
    nav = client.get(f"{base}/wall"[len(PREFIX):], headers=NAV)
    sub = client.get(f"{base}/wall"[len(PREFIX):],
                     headers={"sec-fetch-mode": "cors",
                              "sec-fetch-dest": "empty"})
    assert shim_cfg(nav.text)["status"] == 403
    assert shim_cfg(sub.text)["status"] == 0
    assert shim_cfg(sub.text)["wall"] is None


@pytest.mark.parametrize("status,body,kind", [
    (403, b"<html>nothing here</html>", None),
    (403, b'<script src="https://www.google.com/recaptcha/api.js">', "captcha"),
    (403, b"<html>hcaptcha</html>", "captcha"),
    (503, b"<html>/cdn-cgi/challenge-platform/h/b/orchestrate</html>", "captcha"),
    (429, b"<html>cf_chl_opt</html>", "captcha"),
    (403, b"<html>Turnstile</html>", "captcha"),
    (403, b"<html>Please enable JavaScript to continue</html>", "js"),
    (451, b"<html>Access Denied</html>", "js"),
    # A status that is the site answering the request it was asked: not a wall,
    # whatever the page happens to say.
    (200, b"<html>Please enable JavaScript</html>", None),
    (404, b"<html>recaptcha</html>", None),
])
def test_which_bodies_read_as_a_wall(status, body, kind):
    assert A.browse_wall(status, body) == kind


def test_only_the_head_of_a_body_is_read_for_a_wall():
    # A wall is a small page that says what it is at the top of itself; a long
    # page that mentions one of the words further down is not one.
    far = b"x" * (A.BROWSE_WALL_SCAN + 64) + b"turnstile"
    assert A.browse_wall(403, far) is None
    assert A.browse_wall(403, b"turnstile" + far) == "captcha"
