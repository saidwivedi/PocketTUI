// ============================================================
// Terminal
// ============================================================
// Two ramps so the terminal follows the app theme instead of sitting as a dark
// slab inside a paper shell. Swappable as whole objects — the terminal reads one
// at construction and takes the other on a live theme toggle.
const TERM_THEME_DARK = {
  background: "#16140f",
  foreground: "#f3ede0",
  cursor: "#d4865a",
  cursorAccent: "#16140f",
  selectionBackground: "rgba(212, 134, 90, 0.32)",
  selectionForeground: "#f3ede0",
  black:         "#2e2a21",
  red:           "#c46a5e",
  green:         "#7a8e6a",
  yellow:        "#c98a5a",
  blue:          "#6884a0",
  magenta:       "#8a7ab0",
  cyan:          "#6a9aa6",
  white:         "#d8d1c0",
  brightBlack:   "#5a5347",
  brightRed:     "#d88276",
  brightGreen:   "#95a882",
  brightYellow:  "#e0a473",
  brightBlue:    "#83a0bc",
  brightMagenta: "#a596c9",
  brightCyan:    "#85b5c1",
  brightWhite:   "#f3ede0",
};

// Light ramp is cmux's "Apple System Colors Light" verbatim — the system hues
// are what macOS terminals paint, so anything already tuned for them reads right.
const TERM_THEME_LIGHT = {
  background: "#feffff",
  foreground: "#000000",
  cursor: "#98989d",
  cursorAccent: "#ffffff",
  selectionBackground: "#abd8ff",
  selectionForeground: "#000000",
  black:         "#1a1a1a",
  red:           "#cc372e",
  green:         "#26a439",
  yellow:        "#cdac08",
  blue:          "#0869cb",
  magenta:       "#9647bf",
  cyan:          "#479ec2",
  white:         "#98989d",
  brightBlack:   "#464646",
  brightRed:     "#ff453a",
  brightGreen:   "#32d74b",
  brightYellow:  "#e5bc00",
  brightBlue:    "#0a84ff",
  brightMagenta: "#bf5af2",
  brightCyan:    "#69c9f2",
  brightWhite:   "#ffffff",
};

function resolvedDark() {
  return document.documentElement.getAttribute("data-theme") === "dark";
}
function currentTermTheme() {
  return resolvedDark() ? TERM_THEME_DARK : TERM_THEME_LIGHT;
}

// The fixed terminal screen does not reliably paint the iOS safe-area strip, so
// the root background has to match whatever screen is showing. Same value drives
// the status-bar tint.
function syncChrome() {
  const open = $("screen-term").classList.contains("active");
  const termBg = currentTermTheme().background;
  const bg = open ? termBg : (resolvedDark() ? "#16140f" : "#FAF8F3");
  document.documentElement.style.setProperty("--term-bg", termBg);
  // background-color only — the shorthand would drop body's paper-grain image.
  document.documentElement.style.backgroundColor = bg;
  document.body.style.backgroundColor = bg;
  document.documentElement.classList.toggle("term-open", open);
  document.body.classList.toggle("term-open", open);
  $("meta-theme-color").content = bg;
}

let term = null, fitAddon = null, sock = null;
let currentSession = null, retries = 0, retryTimer = null;

// Pinch-to-zoom sets this and it survives the session. Bounds are the same ones
// the gesture clamps to, applied again on read so a hand-edited or stale value
// can never open the terminal at an unusable size.
const FONT_MIN = 8, FONT_MAX = 24, FONT_DEFAULT = 13;
function storedFontSize() {
  try {
    const v = parseInt(localStorage.getItem("pockettui_fontsize"), 10);
    if (Number.isFinite(v)) return Math.min(FONT_MAX, Math.max(FONT_MIN, v));
  } catch (e) {}
  return FONT_DEFAULT;
}

// The one way a chosen size reaches the live terminal: onto xterm, then a
// forced refit so the grid and the backend end up agreeing with what is
// rendered, then remembered. The pinch settles through here and the shortcuts
// sheet's stepper presses through here, so the touch and the keyboard route to
// the same size can never drift apart. Returns what was actually applied — the
// clamp is here rather than at each caller.
function applyFontSize(px) {
  const size = Math.min(FONT_MAX, Math.max(FONT_MIN, Math.round(px)));
  if (term) term.options.fontSize = size;
  refit(0);
  try { localStorage.setItem("pockettui_fontsize", String(size)); } catch (e) {}
  return size;
}

// Shift+Enter sends a bare line feed instead of xterm's carriage return. The
// agent TUIs (Claude Code and friends) bind \n to "newline, don't submit" out of
// the box, and a lone \n crosses tmux untouched — whereas the kitty encoding
// \x1b[13;2u would be read as literal text, since nothing in this stack ever
// negotiates that protocol.
//
// preventDefault stops the browser writing the line break into xterm's hidden
// textarea, and the false is returned for keypress and keyup as well as keydown:
// a keydown the handler refuses leaves _keyDownHandled false, so xterm's
// keypress path would still see charCode 13 and send its own \r on top of ours.
function shiftEnterIsNewline(ev) {
  if (ev.key !== "Enter" || !ev.shiftKey) return true;
  if (ev.ctrlKey || ev.altKey || ev.metaKey) return true;
  if (ev.type === "keydown") {
    ev.preventDefault();
    send("\n");
  }
  return false;
}

function ensureTerm() {
  if (term) return;
  term = new Terminal({
    fontFamily: 'Menlo, ui-monospace, "SF Mono", monospace',
    fontSize: storedFontSize(),
    letterSpacing: 0,
    lineHeight: 1.15,
    cursorBlink: true,
    scrollback: 2000,
    theme: currentTermTheme(),
    allowProposedApi: true,
  });
  fitAddon = new FitAddon.FitAddon();
  term.loadAddon(fitAddon);
  term.open($("term-host"));
  useWebgl();
  term.onData(d => send(d));
  term.attachCustomKeyEventHandler(shiftEnterIsNewline);
  term.registerLinkProvider({ provideLinks: provideImageLinks });
  // A selection can go away without the gesture asking — a reset, or xterm
  // dropping it on a repaint — and the Copy pill must not outlive it.
  term.onSelectionChange(() => termSelectionCleared());
}

// The GPU renderer, which is what makes a flick scroll smoothly on a phone. It
// is strictly an upgrade: anything that goes wrong — no WebGL2, a blocked
// context, a driver that drops the context later when the tab is backgrounded —
// disposes the addon, and xterm falls back to the DOM renderer it opened with.
function useWebgl() {
  if (typeof WebglAddon === "undefined") return;
  try {
    const addon = new WebglAddon.WebglAddon();
    addon.onContextLoss(() => { try { addon.dispose(); } catch (e) {} });
    term.loadAddon(addon);
  } catch (e) {}
}

