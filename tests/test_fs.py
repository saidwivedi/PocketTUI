"""Tests for the file-explorer routes.

Everything runs against tmp_path through the FastAPI TestClient, so the suite
touches nothing outside the sandbox pytest hands it. AUTH_TOKEN is None in a
bare import of app, which is exactly the middleware's --no-auth short-circuit —
the auth path itself is covered by the transcribe suite's server-level tests.
"""

import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as A  # noqa: E402


@pytest.fixture
def client():
    return TestClient(A.app)


@pytest.fixture
def tree(tmp_path):
    """A small directory tree with the shapes the explorer distinguishes."""
    (tmp_path / "beta.txt").write_text("beta\n")
    (tmp_path / "Alpha.py").write_text("print('hi')\n")
    (tmp_path / ".dotfile").write_text("hidden\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "inner.md").write_text("# inner\n")
    (tmp_path / "zdir").mkdir()
    return tmp_path


def entry_names(payload):
    return [e["name"] for e in payload["entries"]]


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------

def test_list_dirs_first_then_files_alphabetical_with_dotfiles(client, tree):
    r = client.get("/api/fs/list", params={"path": str(tree)})
    assert r.status_code == 200
    data = r.json()
    assert data["path"] == str(tree)
    assert entry_names(data) == ["sub", "zdir", ".dotfile", "Alpha.py", "beta.txt"]
    by_name = {e["name"]: e for e in data["entries"]}
    assert by_name["sub"]["type"] == "dir"
    assert by_name["beta.txt"]["type"] == "file"
    assert by_name["beta.txt"]["size"] == 5
    assert by_name["beta.txt"]["mtime"] > 0


def test_list_defaults_to_home_and_reports_it(client, tree, monkeypatch):
    monkeypatch.setenv("HOME", str(tree))
    r = client.get("/api/fs/list")
    assert r.status_code == 200
    assert r.json()["path"] == str(tree)
    assert r.json()["home"] == str(tree)


def test_list_expands_tilde(client, tree, monkeypatch):
    monkeypatch.setenv("HOME", str(tree))
    r = client.get("/api/fs/list", params={"path": "~/sub"})
    assert r.status_code == 200
    assert r.json()["path"] == str(tree / "sub")


def test_list_missing_is_404_and_file_is_400(client, tree):
    assert client.get("/api/fs/list",
                      params={"path": str(tree / "absent")}).status_code == 404
    r = client.get("/api/fs/list", params={"path": str(tree / "beta.txt")})
    assert r.status_code == 400
    assert r.json()["error"] == "not_a_directory"


def test_list_relative_path_is_400(client):
    assert client.get("/api/fs/list", params={"path": "etc"}).status_code == 400


def test_list_classifies_symlinks_through_the_link(client, tree):
    os.symlink(tree / "sub", tree / "sublink")
    os.symlink(tree / "gone", tree / "broken")
    r = client.get("/api/fs/list", params={"path": str(tree)})
    by_name = {e["name"]: e for e in r.json()["entries"]}
    assert by_name["sublink"]["type"] == "dir"
    assert by_name["broken"]["type"] == "link"


def test_list_applies_path_rewrites(client, tree, monkeypatch):
    monkeypatch.setattr(A, "PATH_REWRITES", [("/remote/mount", str(tree))])
    r = client.get("/api/fs/list", params={"path": "/remote/mount/sub"})
    assert r.status_code == 200
    assert r.json()["path"] == str(tree / "sub")


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------

def test_read_returns_content_and_hash(client, tree):
    r = client.get("/api/fs/read", params={"path": str(tree / "beta.txt")})
    assert r.status_code == 200
    data = r.json()
    assert data["content"] == "beta\n"
    assert data["hash"] == A.fs_hash(b"beta\n")
    assert data["size"] == 5
    assert data["lossy"] is False


def test_read_rejects_binary_with_its_own_code(client, tree):
    (tree / "blob.bin").write_bytes(b"PK\x00\x03rest")
    r = client.get("/api/fs/read", params={"path": str(tree / "blob.bin")})
    assert r.status_code == 415
    assert r.json()["error"] == "binary_file"


def test_read_flags_non_utf8_as_lossy(client, tree):
    (tree / "latin.txt").write_bytes(b"caf\xe9\n")
    r = client.get("/api/fs/read", params={"path": str(tree / "latin.txt")})
    assert r.status_code == 200
    data = r.json()
    assert data["lossy"] is True
    assert "�" in data["content"]
    # The hash is still of the on-disk bytes, not of the lossy decode.
    assert data["hash"] == A.fs_hash(b"caf\xe9\n")


def test_read_size_cap(client, tree, monkeypatch):
    monkeypatch.setattr(A, "MAX_TEXT_BYTES", 10)
    (tree / "big.txt").write_text("x" * 11)
    r = client.get("/api/fs/read", params={"path": str(tree / "big.txt")})
    assert r.status_code == 413
    assert r.json()["error"] == "too_large"


def test_read_missing_is_404(client, tree):
    assert client.get("/api/fs/read",
                      params={"path": str(tree / "absent")}).status_code == 404


def test_read_applies_path_rewrites(client, tree, monkeypatch):
    monkeypatch.setattr(A, "PATH_REWRITES", [("/remote/mount", str(tree))])
    r = client.get("/api/fs/read", params={"path": "/remote/mount/beta.txt"})
    assert r.status_code == 200
    assert r.json()["content"] == "beta\n"


# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------

def test_write_happy_path_preserves_mode(client, tree):
    target = tree / "beta.txt"
    os.chmod(target, 0o750)
    base = A.fs_hash(b"beta\n")
    r = client.post("/api/fs/write", json={
        "path": str(target), "content": "edited\n", "hash": base})
    assert r.status_code == 200
    assert target.read_text() == "edited\n"
    assert r.json()["hash"] == A.fs_hash(b"edited\n")
    assert (os.stat(target).st_mode & 0o777) == 0o750


def test_write_stale_hash_is_409_with_current_state(client, tree):
    target = tree / "beta.txt"
    r = client.post("/api/fs/write", json={
        "path": str(target), "content": "clobber\n", "hash": A.fs_hash(b"old")})
    assert r.status_code == 409
    data = r.json()
    assert data["error"] == "conflict"
    assert data["hash"] == A.fs_hash(b"beta\n")
    assert "content" not in data
    assert target.read_text() == "beta\n"


def test_write_empty_hash_creates_a_new_file(client, tree):
    target = tree / "fresh.txt"
    r = client.post("/api/fs/write", json={
        "path": str(target), "content": "born\n", "hash": ""})
    assert r.status_code == 200
    assert target.read_text() == "born\n"


def test_write_empty_hash_will_not_clobber_an_existing_file(client, tree):
    r = client.post("/api/fs/write", json={
        "path": str(tree / "beta.txt"), "content": "x", "hash": ""})
    assert r.status_code == 409
    assert r.json()["hash"] == A.fs_hash(b"beta\n")
    assert (tree / "beta.txt").read_text() == "beta\n"


def test_write_to_a_deleted_file_reports_hash_gone(client, tree):
    target = tree / "beta.txt"
    base = A.fs_hash(b"beta\n")
    target.unlink()
    r = client.post("/api/fs/write", json={
        "path": str(target), "content": "late\n", "hash": base})
    assert r.status_code == 409
    assert r.json()["hash"] == ""


def test_write_missing_parent_is_404(client, tree):
    r = client.post("/api/fs/write", json={
        "path": str(tree / "nodir" / "f.txt"), "content": "x", "hash": ""})
    assert r.status_code == 404


def test_write_conflict_check_is_atomic_against_a_concurrent_writer(client, tree,
                                                                   monkeypatch):
    """Two writers off the same base hash: one wins, the other gets a 409.

    The second request is forced to attempt its compare while the first is
    parked between compare and write, which is exactly the window a
    check-then-write endpoint loses — without the lock both would answer 200
    and the later write would silently discard the earlier one.
    """
    target = tree / "beta.txt"
    base = A.fs_hash(b"beta\n")

    real_write = A.atomic_write
    entered = threading.Event()
    proceed = threading.Event()
    seen = []
    seen_lock = threading.Lock()

    def slow_first_write(p, data, mode):
        with seen_lock:
            seen.append(data)
            first = len(seen) == 1
        if first:
            entered.set()
            proceed.wait(5.0)
        return real_write(p, data, mode)

    monkeypatch.setattr(A, "atomic_write", slow_first_write)

    results = {}

    def save(name):
        results[name] = client.post("/api/fs/write", json={
            "path": str(target), "content": name + "\n", "hash": base})

    first = threading.Thread(target=save, args=("one",))
    first.start()
    assert entered.wait(5.0), "first writer never reached atomic_write"

    second = threading.Thread(target=save, args=("two",))
    second.start()
    # Long enough for the second request to reach the compare (or block on the
    # lock) while the first is still parked inside the critical section.
    time.sleep(0.2)
    proceed.set()
    first.join(10.0)
    second.join(10.0)
    assert not first.is_alive() and not second.is_alive()

    codes = sorted(r.status_code for r in results.values())
    assert codes == [200, 409], codes
    winner = next(n for n, r in results.items() if r.status_code == 200)
    loser = next(r for r in results.values() if r.status_code == 409)
    assert loser.json()["error"] == "conflict"
    assert target.read_text() == winner + "\n"
    assert loser.json()["hash"] == A.fs_hash(target.read_bytes())


# ---------------------------------------------------------------------------
# mkdir / rename / delete
# ---------------------------------------------------------------------------

def test_mkdir_creates_and_conflicts(client, tree):
    r = client.post("/api/fs/mkdir", json={"path": str(tree / "newdir")})
    assert r.status_code == 200
    assert (tree / "newdir").is_dir()
    assert client.post("/api/fs/mkdir",
                       json={"path": str(tree / "newdir")}).status_code == 409


def test_rename_happy_path(client, tree):
    r = client.post("/api/fs/rename", json={
        "src": str(tree / "beta.txt"), "dst": str(tree / "gamma.txt")})
    assert r.status_code == 200
    assert not (tree / "beta.txt").exists()
    assert (tree / "gamma.txt").read_text() == "beta\n"


def test_rename_never_clobbers(client, tree):
    r = client.post("/api/fs/rename", json={
        "src": str(tree / "beta.txt"), "dst": str(tree / "Alpha.py")})
    assert r.status_code == 409
    assert (tree / "Alpha.py").read_text() == "print('hi')\n"


def test_rename_missing_src_is_404(client, tree):
    assert client.post("/api/fs/rename", json={
        "src": str(tree / "absent"), "dst": str(tree / "x")}).status_code == 404


def test_delete_file(client, tree):
    r = client.post("/api/fs/delete", json={"path": str(tree / "beta.txt")})
    assert r.status_code == 200
    assert not (tree / "beta.txt").exists()


def test_delete_empty_dir_only(client, tree):
    r = client.post("/api/fs/delete", json={"path": str(tree / "sub")})
    assert r.status_code == 409
    assert r.json()["error"] == "not_empty"
    assert (tree / "sub" / "inner.md").exists()
    assert client.post("/api/fs/delete",
                       json={"path": str(tree / "zdir")}).status_code == 200
    assert not (tree / "zdir").exists()


def test_delete_dir_symlink_removes_the_link_only(client, tree):
    os.symlink(tree / "sub", tree / "sublink")
    r = client.post("/api/fs/delete", json={"path": str(tree / "sublink")})
    assert r.status_code == 200
    assert not (tree / "sublink").exists()
    assert (tree / "sub" / "inner.md").exists()


# ---------------------------------------------------------------------------
# upload / download
# ---------------------------------------------------------------------------

def test_upload_stores_raw_body(client, tree):
    target = tree / "up.bin"
    r = client.post(f"/api/fs/upload?path={target}", content=b"\x00\x01raw")
    assert r.status_code == 200
    assert target.read_bytes() == b"\x00\x01raw"


def test_upload_no_clobber_without_overwrite(client, tree):
    target = tree / "beta.txt"
    r = client.post(f"/api/fs/upload?path={target}", content=b"new")
    assert r.status_code == 409
    assert r.json()["error"] == "exists"
    r = client.post(f"/api/fs/upload?path={target}&overwrite=1", content=b"new")
    assert r.status_code == 200
    assert target.read_bytes() == b"new"


def test_upload_size_cap(client, tree, monkeypatch):
    monkeypatch.setattr(A, "MAX_UPLOAD_BYTES", 8)
    r = client.post(f"/api/fs/upload?path={tree / 'cap.bin'}",
                    content=b"123456789")
    assert r.status_code == 413


def test_download_any_file_as_attachment(client, tree):
    (tree / "blob.bin").write_bytes(b"PK\x00\x03rest")
    r = client.get("/api/fs/download", params={"path": str(tree / "blob.bin")})
    assert r.status_code == 200
    assert r.content == b"PK\x00\x03rest"
    dispo = r.headers["content-disposition"]
    assert dispo.startswith("attachment") and "blob.bin" in dispo


def test_download_missing_is_404(client, tree):
    assert client.get("/api/fs/download",
                      params={"path": str(tree / "absent")}).status_code == 404


# ---------------------------------------------------------------------------
# signed download links
# ---------------------------------------------------------------------------

def mint(client, path):
    """The minted link, as a server-absolute path the TestClient can GET.

    The route answers it relative (no leading slash) because that is what the
    client's apiURL() composes against; the leading slash is what makes it a
    request here.
    """
    r = client.get("/api/fs/download_link", params={"path": str(path)})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["url"].startswith("api/fs/signed_download?")
    return "/" + data["url"], data


def test_download_link_round_trip_streams_the_file_as_an_attachment(client, tree):
    (tree / "blob.bin").write_bytes(b"PK\x00\x03rest")
    url, data = mint(client, tree / "blob.bin")
    assert data["name"] == "blob.bin"
    assert data["expires_in"] == A.DOWNLOAD_TTL

    r = client.get(url)
    assert r.status_code == 200
    assert r.content == b"PK\x00\x03rest"
    dispo = r.headers["content-disposition"]
    assert dispo.startswith("attachment") and "blob.bin" in dispo


def test_signed_download_needs_no_token_header(client, tree, monkeypatch):
    """The whole point: a bare browser navigation, which carries no header."""
    monkeypatch.setattr(A, "LIMITER", A.AuthLimiter())
    url, _ = mint(client, tree / "beta.txt")
    monkeypatch.setattr(A, "AUTH_TOKEN", "ABCDEFGHIJ")

    r = client.get(url)
    assert r.status_code == 200
    assert r.content == b"beta\n"
    # The neighbouring route is still gated, so this is an exemption for the
    # signed path alone and not a hole in the middleware.
    assert client.get("/api/fs/download",
                      params={"path": str(tree / "beta.txt")}).status_code == 401


def test_signed_download_expired_is_rejected(client, tree):
    target = str(tree / "beta.txt")
    stale = int(time.time()) - 1
    r = client.get("/api/fs/signed_download", params={
        "path": target, "exp": stale, "sig": A.download_sig(target, stale)})
    assert r.status_code == 403
    assert r.json()["error"] == "expired"


def test_signed_download_unsigned_is_rejected(client, tree):
    r = client.get("/api/fs/signed_download", params={"path": str(tree / "beta.txt")})
    assert r.status_code == 403
    assert r.json()["error"] == "bad_signature"


def test_signed_download_tampered_path_is_rejected(client, tree):
    (tree / "secret.txt").write_text("secret\n")
    url, _ = mint(client, tree / "beta.txt")
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))

    for swapped in (str(tree / "secret.txt"),
                    # A traversal appended to the signed path is a different
                    # path, so it needs a signature of its own — and only the
                    # gated mint hands those out.
                    str(tree / "sub" / ".." / "secret.txt")):
        r = client.get("/api/fs/signed_download",
                       params={**q, "path": swapped})
        assert r.status_code == 403
        assert r.json()["error"] == "bad_signature"
    # Untampered, the same query still works — the rejection was the swap.
    assert client.get(url).status_code == 200


def test_signed_download_expiry_is_covered_by_the_signature(client, tree):
    url, _ = mint(client, tree / "beta.txt")
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
    r = client.get("/api/fs/signed_download",
                   params={**q, "exp": int(q["exp"]) + 86400})
    assert r.status_code == 403
    assert r.json()["error"] == "bad_signature"


def test_download_link_refuses_a_path_it_cannot_resolve(client, tree):
    # fs_path()'s rule, the one every /api/fs/* route already applies: nothing
    # that is not an absolute local path gets a link, so nothing relative to
    # the server's cwd can be signed at all.
    for bad in ("relative/beta.txt", "../../etc/passwd", ""):
        assert client.get("/api/fs/download_link",
                          params={"path": bad}).status_code == 404
    # A directory is not a download either.
    assert client.get("/api/fs/download_link",
                      params={"path": str(tree / "sub")}).status_code == 404


def test_signed_download_of_a_vanished_file_is_404(client, tree):
    (tree / "doomed.bin").write_bytes(b"x")
    url, _ = mint(client, tree / "doomed.bin")
    (tree / "doomed.bin").unlink()
    assert client.get(url).status_code == 404


# ---------------------------------------------------------------------------
# signed media links
# ---------------------------------------------------------------------------
# What the viewer's <img>/<video> loads, since neither can send the header.

@pytest.fixture
def shot(tree):
    """A file the media allowlist accepts, with bytes worth reading back."""
    p = tree / "shot.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"pixels")
    return p


def mint_file(client, path):
    r = client.get("/api/file_link", params={"path": str(path)})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["url"].startswith("api/signed_file?")
    return "/" + data["url"], data


def test_file_link_round_trip_serves_the_media_inline(client, shot):
    url, data = mint_file(client, shot)
    assert data["expires_in"] == A.FILE_TTL

    r = client.get(url)
    assert r.status_code == 200
    assert r.content == b"\x89PNG\r\n\x1a\npixels"
    assert r.headers["content-type"] == "image/png"
    # Inline is the whole point: an attachment would make the tag a download.
    assert "content-disposition" not in r.headers


def test_signed_file_serves_a_range(client, shot):
    """What a long video's playback keeps coming back for."""
    url, _ = mint_file(client, shot)
    r = client.get(url, headers={"Range": "bytes=0-3"})
    assert r.status_code == 206
    assert r.content == b"\x89PNG"


def test_signed_file_needs_no_token_header(client, shot, monkeypatch):
    """The whole point: a tag's own load, which carries no header."""
    monkeypatch.setattr(A, "LIMITER", A.AuthLimiter())
    url, _ = mint_file(client, shot)
    monkeypatch.setattr(A, "AUTH_TOKEN", "ABCDEFGHIJ")

    r = client.get(url)
    assert r.status_code == 200
    assert r.content == b"\x89PNG\r\n\x1a\npixels"
    # The route it stands in for is still gated, and so is the mint — the
    # exemption is the signed path's alone.
    assert client.get("/api/file", params={"path": str(shot)}).status_code == 401
    assert client.get("/api/file_link", params={"path": str(shot)}).status_code == 401


def test_signed_file_expired_is_rejected(client, shot):
    target = str(shot)
    stale = int(time.time()) - 1
    r = client.get("/api/signed_file", params={
        "path": target, "exp": stale, "sig": A.file_sig(target, stale)})
    assert r.status_code == 403
    assert r.json()["error"] == "expired"


def test_signed_file_unsigned_or_garbled_is_rejected(client, shot):
    for params in ({"path": str(shot)},
                   {"path": str(shot), "exp": "soon", "sig": "x" * 64},
                   {"path": str(shot), "exp": int(time.time()) + 60, "sig": "nope"}):
        r = client.get("/api/signed_file", params=params)
        assert r.status_code == 403
        assert r.json()["error"] == "bad_signature"


def test_signed_file_tampered_path_is_rejected(client, tree, shot):
    (tree / "other.png").write_bytes(b"\x89PNG other")
    url, _ = mint_file(client, shot)
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
    r = client.get("/api/signed_file", params={**q, "path": str(tree / "other.png")})
    assert r.status_code == 403
    assert r.json()["error"] == "bad_signature"
    assert client.get(url).status_code == 200


def test_a_download_link_is_not_a_viewer_link(client, shot):
    """Each purpose signs under its own tag, on the one shared key."""
    target = str(shot)
    expires = int(time.time()) + 60
    r = client.get("/api/signed_file", params={
        "path": target, "exp": expires, "sig": A.download_sig(target, expires)})
    assert r.status_code == 403
    assert r.json()["error"] == "bad_signature"


def test_file_link_refuses_what_api_file_would_refuse(client, tree):
    # media_file()'s rules, answered 404 alike: not the allowlist, not absolute,
    # not there, not a file.
    for bad in (str(tree / "beta.txt"), "relative/shot.png", "",
                str(tree / "absent.png"), str(tree / "sub")):
        assert client.get("/api/file_link", params={"path": bad}).status_code == 404


def test_signed_file_rechecks_the_allowlist(client, tree):
    """A signature is not a licence to serve a file /api/file would not."""
    target = str(tree / "beta.txt")
    expires = int(time.time()) + 60
    r = client.get("/api/signed_file", params={
        "path": target, "exp": expires, "sig": A.file_sig(target, expires)})
    assert r.status_code == 404


def test_signed_file_of_a_vanished_file_is_404(client, shot):
    url, _ = mint_file(client, shot)
    shot.unlink()
    assert client.get(url).status_code == 404


# ---------------------------------------------------------------------------
# signed rendered pages
# ---------------------------------------------------------------------------
# What a tapped .html opens as: the page itself plus whatever it references,
# all under one signature over the folder they share.

@pytest.fixture
def site(tree):
    """A page with a sibling stylesheet, and a secret one folder up from both."""
    d = tree / "report"
    d.mkdir()
    (d / "index.html").write_text(
        "<link rel=stylesheet href=style.css><h1>hi</h1>\n")
    (d / "style.css").write_text("h1 { color: red }\n")
    (tree / "secret.txt").write_text("secret\n")
    return d / "index.html"


def mint_site(client, path):
    r = client.get("/api/fs/render_link", params={"path": str(path)})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["url"].startswith("api/fs/site/")
    return "/" + data["url"], data


def test_render_link_round_trip_serves_the_page_sandboxed(client, site):
    url, data = mint_site(client, site)
    assert data["expires_in"] == A.SITE_TTL

    r = client.get(url)
    assert r.status_code == 200
    assert "<h1>hi</h1>" in r.text
    assert r.headers["content-type"] == "text/html; charset=utf-8"
    # The whole reason this route exists and .html stays out of MEDIA_TYPES:
    # no allow-same-origin, so the page cannot read the app's stored token.
    assert r.headers["content-security-policy"] == "sandbox allow-scripts"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["content-disposition"] == "inline"
    assert r.headers["cache-control"] == "no-store"


def test_render_link_serves_a_sibling_under_the_same_signature(client, site):
    """A relative href in the page, resolved by the browser against its URL."""
    url, _ = mint_site(client, site)
    r = client.get(url.rsplit("/", 1)[0] + "/style.css")
    assert r.status_code == 200
    assert r.text == "h1 { color: red }\n"
    assert r.headers["content-type"] == "text/css; charset=utf-8"
    assert r.headers["content-security-policy"] == "sandbox allow-scripts"


def test_render_link_needs_no_token_header(client, site, monkeypatch):
    """The whole point: a bare navigation into a new tab, which carries none."""
    monkeypatch.setattr(A, "LIMITER", A.AuthLimiter())
    url, _ = mint_site(client, site)
    monkeypatch.setattr(A, "AUTH_TOKEN", "ABCDEFGHIJ")

    assert client.get(url).status_code == 200
    # The mint it comes from is still gated — the exemption is the signed
    # path's alone, and it is a prefix match rather than the exact one its
    # neighbours get.
    assert client.get("/api/fs/render_link",
                      params={"path": str(site)}).status_code == 401


def test_render_link_refuses_to_leave_the_signed_folder(client, tree, site):
    """The signature buys one folder for an hour, not the disk."""
    base = mint_site(client, site)[0].rsplit("/", 1)[0]
    # Percent-encoded, because a literal ../ is collapsed away by the client
    # before it is ever sent — this is the form that reaches the check.
    for rest in ("%2e%2e/secret.txt", "sub/%2e%2e/%2e%2e/secret.txt", ""):
        assert client.get(base + "/" + rest).status_code == 404, rest
    # A symlink is followed and then judged by where it lands, which is why the
    # resolve() is on both sides.
    (site.parent / "out.txt").symlink_to(tree / "secret.txt")
    assert client.get(base + "/out.txt").status_code == 404


def test_signed_site_expired_is_rejected(client, site):
    directory = str(site.parent)
    stale = int(time.time()) - 1
    r = client.get(f"/api/fs/site/{stale}/{A.site_sig(directory, stale)}"
                   f"/{A.site_root(directory)}/index.html")
    assert r.status_code == 403
    assert r.json()["error"] == "expired"


def test_signed_site_tampered_or_unsigned_is_rejected(client, tree, site):
    url, _ = mint_site(client, site)
    exp, sig, root, name = url[len("/api/fs/site/"):].split("/")
    for bad in (f"/api/fs/site/{exp}/{'0' * 64}/{root}/{name}",
                # A different folder needs a signature of its own, and only the
                # gated mint hands those out.
                f"/api/fs/site/{exp}/{sig}/{A.site_root(str(tree))}/secret.txt",
                # The expiry is covered by the signature too.
                f"/api/fs/site/{int(exp) + 86400}/{sig}/{root}/{name}",
                f"/api/fs/site/soon/{sig}/{root}/{name}",
                # Not base64 at all, so there is no folder to have signed.
                f"/api/fs/site/{exp}/{sig}/not-base64!!/{name}"):
        r = client.get(bad)
        assert r.status_code == 403, bad
        assert r.json()["error"] == "bad_signature"
    assert client.get(url).status_code == 200


def test_a_viewer_link_is_not_a_site_link(client, site):
    """Each purpose signs under its own tag, on the one shared key."""
    directory = str(site.parent)
    expires = int(time.time()) + 60
    r = client.get(f"/api/fs/site/{expires}/{A.file_sig(directory, expires)}"
                   f"/{A.site_root(directory)}/index.html")
    assert r.status_code == 403
    assert r.json()["error"] == "bad_signature"


def test_render_link_refuses_a_path_it_cannot_resolve(client, tree):
    for bad in ("relative/page.html", "", str(tree / "absent.html"),
                str(tree / "sub")):
        assert client.get("/api/fs/render_link",
                          params={"path": bad}).status_code == 404


def test_signed_site_of_a_vanished_page_is_404(client, site):
    url, _ = mint_site(client, site)
    site.unlink()
    assert client.get(url).status_code == 404


# ---------------------------------------------------------------------------
# session cwd
# ---------------------------------------------------------------------------

def test_session_cwd_answers_the_resolved_pane(client, monkeypatch):
    monkeypatch.setattr(A, "resolve_target", lambda s, d: "work" if s else "")
    monkeypatch.setattr(A, "pane_cwd", lambda name: "/somewhere")
    r = client.get("/api/session_cwd", params={"session": "work"})
    assert r.status_code == 200
    assert r.json()["cwd"] == "/somewhere"


def test_session_cwd_empty_without_a_session(client, monkeypatch):
    monkeypatch.setattr(A, "resolve_target", lambda s, d: "")
    r = client.get("/api/session_cwd", params={"session": "gone"})
    assert r.status_code == 200
    assert r.json()["cwd"] == ""


# ---------------------------------------------------------------------------
# git diff pane
# ---------------------------------------------------------------------------
# Against a real repo built in tmp_path, because what is being tested is the
# parsing of git's own output — a stubbed `git` would only test the stub.

HAVE_GIT = shutil.which("git") is not None
needs_git = pytest.mark.skipif(not HAVE_GIT, reason="git is not installed")


def run_git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    """A committed repo with one modified file, one untracked, one staged."""
    root = tmp_path / "proj"
    (root / "sub").mkdir(parents=True)
    (root / "tracked.txt").write_text("one\ntwo\nthree\n")
    run_git(root, "init", "-q", ".")
    run_git(root, "add", "tracked.txt")
    run_git(root, "-c", "user.email=t@example.com", "-c", "user.name=T",
            "commit", "-qm", "init")
    (root / "tracked.txt").write_text("one\nTWO\nthree\n")
    (root / "sub" / "fresh.txt").write_text("brand new\n")
    (root / "staged.txt").write_text("staged\n")
    run_git(root, "add", "staged.txt")
    return root


@pytest.fixture
def in_repo(repo, monkeypatch):
    """A session whose visible pane sits in the repo's subdirectory."""
    monkeypatch.setattr(A, "resolve_target", lambda s, d: "work" if s else "")
    monkeypatch.setattr(A, "pane_cwd", lambda name: str(repo / "sub"))
    return repo


@needs_git
def test_tracked_scope_lists_both_index_and_worktree_and_nothing_new(client, in_repo):
    """The default list: what git already knows, staged and not, and not one
    row of the walk over what it has never seen — that is the other tab, and
    the whole point of the split is that this call does not pay for it."""
    r = client.get("/api/git/changes", params={"session": "work"})
    assert r.status_code == 200
    data = r.json()
    assert data["root"] == str(in_repo)
    assert [f["path"] for f in data["files"]] == ["staged.txt", "tracked.txt"]
    by_path = {f["path"]: f for f in data["files"]}
    assert by_path["staged.txt"]["status"] == "A "
    assert by_path["tracked.txt"]["status"] == " M"
    # X is the index and Y the worktree, which is the pane's two lists.
    assert (by_path["staged.txt"]["staged"],
            by_path["staged.txt"]["unstaged"]) == (True, False)
    assert (by_path["tracked.txt"]["staged"],
            by_path["tracked.txt"]["unstaged"]) == (False, True)


@needs_git
def test_untracked_scope_lists_new_files_and_collapses_a_new_folder(client, in_repo):
    """The other list: only what git has never seen, and a folder with nothing
    tracked in it as one row ending in a slash rather than as its contents."""
    (in_repo / "notes.txt").write_text("notes\n")
    (in_repo / "scratch").mkdir()
    (in_repo / "scratch" / "a.txt").write_text("a\n")
    (in_repo / "scratch" / "b.txt").write_text("b\n")
    r = client.get("/api/git/changes",
                   params={"session": "work", "scope": "untracked"})
    assert r.status_code == 200
    data = r.json()
    assert data["root"] == str(in_repo)
    # sub/ holds one untracked file and nothing tracked, so it collapses too.
    assert [f["path"] for f in data["files"]] == [
        "notes.txt", "scratch/", "sub/"]
    for f in data["files"]:
        assert (f["status"], f["staged"], f["unstaged"]) == ("??", False, True)


@needs_git
def test_untracked_scope_leaves_out_an_ignored_folder(client, in_repo):
    (in_repo / ".gitignore").write_text("build/\n")
    (in_repo / "build").mkdir()
    (in_repo / "build" / "out.o").write_text("o\n")
    files = client.get("/api/git/changes",
                       params={"session": "work", "scope": "untracked"}).json()["files"]
    assert [f["path"] for f in files] == [".gitignore", "sub/"]


@needs_git
def test_changes_says_how_long_it_took(client, in_repo):
    """The pane paces its poll by this, so every answer carries it — including
    the ones that never reached git at all."""
    for params in ({"session": "work"},
                   {"session": "work", "scope": "untracked"},
                   {"session": ""}):
        data = client.get("/api/git/changes", params=params).json()
        assert isinstance(data["elapsed_ms"], int)
        assert data["elapsed_ms"] >= 0


@needs_git
def test_changes_names_the_new_path_of_a_rename_once(client, in_repo):
    run_git(in_repo, "mv", "tracked.txt", "renamed.txt")
    r = client.get("/api/git/changes", params={"session": "work"})
    files = r.json()["files"]
    # The old path rides in a field of its own; it must not become a row.
    assert [f["path"] for f in files] == ["renamed.txt", "staged.txt"]
    assert files[0]["status"][0] == "R"


@needs_git
def test_changes_outside_a_repo_says_so_at_200(client, tmp_path, monkeypatch):
    monkeypatch.setattr(A, "resolve_target", lambda s, d: "work")
    monkeypatch.setattr(A, "pane_cwd", lambda name: str(tmp_path))
    r = client.get("/api/git/changes", params={"session": "work"})
    assert r.status_code == 200
    data = r.json()
    data.pop("elapsed_ms")
    assert data == {"root": "", "files": [], "error": "Not a git repository"}


def test_changes_without_a_session_is_empty(client, monkeypatch):
    monkeypatch.setattr(A, "resolve_target", lambda s, d: "")
    r = client.get("/api/git/changes", params={"session": ""})
    assert r.status_code == 200
    data = r.json()
    data.pop("elapsed_ms")
    assert data == {"root": "", "files": []}


@needs_git
def test_diff_of_a_tracked_file_is_the_worktree_against_the_index(client, in_repo):
    r = client.get("/api/git/diff",
                   params={"root": str(in_repo), "path": "tracked.txt"})
    assert r.status_code == 200
    data = r.json()
    assert data["truncated"] is False
    assert "@@ -1,3 +1,3 @@" in data["diff"]
    assert "-two" in data["diff"] and "+TWO" in data["diff"]


@needs_git
def test_diff_of_a_staged_file_needs_the_staged_scope(client, in_repo):
    """A staged add is in the index and not in the worktree's own diff."""
    unstaged = client.get("/api/git/diff",
                          params={"root": str(in_repo), "path": "staged.txt"})
    assert unstaged.json()["diff"] == ""
    staged = client.get("/api/git/diff", params={
        "root": str(in_repo), "path": "staged.txt", "scope": "staged"})
    assert "+staged" in staged.json()["diff"]


@needs_git
def test_diff_of_an_untracked_file_is_the_whole_file_added(client, in_repo):
    r = client.get("/api/git/diff",
                   params={"root": str(in_repo), "path": "sub/fresh.txt"})
    data = r.json()
    assert "+brand new" in data["diff"]
    assert data["truncated"] is False


@needs_git
def test_diff_of_a_binary_file_says_binary_rather_than_nothing(client, in_repo):
    (in_repo / "blob.bin").write_bytes(b"\x00\x01\x02\xff" * 64)
    run_git(in_repo, "add", "blob.bin")
    # Staged, so the staged scope is the list it is in and the one asked.
    r = client.get("/api/git/diff", params={
        "root": str(in_repo), "path": "blob.bin", "scope": "staged"})
    assert r.json() == {"diff": "", "binary": True}


@needs_git
def test_diff_truncates_at_the_cap_and_says_so(client, in_repo, monkeypatch):
    monkeypatch.setattr(A, "GIT_DIFF_MAX", 60)
    r = client.get("/api/git/diff",
                   params={"root": str(in_repo), "path": "tracked.txt"})
    data = r.json()
    assert data["truncated"] is True
    assert len(data["diff"]) == 60


@needs_git
def test_diff_refuses_a_path_that_climbs_out_of_the_root(client, in_repo):
    for bad in ("../outside.txt", "sub/../../outside.txt", "/etc/passwd", ""):
        r = client.get("/api/git/diff",
                       params={"root": str(in_repo), "path": bad})
        assert r.status_code == 400, bad


def test_diff_refuses_a_root_that_is_not_a_directory(client, tmp_path):
    for bad in ("relative/path", str(tmp_path / "absent"), ""):
        r = client.get("/api/git/diff", params={"root": bad, "path": "a.txt"})
        assert r.status_code == 400, bad


# ---------------------------------------------------------------------------
# git apply — the pane's stage, revert, unstage and discard
# ---------------------------------------------------------------------------
# Against the same real repo, and through the same two GETs the pane uses to
# decide what to send: a hunk action is only correct if the hunk the diff route
# handed out is the one the apply route takes back.


def split_patch(text):
    """(header block, list of hunks) of one file's diff, as the pane splits it.

    Everything before the first `@@` is the header every one-hunk patch is
    rebuilt with; each `@@` starts a hunk that runs to the next one.
    """
    header, hunks = [], []
    for line in text.rstrip("\n").split("\n"):
        if line.startswith("@@"):
            hunks.append([line])
        elif hunks:
            hunks[-1].append(line)
        else:
            header.append(line)
    return header, hunks


def one_hunk(text, i):
    """The one-hunk patch the client sends for hunk `i` of `text`."""
    header, hunks = split_patch(text)
    return "\n".join(header + hunks[i]) + "\n"


def git_diff(client, root, path, scope="unstaged"):
    return client.get("/api/git/diff", params={
        "root": str(root), "path": path, "scope": scope}).json()["diff"]


def git_apply(client, root, path, action, patch=None):
    body = {"root": str(root), "path": path, "action": action}
    if patch is not None:
        body["patch"] = patch
    return client.post("/api/git/apply", json=body)


def git_row(client, path, scope="tracked"):
    files = client.get("/api/git/changes",
                       params={"session": "work", "scope": scope}).json()["files"]
    return next((f for f in files if f["path"] == path), None)


def wide_text(first="1", last="20"):
    """A 20-line file with swappable ends — far enough apart that a change at
    each is two hunks rather than one."""
    lines = [str(n) for n in range(1, 21)]
    lines[0], lines[-1] = first, last
    return "".join(line + "\n" for line in lines)


@pytest.fixture
def wide(in_repo):
    """A committed 20-line file changed at both ends, neither change staged."""
    (in_repo / "wide.txt").write_text(wide_text())
    run_git(in_repo, "add", "wide.txt")
    run_git(in_repo, "-c", "user.email=t@example.com", "-c", "user.name=T",
            "commit", "-qm", "wide")
    (in_repo / "wide.txt").write_text(wide_text("A", "Z"))
    return in_repo


@needs_git
def test_a_partly_staged_file_is_in_both_lists_with_a_diff_each(client, wide):
    (wide / "wide.txt").write_text(wide_text("A"))
    run_git(wide, "add", "wide.txt")
    (wide / "wide.txt").write_text(wide_text("A", "Z"))
    row = git_row(client, "wide.txt")
    assert row["status"] == "MM"
    assert row["staged"] is True and row["unstaged"] is True
    staged = git_diff(client, wide, "wide.txt", "staged")
    unstaged = git_diff(client, wide, "wide.txt")
    assert "+A" in staged and "+Z" not in staged
    assert "+Z" in unstaged and "+A" not in unstaged


@needs_git
def test_stage_hunk_leaves_one_hunk_in_each_scope(client, wide):
    text = git_diff(client, wide, "wide.txt")
    assert len(split_patch(text)[1]) == 2
    assert git_apply(client, wide, "wide.txt", "stage_hunk",
                     one_hunk(text, 0)).status_code == 200
    staged = git_diff(client, wide, "wide.txt", "staged")
    unstaged = git_diff(client, wide, "wide.txt")
    assert "+A" in staged and "+Z" not in staged
    assert "+Z" in unstaged and "+A" not in unstaged
    assert (wide / "wide.txt").read_text() == wide_text("A", "Z")


@needs_git
def test_unstage_hunk_takes_it_back_out_of_the_index(client, wide):
    git_apply(client, wide, "wide.txt", "stage_hunk",
              one_hunk(git_diff(client, wide, "wide.txt"), 0))
    staged = git_diff(client, wide, "wide.txt", "staged")
    assert git_apply(client, wide, "wide.txt", "unstage_hunk",
                     one_hunk(staged, 0)).status_code == 200
    assert git_diff(client, wide, "wide.txt", "staged") == ""
    assert git_row(client, "wide.txt")["staged"] is False
    # Unstaging is an index move only; the worktree keeps both changes.
    assert (wide / "wide.txt").read_text() == wide_text("A", "Z")


@needs_git
def test_revert_hunk_drops_it_and_applying_it_forward_is_the_undo(client, wide):
    patch = one_hunk(git_diff(client, wide, "wide.txt"), 1)
    assert git_apply(client, wide, "wide.txt", "revert_hunk",
                     patch).status_code == 200
    # Only the block that was reverted: the change at the other end stands.
    assert (wide / "wide.txt").read_text() == wide_text("A")
    assert git_apply(client, wide, "wide.txt", "apply_hunk",
                     patch).status_code == 200
    assert (wide / "wide.txt").read_text() == wide_text("A", "Z")


@needs_git
def test_a_hunk_that_no_longer_applies_is_a_409_saying_so(client, wide):
    patch = one_hunk(git_diff(client, wide, "wide.txt"), 1)
    (wide / "wide.txt").write_text(wide_text("A", "Q"))
    r = git_apply(client, wide, "wide.txt", "revert_hunk", patch)
    assert r.status_code == 409
    assert "does not apply" in r.json()["detail"]


@needs_git
def test_stage_file_stages_an_untracked_file_whole(client, in_repo):
    assert git_apply(client, in_repo, "sub/fresh.txt",
                     "stage_file").status_code == 200
    row = git_row(client, "sub/fresh.txt")
    assert row["status"] == "A "
    assert row["staged"] is True and row["unstaged"] is False


@needs_git
def test_stage_file_records_a_deletion_too(client, in_repo):
    (in_repo / "tracked.txt").unlink()
    assert git_apply(client, in_repo, "tracked.txt",
                     "stage_file").status_code == 200
    assert git_row(client, "tracked.txt")["status"] == "D "


@needs_git
def test_discard_file_restores_a_tracked_file_from_the_index(client, in_repo):
    assert git_apply(client, in_repo, "tracked.txt",
                     "discard_file").status_code == 200
    assert (in_repo / "tracked.txt").read_text() == "one\ntwo\nthree\n"
    assert git_row(client, "tracked.txt") is None


@needs_git
def test_discard_file_deletes_an_untracked_file(client, in_repo):
    assert git_apply(client, in_repo, "sub/fresh.txt",
                     "discard_file").status_code == 200
    assert not (in_repo / "sub" / "fresh.txt").exists()
    assert git_row(client, "sub/fresh.txt", "untracked") is None


@needs_git
def test_discard_deletes_an_untracked_folder_whole(client, in_repo):
    """A row the untracked list collapsed is a tree, and git holds no copy of
    any of it — so discarding one is deleting it, trailing slash and all."""
    (in_repo / "scratch").mkdir()
    (in_repo / "scratch" / "deep").mkdir()
    (in_repo / "scratch" / "deep" / "a.txt").write_text("a\n")
    assert git_apply(client, in_repo, "scratch/",
                     "discard_file").status_code == 200
    assert not (in_repo / "scratch").exists()
    assert git_row(client, "scratch/", "untracked") is None


@needs_git
def test_discard_refuses_a_symlinked_folder(client, in_repo):
    """lstat, not stat: the link is what the row named, and following it would
    empty whatever it points at."""
    (in_repo / "elsewhere").mkdir()
    (in_repo / "elsewhere" / "keep.txt").write_text("keep\n")
    (in_repo / "link").symlink_to(in_repo / "elsewhere")
    r = git_apply(client, in_repo, "link/", "discard_file")
    assert r.status_code == 409
    assert (in_repo / "link").is_symlink()
    assert (in_repo / "elsewhere" / "keep.txt").exists()


@needs_git
def test_discard_refuses_an_untracked_path_that_is_not_a_regular_file(client, in_repo):
    (in_repo / "link").symlink_to("/etc/passwd")
    r = git_apply(client, in_repo, "link", "discard_file")
    assert r.status_code == 409
    assert (in_repo / "link").is_symlink()


@needs_git
def test_unstage_file_puts_a_staged_add_back_to_untracked(client, in_repo):
    assert git_apply(client, in_repo, "staged.txt",
                     "unstage_file").status_code == 200
    assert git_row(client, "staged.txt", "untracked")["status"] == "??"


@needs_git
def test_the_staged_scope_works_before_the_first_commit(client, tmp_path,
                                                        monkeypatch):
    """No HEAD to compare against: --cached uses the empty tree, and it is
    `reset` rather than `restore --staged` that can empty an index entry."""
    root = tmp_path / "fresh"
    root.mkdir()
    run_git(root, "init", "-q", ".")
    (root / "a.txt").write_text("a\n")
    run_git(root, "add", "a.txt")
    monkeypatch.setattr(A, "resolve_target", lambda s, d: "work")
    monkeypatch.setattr(A, "pane_cwd", lambda name: str(root))
    assert "+a" in git_diff(client, root, "a.txt", "staged")
    assert git_apply(client, root, "a.txt", "unstage_file").status_code == 200
    assert git_row(client, "a.txt", "untracked")["status"] == "??"


@needs_git
def test_apply_refuses_a_path_that_climbs_out_of_the_root(client, in_repo):
    for bad in ("../outside.txt", "sub/../../outside.txt", "/etc/passwd", ""):
        assert git_apply(client, in_repo, bad,
                         "stage_file").status_code == 400, bad


def test_apply_refuses_a_root_that_is_not_a_directory(client, tmp_path):
    for bad in ("relative/path", str(tmp_path / "absent"), ""):
        assert git_apply(client, bad, "a.txt", "stage_file").status_code == 400


def test_version_names_the_git_routes_as_a_capability(client):
    """A shell newer than the server must be able to tell that these three
    routes are there before it opens a pane that would 404 on all of them."""
    assert client.get("/api/version").json()["capabilities"]["git"] is True


@needs_git
def test_apply_refuses_an_unknown_action_and_a_patch_with_no_hunk(client, in_repo):
    assert git_apply(client, in_repo, "tracked.txt",
                     "commit_everything").status_code == 400
    assert git_apply(client, in_repo, "tracked.txt", "stage_hunk",
                     "not a patch at all\n").status_code == 400
    assert git_apply(client, in_repo, "tracked.txt", "stage_hunk",
                     "   ").status_code == 400
