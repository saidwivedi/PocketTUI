"""Tests for the file-explorer routes.

Everything runs against tmp_path through the FastAPI TestClient, so the suite
touches nothing outside the sandbox pytest hands it. AUTH_TOKEN is None in a
bare import of app, which is exactly the middleware's --no-auth short-circuit —
the auth path itself is covered by the transcribe suite's server-level tests.
"""

import os
import sys
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
