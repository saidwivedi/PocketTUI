<script>
// Resolve theme before paint to avoid flash. The status-bar tint and the root
// background must be set here too — they track the app's resolved theme, not the
// OS preference, so a media-query meta tag would be wrong whenever they differ.
// One-time rename migration (agents_* -> pockettui_*). Runs before any key is
// read so an existing install keeps its backend and theme instead of falling
// back to the first-run sheet. Drop once no old installs are left.
(function() {
  ["backend", "theme"].forEach(function(k) {
    var old = localStorage.getItem("agents_" + k);
    if (old !== null && localStorage.getItem("pockettui_" + k) === null) {
      localStorage.setItem("pockettui_" + k, old);
    }
    if (old !== null) localStorage.removeItem("agents_" + k);
  });
})();

// ------------------------------------------------------------
// A terminal palette, read as a skin for the whole app
// ------------------------------------------------------------
// The colour maths and the derivation live here, in the one script that runs
// before paint, because a chosen palette has to be on the document before the
// first frame or the app flashes Paper and then repaints. The app's own script
// loads much later and reuses these as globals, so there is exactly one
// derivation and the boot paint can never disagree with a live palette swap.
function hexToRgb(h) {
  h = String(h || "").replace("#", "");
  if (h.length === 3) h = h[0] + h[0] + h[1] + h[1] + h[2] + h[2];
  return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16),
          parseInt(h.slice(4, 6), 16)];
}
function rgbToHex(c) {
  return "#" + c.map(function(v) {
    var n = Math.round(Math.min(255, Math.max(0, v)));
    return (n < 16 ? "0" : "") + n.toString(16);
  }).join("");
}
// t is how far to travel from a to b, 0..1.
function mixHex(a, b, t) {
  var x = hexToRgb(a), y = hexToRgb(b);
  return rgbToHex([0, 1, 2].map(function(i) { return x[i] + (y[i] - x[i]) * t; }));
}
function rgbaHex(h, a) {
  var c = hexToRgb(h);
  return "rgba(" + c[0] + ", " + c[1] + ", " + c[2] + ", " + a + ")";
}
// WCAG relative luminance, which is also what decides whether a palette is a
// light one or a dark one.
function srgbLum(h) {
  var c = hexToRgb(h).map(function(v) {
    v = v / 255;
    return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
  });
  return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
}
function contrastOf(a, b) {
  var x = srgbLum(a), y = srgbLum(b);
  return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05);
}
function paletteIsDark(theme) { return srgbLum(theme.background) < 0.5; }

// Drag a colour toward black or white — whichever is away from the surface it
// sits on — until it clears the ratio. Someone else's scheme is free to pair a
// yellow with a cream background; the app still has to be readable on it.
function ensureContrast(c, bg, want) {
  if (contrastOf(c, bg) >= want) return c;
  var to = srgbLum(bg) >= 0.5 ? "#000000" : "#ffffff";
  var lo = 0, hi = 1;
  for (var i = 0; i < 12; i++) {
    var mid = (lo + hi) / 2;
    if (contrastOf(mixHex(c, to, mid), bg) >= want) hi = mid; else lo = mid;
  }
  return mixHex(c, to, hi);
}
// The normal slot where it carries on the background, the bright one where it
// does not, and a forced lift where neither does.
function pickInk(normal, bright, bg, want) {
  if (contrastOf(normal, bg) >= want) return normal;
  if (contrastOf(bright, bg) >= want) return bright;
  return ensureContrast(normal, bg, want);
}

// An xterm ITheme -> the app's own colour tokens. The ratios are the ones the
// Paper palettes already use, measured off the two :root blocks in the
// stylesheet, so a scheme comes out with the same hierarchy rather than with
// sixteen terminal colours pasted over a paper shell.
//
// Two rules are not symmetric, because Paper's are not either. A recess goes
// toward the ink in both modes; a card goes lighter in both — toward the ink on
// a dark background, toward white on a light one. A scheme whose background is
// already white (GitHub Light) has no lighter left, so its card takes a hair of
// ink instead, which is what a white-page scheme does anyway.
function chromeTokensFor(theme) {
  var bg = theme.background, fg = theme.foreground;
  var dark = paletteIsDark(theme);
  var headroom = Math.min.apply(null, hexToRgb(bg)) < 250;
  var ink = ensureContrast(fg, bg, 4.5);
  var card = dark ? mixHex(bg, fg, 0.04)
    : (headroom ? mixHex(bg, "#ffffff", 0.45) : mixHex(bg, fg, 0.022));

  // The accent. A scheme's cursor is its own pick of a colour that stands out,
  // so it wins — but only where it is not simply the foreground again, which is
  // what more than half of these set it to, and where it carries on the
  // background. Blue is the fallback because it is the one slot every scheme
  // gives a hue to.
  var cursor = theme.cursor || fg;
  var fgd = hexToRgb(fg), cd = hexToRgb(cursor);
  var apart = Math.max(Math.abs(fgd[0] - cd[0]), Math.abs(fgd[1] - cd[1]),
                       Math.abs(fgd[2] - cd[2]));
  var accent = (apart >= 24 && contrastOf(cursor, bg) >= 3)
    ? cursor : pickInk(theme.blue, theme.brightBlue, bg, 3);

  var good = pickInk(theme.green, theme.brightGreen, bg, 3);
  var warn = pickInk(theme.yellow, theme.brightYellow, bg, 3);
  var bad = pickInk(theme.red, theme.brightRed, bg, 3);
  var blue = pickInk(theme.blue, theme.brightBlue, bg, 3);
  // The diff rows are the colour laid over the background at a sixth, and the
  // ink is checked against that tint rather than against bare paper, since the
  // tint is the surface it is actually read on.
  var addBg = mixHex(bg, theme.green, 0.15);
  var delBg = mixHex(bg, theme.red, 0.15);
  var hunkBg = mixHex(bg, theme.blue, 0.15);

  return {
    "--paper": bg,
    "--paper-rec": mixHex(bg, fg, dark ? 0.02 : 0.04),
    "--card": card,
    "--card-2": mixHex(card, fg, dark ? 0.045 : 0.025),
    "--hair": mixHex(bg, fg, 0.10),
    "--hair-strong": mixHex(bg, fg, 0.18),
    "--ink": ink,
    "--ink-2": mixHex(ink, bg, 0.13),
    "--secondary": ensureContrast(mixHex(ink, bg, 0.35), bg, 3),
    "--tertiary": mixHex(ink, bg, 0.50),
    "--whisper": mixHex(ink, bg, 0.66),
    "--umber": accent,
    "--umber-soft": rgbaHex(accent, 0.10),
    "--umber-tint": rgbaHex(accent, 0.06),
    "--good": good,
    "--warn": warn,
    "--bad": bad,
    "--diff-add-bg": addBg,
    "--diff-add-ink": ensureContrast(good, addBg, 3),
    "--diff-del-bg": delBg,
    "--diff-del-ink": ensureContrast(bad, delBg, 3),
    "--diff-hunk-bg": hunkBg,
    "--diff-hunk-ink": ensureContrast(blue, hunkBg, 3),
    "--diff-blue": ensureContrast(blue, hunkBg, 3),
    // The two 14px marks in the diff pane, which need saturation rather than
    // readability as a paragraph: the bright slot first, the normal one behind.
    "--diff-yes": pickInk(theme.brightGreen, theme.green, bg, 3),
    "--diff-no": pickInk(theme.brightRed, theme.red, bg, 3),
  };
}

// Written as inline custom properties on the document, which is the one place
// that outranks both :root blocks without touching either. The names that were
// written are kept so returning to Paper can remove exactly those and hand the
// stylesheet its own blocks back.
var appliedChromeTokens = [];
function applyPaletteChrome(theme) {
  clearPaletteChrome();
  var t = chromeTokensFor(theme);
  var s = document.documentElement.style;
  for (var k in t) { s.setProperty(k, t[k]); appliedChromeTokens.push(k); }
  // A palette brings its own light or dark: everything keyed off data-theme —
  // the grain, the terminal's Paper ramps, the raised key bar — reads the
  // background it is actually painted on.
  document.documentElement.setAttribute("data-theme",
                                        paletteIsDark(theme) ? "dark" : "light");
}
function clearPaletteChrome() {
  var s = document.documentElement.style;
  for (var i = 0; i < appliedChromeTokens.length; i++) {
    s.removeProperty(appliedChromeTokens[i]);
  }
  appliedChromeTokens = [];
}

// ------------------------------------------------------------
// The chosen pair
// ------------------------------------------------------------
// A palette is a light one or a dark one — its background says which — so the
// choice is two of them: one for each way the app can be painted, with
// pockettui_theme deciding which half is live. Both halves carry their resolved
// ITheme alongside the choice for exactly this moment: the preset table lives in
// the app's script, which has not run yet, and asking the user to watch a Paper
// flash first is not an option.
var TERM_THEME_KEY = "pockettui_term_theme";
// Paper's own two backgrounds. Not the terminal ramps' — these are the shell's
// --paper, which is what every screen but the terminal is painted in.
var PAPER_BG_LIGHT = "#FAF8F3", PAPER_BG_DARK = "#16140f";

function prefIsDark(pref) {
  return pref === "dark" || (pref === "auto" &&
    window.matchMedia("(prefers-color-scheme: dark)").matches);
}

// Paper is the one choice that is not a derived skin — it hands the document
// back to the stylesheet's own two :root blocks — so it is the one this script
// has to know by name. A missing or unreadable half is Paper too, which is also
// what a fresh install has.
function entryIsPaper(e) {
  return !e || !e.theme || !e.theme.background ||
    e.preset === "paper-light" || e.preset === "paper-dark";
}

// What is stored, read forgivingly. Before the pair there was one palette for
// both ways round; it becomes the half its own background puts it in and the
// other stays Paper, so a device already running Gruvbox Dark keeps it as its
// dark one. Anything else — absent, garbage — is the default pair, both Paper.
function readTermPair() {
  var raw = null;
  try { raw = JSON.parse(localStorage.getItem(TERM_THEME_KEY) || "null"); } catch (e) {}
  if (!raw || typeof raw !== "object") return { light: null, dark: null };
  if (raw.light || raw.dark) return { light: raw.light || null, dark: raw.dark || null };
  if (!raw.theme || !raw.theme.background) return { light: null, dark: null };
  var one = raw.custom ? { custom: true, name: raw.name, theme: raw.theme }
                       : { preset: raw.preset, theme: raw.theme };
  return paletteIsDark(raw.theme) ? { light: null, dark: one }
                                  : { light: one, dark: null };
}

// The one paint, taken by the first frame and by every later swap: a Paper half
// is data-theme and nothing else, so the stylesheet's blocks take over; any
// other half writes the derived tokens over them. The half itself already
// agrees with data-theme — it was sorted into this slot by its own luminance.
function applyEntryChrome(entry, dark) {
  if (entryIsPaper(entry)) {
    clearPaletteChrome();
    document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
  } else {
    applyPaletteChrome(entry.theme);
  }
}

(function() {
  var dark = prefIsDark(localStorage.getItem("pockettui_theme") || "auto");
  var entry = readTermPair()[dark ? "dark" : "light"];
  applyEntryChrome(entry, dark);
  var bg = entryIsPaper(entry) ? (dark ? PAPER_BG_DARK : PAPER_BG_LIGHT)
                               : entry.theme.background;
  document.documentElement.style.backgroundColor = bg;
  document.getElementById("meta-theme-color").content = bg;
})();
</script>
