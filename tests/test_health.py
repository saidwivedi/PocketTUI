"""The two things that answer "is a PocketTUI backend alive here?".

The descriptor at /.well-known/pockettui is what the phone and the installer
ask when nothing else is working, so the point of these tests is that it stays
ungated and stays quiet: a product name and a version, never a hostname, a port
or a capability list. runtime.json is the other half of the answer, the one the
installer reads when the descriptor says nothing at all.
"""

import json
import os
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as A  # noqa: E402

TOKEN = "ABCDEFGHIJ"


@pytest.fixture
def client():
    return TestClient(A.app)


@pytest.fixture(autouse=True)
def fresh_limits(monkeypatch):
    monkeypatch.setattr(A, "LIMITER", A.AuthLimiter())


# ---------------------------------------------------------------------------
# The descriptor
# ---------------------------------------------------------------------------

def test_descriptor_answers_without_a_token(client, monkeypatch):
    monkeypatch.setattr(A, "AUTH_TOKEN", TOKEN)
    r = client.get("/.well-known/pockettui")
    assert r.status_code == 200
    assert r.json() == {"product": "pockettui", "version": A.installed_version()}
    assert r.headers["cache-control"] == "no-store"

    # The same client, the same missing header: the API is still shut.
    assert client.get("/api/version").status_code == 401


def test_descriptor_says_nothing_about_the_host(client, monkeypatch):
    monkeypatch.setattr(A, "AUTH_TOKEN", TOKEN)
    body = client.get("/.well-known/pockettui").json()
    assert set(body) == {"product", "version"}


# ---------------------------------------------------------------------------
# runtime.json
# ---------------------------------------------------------------------------

@pytest.fixture
def runtime_path(tmp_path, monkeypatch):
    p = tmp_path / "runtime.json"
    monkeypatch.setattr(A, "RUNTIME_PATH", p)
    return p


def test_write_runtime_records_this_process(runtime_path):
    A.write_runtime("127.0.0.1", 5599)
    state = json.loads(runtime_path.read_text())
    assert state["pid"] == os.getpid()
    assert state["host"] == "127.0.0.1"
    assert state["port"] == 5599
    assert state["version"] == A.installed_version()
    assert state["started_at"].endswith("Z")


def test_clear_runtime_removes_it(runtime_path):
    A.write_runtime("0.0.0.0", 5560)
    assert runtime_path.exists()
    A.clear_runtime()
    assert not runtime_path.exists()


def test_clear_runtime_on_a_missing_file_is_not_an_error(runtime_path):
    assert not runtime_path.exists()
    A.clear_runtime()          # a crash before write_runtime lands here


def test_an_unwritable_dir_costs_the_file_and_nothing_else(tmp_path, monkeypatch):
    """Never fatal: the diagnosis is worth less than the server."""
    monkeypatch.setattr(A, "RUNTIME_PATH", tmp_path / "no-such-dir" / "runtime.json")
    A.write_runtime("127.0.0.1", 5560)
