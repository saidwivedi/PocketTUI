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
# Environment:
#   POCKETTUI_DIR    install directory   (default: ~/pockettui)
#   POCKETTUI_FORCE  1 to overwrite an existing install
#   PORT             port to serve on    (default: 5560)

set -eu

BASE_URL="https://pockettui.com"
TARBALL_URL="$BASE_URL/pockettui.tar.gz"
INSTALL_DIR="${POCKETTUI_DIR:-$HOME/pockettui}"
PORT="${PORT:-5560}"
SERVICE_NAME="pockettui"
MIN_PY_MINOR=10

# Run from a clone (./install.sh) the sources are already here, so use them and
# leave the network alone; piped from curl there is nothing next to the script
# and the tarball is the only way to get them. $0 is the script itself in the
# first case and "bash" (or a pipe) in the second, so the test is whether the
# directory it resolves to actually holds the app.
SRC_DIR="$(cd "$(dirname "$0")" 2>/dev/null && pwd || true)"
LOCAL_CHECKOUT=0
if [[ -n "$SRC_DIR" ]] && [[ -f "$SRC_DIR/app.py" ]] && [[ -f "$SRC_DIR/mobile_app.html" ]]; then
    LOCAL_CHECKOUT=1
fi

say()  { printf '%s\n' "$*"; }
step() { printf '\n==> %s\n' "$*"; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

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
USER_BIN="$HOME/.local/bin"

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

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------
step "Checking prerequisites"

# Nothing here is fatal any more. What is missing is noted and then provided in
# user space further down — a missing tmux or an unusable python3 is a thing to
# fix, not a reason to send the user away to their package manager.
if command -v python3 >/dev/null 2>&1; then
    PY_VER="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo '?')"
    if usable_python3; then
        say "  python3 $PY_VER"
    else
        say "  python3 $PY_VER — not usable for a venv (needs 3.${MIN_PY_MINOR}+ with venv/ensurepip)"
    fi
else
    say "  python3 not found"
fi

HAVE_TMUX=0
if command -v tmux >/dev/null 2>&1; then
    HAVE_TMUX=1
    say "  tmux $(tmux -V | awk '{print $2}')"
else
    say "  tmux not found — it will be installed into this install's own"
    say "    environment (see below); no system packages are touched."
fi

# The backend only needs to be reachable from the phone; Tailscale is the
# easiest way to do that, but it is not this script's job to require it.
if command -v tailscale >/dev/null 2>&1; then
    say "  tailscale $(tailscale version 2>/dev/null | head -1)"
else
    say "  tailscale not found — you will need some other way to reach this"
    say "    machine from your phone (see the notes at the end)."
fi

# Only the download path needs curl; a local checkout already has the sources.
if [[ "$LOCAL_CHECKOUT" != "1" ]]; then
    command -v curl >/dev/null 2>&1 || die "curl not found."
fi

# ---------------------------------------------------------------------------
# Install directory
# ---------------------------------------------------------------------------
FRESH_DIR=1
# ./install.sh inside a clone, with no POCKETTUI_DIR set to send it elsewhere:
# the install dir *is* the checkout. There is nothing to overwrite and nothing
# to ask about — just build the venv where the sources already are.
if [[ "$LOCAL_CHECKOUT" == "1" ]] && [[ -e "$INSTALL_DIR" ]] \
   && [[ "$SRC_DIR" -ef "$INSTALL_DIR" ]]; then
    FRESH_DIR=0
    step "Installing into this checkout at $INSTALL_DIR"
    say "  The sources are already here; only .venv and start.sh are added."
elif [[ -e "$INSTALL_DIR" ]]; then
    FRESH_DIR=0
    if [[ "${POCKETTUI_FORCE:-}" != "1" ]]; then
        die "$INSTALL_DIR already exists. Re-run with POCKETTUI_FORCE=1 to overwrite, or set POCKETTUI_DIR."
    fi
    step "Replacing existing install at $INSTALL_DIR (POCKETTUI_FORCE=1)"
    say "  These are overwritten by the new copy:"
    for f in app.py mobile_app.html sw.js pockettui.service install.sh \
             icon-192.png icon-512.png vendor; do
        [[ -e "$INSTALL_DIR/$f" ]] && say "    $f"
    done
    # The venv is rebuilt on top of whatever is there; nothing else is removed.
    [[ -e "$INSTALL_DIR/.venv" ]] && say "    .venv (dependencies reinstalled)"
    say "  Anything else already in that directory is left alone."
    note "replaced files in $INSTALL_DIR"
fi

if [[ "$LOCAL_CHECKOUT" == "1" ]]; then
    step "Copying source from this checkout"
    say "  $SRC_DIR"
    mkdir -p "$INSTALL_DIR"
    [[ "$FRESH_DIR" == "1" ]] && note "created $INSTALL_DIR"
    # Only the files the backend actually runs, mirroring what the tarball ships.
    # Absent ones are skipped rather than fatal: a checkout is allowed to be
    # missing the built icons, and app.py + mobile_app.html were already checked.
    for f in app.py requirements.txt mobile_app.html sw.js vendor \
             icon-192.png icon-512.png pockettui.service install.sh run.sh; do
        [[ -e "$SRC_DIR/$f" ]] || continue
        # Installing from inside the install dir would be cp-onto-itself.
        [[ "$SRC_DIR/$f" -ef "$INSTALL_DIR/$f" ]] && continue
        cp -R "$SRC_DIR/$f" "$INSTALL_DIR/" || die "could not copy $f"
    done
    say "  -> $INSTALL_DIR"
else
    step "Downloading source"
    mkdir -p "$INSTALL_DIR"
    [[ "$FRESH_DIR" == "1" ]] && note "created $INSTALL_DIR"
    # `mktemp -t X.tar.gz` means different things to GNU and BSD mktemp (BSD
    # appends its own suffix to the template, GNU treats the whole thing as one),
    # so name the file in full ourselves and let mktemp fill only the X's.
    TMP_TGZ="$(mktemp "${TMPDIR:-/tmp}/pockettui.XXXXXX")" || die "could not create a temp file"
    trap 'rm -f "$TMP_TGZ"' EXIT
    curl -fsSL "$TARBALL_URL" -o "$TMP_TGZ" || die "could not download $TARBALL_URL"
    tar -xzf "$TMP_TGZ" -C "$INSTALL_DIR" || die "could not extract the tarball"
    say "  -> $INSTALL_DIR"
fi

# ---------------------------------------------------------------------------
# Python environment
# ---------------------------------------------------------------------------
step "Creating virtualenv"

# Where the interpreter ends up, and — when tmux comes from a conda env rather
# than the system — the bin directory the runtime has to have on PATH. start.sh,
# the systemd unit and the launchd agent all need both.
VENV_PY="$INSTALL_DIR/.venv/bin/python"
ENV_KIND="venv"
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
        say "  tmux $("$MAMBA_PREFIX/bin/tmux" -V 2>/dev/null | awk '{print $2}') from conda-forge"
    fi
    note "created a micromamba env at $MAMBA_PREFIX"
    say "  $ENV_KIND"
}

# Build the venv with uv, which brings its own interpreter when the system has
# none good enough. Returns non-zero if uv could not do it.
create_uv_venv() {
    uv venv --python "3.${MIN_PY_MINOR}" "$INSTALL_DIR/.venv" >/dev/null 2>&1 \
        || uv venv "$INSTALL_DIR/.venv" >/dev/null 2>&1
}

# No system tmux is the one problem a venv cannot solve, so it decides the whole
# strategy: only a conda env can supply tmux without root. Everything else is
# the ordinary uv -> venv -> micromamba preference.
if [[ "$HAVE_TMUX" != "1" ]]; then
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
    say "  $INSTALL_DIR/.venv (uv)"
elif usable_python3 && python3 -m venv "$INSTALL_DIR/.venv" 2>/dev/null; then
    say "  $INSTALL_DIR/.venv"
elif command -v micromamba >/dev/null 2>&1 && create_mamba_env 0; then
    # Debian splits venv out of the stdlib, and some distro pythons ship without
    # ensurepip at all. A micromamba env brings its own interpreter.
    say "  no usable python3 venv — falling back to a micromamba env"
    adopt_mamba_env
elif install_uv && create_uv_venv; then
    # tmux is present, so only the Python side is broken and uv is enough to fix
    # it — it downloads a managed interpreter of its own.
    ENV_KIND="venv (uv)"
    say "  $INSTALL_DIR/.venv (uv, with its own Python)"
else
    die "could not build a Python environment. Install Python 3.${MIN_PY_MINOR}+ and re-run:  $(pkg_install_cmd "$(pkg_name venv)")"
fi

step "Installing dependencies (this takes a minute)"
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
say "  fastapi + uvicorn installed"

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
# Service
# ---------------------------------------------------------------------------
UNIT_DIR="$HOME/.config/systemd/user"
UNIT_PATH="$UNIT_DIR/$SERVICE_NAME.service"
SERVICE_INSTALLED=0
HAVE_SYSTEMD=0
if [[ "$(uname -s)" == "Linux" ]] && command -v systemctl >/dev/null 2>&1 \
   && systemctl --user show-environment >/dev/null 2>&1; then
    HAVE_SYSTEMD=1
fi

# macOS has no systemd; the same job — start at login, restart if it dies — is a
# launchd user agent (a LaunchAgent, per-user, no root).
AGENT_LABEL="com.pockettui.server"
AGENT_DIR="$HOME/Library/LaunchAgents"
AGENT_PATH="$AGENT_DIR/$AGENT_LABEL.plist"
HAVE_LAUNCHD=0
if [[ "$(uname -s)" == "Darwin" ]] && command -v launchctl >/dev/null 2>&1; then
    HAVE_LAUNCHD=1
fi

# A service starts with almost no environment, so when tmux lives in the
# install's own conda env the unit has to name that bin dir itself. On a normal
# install ENV_BIN is empty and this line is absent, leaving the unit as it was.
UNIT_ENV_LINE=""
[[ -n "$ENV_BIN" ]] && UNIT_ENV_LINE="Environment=PATH=$ENV_BIN:/usr/local/bin:/usr/bin:/bin"

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
WorkingDirectory=$INSTALL_DIR${UNIT_ENV_LINE:+
$UNIT_ENV_LINE}
ExecStart=$VENV_PY $INSTALL_DIR/app.py --port $PORT

[Install]
WantedBy=default.target
EOF
)"

UNIT_BACKUP=""

# daemon-reload, then enable + start, then report. Shared by the "we wrote the
# unit" and the "the user kept theirs" paths, which differ only in wording.
start_service() {
    local what="$1"
    touched_outside          # enabling a user unit is systemd state, not a file
    systemctl --user daemon-reload
    systemctl --user enable --now "$SERVICE_NAME" >/dev/null 2>&1 || true
    if systemctl --user is-active --quiet "$SERVICE_NAME"; then
        SERVICE_INSTALLED=1
        say "  $SERVICE_NAME running on port $PORT"
        note "$what and started $SERVICE_NAME"
    else
        say "  the service did not start; check:"
        say "    systemctl --user status $SERVICE_NAME"
        note "$what (service not running)"
    fi
}

# The launchd counterpart of UNIT_ENV_LINE: same reason, plist spelling. Empty
# on a normal install, so the agent keeps the shape it had before.
AGENT_ENV_BLOCK=""
if [[ -n "$ENV_BIN" ]]; then
    AGENT_ENV_BLOCK="
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$ENV_BIN:/usr/local/bin:/usr/bin:/bin</string>
    </dict>"
fi

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
    <string>$INSTALL_DIR</string>$AGENT_ENV_BLOCK
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
    if launchctl list 2>/dev/null | grep -q "$AGENT_LABEL"; then
        SERVICE_INSTALLED=1
        say "  $AGENT_LABEL running on port $PORT"
        note "$what and started $AGENT_LABEL"
    else
        say "  the agent did not start; check:"
        say "    launchctl list | grep $AGENT_LABEL"
        say "    $INSTALL_DIR/pockettui.log"
        note "$what (agent not running)"
    fi
}

# Everything above this point stayed inside $INSTALL_DIR. A service touches the
# rest of the system, so it only happens if the user says so out loud.
if [[ "$HAVE_SYSTEMD" == "1" ]]; then
    step "Optional: run automatically in the background"
    say "  This would write $UNIT_PATH,"
    say "  run 'systemctl --user daemon-reload', and enable + start the service."

    # A unit is a whole file: there is no appending to it the way there is with
    # .tmux.conf, so an existing one can only be replaced, left alone, or the
    # step abandoned. Which of those it is has to be answered out loud.
    UNIT_EXISTS=0
    UNIT_SAME=0
    if [[ -e "$UNIT_PATH" ]]; then
        UNIT_EXISTS=1
        [[ "$(cat "$UNIT_PATH")" == "$UNIT_CONTENT" ]] && UNIT_SAME=1
    fi

    if [[ "$UNIT_EXISTS" == "1" ]] && [[ "$UNIT_SAME" == "1" ]]; then
        # Nothing to lose and nothing to decide: the file already says exactly
        # what we would write. Don't ask, don't back up, don't rewrite it.
        say ""
        say "  $UNIT_PATH already exists and is"
        say "  identical to what this installer writes — leaving it as it is."
        if confirm "  Reload and (re)start the service?"; then
            start_service "kept the identical $UNIT_PATH"
        else
            say "  Skipped — nothing outside $INSTALL_DIR was touched."
            note "left $UNIT_PATH alone (already identical)"
        fi
    elif [[ "$UNIT_EXISTS" == "1" ]]; then
        say ""
        say "  NOTE: $UNIT_PATH already exists"
        say "  and differs from what this installer writes. If you customised it"
        say "  (port, Environment=, Restart=), that is your file, not ours."
        if [[ "$INTERACTIVE" != "1" ]]; then
            # Nobody to ask, and the one irreversible option is the overwrite.
            say ""
            say "  Non-interactive (piped) run — your unit is left untouched."
            say "  To replace it with ours, back it up and write it yourself:"
            say ""
            say "      cp $UNIT_PATH $UNIT_PATH.bak"
            say "      \$EDITOR $UNIT_PATH   # see the unit printed below"
            say "      systemctl --user daemon-reload"
            say "      systemctl --user enable --now $SERVICE_NAME"
            note "left the existing $UNIT_PATH alone (non-interactive)"
        elif confirm "  OVERWRITE it with PocketTUI's unit (a backup is kept)?"; then
            # Backup first, and only then write. Seconds-resolution UTC sorts
            # lexically and a second run lands on a different name.
            UNIT_BACKUP="$UNIT_PATH.bak.$(date -u +%Y%m%d-%H%M%S)"
            if ! cp -p "$UNIT_PATH" "$UNIT_BACKUP"; then
                UNIT_BACKUP=""
                die "could not back up $UNIT_PATH — refusing to overwrite it."
            fi
            say "  Backed up your unit to:"
            say "      $UNIT_BACKUP"
            printf '%s\n' "$UNIT_CONTENT" > "$UNIT_PATH"
            note "backed up the old unit to $UNIT_BACKUP"
            start_service "overwrote $UNIT_PATH"
        elif confirm "  Keep your unit as it is and just enable + start it?"; then
            # Their file, their ExecStart — we have not read it and it need not
            # point at this install at all, so starting it is its own question.
            start_service "kept your existing $UNIT_PATH"
        else
            say "  Skipped — $UNIT_PATH was not touched."
            note "skipped service install (declined)"
        fi
    elif [[ "$INTERACTIVE" != "1" ]]; then
        say ""
        say "  Non-interactive (piped) run — skipping. To do it yourself, see below."
        note "skipped service install (non-interactive)"
    elif confirm "  Install and start the systemd user service?"; then
        # Bare `mkdir -p` here would abort the whole script under `set -e` on an
        # unwritable ~/.config, losing the summary and next steps for an install
        # that otherwise succeeded. Report and carry on instead.
        if ! mkdir -p "$UNIT_DIR" 2>/dev/null; then
            say "  WARNING: could not create $UNIT_DIR — leaving it alone."
            say "  PocketTUI works regardless; start it by hand with:"
            say "      $INSTALL_DIR/start.sh"
            note "could not create $UNIT_DIR (no service installed)"
        elif ! printf '%s\n' "$UNIT_CONTENT" > "$UNIT_PATH" 2>/dev/null; then
            say "  WARNING: could not write $UNIT_PATH — leaving it alone."
            say "  PocketTUI works regardless; start it by hand with:"
            say "      $INSTALL_DIR/start.sh"
            note "could not write $UNIT_PATH (no service installed)"
        else
            start_service "installed $UNIT_PATH"
        fi
    else
        say "  Skipped — nothing outside $INSTALL_DIR was touched."
        note "skipped service install (declined)"
    fi
elif [[ "$HAVE_LAUNCHD" == "1" ]]; then
    # Same shape as the systemd branch above, in launchd's spelling.
    step "Optional: run automatically in the background"
    say "  This would write $AGENT_PATH"
    say "  and load it with launchctl, so PocketTUI starts at login."

    AGENT_EXISTS=0
    AGENT_SAME=0
    if [[ -e "$AGENT_PATH" ]]; then
        AGENT_EXISTS=1
        [[ "$(cat "$AGENT_PATH")" == "$AGENT_CONTENT" ]] && AGENT_SAME=1
    fi

    if [[ "$AGENT_EXISTS" == "1" ]] && [[ "$AGENT_SAME" == "1" ]]; then
        say ""
        say "  $AGENT_PATH already exists and is"
        say "  identical to what this installer writes — leaving it as it is."
        if confirm "  Reload and (re)start the agent?"; then
            start_agent "kept the identical $AGENT_PATH"
        else
            say "  Skipped — nothing outside $INSTALL_DIR was touched."
            note "left $AGENT_PATH alone (already identical)"
        fi
    elif [[ "$AGENT_EXISTS" == "1" ]]; then
        say ""
        say "  NOTE: $AGENT_PATH already exists"
        say "  and differs from what this installer writes. If you customised it,"
        say "  that is your file, not ours."
        if [[ "$INTERACTIVE" != "1" ]]; then
            say ""
            say "  Non-interactive (piped) run — your agent is left untouched."
            say "  To replace it with ours, back it up and write it yourself:"
            say ""
            say "      cp $AGENT_PATH $AGENT_PATH.bak"
            say "      \$EDITOR $AGENT_PATH"
            say "      launchctl bootout gui/\$(id -u)/$AGENT_LABEL"
            say "      launchctl bootstrap gui/\$(id -u) $AGENT_PATH"
            note "left the existing $AGENT_PATH alone (non-interactive)"
        elif confirm "  OVERWRITE it with PocketTUI's agent (a backup is kept)?"; then
            AGENT_BACKUP="$AGENT_PATH.bak.$(date -u +%Y%m%d-%H%M%S)"
            if ! cp -p "$AGENT_PATH" "$AGENT_BACKUP"; then
                AGENT_BACKUP=""
                die "could not back up $AGENT_PATH — refusing to overwrite it."
            fi
            say "  Backed up your agent to:"
            say "      $AGENT_BACKUP"
            # Unload the old one before its file is replaced, so launchd is not
            # left holding a definition that no longer exists on disk.
            launchctl bootout "gui/$(id -u)/$AGENT_LABEL" >/dev/null 2>&1 \
                || launchctl unload -w "$AGENT_PATH" >/dev/null 2>&1 || true
            printf '%s\n' "$AGENT_CONTENT" > "$AGENT_PATH"
            note "backed up the old agent to $AGENT_BACKUP"
            start_agent "overwrote $AGENT_PATH"
        elif confirm "  Keep your agent as it is and just load it?"; then
            start_agent "kept your existing $AGENT_PATH"
        else
            say "  Skipped — $AGENT_PATH was not touched."
            note "skipped agent install (declined)"
        fi
    elif [[ "$INTERACTIVE" != "1" ]]; then
        say ""
        say "  Non-interactive (piped) run — skipping. To do it yourself, see below."
        note "skipped agent install (non-interactive)"
    elif confirm "  Install and start the launchd user agent?"; then
        if ! mkdir -p "$AGENT_DIR" 2>/dev/null; then
            say "  WARNING: could not create $AGENT_DIR — leaving it alone."
            say "  PocketTUI works regardless; start it by hand with:"
            say "      $INSTALL_DIR/start.sh"
            note "could not create $AGENT_DIR (no agent installed)"
        elif ! printf '%s\n' "$AGENT_CONTENT" > "$AGENT_PATH" 2>/dev/null; then
            say "  WARNING: could not write $AGENT_PATH — leaving it alone."
            say "  PocketTUI works regardless; start it by hand with:"
            say "      $INSTALL_DIR/start.sh"
            note "could not write $AGENT_PATH (no agent installed)"
        else
            start_agent "installed $AGENT_PATH"
        fi
    else
        say "  Skipped — nothing outside $INSTALL_DIR was touched."
        note "skipped agent install (declined)"
    fi
else
    step "No systemd user session — no service installed"
    note "skipped service install (no systemd user session)"
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
if command -v tmux >/dev/null 2>&1; then
    # An already-present uncommented copy means there is nothing to do; a
    # commented-out one does not count. Leading whitespace and any internal
    # spacing are tolerated so re-runs never stack duplicates.
    if [[ -e "$TMUX_CONF" ]] \
       && grep -Eq '^[[:space:]]*set-environment[[:space:]]+-gu[[:space:]]+ZDOTDIR[[:space:]]*$' "$TMUX_CONF"; then
        step "tmux config already has the ZDOTDIR line"
        say "  $TMUX_CONF is fine as it is — nothing to do."
    else
        step "Optional: keep new tmux sessions out of a stale ZDOTDIR"
        say "  This would append one line to $TMUX_CONF:"
        say ""
        say "      $TMUX_LINE"
        say ""
        say "  It only affects tmux sessions you start yourself; PocketTUI's own"
        say "  sessions already set ZDOTDIR=\$HOME."
        if [[ "$INTERACTIVE" != "1" ]]; then
            say ""
            say "  Non-interactive (piped) run — skipping. Add that line yourself if"
            say "  your tmux sessions come up without your zsh config."
            note "skipped tmux.conf line (non-interactive)"
        elif confirm "  Append that line to $TMUX_CONF?"; then
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
                touched_outside
                note "appended the ZDOTDIR line to $TMUX_CONF"
            else
                say "  WARNING: $TMUX_CONF is not writable — leaving it alone."
                say "  PocketTUI works regardless; add this line by hand if you want it:"
                say "      $TMUX_LINE"
                note "could not write $TMUX_CONF (left alone)"
            fi
        else
            say "  Skipped — $TMUX_CONF was not touched. To do it yourself, add:"
            say "      $TMUX_LINE"
            note "skipped tmux.conf line (declined)"
        fi
    fi
fi

# ---------------------------------------------------------------------------
# Next steps
# ---------------------------------------------------------------------------
cat <<EOF

============================================================
Installed to $INSTALL_DIR

What this script changed:
EOF
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
# Only true when nothing outside $INSTALL_DIR was touched. DID cannot answer
# this: it also holds install-dir work and steps the user declined.
[[ "$OUTSIDE" -eq 0 ]] && say "  Nothing else on this machine was modified."
say ""

if [[ -n "$UNIT_BACKUP" ]]; then
    say "Your previous systemd unit is saved at:"
    say "    $UNIT_BACKUP"
    say "To put it back:  cp $UNIT_BACKUP $UNIT_PATH"
    say "                 systemctl --user daemon-reload"
    say ""
fi

if [[ -n "$AGENT_BACKUP" ]]; then
    say "Your previous launchd agent is saved at:"
    say "    $AGENT_BACKUP"
    say "To put it back:  cp $AGENT_BACKUP $AGENT_PATH"
    say "                 launchctl bootout gui/\$(id -u)/$AGENT_LABEL"
    say "                 launchctl bootstrap gui/\$(id -u) $AGENT_PATH"
    say ""
fi

say "NEXT STEPS"
say ""

# Step 1 depends on whether the service is already running the backend.
if [[ "$SERVICE_INSTALLED" == "1" ]] && [[ "$HAVE_SYSTEMD" == "1" ]]; then
    cat <<EOF
1. The backend is already running (systemd unit $SERVICE_NAME) and will
   come back on boot. Check it any time with:

       systemctl --user status $SERVICE_NAME

   A user service only survives logout if lingering is on:

       loginctl enable-linger $USER

EOF
elif [[ "$SERVICE_INSTALLED" == "1" ]]; then
    cat <<EOF
1. The backend is already running (launchd agent $AGENT_LABEL) and will
   come back at login. Check it any time with:

       launchctl list | grep $AGENT_LABEL
       tail -f $INSTALL_DIR/pockettui.log

   To stop it:

       launchctl bootout gui/\$(id -u)/$AGENT_LABEL

EOF
else
    cat <<EOF
1. Start the backend:

       $INSTALL_DIR/start.sh

   Leave that running. (To run it in the background instead, re-run this
   installer from a terminal and say yes to the service step.)

EOF
fi

cat <<EOF
2. Expose it on your tailnet:

       tailscale serve --bg --set-path /pockettui $PORT

   - needs HTTPS certificates enabled once in the Tailscale admin console
     (admin console > DNS > HTTPS Certificates)
   - find your hostname with:  tailscale serve status
     (looks like https://<machine>.<tailnet>.ts.net)

3. On your phone (Tailscale connected) open:

       $BASE_URL/app/

   and when asked, enter:

       https://<machine>.<tailnet>.ts.net/pockettui

   (alternative if you skip path-serving: tailscale serve --bg $PORT,
   then enter the hostname and put $PORT in the port field)

4. In Safari: Share -> Add to Home Screen to install it as an app.

============================================================
EOF
