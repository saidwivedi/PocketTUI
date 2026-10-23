#!/bin/bash
# Install the PocketTUI desktop app on this machine.
#
#   curl -fsSL https://pockettui.com/desktop.sh | bash
#
# macOS: puts PocketTUI.app in /Applications (or ~/Applications when that is not
# writable) and opens it. The app is not notarized, so a dmg downloaded in a
# browser is quarantined and Gatekeeper refuses to open it; a file fetched with
# curl carries no quarantine attribute, which is why this script exists.
#
# Linux: puts the AppImage at ~/.local/bin/PocketTUI.AppImage and adds a menu
# entry. No sudo, so the .deb is left to whoever wants it.
#
# Running it again updates the app in place. This is the desktop window only;
# the backend on the computer you connect to is install.sh.
#
# Environment:
#   POCKETTUI_DESKTOP_URL   where the release files live
#                           (default: the latest GitHub release)
#   POCKETTUI_DESKTOP_ARCH  arm64 or x86_64, instead of what uname -m says
#   POCKETTUI_ICON_URL      the menu icon on Linux (default: pockettui.com/app/icon-512.png)
#   POCKETTUI_APPS_DIR      where PocketTUI.app goes on macOS (default: /Applications)
#   POCKETTUI_NO_OPEN       1 to install without opening the app (same as --no-open)

set -eu

DESKTOP_URL="${POCKETTUI_DESKTOP_URL:-https://github.com/saidwivedi/pockettui/releases/latest/download}"
ICON_URL="${POCKETTUI_ICON_URL:-https://pockettui.com/app/icon-512.png}"
APPS_DIR="${POCKETTUI_APPS_DIR:-/Applications}"
NO_OPEN="${POCKETTUI_NO_OPEN:-0}"
# Test-only: lets the macOS path run on a Linux test box. Not for users.
OS="${POCKETTUI_DESKTOP_OS:-$(uname -s)}"

usage() {
    cat <<'EOF'
Install or update the PocketTUI desktop app.

  curl -fsSL https://pockettui.com/desktop.sh | bash
  curl -fsSL https://pockettui.com/desktop.sh | bash -s -- --no-open

macOS: PocketTUI.app in /Applications (or ~/Applications), then opened.
Linux: ~/.local/bin/PocketTUI.AppImage plus an app menu entry.

Options:
  --no-open   install without opening the app (POCKETTUI_NO_OPEN=1)
  -h, --help  show this help
EOF
}

for arg in ${@+"$@"}; do
    case "$arg" in
        -h|--help) usage; exit 0 ;;
        --no-open) NO_OPEN=1 ;;
        *) printf 'ERROR: unknown option %s (see --help)\n' "$arg" >&2; exit 1 ;;
    esac
done

# Same colour rules as install.sh: off for a pipe, NO_COLOR and TERM=dumb.
C_RESET=""; C_STEP=""; C_OK=""; C_WARN=""; C_DIM=""
if [[ -t 1 ]] && [[ -z "${NO_COLOR:-}" ]] && [[ "${TERM:-}" != "dumb" ]]; then
    C_RESET=$'\033[0m'
    C_STEP=$'\033[36m'
    C_OK=$'\033[32m'
    C_WARN=$'\033[33m'
    C_DIM=$'\033[2m'
fi

say()  { printf '%s\n' "$*"; }
warn() { printf '%s  %s%s\n' "$C_WARN" "$*" "$C_RESET"; }
die()  { printf '%sERROR: %s%s\n' "$C_WARN" "$*" "$C_RESET" >&2; exit 1; }

# One step, one line, status on the right, as in install.sh.
STEP_OPEN=""
STEP_WIDTH=34
step() { STEP_OPEN="$*"; }
step_done() {
    local status="$1" colour="${2:-$C_OK}" pad="" plain n
    plain="==> $STEP_OPEN"
    n=$(( STEP_WIDTH - ${#plain} ))
    [[ "$n" -lt 1 ]] && n=1
    while [[ "$n" -gt 0 ]]; do pad="$pad "; n=$((n - 1)); done
    printf '%s==>%s %s%s%s%s%s\n' \
        "$C_STEP" "$C_RESET" "$STEP_OPEN" "$pad" "$colour" "$status" "$C_RESET"
    STEP_OPEN=""
}

command -v curl >/dev/null 2>&1 || die "curl not found."

# Under Rosetta a Terminal on Apple Silicon reports x86_64; the hardware flag
# says what the machine really is, and the arm64 build is the one to run there.
ARCH="${POCKETTUI_DESKTOP_ARCH:-}"
if [[ -z "$ARCH" ]]; then
    ARCH="$(uname -m)"
    if [[ "$OS" == "Darwin" ]] && [[ "$ARCH" == "x86_64" ]] \
       && [[ "$(sysctl -n hw.optional.arm64 2>/dev/null || true)" == "1" ]]; then
        ARCH="arm64"
    fi
fi

case "$OS:$ARCH" in
    Darwin:arm64|Darwin:aarch64) ASSET="PocketTUI-mac-arm64.zip" ;;
    Darwin:x86_64)               ASSET="PocketTUI-mac-x64.zip" ;;
    Linux:x86_64|Linux:amd64)    ASSET="PocketTUI-linux-x86_64.AppImage" ;;
    Linux:aarch64|Linux:arm64)   ASSET="PocketTUI-linux-arm64.AppImage" ;;
    Darwin:*|Linux:*)
        die "there is no desktop app for $OS on $ARCH. Use the browser instead: https://pockettui.com/app/" ;;
    *)
        say "There is no PocketTUI desktop app for $OS."
        say "The browser is the client there: open https://pockettui.com/app/"
        exit 1 ;;
esac

TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/pockettui-desktop.XXXXXX")" || die "could not create a temp directory"
trap 'rm -rf "$TMP_DIR"' EXIT

# A progress bar on a terminal (stderr, so it shows under curl | bash too),
# nothing in a log.
CURL_SHOW="-s"
[[ -t 2 ]] && CURL_SHOW="--progress-bar"

say "${C_STEP}==>${C_RESET} Downloading $ASSET"
curl -fL $CURL_SHOW "$DESKTOP_URL/$ASSET" -o "$TMP_DIR/$ASSET" \
    || die "could not download $DESKTOP_URL/$ASSET"

# ---------------------------------------------------------------------------
# macOS
# ---------------------------------------------------------------------------
if [[ "$OS" == "Darwin" ]]; then
    step "Unpacking PocketTUI.app"
    mkdir -p "$TMP_DIR/x"
    # ditto keeps the bundle's symlinks, signature and extended attributes the
    # way Finder would; unzip is only the fallback for a system without it.
    if command -v ditto >/dev/null 2>&1; then
        ditto -xk "$TMP_DIR/$ASSET" "$TMP_DIR/x" || die "could not unpack $ASSET"
    elif command -v unzip >/dev/null 2>&1; then
        unzip -q "$TMP_DIR/$ASSET" -d "$TMP_DIR/x" || die "could not unpack $ASSET"
    else
        die "neither ditto nor unzip is available to unpack $ASSET"
    fi
    [[ -d "$TMP_DIR/x/PocketTUI.app" ]] || die "$ASSET did not contain PocketTUI.app"
    step_done "ok"

    step "Installing"
    FELL_BACK=0
    if ! { [[ -d "$APPS_DIR" ]] && [[ -w "$APPS_DIR" ]]; }; then
        FELL_BACK=1
        FIRST_CHOICE="$APPS_DIR"
        APPS_DIR="$HOME/Applications"
        mkdir -p "$APPS_DIR" || die "could not create $APPS_DIR"
    fi
    APP_PATH="$APPS_DIR/PocketTUI.app"
    # Staged next to the target first, so the old bundle is removed only once
    # the new one is complete on the same volume, and the swap is one rename.
    STAGE="$APPS_DIR/.PocketTUI.app.new"
    rm -rf "$STAGE"
    mv "$TMP_DIR/x/PocketTUI.app" "$STAGE" || { rm -rf "$STAGE"; die "could not copy PocketTUI.app into $APPS_DIR"; }
    UPDATED=0
    if [[ -e "$APP_PATH" ]]; then
        UPDATED=1
        rm -rf "$APP_PATH" || { rm -rf "$STAGE"; die "could not remove the old $APP_PATH"; }
    fi
    mv "$STAGE" "$APP_PATH" || die "could not move PocketTUI.app into $APPS_DIR"
    # curl sets no quarantine flag, so this normally finds nothing to remove.
    if command -v xattr >/dev/null 2>&1; then
        xattr -dr com.apple.quarantine "$APP_PATH" 2>/dev/null || true
    fi
    if [[ "$UPDATED" == "1" ]]; then
        step_done "updated"
    else
        step_done "ok"
    fi

    say ""
    say "  PocketTUI.app is in $APPS_DIR."
    if [[ "$FELL_BACK" == "1" ]]; then
        say "  ${C_DIM}($FIRST_CHOICE is not writable for this user, so it went to"
        say "  $APPS_DIR instead.)$C_RESET"
    fi
    # Without this, open would only bring the old, still running copy forward.
    if [[ "$UPDATED" == "1" ]] && pgrep -x PocketTUI >/dev/null 2>&1; then
        say "  PocketTUI is running; quit it and open it again to use the new version."
    elif [[ "$NO_OPEN" != "1" ]] && [[ -t 1 ]]; then
        open -a "$APP_PATH" || warn "could not open $APP_PATH; open it from Finder."
    fi
    say ""
    exit 0
fi

# ---------------------------------------------------------------------------
# Linux
# ---------------------------------------------------------------------------
BIN_DIR="$HOME/.local/bin"
APPIMAGE="$BIN_DIR/PocketTUI.AppImage"
ICON_DIR="$HOME/.local/share/icons/hicolor/512x512/apps"
APPS_MENU="$HOME/.local/share/applications"

step "Installing"
mkdir -p "$BIN_DIR" || die "could not create $BIN_DIR"
chmod +x "$TMP_DIR/$ASSET"
UPDATED=0
[[ -e "$APPIMAGE" ]] && UPDATED=1
# A rename over the old file: a copy that is running keeps its inode.
mv -f "$TMP_DIR/$ASSET" "$APPIMAGE" || die "could not write $APPIMAGE"
if [[ "$UPDATED" == "1" ]]; then
    step_done "updated"
else
    step_done "ok"
fi

step "App menu entry"
mkdir -p "$ICON_DIR" "$APPS_MENU" || die "could not create the menu directories under $HOME/.local/share"
ICON_OK=1
if curl -fsSL --max-time 30 "$ICON_URL" -o "$TMP_DIR/icon.png"; then
    mv -f "$TMP_DIR/icon.png" "$ICON_DIR/pockettui.png"
else
    ICON_OK=0
fi
cat > "$APPS_MENU/pockettui.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=PocketTUI
Comment=PocketTUI desktop app
Exec="$APPIMAGE" %U
TryExec=$APPIMAGE
Icon=pockettui
Terminal=false
Categories=Utility;Development;
EOF
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$APPS_MENU" >/dev/null 2>&1 || true
fi
if [[ "$ICON_OK" == "1" ]]; then
    step_done "ok"
else
    step_done "no icon" "$C_WARN"
    warn "could not fetch $ICON_URL; the menu entry has no icon until the next run."
fi

say ""
say "  PocketTUI is installed at $APPIMAGE."
say "  Start it from your app menu, or run:"
say "      $APPIMAGE"
# AppImages mount themselves through FUSE 2, which newer distributions no
# longer install by default; without it the app does not start at all.
LDCONFIG="$(command -v ldconfig 2>/dev/null || true)"
[[ -z "$LDCONFIG" ]] && [[ -x /sbin/ldconfig ]] && LDCONFIG=/sbin/ldconfig
if [[ -n "$LDCONFIG" ]] && ! "$LDCONFIG" -p 2>/dev/null | grep -q 'libfuse\.so\.2'; then
    say ""
    warn "FUSE 2 (libfuse.so.2), which AppImages need, is not installed. Install your"
    warn "distribution's libfuse2 package, or run: $APPIMAGE --appimage-extract-and-run"
fi
say ""
