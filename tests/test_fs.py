"""Tests for the file-explorer routes.

Everything runs against tmp_path through the FastAPI TestClient, so the suite
touches nothing outside the sandbox pytest hands it. AUTH_TOKEN is None in a
bare import of app, which is exactly the middleware's --no-auth short-circuit —
the auth path itself is covered by the transcribe suite's server-level tests.
"""

import os
import sys
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
