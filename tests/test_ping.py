"""The server's daily install check (app.py): one POST a UTC day at most,
carrying only the install id, the version and the platform, and nothing at
all when it is switched off. The sender is replaced, so nothing leaves the
machine.
"""

import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as A  # noqa: E402


@pytest.fixture
def install(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "INSTALL_ID_PATH", tmp_path / ".install_id")
    monkeypatch.setattr(A, "NO_PING_PATH", tmp_path / ".no_ping")
    monkeypatch.setattr(A, "PING_DAY_PATH", tmp_path / ".ping_day")
    monkeypatch.delenv("POCKETTUI_NO_PING", raising=False)
    sent = []

    def fake_send(payload):
        sent.append(payload)
        return True

    monkeypatch.setattr(A, "send_ping", fake_send)
    return tmp_path, sent


def test_sent_at_most_once_per_utc_day(install):
    d, sent = install
    assert A.ping_once("2026-10-14") is True
    assert A.ping_once("2026-10-14") is False
    assert A.ping_once("2026-10-14") is False
    assert len(sent) == 1
    assert A.ping_once("2026-10-15") is True
    assert len(sent) == 2
    # The same id both days, and it is the one on disk.
    assert sent[0]["id"] == sent[1]["id"] == (d / ".install_id").read_text().strip()
    assert (d / ".ping_day").read_text().strip() == "2026-10-15"


def test_payload_is_only_id_version_os_arch(install):
    _, sent = install
    A.ping_once("2026-10-14")
    (p,) = sent
    assert set(p) == {"id", "v", "os", "arch"}
    assert re.fullmatch(r"[0-9a-f]{32}", p["id"])
    assert p["os"] in ("linux", "mac", "other")
    assert p["arch"] in ("x86_64", "arm64", "other")
    assert p["v"] == A.installed_version()
    json.dumps(p)


def test_existing_id_is_kept(install):
    d, sent = install
    (d / ".install_id").write_text("0123456789abcdef0123456789abcdef\n")
    A.ping_once("2026-10-14")
    assert sent[0]["id"] == "0123456789abcdef0123456789abcdef"
    # A malformed file is replaced rather than sent.
    (d / ".install_id").write_text("not an id\n")
    A.ping_once("2026-10-15")
    assert re.fullmatch(r"[0-9a-f]{32}", sent[1]["id"])
    assert sent[1]["id"] != "0123456789abcdef0123456789abcdef"


@pytest.mark.parametrize("how", ["env", "file"])
def test_opted_out_sends_nothing_and_mints_no_id(install, monkeypatch, how):
    d, sent = install
    if how == "env":
        monkeypatch.setenv("POCKETTUI_NO_PING", "1")
    else:
        (d / ".no_ping").write_text("")
    assert A.ping_once("2026-10-14") is False
    assert sent == []
    assert not (d / ".install_id").exists()
    assert not (d / ".ping_day").exists()


def test_a_failed_send_is_retried_later_the_same_day(install, monkeypatch):
    d, _ = install
    calls = []
    monkeypatch.setattr(A, "send_ping", lambda p: calls.append(p) or False)
    assert A.ping_once("2026-10-14") is False
    assert not (d / ".ping_day").exists()
    monkeypatch.setattr(A, "send_ping", lambda p: calls.append(p) or True)
    assert A.ping_once("2026-10-14") is True
    assert len(calls) == 2


def test_real_sender_fails_silently_and_fast(monkeypatch):
    # An address nothing listens on: the sender answers False, never raises.
    monkeypatch.setattr(A, "PING_URL", "http://127.0.0.1:9/api/ping")
    assert A.send_ping({"id": "x"}) is False

