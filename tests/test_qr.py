"""Tests for the vendored QR generator and the pairing payload it encodes.

The installer builds the payload in a Python heredoc, not in this codebase's
own modules, so there is nothing to import and call directly — the shape is
instead reproduced here as a small helper that mirrors the heredoc exactly,
and the round-trip is checked against app.py's own token/address contracts so
a change to either one breaks this test rather than silently drifting.
"""

import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as A  # noqa: E402
import qrcodegen  # noqa: E402


def _build_pair_payload(token, address=None):
    """Mirrors the install.sh heredoc's payload construction exactly:
    {"v": 1, "a": <address>, "t": <token>} (a omitted when None), base64url
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
