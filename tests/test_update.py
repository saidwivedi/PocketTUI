"""POST /api/update: the one-tap server update the phone starts.

Against a real tmux on an isolated socket (`-L pockettui-update-test`), the way
test_ws_attach.py's integration class does, because the thing worth proving is
what actually lands in the pane: that a session appears under the name the shell
tells the user to open, that the wrapper is invoked with `update`, and that the
os.setsid() in front of it really did drop the controlling terminal — an
installer that can still open /dev/tty would sit at a prompt nobody can see.
"""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as A  # noqa: E402

TMUX_TEST_BIN = ["tmux", "-L", "pockettui-update-test", "-f", "/dev/null"]


@pytest.fixture
def client():
    with TestClient(A.app) as c:
        yield c


@pytest.fixture(autouse=True)
def isolated_tmux(monkeypatch):
    monkeypatch.setattr(A, "TMUX_BIN", list(TMUX_TEST_BIN))
    monkeypatch.setattr(A, "AUTH_TOKEN", None)
    yield
    subprocess.run([*TMUX_TEST_BIN, "kill-server"], capture_output=True,
                   timeout=10)


def write_wrapper(tmp_path, monkeypatch, status=0):
    """A stand-in for the `pockettui` command that records how it was called.

    Writes its argv and whether it could open a controlling terminal, so the
    test can read back both halves of the contract from one run, and exits with
    `status` so the pane's own report of it can be checked.
    """
    log = tmp_path / "called.txt"
    wrapper = tmp_path / "pockettui"
    wrapper.write_text(
        "#!/bin/bash\n"
        f'printf "argv=%s\\n" "$*" >> "{log}"\n'
        f'if (exec 3<>/dev/tty) 2>/dev/null; then\n'
        f'  echo "tty=yes" >> "{log}"\n'
        f'else\n'
        f'  echo "tty=no" >> "{log}"\n'
        f'fi\n'
        f'exit {status}\n'
    )
    wrapper.chmod(0o755)
    monkeypatch.setattr(A, "update_command", lambda: str(wrapper))
    return log


@pytest.fixture
def fake_wrapper(tmp_path, monkeypatch):
    return write_wrapper(tmp_path, monkeypatch)


def read_when(path, marker, timeout=10.0):
    """The log's text once it carries `marker`, or whatever it has at timeout."""
    deadline = time.monotonic() + timeout
    text = ""
    while time.monotonic() < deadline:
        try:
            text = path.read_text()
        except OSError:
            text = ""
        if marker in text:
            return text
        time.sleep(0.1)
    return text


def pane_when(marker, timeout=10.0):
    """The update pane's text once it carries `marker`, or the last read."""
    deadline = time.monotonic() + timeout
    text = ""
    while time.monotonic() < deadline:
        text = A.tmux("capture-pane", "-p", "-t", A.UPDATE_SESSION)[1]
        if marker in text:
            return text
        time.sleep(0.1)
    return text


def test_capability_is_false_without_a_wrapper(monkeypatch):
    monkeypatch.setattr(A, "update_command", lambda: None)
    assert A.server_capabilities()["update"] is False


def test_capability_is_true_with_one(monkeypatch):
    monkeypatch.setattr(A, "update_command", lambda: "/usr/bin/pockettui")
    assert A.server_capabilities()["update"] is True


def test_update_refuses_without_a_wrapper(client, monkeypatch):
    monkeypatch.setattr(A, "update_command", lambda: None)
    r = client.post("/api/update")
    assert r.status_code == 501
    assert r.json() == {"error": "update_unavailable"}


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")
def test_update_runs_the_wrapper_in_a_watchable_session(client, fake_wrapper):
    r = client.post("/api/update")
    assert r.status_code == 200
    assert r.json() == {"session": A.UPDATE_SESSION}

    # Named exactly what the shell tells the user to open, and listed by tmux —
    # which is what puts it in the app's session list.
    assert A.session_exists(A.UPDATE_SESSION)
    rc, out = A.tmux("list-sessions", "-F", "#{session_name}")
    assert rc == 0
    assert A.UPDATE_SESSION in out.split()

    # A second tap while it is running is refused rather than starting a
    # second installer over the first.
    again = client.post("/api/update")
    assert again.status_code == 409
    assert again.json() == {"error": "already_running"}

    text = read_when(fake_wrapper, "tty=")
    assert "argv=update" in text
    # os.setsid() dropped the controlling terminal, so install.sh's own probe
    # fails and it runs with INTERACTIVE=0.
    assert "tty=no" in text
    assert "status 0" in pane_when("update finished")

    A.tmux("kill-session", "-t", f"={A.UPDATE_SESSION}")
    assert not A.session_exists(A.UPDATE_SESSION)


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")
def test_pane_reports_the_wrappers_exit_status(client, tmp_path, monkeypatch):
    """The status line is the wrapper's, not the echo standing in front of it.

    A failed update has to be legible in the pane the user was told to open, so
    $? is caught before anything else in the command can overwrite it.
    """
    write_wrapper(tmp_path, monkeypatch, status=7)
    assert client.post("/api/update").status_code == 200
    assert "status 7" in pane_when("update finished")
    # A failed run keeps its pane: the status line is only useful with the
    # installer's output still above it.
    time.sleep(1.5)
    assert A.session_exists(A.UPDATE_SESSION)
    A.tmux("kill-session", "-t", f"={A.UPDATE_SESSION}")


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")
def test_a_successful_update_lets_its_session_go(client, fake_wrapper, monkeypatch):
    """Status 0 has nothing left to show, so the session leaves the list on its
    own rather than sitting there finished until someone kills it."""
    monkeypatch.setattr(A, "UPDATE_LINGER_OK", 1)
    assert client.post("/api/update").status_code == 200
    assert "status 0" in pane_when("update finished")
    deadline = time.time() + 6
    while A.session_exists(A.UPDATE_SESSION) and time.time() < deadline:
        time.sleep(0.2)
    assert not A.session_exists(A.UPDATE_SESSION)


def test_update_command_needs_an_executable(monkeypatch, tmp_path):
    plain = tmp_path / "pockettui"
    plain.write_text("#!/bin/bash\n")
    monkeypatch.setattr(A.shutil, "which",
                        lambda name: str(plain) if name == "pockettui" else "/usr/bin/tmux")
    assert A.update_command() is None
    plain.chmod(0o755)
    assert A.update_command() == str(plain)


def test_update_command_needs_tmux(monkeypatch, tmp_path):
    wrapper = tmp_path / "pockettui"
    wrapper.write_text("#!/bin/bash\n")
    wrapper.chmod(0o755)
    monkeypatch.setattr(A.shutil, "which",
                        lambda name: str(wrapper) if name == "pockettui" else None)
    assert A.update_command() is None
