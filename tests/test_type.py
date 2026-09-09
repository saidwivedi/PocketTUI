"""POST /api/session/type: text at the prompt, and never an Enter behind it.

Against a real tmux on an isolated socket (`-L pockettui-type-test`), the way
test_ws_attach.py's integration class and test_update.py do, because the claim
worth proving is one only a real pane can answer: that the command shows up on
the line the user is looking at and then nothing happens. A scripted tmux can
show what argv was built; only tmux itself can show that a shell handed those
keys did not run anything.
"""

import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as A  # noqa: E402

TMUX_TEST_BIN = ["tmux", "-L", "pockettui-type-test", "-f", "/dev/null"]

# Long enough for a shell that was going to run the line to have run it and
# printed something. The assertion is about the absence of output, so this is
# the whole strength of it.
SETTLE = 0.5


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


def pane(name):
    # "=name:" for the same reason the route uses it: capture-pane wants a pane,
    # and a bare session name is not one.
    return A.tmux("capture-pane", "-p", "-t", f"={name}:")[1]


def pane_when(name, marker, timeout=10.0):
    """The pane's text once it carries `marker`, or the last read."""
    deadline = time.monotonic() + timeout
    text = ""
    while time.monotonic() < deadline:
        text = pane(name)
        if marker in text:
            return text
        time.sleep(0.1)
    return text


def new_session(name, cols=80, rows=24):
    # `sh` rather than the user's login shell: a prompt this test can predict,
    # and no rc file to print something that would look like output.
    rc, _ = A.tmux("new-session", "-d", "-s", name,
                   "-x", str(cols), "-y", str(rows), "sh")
    assert rc == 0, f"could not create {name}"


def output_lines(text, marker):
    """Lines that are the command having run, not the command sitting there."""
    return [line for line in text.splitlines()
            if line.strip() == marker]


pytestmark = pytest.mark.skipif(shutil.which("tmux") is None,
                                reason="tmux not installed")


def test_the_command_sits_at_the_prompt_unsubmitted(client):
    new_session("t3")
    r = client.post("/api/session/type",
                    json={"name": "t3", "text": "echo TYPED_MARKER"})
    assert r.status_code == 200
    assert r.json() == {"session": "t3", "chars": len("echo TYPED_MARKER")}

    # It reaches the pane …
    text = pane_when("t3", "echo TYPED_MARKER")
    assert "echo TYPED_MARKER" in text
    # … and stays there. A shell that had been given an Enter would have echoed
    # TYPED_MARKER on a line of its own by now.
    time.sleep(SETTLE)
    assert output_lines(pane("t3"), "TYPED_MARKER") == []


def test_a_newline_in_the_request_submits_nothing(client):
    new_session("t3")
    r = client.post("/api/session/type",
                    json={"name": "t3", "text": "echo SECOND\n"})
    assert r.status_code == 200
    # The newline never left the server, so the pane holds the rest of the line
    # and the shell is still waiting on it.
    assert r.json()["chars"] == len("echo SECOND")
    assert "echo SECOND" in pane_when("t3", "echo SECOND")
    time.sleep(SETTLE)
    assert output_lines(pane("t3"), "SECOND") == []


def test_text_lands_in_the_view_this_device_is_watching(client):
    new_session("t3")
    # The phone's own grouped view of t3: a second session sharing the window,
    # which is what the shell attaches to and therefore what it must be typed
    # into.
    rc, _ = A.tmux("new-session", "-d", "-t", "t3", "-s", "phone-t3")
    assert rc == 0
    r = client.post("/api/session/type",
                    json={"name": "t3", "dev": "phone", "text": "echo VIEWED"})
    assert r.status_code == 200
    assert r.json()["session"] == "phone-t3"
    assert "echo VIEWED" in pane_when("phone-t3", "echo VIEWED")
    time.sleep(SETTLE)
    assert output_lines(pane("phone-t3"), "VIEWED") == []
