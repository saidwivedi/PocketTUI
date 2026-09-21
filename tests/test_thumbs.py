"""Tests for the explorer grid's thumbnail route.

The fixtures are real files, encoded by the same ffmpeg the route renders
with: a thumbnailer tested against hand-written magic bytes would pass while
producing nothing a browser could show. The whole module skips where that
binary is absent, which is the one install where the route answers nothing.
THUMB_DIR is monkeypatched into tmp_path throughout, so the suite never writes
to the real ~/.pockettui/thumbs.
"""

import os
import struct
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as A  # noqa: E402

pytestmark = pytest.mark.skipif(A.ffmpeg_exe() is None,
                                reason="no ffmpeg to render thumbnails with")


@pytest.fixture
def client():
    return TestClient(A.app)


@pytest.fixture(autouse=True)
def thumbs(tmp_path, monkeypatch):
    """The cache directory, somewhere pytest will clean up."""
    d = tmp_path / "thumbs"
    monkeypatch.setattr(A, "THUMB_DIR", d)
    return d


def encode(*args) -> None:
    subprocess.run([A.ffmpeg_exe(), "-nostdin", "-hide_banner", "-loglevel",
                    "error", "-y", *[str(a) for a in args]], check=True,
                   capture_output=True, timeout=30)


@pytest.fixture
def shot(tmp_path):
    """A 64x48 PNG — already smaller than a tile, so nothing is scaled."""
    p = tmp_path / "shot.png"
    encode("-f", "lavfi", "-i", "color=c=red:s=64x48", "-frames:v", "1", p)
    return p


@pytest.fixture
def clip(tmp_path):
    """Two seconds of video, long enough for the frame grab at one second."""
    p = tmp_path / "clip.mp4"
    encode("-f", "lavfi", "-i", "testsrc=size=64x48:rate=10", "-t", "2", p)
    return p


def jpeg_size(data: bytes) -> tuple[int, int]:
    """(width, height) off the JPEG's own start-of-frame marker.

    Parsed by hand rather than with Pillow: the point of rendering through
    ffmpeg is that this install needs no second image library, and a test that
    installed one would quietly undo that.
    """
    assert data[:2] == b"\xff\xd8", "not a JPEG"
    i = 2
    while i < len(data):
        assert data[i] == 0xFF, f"lost the marker chain at {i}"
        marker, length = data[i + 1], struct.unpack(">H", data[i + 2:i + 4])[0]
        # Every SOFn carries the dimensions; C4/C8/CC are other things.
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return w, h
        i += 2 + length
    raise AssertionError("no start-of-frame in the JPEG")


def thumb(client, path, **kw):
    return client.get("/api/fs/thumb", params={"path": str(path)}, **kw)


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

def test_image_thumb_is_a_jpeg_within_the_tile(client, shot):
    r = thumb(client, shot)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "image/jpeg"
    assert max(jpeg_size(r.content)) <= A.THUMB_MAX_PX


def test_a_large_image_is_scaled_down_keeping_its_aspect(client, tmp_path):
    p = tmp_path / "wide.png"
    encode("-f", "lavfi", "-i", "color=c=blue:s=600x400", "-frames:v", "1", p)
    w, h = jpeg_size(thumb(client, p).content)
    assert w == A.THUMB_MAX_PX
    assert abs(w / h - 600 / 400) < 0.02


def test_video_thumb_is_a_jpeg(client, clip):
    r = thumb(client, clip)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "image/jpeg"
    assert max(jpeg_size(r.content)) <= A.THUMB_MAX_PX


def test_a_clip_shorter_than_the_seek_still_renders(client, tmp_path):
    """The retry at zero: seeking a second into a third of a second writes
    nothing, and ffmpeg calls that a success."""
    p = tmp_path / "blink.mp4"
    encode("-f", "lavfi", "-i", "testsrc=size=64x48:rate=10", "-t", "0.3", p)
    r = thumb(client, p)
    assert r.status_code == 200, r.text
    assert jpeg_size(r.content) == (64, 48)


def test_ffmpeg_failure_is_a_422(client, tmp_path):
    p = tmp_path / "broken.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"not an image")
    r = thumb(client, p)
    assert r.status_code == 422
    assert r.json()["error"] == "thumb_failed"


# ---------------------------------------------------------------------------
# cache
# ---------------------------------------------------------------------------

def test_the_second_call_is_served_from_disk(client, shot, thumbs, monkeypatch):
    first = thumb(client, shot)
    assert first.status_code == 200
    cached = list(thumbs.glob("*.jpg"))
    assert len(cached) == 1
    assert cached[0].read_bytes() == first.content

    runs = []
    real = subprocess.run
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **kw: (runs.append(a), real(*a, **kw))[1])
    second = thumb(client, shot)
    assert second.status_code == 200
    assert second.content == first.content
    assert runs == []


def test_an_edited_file_renders_again(client, shot, thumbs):
    first = thumb(client, shot)
    encode("-f", "lavfi", "-i", "color=c=green:s=64x48", "-frames:v", "1", shot)
    second = thumb(client, shot)
    assert second.status_code == 200
    assert second.headers["etag"] != first.headers["etag"]
    assert len(list(thumbs.glob("*.jpg"))) == 2


def test_the_oldest_entries_are_evicted(client, shot, thumbs, monkeypatch):
    monkeypatch.setattr(A, "THUMB_KEEP", 3)
    thumbs.mkdir(parents=True)
    old = []
    for n in range(5):
        p = thumbs / f"{n:040x}.jpg"
        p.write_bytes(b"stale")
        os.utime(p, (time.time() - 100 + n, time.time() - 100 + n))
        old.append(p)

    r = thumb(client, shot)
    assert r.status_code == 200
    left = sorted(p.name for p in thumbs.glob("*.jpg"))
    assert len(left) == 3
    # The tile just rendered survives, and the two oldest stale ones are gone.
    assert not old[0].exists() and not old[1].exists()
    assert old[4].name in left


def test_an_unwritable_cache_still_serves_the_thumbnail(client, shot, thumbs):
    thumbs.parent.mkdir(parents=True, exist_ok=True)
    thumbs.write_bytes(b"not a directory")
    r = thumb(client, shot)
    assert r.status_code == 200
    assert max(jpeg_size(r.content)) <= A.THUMB_MAX_PX


# ---------------------------------------------------------------------------
# caching headers
# ---------------------------------------------------------------------------

def test_the_headers_let_the_browser_hold_the_tile(client, shot):
    r = thumb(client, shot)
    assert r.headers["cache-control"] == "private, max-age=3600"
    assert r.headers["etag"].startswith('"') and r.headers["etag"].endswith('"')


def test_a_revalidation_is_a_304(client, shot):
    etag = thumb(client, shot).headers["etag"]
    r = thumb(client, shot, headers={"If-None-Match": etag})
    assert r.status_code == 304
    assert r.content == b""
    assert r.headers["etag"] == etag
    assert r.headers["cache-control"] == "private, max-age=3600"
    # A tag the client no longer holds is not a match.
    assert thumb(client, shot, headers={"If-None-Match": '"stale"'}).status_code == 200


# ---------------------------------------------------------------------------
# what it refuses
# ---------------------------------------------------------------------------

def test_vector_art_is_unsupported(client, tmp_path):
    p = tmp_path / "logo.svg"
    p.write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
    r = thumb(client, p)
    assert r.status_code == 415
    assert r.json()["error"] == "unsupported_type"


def test_a_text_file_is_unsupported(client, tmp_path):
    p = tmp_path / "notes.txt"
    p.write_text("no picture here\n")
    assert thumb(client, p).status_code == 415


def test_a_missing_file_is_404(client, tmp_path):
    r = thumb(client, tmp_path / "absent.png")
    assert r.status_code == 404
    assert r.json()["error"] == "not_found"


def test_a_directory_is_404(client, tmp_path):
    (tmp_path / "album.png").mkdir()
    assert thumb(client, tmp_path / "album.png").status_code == 404
    assert thumb(client, tmp_path).status_code == 404


def test_a_relative_path_is_refused(client):
    r = client.get("/api/fs/thumb", params={"path": "pictures/shot.png"})
    assert r.status_code == 400
    assert r.json()["error"] == "bad_path"


def test_it_is_gated_like_its_neighbours(client, shot, monkeypatch):
    monkeypatch.setattr(A, "LIMITER", A.AuthLimiter())
    monkeypatch.setattr(A, "AUTH_TOKEN", "ABCDEFGHIJ")
    unauthenticated = thumb(client, shot).status_code
    assert unauthenticated == client.get(
        "/api/fs/read", params={"path": str(shot)}).status_code == 401


# ---------------------------------------------------------------------------
# capability
# ---------------------------------------------------------------------------

def test_the_capability_follows_ffmpeg(client, monkeypatch):
    assert "thumbs" in client.get("/api/version").json()["capabilities"]
    monkeypatch.setattr(A, "ffmpeg_exe", lambda: "/usr/bin/ffmpeg")
    assert A.server_capabilities()["thumbs"] is True
    monkeypatch.setattr(A, "ffmpeg_exe", lambda: None)
    assert A.server_capabilities()["thumbs"] is False
