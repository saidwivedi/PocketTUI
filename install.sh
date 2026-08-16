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
MIN_PY_MINOR=9

say()  { printf '%s\n' "$*"; }
step() { printf '\n==> %s\n' "$*"; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

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

command -v python3 >/dev/null 2>&1 || die \
    "python3 not found. Install Python ${MIN_PY_MINOR}+ and re-run (e.g. apt install python3 python3-venv)."

PY_VER="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
PY_MAJOR="${PY_VER%%.*}"
PY_MINOR="${PY_VER##*.}"
if [[ "$PY_MAJOR" -lt 3 ]] || { [[ "$PY_MAJOR" -eq 3 ]] && [[ "$PY_MINOR" -lt "$MIN_PY_MINOR" ]]; }; then
    die "Python 3.${MIN_PY_MINOR}+ required, found $PY_VER."
fi
say "  python3 $PY_VER"

command -v tmux >/dev/null 2>&1 || die \
    "tmux not found. Install it and re-run (e.g. apt install tmux)."
say "  tmux $(tmux -V | awk '{print $2}')"

# The backend only needs to be reachable from the phone; Tailscale is the
# easiest way to do that, but it is not this script's job to require it.
if command -v tailscale >/dev/null 2>&1; then
    say "  tailscale $(tailscale version 2>/dev/null | head -1)"
else
    say "  tailscale not found — you will need some other way to reach this"
    say "    machine from your phone (see the notes at the end)."
fi

command -v curl >/dev/null 2>&1 || die "curl not found."

# ---------------------------------------------------------------------------
# Install directory
# ---------------------------------------------------------------------------
FRESH_DIR=1
if [[ -e "$INSTALL_DIR" ]]; then
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

step "Downloading source"
mkdir -p "$INSTALL_DIR"
[[ "$FRESH_DIR" == "1" ]] && note "created $INSTALL_DIR"
TMP_TGZ="$(mktemp -t pockettui.XXXXXX.tar.gz)"
trap 'rm -f "$TMP_TGZ"' EXIT
curl -fsSL "$TARBALL_URL" -o "$TMP_TGZ" || die "could not download $TARBALL_URL"
tar -xzf "$TMP_TGZ" -C "$INSTALL_DIR" || die "could not extract the tarball"
say "  -> $INSTALL_DIR"

# ---------------------------------------------------------------------------
# Python environment
# ---------------------------------------------------------------------------
step "Creating virtualenv"
if ! python3 -m venv "$INSTALL_DIR/.venv" 2>/dev/null; then
    die "python3 -m venv failed. On Debian/Ubuntu: apt install python3-venv"
fi

step "Installing dependencies (this takes a minute)"
if ! "$INSTALL_DIR/.venv/bin/pip" install --quiet --upgrade pip; then
    die "could not upgrade pip inside the venv"
fi
if ! "$INSTALL_DIR/.venv/bin/pip" install --quiet fastapi 'uvicorn[standard]'; then
    die "could not install fastapi/uvicorn. Check your network and try again."
fi
say "  fastapi + uvicorn installed"

# ---------------------------------------------------------------------------
# Run script
# ---------------------------------------------------------------------------
cat > "$INSTALL_DIR/start.sh" <<EOF
#!/bin/bash
# Run the PocketTUI backend (generated by install.sh).
cd "\$(dirname "\$0")"
exec .venv/bin/python app.py --port $PORT "\$@"
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
ExecStart=$INSTALL_DIR/.venv/bin/python $INSTALL_DIR/app.py --port $PORT

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
say "  - created $INSTALL_DIR/.venv (fastapi, uvicorn)"
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

say "NEXT STEPS"
say ""

# Step 1 depends on whether the service is already running the backend.
if [[ "$SERVICE_INSTALLED" == "1" ]]; then
    cat <<EOF
1. The backend is already running (systemd unit $SERVICE_NAME) and will
   come back on boot. Check it any time with:

       systemctl --user status $SERVICE_NAME

   A user service only survives logout if lingering is on:

       loginctl enable-linger $USER

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
