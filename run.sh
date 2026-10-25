#!/bin/bash
# Run the PocketTUI tmux terminal backend (port 5560) from a clone.
#
# Picks an environment without asking: a micromamba `pockettui` env if one is
# already there, otherwise a plain venv at .venv next to this script, created on
# first run. Nothing outside the clone is installed or modified.

set -eu

# `readlink -f` is GNU-only (macOS ships the BSD one, which has no -f), so
# resolve the directory the portable way instead.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

MIN_PY_MINOR=10

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# The install command for whichever package manager this machine actually has,
# so a missing tmux points at something runnable rather than an apt line on a
# Fedora box. run.sh only ever prints these: it never installs anything system
# wide and never uses sudo.
pkg_install_cmd() {
    local pkg="$1"
    if command -v brew >/dev/null 2>&1;    then printf 'brew install %s' "$pkg"
    elif command -v apt-get >/dev/null 2>&1; then printf 'sudo apt-get install -y %s' "$pkg"
    elif command -v dnf >/dev/null 2>&1;     then printf 'sudo dnf install -y %s' "$pkg"
    elif command -v yum >/dev/null 2>&1;     then printf 'sudo yum install -y %s' "$pkg"
    elif command -v pacman >/dev/null 2>&1;  then printf 'sudo pacman -S --noconfirm %s' "$pkg"
    elif command -v zypper >/dev/null 2>&1;  then printf 'sudo zypper install -y %s' "$pkg"
    elif command -v apk >/dev/null 2>&1;     then printf 'sudo apk add %s' "$pkg"
    else printf 'install %s with your package manager' "$pkg"
    fi
}

# A missing tmux is install.sh's problem to solve, not a run script's: it can
# provide one user-space from conda-forge, which is more than this should do
# behind the user's back on a plain `./run.sh`.
if ! command -v tmux >/dev/null 2>&1; then
    die "tmux not found. Run ./install.sh, which can install one into this
       install's own environment without root — or install it yourself:
       $(pkg_install_cmd tmux)"
fi

# ---------------------------------------------------------------------------
# Environment: an existing micromamba env, else a venv in the clone
# ---------------------------------------------------------------------------
# micromamba is optional. Only an env that is already there is used — this
# script never creates one, so a machine without micromamba is not a lesser
# case, just the venv path.
if command -v micromamba >/dev/null 2>&1 \
   && micromamba env list 2>/dev/null | awk '{print $1}' | grep -qx pockettui; then
    exec micromamba run -n pockettui python app.py --port 5560 "$@"
fi

VENV="$SCRIPT_DIR/.venv"
VENV_PY="$VENV/bin/python"

# Is there a python3 that is both new enough and able to build a venv?
usable_python3() {
    local ver major minor
    command -v python3 >/dev/null 2>&1 || return 1
    ver="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)" || return 1
    major="${ver%%.*}"
    minor="${ver##*.}"
    [[ "$major" -eq 3 ]] && [[ "$minor" -ge "$MIN_PY_MINOR" ]] || return 1
    python3 -c 'import venv, ensurepip' >/dev/null 2>&1
}

# Missing venv, or one that predates a dependency: build it. `import fastapi`
# rather than a marker file, so a half-finished earlier run self-heals.
if [[ ! -x "$VENV_PY" ]] || ! "$VENV_PY" -c 'import fastapi, uvicorn' >/dev/null 2>&1; then
    # uv first, matching install.sh: it is faster and can bring its own Python
    # when the system one is too old. Falling back to the stdlib venv otherwise.
    USED_UV=0
    if [[ ! -x "$VENV_PY" ]]; then
        printf 'Creating %s (first run) ...\n' "$VENV"
        if command -v uv >/dev/null 2>&1 \
           && { uv venv --python "3.${MIN_PY_MINOR}" "$VENV" >/dev/null 2>&1 \
                || uv venv "$VENV" >/dev/null 2>&1; }; then
            USED_UV=1
        elif usable_python3; then
            python3 -m venv "$VENV" || die \
                "python3 -m venv failed. Run ./install.sh, which can build an
       environment without root, or install the venv module yourself."
        else
            die "no usable Python 3.${MIN_PY_MINOR}+ with venv support.
       Run ./install.sh — it can provision one without root."
        fi
    elif command -v uv >/dev/null 2>&1 && [[ ! -x "$VENV/bin/pip" ]]; then
        # An existing venv with no pip in it is a uv-made one; keep using uv.
        USED_UV=1
    fi

    printf 'Installing dependencies ...\n'
    # A uv-created venv has no pip inside it, so the installer has to match the
    # environment it is filling.
    if [[ "$USED_UV" == "1" ]]; then
        if [[ -f "$SCRIPT_DIR/requirements.txt" ]]; then
            VIRTUAL_ENV="$VENV" uv pip install --quiet -r "$SCRIPT_DIR/requirements.txt" \
                || die "could not install from requirements.txt. Check your network and try again."
        else
            VIRTUAL_ENV="$VENV" uv pip install --quiet fastapi 'uvicorn[standard]' \
                || die "could not install fastapi/uvicorn. Check your network and try again."
        fi
    else
        "$VENV_PY" -m pip install --quiet --upgrade pip \
            || die "could not upgrade pip inside $VENV"
        if [[ -f "$SCRIPT_DIR/requirements.txt" ]]; then
            "$VENV_PY" -m pip install --quiet -r "$SCRIPT_DIR/requirements.txt" \
                || die "could not install from requirements.txt. Check your network and try again."
        else
            "$VENV_PY" -m pip install --quiet fastapi 'uvicorn[standard]' \
                || die "could not install fastapi/uvicorn. Check your network and try again."
        fi
    fi
fi

exec "$VENV_PY" app.py --port 5560 "$@"
