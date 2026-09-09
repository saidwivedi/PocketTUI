"""install.sh, in the pieces that decide what an *update* does to a live install.

The whole script cannot run here — it downloads a tarball, builds an
environment and writes a systemd unit — so each test lifts the region under
test out of install.sh by its own anchor lines and runs that under bash with
the surrounding helpers stubbed. Slicing rather than copying: a test that
carried its own copy of the code would keep passing after the real one drifted.

What is worth pinning down is the September field report: an install whose tmux
came from its own micromamba env was updated on a machine that had since grown
a system tmux, the installer re-decided the environment from scratch, and the
new unit named a different interpreter than the file on disk — which then
failed the byte match that says "this unit is ours", so it was left alone and
restarted with the default KillMode, taking every tmux session with it.
"""

import os
import subprocess
from pathlib import Path

import pytest

INSTALL_SH = Path(__file__).resolve().parent.parent / "install.sh"

# Everything the sliced regions call that lives outside them. note() prints so
# a test can read the changelog line back off stdout.
PRELUDE = r"""
set -eu
C_RESET=""; C_WARN=""; C_DIM=""; C_STEP=""; C_OK=""; C_CODE=""; C_RULE=""
say()  { printf '%s\n' "$*"; }
vsay() { [[ "${VERBOSE:-0}" == "1" ]] && printf '%s\n' "$*" || true; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
note() { printf 'NOTE: %s\n' "$*"; }
step() { :; }
step_quiet() { printf 'STEP: %s\n' "$*"; }
step_done() { :; }
touched_outside() { :; }
install_uv() { printf 'CALLED: install_uv\n'; return 1; }
install_micromamba() { printf 'CALLED: install_micromamba\n'; return 1; }
usable_python3() { return 1; }
pkg_install_cmd() { printf 'install %s' "$1"; }
pkg_name() { printf '%s' "$1"; }
MIN_PY_MINOR=10
"""


def slice_sh(start, end, include_end=False):
    """The lines of install.sh from `start` to `end`, matched whole and exact."""
    lines = INSTALL_SH.read_text().splitlines()
    try:
        i = lines.index(start)
        j = lines.index(end, i + 1)
    except ValueError as exc:  # an anchor moved: the test is stale, not the code
        raise AssertionError(f"anchor not found in install.sh: {exc}") from exc
    return "\n".join(lines[i:j + 1 if include_end else j]) + "\n"


ENV_SECTION = ('VENV_PY="$INSTALL_DIR/.venv/bin/python"', 'step_done "$ENV_LABEL"')
UNIT_SECTION = ('TMUX_BIN_DIR="$ENV_BIN"',
                "# The one unit shape that shipped before the marker existed, frozen. This is")
GBU_SECTION = ("generated_by_us() {", "}")
RC_SECTION = ("PATH_LINE_ADDED=0", "# Pairing token")
KILLMODE_SECTION = ("        UNIT_RESTART=1",
                    '        if [[ "$UNIT_RESTART" == "1" ]]; then')


def run_bash(tmp_path, body, env=None, name="harness.sh"):
    script = tmp_path / name
    script.write_text(PRELUDE + body)
    full = dict(os.environ)
    full.update(env or {})
    return subprocess.run(["bash", str(script)], capture_output=True, text=True,
                          env=full, timeout=60)


def fake_bin(tmp_path):
    """A PATH front holding the tools these tests want to catch being run."""
    d = tmp_path / "fakebin"
    d.mkdir()
    (d / "tmux").write_text('#!/bin/bash\necho "tmux 3.4"\n')
    # uv leaves a trace and would build a .venv, so a run that never touches it
    # is proof the strategy chain was skipped rather than merely repeated.
    (d / "uv").write_text(
        "#!/bin/bash\n"
        f'echo "$*" >> "{tmp_path}/uv-called.txt"\n'
        'if [[ "${1:-}" == "venv" ]]; then\n'
        '  mkdir -p "${!#}/bin" && printf "#!/bin/bash\\nexit 1\\n" > "${!#}/bin/python"\n'
        '  chmod +x "${!#}/bin/python"\n'
        'fi\n'
    )
    for f in d.iterdir():
        f.chmod(0o755)
    return d


def install_dir(tmp_path):
    d = tmp_path / "pockettui"
    d.mkdir()
    return d


def make_exe(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)


def env_harness(inst, have_tmux="1"):
    """The environment section, then the unit template it feeds."""
    return (
        f'INSTALL_DIR="{inst}"\n'
        f'MAMBA_PREFIX="$INSTALL_DIR/.micromamba"\n'
        'PORT=5560\n'
        f'HAVE_TMUX={have_tmux}\n'
        + slice_sh(*ENV_SECTION)
        + slice_sh(*UNIT_SECTION)
        + 'printf "ENV_KIND=%s\\nENV_LABEL=%s\\nVENV_PY=%s\\n" '
          '"$ENV_KIND" "$ENV_LABEL" "$VENV_PY"\n'
        + 'printf -- "---UNIT---\\n%s\\n" "$UNIT_CONTENT"\n'
    )


def parse(out):
    return dict(
        line.split("=", 1) for line in out.splitlines()
        if line.startswith(("ENV_KIND=", "ENV_LABEL=", "VENV_PY="))
    )


def unit_of(out):
    return out.split("---UNIT---\n", 1)[1]


# ---------------------------------------------------------------------------
# (a) an update keeps the environment the install already has
# ---------------------------------------------------------------------------

def test_update_reuses_micromamba_env_despite_new_system_tmux(tmp_path):
    """The field case: tmux and uv are both on PATH now, and neither matters."""
    inst = install_dir(tmp_path)
    make_exe(inst / ".micromamba/bin/python", "#!/bin/bash\nexit 0\n")
    make_exe(inst / ".micromamba/bin/tmux", '#!/bin/bash\necho "tmux 3.4"\n')
    bin_dir = fake_bin(tmp_path)

    r = run_bash(tmp_path, env_harness(inst),
                 env={"PATH": f"{bin_dir}:{os.environ['PATH']}"})
    assert r.returncode == 0, r.stderr

    got = parse(r.stdout)
    assert got["VENV_PY"] == f"{inst}/.micromamba/bin/python"
    assert got["ENV_LABEL"] == "micromamba"
    assert "reused the micromamba env" in r.stdout

    unit = unit_of(r.stdout)
    assert f"ExecStart={inst}/.micromamba/bin/python {inst}/app.py --port 5560" in unit
    assert f'Environment="PATH={inst}/.micromamba/bin:' in unit

    # The strategy chain never ran: uv was not called and no venv was built.
    assert not (tmp_path / "uv-called.txt").exists()
    assert not (inst / ".venv").exists()


def test_update_reuses_a_pip_venv(tmp_path):
    inst = install_dir(tmp_path)
    make_exe(inst / ".venv/bin/python",
             '#!/bin/bash\n[[ "$*" == "-m pip --version" ]] && exit 0\nexit 1\n')
    bin_dir = fake_bin(tmp_path)

    r = run_bash(tmp_path, env_harness(inst),
                 env={"PATH": f"{bin_dir}:{os.environ['PATH']}"})
    assert r.returncode == 0, r.stderr
    got = parse(r.stdout)
    assert got["VENV_PY"] == f"{inst}/.venv/bin/python"
    assert got["ENV_KIND"] == "venv"        # python -m pip, not uv pip
    assert not (tmp_path / "uv-called.txt").exists()


def test_update_reuses_a_uv_venv_and_keeps_uv_as_the_installer(tmp_path):
    """A uv-made venv has no pip, so the dependency step must stay on uv pip."""
    inst = install_dir(tmp_path)
    make_exe(inst / ".venv/bin/python", "#!/bin/bash\nexit 1\n")
    bin_dir = fake_bin(tmp_path)

    r = run_bash(tmp_path, env_harness(inst),
                 env={"PATH": f"{bin_dir}:{os.environ['PATH']}"})
    assert r.returncode == 0, r.stderr
    assert parse(r.stdout)["ENV_KIND"] == "venv (uv)"
    # uv was found on PATH, so it was not re-installed, and not run either.
    assert "CALLED: install_uv" not in r.stdout
    assert not (tmp_path / "uv-called.txt").exists()


def test_fresh_install_still_chooses_a_strategy(tmp_path):
    """Nothing on disk to adopt: the uv branch runs exactly as it always did."""
    inst = install_dir(tmp_path)
    bin_dir = fake_bin(tmp_path)

    r = run_bash(tmp_path, env_harness(inst),
                 env={"PATH": f"{bin_dir}:{os.environ['PATH']}"})
    assert r.returncode == 0, r.stderr
    got = parse(r.stdout)
    assert got["ENV_KIND"] == "venv (uv)"
    assert got["ENV_LABEL"] == "uv"
    assert (tmp_path / "uv-called.txt").exists()


# ---------------------------------------------------------------------------
# (b) our own unit, recognised by what it runs
# ---------------------------------------------------------------------------

MICROMAMBA_UNIT = """[Unit]
Description=PocketTUI tmux terminal backend (port 5560)
After=network.target
StartLimitIntervalSec=0

[Service]
Type=simple
Restart=always
RestartSec=10
WorkingDirectory={inst}
Environment="PATH={py_dir}:/usr/local/bin:/usr/bin:/bin"
ExecStart={py} {app}/app.py --port 5560

[Install]
WantedBy=default.target
"""


def gbu_harness(inst, path):
    return (
        f'INSTALL_DIR="{inst}"\n'
        + slice_sh(*GBU_SECTION, include_end=True)
        + f'if generated_by_us "{path}" "#MARKER" "LEGACY" "$INSTALL_DIR/app.py"; then\n'
          '  echo OURS\nelse\n  echo THEIRS\nfi\n'
    )


def test_pre_marker_unit_is_ours_when_it_runs_our_app(tmp_path):
    """The byte match fails here — the interpreter and PATH line have drifted."""
    inst = install_dir(tmp_path)
    unit = tmp_path / "pockettui.service"
    unit.write_text(MICROMAMBA_UNIT.format(
        inst=inst, app=inst, py=f"{inst}/.micromamba/bin/python",
        py_dir=f"{inst}/.micromamba/bin"))

    r = run_bash(tmp_path, gbu_harness(inst, unit))
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "OURS"


def test_a_unit_running_someone_elses_app_is_left_alone(tmp_path):
    inst = install_dir(tmp_path)
    unit = tmp_path / "other.service"
    unit.write_text(MICROMAMBA_UNIT.format(
        inst="/opt/other", app="/opt/other", py="/usr/bin/python3",
        py_dir="/usr/bin"))

    r = run_bash(tmp_path, gbu_harness(inst, unit))
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "THEIRS"


def test_a_plist_running_our_app_is_ours(tmp_path):
    inst = install_dir(tmp_path)
    plist = tmp_path / "com.pockettui.plist"
    plist.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<plist version="1.0"><dict>\n'
        '  <key>ProgramArguments</key>\n  <array>\n'
        f'    <string>{inst}/.micromamba/bin/python</string>\n'
        f'    <string>{inst}/app.py</string>\n'
        '  </array>\n</dict></plist>\n')

    r = run_bash(tmp_path, gbu_harness(inst, plist))
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "OURS"


def test_a_file_that_only_mentions_our_app_is_not_ours(tmp_path):
    """A comment or a WorkingDirectory naming the install is not an ExecStart."""
    inst = install_dir(tmp_path)
    unit = tmp_path / "mentions.service"
    unit.write_text(
        "[Service]\n"
        f"# replaces {inst}/app.py\n"
        f"WorkingDirectory={inst}\n"
        "ExecStart=/usr/bin/true\n")

    r = run_bash(tmp_path, gbu_harness(inst, unit))
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "THEIRS"


def test_the_marker_still_says_ours(tmp_path):
    inst = install_dir(tmp_path)
    unit = tmp_path / "marked.service"
    unit.write_text("#MARKER\n[Service]\nExecStart=/usr/bin/true\n")

    r = run_bash(tmp_path, gbu_harness(inst, unit))
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "OURS"


# ---------------------------------------------------------------------------
# (c) ~/.local/bin on PATH
# ---------------------------------------------------------------------------

def rc_harness(home, off_path="1", interactive="1", answer=0):
    return (
        f'USER_BIN="{home}/.local/bin"\n'
        f'WRAPPER_PATH="$USER_BIN/pockettui"\n'
        f'WRAPPER_OFF_PATH={off_path}\n'
        f'INTERACTIVE={interactive}\n'
        f'confirm() {{ return {answer}; }}\n'
        + slice_sh(*RC_SECTION)
        + 'printf "PATH_LINE_ADDED=%s\\n" "$PATH_LINE_ADDED"\n'
    )


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    (h / ".local/bin").mkdir(parents=True)
    return h


def test_path_line_is_appended_once_and_only_once(tmp_path, home):
    rc = home / ".zshrc"
    rc.write_text("alias ll='ls -l'\n")

    body = rc_harness(home)
    first = run_bash(tmp_path, body, env={"HOME": str(home)}, name="rc1.sh")
    assert first.returncode == 0, first.stderr
    assert "PATH_LINE_ADDED=1" in first.stdout
    assert f'export PATH="$HOME/.local/bin:$PATH"' in rc.read_text()
    assert "added by PocketTUI" in rc.read_text()
    after_first = rc.read_text()

    second = run_bash(tmp_path, body, env={"HOME": str(home)}, name="rc2.sh")
    assert second.returncode == 0, second.stderr
    assert "PATH_LINE_ADDED=0" in second.stdout
    assert rc.read_text() == after_first


def test_both_rc_files_get_the_line_and_a_missing_one_is_not_created(tmp_path, home):
    (home / ".bashrc").write_text("# bash\n")
    r = run_bash(tmp_path, rc_harness(home), env={"HOME": str(home)})
    assert r.returncode == 0, r.stderr
    assert 'export PATH="$HOME/.local/bin:$PATH"' in (home / ".bashrc").read_text()
    assert not (home / ".zshrc").exists()


def test_nothing_happens_when_the_dir_is_already_on_path(tmp_path, home):
    rc = home / ".zshrc"
    rc.write_text("# nothing here\n")
    r = run_bash(tmp_path, rc_harness(home, off_path="0"),
                 env={"HOME": str(home)})
    assert r.returncode == 0, r.stderr
    assert "PATH_LINE_ADDED=0" in r.stdout
    assert rc.read_text() == "# nothing here\n"


def test_an_rc_that_already_has_its_own_line_is_left_alone(tmp_path, home):
    rc = home / ".bashrc"
    rc.write_text('PATH="$HOME/.local/bin:$PATH"\n')
    r = run_bash(tmp_path, rc_harness(home), env={"HOME": str(home)})
    assert r.returncode == 0, r.stderr
    assert "PATH_LINE_ADDED=0" in r.stdout
    assert rc.read_text() == 'PATH="$HOME/.local/bin:$PATH"\n'


def test_a_commented_out_line_does_not_count(tmp_path, home):
    rc = home / ".bashrc"
    rc.write_text('# export PATH="$HOME/.local/bin:$PATH"\n')
    r = run_bash(tmp_path, rc_harness(home), env={"HOME": str(home)})
    assert r.returncode == 0, r.stderr
    assert "PATH_LINE_ADDED=1" in r.stdout


def test_declining_touches_nothing(tmp_path, home):
    rc = home / ".zshrc"
    rc.write_text("# mine\n")
    r = run_bash(tmp_path, rc_harness(home, answer=1), env={"HOME": str(home)})
    assert r.returncode == 0, r.stderr
    assert "PATH_LINE_ADDED=0" in r.stdout
    assert rc.read_text() == "# mine\n"


def test_a_non_interactive_run_only_says_so(tmp_path, home):
    rc = home / ".zshrc"
    rc.write_text("# mine\n")
    r = run_bash(tmp_path, rc_harness(home, interactive="0"),
                 env={"HOME": str(home)})
    assert r.returncode == 0, r.stderr
    assert "PATH_LINE_ADDED=0" in r.stdout
    assert "NOTE: skipped the PATH line" in r.stdout
    assert rc.read_text() == "# mine\n"


# ---------------------------------------------------------------------------
# restarting somebody else's unit, when that unit kills its own control group
# ---------------------------------------------------------------------------

def killmode_harness(unit, interactive="1", answer=0):
    return (
        f'UNIT_PATH="{unit}"\n'
        'SERVICE_NAME=pockettui\n'
        f'INTERACTIVE={interactive}\n'
        f'confirm() {{ printf "ASKED: %s\\n" "$*"; return {answer}; }}\n'
        + slice_sh(*KILLMODE_SECTION)
        + 'printf "UNIT_RESTART=%s\\n" "$UNIT_RESTART"\n'
    )


def test_a_unit_without_killmode_is_not_restarted_behind_the_users_back(tmp_path):
    unit = tmp_path / "theirs.service"
    unit.write_text("[Service]\nExecStart=/usr/bin/true\n")

    r = run_bash(tmp_path, killmode_harness(unit, answer=1))
    assert r.returncode == 0, r.stderr
    assert "KillMode=process" in r.stdout          # the warning names the fix
    assert "ends every tmux session" in r.stdout
    assert "ASKED:" in r.stdout
    assert "UNIT_RESTART=0" in r.stdout


def test_saying_yes_restarts_it(tmp_path):
    unit = tmp_path / "theirs.service"
    unit.write_text("[Service]\nExecStart=/usr/bin/true\n")

    r = run_bash(tmp_path, killmode_harness(unit, answer=0))
    assert r.returncode == 0, r.stderr
    assert "UNIT_RESTART=1" in r.stdout


def test_a_unit_that_already_has_killmode_is_restarted_without_a_word(tmp_path):
    unit = tmp_path / "theirs.service"
    unit.write_text("[Service]\nKillMode=process\nExecStart=/usr/bin/true\n")

    r = run_bash(tmp_path, killmode_harness(unit))
    assert r.returncode == 0, r.stderr
    assert "ASKED:" not in r.stdout
    assert "ends every tmux session" not in r.stdout
    assert "UNIT_RESTART=1" in r.stdout


def test_a_non_interactive_run_warns_and_carries_on(tmp_path):
    unit = tmp_path / "theirs.service"
    unit.write_text("[Service]\nExecStart=/usr/bin/true\n")

    r = run_bash(tmp_path, killmode_harness(unit, interactive="0"))
    assert r.returncode == 0, r.stderr
    assert "ends every tmux session" in r.stdout
    assert "ASKED:" not in r.stdout
    assert "NOTE: restarted pockettui without KillMode=process" in r.stdout
    assert "UNIT_RESTART=1" in r.stdout


def test_no_rc_file_at_all_leaves_the_hint_to_do_the_work(tmp_path, home):
    r = run_bash(tmp_path, rc_harness(home), env={"HOME": str(home)})
    assert r.returncode == 0, r.stderr
    assert "PATH_LINE_ADDED=0" in r.stdout
    assert not (home / ".bashrc").exists()
    assert not (home / ".zshrc").exists()


# ---------------------------------------------------------------------------
# (d) is the backend answering, and if not, why not
# ---------------------------------------------------------------------------

WAIT_SECTION = ("SERVER_UP=0", "start_service() {")
WRAPPER_HELPERS = ("local_version() {", "run_installer() {")
WRAPPER_CASE = ('case "${1:-}" in', "EOF")


def fake_curl(tmp_path, code):
    """A curl that answers the probe with one scripted HTTP code."""
    d = tmp_path / "curlbin"
    make_exe(d / "curl", f'#!/bin/bash\nprintf %s "{code}"\n')
    return d


def with_curl(tmp_path, code):
    return {"PATH": f"{fake_curl(tmp_path, code)}:{os.environ['PATH']}"}


# runtime.json as app.py writes it, with the pid left to the caller.
RUNTIME_JSON = ('{"version":"0.9.12","pid":%s,"host":"127.0.0.1",'
                '"port":5560,"started_at":"2026-09-10T09:00:00Z"}')


def wait_harness(inst, runtime=""):
    return (
        f'INSTALL_DIR="{inst}"\n'
        "PORT=5560\n"
        + runtime
        + slice_sh(*WAIT_SECTION)
        + 'if wait_for_server 2; then echo UP; else echo DOWN; fi\n'
          'printf "SERVER_UP=%s PROBE_CODE=%s\\n" "$SERVER_UP" "$PROBE_CODE"\n'
          'describe_backend_failure\n'
    )


def live_pid(inst):
    """This very shell is the backend: a pid that is certainly alive."""
    return "printf '%s\\n' \"$$\" > \"%s/runtime.json\"\n" % (RUNTIME_JSON, inst)


def dead_pid(inst):
    """A pid that existed and was reaped: the started-then-crashed case."""
    return ("( exit 0 ) & dead=$!\n"
            'wait "$dead" 2>/dev/null || true\n'
            + "printf '%s\\n' \"$dead\" > \"%s/runtime.json\"\n" % (RUNTIME_JSON, inst))


@pytest.mark.parametrize("code", ["200", "404"])
def test_the_descriptor_and_an_older_build_both_count_as_up(tmp_path, code):
    """404 is a version that predates the descriptor route, not a dead server."""
    inst = install_dir(tmp_path)
    r = run_bash(tmp_path, wait_harness(inst), env=with_curl(tmp_path, code))
    assert r.returncode == 0, r.stderr
    assert "UP" in r.stdout
    assert f"SERVER_UP=1 PROBE_CODE={code}" in r.stdout


@pytest.mark.parametrize("code", ["502", "000"])
def test_a_proxy_error_or_no_answer_is_not_up(tmp_path, code):
    inst = install_dir(tmp_path)
    r = run_bash(tmp_path, wait_harness(inst), env=with_curl(tmp_path, code))
    assert r.returncode == 0, r.stderr
    assert "DOWN" in r.stdout
    assert f"SERVER_UP=0 PROBE_CODE={code}" in r.stdout


def test_a_live_pid_that_will_not_answer_is_named_as_such(tmp_path):
    inst = install_dir(tmp_path)
    r = run_bash(tmp_path, wait_harness(inst, live_pid(inst)),
                 env=with_curl(tmp_path, "502"))
    assert r.returncode == 0, r.stderr
    assert "The backend is up (pid" in r.stdout
    assert "on port 5560 but the HTTP probe failed (last answer: 502)." in r.stdout


def test_a_dead_pid_reads_as_started_and_exited(tmp_path):
    inst = install_dir(tmp_path)
    r = run_bash(tmp_path, wait_harness(inst, dead_pid(inst)),
                 env=with_curl(tmp_path, "000"))
    assert r.returncode == 0, r.stderr
    assert "is gone: the server started and then exited." in r.stdout


def test_no_runtime_file_reads_as_never_started(tmp_path):
    inst = install_dir(tmp_path)
    r = run_bash(tmp_path, wait_harness(inst), env=with_curl(tmp_path, "000"))
    assert r.returncode == 0, r.stderr
    assert f"The backend never started (no {inst}/runtime.json)." in r.stdout


# ---------------------------------------------------------------------------
# (e) the wrapper's own status subcommand
# ---------------------------------------------------------------------------

def wrapper_harness(inst, runtime="", args="status", installer_rc=0):
    """The wrapper's helpers and its case block, with run_installer stubbed."""
    return (
        f'INSTALL_DIR="{inst}"\n'
        'BASE_URL="https://pockettui.invalid"\n'
        'SERVICE_NAME="pockettui"\n'
        'WRAPPER_BIN="/tmp/bin"\n'
        "PORT=5560\n"
        + runtime
        + slice_sh(*WRAPPER_HELPERS)
        + 'run_installer() { printf "CALLED: run_installer %s\\n" "$*"; '
          f'return {installer_rc}; }}\n'
          f'set -- {args}\n'
        + slice_sh(*WRAPPER_CASE)
    )


def test_status_reports_version_backend_and_probe(tmp_path):
    inst = install_dir(tmp_path)
    (inst / "VERSION").write_text("0.9.12\n")
    r = run_bash(tmp_path, wrapper_harness(inst, live_pid(inst)),
                 env=with_curl(tmp_path, "200"))
    assert r.returncode == 0, r.stderr
    lines = [l for l in r.stdout.splitlines() if l.startswith(("installed", "backend", "probe"))]
    assert len(lines) == 3, r.stdout
    assert lines[0] == "installed  0.9.12"
    assert lines[1].startswith("backend    running (pid ")
    assert lines[1].endswith(", port 5560)")
    assert lines[2] == "probe      answering on http://127.0.0.1:5560"


def test_status_says_so_when_nothing_is_running(tmp_path):
    inst = install_dir(tmp_path)
    r = run_bash(tmp_path, wrapper_harness(inst), env=with_curl(tmp_path, "000"))
    assert r.returncode == 0, r.stderr
    assert "installed  unknown" in r.stdout
    assert f"backend    not running (no {inst}/runtime.json)" in r.stdout
    assert "probe      no answer on http://127.0.0.1:5560" in r.stdout


def test_status_separates_a_live_process_from_an_unreachable_one(tmp_path):
    """The case that sends the user to the proxy rather than to the service."""
    inst = install_dir(tmp_path)
    r = run_bash(tmp_path, wrapper_harness(inst, live_pid(inst)),
                 env=with_curl(tmp_path, "502"))
    assert r.returncode == 0, r.stderr
    assert "backend    running (pid " in r.stdout
    assert "probe      http://127.0.0.1:5560 answered 502" in r.stdout


def test_status_never_runs_the_installer(tmp_path):
    inst = install_dir(tmp_path)
    r = run_bash(tmp_path, wrapper_harness(inst), env=with_curl(tmp_path, "200"))
    assert r.returncode == 0, r.stderr
    assert "CALLED: run_installer" not in r.stdout


WRAPPER_WRITE_SECTION = ("WRAPPER_SAME=0", "fi")

NEW_WRAPPER = "#!/bin/bash\nthe new wrapper"


def wrapper_write_harness(user_bin):
    """The region that puts the generated wrapper on disk, and nothing else."""
    return (
        f'USER_BIN="{user_bin}"\n'
        'WRAPPER_PATH="$USER_BIN/pockettui"\n'
        "WRAPPER_WRITTEN=0\n"
        f"WRAPPER_CONTENT='{NEW_WRAPPER}'\n"
        + slice_sh(*WRAPPER_WRITE_SECTION, include_end=True)
        + 'printf "WRAPPER_WRITTEN=%s\\n" "$WRAPPER_WRITTEN"\n'
    )


def test_the_wrapper_is_renamed_over_not_rewritten_in_place(tmp_path):
    """An update is driven by the wrapper, and bash reads a script by offset:
    truncating the same inode made the live wrapper resume inside the new bytes
    and print the usage block after a successful update."""
    user_bin = tmp_path / "bin"
    user_bin.mkdir()
    wrapper = user_bin / "pockettui"
    make_exe(wrapper, "#!/bin/bash\nthe old wrapper\n")
    before = wrapper.stat().st_ino

    r = run_bash(tmp_path, wrapper_write_harness(user_bin))
    assert r.returncode == 0, r.stderr
    assert "WRAPPER_WRITTEN=1" in r.stdout

    after = wrapper.stat()
    assert after.st_ino != before, "the wrapper was rewritten in place"
    assert wrapper.read_text() == NEW_WRAPPER + "\n"
    assert after.st_mode & 0o111, "the renamed wrapper is not executable"
    # The temp file it went through is gone: only the command is left.
    assert [p.name for p in user_bin.iterdir()] == ["pockettui"]


# ---------------------------------------------------------------------------
# (f) an update that does not work has to be undoable
# ---------------------------------------------------------------------------

SNAPSHOT_SECTION = ('    if [[ "$UPDATE" == "1" ]]; then',
                    "        # The tarball ships the complete vendor set, so anything left in there")
STATE_SECTION = ("write_update_state() {", "}")
RESTORE_SECTION = ("restore_prev() {", "}")
SELFCHECK_SECTION = ('step "Checking the new code"', "fi")
ROLLBACK_SECTION = ("rollback_update() {", "}")

# Every file the update overwrites, which is exactly what the snapshot has to
# hold. vendor/ is the one directory in the list and the one that would nest if
# it were copied back onto itself.
SNAPSHOT_FILES = ["app.py", "resolver.py", "mobile_app.html", "sw.js",
                  "pockettui.service", "install.sh", "setup_voice.sh",
                  "requirements.txt", "qrcodegen.py", "icon-192.png",
                  "icon-512.png", "VERSION"]


def fill_install(inst, mark):
    """An install dir holding one recognisable version of every file."""
    for f in SNAPSHOT_FILES:
        (inst / f).write_text(f"{f} {mark}\n")
    (inst / "vendor").mkdir(exist_ok=True)
    (inst / "vendor/xterm.js").write_text(f"xterm {mark}\n")
    (inst / "VERSION").write_text("0.9.12\n" if mark == "old" else "0.9.13\n")


def state_of(inst):
    return (inst / "update-state.json").read_text()


def snapshot_harness(inst):
    """The head of the update branch, up to the point it starts overwriting."""
    return (
        f'INSTALL_DIR="{inst}"\n'
        "VERBOSE=0\nUPDATE=1\nROTATE_TOKEN=0\nPREV_SAVED=0\n"
        'OLD_VERSION="0.9.12"\nNEW_VERSION="0.9.13"\n'
        + slice_sh(*STATE_SECTION, include_end=True)
        + slice_sh(*SNAPSHOT_SECTION)
        # The slice stops inside the branch it opened, on purpose: everything
        # after it is the overwriting this test is not running.
        + "fi\n"
          'printf "PREV_SAVED=%s\\n" "$PREV_SAVED"\n'
    )


def test_the_snapshot_holds_a_byte_copy_of_every_file_the_update_replaces(tmp_path):
    inst = install_dir(tmp_path)
    fill_install(inst, "old")
    r = run_bash(tmp_path, snapshot_harness(inst))
    assert r.returncode == 0, r.stderr
    assert "PREV_SAVED=1" in r.stdout
    for f in SNAPSHOT_FILES:
        assert (inst / ".prev" / f).read_bytes() == (inst / f).read_bytes(), f
    assert (inst / ".prev/vendor/xterm.js").read_text() == "xterm old\n"


def test_the_snapshot_records_that_an_update_is_running(tmp_path):
    inst = install_dir(tmp_path)
    fill_install(inst, "old")
    r = run_bash(tmp_path, snapshot_harness(inst))
    assert r.returncode == 0, r.stderr
    written = state_of(inst)
    assert '"status":"running"' in written
    assert '"from":"0.9.12"' in written
    assert '"to":"0.9.13"' in written
    # Written whole and renamed, so nothing is left half way.
    assert not (inst / "update-state.json.tmp").exists()


def selfcheck_harness(inst, ok, update="1", prev_saved="1"):
    make_exe(inst / "fakepy", "#!/bin/bash\nexit %d\n" % (0 if ok else 1))
    return (
        f'INSTALL_DIR="{inst}"\n'
        f'VENV_PY="{inst}/fakepy"\n'
        f"VERBOSE=0\nUPDATE={update}\nPREV_SAVED={prev_saved}\n"
        'OLD_VERSION="0.9.12"\nNEW_VERSION="0.9.13"\n'
        + slice_sh(*STATE_SECTION, include_end=True)
        + slice_sh(*RESTORE_SECTION, include_end=True)
        + slice_sh(*SELFCHECK_SECTION, include_end=True)
        + "echo PAST_THE_CHECK\n"
    )


def test_code_that_loads_is_waved_through(tmp_path):
    inst = install_dir(tmp_path)
    r = run_bash(tmp_path, selfcheck_harness(inst, ok=True))
    assert r.returncode == 0, r.stderr
    assert "PAST_THE_CHECK" in r.stdout


def test_code_that_does_not_load_puts_the_old_version_back(tmp_path):
    """Nothing is restarted: the check runs before the service is touched, so a
    failure here costs the user nothing but the update."""
    inst = install_dir(tmp_path)
    fill_install(inst, "old")
    (inst / ".prev").mkdir()
    for f in SNAPSHOT_FILES:
        (inst / ".prev" / f).write_text(f"{f} old\n")
    (inst / ".prev/VERSION").write_text("0.9.12\n")
    (inst / ".prev/vendor").mkdir()
    (inst / ".prev/vendor/xterm.js").write_text("xterm old\n")
    fill_install(inst, "new")          # what the update just unpacked

    r = run_bash(tmp_path, selfcheck_harness(inst, ok=False))
    assert r.returncode == 1
    assert "PAST_THE_CHECK" not in r.stdout
    assert "restored version 0.9.12" in r.stderr
    assert "nothing was restarted" in r.stderr
    for f in SNAPSHOT_FILES:
        assert (inst / f).read_bytes() == (inst / ".prev" / f).read_bytes(), f
    assert (inst / "VERSION").read_text() == "0.9.12\n"
    # cp -R onto a directory that is already there nests it; the restore removes
    # the target first, so vendor/ must not have grown a vendor/ of its own.
    assert (inst / "vendor/xterm.js").read_text() == "xterm old\n"
    assert not (inst / "vendor/vendor").exists()
    assert '"status":"failed"' in state_of(inst)


def test_a_fresh_install_that_fails_the_check_just_stops(tmp_path):
    """There is no previous version to go back to, so the only thing to do is
    say which command shows why."""
    inst = install_dir(tmp_path)
    r = run_bash(tmp_path, selfcheck_harness(inst, ok=False, update="0",
                                             prev_saved="0"))
    assert r.returncode == 1
    assert "failed its self-check" in r.stderr
    assert "--selfcheck" in r.stderr
    assert not (inst / "update-state.json").exists()


def sequenced_curl(tmp_path, codes):
    """A curl that answers each call with the next code, then repeats the last."""
    d = tmp_path / "curlbin"
    calls = tmp_path / "curl-calls"
    make_exe(d / "curl",
             "#!/bin/bash\n"
             f'n="$(cat "{calls}" 2>/dev/null || echo 0)"\n'
             f'echo $((n + 1)) > "{calls}"\n'
             f'codes=({" ".join(codes)})\n'
             'i="$n"\n'
             '[[ "$i" -ge "${#codes[@]}" ]] && i=$(( ${#codes[@]} - 1 ))\n'
             'printf %s "${codes[$i]}"\n')
    return d


def rollback_harness(inst, how="systemd"):
    return (
        f'INSTALL_DIR="{inst}"\n'
        "PORT=5560\nVERBOSE=0\n"
        'SERVICE_NAME="pockettui"\nAGENT_LABEL="com.pockettui.server"\n'
        "ROLLED_BACK=0\n"
        'OLD_VERSION="0.9.12"\nNEW_VERSION="0.9.13"\n'
        + slice_sh(*STATE_SECTION, include_end=True)
        + slice_sh(*RESTORE_SECTION, include_end=True)
        + slice_sh(*WAIT_SECTION)
        + slice_sh(*ROLLBACK_SECTION, include_end=True)
        + f"rollback_update {how} || true\n"
          'printf "ROLLED_BACK=%s\\n" "$ROLLED_BACK"\n'
    )


def prev_only(inst):
    """The state after an update: new files in place, old ones in .prev."""
    (inst / ".prev").mkdir()
    for f in SNAPSHOT_FILES:
        (inst / ".prev" / f).write_text(f"{f} old\n")
    (inst / ".prev/VERSION").write_text("0.9.12\n")
    fill_install(inst, "new")


def test_a_service_that_will_not_answer_is_rolled_back_and_restarted(tmp_path):
    inst = install_dir(tmp_path)
    prev_only(inst)
    curl = sequenced_curl(tmp_path, ["502", "200"])
    log = tmp_path / "restart.txt"
    make_exe(curl / "systemctl", f'#!/bin/bash\necho "systemctl $*" >> "{log}"\n')
    r = run_bash(tmp_path, rollback_harness(inst),
                 env={"PATH": f"{curl}:{os.environ['PATH']}"})
    assert r.returncode == 0, r.stderr
    assert log.read_text().strip() == "systemctl --user restart pockettui"
    assert "Rolled back to version 0.9.12." in r.stdout
    assert "ROLLED_BACK=1" in r.stdout
    assert (inst / "app.py").read_text() == "app.py old\n"
    assert (inst / "VERSION").read_text() == "0.9.12\n"
    written = state_of(inst)
    assert '"status":"rolled_back"' in written
    assert '"exit":1' in written


def test_a_rollback_that_does_not_come_back_either_says_failed(tmp_path):
    inst = install_dir(tmp_path)
    prev_only(inst)
    curl = sequenced_curl(tmp_path, ["502"])
    make_exe(curl / "systemctl", "#!/bin/bash\nexit 0\n")
    r = run_bash(tmp_path, rollback_harness(inst),
                 env={"PATH": f"{curl}:{os.environ['PATH']}"})
    assert r.returncode == 0, r.stderr
    assert "ROLLED_BACK=0" in r.stdout
    assert '"status":"failed"' in state_of(inst)


def test_nothing_to_roll_back_to_leaves_the_install_where_it_is(tmp_path):
    inst = install_dir(tmp_path)
    fill_install(inst, "new")
    curl = sequenced_curl(tmp_path, ["502"])
    make_exe(curl / "systemctl", "#!/bin/bash\nexit 0\n")
    r = run_bash(tmp_path, rollback_harness(inst),
                 env={"PATH": f"{curl}:{os.environ['PATH']}"})
    assert r.returncode == 0, r.stderr
    assert "no snapshot to roll back to" in r.stdout
    assert "ROLLED_BACK=0" in r.stdout
    assert (inst / "app.py").read_text() == "app.py new\n"
    assert '"status":"failed"' in state_of(inst)


def test_the_mac_path_kickstarts_the_agent_instead(tmp_path):
    inst = install_dir(tmp_path)
    prev_only(inst)
    curl = sequenced_curl(tmp_path, ["200"])
    log = tmp_path / "restart.txt"
    make_exe(curl / "launchctl", f'#!/bin/bash\necho "launchctl $*" >> "{log}"\n')
    r = run_bash(tmp_path, rollback_harness(inst, "launchd"),
                 env={"PATH": f"{curl}:{os.environ['PATH']}"})
    assert r.returncode == 0, r.stderr
    assert log.read_text().startswith("launchctl kickstart -k gui/")
    assert "ROLLED_BACK=1" in r.stdout


# ---------------------------------------------------------------------------
# (g) the wrapper refuses to install an older build over a newer one
# ---------------------------------------------------------------------------

def test_an_older_remote_version_is_refused(tmp_path):
    inst = install_dir(tmp_path)
    (inst / "VERSION").write_text("0.9.12\n")
    r = run_bash(tmp_path, wrapper_harness(inst, args="update"),
                 env=with_curl(tmp_path, "0.9.10"))
    assert r.returncode == 1
    assert "Refusing to downgrade 0.9.12 -> 0.9.10" in r.stderr
    assert "--allow-downgrade" in r.stderr
    assert "CALLED: run_installer" not in r.stdout


def test_the_flag_allows_it_and_is_not_passed_on(tmp_path):
    """install.sh has no such flag, so handing it over would be an error the
    user never made."""
    inst = install_dir(tmp_path)
    (inst / "VERSION").write_text("0.9.12\n")
    r = run_bash(tmp_path, wrapper_harness(inst, args="update --allow-downgrade -v"),
                 env=with_curl(tmp_path, "0.9.10"))
    assert r.returncode == 0, r.stderr
    assert "CALLED: run_installer -v" in r.stdout
    assert "--allow-downgrade" not in r.stdout


def test_a_newer_remote_version_runs_the_installer(tmp_path):
    inst = install_dir(tmp_path)
    (inst / "VERSION").write_text("0.9.12\n")
    r = run_bash(tmp_path, wrapper_harness(inst, args="update"),
                 env=with_curl(tmp_path, "0.9.13"))
    assert r.returncode == 0, r.stderr
    assert "CALLED: run_installer" in r.stdout


def test_an_unknown_version_on_either_side_is_not_a_comparison(tmp_path):
    """No VERSION file, or no network: the update goes ahead as it always did."""
    inst = install_dir(tmp_path)
    r = run_bash(tmp_path, wrapper_harness(inst, args="update"),
                 env=with_curl(tmp_path, "0.9.10"))
    assert r.returncode == 0, r.stderr
    assert "CALLED: run_installer" in r.stdout


def test_the_double_digit_field_is_compared_as_a_number(tmp_path):
    """0.9.9 -> 0.9.10 is an update, not a downgrade, which is the whole reason
    the comparison is not a string one."""
    inst = install_dir(tmp_path)
    (inst / "VERSION").write_text("0.9.9\n")
    r = run_bash(tmp_path, wrapper_harness(inst, args="update"),
                 env=with_curl(tmp_path, "0.9.10"))
    assert r.returncode == 0, r.stderr
    assert "CALLED: run_installer" in r.stdout


def test_an_installer_that_died_mid_update_closes_the_record(tmp_path):
    """Otherwise the phone watches a "running" that nothing will ever end."""
    inst = install_dir(tmp_path)
    (inst / "VERSION").write_text("0.9.12\n")
    (inst / "update-state.json").write_text(
        '{"status":"running","from":"0.9.12","to":"0.9.13","exit":0,"ts":1757500000}\n')
    r = run_bash(tmp_path, wrapper_harness(inst, args="update", installer_rc=3),
                 env=with_curl(tmp_path, "0.9.13"))
    assert r.returncode == 3
    written = state_of(inst)
    assert '"status":"failed"' in written
    assert '"exit":3' in written


def test_a_finished_record_is_left_alone(tmp_path):
    """install.sh already said how it ended; the wrapper must not overwrite it."""
    inst = install_dir(tmp_path)
    (inst / "VERSION").write_text("0.9.12\n")
    (inst / "update-state.json").write_text('{"status":"rolled_back","exit":1}\n')
    r = run_bash(tmp_path, wrapper_harness(inst, args="update", installer_rc=1),
                 env=with_curl(tmp_path, "0.9.13"))
    assert r.returncode == 1
    assert state_of(inst) == '{"status":"rolled_back","exit":1}\n'
