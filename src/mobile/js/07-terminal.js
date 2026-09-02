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

// One route to the clipboard for both desktop copy paths. writeText is the real
// one; where it is missing, xterm's own hidden textarea is borrowed for a legacy
// execCommand copy — the same field its native copy handler seeds — and then put
// back as it was, since anything left sitting in there can be emitted as input
// later.
function writeClipboard(text) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    return navigator.clipboard.writeText(text);
  }
  const ta = term && term.textarea;
  if (!ta) return Promise.reject(new Error("no clipboard"));
  const keep = ta.value;
  ta.value = text;
  ta.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch (e) {}
  ta.value = keep;
  return ok ? Promise.resolve() : Promise.reject(new Error("copy refused"));
}

// Ctrl/Cmd+Shift+C copies the selection outright. Off a Mac, Ctrl+C copies it
// too and then drops it, the way VS Code's terminal does, so the very next
// Ctrl+C is the SIGINT it has always been; with nothing selected neither key is
// anything but what it was. A Mac has Cmd+C for that, which the browser's own
// copy event already answers, and keeps Ctrl+C as the interrupt.
//
// The write happens inside the keydown, which is the user gesture the clipboard
// wants, and false is returned for keypress and keyup as well so xterm cannot
// send its own ETX on top.
function copyKeyBinding(ev) {
  const isC = ev.key === "c" || ev.key === "C";
  const explicit = isC && ev.shiftKey && (ev.ctrlKey || ev.metaKey) && !ev.altKey;
  const interrupt = isC && ev.ctrlKey && !ev.shiftKey && !ev.altKey && !ev.metaKey &&
    !/Mac/.test(navigator.userAgent);
  if (!explicit && !interrupt) return true;
  if (!term || !term.hasSelection()) return true;
  if (ev.type === "keydown") {
    ev.preventDefault();
    const text = term.getSelection();
    if (interrupt) term.clearSelection();
    writeClipboard(text).then(() => toast("Copied")).catch(() => toast("Clipboard blocked"));
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
    // tmux owns the mouse, so a drag is a mouse report unless a modifier holds
    // it back: Shift elsewhere, Option on a Mac -- and only if this is on.
    macOptionClickForcesSelection: true,
  });
  fitAddon = new FitAddon.FitAddon();
  term.loadAddon(fitAddon);
  term.open($("term-host"));
  useWebgl();
  term.onData(d => send(d));
  // Both handlers have to agree before xterm sees the key: the first one to
  // claim it returns false and the chain stops there.
  term.attachCustomKeyEventHandler(ev => copyKeyBinding(ev) && shiftEnterIsNewline(ev));
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

// ============================================================
// Mouse copy
// ============================================================
// tmux runs with its own mouse mode on, so xterm reports every click to it and
// only a Shift+drag makes a selection at all. Every one of those reports also
// counts as user input, and xterm answers user input by dropping the selection —
// which is what left the OS clipboard holding whatever was on it before.
(function mouseCopy() {
  const host = $("term-host");

  // Select-to-copy, the way X11 terminals have always done it: the text is on
  // the clipboard the moment the mouse lets go of it, before anything else can
  // clear the selection. Pointer events are the discriminator — the long-press
  // gesture selects through the same terminal and does its own copy from the
  // pill, and its pointers are touch ones.
  let fromMouse = false;
  host.addEventListener("pointerdown", (e) => { fromMouse = e.pointerType === "mouse"; }, true);
  document.addEventListener("pointerup", () => {
    if (!fromMouse) return;
    fromMouse = false;
    if (!term || !term.hasSelection()) return;
    // Nothing was asked for out loud, so a browser that withholds the clipboard
    // here says nothing either.
    writeClipboard(term.getSelection()).catch(() => {});
  });

  // A right-click is a mouse report like any other, so the selection is gone
  // before xterm's own right-click handler can seed the hidden textarea from it,
  // and the native Copy in the context menu comes up empty. Keep the report to
  // ourselves whenever there is a selection to protect, and seed the textarea
  // here instead — the menu then opens on the selected text. Only mousedown is
  // stopped: contextmenu still fires, so xterm's handler does the same thing
  // again on the browsers that use that route.
  host.addEventListener("mousedown", (e) => {
    if (e.button !== 2 || !term || !term.textarea || !term.hasSelection()) return;
    e.stopPropagation();
    const ta = term.textarea;
    const box = (host.querySelector(".xterm-screen") || host).getBoundingClientRect();
    ta.style.width = "20px";
    ta.style.height = "20px";
    ta.style.left = (e.clientX - box.left - 10) + "px";
    ta.style.top = (e.clientY - box.top - 10) + "px";
    ta.style.zIndex = "1000";
    ta.focus();
    ta.value = term.getSelection();
    ta.select();
  }, true);
})();

