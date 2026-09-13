"""Tests for the attachment staging route.

Everything drives store_upload directly, the way the image suite drives
store_image: the route is a thin wrapper around it, and the interesting parts
(what a phone's filename becomes on disk, what gets refused, what gets pruned)
are all in the worker. UPLOAD_DIR is monkeypatched into tmp_path throughout, so
the suite never writes to the real ~/.pockettui/uploads.
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


BLOB = b"attachment bytes, of no particular format\n"


@pytest.fixture
def staged(tmp_path, monkeypatch):
    """The staging directory, somewhere pytest will clean up."""
    d = tmp_path / "uploads"
    monkeypatch.setattr(A, "UPLOAD_DIR", d)
    return d


# ---------------------------------------------------------------------------
# What lands on disk
# ---------------------------------------------------------------------------

def test_a_file_is_staged_whole_and_reported_back(staged):
    r = A.store_upload(BLOB, "notes.txt", "work", "phone")
    assert r.status_code == 200
    data = body(r)
    p = Path(data["path"])
    assert p.parent == staged
    assert p.read_bytes() == BLOB
    assert data["bytes"] == len(BLOB)
    assert data["name"] == p.name


def test_the_stamp_makes_two_uploads_of_one_name_distinct(staged):
    """Same name twice, two files: the hex tail is what keeps them apart within
    the same second."""
    a = body(A.store_upload(BLOB, "notes.txt", "", ""))["path"]
    b = body(A.store_upload(BLOB, "notes.txt", "", ""))["path"]
    assert a != b
    assert sorted(p.name for p in staged.iterdir()) == sorted(
        [Path(a).name, Path(b).name])


def test_the_staged_name_survives_an_unbracketed_paste(staged):
    """No whitespace anywhere: the client types this path straight at a prompt."""
    path = body(A.store_upload(BLOB, "notes.txt", "", ""))["path"]
    assert re.search(r"/notes-\d{8}-\d{6}-[0-9a-f]{6}\.txt$", path)
    assert not re.search(r"\s", path)


def test_a_missing_staging_directory_is_created(staged, monkeypatch):
    """Unlike /api/fs/upload, which 404s: the client named no path to get wrong."""
    nested = staged / "not" / "there" / "yet"
    monkeypatch.setattr(A, "UPLOAD_DIR", nested)
    r = A.store_upload(BLOB, "notes.txt", "", "")
    assert r.status_code == 200
    assert nested.is_dir()
    assert Path(body(r)["path"]).parent == nested


# ---------------------------------------------------------------------------
# What a phone's filename becomes
# ---------------------------------------------------------------------------

def test_a_traversing_name_cannot_leave_the_staging_directory(staged):
    name = Path(body(A.store_upload(BLOB, "../../x.txt", "", ""))["name"])
    assert name == Path(name.name)
    assert (staged / name).is_file()
    assert name.name.startswith("x-")


@pytest.mark.parametrize("given, stem", [
    ("my report final.pdf", "my-report-final"),
    ("ünïcode ✓.txt", "n-code"),
    ("../../etc/passwd", "etc-passwd"),
    ("..", "file"),
    (".hidden", "hidden"),
    ("", "file"),
    ("a:b|c.txt", "a-b-c"),
])
def test_the_stem_keeps_only_what_a_prompt_can_take(given, stem):
    """Spaces, colons, separators and unicode all collapse to dashes, and a
    leading dot or dash goes: the first hides the file, the second reads as a
    flag."""
    root, _ = os.path.splitext(given)
    assert A.upload_name(root) == stem


def test_a_long_name_is_cut_before_the_stamp_is_appended(staged):
    """A filesystem limit is not a reason to fail an upload that is otherwise
    fine."""
    r = A.store_upload(BLOB, "n" * 400 + ".txt", "", "")
    assert r.status_code == 200
    name = body(r)["name"]
    assert name.startswith("n" * 64 + "-")
    assert name.endswith(".txt")


@pytest.mark.parametrize("given, ext", [
    ("notes.TXT", ".txt"),
    ("photo.JPEG", ".jpeg"),
    ("archive.tar.gz", ".gz"),
    ("notes.wayoverlong", ""),
    ("notes.", ""),
    ("README", ""),
])
def test_only_a_short_alphanumeric_extension_is_carried_through(staged, given, ext):
    assert Path(body(A.store_upload(BLOB, given, "", ""))["path"]).suffix == ext


# ---------------------------------------------------------------------------
# What gets refused
# ---------------------------------------------------------------------------

def test_an_empty_body_is_refused_before_anything_is_written(staged):
    r = A.store_upload(b"", "notes.txt", "", "")
    assert r.status_code == 400
    assert body(r) == {"error": "empty"}
    assert not staged.exists()


def test_an_oversize_upload_is_refused(staged, monkeypatch):
    """The cap, moved down to where a test can reach it without 50MB of memory."""
    monkeypatch.setattr(A, "MAX_UPLOAD_BYTES", 10)
    r = A.store_upload(b"\x00" * 11, "notes.txt", "", "")
    assert r.status_code == 413
    assert body(r) == {"error": "too_large"}
    assert not staged.exists()


# ---------------------------------------------------------------------------
# Pruning
# ---------------------------------------------------------------------------

def test_an_upload_prunes_the_dir_back_to_the_newest_keep(staged):
    """Older attachments go, and the one that just arrived is never the one
    evicted."""
    staged.mkdir(parents=True)
    old = []
    for i in range(35):
        p = staged / f"old{i:02d}-20250101-000000-aaaaaa.txt"
        p.write_bytes(BLOB)
        os.utime(p, (1_700_000_000 + i, 1_700_000_000 + i))
        old.append(p)

    fresh = Path(body(A.store_upload(BLOB, "notes.txt", "", ""))["path"])

    survivors = sorted(staged.iterdir())
    assert len(survivors) == A.UPLOAD_KEEP
    assert fresh in survivors
    # The newest of the pre-existing files fill the rest; the oldest are gone.
    assert set(old[-(A.UPLOAD_KEEP - 1):]) < set(survivors)
    assert not any(p.exists() for p in old[:-(A.UPLOAD_KEEP - 1)])


def test_pruning_leaves_the_paste_directory_alone(tmp_path, monkeypatch):
    """Two staging directories, one helper: an attachment must not evict an
    image that Claude Code has not read yet."""
    images = tmp_path / "images"
    images.mkdir()
    monkeypatch.setattr(A, "IMAGE_DIR", images)
    monkeypatch.setattr(A, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(A, "UPLOAD_KEEP", 1)
    kept = images / "paste-20250101-000000-aaaaaa.png"
    kept.write_bytes(b"\x89PNG\r\n\x1a\n")

    A.store_upload(BLOB, "notes.txt", "", "")
    assert kept.is_file()


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

def test_the_route_sits_under_the_authenticated_prefix():
    """/api/ is what the require_token middleware keys on, so the path itself is
    the whole auth story, worth pinning without standing up a client."""
    assert "/api/upload" in {r.path for r in A.app.routes}


def test_the_capability_is_advertised():
    """The shell checks this one strictly, so a server without the route must
    read as lacking it rather than as merely old."""
    assert A.CAPABILITIES["upload"] is True
