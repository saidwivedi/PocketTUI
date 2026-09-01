#!/bin/bash
# Install the PocketTUI backend on this machine.
#
#   curl -fsSL https://pockettui.com/install.sh | bash
#
# Fetches the source tarball, builds a venv, and (on Linux with systemd)
# offers to install a user service. The phone client is the hosted page at
# https://pockettui.com/app/ — it asks for this machine's address
# on first run, so nothing about your host is baked into it.
#
# Re-running it on a machine that already has PocketTUI updates that install in
# place, keeping the pairing code, the venv and the voice models:
#
#   curl -fsSL https://pockettui.com/install.sh | bash -s -- --update
#
# or, once installed, just `pockettui update`.
#
# Environment:
#   POCKETTUI_DIR      install directory   (default: ~/pockettui)
#   POCKETTUI_UPDATE   1 to update an existing install (same as --update)
#   POCKETTUI_FORCE    1 to overwrite an existing install
#   POCKETTUI_VERBOSE  1 for the full per-step detail (same as --verbose)
#   POCKETTUI_SERVICE_NAME  systemd unit / launchd label  (default: pockettui)
#   POCKETTUI_BASE_URL where install.sh, the tarball and version.txt live
#   POCKETTUI_BIN      directory for the `pockettui` command (default: ~/.local/bin)
#   PORT               port to serve on    (default: 5560)

set -eu

# Overridable so a test harness (or a private mirror) can point the whole script
# at another origin: the tarball, version.txt and the copy of install.sh the
# wrapper re-fetches all hang off this one value.
BASE_URL="${POCKETTUI_BASE_URL:-https://pockettui.com}"
TARBALL_URL="$BASE_URL/pockettui.tar.gz"
VERSION_URL="$BASE_URL/version.txt"
INSTALL_DIR="${POCKETTUI_DIR:-$HOME/pockettui}"
PORT="${PORT:-5560}"
# Overridable so a second install on another port can have its own unit rather
# than fighting the first one for the name.
SERVICE_NAME="${POCKETTUI_SERVICE_NAME:-pockettui}"
MIN_PY_MINOR=10

# Run from a clone (./install.sh) the sources are already here, so use them and
# leave the network alone; piped from curl there is nothing next to the script
# and the tarball is the only way to get them. $0 is the script itself in the
# first case and "bash" (or a pipe) in the second, so the test is whether the
# directory it resolves to actually holds the app.
SRC_DIR="$(cd "$(dirname "$0")" 2>/dev/null && pwd || true)"
LOCAL_CHECKOUT=0
if [[ -n "$SRC_DIR" ]] && [[ -f "$SRC_DIR/app.py" ]] && [[ -f "$SRC_DIR/build_mobile.py" ]]; then
    LOCAL_CHECKOUT=1
fi

# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
# A successful run is a handful of lines: one per step with a status on the
# right, then the pairing code and what to type on the phone. Everything that
# used to be printed as a tutorial is written into $INSTALL_DIR/README.md and
# pointed at with one line. Quiet applies to the SUCCESS path only — warnings,
# prompts and failures still say everything they said before, because a failed
# install has to stay diagnosable from the terminal alone.
VERBOSE="${POCKETTUI_VERBOSE:-0}"
# Update an install that is already there rather than refusing to touch it. As a
# flag it survives the documented pipe (`curl … | bash -s -- --update`); as an
# environment variable it is what the wrapper and any automation can set.
UPDATE="${POCKETTUI_UPDATE:-0}"
for arg in ${@+"$@"}; do
    case "$arg" in
        -v|--verbose) VERBOSE=1 ;;
        --update)     UPDATE=1 ;;
    esac
done

# Colour, only where it can do no harm: a pipe into a file or a pager, NO_COLOR
# (https://no-color.org), and TERM=dumb all turn it off. Disabled means empty
# strings rather than a second set of printf calls, so the layout of a coloured
# and an uncoloured run is identical minus the escapes. Note that
# `curl … | bash` pipes into *stdin*; stdout is still the terminal, so the
# documented install path does get colour.
C_RESET=""; C_STEP=""; C_OK=""; C_WARN=""; C_DIM=""; C_CODE=""; C_RULE=""
if [[ -t 1 ]] && [[ -z "${NO_COLOR:-}" ]] && [[ "${TERM:-}" != "dumb" ]]; then
    C_RESET=$'\033[0m'
    C_STEP=$'\033[36m'          # step markers: modest
    C_OK=$'\033[32m'
    C_WARN=$'\033[33m'
    C_DIM=$'\033[2m'            # secondary detail
    C_CODE=$'\033[1;35m'        # the pairing code: the loudest thing on screen
    C_RULE=$'\033[35m'
fi

say()  { printf '%s\n' "$*"; }
die()  { printf '%sERROR: %s%s\n' "$C_WARN" "$*" "$C_RESET" >&2; exit 1; }

# Detail that only a --verbose run wants. Anything a user must act on (a
# warning, a prompt, a failure) uses say/die and is printed either way.
vsay() { [[ "$VERBOSE" == "1" ]] && printf '%s\n' "$*" || true; }

# One step, one line. In quiet mode the heading is held back until its status is
# known so the two can share a line; --verbose prints the old multi-line form.
STEP_OPEN=""
step() {
    STEP_OPEN="$*"
    [[ "$VERBOSE" == "1" ]] && printf '\n%s==> %s%s\n' "$C_STEP" "$*" "$C_RESET"
    return 0
}

# A heading that has no status of its own and so no place on a quiet run: the
# install-directory and download steps, whose result is implied by the steps
# that follow. Printed in full by --verbose, silent otherwise.
step_quiet() {
    [[ "$VERBOSE" == "1" ]] && printf '\n%s==> %s%s\n' "$C_STEP" "$*" "$C_RESET"
    return 0
}

# Close the open step with a right-aligned status. Padding is computed on the
# uncoloured text, so the columns line up whether or not colour is on.
STEP_WIDTH=34
step_done() {
    local status="$1" colour="${2:-$C_OK}" pad=""
    if [[ "$VERBOSE" == "1" ]]; then
        # The verbose run has already printed the detail this status summarises,
        # so repeating the one-word version under it would only be noise.
        STEP_OPEN=""
        return 0
    fi
    [[ -n "$STEP_OPEN" ]] || return 0
    # printf cannot pad a string that already contains escapes, so pad first.
    local plain="==> $STEP_OPEN"
    local n=$(( STEP_WIDTH - ${#plain} ))
    [[ "$n" -lt 1 ]] && n=1
    while [[ "$n" -gt 0 ]]; do pad="$pad "; n=$((n - 1)); done
    printf '%s==>%s %s%s%s%s%s\n' \
        "$C_STEP" "$C_RESET" "$STEP_OPEN" "$pad" "$colour" "$status" "$C_RESET"
    STEP_OPEN=""
}

# ---------------------------------------------------------------------------
# Package manager (hints only)
# ---------------------------------------------------------------------------
# This script never runs a package manager and never uses sudo. What is missing
# gets provided in user space instead (see the rescue helpers below), so the
# detected manager is only ever quoted back in a message — an apt line on a
# Fedora box is worse than no line at all. Bash 3.2 (still /bin/bash on macOS)
# has no associative arrays, so this is a plain name plus two case statements.
PKG=""
for candidate in brew apt-get dnf yum pacman zypper apk; do
    if command -v "$candidate" >/dev/null 2>&1; then
        PKG="$candidate"
        break
    fi
done

# The command a user would type to install $1 themselves. Printed, never run.
pkg_install_cmd() {
    case "$PKG" in
        brew)    printf 'brew install %s' "$1" ;;
        apt-get) printf 'sudo apt-get install -y %s' "$1" ;;
        dnf)     printf 'sudo dnf install -y %s' "$1" ;;
        yum)     printf 'sudo yum install -y %s' "$1" ;;
        pacman)  printf 'sudo pacman -S --noconfirm %s' "$1" ;;
        zypper)  printf 'sudo zypper install -y %s' "$1" ;;
        apk)     printf 'sudo apk add %s' "$1" ;;
        *)       printf 'install %s with your package manager' "$1" ;;
    esac
}

# Package names diverge more than the commands do. Only the ones this script
# mentions are mapped; $1 is the generic name.
pkg_name() {
    case "$1:$PKG" in
        tmux:*)             printf 'tmux' ;;
        python3:brew)       printf 'python' ;;
        python3:pacman)     printf 'python' ;;
        python3:apk)        printf 'python3' ;;
        python3:*)          printf 'python3' ;;
        venv:apt-get)       printf 'python3-venv' ;;
        venv:dnf|venv:yum)  printf 'python3-libs' ;;
        venv:zypper)        printf 'python3-venv' ;;
        venv:*)             printf 'python3' ;;
        *)                  printf '%s' "$1" ;;
    esac
}

# ---------------------------------------------------------------------------
# User-space rescue
# ---------------------------------------------------------------------------
# Everything here installs under the user's own home and needs no password. The
# alternative — asking for sudo from a script that may be running piped from
# curl, with no terminal to type into — is worse on every axis.
# Also where the `pockettui` command is written. Overridable so a test run — or
# a machine that puts user binaries somewhere else — can be pointed elsewhere
# without writing into the real ~/.local/bin.
USER_BIN="${POCKETTUI_BIN:-$HOME/.local/bin}"

# conda-forge's name for this platform, used to build the micromamba URL.
mamba_platform() {
    local os arch
    os="$(uname -s)"
    arch="$(uname -m)"
    case "$os:$arch" in
        Linux:x86_64)           printf 'linux-64' ;;
        Linux:aarch64|Linux:arm64) printf 'linux-aarch64' ;;
        Darwin:arm64)           printf 'osx-arm64' ;;
        Darwin:x86_64)          printf 'osx-64' ;;
        *)                      return 1 ;;
    esac
}

# Fetch the official static micromamba into ~/.local/bin. One self-contained
# binary with no dependencies, which is exactly what makes it the way out of a
# machine that has neither a usable python3 nor tmux.
install_micromamba() {
    local plat url
    plat="$(mamba_platform)" || { say "  unsupported platform for micromamba ($(uname -s)/$(uname -m))"; return 1; }
    url="https://micro.mamba.pm/api/micromamba/$plat/latest"
    command -v curl >/dev/null 2>&1 || { say "  curl not found — cannot fetch micromamba"; return 1; }
    mkdir -p "$USER_BIN" || return 1
    say "  fetching micromamba ($plat) into $USER_BIN ..."
    # The endpoint serves a .tar.bz2 whose micromamba lives at bin/micromamba.
    # -O strips the leading path so it lands as the file we then chmod.
    if ! curl -fsSL "$url" | tar -xj -O "bin/micromamba" > "$USER_BIN/micromamba" 2>/dev/null; then
        rm -f "$USER_BIN/micromamba"
        say "  could not download micromamba from $url"
        return 1
    fi
    chmod +x "$USER_BIN/micromamba" || return 1
    [[ -s "$USER_BIN/micromamba" ]] || { rm -f "$USER_BIN/micromamba"; return 1; }
    PATH="$USER_BIN:$PATH"
    export PATH
    touched_outside
    note "installed micromamba into $USER_BIN (no system packages were touched)"
    return 0
}

# Fetch uv via its official standalone installer. Only used when tmux is already
# present and it is just the Python side that is unusable, since uv can provision
# its own interpreter but cannot provide tmux.
install_uv() {
    command -v curl >/dev/null 2>&1 || { say "  curl not found — cannot fetch uv"; return 1; }
    mkdir -p "$USER_BIN" || return 1
    say "  fetching uv into $USER_BIN ..."
    if ! curl -fsSL https://astral.sh/uv/install.sh \
         | env UV_INSTALL_DIR="$USER_BIN" INSTALLER_NO_MODIFY_PATH=1 sh >/dev/null 2>&1; then
        say "  could not run the uv installer"
        return 1
    fi
    [[ -x "$USER_BIN/uv" ]] || { say "  uv installer did not leave a binary in $USER_BIN"; return 1; }
    PATH="$USER_BIN:$PATH"
    export PATH
    touched_outside
    note "installed uv into $USER_BIN (no system packages were touched)"
    return 0
}

# Is there a python3 that is both new enough and able to build a venv? Answering
# this before choosing a path is what keeps the fallback chain readable.
usable_python3() {
    local ver major minor
    command -v python3 >/dev/null 2>&1 || return 1
    ver="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)" || return 1
    major="${ver%%.*}"
    minor="${ver##*.}"
    [[ "$major" -eq 3 ]] && [[ "$minor" -ge "$MIN_PY_MINOR" ]] || return 1
    python3 -c 'import venv, ensurepip' >/dev/null 2>&1
}

# What this run actually changed, printed back at the end.
DID=()
note() { DID+=("$*"); }

# DID also collects work inside $INSTALL_DIR and steps that were skipped, so it
# cannot answer "did anything outside the install directory change?". Only the
# handful of sites that truly touch the wider system bump this.
OUTSIDE=0
touched_outside() { OUTSIDE=$((OUTSIDE + 1)); }

# Piped into bash (curl | bash), stdin is the script itself, so prompts have to
# come from the terminal. Without one there is nobody to ask: everything that
# would touch the system outside the install dir is skipped and printed instead.
# A failed `exec` redirection prints its own error regardless of a trailing
# 2>/dev/null, so probe in a subshell first and only open fd 3 if that works.
INTERACTIVE=0
if [[ -e /dev/tty ]] && (exec 3<>/dev/tty) 2>/dev/null; then
    exec 3<>/dev/tty
    INTERACTIVE=1
fi

# Ask a yes/no question; anything but an explicit yes is No.
confirm() {
    local prompt="$1" reply=""
    [[ "$INTERACTIVE" == "1" ]] || return 1
    printf '%s [y/N] ' "$prompt" >&3
    IFS= read -r reply <&3 || return 1
    [[ "$reply" == "y" || "$reply" == "Y" || "$reply" == "yes" || "$reply" == "YES" ]]
}

# Ask what to do about an existing install: update, re-install (rotates the
# pairing code), or leave it alone. $1 is the label for option 1, since
# whether it names a target version (or is honestly a re-apply when the
# remote is the same version) depends on what the caller already worked out.
# Default on empty input is 1 (Update) — that is what almost everyone here
# wants, and it keeps `[Enter]` doing the same thing y/N update prompts used
# to. Only INTERACTIVE (fd 3 open) calls this.
existing_install_ask() {
    local update_label="$1" reply
    {
        printf '    %s1) %s%s\n' "$C_STEP" "$update_label" "$C_RESET"
        printf '    %s2) Re-install%s %s— fresh copy, generates a NEW pairing code%s\n' "$C_STEP" "$C_RESET" "$C_DIM" "$C_RESET"
        printf '    %s3) Leave it alone%s\n' "$C_STEP" "$C_RESET"
        printf '  choice %s[1-3]%s: ' "$C_DIM" "$C_RESET"
    } >&3
    IFS= read -r reply <&3 || reply=1
    [[ -z "$reply" ]] && reply=1
    printf '%s' "$reply"
}

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------
step "Checking prerequisites"

# Nothing here is fatal any more. What is missing is noted and then provided in
# user space further down — a missing tmux or an unusable python3 is a thing to
# fix, not a reason to send the user away to their package manager. Quiet runs
# collapse to "ok" or to the one thing that is missing; --verbose keeps the
# per-tool lines.
MISSING=""
if command -v python3 >/dev/null 2>&1; then
    PY_VER="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo '?')"
    if usable_python3; then
        vsay "  python3 $PY_VER"
    else
        vsay "  python3 $PY_VER — not usable for a venv (needs 3.${MIN_PY_MINOR}+ with venv/ensurepip)"
        MISSING="python"
    fi
else
    vsay "  python3 not found"
    MISSING="python"
fi

HAVE_TMUX=0
if command -v tmux >/dev/null 2>&1; then
    HAVE_TMUX=1
    vsay "  tmux $(tmux -V | awk '{print $2}')"
else
    vsay "  tmux not found — it will be installed into this install's own"
    vsay "    environment (see below); no system packages are touched."
    MISSING="${MISSING:+$MISSING, }tmux"
fi

# The backend only needs to be reachable from the phone; Tailscale is the
# easiest way to do that, but it is not this script's job to require it.
HAVE_TAILSCALE=0
if command -v tailscale >/dev/null 2>&1; then
    HAVE_TAILSCALE=1
    vsay "  tailscale $(tailscale version 2>/dev/null | head -1)"
else
    vsay "  tailscale not found — you will need some other way to reach this"
    vsay "    machine from your phone (see the notes at the end)."
    vsay "    Whatever route you use, the pairing code set up below keeps the"
    vsay "    connection authenticated — a LAN address is not an open shell."
fi

if [[ -n "$MISSING" ]]; then
    step_done "$MISSING missing — will provide" "$C_WARN"
else
    step_done "ok"
fi

# Only the download path needs curl; a local checkout already has the sources.
if [[ "$LOCAL_CHECKOUT" != "1" ]]; then
    command -v curl >/dev/null 2>&1 || die "curl not found."
fi

# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------
# The stamp deploy_cloudflare.sh puts in the tarball, read back from disk. An
# install made before versioning existed, and a checkout, have no VERSION file:
# both answer "unknown" rather than failing, and every comparison below treats
# an unknown as "cannot tell, so offer the update".
installed_version() {
    local v=""
    if [[ -r "$INSTALL_DIR/VERSION" ]]; then
        v="$(tr -d '[:space:]' < "$INSTALL_DIR/VERSION" 2>/dev/null || true)"
    fi
    printf '%s' "${v:-unknown}"
}

# What the site is serving, for the "update to what?" half of the question. Only
# ever advisory: no network, no version.txt (an older deploy), or a proxy
# serving an error page all read as "unknown" and change nothing but the
# wording. Short timeouts because this runs before anything useful has happened
# and a hung fetch would look like a hung installer.
remote_version() {
    local v=""
    if command -v curl >/dev/null 2>&1; then
        v="$(curl -fsSL --max-time 5 "$VERSION_URL" 2>/dev/null \
             | head -1 | tr -d '[:space:]' || true)"
    fi
    # A stamp is digits-dot-digits-dot-digits; anything else came from an error page.
    case "$v" in
        [0-9]*.[0-9]*.[0-9]*) printf '%s' "$v" ;;
        *)                    printf 'unknown' ;;
    esac
}

# ---------------------------------------------------------------------------
# Install directory
# ---------------------------------------------------------------------------
FRESH_DIR=1
# Set on the paths below that are conceptually a re-install rather than an
# update, so the pairing token is minted fresh even where UPDATE=1 is also
# set to reuse the update file-replacement flow.
ROTATE_TOKEN=0
# ./install.sh inside a clone, with no POCKETTUI_DIR set to send it elsewhere:
# the install dir *is* the checkout. There is nothing to overwrite and nothing
# to ask about — just build the venv where the sources already are.
if [[ "$LOCAL_CHECKOUT" == "1" ]] && [[ -e "$INSTALL_DIR" ]] \
   && [[ "$SRC_DIR" -ef "$INSTALL_DIR" ]]; then
    FRESH_DIR=0
    step_quiet "Installing into this checkout at $INSTALL_DIR"
    vsay "  The sources are already here; only .venv and start.sh are added."
    # ROTATE_TOKEN is left at 0 here: this path is re-run often during
    # development, and rotating the token on every re-run would unpair an
    # already-paired phone constantly.
elif [[ -e "$INSTALL_DIR" ]]; then
    FRESH_DIR=0
    OLD_VERSION="$(installed_version)"
    # Not asked for outright, but there is a terminal to ask on: an existing
    # install is much more often a user who wants the new version than one who
    # meant to install a second time, so offer that instead of refusing.
    if [[ "$UPDATE" != "1" ]] && [[ "${POCKETTUI_FORCE:-}" != "1" ]] \
       && [[ "$INTERACTIVE" == "1" ]]; then
        NEW_VERSION="$(remote_version)"
        say ""
        say "  PocketTUI is already installed at $INSTALL_DIR (version $OLD_VERSION)."
        UPDATE_LABEL="Update — keeps your pairing code"
        if [[ "$NEW_VERSION" != "unknown" ]] && [[ "$NEW_VERSION" == "$OLD_VERSION" ]]; then
            UPDATE_LABEL="Re-apply the current version — keeps your pairing code"
        elif [[ "$NEW_VERSION" != "unknown" ]]; then
            say "  Version $NEW_VERSION is available."
            UPDATE_LABEL="Update — keeps your pairing code, -> $NEW_VERSION"
        fi
        EXISTING_CHOICE="$(existing_install_ask "$UPDATE_LABEL")"
        case "$EXISTING_CHOICE" in
            2) UPDATE=1; ROTATE_TOKEN=1 ;;
            1) UPDATE=1 ;;
            *) UPDATE=0 ;;
        esac
        if [[ "$UPDATE" != "1" ]]; then
            say ""
            say "  Left alone — nothing was changed."
            say "  To install a second copy somewhere else:"
            say "      curl -fsSL $BASE_URL/install.sh | POCKETTUI_DIR=~/somewhere bash"
            say ""
            exit 0
        fi
        say ""
    fi
    if [[ "$UPDATE" != "1" ]] && [[ "${POCKETTUI_FORCE:-}" != "1" ]]; then
        die "$INSTALL_DIR already exists (version $OLD_VERSION).
  To update it in place (pairing code, voice models and venv are kept):
      curl -fsSL $BASE_URL/install.sh | bash -s -- --update
  Or, if PocketTUI is already on your PATH:
      pockettui update
  Or install somewhere else:
      curl -fsSL $BASE_URL/install.sh | POCKETTUI_DIR=~/somewhere bash"
    fi
    if [[ "$UPDATE" == "1" ]]; then
        step_quiet "Updating the install at $INSTALL_DIR (from version $OLD_VERSION)"
        vsay "  The new copy replaces the program files:"
        for f in app.py resolver.py mobile_app.html sw.js pockettui.service \
                 install.sh setup_voice.sh requirements.txt qrcodegen.py \
                 icon-192.png icon-512.png vendor; do
            [[ -e "$INSTALL_DIR/$f" ]] && vsay "    $f"
        done
        if [[ "$ROTATE_TOKEN" == "1" ]]; then
            vsay "  Kept as they are: voice/, .voice_learned.json, .venv."
            vsay "  A new pairing code is generated, since this is a re-install."
        else
            vsay "  Kept as they are: .token, voice/, .voice_learned.json, .venv."
        fi
        # The tarball ships the complete vendor set, so anything left in there
        # afterwards is a file upstream deleted — it would otherwise be served
        # for the life of the install. Scoped to vendor/ alone: it is the only
        # directory whose contents are entirely ours.
        if [[ -d "$INSTALL_DIR/vendor" ]]; then
            rm -rf "${INSTALL_DIR:?}/vendor"
            vsay "  cleared vendor/ so removed files do not linger"
        fi
    else
        step_quiet "Replacing existing install at $INSTALL_DIR (POCKETTUI_FORCE=1)"
        ROTATE_TOKEN=1
        vsay "  These are overwritten by the new copy:"
        for f in app.py mobile_app.html sw.js pockettui.service install.sh \
                 qrcodegen.py icon-192.png icon-512.png vendor; do
            [[ -e "$INSTALL_DIR/$f" ]] && vsay "    $f"
        done
        # The venv is rebuilt on top of whatever is there; nothing else is removed.
        [[ -e "$INSTALL_DIR/.venv" ]] && vsay "    .venv (dependencies reinstalled)"
        vsay "    .token (a new pairing code is generated)"
        vsay "  Anything else already in that directory is left alone."
        note "replaced files in $INSTALL_DIR"
    fi
fi

# Only ever set on the branch above, which is the only place an install can
# already exist. A fresh install is not an update however it was invoked, so the
# flag is cleared rather than left to drive the update-only wording below.
if [[ "$FRESH_DIR" == "1" ]]; then
    UPDATE=0
fi
OLD_VERSION="${OLD_VERSION:-unknown}"

if [[ "$LOCAL_CHECKOUT" == "1" ]]; then
    step_quiet "Copying source from this checkout"
    vsay "  $SRC_DIR"
    mkdir -p "$INSTALL_DIR"
    [[ "$FRESH_DIR" == "1" ]] && note "created $INSTALL_DIR"
    # Only the files the backend actually runs, mirroring what the tarball ships.
    # Absent ones are skipped rather than fatal. The front end is not in this
    # list: a checkout keeps it split under src/ and its icons in assets/, so
    # mobile_app.html, sw.js and the icons are built into $INSTALL_DIR further
    # down, once there is a Python to run build_mobile.py with. app.py was
    # already checked.
    for f in app.py resolver.py requirements.txt vendor qrcodegen.py \
             pockettui.service install.sh run.sh setup_voice.sh; do
        [[ -e "$SRC_DIR/$f" ]] || continue
        # Installing from inside the install dir would be cp-onto-itself.
        [[ "$SRC_DIR/$f" -ef "$INSTALL_DIR/$f" ]] && continue
        cp -R "$SRC_DIR/$f" "$INSTALL_DIR/" || die "could not copy $f"
    done
    vsay "  -> $INSTALL_DIR"
else
    step_quiet "Downloading source"
    mkdir -p "$INSTALL_DIR"
    [[ "$FRESH_DIR" == "1" ]] && note "created $INSTALL_DIR"
    # `mktemp -t X.tar.gz` means different things to GNU and BSD mktemp (BSD
    # appends its own suffix to the template, GNU treats the whole thing as one),
    # so name the file in full ourselves and let mktemp fill only the X's.
    TMP_TGZ="$(mktemp "${TMPDIR:-/tmp}/pockettui.XXXXXX")" || die "could not create a temp file"
    trap 'rm -f "$TMP_TGZ"' EXIT
    curl -fsSL "$TARBALL_URL" -o "$TMP_TGZ" || die "could not download $TARBALL_URL"
    tar -xzf "$TMP_TGZ" -C "$INSTALL_DIR" || die "could not extract the tarball"
    vsay "  -> $INSTALL_DIR"
fi

# ---------------------------------------------------------------------------
# Python environment
# ---------------------------------------------------------------------------
step "Python environment"

# Where the interpreter ends up, and — when tmux comes from a conda env rather
# than the system — the bin directory the runtime has to have on PATH. start.sh,
# the systemd unit and the launchd agent all need both.
VENV_PY="$INSTALL_DIR/.venv/bin/python"
ENV_KIND="venv"
# The one-word provenance shown on the quiet run's status column: which of the
# three routes actually produced the environment. ENV_KIND stays as it was —
# it selects the installer below and is quoted in the changelog.
ENV_LABEL="venv"
MAMBA_PREFIX="$INSTALL_DIR/.micromamba"
ENV_BIN=""          # non-empty only when the env must be on PATH (its tmux)

# A conda env holding both tmux and python, created with the given micromamba.
# The one path that can rescue a machine with no system tmux at all: conda-forge
# ships tmux as a relocatable package, so it needs no root and no compiler.
create_mamba_env() {
    local want_tmux="$1" specs
    specs="python>=3.${MIN_PY_MINOR}"
    if [[ "$want_tmux" == "1" ]]; then
        say "  no system tmux — providing tmux from conda-forge inside this install"
    fi
    # -p (an explicit prefix) rather than -n (a name): where a *named* env lands
    # depends on the root prefix and on how micromamba was set up, so the path to
    # its interpreter is not something this script can predict. A prefix inside
    # the install dir is deterministic and self-contained — removing the install
    # directory removes the environment with it.
    #
    # -c conda-forge explicitly: a micromamba installed without a ~/.condarc has
    # no default channel, and the solve then fails with "python >=3.9 does not
    # exist" on exactly the machines that need this path.
    if [[ "$want_tmux" == "1" ]]; then
        micromamba create -y -p "$MAMBA_PREFIX" -c conda-forge "$specs" tmux >/dev/null 2>&1
    else
        micromamba create -y -p "$MAMBA_PREFIX" -c conda-forge "$specs" >/dev/null 2>&1
    fi
}

# Adopt the micromamba env as the environment for this install.
adopt_mamba_env() {
    [[ -x "$MAMBA_PREFIX/bin/python" ]] \
        || die "could not locate python in the micromamba env at $MAMBA_PREFIX."
    VENV_PY="$MAMBA_PREFIX/bin/python"
    ENV_KIND="micromamba env at $MAMBA_PREFIX"
    # Only pin PATH when this env is also where tmux comes from. app.py runs
    # tmux by bare name, so the env's bin dir has to precede the system one.
    if [[ -x "$MAMBA_PREFIX/bin/tmux" ]]; then
        ENV_BIN="$MAMBA_PREFIX/bin"
        vsay "  tmux $("$MAMBA_PREFIX/bin/tmux" -V 2>/dev/null | awk '{print $2}') from conda-forge"
    fi
    note "created a micromamba env at $MAMBA_PREFIX"
    ENV_LABEL="micromamba"
    vsay "  $ENV_KIND"
}

# Build the venv with uv, which brings its own interpreter when the system has
# none good enough. Returns non-zero if uv could not do it.
create_uv_venv() {
    # An existing venv is a success, not a failure: `uv venv` refuses to write
    # into one without --clear, and treating that refusal as "uv cannot do it"
    # sent every re-run down the fallback chain to micromamba — so a second run
    # rebuilt the environment a different way, rewrote the unit, and then warned
    # that the unit it had just written differed from the one before it.
    # Re-using it is also what the pip step below expects: dependencies are
    # installed into whatever is there, so a fresh venv is only needed once.
    if [[ -x "$INSTALL_DIR/.venv/bin/python" ]]; then
        return 0
    fi
    uv venv --python "3.${MIN_PY_MINOR}" "$INSTALL_DIR/.venv" >/dev/null 2>&1 \
        || uv venv "$INSTALL_DIR/.venv" >/dev/null 2>&1
}

# No system tmux is the one problem a venv cannot solve, so it decides the whole
# strategy: only a conda env can supply tmux without root. Everything else is
# the ordinary uv -> venv -> micromamba preference.
if [[ "$HAVE_TMUX" != "1" ]]; then
    # Said out loud because it overrides the uv preference: with no system tmux,
    # a venv cannot help — only a conda env supplies tmux without root.
    vsay "  no system tmux — using micromamba, which can provide both tmux and python"
    if ! command -v micromamba >/dev/null 2>&1; then
        install_micromamba \
            || die "tmux is missing and micromamba could not be installed. Install tmux yourself and re-run:  $(pkg_install_cmd tmux)"
    fi
    create_mamba_env 1 \
        || die "could not create an environment with tmux. Install tmux yourself and re-run:  $(pkg_install_cmd tmux)"
    adopt_mamba_env
elif command -v uv >/dev/null 2>&1 && create_uv_venv; then
    # uv first: it is the fastest, and it can provision its own Python when the
    # system one is too old, so it subsumes the venv path rather than competing.
    ENV_KIND="venv (uv)"
    ENV_LABEL="uv"
    vsay "  using uv (found on PATH)"
    vsay "  $INSTALL_DIR/.venv (uv)"
elif usable_python3 && python3 -m venv "$INSTALL_DIR/.venv" 2>/dev/null; then
    ENV_LABEL="venv"
    vsay "  no uv on PATH — using python3 -m venv"
    vsay "  $INSTALL_DIR/.venv"
elif command -v micromamba >/dev/null 2>&1 && create_mamba_env 0; then
    # Debian splits venv out of the stdlib, and some distro pythons ship without
    # ensurepip at all. A micromamba env brings its own interpreter.
    vsay "  no usable python3 venv — falling back to a micromamba env"
    adopt_mamba_env
elif install_uv && create_uv_venv; then
    # tmux is present, so only the Python side is broken and uv is enough to fix
    # it — it downloads a managed interpreter of its own.
    ENV_KIND="venv (uv)"
    ENV_LABEL="uv (own python)"
    vsay "  $INSTALL_DIR/.venv (uv, with its own Python)"
else
    die "could not build a Python environment. Install Python 3.${MIN_PY_MINOR}+ and re-run:  $(pkg_install_cmd "$(pkg_name venv)")"
fi
step_done "$ENV_LABEL"

# ---------------------------------------------------------------------------
# The front end, from a checkout
# ---------------------------------------------------------------------------
# The tarball ships mobile_app.html, sw.js and the icons already built. A
# checkout does not: they are assembled from src/mobile/ and assets/. This runs
# here rather than with the rest of the copying because it needs an interpreter,
# and the one just built is the only one this script is sure of. build_mobile.py
# is stdlib-only, so nothing is installed yet and that is fine.
if [[ "$LOCAL_CHECKOUT" == "1" ]]; then
    step "Front end"
    if ! (cd "$SRC_DIR" && "$VENV_PY" build_mobile.py --assemble-only \
              --emit-runtime "$INSTALL_DIR" >/dev/null); then
        die "could not build the front end from $SRC_DIR"
    fi
    vsay "  assembled mobile_app.html, sw.js and the icons into $INSTALL_DIR"
    step_done "ok"
fi

step "Dependencies"
# Inside an environment we just made, so nothing here is a system change and
# nothing needs asking.
#
# A uv-created venv deliberately has no pip in it, so the installer has to match
# the environment: uv pip for those, python -m pip for the rest (-m pip rather
# than the bin/pip script, since the micromamba path has no .venv/bin/pip).
REQ_FILE="$INSTALL_DIR/requirements.txt"
if [[ "$ENV_KIND" == "venv (uv)" ]]; then
    if [[ -f "$REQ_FILE" ]]; then
        VIRTUAL_ENV="$INSTALL_DIR/.venv" uv pip install --quiet -r "$REQ_FILE" \
            || die "could not install from requirements.txt. Check your network and try again."
    else
        VIRTUAL_ENV="$INSTALL_DIR/.venv" uv pip install --quiet fastapi 'uvicorn[standard]' \
            || die "could not install fastapi/uvicorn. Check your network and try again."
    fi
else
    if ! "$VENV_PY" -m pip install --quiet --upgrade pip; then
        die "could not upgrade pip inside the environment"
    fi
    # requirements.txt ships in the tarball and the checkout; the inline list
    # stays as the fallback for a tarball built before it existed.
    if [[ -f "$REQ_FILE" ]]; then
        if ! "$VENV_PY" -m pip install --quiet -r "$REQ_FILE"; then
            die "could not install from requirements.txt. Check your network and try again."
        fi
    elif ! "$VENV_PY" -m pip install --quiet fastapi 'uvicorn[standard]'; then
        die "could not install fastapi/uvicorn. Check your network and try again."
    fi
fi
vsay "  fastapi + uvicorn installed"
step_done "ok"

# ---------------------------------------------------------------------------
# Run script
# ---------------------------------------------------------------------------
# When tmux comes from the install's own conda env, every launcher has to put
# that bin dir first: app.py runs tmux by bare name, so PATH is what decides
# whether it finds one at all.
ENV_PATH_LINE=""
[[ -n "$ENV_BIN" ]] && ENV_PATH_LINE="export PATH=\"$ENV_BIN:\$PATH\""

cat > "$INSTALL_DIR/start.sh" <<EOF
#!/bin/bash
# Run the PocketTUI backend (generated by install.sh).
cd "\$(dirname "\$0")"
$ENV_PATH_LINE
exec "$VENV_PY" app.py --port $PORT "\$@"
EOF
chmod +x "$INSTALL_DIR/start.sh"

# ---------------------------------------------------------------------------
# The `pockettui` command
# ---------------------------------------------------------------------------
# Regenerated on every run, exactly like start.sh, so it always names the
# install it belongs to. Two things a user needs a command for after the
# install: updating, and knowing which version is on this machine.
#
# `update` re-fetches install.sh rather than running the copy in the install
# dir. The copy is the one that shipped with the *installed* version, so an
# updater bug would be permanent — a self-update that cannot be fixed by
# updating. The local copy stays as the offline fallback.
#
# Built in two halves, and the split is deliberate: the three values that vary
# per install are interpolated by the first heredoc, and the body — which is
# full of $ and backslashes the wrapper has to evaluate for itself — comes from
# a quoted one, where nothing is substituted and nothing has to be escaped.
# Escaping a whole script by hand is how a generator writes a file that is one
# missed backslash away from being silently wrong.
WRAPPER_PATH="$USER_BIN/pockettui"
WRAPPER_WRITTEN=0
WRAPPER_CONTENT="$(cat <<EOF
#!/bin/bash
# The PocketTUI command (generated by install.sh).
set -eu

INSTALL_DIR="$INSTALL_DIR"
BASE_URL="$BASE_URL"
SERVICE_NAME="$SERVICE_NAME"
WRAPPER_BIN="$USER_BIN"
EOF
cat <<'EOF'

local_version() {
    if [[ -r "$INSTALL_DIR/VERSION" ]]; then
        tr -d '[:space:]' < "$INSTALL_DIR/VERSION"
    else
        printf 'unknown'
    fi
}

# Advisory only, exactly as in install.sh: no network, no version.txt, or a
# captive portal serving HTML all read as "unknown" rather than as a version.
remote_version() {
    local v=""
    if command -v curl >/dev/null 2>&1; then
        v="$(curl -fsSL --max-time 5 "$BASE_URL/version.txt" 2>/dev/null | head -1 | tr -d '[:space:]' || true)"
    fi
    case "$v" in
        ([0-9]*.[0-9]*.[0-9]*) printf '%s' "$v" ;;
        (*)                    printf 'unknown' ;;
    esac
}

# Which install.sh to hand the job to, printed for the caller. The fresh copy
# from the site is preferred over the one in the install dir: that one shipped
# with the version being replaced, so an updater bug in it would be permanent —
# a self-update that cannot be fixed by updating. The installed copy is the
# fallback for a machine that cannot reach the site.
FRESH_INSTALLER=""
cleanup() { [[ -n "$FRESH_INSTALLER" ]] && rm -f "$FRESH_INSTALLER"; return 0; }
trap cleanup EXIT

# Everything this install was configured with, handed back to the installer so
# an update lands on the same directory, the same origin, the same unit name and
# the same bin dir it came from — not on whatever the defaults would pick.
INSTALLER_ENV=(POCKETTUI_DIR="$INSTALL_DIR" POCKETTUI_BASE_URL="$BASE_URL"
               POCKETTUI_SERVICE_NAME="$SERVICE_NAME" POCKETTUI_BIN="$WRAPPER_BIN")

run_installer() {
    FRESH_INSTALLER="${TMPDIR:-/tmp}/pockettui-install.$$.sh"
    if command -v curl >/dev/null 2>&1 && curl -fsSL --max-time 30 "$BASE_URL/install.sh" -o "$FRESH_INSTALLER" 2>/dev/null; then
        env "${INSTALLER_ENV[@]}" bash "$FRESH_INSTALLER" --update ${@+"$@"}
    elif [[ -f "$INSTALL_DIR/install.sh" ]]; then
        echo "Could not fetch $BASE_URL/install.sh — using the copy in $INSTALL_DIR." >&2
        env "${INSTALLER_ENV[@]}" bash "$INSTALL_DIR/install.sh" --update ${@+"$@"}
    else
        echo "Could not fetch $BASE_URL/install.sh, and there is no copy at" >&2
        echo "$INSTALL_DIR/install.sh. Check your network and try again." >&2
        return 1
    fi
}

case "${1:-}" in
    (update)
        shift
        run_installer ${@+"$@"}
        ;;
    (version|--version|-V)
        have="$(local_version)"
        there="$(remote_version)"
        echo "installed  $have"
        if [[ "$there" == "unknown" ]]; then
            echo "latest     unknown (could not reach $BASE_URL)"
        elif [[ "$there" == "$have" ]]; then
            echo "latest     $there  (up to date)"
        else
            echo "latest     $there"
            echo
            echo "An update is available. Install it with:  pockettui update"
        fi
        ;;
    (*)
        echo "usage: pockettui update | version"
        echo
        echo "  update   fetch and install the current version, in place"
        echo "  version  what is installed here, and what is current"
        ;;
esac
EOF
)"

# A re-run that would write the same bytes has changed nothing outside the
# install dir, and must not claim it did — same reasoning as the unit file.
WRAPPER_SAME=0
[[ -e "$WRAPPER_PATH" ]] && [[ "$(cat "$WRAPPER_PATH" 2>/dev/null || true)" == "$WRAPPER_CONTENT" ]] \
    && WRAPPER_SAME=1

# Never fatal: the command is a convenience and the curl one-liner does the same
# job, so an unwritable ~/.local/bin costs a line in the changelog, not the run.
if mkdir -p "$USER_BIN" 2>/dev/null \
   && printf '%s\n' "$WRAPPER_CONTENT" > "$WRAPPER_PATH" 2>/dev/null; then
    chmod +x "$WRAPPER_PATH" 2>/dev/null || true
    WRAPPER_WRITTEN=1
    vsay "  wrote $WRAPPER_PATH"
    if [[ "$WRAPPER_SAME" != "1" ]]; then
        touched_outside
        note "wrote the 'pockettui' command to $WRAPPER_PATH"
    fi
else
    vsay "  could not write $WRAPPER_PATH — the 'pockettui' command was skipped"
    note "could not write $WRAPPER_PATH (no 'pockettui' command)"
fi

# Worth a line only when the command exists but nothing would find it. Same
# question the micromamba/uv rescue paths raise about this directory.
WRAPPER_OFF_PATH=0
if [[ "$WRAPPER_WRITTEN" == "1" ]]; then
    case ":$PATH:" in
        *":$USER_BIN:"*) ;;
        *) WRAPPER_OFF_PATH=1 ;;
    esac
fi

# ---------------------------------------------------------------------------
# Pairing token
# ---------------------------------------------------------------------------
# app.py refuses to start without a token at $INSTALL_DIR/.token, and owns the
# only implementation of its format (10 base32 chars, XXXXX-XXXXX display) via
# --rotate-token. Importing it here (rather than calling --rotate-token blind)
# lets an existing valid token be read back and kept as-is: re-running the
# installer must not rotate it, or every already-paired phone breaks silently.
# TOKEN_KEPT drives the wording of the summary line further down; the
# keep-or-mint decision below is made the same way, off the same read_token().
# A re-install (POCKETTUI_FORCE=1, or answering yes to "Re-install it
# anyway?") sets ROTATE_TOKEN=1 to mint a new one instead: dropping the old
# .token here makes read_token() below come back empty, so the existing
# generate+write path naturally mints a fresh token without duplicating its
# logic.
[[ "$ROTATE_TOKEN" == "1" ]] && rm -f "$INSTALL_DIR/.token"
TOKEN_KEPT=0
"$VENV_PY" -c "
import sys
sys.path.insert(0, '$INSTALL_DIR')
import app
sys.exit(0 if app.read_token() else 1)
" && TOKEN_KEPT=1

TOKEN_DISPLAY="$("$VENV_PY" - "$INSTALL_DIR" <<'PYEOF'
import sys
sys.path.insert(0, sys.argv[1])
import app

token = app.read_token()
if not token:
    token = app.generate_token()
    app.write_token(token)
print(app.format_token(token))
PYEOF
)" || die "Could not set up a pairing token in $INSTALL_DIR/.token."
[[ -n "$TOKEN_DISPLAY" ]] || die "Could not set up a pairing token in $INSTALL_DIR/.token."

# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------
UNIT_DIR="$HOME/.config/systemd/user"
UNIT_PATH="$UNIT_DIR/$SERVICE_NAME.service"
SERVICE_INSTALLED=0
# The nohup fallback: running now, but nothing brings it back after a reboot.
# Tracked apart from SERVICE_INSTALLED so the summary can say which it is.
BACKGROUND_STARTED=0
HAVE_SYSTEMD=0
if [[ "$(uname -s)" == "Linux" ]] && command -v systemctl >/dev/null 2>&1 \
   && systemctl --user show-environment >/dev/null 2>&1; then
    HAVE_SYSTEMD=1
fi

# macOS has no systemd; the same job — start at login, restart if it dies — is a
# launchd user agent (a LaunchAgent, per-user, no root).
AGENT_LABEL="com.${SERVICE_NAME}.server"
AGENT_DIR="$HOME/Library/LaunchAgents"
AGENT_PATH="$AGENT_DIR/$AGENT_LABEL.plist"
HAVE_LAUNCHD=0
if [[ "$(uname -s)" == "Darwin" ]] && command -v launchctl >/dev/null 2>&1; then
    HAVE_LAUNCHD=1
fi

# A service starts with almost no environment, so the launcher has to name the
# directory tmux actually lives in. app.py runs tmux by bare name, and the PATH
# systemd and launchd hand a unit covers /usr/bin but not /opt/homebrew/bin, a
# Nix profile, ~/.local/bin, or this install's own conda env. Preflight looked
# for tmux on the *interactive* PATH, so whatever it found there has to be
# carried across explicitly — otherwise the install looks successful, the
# service starts, and every tmux call inside it fails.
#
# The install's own env wins when it is the one that supplied tmux; otherwise it
# is the tmux preflight validated, resolved once here rather than trusted to be
# on some other PATH later.
TMUX_BIN_DIR="$ENV_BIN"
if [[ -z "$TMUX_BIN_DIR" ]]; then
    TMUX_EXE="$(command -v tmux 2>/dev/null || true)"
    [[ -n "$TMUX_EXE" ]] && TMUX_BIN_DIR="$(dirname "$TMUX_EXE")"
fi

# The PATH every generated service gets. Prepended, not appended: where two tmux
# binaries exist, the one this install validated is the one that has to win.
SERVICE_PATH="/usr/local/bin:/usr/bin:/bin"
if [[ -n "$TMUX_BIN_DIR" ]]; then
    case ":$SERVICE_PATH:" in
        (*":$TMUX_BIN_DIR:"*) ;;
        (*) SERVICE_PATH="$TMUX_BIN_DIR:$SERVICE_PATH" ;;
    esac
fi

# The unit we would install. Built once, up here, so that the "is the file on
# disk already exactly this?" check and the write that follows can never drift
# apart the way two copies of a heredoc would.
UNIT_CONTENT="$(cat <<EOF
[Unit]
Description=PocketTUI tmux terminal backend (port $PORT)
After=network.target
StartLimitIntervalSec=0

[Service]
Type=simple
Restart=always
RestartSec=10
WorkingDirectory=$INSTALL_DIR
Environment="PATH=$SERVICE_PATH"
ExecStart=$VENV_PY $INSTALL_DIR/app.py --port $PORT

[Install]
WantedBy=default.target
EOF
)"

UNIT_BACKUP=""

# Whether the backend is actually answering, which is the only claim worth
# making at the end. `systemctl is-active` says the process was spawned, not
# that uvicorn bound the port — a unit that crash-loops on an occupied port is
# "active" for the first second of every restart. GET / is unauthenticated
# (the token guards the API, not the page), so a 2xx here means the server is
# up and reachable. Poll rather than sleep-once: a cold start is usually well
# under a second but a loaded machine can take several.
SERVER_UP=0
# POSIX sleep only promises integer seconds; GNU and BSD both take fractions.
# Probe once rather than assume, so a minimal /bin/sleep polls at 1s instead of
# erroring out of the loop on every iteration.
SLEEP_TICK=1
sleep 0.2 >/dev/null 2>&1 && SLEEP_TICK=0.5
wait_for_server() {
    local tries="${1:-20}"
    command -v curl >/dev/null 2>&1 || return 1
    while [[ "$tries" -gt 0 ]]; do
        if curl -sf -o /dev/null --max-time 2 "http://127.0.0.1:$PORT/" 2>/dev/null; then
            SERVER_UP=1
            return 0
        fi
        sleep "$SLEEP_TICK"
        tries=$((tries - 1))
    done
    return 1
}

# daemon-reload, then enable + start, then verify. Shared by the "we wrote the
# unit" and the "the user kept theirs" paths, which differ only in wording.
start_service() {
    local what="$1"
    touched_outside          # enabling a user unit is systemd state, not a file
    systemctl --user daemon-reload
    systemctl --user enable --now "$SERVICE_NAME" >/dev/null 2>&1 || true
    # A re-run against an already-running unit has to pick up the new code and
    # the (possibly rewritten) unit file; enable --now alone would leave the old
    # process in place.
    systemctl --user restart "$SERVICE_NAME" >/dev/null 2>&1 || true
    if wait_for_server; then
        SERVICE_INSTALLED=1
        vsay "  $SERVICE_NAME running on port $PORT"
        note "$what and started $SERVICE_NAME"
        step_done "running"
    else
        SERVICE_INSTALLED=1
        note "$what (service not answering on port $PORT)"
        step_done "not responding" "$C_WARN"
        # A failure is never quiet: this is the whole diagnosis, printed in both
        # modes, because the user has nothing else to go on.
        say ""
        say "  $SERVICE_NAME was started but nothing answered on port $PORT."
        say "  See what it said:"
        say "      systemctl --user status $SERVICE_NAME"
        say "      journalctl --user -u $SERVICE_NAME -n 50 --no-pager"
        say ""
    fi
}

# The launchd equivalent of UNIT_CONTENT, built once for the same reason.
# RunAtLoad starts it now and at every login; KeepAlive brings it back if it
# dies, which is what Restart=always does on the systemd side.
AGENT_CONTENT="$(cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$AGENT_LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$VENV_PY</string>
        <string>$INSTALL_DIR/app.py</string>
        <string>--port</string>
        <string>$PORT</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$INSTALL_DIR</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$SERVICE_PATH</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$INSTALL_DIR/pockettui.log</string>
    <key>StandardErrorPath</key>
    <string>$INSTALL_DIR/pockettui.log</string>
</dict>
</plist>
EOF
)"

AGENT_BACKUP=""

# Load the agent and report. `bootstrap gui/<uid>` is the modern spelling;
# `load -w` is the one that works on 10.10 and earlier, and is still accepted
# (deprecated) after. An already-loaded agent makes bootstrap fail, so unload
# first — that is also what makes a re-run pick up a rewritten plist.
start_agent() {
    local what="$1" uid
    uid="$(id -u)"
    touched_outside          # a loaded agent is launchd state, not just a file
    launchctl bootout "gui/$uid/$AGENT_LABEL" >/dev/null 2>&1 || true
    if ! launchctl bootstrap "gui/$uid" "$AGENT_PATH" >/dev/null 2>&1; then
        launchctl unload -w "$AGENT_PATH" >/dev/null 2>&1 || true
        launchctl load -w "$AGENT_PATH" >/dev/null 2>&1 || true
    fi
    # Same reasoning as start_service: `launchctl list` shows a label launchd
    # knows about, not a server that bound the port.
    if wait_for_server; then
        SERVICE_INSTALLED=1
        vsay "  $AGENT_LABEL running on port $PORT"
        note "$what and started $AGENT_LABEL"
        step_done "running"
    else
        SERVICE_INSTALLED=1
        note "$what (agent not answering on port $PORT)"
        step_done "not responding" "$C_WARN"
        say ""
        say "  $AGENT_LABEL was loaded but nothing answered on port $PORT."
        say "  See what it said:"
        say "      launchctl list | grep $AGENT_LABEL"
        say "      tail -50 $INSTALL_DIR/pockettui.log"
        say ""
    fi
}

# No service manager, or one that would not take our unit: run start.sh detached
# so the server is genuinely up when the installer exits. nohup plus disown
# survives this shell; it does NOT survive a reboot, which is why it is the
# fallback and not the plan, and why the summary says so out loud.
start_background() {
    local log="$INSTALL_DIR/pockettui.log"
    # Already answering (a service from an earlier run, or a hand-started copy)?
    # Starting a second one would only collide on the port.
    if wait_for_server 1; then
        BACKGROUND_STARTED=1
        note "the backend was already running on port $PORT"
        step_done "already running"
        return 0
    fi
    nohup "$INSTALL_DIR/start.sh" >>"$log" 2>&1 </dev/null &
    disown 2>/dev/null || true
    if wait_for_server; then
        BACKGROUND_STARTED=1
        note "started $INSTALL_DIR/start.sh in the background"
        step_done "running (no service)"
    else
        note "could not start $INSTALL_DIR/start.sh"
        step_done "failed to start" "$C_WARN"
        say ""
        say "  Nothing answered on port $PORT. See what it said:"
        say "      tail -50 $log"
        say "  Or run it in the foreground to watch it fail:"
        say "      $INSTALL_DIR/start.sh"
        say ""
    fi
}

# Starting the backend is the point of the install, so it is not a question any
# more: with a service manager the unit is written and started, without one the
# server is put in the background by hand. What stays a question is somebody
# else's file — an existing unit that differs from ours is theirs, and is never
# overwritten without being asked, exactly as before.
step "Starting PocketTUI"
if [[ "$HAVE_SYSTEMD" == "1" ]]; then
    vsay "  This writes $UNIT_PATH,"
    vsay "  runs 'systemctl --user daemon-reload', and enables + starts the service."

    # A unit is a whole file: there is no appending to it the way there is with
    # .tmux.conf, so an existing one can only be replaced, left alone, or the
    # step abandoned.
    UNIT_EXISTS=0
    UNIT_SAME=0
    if [[ -e "$UNIT_PATH" ]]; then
        UNIT_EXISTS=1
        [[ "$(cat "$UNIT_PATH")" == "$UNIT_CONTENT" ]] && UNIT_SAME=1
    fi

    if [[ "$UNIT_EXISTS" == "1" ]] && [[ "$UNIT_SAME" == "1" ]]; then
        # Nothing to lose and nothing to decide: the file already says exactly
        # what we would write. Don't back it up, don't rewrite it, just start it.
        vsay "  $UNIT_PATH already matches what this installer writes."
        start_service "kept the identical $UNIT_PATH"
    elif [[ "$UNIT_EXISTS" == "1" ]]; then
        # Their file, possibly customised (port, Environment=, Restart=). The
        # one irreversible option is the overwrite, so it never happens on its
        # own — the automatic path is to leave it and start what is there.
        # Printed in both modes: the service now running may not be ours.
        say ""
        say "  ${C_WARN}NOTE${C_RESET} $UNIT_PATH already exists and differs from"
        say "  the unit this installer writes — leaving your file alone and"
        say "  starting it as it is. To use ours instead, replace it yourself:"
        say "      cp $UNIT_PATH $UNIT_PATH.bak"
        say "      \$EDITOR $UNIT_PATH   # see the unit in the notes file"
        say "      systemctl --user daemon-reload"
        say ""
        note "left the existing $UNIT_PATH alone (differs from ours)"
        start_service "kept your existing $UNIT_PATH"
    else
        # Bare `mkdir -p` here would abort the whole script under `set -e` on an
        # unwritable ~/.config, losing the summary and next steps for an install
        # that otherwise succeeded. Report, fall back to nohup, and carry on.
        if ! mkdir -p "$UNIT_DIR" 2>/dev/null; then
            say "  WARNING: could not create $UNIT_DIR — starting without a service."
            note "could not create $UNIT_DIR (no service installed)"
            start_background
        elif ! printf '%s\n' "$UNIT_CONTENT" > "$UNIT_PATH" 2>/dev/null; then
            say "  WARNING: could not write $UNIT_PATH — starting without a service."
            note "could not write $UNIT_PATH (no service installed)"
            start_background
        else
            start_service "installed $UNIT_PATH"
        fi
    fi
elif [[ "$HAVE_LAUNCHD" == "1" ]]; then
    # Same shape as the systemd branch above, in launchd's spelling.
    vsay "  This writes $AGENT_PATH and loads it with launchctl,"
    vsay "  so PocketTUI starts at login."

    AGENT_EXISTS=0
    AGENT_SAME=0
    if [[ -e "$AGENT_PATH" ]]; then
        AGENT_EXISTS=1
        [[ "$(cat "$AGENT_PATH")" == "$AGENT_CONTENT" ]] && AGENT_SAME=1
    fi

    if [[ "$AGENT_EXISTS" == "1" ]] && [[ "$AGENT_SAME" == "1" ]]; then
        vsay "  $AGENT_PATH already matches what this installer writes."
        start_agent "kept the identical $AGENT_PATH"
    elif [[ "$AGENT_EXISTS" == "1" ]]; then
        say ""
        say "  ${C_WARN}NOTE${C_RESET} $AGENT_PATH already exists and differs from"
        say "  the agent this installer writes — leaving your file alone and"
        say "  loading it as it is. To use ours instead, replace it yourself:"
        say "      cp $AGENT_PATH $AGENT_PATH.bak"
        say "      \$EDITOR $AGENT_PATH   # see the agent in the notes file"
        say ""
        note "left the existing $AGENT_PATH alone (differs from ours)"
        start_agent "kept your existing $AGENT_PATH"
    else
        if ! mkdir -p "$AGENT_DIR" 2>/dev/null; then
            say "  WARNING: could not create $AGENT_DIR — starting without an agent."
            note "could not create $AGENT_DIR (no agent installed)"
            start_background
        elif ! printf '%s\n' "$AGENT_CONTENT" > "$AGENT_PATH" 2>/dev/null; then
            say "  WARNING: could not write $AGENT_PATH — leaving it alone."
            note "could not write $AGENT_PATH (no agent installed)"
            start_background
        else
            start_agent "installed $AGENT_PATH"
        fi
    fi
else
    # No systemd user session and no launchd: a container, an ssh login with no
    # user bus, a BSD. Nothing can bring the server back after a reboot, but the
    # installer can still leave it running now, which is what the user came for.
    vsay "  No systemd user session and no launchd — starting in the background."
    note "no service manager available (started in the background)"
    start_background
fi

# ---------------------------------------------------------------------------
# tmux config
# ---------------------------------------------------------------------------
TMUX_CONF="$HOME/.tmux.conf"
TMUX_LINE="set-environment -gu ZDOTDIR"

# Same class as the service above: this edits a file outside $INSTALL_DIR, so it
# only happens if the user says so out loud. Multiplexers that wrap the shell for
# shell-integration (cmux is the one seen in the wild) point ZDOTDIR at a
# per-attach relay dir (~/.cmux/relay/<port>.shell). If the tmux SERVER is first
# started from inside one, that value is captured into the server environment,
# where it outlives the relay dir: every later session inherits it, zsh looks for
# .zshrc in a directory that is gone, and the shell comes up with none of the
# user's config. app.py already pins ZDOTDIR=$HOME for the sessions it creates,
# so this line only matters for sessions the user makes some other way.
#
# An update skips the offer entirely: it was already answered on the install
# that is being updated, and re-asking an optional question on every version
# bump is how an update stops being a thing people run.
if [[ "$UPDATE" != "1" ]] && command -v tmux >/dev/null 2>&1; then
    # An already-present uncommented copy means there is nothing to do; a
    # commented-out one does not count. Leading whitespace and any internal
    # spacing are tolerated so re-runs never stack duplicates.
    if [[ -e "$TMUX_CONF" ]] \
       && grep -Eq '^[[:space:]]*set-environment[[:space:]]+-gu[[:space:]]+ZDOTDIR[[:space:]]*$' "$TMUX_CONF"; then
        step_quiet "tmux config already has the ZDOTDIR line"
        vsay "  $TMUX_CONF is fine as it is — nothing to do."
    else
        step_quiet "Optional: keep new tmux sessions out of a stale ZDOTDIR"
        vsay "  This would append one line to $TMUX_CONF:"
        vsay ""
        vsay "      $TMUX_LINE"
        vsay ""
        vsay "  It only affects tmux sessions you start yourself; PocketTUI's own"
        vsay "  sessions already set ZDOTDIR=\$HOME."
        if [[ "$INTERACTIVE" != "1" ]]; then
            # Nobody to ask. Silent on a quiet run: it is optional, it affects
            # only the user's own tmux sessions, and README.md has the line.
            vsay ""
            vsay "  Non-interactive (piped) run — skipping. Add that line yourself if"
            vsay "  your tmux sessions come up without your zsh config."
            note "skipped tmux.conf line (non-interactive)"
        elif confirm "  Add one line to $TMUX_CONF so your own tmux sessions keep your shell config?"; then
            # A failed >> redirection prints its own error whatever we do with
            # stderr, so check for a writable target first rather than finding
            # out by botching one. An absent file needs a writable directory.
            if { [[ -e "$TMUX_CONF" ]] && [[ -w "$TMUX_CONF" ]]; } \
               || { [[ ! -e "$TMUX_CONF" ]] && [[ -w "$HOME" ]]; }; then
                # Append, never truncate. A file whose last line has no newline
                # would otherwise get our line glued on, so top one up first.
                if [[ -s "$TMUX_CONF" ]] && [[ -n "$(tail -c 1 "$TMUX_CONF")" ]]; then
                    printf '\n' >> "$TMUX_CONF"
                fi
                { printf '\n# --- drop a wrapper'"'"'s per-attach ZDOTDIR (added by PocketTUI) ---\n'
                  printf '%s\n' "$TMUX_LINE"; } >> "$TMUX_CONF"
                say "  Added. Existing tmux servers keep the old environment until"
                say "  they restart (tmux kill-server), or clear it now with:"
                say "      tmux $TMUX_LINE"
                say ""
                touched_outside
                note "appended the ZDOTDIR line to $TMUX_CONF"
            else
                say "  WARNING: $TMUX_CONF is not writable — leaving it alone."
                say "  PocketTUI works regardless; add this line by hand if you want it:"
                say "      $TMUX_LINE"
                note "could not write $TMUX_CONF (left alone)"
            fi
        else
            vsay "  Skipped — $TMUX_CONF was not touched. To do it yourself, add:"
            vsay "      $TMUX_LINE"
            note "skipped tmux.conf line (declined)"
        fi
    fi
fi

# ---------------------------------------------------------------------------
# Voice-to-text
# ---------------------------------------------------------------------------
# Optional in the same sense as the two steps above: the install is complete and
# the backend is running without it. app.py answers the mic key with
# a 503 "not_setup" while voice/ is absent, so nothing here can break a working
# install — which is also why every failure below is a warning plus the run-later
# hint, never a die. The work itself is setup_voice.sh, which ships next to
# app.py and is idempotent, so the hint is always a valid thing to run.
VOICE_SCRIPT="$INSTALL_DIR/setup_voice.sh"
VOICE_HINT="cd $INSTALL_DIR && ./setup_voice.sh"

# Whisper needs both its files (a half-finished build reads as no build at
# all); Parakeet needs a model directory holding all four files sherpa-onnx
# requires — mirrors app.py's _parakeet_dir_complete()/PARAKEET_FILES, so this
# check and the one that decides whether app.py can load Parakeet never
# disagree. Either engine alone is enough for the mic to work; app.py prefers
# Parakeet when both are present.
voice_installed_whisper() {
    [[ -x "$INSTALL_DIR/voice/whisper-cli" ]] && [[ -s "$INSTALL_DIR/voice/ggml-base.en.bin" ]]
}

voice_installed_parakeet() {
    local dir
    for dir in "$INSTALL_DIR"/voice/parakeet/sherpa-onnx-*parakeet*; do
        [[ -d "$dir" ]] || continue
        [[ -f "$dir/encoder.int8.onnx" ]] || continue
        [[ -f "$dir/decoder.int8.onnx" ]] || continue
        [[ -f "$dir/joiner.int8.onnx" ]] || continue
        [[ -f "$dir/tokens.txt" ]] || continue
        return 0
    done
    return 1
}

# What setup_voice.sh cannot provide for itself, and only for choices that
# include whisper — Parakeet is a plain download and needs none of this. git
# is in there because it clones whisper.cpp; ffmpeg is not, because it is a
# runtime dependency of the transcription, not of the build — a missing one is
# a warning below, not a reason to skip the build. Two spellings come out of
# one pass: a comma list to read, and the package names to hand
# pkg_install_cmd. c++ is the compiler this needs but nobody's package is
# called that, so it is quoted as its distro name.
VOICE_MISSING=""
VOICE_MISSING_PKGS=""
voice_check_tools() {
    local tool pkg
    VOICE_MISSING=""
    VOICE_MISSING_PKGS=""
    for tool in cmake make c++ git; do
        command -v "$tool" >/dev/null 2>&1 && continue
        case "$tool:$PKG" in
            c++:apt-get)          pkg="g++" ;;
            c++:dnf|c++:yum)      pkg="gcc-c++" ;;
            c++:zypper)           pkg="gcc-c++" ;;
            c++:pacman)           pkg="gcc" ;;
            c++:apk)              pkg="g++" ;;
            c++:*)                pkg="a C++ compiler" ;;
            *)                    pkg="$tool" ;;
        esac
        VOICE_MISSING="${VOICE_MISSING:+$VOICE_MISSING, }$tool"
        VOICE_MISSING_PKGS="${VOICE_MISSING_PKGS:+$VOICE_MISSING_PKGS }$pkg"
    done
    [[ -n "$VOICE_MISSING" ]]
}

# Ask which engine(s) to install, the same four choices setup_voice.sh offers
# when run with no flag — this is the interactive equivalent of that menu. Any
# reply other than 1-3 (including EOF) reads as choice 4, None. Only
# INTERACTIVE (fd 3 open) calls this; a non-interactive run never does.
voice_ask_engine() {
    local reply
    {
        printf '  Which voice engine should PocketTUI use?\n'
        printf '    %s1) Parakeet%s %s(recommended, ~600 MB download, fastest)%s\n' "$C_STEP" "$C_RESET" "$C_OK" "$C_RESET"
        printf '    %s2) Whisper%s %s(~142 MB, builds whisper.cpp)%s\n' "$C_STEP" "$C_RESET" "$C_DIM" "$C_RESET"
        printf '    %s3) Both%s\n' "$C_STEP" "$C_RESET"
        printf '    %s4) None%s %s— use phone dictation%s\n' "$C_STEP" "$C_RESET" "$C_DIM" "$C_RESET"
        printf '  choice %s[1-4]%s: ' "$C_DIM" "$C_RESET"
    } >&3
    IFS= read -r reply <&3 || reply=4
    printf '%s' "$reply"
}

if [[ -f "$VOICE_SCRIPT" ]]; then
    HAVE_WHISPER=0; voice_installed_whisper && HAVE_WHISPER=1
    HAVE_PARAKEET=0; voice_installed_parakeet && HAVE_PARAKEET=1

    if [[ "$HAVE_WHISPER" == "1" && "$HAVE_PARAKEET" == "1" ]]; then
        step_quiet "Voice-to-text already set up"
        vsay "  $INSTALL_DIR/voice already has Parakeet and whisper-cli — nothing to do."
    else
        step_quiet "Optional: local voice-to-text for code dictation"
        if [[ "$HAVE_WHISPER" == "1" ]]; then
            vsay "  Whisper is set up; Parakeet (faster, more accurate) is not."
        elif [[ "$HAVE_PARAKEET" == "1" ]]; then
            vsay "  Parakeet is set up; whisper (the fallback engine) is not."
        else
            vsay "  Transcribes dictation locally — nothing leaves this machine."
        fi
        if [[ "$UPDATE" == "1" ]]; then
            # An update installs the version that is on offer, nothing else. A
            # 600 MB download is not part of that, and someone updating has
            # already had this question once — one line, and on with it. One
            # engine installed is a complete setup (the menu below offers a
            # single engine), so there's nothing to say unless neither is in.
            if [[ "$HAVE_WHISPER" == "0" && "$HAVE_PARAKEET" == "0" ]]; then
                say "  ${C_DIM}Voice-to-text (local dictation) is not set up. To add it:"
                say "      $VOICE_HINT$C_RESET"
                note "left voice setup alone (update)"
            fi
        elif [[ "$INTERACTIVE" != "1" ]]; then
            # Nobody to ask, and this can be a multi-minute build or a 600 MB
            # download — described rather than done, like the two optional
            # steps above it. Printed in both modes: unlike those, this one is
            # not written up in the notes file. One engine installed is a
            # complete setup, so this only fires when neither is in.
            if [[ "$HAVE_WHISPER" == "0" && "$HAVE_PARAKEET" == "0" ]]; then
                say "  ${C_DIM}Voice-to-text (local dictation) is not set up. To add it:"
                say "      $VOICE_HINT$C_RESET"
                note "skipped voice setup (non-interactive)"
            fi
        else
            VOICE_CHOICE="$(voice_ask_engine)"
            # An engine already installed is dropped from the choice rather than
            # rebuilt or re-downloaded — setup_voice.sh's per-half idempotency
            # would skip it anyway, but this keeps the flag sent matching what
            # is actually about to happen.
            WANT_WHISPER=0; WANT_PARAKEET=0
            case "$VOICE_CHOICE" in
                1) WANT_PARAKEET=1 ;;
                2) WANT_WHISPER=1 ;;
                3) WANT_WHISPER=1; WANT_PARAKEET=1 ;;
                *) WANT_WHISPER=-1 ;;  # sentinel for "4 / None"
            esac
            if [[ "$WANT_WHISPER" == "-1" ]]; then
                vsay "  Skipped — nothing was installed. Phone dictation works regardless."
                vsay "  To add local voice-to-text later:"
                vsay "      $VOICE_HINT"
                note "skipped voice setup (declined)"
            else
                [[ "$HAVE_WHISPER" == "1" ]] && WANT_WHISPER=0
                [[ "$HAVE_PARAKEET" == "1" ]] && WANT_PARAKEET=0
                if [[ "$WANT_WHISPER" == "0" && "$WANT_PARAKEET" == "0" ]]; then
                    vsay "  Already installed — nothing to do."
                    note "skipped voice setup (chosen engine already installed)"
                elif [[ "$WANT_WHISPER" == "1" ]] && voice_check_tools; then
                    # A compiler and cmake are not installable in user space the
                    # way micromamba and uv are, so this is the one place the
                    # package manager is quoted back rather than worked around.
                    # Parakeet is a plain download and needs none of this, so a
                    # Parakeet-only choice never reaches here.
                    say "  ${C_WARN}Not building whisper: $VOICE_MISSING missing.$C_RESET Install with:"
                    say "      $(pkg_install_cmd "$VOICE_MISSING_PKGS")"
                    say "  Then set voice up with:"
                    say "      $VOICE_HINT"
                    say ""
                    note "skipped voice setup ($VOICE_MISSING missing)"
                else
                    # Only needed once someone actually dictates, so it is said
                    # and then setup goes ahead regardless.
                    if ! command -v ffmpeg >/dev/null 2>&1; then
                        say "  ${C_WARN}NOTE${C_RESET} ffmpeg is not installed — transcription needs it at"
                        say "  runtime. Install it before using the mic:  $(pkg_install_cmd ffmpeg)"
                    fi
                    if [[ "$WANT_WHISPER" == "1" && "$WANT_PARAKEET" == "1" ]]; then
                        VOICE_FLAG="--all"
                        say "  Setting up whisper and Parakeet — this takes a few minutes."
                    elif [[ "$WANT_WHISPER" == "1" ]]; then
                        VOICE_FLAG="--whisper"
                        say "  Building whisper.cpp — this takes a few minutes."
                    else
                        VOICE_FLAG="--parakeet"
                        say "  Downloading Parakeet — this takes a while on a slow connection."
                    fi
                    say ""
                    # Its output goes straight to the terminal: a build or a
                    # 600 MB download this long with nothing on screen reads as
                    # a hang. `set -e` would take the whole install down with
                    # it, hence the explicit test.
                    if bash "$VOICE_SCRIPT" "$VOICE_FLAG"; then
                        say ""
                        say "  Voice is ready — the mic key will transcribe now."
                        note "set up voice-to-text in $INSTALL_DIR/voice"
                        # A service started before voice/ existed has already
                        # decided it is absent, so it has to look again.
                        if [[ "$SERVICE_INSTALLED" == "1" ]] || [[ "$BACKGROUND_STARTED" == "1" ]]; then
                            say "  ${C_DIM}Restart PocketTUI to pick it up.$C_RESET"
                        fi
                        say ""
                    else
                        say ""
                        say "  ${C_WARN}Voice setup did not finish — the rest of the install is fine"
                        say "  and PocketTUI works without it.$C_RESET Try it again with:"
                        say "      $VOICE_HINT"
                        say ""
                        note "voice setup failed (install unaffected)"
                    fi
                fi
            fi
        fi
    fi
fi

# ---------------------------------------------------------------------------
# Where the phone should point
# ---------------------------------------------------------------------------
# Printing the real hostname beats printing a <machine>.<tailnet> placeholder
# the user has to go and resolve, but not at the cost of hanging: a tailscaled
# that is starting, logged out, or wedged can make these calls block for a long
# time. Every call is wrapped in a timeout, every failure falls through to the
# placeholder, and none of them is allowed to fail the script.
TS_HOST=""
ts_try() {
    # `timeout` is coreutils and not on macOS by default, so it is used only if
    # present; the tailscale CLI's own --timeout covers the common case.
    if command -v timeout >/dev/null 2>&1; then
        timeout 3 "$@" 2>/dev/null
    else
        "$@" 2>/dev/null
    fi
}

if [[ "$HAVE_TAILSCALE" == "1" ]]; then
    # `status --json` carries Self.DNSName, the fully-qualified name with a
    # trailing dot. Parsed with sed rather than jq, which is not a dependency
    # this installer is willing to acquire.
    TS_HOST="$(ts_try tailscale status --json \
        | sed -n 's/.*"DNSName"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
        | head -1 | sed 's/\.$//' || true)"
    # Older CLIs, or a machine where --json is unavailable: `serve status`
    # prints the https:// URL on its first line.
    if [[ -z "$TS_HOST" ]]; then
        TS_HOST="$(ts_try tailscale serve status \
            | sed -n 's|^https://\([^ /]*\).*|\1|p' | head -1 || true)"
    fi
fi

SERVE_CMD="tailscale serve --bg --set-path /pockettui $PORT"

# Publishing the port is what turns the tailnet name into a working address, but
# the installer only ever prints the command — it never runs it. Running it from
# here was three failure modes wearing one coat: a sudo password prompt in the
# middle of an install for everyone who is not the Tailscale operator, a wedged
# tailscaled that swallowed the run until a timeout fired, and cert provisioning
# that hangs behind a question the user cannot see. One line the user pastes has
# none of those. The `serve status` gate is kept so a re-run on an already
# published machine stays silent.
if [[ "$HAVE_TAILSCALE" == "1" ]] \
   && ! ts_try tailscale serve status \
        | grep -q "^|-- /pockettui .*:$PORT\$"; then
    step_quiet "Publish port $PORT on your tailnet yourself"
    vsay "  Not published yet — the tailnet name resolves but /pockettui 404s."
    if [[ -n "$TS_HOST" ]]; then
        vsay "  This makes it https://$TS_HOST/pockettui:"
    else
        vsay "  This publishes it:"
    fi
    vsay ""
    vsay "      $SERVE_CMD"
    vsay ""
    vsay "  ${C_DIM}(prefix it with sudo if you are not the Tailscale operator)$C_RESET"
    note "tailscale serve left to the user (command printed)"
fi

# A tailnet name that resolves says nothing about whether /pockettui is actually
# published — the reported bug was an address that looked right and 404ed. Only
# a path that answers is presented as the address, so probe it: 404 is the
# unpublished case, anything else that comes back at all is good enough. curl is
# not a dependency on the local-checkout path and the probe is never fatal.
TS_SERVED=0
if [[ -n "$TS_HOST" ]] && command -v curl >/dev/null 2>&1; then
    TS_CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
        "https://$TS_HOST/pockettui/" 2>/dev/null || true)"
    case "$TS_CODE" in
        ""|000|404) TS_SERVED=0 ;;
        *)          TS_SERVED=1 ;;
    esac
fi

# The LAN address. It is the thing that works right now whenever the tailnet URL
# is unverified, so it is collected in that case too, not only when there is no
# tailnet name at all. hostname -I is Linux-only; ipconfig is the macOS
# spelling. Both are allowed to come back empty.
#
# hostname -I prints every address on the box in an arbitrary order, Tailscale's
# among them, so field 1 is not a LAN address — it is whichever address sorted
# first. Printing a tailnet IP under the words "on this LAN" is the same bug as
# printing an unpublished serve URL, so the candidates are filtered rather than
# taken on faith. Glob matching keeps this bash 3.2 clean with no new tools.
lan_candidate() {
    local _ip _private="" _other=""
    for _ip in $1; do
        case "$_ip" in
            # IPv6 (Tailscale's fd7a:115c:a1e0::/48 included) and loopback are
            # not addresses to hand a phone.
            *:*|127.*) continue ;;
            # 100.64.0.0/10 is the CGNAT block Tailscale allocates from. Only
            # 100.64-127 is carrier-grade NAT; 100.0-63 and 100.128-255 are
            # ordinary public space and must survive this filter.
            100.6[4-9].*|100.[7-9][0-9].*|100.1[0-1][0-9].*|100.12[0-7].*) continue ;;
            # A real RFC1918 address is the one most likely to be the LAN, so it
            # wins over anything else still standing.
            10.*|192.168.*|172.1[6-9].*|172.2[0-9].*|172.3[0-1].*)
                if [[ -z "$_private" ]]; then _private="$_ip"; fi ;;
            *)
                if [[ -z "$_other" ]]; then _other="$_ip"; fi ;;
        esac
    done
    printf '%s' "${_private:-$_other}"
}

LAN_IP=""
if [[ "$TS_SERVED" != "1" ]]; then
    if command -v hostname >/dev/null 2>&1; then
        LAN_IP="$(lan_candidate "$(hostname -I 2>/dev/null || true)" || true)"
    fi
    if [[ -z "$LAN_IP" ]] && command -v ipconfig >/dev/null 2>&1; then
        LAN_IP="$(ipconfig getifaddr en0 2>/dev/null || true)"
    fi
fi

# Only an address known to answer earns the headline: a verified serve, or the
# LAN address. A tailnet name whose /pockettui 404s is not one of them, and the
# warning below already says so in full — repeating it on the Address line would
# print the broken address twice and contradict the explanation. With neither,
# there is no Address line at all; the summary states the problem instead.
PHONE_ADDR=""
if [[ "$TS_SERVED" == "1" ]]; then
    PHONE_ADDR="$TS_HOST/pockettui"
elif [[ -n "$LAN_IP" ]]; then
    PHONE_ADDR="$LAN_IP:$PORT"
fi

# ---------------------------------------------------------------------------
# README in the install dir
# ---------------------------------------------------------------------------
# Everything the old summary printed as a tutorial — the Tailscale steps, the
# rotate-token command, how to stop and inspect the service, Add to Home Screen
# — lives here instead. It is written on every run so it always describes the
# install that is actually on disk (this port, this interpreter, this unit), and
# the final summary points at it with one line.
if [[ "$HAVE_SYSTEMD" == "1" ]]; then
    README_SERVICE="## The service

PocketTUI runs as a systemd **user** service, started at boot.

    systemctl --user status $SERVICE_NAME     # is it running?
    systemctl --user restart $SERVICE_NAME    # after changing app.py
    systemctl --user stop $SERVICE_NAME       # stop it
    systemctl --user disable $SERVICE_NAME    # stop it starting at boot
    journalctl --user -u $SERVICE_NAME -f     # follow the log

The unit is at \`$UNIT_PATH\`.

A user service only survives you logging out if lingering is on:

    loginctl enable-linger $USER"
elif [[ "$HAVE_LAUNCHD" == "1" ]]; then
    README_SERVICE="## The service

PocketTUI runs as a launchd user agent, started at login.

    launchctl list | grep $AGENT_LABEL              # is it running?
    launchctl kickstart -k gui/\$(id -u)/$AGENT_LABEL  # restart it
    launchctl bootout gui/\$(id -u)/$AGENT_LABEL       # stop it
    tail -f $INSTALL_DIR/pockettui.log

The agent is at \`$AGENT_PATH\`."
else
    README_SERVICE="## Running it

This machine has no systemd user session and no launchd, so the installer
started the server in the background with nohup. That does **not** survive a
reboot. After one, start it again with:

    $INSTALL_DIR/start.sh

To stop it:

    pkill -f '$INSTALL_DIR/app.py'"
fi

# Installing into a checkout (./install.sh with no POCKETTUI_DIR) means the
# install dir IS the repo, which already has its own README.md — the project's,
# not this install's. Writing ours there would destroy a tracked file, so the
# generated notes go to a distinct name in that one case. Everywhere else, where
# the install dir holds only what the tarball unpacked, README.md is the name a
# user will actually look for.
NOTES_NAME="README.md"
if [[ "$LOCAL_CHECKOUT" == "1" ]] && [[ "$SRC_DIR" -ef "$INSTALL_DIR" ]]; then
    NOTES_NAME="POCKETTUI-NOTES.md"
fi
NOTES_PATH="$INSTALL_DIR/$NOTES_NAME"

cat > "$NOTES_PATH" <<READMEEOF
# PocketTUI

Installed by install.sh. The backend serves port $PORT from \`$INSTALL_DIR\`;
the phone client is the hosted page at $BASE_URL/app/.

## Pairing

The pairing code was printed by the installer and is stored in
\`$INSTALL_DIR/.token\`. Anyone with both the address and that code can use
this machine's shell, so treat it like a password.

To invalidate it and force every paired phone to enter a new one:

    $VENV_PY $INSTALL_DIR/app.py --rotate-token

Updating (\`--update\`) does **not** rotate the code; an existing one is kept
so already-paired phones keep working. Forcing a re-install over an existing
directory (\`POCKETTUI_FORCE=1\`, or choosing to re-install when already on
the latest version) mints a new one.

## Updating

    pockettui update

or, the same thing without relying on the command being on your PATH:

    curl -fsSL $BASE_URL/install.sh | bash -s -- --update

An update replaces the program files in place. The pairing code, the voice
models in \`voice/\`, the learned-word store and this install's Python
environment are all kept, so nothing has to be paired or downloaded again.
\`pockettui version\` says which build is installed here and whether there is
a newer one.

$README_SERVICE

## Reaching it from your phone

The backend only has to be reachable from the phone. Tailscale is the easiest
way, and puts it on your own tailnet rather than the public internet.

1. Install Tailscale on this machine and on the phone, both logged into the
   same tailnet.
2. Enable HTTPS certificates once, in the Tailscale admin console
   (admin console > DNS > HTTPS Certificates).
3. Publish this port under a path:

       tailscale serve --bg --set-path /pockettui $PORT

   install.sh does not run this for you — it prints it when Tailscale is
   present and the path is not published yet, and leaves it to you. It only
   runs without \`sudo\` for the machine's Tailscale operator (the user in
   \`tailscale debug prefs\`, under \`OperatorUser\`); for anyone else the
   command needs \`sudo\` in front.

   To undo it:

       tailscale serve --set-path /pockettui off

4. Find the address:

       tailscale serve status

   It looks like \`https://<machine>.<tailnet>.ts.net\`, so the address to
   type into the phone is \`<machine>.<tailnet>.ts.net/pockettui\`, with the
   port field left **empty** — the serve terminates TLS on 443 and forwards
   to $PORT itself; putting $PORT in the port field points the phone at the
   wrong door. Until the path is published the name still resolves but
   returns 404 — on the same network, opening \`http://<this machine's
   IP>:$PORT/\` directly on the phone works in the meantime (the backend
   serves this same app; only the code is needed).

If you would rather not path-serve, \`tailscale serve --bg $PORT\` publishes it
at the root instead; then enter the bare hostname on the phone, again with the
port field empty.

Any other route works too — a VPN or an SSH tunnel with the app at
$BASE_URL/app/, or the backend's own \`http://<address>:$PORT/\` page on a
network the phone shares. One thing that cannot work: typing a plain-http
address into the hosted app — it is served over https, and browsers refuse
to let an https page call an http backend. Whatever route you
use, the pairing code keeps the connection authenticated: an address alone is
not an open shell.

## On the phone

1. Open $BASE_URL/app/
2. On iPhone or iPad, add it to the Home Screen first: Share > Add to Home
   Screen. Safari and the home-screen app keep separate storage, so an address
   and code typed in the Safari tab do not carry over to the installed app and
   it asks for them again. On Android Chrome the same thing is under the menu,
   as "Install app", and storage is shared either way.
3. Open the icon and enter the address and the pairing code there.

## What is in here

- \`app.py\` — the backend
- \`start.sh\` — runs it on port $PORT with this install's interpreter
- \`.token\` — the pairing code
- \`VERSION\` — which build this is
- \`.venv\` / \`.micromamba\` — this install's Python environment
READMEEOF

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
# --verbose keeps the full changelog: on a `curl | bash` install, the question
# "what did that just do to my machine?" deserves an answer that does not
# require reading the script.
# What is on disk now that the tarball has been unpacked, which is the only
# version worth reporting: the one the user is about to run.
NOW_VERSION="$(installed_version)"

if [[ "$VERBOSE" == "1" ]]; then
    say ""
    say "============================================================"
    if [[ "$UPDATE" == "1" ]]; then
        say "Updated $INSTALL_DIR"
    else
        say "Installed to $INSTALL_DIR"
    fi
    say ""
    say "What this script changed:"
    # ${arr[@]+...} because bash 3.2 — still /bin/bash on macOS — treats an empty
    # array's expansion as an unbound variable under `set -u` and would abort here.
    for d in ${DID[@]+"${DID[@]}"}; do say "  - $d"; done
    case "$ENV_KIND" in
        venv)        say "  - created $INSTALL_DIR/.venv (fastapi, uvicorn)" ;;
        "venv (uv)") say "  - created $INSTALL_DIR/.venv with uv (fastapi, uvicorn)" ;;
        *)           say "  - created the $ENV_KIND (fastapi, uvicorn)" ;;
    esac
    [[ -n "$ENV_BIN" ]] && say "  - tmux is provided by that environment (no system tmux was found)"
    say "  - wrote $INSTALL_DIR/start.sh"
    say "  - wrote $NOTES_PATH"
    if [[ "$TOKEN_KEPT" == "1" ]]; then
        say "  - kept the existing pairing token at $INSTALL_DIR/.token"
    else
        say "  - wrote a new pairing token to $INSTALL_DIR/.token"
    fi
    # Only true when nothing outside $INSTALL_DIR was touched. DID cannot answer
    # this: it also holds install-dir work and steps the user declined.
    [[ "$OUTSIDE" -eq 0 ]] && say "  Nothing else on this machine was modified."
fi

# The one thing an update is asked to report, and it is not verbose-only: the
# whole reason for running it was to change this number. A version that did not
# move (a re-install, or a deploy that has not landed yet) is said as plainly as
# one that did, rather than dressed up as a successful upgrade.
if [[ "$UPDATE" == "1" ]]; then
    say ""
    if [[ "$NOW_VERSION" == "$OLD_VERSION" ]]; then
        say "  ${C_OK}Re-installed version $NOW_VERSION.$C_RESET"
    else
        say "  ${C_OK}Updated $OLD_VERSION -> $NOW_VERSION.$C_RESET"
    fi
fi

# A backup only exists when the user asked for an overwrite, so it is never
# noise — printed in both modes.
if [[ -n "$UNIT_BACKUP" ]]; then
    say ""
    say "Your previous systemd unit is saved at:"
    say "    $UNIT_BACKUP"
    say "To put it back:  cp $UNIT_BACKUP $UNIT_PATH"
    say "                 systemctl --user daemon-reload"
fi
if [[ -n "$AGENT_BACKUP" ]]; then
    say ""
    say "Your previous launchd agent is saved at:"
    say "    $AGENT_BACKUP"
    say "To put it back:  cp $AGENT_BACKUP $AGENT_PATH"
    say "                 launchctl bootout gui/\$(id -u)/$AGENT_LABEL"
    say "                 launchctl bootstrap gui/\$(id -u) $AGENT_PATH"
fi

# A QR code of the pairing URL, printed above the Address/Code box so a phone
# camera can skip typing both. Content matches the branch below exactly: the
# hosted-app URL with the address folded in when a route is verified, the
# backend's own address with no "a" when it is the LAN page itself. Built and
# rendered in Python — qrcodegen.py is vendored, zero deps — never in bash.
# Every failure here is soft: a QR is a convenience, never something the
# install can fail over. $VENV_PY exists by this point; the token is read
# canonically via app.read_token() rather than reformatted from $TOKEN_DISPLAY.
# Prints the QR and returns 0, or prints nothing (an explanatory dim note if
# there is a reason) and returns 1 — the caller only prints the security
# warning and the blank line around it when a QR actually went to the screen.
print_qr() {
    local url="$1"
    [[ -f "$INSTALL_DIR/qrcodegen.py" ]] || return 1
    # The half-block renderer prints explicit ANSI colours and Unicode block
    # glyphs, so it only belongs on a terminal that has both switched on. A
    # dumb/NO_COLOR terminal or a non-UTF-8 locale falls back to qrencode, which
    # brings its own encoding-aware renderer, and finally to no QR at all.
    local locale_utf8=0
    case "${LC_ALL:-${LC_CTYPE:-${LANG:-}}}" in *UTF-8*|*utf-8*|*UTF8*|*utf8*) locale_utf8=1 ;; esac
    if [[ -n "$C_RESET" ]] && [[ "$locale_utf8" == "1" ]]; then
        say "  Scan to pair:"
        "$VENV_PY" - "$INSTALL_DIR" "$url" <<'PYEOF' || true
import sys
sys.path.insert(0, sys.argv[1])
import qrcodegen

qr = qrcodegen.QrCode.encode_text(sys.argv[2], qrcodegen.QrCode.Ecc.MEDIUM)
size = qr.get_size()
QUIET = 2
WHITE_BG = "\033[48;5;231m"
BLACK_FG = "\033[38;5;16m"
RESET = "\033[0m"

def dark(x, y):
    if x < 0 or y < 0 or x >= size or y >= size:
        return False
    return qr.get_module(x, y)

lo = -QUIET
hi = size + QUIET
lines = []
for y in range(lo, hi, 2):
    row = [WHITE_BG, BLACK_FG]
    for x in range(lo, hi):
        top, bot = dark(x, y), dark(x, y + 1)
        row.append("█" if top and bot else "▀" if top else "▄" if bot else " ")
    row.append(RESET)
    lines.append("".join(row))
print("\n".join(lines))
PYEOF
    elif command -v qrencode >/dev/null 2>&1; then
        say "  Scan to pair:"
        qrencode -t ANSIUTF8 "$url" || true
    else
        say "  ${C_DIM}(QR skipped — no colour terminal and qrencode not found)$C_RESET"
        return 1
    fi
}

# What to open depends on the route. The hosted app is https, and a browser
# will not let an https page call a plain-http backend, so a LAN address can
# never be typed into $BASE_URL/app/ — but the backend serves the same shell
# itself, so on a LAN the phone opens it directly and only the code is left to
# type. A verified serve is https end to end, so there the hosted app plus the
# address works. The URL comes first because it is the first thing to do.
#
# The QR payload mirrors that: #pair=<base64url JSON {v,a,t}>, "a" the address
# exactly as the settings sheet's address field expects (no scheme forced —
# normalizeBackend adds one), omitted for the LAN page since it is the backend
# talking to itself; "t" the canonical 10-char token. Building it is the same
# soft-fail Python-in-heredoc rule as the box below it: anyone who scans the
# code gets this machine's shell, so the warning is printed right under it.
RULE="─────────────────────────────────────"
say ""
if [[ "$TS_SERVED" == "1" ]]; then
    QR_URL="$("$VENV_PY" - "$INSTALL_DIR" "$BASE_URL/app/" "$TS_HOST/pockettui" <<'PYEOF' || true
import base64, json, sys
sys.path.insert(0, sys.argv[1])
import app

payload = {"v": 1, "a": sys.argv[3], "t": app.read_token()}
enc = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).rstrip(b"=")
print(sys.argv[2] + "#pair=" + enc.decode())
PYEOF
    )"
    if [[ -n "$QR_URL" ]] && print_qr "$QR_URL"; then
        say "  ${C_DIM}The QR contains the pairing code — anyone who scans it gets this"
        say "  machine's shell. Don't screenshot or share it.$C_RESET"
        say ""
    fi
    say "  On your phone open  $BASE_URL/app/"
    say ""
    say "  $C_RULE$RULE$C_RESET"
    printf '   Address   %s%s%s\n' "$C_CODE" "$PHONE_ADDR" "$C_RESET"
    printf '   Code      %s%s%s\n' "$C_CODE" "$TOKEN_DISPLAY" "$C_RESET"
    say "  $C_RULE$RULE$C_RESET"
elif [[ -n "$LAN_IP" ]]; then
    QR_URL="$("$VENV_PY" - "$INSTALL_DIR" "http://$LAN_IP:$PORT/" <<'PYEOF' || true
import base64, json, sys
sys.path.insert(0, sys.argv[1])
import app

payload = {"v": 1, "t": app.read_token()}
enc = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).rstrip(b"=")
print(sys.argv[2] + "#pair=" + enc.decode())
PYEOF
    )"
    if [[ -n "$QR_URL" ]] && print_qr "$QR_URL"; then
        say "  ${C_DIM}The QR contains the pairing code — anyone who scans it gets this"
        say "  machine's shell. Don't screenshot or share it.$C_RESET"
        say ""
    fi
    say "  On your phone open  ${C_CODE}http://$LAN_IP:$PORT/$C_RESET"
    say ""
    say "  $C_RULE$RULE$C_RESET"
    printf '   Code      %s%s%s\n' "$C_CODE" "$TOKEN_DISPLAY" "$C_RESET"
    say "  $C_RULE$RULE$C_RESET"
    say "  ${C_DIM}(that page is the backend itself — no address to enter)$C_RESET"
else
    say "  $C_RULE$RULE$C_RESET"
    printf '   Code      %s%s%s\n' "$C_CODE" "$TOKEN_DISPLAY" "$C_RESET"
    say "  $C_RULE$RULE$C_RESET"
fi
if [[ -z "$PHONE_ADDR" ]]; then
    say ""
    # Nothing reachable was found. The install itself is fine — what is missing
    # is a route — and saying so is more use than inventing an address the user
    # would then have to debug. The backend is only vouched for when it actually
    # answered; the line below this block reports it when it did not.
    say "  ${C_WARN}No address to give you yet: this machine has no route a phone"
    say "  can reach.$C_RESET"
    if [[ "$SERVER_UP" == "1" ]]; then
        say "  ${C_DIM}The backend is running and the code above is valid — only the"
        say "  route is missing.$C_RESET"
    fi
    say "  ${C_WARN}Publish it on a tailnet, put this machine on a network, or"
    say "  reach it over any VPN or SSH tunnel.$C_RESET"
    say "  ${C_DIM}The routes are written out in $NOTES_PATH.$C_RESET"
fi

# No verified tailnet URL: whatever is above is at best the LAN address, so what
# is left to explain is the tailnet address that does not exist yet. A name that
# resolves but 404s is called out as exactly that rather than being printed as
# if it were reachable.
if [[ "$TS_SERVED" != "1" ]]; then
    say ""
    if [[ -n "$TS_HOST" ]]; then
        say "  ${C_WARN}Your tailnet address will be https://$TS_HOST/pockettui,"
        say "  but it is not published yet — it 404s until you run:$C_RESET"
        say "      $SERVE_CMD"
        say "  ${C_DIM}(prefix it with sudo if you are not the Tailscale operator)$C_RESET"
    elif [[ "$HAVE_TAILSCALE" == "1" ]]; then
        say "  ${C_DIM}Tailscale is installed but not reporting a name yet. Once it is"
        say "  logged in, '$SERVE_CMD'"
        say "  gives you a <machine>.<tailnet>.ts.net/pockettui address too.$C_RESET"
    elif [[ -n "$PHONE_ADDR" ]]; then
        # Only worth saying when there is an address above to say it about; the
        # no-address case has already said its piece and pointed at the notes.
        say "  ${C_DIM}That address works on this LAN. For a route from anywhere,"
        say "  install Tailscale — see $NOTES_PATH.$C_RESET"
    fi
fi

# The order matters on iOS and only on iOS: a home-screen web app gets its own
# storage container, and Add to Home Screen does not copy the Safari tab's. The
# address and code live in localStorage, so entering them in Safari first and
# adding the icon afterwards produces an app that asks for setup again. The
# installer cannot know which phone this is, so everyone gets the sentence.
# Scanning the QR is no exception — it pairs whichever container is open, so a
# scan in Safari still leaves the installed app to ask once more after adding.
if [[ "$SERVER_UP" == "1" ]]; then
    say ""
    say "  ${C_WARN}On iPhone/iPad, Add to Home Screen FIRST, then enter the address"
    say "  and code inside the installed app — after adding, it asks for the"
    say "  code once more, even if you already scanned or typed it in Safari.$C_RESET"
fi

# A LAN address hands the phone a port that a host firewall can be dropping
# silently: the backend is up and listening, the phone just gets nothing. Only
# worth saying when the address really is the LAN one — a verified tailnet URL
# has already proved the path. Detection is best-effort and advisory: no rule is
# ever changed, no sudo is asked for, and a status query that needs root and
# fails prints nothing rather than a false alarm.
if [[ "$SERVER_UP" == "1" ]] && [[ -n "$PHONE_ADDR" ]] && [[ "$TS_SERVED" != "1" ]]; then
    FW_CMD=""
    if command -v ufw >/dev/null 2>&1; then
        if ts_try ufw status | grep -qi '^Status: active'; then
            FW_CMD="sudo ufw allow $PORT/tcp"
        fi
    fi
    if [[ -z "$FW_CMD" ]] && command -v firewall-cmd >/dev/null 2>&1; then
        if [[ "$(ts_try firewall-cmd --state || true)" == "running" ]]; then
            FW_CMD="sudo firewall-cmd --add-port=$PORT/tcp --permanent && sudo firewall-cmd --reload"
        fi
    fi
    if [[ -n "$FW_CMD" ]]; then
        say ""
        say "  ${C_WARN}A firewall is active here, so port $PORT may need to be allowed"
        say "  before the phone can reach it:$C_RESET"
        say "      $FW_CMD"
    fi
fi

# Whichever way it ended up running, say so honestly, and never claim a reboot
# will bring back something nohup started.
say ""
if [[ "$SERVER_UP" != "1" ]]; then
    say "  ${C_WARN}The backend is not answering on port $PORT — see above.$C_RESET"
elif [[ "$BACKGROUND_STARTED" == "1" ]]; then
    say "  ${C_DIM}Running on port $PORT. No service manager here, so start it"
    say "  again after a reboot: $INSTALL_DIR/start.sh$C_RESET"
fi

# How to get the next version. A fresh install has never been told; an update
# already knows and does not need telling again. The command is only named as a
# command when a shell would actually find it — off PATH it is a full path, and
# the curl line is what the user gets either way.
if [[ "$UPDATE" != "1" ]]; then
    say ""
    if [[ "$WRAPPER_WRITTEN" == "1" ]] && [[ "$WRAPPER_OFF_PATH" != "1" ]]; then
        say "  ${C_DIM}To update later:  pockettui update$C_RESET"
    elif [[ "$WRAPPER_OFF_PATH" == "1" ]]; then
        say "  ${C_DIM}To update later:  $WRAPPER_PATH update"
        say "  ($USER_BIN is not on your PATH — add it to use 'pockettui' by name.)$C_RESET"
    else
        say "  ${C_DIM}To update later:  curl -fsSL $BASE_URL/install.sh | bash -s -- --update$C_RESET"
    fi
fi
say ""
