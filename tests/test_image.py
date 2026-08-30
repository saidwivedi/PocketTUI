"""Tests for the pasted-image staging route.

Everything drives store_image directly, the way the transcribe suite drives
transcribe: the route is a thin wrapper around it, and the interesting parts —
what counts as an image, where the file lands, what gets pruned — are all in
the worker. IMAGE_DIR is monkeypatched into tmp_path throughout, so the suite
never writes to the real ~/.pockettui/images.
"""

import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as A  # noqa: E402


def body(response):
    """The decoded JSON of a JSONResponse, which holds bytes rather than a dict."""
    import json
    return json.loads(response.body)


# Magic bytes plus padding, built by hand: what the sniffer reads is the head of
# the file, so a real encoder would only make these slower to write.
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
GIF87 = b"GIF87a" + b"\x00" * 64
GIF89 = b"GIF89a" + b"\x00" * 64
WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 64


@pytest.fixture
def staged(tmp_path, monkeypatch):
    """The staging directory, somewhere pytest will clean up."""
    d = tmp_path / "images"
    monkeypatch.setattr(A, "IMAGE_DIR", d)
    return d


# ---------------------------------------------------------------------------
# What lands on disk
# ---------------------------------------------------------------------------

def test_a_png_is_staged_whole_and_reported_back(staged):
    r = A.store_image(PNG, "work", "phone")
    assert r.status_code == 200
    data = body(r)
    p = Path(data["path"])
    assert p.suffix == ".png"
    assert p.parent == staged
    assert p.read_bytes() == PNG
    assert data["bytes"] == len(PNG)


@pytest.mark.parametrize("raw, ext", [
    (JPEG, ".jpg"),
    (GIF87, ".gif"),
    (GIF89, ".gif"),
    (WEBP, ".webp"),
])
def test_each_format_lands_under_its_own_extension(staged, raw, ext):
    """Both GIF versions are one format; the extension is what /api/file types."""
    r = A.store_image(raw, "", "")
    assert r.status_code == 200
    assert Path(body(r)["path"]).suffix == ext


def test_a_missing_staging_directory_is_created(staged, monkeypatch):
    """Unlike /api/fs/upload, which 404s: the client named no path to get wrong."""
    nested = staged / "not" / "there" / "yet"
    monkeypatch.setattr(A, "IMAGE_DIR", nested)
    r = A.store_image(PNG, "", "")
    assert r.status_code == 200
    assert nested.is_dir()
    assert Path(body(r)["path"]).parent == nested


def test_the_staged_name_survives_an_unbracketed_paste(staged):
    """No whitespace anywhere: the client types this path straight at a prompt."""
    path = body(A.store_image(PNG, "", ""))["path"]
    assert re.search(r"/paste-\d{8}-\d{6}-[0-9a-f]{6}\.(png|jpg|gif|webp)$", path)
    assert not re.search(r"\s", path)


# ---------------------------------------------------------------------------
# What gets refused
# ---------------------------------------------------------------------------

def test_an_empty_body_is_refused_before_anything_is_written(staged):
    r = A.store_image(b"", "", "")
    assert r.status_code == 422
    assert body(r) == {"error": "empty"}
    assert not staged.exists()


def test_text_bytes_are_not_an_image(staged):
    """The function takes no Content-Type, so the client's could never have lied
    its way past this."""
    r = A.store_image(b"hello, not an image", "", "")
    assert r.status_code == 422
    assert body(r) == {"error": "not_image"}


def test_a_truncated_header_is_refused_rather_than_crashing(staged):
    """Slicing past the end of a short body is the sniffer's whole short-input
    story — three bytes must answer, not raise."""
    r = A.store_image(b"\x89PN", "", "")
    assert r.status_code == 422
    assert body(r) == {"error": "not_image"}


def test_an_oversize_paste_is_refused(staged, monkeypatch):
    """The cap, moved down to where a test can reach it without 15MB of memory."""
    monkeypatch.setattr(A, "MAX_IMAGE_BYTES", 100)
    r = A.store_image(PNG + b"\x00" * 101, "", "")
    assert r.status_code == 413
    assert body(r) == {"error": "too_large"}


# ---------------------------------------------------------------------------
# Pruning
# ---------------------------------------------------------------------------

def test_a_paste_prunes_the_dir_back_to_the_newest_keep(staged):
    """Older pastes go, and the one that just arrived is never the one evicted."""
    staged.mkdir(parents=True)
    old = []
    for i in range(35):
        p = staged / f"paste-20250101-0000{i:02d}-aaaaaa.png"
        p.write_bytes(PNG)
        os.utime(p, (1_700_000_000 + i, 1_700_000_000 + i))
        old.append(p)

    fresh = Path(body(A.store_image(PNG, "", ""))["path"])

    survivors = sorted(staged.glob("paste-*"))
    assert len(survivors) == A.IMAGE_KEEP
    assert fresh in survivors
    # The newest of the pre-existing files fill the rest; the oldest are gone.
    assert set(old[-(A.IMAGE_KEEP - 1):]) < set(survivors)
    assert not any(p.exists() for p in old[:-(A.IMAGE_KEEP - 1)])


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

def test_the_route_sits_under_the_authenticated_prefix():
    """/api/ is what the require_token middleware keys on, so the path itself is
    the whole auth story — worth pinning without standing up a client."""
    assert "/api/image" in {r.path for r in A.app.routes}
