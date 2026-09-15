// ============================================================
// Appearance
// ============================================================
// Two settings that both end up as colours on the terminal, and they are not
// the same question. The app theme is this shell's own light/dark chrome; a
// palette is the 16 ANSI slots plus a background, which is what a prompt,
// LS_COLORS or a tmux status line was tuned against on the computer. Someone
// running Gruvbox everywhere wants Gruvbox here whichever way the phone's
// chrome is set, so picking a palette never touches data-theme.
//
// The schemes below are transcribed from mbadolato/iTerm2-Color-Schemes'
// windowsterminal/*.json (its purple/brightPurple are xterm's magenta and
// brightMagenta), generated rather than typed, so the hues are the ones their
// authors published.
//
// One departure from the source files: selection is that scheme's selection
// colour at 0.3 alpha instead of the opaque hex. Windows Terminal composites a
// selection over the text; xterm paints it behind, so an opaque selection out
// of one of these — several are the foreground colour itself — swallows the
// line it is highlighting.
const TERM_PRESETS = [
  { id: "solarized-dark", name: "Solarized Dark", theme: {
    background: "#002b36",
    foreground: "#839496",
    cursor: "#839496",
    cursorAccent: "#002b36",
    selectionBackground: "rgba(7, 54, 66, 0.3)",
    black:         "#073642",
    red:           "#dc322f",
    green:         "#859900",
    yellow:        "#b58900",
    blue:          "#268bd2",
    magenta:       "#d33682",
    cyan:          "#2aa198",
    white:         "#eee8d5",
    brightBlack:   "#335e69",
    brightRed:     "#cb4b16",
    brightGreen:   "#586e75",
    brightYellow:  "#657b83",
    brightBlue:    "#839496",
    brightMagenta: "#6c71c4",
    brightCyan:    "#93a1a1",
    brightWhite:   "#fdf6e3",
  } },
  { id: "solarized-light", name: "Solarized Light", theme: {
    background: "#fdf6e3",
    foreground: "#657b83",
    cursor: "#657b83",
    cursorAccent: "#fdf6e3",
    selectionBackground: "rgba(238, 232, 213, 0.3)",
    black:         "#073642",
    red:           "#dc322f",
    green:         "#859900",
    yellow:        "#b58900",
    blue:          "#268bd2",
    magenta:       "#d33682",
    cyan:          "#2aa198",
    white:         "#bbb5a2",
    brightBlack:   "#002b36",
    brightRed:     "#cb4b16",
    brightGreen:   "#586e75",
    brightYellow:  "#657b83",
    brightBlue:    "#839496",
    brightMagenta: "#6c71c4",
    brightCyan:    "#93a1a1",
    brightWhite:   "#fdf6e3",
  } },
  { id: "gruvbox-dark", name: "Gruvbox Dark", theme: {
    background: "#282828",
    foreground: "#ebdbb2",
    cursor: "#ebdbb2",
    cursorAccent: "#282828",
    selectionBackground: "rgba(102, 92, 84, 0.3)",
    black:         "#282828",
    red:           "#cc241d",
    green:         "#98971a",
    yellow:        "#d79921",
    blue:          "#458588",
    magenta:       "#b16286",
    cyan:          "#689d6a",
    white:         "#a89984",
    brightBlack:   "#928374",
    brightRed:     "#fb4934",
    brightGreen:   "#b8bb26",
    brightYellow:  "#fabd2f",
    brightBlue:    "#83a598",
    brightMagenta: "#d3869b",
    brightCyan:    "#8ec07c",
    brightWhite:   "#ebdbb2",
  } },
  { id: "gruvbox-light", name: "Gruvbox Light", theme: {
    background: "#fbf1c7",
    foreground: "#3c3836",
    cursor: "#3c3836",
    cursorAccent: "#fbf1c7",
    selectionBackground: "rgba(60, 56, 54, 0.3)",
    black:         "#fbf1c7",
    red:           "#cc241d",
    green:         "#98971a",
    yellow:        "#d79921",
    blue:          "#458588",
    magenta:       "#b16286",
    cyan:          "#689d6a",
    white:         "#7c6f64",
    brightBlack:   "#928374",
    brightRed:     "#9d0006",
    brightGreen:   "#79740e",
    brightYellow:  "#b57614",
    brightBlue:    "#076678",
    brightMagenta: "#8f3f71",
    brightCyan:    "#427b58",
    brightWhite:   "#3c3836",
  } },
  { id: "dracula", name: "Dracula", theme: {
    background: "#282a36",
    foreground: "#f8f8f2",
    cursor: "#f8f8f2",
    cursorAccent: "#282a36",
    selectionBackground: "rgba(68, 71, 90, 0.3)",
    black:         "#21222c",
    red:           "#ff5555",
    green:         "#50fa7b",
    yellow:        "#f1fa8c",
    blue:          "#bd93f9",
    magenta:       "#ff79c6",
    cyan:          "#8be9fd",
    white:         "#f8f8f2",
    brightBlack:   "#6272a4",
    brightRed:     "#ff6e6e",
    brightGreen:   "#69ff94",
    brightYellow:  "#ffffa5",
    brightBlue:    "#d6acff",
    brightMagenta: "#ff92df",
    brightCyan:    "#a4ffff",
    brightWhite:   "#ffffff",
  } },
  { id: "nord", name: "Nord", theme: {
    background: "#2e3440",
    foreground: "#d8dee9",
    cursor: "#eceff4",
    cursorAccent: "#2e3440",
    selectionBackground: "rgba(236, 239, 244, 0.3)",
    black:         "#3b4252",
    red:           "#bf616a",
    green:         "#a3be8c",
    yellow:        "#ebcb8b",
    blue:          "#81a1c1",
    magenta:       "#b48ead",
    cyan:          "#88c0d0",
    white:         "#e5e9f0",
    brightBlack:   "#596377",
    brightRed:     "#bf616a",
    brightGreen:   "#a3be8c",
    brightYellow:  "#ebcb8b",
    brightBlue:    "#81a1c1",
    brightMagenta: "#b48ead",
    brightCyan:    "#8fbcbb",
    brightWhite:   "#eceff4",
  } },
  { id: "one-dark", name: "One Dark", theme: {
    background: "#21252b",
    foreground: "#abb2bf",
    cursor: "#abb2bf",
    cursorAccent: "#21252b",
    selectionBackground: "rgba(50, 56, 68, 0.3)",
    black:         "#21252b",
    red:           "#e06c75",
    green:         "#98c379",
    yellow:        "#e5c07b",
    blue:          "#61afef",
    magenta:       "#c678dd",
    cyan:          "#56b6c2",
    white:         "#abb2bf",
    brightBlack:   "#767676",
    brightRed:     "#e06c75",
    brightGreen:   "#98c379",
    brightYellow:  "#e5c07b",
    brightBlue:    "#61afef",
    brightMagenta: "#c678dd",
    brightCyan:    "#56b6c2",
    brightWhite:   "#abb2bf",
  } },
  { id: "catppuccin-mocha", name: "Catppuccin Mocha", theme: {
    background: "#1e1e2e",
    foreground: "#cdd6f4",
    cursor: "#f5e0dc",
    cursorAccent: "#1e1e2e",
    selectionBackground: "rgba(245, 224, 220, 0.3)",
    black:         "#45475a",
    red:           "#f38ba8",
    green:         "#a6e3a1",
    yellow:        "#f9e2af",
    blue:          "#89b4fa",
    magenta:       "#f5c2e7",
    cyan:          "#94e2d5",
    white:         "#bac2de",
    brightBlack:   "#585b70",
    brightRed:     "#f7aec2",
    brightGreen:   "#c2ecbf",
    brightYellow:  "#fcd682",
    brightBlue:    "#aeccfc",
    brightMagenta: "#f398da",
    brightCyan:    "#b1eae1",
    brightWhite:   "#a6adc8",
  } },
  { id: "catppuccin-latte", name: "Catppuccin Latte", theme: {
    background: "#eff1f5",
    foreground: "#4c4f69",
    cursor: "#dc8a78",
    cursorAccent: "#eff1f5",
    selectionBackground: "rgba(220, 138, 120, 0.3)",
    black:         "#bcc0cc",
    red:           "#d20f39",
    green:         "#40a02b",
    yellow:        "#df8e1d",
    blue:          "#1e66f5",
    magenta:       "#ea76cb",
    cyan:          "#179299",
    white:         "#5c5f77",
    brightBlack:   "#acb0be",
    brightRed:     "#e7103f",
    brightGreen:   "#46b02f",
    brightYellow:  "#e49931",
    brightBlue:    "#3878f6",
    brightMagenta: "#ef95d7",
    brightCyan:    "#19a1a8",
    brightWhite:   "#6c6f85",
  } },
  { id: "tokyo-night", name: "Tokyo Night", theme: {
    background: "#1a1b26",
    foreground: "#c0caf5",
    cursor: "#c0caf5",
    cursorAccent: "#1a1b26",
    selectionBackground: "rgba(40, 52, 87, 0.3)",
    black:         "#15161e",
    red:           "#f7768e",
    green:         "#9ece6a",
    yellow:        "#e0af68",
    blue:          "#7aa2f7",
    magenta:       "#bb9af7",
    cyan:          "#7dcfff",
    white:         "#a9b1d6",
    brightBlack:   "#414868",
    brightRed:     "#f7768e",
    brightGreen:   "#9ece6a",
    brightYellow:  "#e0af68",
    brightBlue:    "#7aa2f7",
    brightMagenta: "#bb9af7",
    brightCyan:    "#7dcfff",
    brightWhite:   "#c0caf5",
  } },
  { id: "github-light", name: "GitHub Light", theme: {
    background: "#ffffff",
    foreground: "#1f2328",
    cursor: "#0969da",
    cursorAccent: "#ffffff",
    selectionBackground: "rgba(31, 35, 40, 0.3)",
    black:         "#24292f",
    red:           "#cf222e",
    green:         "#116329",
    yellow:        "#4d2d00",
    blue:          "#0969da",
    magenta:       "#8250df",
    cyan:          "#1b7c83",
    white:         "#6e7781",
    brightBlack:   "#57606a",
    brightRed:     "#a40e26",
    brightGreen:   "#1a7f37",
    brightYellow:  "#633c01",
    brightBlue:    "#218bff",
    brightMagenta: "#a475f9",
    brightCyan:    "#3192aa",
    brightWhite:   "#8c959f",
  } },
];

// Where a chosen palette lives. Holds {preset:"<id>"} for one of the above or
// {custom:{...ITheme}, name:"..."} for an imported one; absent is Paper, the
// default, which is the pair of ramps in 07-terminal.js following the chrome.
const TERM_THEME_KEY = "pockettui_term_theme";

// xterm's ANSI slots in the order iTerm2 numbers them, which is also the order
// the swatch strip paints and the order "Ansi N Color" indexes.
const ANSI_SLOTS = [
  "black", "red", "green", "yellow", "blue", "magenta", "cyan", "white",
  "brightBlack", "brightRed", "brightGreen", "brightYellow",
  "brightBlue", "brightMagenta", "brightCyan", "brightWhite",
];

function storedTermChoice() {
  try {
    const raw = localStorage.getItem(TERM_THEME_KEY);
    if (!raw) return null;
    const o = JSON.parse(raw);
    return o && typeof o === "object" ? o : null;
  } catch (e) { return null; }
}

// The palette in force, or null for Paper. currentTermTheme() (07-terminal.js)
// is the only caller that matters: null there means "follow the app theme",
// which is what every install had before this tab existed.
function storedTermPalette() {
  const c = storedTermChoice();
  if (!c) return null;
  if (c.custom) return c.custom;
  const p = TERM_PRESETS.find((x) => x.id === c.preset);
  return p ? p.theme : null;
}

// Stored, then straight onto the screen through the same call the theme button
// makes: xterm takes the new object and the chrome follows it, so the status
// bar and the safe-area strip change with the terminal underneath the sheet
// rather than at the next open.
function setTermChoice(choice) {
  try {
    if (choice) localStorage.setItem(TERM_THEME_KEY, JSON.stringify(choice));
    else localStorage.removeItem(TERM_THEME_KEY);
  } catch (e) {}
  applyTermTheme();
  syncAppearance();
}

// ------------------------------------------------------------
// Reading someone else's scheme
// ------------------------------------------------------------
// #rgb, #rrggbb, either case, with or without the hash — anything else is not a
// colour and the caller says so rather than guessing.
function hexColor(v) {
  const s = String(v == null ? "" : v).trim().replace(/^#/, "");
  if (/^[0-9a-f]{3}$/i.test(s)) {
    return ("#" + s[0] + s[0] + s[1] + s[1] + s[2] + s[2]).toLowerCase();
  }
  if (/^[0-9a-f]{6}$/i.test(s)) return ("#" + s).toLowerCase();
  return "";
}

function hexAlpha(hex, a) {
  const h = hex.slice(1);
  return "rgba(" + parseInt(h.slice(0, 2), 16) + ", " + parseInt(h.slice(2, 4), 16)
    + ", " + parseInt(h.slice(4, 6), 16) + ", " + a + ")";
}

// The one gate every import passes: 16 ANSI slots, a background and a
// foreground, or it is not a terminal scheme. Everything else is optional and
// derived — a cursor is the text colour, its accent the background it sits on,
// and selection the same 0.3-alpha rule the presets above use, so an import can
// never come out with unreadable highlighting.
function schemeToTheme(name, raw) {
  const t = {};
  for (const k of ANSI_SLOTS) {
    const c = hexColor(raw[k]);
    if (!c) throw new Error("no " + k + " colour");
    t[k] = c;
  }
  const bg = hexColor(raw.background);
  const fg = hexColor(raw.foreground);
  if (!bg) throw new Error("no background colour");
  if (!fg) throw new Error("no foreground colour");
  t.background = bg;
  t.foreground = fg;
  t.cursor = hexColor(raw.cursor) || fg;
  t.cursorAccent = hexColor(raw.cursorAccent) || bg;
  t.selectionBackground = hexAlpha(hexColor(raw.selectionBackground) || fg, 0.3);
  const selFg = hexColor(raw.selectionForeground);
  if (selFg) t.selectionForeground = selFg;
  return { name: String(name || "").trim() || "Imported", theme: t };
}

// iTerm2 writes a plist of dicts of 0..1 floats, one dict per slot, and names
// nothing: the scheme's name is the file it came in.
const ITERM_KEYS = {
  "Background Color": "background",
  "Foreground Color": "foreground",
  "Cursor Color": "cursor",
  "Cursor Text Color": "cursorAccent",
  "Selection Color": "selectionBackground",
  "Selected Text Color": "selectionForeground",
};

function itermDictHex(dict) {
  const part = {};
  let key = null;
  for (const node of dict.children) {
    if (node.tagName === "key") { key = node.textContent.trim(); continue; }
    if (key) part[key] = parseFloat(node.textContent);
    key = null;
  }
  if (!("Red Component" in part)) return "";
  const byte = (v) => {
    const n = Math.round(Math.min(1, Math.max(0, v || 0)) * 255);
    return (n < 16 ? "0" : "") + n.toString(16);
  };
  return "#" + byte(part["Red Component"]) + byte(part["Green Component"])
    + byte(part["Blue Component"]);
}

function parseItermcolors(xml, nameHint) {
  const doc = new DOMParser().parseFromString(xml, "application/xml");
  if (doc.querySelector("parsererror")) throw new Error("not valid XML");
  const root = doc.querySelector("plist > dict");
  if (!root) throw new Error("not an .itermcolors file");
  const raw = {};
  let key = null;
  for (const node of root.children) {
    if (node.tagName === "key") { key = node.textContent.trim(); continue; }
    const m = key && key.match(/^Ansi (\d+) Color$/);
    const slot = m ? ANSI_SLOTS[parseInt(m[1], 10)] : ITERM_KEYS[key];
    key = null;
    if (!slot || node.tagName !== "dict") continue;
    raw[slot] = itermDictHex(node);
  }
  return schemeToTheme(nameHint, raw);
}

// The three shapes someone actually has to hand: what this app stores (an
// xterm ITheme), a Windows Terminal scheme, and an iTerm2 export. Told apart by
// what they are rather than by asking — a plist opens with a tag, and the two
// JSON dialects differ on one pair of key names.
function parseScheme(text, nameHint) {
  const s = String(text || "").trim();
  if (!s) throw new Error("nothing pasted");
  if (s[0] === "<") return parseItermcolors(s, nameHint);
  let o;
  try { o = JSON.parse(s); } catch (e) { throw new Error("not JSON or an .itermcolors file"); }
  if (!o || typeof o !== "object") throw new Error("not a colour scheme");
  const raw = Object.assign({}, o);
  // Windows Terminal calls the two magentas purple, and its cursor key carries
  // the word Color. Everything else it spells the way xterm does.
  if (raw.magenta === undefined) raw.magenta = raw.purple;
  if (raw.brightMagenta === undefined) raw.brightMagenta = raw.brightPurple;
  if (raw.cursor === undefined) raw.cursor = raw.cursorColor;
  return schemeToTheme(o.name || nameHint, raw);
}

// ------------------------------------------------------------
// The tab
// ------------------------------------------------------------
function paletteError(msg) {
  const p = $("palette-error");
  p.textContent = msg || "";
  p.hidden = !msg;
}

// One row: the name, then the scheme painted on its own background — the
// foreground as a two-letter sample, then the 16 slots in order. A preview
// small enough to sit in a list is still the only honest way to choose, since
// the names mean nothing to anyone who has not already run the scheme.
function paletteRow(id, name, theme, selected) {
  const strip = el("span", { class: "palette-strip",
                             style: "background:" + theme.background });
  strip.appendChild(el("b", { style: "color:" + theme.foreground }, "Aa"));
  for (const k of ANSI_SLOTS) {
    strip.appendChild(el("i", { style: "background:" + theme[k] }));
  }
  const pick = el("button", {
    type: "button", class: "palette-pick", "data-id": id,
    "aria-pressed": selected ? "true" : "false",
  }, el("span", { class: "palette-name" }, name), strip);
  if (selected) pick.appendChild(svgIcon("i-check"));
  return pick;
}

function renderPalettes() {
  const choice = storedTermChoice() || {};
  const list = $("palette-list");
  list.textContent = "";
  if (choice.custom) {
    const item = el("div", { class: "palette-item" });
    item.appendChild(paletteRow("custom", "Custom: " + (choice.name || "Imported"),
                                choice.custom, true));
    // The way back out of an imported scheme, which is also the only way to
    // drop it: it is stored as the choice, so unchoosing it is removing it.
    item.appendChild(el("button", {
      type: "button", class: "list-row-del", id: "btn-palette-drop",
      "aria-label": "Remove this scheme",
    }, svgIcon("i-x")));
    list.appendChild(item);
  }
  // Paper previews as whatever the app theme currently resolves to, since that
  // is exactly what it does on the terminal.
  const paper = resolvedDark() ? TERM_THEME_DARK : TERM_THEME_LIGHT;
  list.appendChild(el("div", { class: "palette-item" },
    paletteRow("", "Paper (follows app theme)", paper,
               !choice.custom && !choice.preset)));
  for (const p of TERM_PRESETS) {
    list.appendChild(el("div", { class: "palette-item" },
      paletteRow(p.id, p.name, p.theme, choice.preset === p.id)));
  }
}

// Painted when the tab is shown rather than when the sheet opens, the same way
// the Keys tab's steppers are: the header's theme button can have moved the
// chrome since the last look.
function syncAppearance() {
  const pref = themePref();
  for (const b of $("app-theme").querySelectorAll("[data-theme]")) {
    b.setAttribute("aria-checked", b.dataset.theme === pref ? "true" : "false");
  }
  renderPalettes();
}

$("app-theme").addEventListener("click", (e) => {
  const b = e.target.closest("[data-theme]");
  if (!b) return;
  setThemePref(b.dataset.theme);
  // The palette rows under this one preview Paper, which has just changed.
  syncAppearance();
});

$("palette-list").addEventListener("click", (e) => {
  if (e.target.closest("#btn-palette-drop")) { setTermChoice(null); return; }
  const pick = e.target.closest(".palette-pick");
  if (!pick) return;
  paletteError("");
  setTermChoice(pick.dataset.id ? { preset: pick.dataset.id } : null);
});

$("btn-palette-reset").addEventListener("click", () => {
  paletteError("");
  setTermChoice(null);
});

// One import path for both ways in: the file picker drops the file's text into
// the box, so what is about to be used is on screen before the button is
// pressed and a rejected file can be corrected rather than re-picked.
function useScheme(nameHint) {
  let parsed;
  try {
    parsed = parseScheme($("palette-paste").value, nameHint || paletteFileName);
  } catch (err) {
    paletteError("Can't read that scheme — " + err.message);
    return;
  }
  paletteError("");
  $("palette-paste").value = "";
  paletteFileName = "";
  setTermChoice({ custom: parsed.theme, name: parsed.name });
  toast("Palette: " + parsed.name);
}

// The name the last picked file carried, since .itermcolors holds none of its
// own and a pasted plist has no name at all.
let paletteFileName = "";

$("btn-palette-use").addEventListener("click", () => useScheme(""));
$("btn-palette-file").addEventListener("click", () => $("palette-file-input").click());
$("palette-file-input").addEventListener("change", (e) => {
  const f = e.target.files && e.target.files[0];
  // Cleared either way: picking the same file twice must still fire a change.
  e.target.value = "";
  if (!f) return;
  paletteFileName = f.name.replace(/\.(json|itermcolors)$/i, "");
  f.text().then((text) => {
    $("palette-paste").value = text;
    useScheme(paletteFileName);
  }).catch(() => paletteError("Can't read that file"));
});
