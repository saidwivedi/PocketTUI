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
def photo(tmp_path):
    """A JPEG source, whose container cannot carry alpha either way."""
    p = tmp_path / "photo.jpg"
    encode("-f", "lavfi", "-i", "color=c=red:s=64x48", "-frames:v", "1", p)
    return p


@pytest.fixture
def cutout(tmp_path):
    """An RGBA PNG — the app icon with rounded corners, in miniature."""
    p = tmp_path / "cutout.png"
    encode("-f", "lavfi", "-i", "color=c=red@0.0:s=64x48,format=rgba",
           "-frames:v", "1", p)
    assert png_info(p.read_bytes())[2] == 6, "the fixture itself lost its alpha"
    return p


def tiny_pdf(width: int = 612, height: int = 792) -> bytes:
    """One US-letter page with a red rectangle on it, written out by hand.

    The fixtures around it are real files for the reason the module docstring
    gives, and so is this one — it is a PDF poppler and Ghostscript both
    rasterise. Written here rather than generated because no PDF library is
    installed and adding one to render four objects would be a dependency for
    nothing. The cross-reference offsets are measured as the body is built, so
    the file is valid rather than merely plausible.
    """
    stream = b"0.9 0.2 0.2 rg 72 72 200 300 re f\n"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}]"
         " /Contents 4 0 R >>").encode(),
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"endstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for n, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % n + body + b"\nendobj\n"
    start = len(out)
    out += b"xref\n0 %d\n" % (len(objects) + 1)
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += (b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
            % (len(objects) + 1, start))
    return bytes(out)


@pytest.fixture
def paper(tmp_path):
    """A one-page PDF, whose tile is that page."""
    p = tmp_path / "paper.pdf"
    p.write_bytes(tiny_pdf())
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


def png_info(data: bytes) -> tuple[int, int, int]:
    """(width, height, colour type) off the PNG's IHDR, parsed the same way.

    Colour type 6 is truecolour with alpha, which is what says the tile kept
    the transparency the source had.
    """
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    assert data[12:16] == b"IHDR", "IHDR is not the first chunk"
    w, h, _depth, colour = struct.unpack(">IIBB", data[16:26])
    return w, h, colour


def thumb(client, path, **kw):
    return client.get("/api/fs/thumb", params={"path": str(path)}, **kw)


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

def test_image_thumb_is_a_png_within_the_tile(client, shot):
    r = thumb(client, shot)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "image/png"
    assert max(png_info(r.content)[:2]) <= A.THUMB_MAX_PX


def test_a_jpeg_source_stays_a_jpeg(client, photo):
    r = thumb(client, photo)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "image/jpeg"
    assert max(jpeg_size(r.content)) <= A.THUMB_MAX_PX


def test_a_transparent_source_keeps_its_alpha(client, cutout, thumbs):
    """The rounded-corner app icon: a JPEG tile would come back black there."""
    r = thumb(client, cutout)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "image/png"
    w, h, colour = png_info(r.content)
    assert colour == 6
    assert max(w, h) <= A.THUMB_MAX_PX
    cached = list(thumbs.glob("*"))
    assert [c.suffix for c in cached] == [".png"]
    assert cached[0].read_bytes() == r.content

    second = thumb(client, cutout)
    assert second.content == r.content
    assert list(thumbs.glob("*")) == cached


def test_a_large_image_is_scaled_down_keeping_its_aspect(client, tmp_path):
    p = tmp_path / "wide.png"
    encode("-f", "lavfi", "-i", "color=c=blue:s=600x400", "-frames:v", "1", p)
    w, h, _colour = png_info(thumb(client, p).content)
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


@pytest.mark.skipif(A.pdf_renderer() is None,
                    reason="no pdftoppm, gs or sips to rasterise a page with")
def test_pdf_thumb_is_page_one_within_the_tile(client, paper, thumbs):
    """The cover, at the same bound and through the same encoder as a photo."""
    r = thumb(client, paper)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "image/jpeg"
    w, h = jpeg_size(r.content)
    assert max(w, h) == A.THUMB_MAX_PX
    # The page's own shape, which is what says the tile came from its geometry
    # rather than from some default the renderer picked.
    assert abs(w / h - 612 / 792) < 0.02
    # A page carries no alpha, so the tile is a JPEG like a video frame's.
    assert [c.suffix for c in thumbs.glob("*")] == [".jpg"]


@pytest.mark.skipif(A.pdf_renderer() is None, reason="no PDF renderer")
def test_an_unreadable_pdf_is_a_422(client, tmp_path):
    p = tmp_path / "torn.pdf"
    p.write_bytes(b"%PDF-1.4\nnot really\n")
    r = thumb(client, p)
    assert r.status_code == 422
    assert r.json()["error"] == "thumb_failed"


def test_a_pdf_is_unsupported_where_nothing_can_render_one(client, paper,
                                                           monkeypatch):
    """An install with no renderer answers the way it does for vector art:
    a fact about what it can make, not a render that failed."""
    monkeypatch.setattr(A, "pdf_renderer", lambda: None)
    r = thumb(client, paper)
    assert r.status_code == 415
    assert r.json()["error"] == "unsupported_type"


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
    cached = list(thumbs.glob("*.png"))
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
    assert len(list(thumbs.glob("*.png"))) == 2


def test_the_oldest_entries_are_evicted(client, shot, thumbs, monkeypatch):
    """The bound is on the cache, not on each suffix: two PNGs and two JPEGs
    plus the tile just rendered leave three files between them."""
    monkeypatch.setattr(A, "THUMB_KEEP", 3)
    thumbs.mkdir(parents=True)
    old = []
    for n, suffix in enumerate((".png", ".jpg", ".png", ".jpg")):
        p = thumbs / f"{n:040x}{suffix}"
        p.write_bytes(b"stale")
        os.utime(p, (time.time() - 100 + n, time.time() - 100 + n))
        old.append(p)

    r = thumb(client, shot)
    assert r.status_code == 200
    left = sorted(p.name for p in thumbs.iterdir())
    assert len(left) == 3
    # The tile just rendered survives, and the two oldest stale ones are gone.
    assert not old[0].exists() and not old[1].exists()
    assert old[2].name in left and old[3].name in left


def test_an_unwritable_cache_still_serves_the_thumbnail(client, shot, thumbs):
    thumbs.parent.mkdir(parents=True, exist_ok=True)
    thumbs.write_bytes(b"not a directory")
    r = thumb(client, shot)
    assert r.status_code == 200
    assert max(png_info(r.content)[:2]) <= A.THUMB_MAX_PX


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


def test_the_pdf_capability_is_its_own(client, monkeypatch):
    """A machine can have ffmpeg and no way to rasterise a page, so the grid
    is told about the two separately."""
    assert "pdf_thumbs" in client.get("/api/version").json()["capabilities"]
    monkeypatch.setattr(A, "pdf_renderer", lambda: ("pdftoppm", "/usr/bin/pdftoppm"))
    assert A.server_capabilities()["pdf_thumbs"] is True
    monkeypatch.setattr(A, "pdf_renderer", lambda: None)
    assert A.server_capabilities()["pdf_thumbs"] is False
    assert A.server_capabilities()["thumbs"] is True
