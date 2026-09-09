"""Tests for the vendored QR generator, the pairing payload, and /api/pair_qr.svg.

app.pair_payload() is what both callers now use — the installer's heredoc at
the end of an install and the Settings card's QR route — so the shape is kept
honest here by an independent mirror of it rather than by the helper testing
itself, and the round-trip is checked against app.py's own token/address
contracts so a change to either one breaks this test rather than silently
drifting.
"""

import base64
import json
import re
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as A  # noqa: E402
import qrcodegen  # noqa: E402

TOKEN = "ABCDEFGHIJ"


@pytest.fixture
def client():
    return TestClient(A.app)


def _build_pair_payload(token, address=None):
    """Mirrors app.pair_payload()'s construction independently:
    {"v": 1, "t": <token>, "a": <address>} (a omitted when None), base64url
    with no padding.
    """
    payload = {"v": 1, "t": token}
    if address is not None:
        payload["a"] = address
    enc = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).rstrip(b"=")
    return enc.decode()


def _decode_pair_payload(enc):
    """The client side's decode: restore padding-free base64url, then JSON."""
    pad = "=" * (-len(enc) % 4)
    return json.loads(base64.urlsafe_b64decode(enc + pad))


def test_vendored_qrcodegen_encodes_a_pair_url():
    # Deterministic, no I/O: encoding a representative pairing URL succeeds and
    # produces a real square grid with at least one dark module.
    token = "ABCDEFGHIJ"
    enc = _build_pair_payload(token, "myhost.example/pockettui")
    url = f"https://pockettui.com/app/#pair={enc}"

    qr = qrcodegen.QrCode.encode_text(url, qrcodegen.QrCode.Ecc.MEDIUM)
    size = qr.get_size()

    assert size > 0
    assert any(
        qr.get_module(x, y) for y in range(size) for x in range(size)
    )


def test_payload_round_trip_ts_served_branch():
    # TS_SERVED=1: address is the tailnet host + /pockettui, no scheme — the
    # settings sheet's normalizeBackend() is the one that adds "https://".
    token = A.generate_token()
    address = "mymachine.tailnet.ts.net/pockettui"
    enc = _build_pair_payload(token, address)

    decoded = _decode_pair_payload(enc)
    assert decoded == {"v": 1, "t": token, "a": address}

    # The token the client reads out of "t" must survive normalize_token()
    # unchanged — it is already canonical (10-char base32, no dash) because
    # the installer reads it via app.read_token(), not the "XXXXX-XXXXX"
    # display string.
    assert A.normalize_token(decoded["t"]) == token

    # The address must be exactly what normalizeBackend(url, "") expects as
    # input: a bare "host[:port][/path]" string with no scheme forced, since
    # normalizeBackend() itself prepends "https://" when one is missing.
    assert not decoded["a"].startswith("http")


def test_payload_round_trip_lan_branch():
    # LAN only: no "a" at all — the backend serves the same shell, so the
    # client's same-origin default needs no address.
    token = A.generate_token()
    enc = _build_pair_payload(token, address=None)

    decoded = _decode_pair_payload(enc)
    assert decoded == {"v": 1, "t": token}
    assert A.normalize_token(decoded["t"]) == token


def test_payload_has_no_base64_padding():
    enc = _build_pair_payload(A.generate_token(), "host/pockettui")
    assert "=" not in enc


def test_pair_payload_matches_the_installer_mirror():
    # Both branches of the installer's printout: a served tailnet address, and
    # the LAN page the backend serves itself, which carries no address at all.
    token = A.generate_token()
    address = "mymachine.tailnet.ts.net/pockettui"
    assert A.pair_payload(address, token) == _build_pair_payload(token, address)
    assert A.pair_payload(None, token) == _build_pair_payload(token, None)
    # An empty address is the same nothing as None, so the LAN branch reads the
    # same whether the caller passes "" or leaves it out.
    assert A.pair_payload("", token) == _build_pair_payload(token, None)


def test_pair_url_appends_the_fragment():
    token = A.generate_token()
    base = "https://pockettui.com/app/"
    url = A.pair_url(base, "host/pockettui", token)

    assert url.startswith(base + "#pair=")
    enc = url.split("#pair=", 1)[1]
    assert _decode_pair_payload(enc) == {"v": 1, "t": token, "a": "host/pockettui"}


def test_qr_svg_is_a_square_svg_with_dark_modules():
    text = A.pair_url("https://pockettui.com/app/", "host/pockettui", TOKEN)
    svg = A.qr_svg(text)

    qr = qrcodegen.QrCode.encode_text(text, qrcodegen.QrCode.Ecc.MEDIUM)
    size = qr.get_size()
    dark = sum(qr.get_module(x, y) for y in range(size) for x in range(size))

    m = re.search(r'viewBox="0 0 (\d+) (\d+)"', svg)
    assert m, svg[:200]
    # The default border is the four-module quiet zone the spec asks for, on
    # every side of a square symbol.
    assert m.group(1) == m.group(2) == str(size + 8)
    assert svg.count("h1v1h-1z") == dark
    # Self-contained: nothing to fetch, which is what lets it render from a
    # blob: URL in the settings sheet.
    assert "http://www.w3.org/2000/svg" in svg
    assert "xlink" not in svg and "<image" not in svg


def test_pair_qr_route_refuses_without_the_token_header(client, monkeypatch):
    monkeypatch.setattr(A, "AUTH_TOKEN", TOKEN)
    r = client.get("/api/pair_qr.svg?base=https%3A%2F%2Fpockettui.com%2Fapp%2F")
    assert r.status_code == 401


def test_pair_qr_route_draws_the_payload_the_helpers_build(client, monkeypatch):
    monkeypatch.setattr(A, "AUTH_TOKEN", TOKEN)
    r = client.get(
        "/api/pair_qr.svg",
        params={"base": "https://pockettui.com/app/", "a": "host/pockettui"},
        headers={A.TOKEN_HEADER: TOKEN},
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/svg+xml")
    assert r.headers["cache-control"] == "no-store"
    # The token in the drawn payload is the server's own, never one the caller
    # passed: it is not in the query string at all.
    assert r.text == A.qr_svg(
        A.pair_url("https://pockettui.com/app/", "host/pockettui", TOKEN)
    )


def test_pair_qr_route_omits_the_address_when_none_is_given(client, monkeypatch):
    monkeypatch.setattr(A, "AUTH_TOKEN", TOKEN)
    r = client.get(
        "/api/pair_qr.svg",
        params={"base": "https://pockettui.com/app/"},
        headers={A.TOKEN_HEADER: TOKEN},
    )
    assert r.status_code == 200
    assert r.text == A.qr_svg(A.pair_url("https://pockettui.com/app/", None, TOKEN))


def test_pair_qr_route_rejects_a_base_that_is_not_a_url(client, monkeypatch):
    monkeypatch.setattr(A, "AUTH_TOKEN", TOKEN)
    h = {A.TOKEN_HEADER: TOKEN}
    for base in ("pockettui.com/app/", "", "https://pockettui.com/app/#pair=x",
                 "javascript:alert(1)"):
        r = client.get("/api/pair_qr.svg", params={"base": base}, headers=h)
        assert r.status_code == 400, base
        assert r.json() == {"error": "bad_base"}
    r = client.get(
        "/api/pair_qr.svg",
        params={"base": "https://pockettui.com/app/", "a": "host/pockettui#x"},
        headers=h,
    )
    assert r.status_code == 400
    assert r.json() == {"error": "bad_address"}


def test_pair_qr_route_has_nothing_to_draw_without_a_token(client, monkeypatch):
    # --no-auth: there is no pairing code to hand another device, so the card
    # gets a 404 and hides itself rather than showing a QR that pairs nothing.
    monkeypatch.setattr(A, "AUTH_TOKEN", None)
    r = client.get("/api/pair_qr.svg", params={"base": "https://pockettui.com/app/"})
    assert r.status_code == 404
    assert r.json() == {"error": "no_token"}


def test_pair_qr_is_advertised_as_a_capability():
    assert A.server_capabilities()["pair_qr"] is True
