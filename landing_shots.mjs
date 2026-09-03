#!/usr/bin/env node
// Regenerate the landing page's app screenshots from the app's own demo mode.
//
//   node landing_shots.mjs [--out DIR] [--only name,name] [--base-url URL]
//
// The shots are the built shell (mobile_build/index.html) driven by headless
// Chrome: real UI, real xterm, real key bar — only the machine behind it is
// invented. Run `python3 build_mobile.py` first; this script does not build,
// because building also rewrites the tracked mobile_app.html at the repo root.
//
// Every shot is taken twice, dark and light, at identical pixel sizes, because
// the landing page swaps `src` by theme and its width/height attributes are
// hard-coded (assets/landing.html). Do not change the sizes below without
// changing those attributes too.
//
// Two of the landing's images are deliberately absent here:
//   viewer.png / viewer-video.png — the media viewer needs a printed image or
//   video path, and the demo filesystem ships neither. Faking one would mean
//   shipping an asset the app never produced, so they are left alone.

import http from "node:http";
import fs from "node:fs";
import fsp from "node:fs/promises";
import path from "node:path";
import os from "node:os";
import { fileURLToPath } from "node:url";
import puppeteer from "puppeteer-core";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const BUILD_DIR = path.join(HERE, "mobile_build");
const SRC_DIR = path.join(HERE, "src", "mobile");
const DEFAULT_OUT = path.join(HERE, "assets", "landing_assets");

// The pairing code shape the app validates (isValidToken): 10 chars of A-Z2-7.
const FAKE_TOKEN = "ABCDE23456";

// ---------------------------------------------------------------- shot specs
//
// `width`/`height` are CSS pixels; every shot is captured at deviceScaleFactor
// 2, so the file is twice that. `phone` picks the touch emulation that decides
// which of the two key-bar layouts the stylesheet uses: a docked bar on a
// phone, the floating key pill beside a pointer (see KEY PILL in styles.css).
//
// Every `cmds` list opens with `clear`, which puts the demo's welcome banner
// behind the screen rather than leaving however much of it the rest of the
// output failed to scroll off — the shot is then the same whatever the pane's
// row count works out to.
//
// `tmux ls` is deliberately not in any list: the demo answers it with its own
// invented sessions (iphone-webapp, laptop-api, scratch, dated 2025), which
// would contradict the rail standing beside it in the same picture. `git log`
// is typed bare rather than as `git log --oneline -4`, because the demo ignores
// the flags and prints full commit blocks — a flag the output visibly does not
// obey is worse in a screenshot than no flag at all.
const SHOTS = {
  "hero-desktop": {
    width: 1160, height: 620, phone: false,
    // The rail only has rows to show if a session list came back, so this one
    // shot runs with the stub backend as well as the demo.
    backend: true, demo: true, rail: true, reportKey: true,
    cmds: ["clear", "git log", "git status", "git branch"],
  },
  "hero-phone": {
    width: 390, height: 844, phone: true,
    demo: true,
    cmds: ["clear", "git log", "git status"],
  },
  "sessions": {
    width: 390, height: 844, phone: true,
    backend: true, demo: false,
  },
  "keybar": {
    width: 390, height: 844, phone: true,
    // No software keyboard exists in headless Chrome, so this is the app's own
    // key controls rather than the old shot's phone keyboard: the bar with its
    // arrow row open, which is the half of that picture the app actually owns.
    // The landing copy describes the keys sitting above the phone keyboard, so
    // the shipped keybar.png stays a real-device screenshot and this capture
    // runs only when named in --only.
    optIn: true,
    demo: true, arrows: true,
    cmds: ["clear", "git log", "git status", "git branch"],
  },
  "voice": {
    width: 390, height: 844, phone: true,
    // The mic key routes to a backend engine, which it first probes over
    // /api/transcribe — so this one needs the stub backend too.
    backend: true, demo: true, voice: true,
    cmds: ["clear", "git log", "git status"],
  },
  // hero-desktop with the microphone running: same pane, same rail, same
  // commands, so the pair reads as one machine doing two things.
  "voice-desktop": {
    width: 1160, height: 620, phone: false,
    backend: true, demo: true, rail: true, reportKey: true, voice: true,
    cmds: ["clear", "git log", "git status", "git branch"],
  },
  // The three below are the only shots not taken inside demo mode — the demo
  // refuses both screens outright (see "the demo filesystem" above). They run
  // as a paired client against the stub, which serves the demo's own tree.
  "explorer": {
    width: 390, height: 844, phone: true,
    backend: true, demo: false, explorer: "~/projects/webapp",
  },
  "editor-desktop": {
    width: 1160, height: 620, phone: false,
    backend: true, demo: false, editor: "~/projects/webapp/src/router.js",
  },
  "editor": {
    width: 390, height: 844, phone: true,
    backend: true, demo: false, editor: "~/projects/webapp/src/router.js",
  },
};

const SCALE = 2;

// The terminal font, per shot class. Both are above the app's own default of
// 13, which is a size for reading a terminal at arm's length rather than for a
// landing page thumbnail. The numbers are chosen by the column count they land
// on, which is what actually decides whether a line of git output wraps. At 16
// the 390px phone is 39 columns — the density of the previous phone shots, where
// "api: 1 windows (created Mon Aug 31 12:" sat on one line — and the desktop's
// 833px pane is 86. Two constants for one number because the two panes are
// unrelated: changing the phone's density must not silently reflow the desktop.
const PHONE_FONT = 16;
const DESKTOP_FONT = 16;

// The rows the rail shows. Invented, like everything else in demo mode, and
// generic on purpose — these are the names a stranger sees on the landing page.
function fakeSessions() {
  const now = Math.floor(Date.now() / 1000);
  return [
    { name: "webapp", command: "bash", windows: 3, created: now - 4 * 60,
      attached: true, state: "", notify: "off" },
    { name: "api", command: "python3", windows: 1, created: now - 7 * 60,
      attached: false, state: "", notify: "off" },
    { name: "train", alias: "model run", command: "nvtop", windows: 2,
      created: now - 22 * 60, attached: false, state: "active", notify: "off" },
    { name: "notes", command: "nvim", windows: 1, created: now - 22 * 60,
      attached: false, state: "", notify: "off" },
  ];
}

// ------------------------------------------------------- the demo filesystem
//
// The file explorer and the editor are not part of demo mode: openFilesAtCwd()
// answers a tap inside the demo with `toast("No files in the demo")` and stops
// there (28-file-explorer.js), because both screens are plain clients of
// /api/fs/* on a real machine. DEMO_FS exists, but only the demo *shell* reads
// it — it backs `ls` and `cat`, nothing else.
//
// So those two screens are served the way the session rail already is: the app
// runs unpaired-to-nothing against the stub below, and the stub answers
// /api/fs/list and /api/fs/read out of the demo's own invented tree. The UI is
// the real one and the data is the same fabricated project the demo terminal
// prints — it is not a second invention, and no app code is touched. The shots
// that use it run with `demo: false`, and they say so in their spec.
//
// The tree is read out of the fragment rather than copied, so it cannot drift
// from what `ls` in the demo shows.
function loadDemoFs() {
  const src = fs.readFileSync(path.join(SRC_DIR, "js", "demo", "11-fake-fs.js"), "utf8");
  const start = src.indexOf("const DEMO_FS = ");
  const end = src.indexOf("\n};", start);
  if (start < 0 || end < 0) throw new Error("could not find DEMO_FS in 11-fake-fs.js");
  const literal = src.slice(start + "const DEMO_FS = ".length, end + 2);
  return new Function("return " + literal)();
}

// DEMO_FS's root key is "home", which the demo shell prints as ~. The server
// side of the app deals in absolute paths and hands back `home` so the client
// can shorten them again, so the tree is mounted at one:
const DEMO_HOME = "/home/demo";

// Directories are objects, files are strings — the fragment's own convention.
function demoFsNode(tree, abs) {
  const rel = abs === DEMO_HOME ? "" : abs.slice(DEMO_HOME.length + 1);
  let node = tree.home;
  for (const seg of rel.split("/").filter(Boolean)) {
    if (!node || typeof node !== "object" || !(seg in node)) return undefined;
    node = node[seg];
  }
  return node;
}

// "~", "~/x", "" and absolute forms all resolve here, and "." / ".." are
// collapsed, so a crumb or a typed address behaves as it does against the real
// backend.
function demoFsResolve(p) {
  let s = (p || "~").trim();
  if (s === "~" || s === "") s = DEMO_HOME;
  else if (s.startsWith("~/")) s = DEMO_HOME + "/" + s.slice(2);
  const out = [];
  for (const seg of s.split("/").filter(Boolean)) {
    if (seg === ".") continue;
    if (seg === "..") { out.pop(); continue; }
    out.push(seg);
  }
  return "/" + out.join("/");
}

// Fixed rather than Date.now()-derived: the explorer prints "3h ago" beside
// every row, and a screenshot whose captions drift between runs is a diff for
// no reason. Spread by name so the column is not one repeated value.
function demoFsMtime(name) {
  let h = 0;
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0;
  return Math.floor(Date.parse("2026-09-01T09:22:00Z") / 1000) - (h % 72) * 3600;
}

// The shape app.py's /api/fs/list returns, ordering included: directories
// first, then case-insensitive by name.
function demoFsList(tree, reqPath) {
  const abs = demoFsResolve(reqPath);
  const node = demoFsNode(tree, abs);
  if (node === undefined) return { code: 404, body: { error: "not_found" } };
  if (typeof node !== "object") return { code: 400, body: { error: "not_a_directory" } };
  const entries = Object.entries(node).map(([name, v]) => ({
    name,
    type: typeof v === "object" ? "dir" : "file",
    size: typeof v === "object" ? 0 : Buffer.byteLength(v, "utf8"),
    mtime: demoFsMtime(name),
  }));
  entries.sort((a, b) => (a.type !== "dir") - (b.type !== "dir") ||
                         a.name.toLowerCase().localeCompare(b.name.toLowerCase()) ||
                         a.name.localeCompare(b.name));
  return { code: 200, body: { path: abs, home: DEMO_HOME, entries } };
}

function demoFsRead(tree, reqPath) {
  const abs = demoFsResolve(reqPath);
  const node = demoFsNode(tree, abs);
  if (typeof node !== "string") return { code: 404, body: { error: "not_found" } };
  return { code: 200, body: {
    path: abs, content: node, hash: "demo", size: Buffer.byteLength(node, "utf8"),
    mtime: demoFsMtime(abs), lossy: false,
  } };
}

// ------------------------------------------------------------- static server
const MIME = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".png": "image/png",
  ".svg": "image/svg+xml",
  ".woff2": "font/woff2",
  ".woff": "font/woff",
  ".ico": "image/x-icon",
};

function sendJSON(res, code, body) {
  const s = JSON.stringify(body);
  res.writeHead(code, {
    "Content-Type": "application/json; charset=utf-8",
    "Cache-Control": "no-store",
  });
  res.end(s);
}

// Serves the built shell plus the handful of endpoints the shell asks about at
// boot. Everything else 404s, which the app already treats as "that server
// cannot do this" rather than as an error worth showing.
function startServer(root) {
  const demoFs = loadDemoFs();
  const server = http.createServer((req, res) => {
    const url = new URL(req.url, "http://127.0.0.1");
    const p = url.pathname;
    if (p === "/api/sessions") return sendJSON(res, 200, { sessions: fakeSessions() });
    if (p === "/api/version") return sendJSON(res, 200, { version: "0.9.13", capabilities: {} });
    // The mic key's pre-flight: anything but a 503 means "voice is set up".
    if (p === "/api/transcribe") return sendJSON(res, 200, { text: "" });
    if (p === "/api/fs/list") {
      const r = demoFsList(demoFs, url.searchParams.get("path") || "");
      return sendJSON(res, r.code, r.body);
    }
    if (p === "/api/fs/read") {
      const r = demoFsRead(demoFs, url.searchParams.get("path") || "");
      return sendJSON(res, r.code, r.body);
    }
    if (p.startsWith("/api/")) return sendJSON(res, 404, { error: "not_found" });

    const rel = p === "/" ? "index.html" : decodeURIComponent(p).replace(/^\/+/, "");
    const file = path.join(root, rel);
    if (!file.startsWith(root)) { res.writeHead(403); return res.end(); }
    fs.readFile(file, (err, buf) => {
      if (err) { res.writeHead(404); return res.end("not found"); }
      res.writeHead(200, {
        "Content-Type": MIME[path.extname(file)] || "application/octet-stream",
        "Cache-Control": "no-store",
      });
      res.end(buf);
    });
  });
  return new Promise((resolve) => {
    server.listen(0, "127.0.0.1", () => {
      resolve({ server, url: `http://127.0.0.1:${server.address().port}` });
    });
  });
}

// ---------------------------------------------------------------- fake audio
// Chrome's built-in fake capture device feeds silence, and the app is watching:
// a take with no signal escalates to "No signal — mic muted?" after 1.5s, which
// is a failure state, not a screenshot. So the fake device is pointed at a file
// instead — a plain 16-bit PCM tone, written here so the script carries no
// binary asset. Chrome loops it for as long as the capture runs.
function writeToneWav() {
  const rate = 48000, secs = 4, n = rate * secs;
  const data = Buffer.alloc(n * 2);
  for (let i = 0; i < n; i++) {
    const t = i / rate;
    // A carrier under a slow swell, so the level meter reads a moving mid-level
    // rather than pinning at one value.
    const a = 0.22 * (0.55 + 0.45 * Math.sin(2 * Math.PI * 0.7 * t));
    data.writeInt16LE(Math.round(a * 32767 * Math.sin(2 * Math.PI * 220 * t)), i * 2);
  }
  const head = Buffer.alloc(44);
  head.write("RIFF", 0);
  head.writeUInt32LE(36 + data.length, 4);
  head.write("WAVE", 8);
  head.write("fmt ", 12);
  head.writeUInt32LE(16, 16);            // PCM fmt chunk size
  head.writeUInt16LE(1, 20);             // PCM
  head.writeUInt16LE(1, 22);             // mono
  head.writeUInt32LE(rate, 24);
  head.writeUInt32LE(rate * 2, 28);      // byte rate
  head.writeUInt16LE(2, 32);             // block align
  head.writeUInt16LE(16, 34);            // bits per sample
  head.write("data", 36);
  head.writeUInt32LE(data.length, 40);
  const file = path.join(os.tmpdir(), "pockettui-landing-tone.wav");
  fs.writeFileSync(file, Buffer.concat([head, data]));
  return file;
}

// -------------------------------------------------------------- chrome paths
// Explicit, because puppeteer-core resolves nothing on its own. Newest
// Chrome-for-Testing wins; the system Chrome is the fallback.
function findChrome() {
  const cache = path.join(os.homedir(), ".cache", "puppeteer", "chrome");
  let dirs = [];
  try { dirs = fs.readdirSync(cache); } catch (e) {}
  const versioned = dirs
    .map((d) => {
      const m = /^linux-(\d+)\.(\d+)\.(\d+)\.(\d+)$/.exec(d);
      if (!m) return null;
      const bin = path.join(cache, d, "chrome-linux64", "chrome");
      if (!fs.existsSync(bin)) return null;
      return { bin, key: m.slice(1).map(Number) };
    })
    .filter(Boolean)
    .sort((a, b) => {
      for (let i = 0; i < 4; i++) if (a.key[i] !== b.key[i]) return b.key[i] - a.key[i];
      return 0;
    });
  if (versioned.length) return versioned[0].bin;
  for (const p of ["/usr/bin/google-chrome", "/usr/bin/google-chrome-stable", "/usr/bin/chromium"]) {
    if (fs.existsSync(p)) return p;
  }
  throw new Error("no Chrome found in ~/.cache/puppeteer/chrome or /usr/bin");
}

// ------------------------------------------------------------------ settling
// Waits on real signals rather than a sleep: web fonts resolved, xterm's own
// write queue drained (its callback fires once the data has been parsed and
// handed to the renderer), then two frames so the compositor has painted.
async function settle(page) {
  await page.evaluate(async () => {
    await document.fonts.ready;
    if (typeof term !== "undefined" && term) {
      await new Promise((r) => term.write("", r));
    }
    await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
  });
}

// xterm measures its cell against the device pixel ratio when it opens, and in
// a page whose ratio was overridden by the emulator it lands on the wrong one:
// the core sizes the grid for a 1x cell while the renderer paints a 2x one, so
// every glyph comes out double width. Touching fontSize is what makes xterm
// re-measure and re-size the canvas backing store; a blinking cursor is a coin
// flip at screenshot time, so it goes off in the same pass.
async function remeasureTerm(page) {
  await page.evaluate(() => {
    term.options.cursorBlink = false;
    const f = term.options.fontSize;
    term.options.fontSize = f + 1;
    term.options.fontSize = f;
    refit(0);
  });
  await page.waitForFunction(() => {
    const c = $("term-host").querySelector(".xterm-screen canvas");
    return c && Math.abs(c.width - c.clientWidth * devicePixelRatio) < 2;
  }, { timeout: 10000, polling: 100 });
}

// Two things the demo puts on screen that belong to the demo rather than to the
// app, and that the landing page should not be advertising:
//
//   - the DEMO badge in the pane's top-right corner. Taken off by removing the
//     class the app itself toggles, not by hiding the element: the stylesheet
//     steps the key pill down out of the badge's way (`#demo-badge.show ~
//     #keybar`), so a badge merely made invisible would leave the pill parked
//     70px lower than a real session puts it.
//   - the italic grey asides under each command ("(there is no repository here
//     — this transcript is written into the demo)"). They are the demo being
//     honest about being a demo, which is right in the demo and wrong in a
//     picture of the product. Suppressed at the three functions that emit them,
//     so no call site has to be found or matched.
//
// Both are page-side only; nothing in the app is edited.
async function quietDemo(page) {
  await page.evaluate(() => {
    $("demo-badge").classList.remove("show");
    window.demoNote = () => "";
    window.demoNoteLine = () => {};
    window.demoFabricatedLine = () => {};
  });
}

// The demo's shell is synchronous for everything this script types, but the
// agent TUIs stream on timers — so wait for the shell to be idle regardless.
async function waitIdle(page, timeout = 5000) {
  await page.waitForFunction(
    () => typeof demoBusy === "undefined" || (!demoBusy && demoTimers.length === 0),
    { timeout, polling: 100 },
  );
}

// --------------------------------------------------------------- one capture
async function capture(browser, base, name, spec, theme, outDir) {
  const page = await browser.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));

  // Nothing but our own origin: the shell carries a Cloudflare analytics beacon
  // that would otherwise hang every load on a DNS timeout.
  await page.setRequestInterception(true);
  page.on("request", (req) => {
    if (req.url().startsWith(base)) req.continue();
    else req.abort();
  });

  await page.setViewport({
    width: spec.width,
    height: spec.height,
    deviceScaleFactor: SCALE,
    isMobile: !!spec.phone,
    hasTouch: !!spec.phone,
  });

  // Seeded before any page script runs, so the pre-paint theme resolver in
  // <head> already reads the right value and nothing flashes.
  await page.evaluateOnNewDocument((seed) => {
    for (const [k, v] of Object.entries(seed)) localStorage.setItem(k, v);
  }, {
    pockettui_theme: theme,
    // A phone-shaped viewport would otherwise raise the install hint over the
    // bottom of the shot. It is a first-visit nudge, not part of the UI.
    pockettui_a2hs_dismissed: "1",
    pockettui_fontsize: String(spec.phone ? PHONE_FONT : DESKTOP_FONT),
    ...(spec.backend ? { pockettui_backend: base, pockettui_token: FAKE_TOKEN } : {}),
    // Forces the local-recording path for the voice shot rather than leaving
    // the engine to be resolved from the backend.
    ...(spec.voice ? { pockettui_voice_engine: "parakeet" } : {}),
  });

  await page.goto(base + (spec.demo ? "/?demo=1" : "/"), { waitUntil: "load" });

  if (spec.demo) {
    await page.waitForFunction(() => typeof term !== "undefined" && term && term.cols > 0,
                               { timeout: 10000 });
    await quietDemo(page);
    await remeasureTerm(page);
  } else {
    await page.waitForFunction(() => $("list").children.length > 0, { timeout: 10000 });
  }
  await settle(page);

  // Both screens are opened through the app's own entry points, so the shot is
  // the state a tap actually produces.
  //
  // The editor goes via the explorer rather than straight to openEditor, and
  // has to: openEditor only swaps #screen-files out for #screen-editor, because
  // the screen it is always entered from is the explorer. Called from the list
  // it leaves #screen-list active underneath, and on a phone that paints the
  // app header straight over the editor's own toolbar.
  const dir = spec.explorer ||
              (spec.editor ? spec.editor.replace(/\/[^/]*$/, "") : null);
  if (dir) {
    await page.evaluate((p) => openExplorer(p), dir);
    await page.waitForFunction(() => $("files-list").children.length > 0, { timeout: 10000 });
  }
  if (spec.editor) {
    // CodeMirror is fetched from vendor/ on first use, so wait for the view
    // itself rather than for the screen class the call sets straight away.
    await page.evaluate((p) => openEditor(p), spec.editor);
    await page.waitForFunction(
      () => $("screen-editor").classList.contains("active") &&
            !!document.querySelector("#screen-editor .cm-content"),
      { timeout: 20000 });
  }

  // The rail is CSS-visible at this width whatever the mode, but demo mode
  // never loads a list into it — ask for one explicitly.
  if (spec.rail) {
    await page.evaluate(() => loadSessions());
    await page.waitForFunction(() => $("list").children.length > 0, { timeout: 10000 });
  }

  for (const cmd of spec.cmds || []) {
    await page.evaluate((c) => send(c + "\r"), cmd);
    await waitIdle(page);
    await settle(page);
  }

  // reportAvailable() is false inside the demo — the demo is nobody's machine,
  // so there is nothing to report about. The pill on the landing page should
  // still show the ring a paired user gets, so it is turned on directly.
  if (spec.reportKey) {
    await page.evaluate(() => {
      const k = $("keybar").querySelector(".k-report");
      if (k) k.classList.add("show");
    });
  }

  if (spec.arrows) {
    await page.evaluate(() => setArrows(true));
    await settle(page);
  }

  if (spec.voice) await startVoiceState(page);

  // Again, and last: opening the arrow row or the compose strip re-fits the
  // terminal, and the re-fit lands back on the emulator's wrong pixel ratio.
  if (spec.demo) {
    await page.evaluate(() => $("demo-badge").classList.remove("show"));
    await remeasureTerm(page);
  }
  await settle(page);

  // The grid the shot actually landed on. Reported because the column count is
  // what decides whether a line of git output wraps, and it is the number to
  // reach for when a font size needs changing.
  const grid = await page.evaluate(
    () => (typeof term !== "undefined" && term ? term.cols + "x" + term.rows : ""));

  const file = path.join(outDir, name + (theme === "light" ? "-light" : "") + ".png");
  await page.screenshot({ path: file, captureBeyondViewport: false });
  await page.close();

  if (errors.length) console.warn(`  ! ${name} ${theme}: page errors: ${errors.join(" | ")}`);
  return { file, grid };
}

// Drives the mic key into a live take. Chrome is launched with a fake capture
// device, so everything below the tap is the app's own recording path —
// getUserMedia, MediaRecorder, the RMS analyser that moves the level meter —
// rather than hand-set classes.
//
// The one override: recAvailable() refuses the local engine inside the demo
// ("the demo has no backend to send audio to"), and it is the only thing in the
// way — the demo does open a session, and the stub server does answer the
// transcribe probe. Replacing that single predicate is what lets the real
// capture run; nothing else about the state is faked. The take is never stopped,
// so no audio is ever uploaded.
async function startVoiceState(page) {
  await page.evaluate(() => { window.recAvailable = () => true; });
  await page.evaluate(() => { $("keybar").querySelector(".k-compose").click(); });
  await page.waitForFunction(() => $("compose").classList.contains("recording"),
                             { timeout: 15000 });
  // Caught at 0:00 the strip reads as a dead control: wait for the counter and
  // the level meter to have taken real readings.
  await page.waitForFunction(
    () => {
      const m = /^(\d+):(\d\d)$/.exec($("compose-rec").querySelector(".elapsed").textContent.trim());
      return !!m && Number(m[1]) * 60 + Number(m[2]) >= 3;
    },
    { timeout: 10000, polling: 200 },
  );
}

// ---------------------------------------------------------------------- main
function parseArgs(argv) {
  const out = { outDir: DEFAULT_OUT, only: null, baseUrl: null };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--out") out.outDir = path.resolve(argv[++i]);
    else if (a === "--only") out.only = argv[++i].split(",").map((s) => s.trim()).filter(Boolean);
    else if (a === "--base-url") out.baseUrl = argv[++i].replace(/\/+$/, "");
    else if (a === "-h" || a === "--help") { out.help = true; }
    else throw new Error("unknown argument: " + a);
  }
  return out;
}

// The build is not run from here (it also rewrites the tracked mobile_app.html),
// but a shell older than its own source would silently produce stale shots.
function checkBuildFresh() {
  const index = path.join(BUILD_DIR, "index.html");
  if (!fs.existsSync(index)) {
    throw new Error(`${index} is missing — run: python3 build_mobile.py`);
  }
  const built = fs.statSync(index).mtimeMs;
  let newest = 0, newestFile = "";
  const walk = (dir) => {
    for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
      const f = path.join(dir, e.name);
      if (e.isDirectory()) walk(f);
      else {
        const m = fs.statSync(f).mtimeMs;
        if (m > newest) { newest = m; newestFile = f; }
      }
    }
  };
  walk(SRC_DIR);
  if (newest > built) {
    console.warn(`! mobile_build/index.html is older than ${path.relative(HERE, newestFile)}`);
    console.warn("  run `python3 build_mobile.py` first, or these shots will be stale.");
  }
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.help) {
    console.log("usage: node landing_shots.mjs [--out DIR] [--only name,name] [--base-url URL]");
    console.log("shots: " + Object.keys(SHOTS).join(", "));
    return;
  }

  const names = args.only || Object.keys(SHOTS).filter((n) => !SHOTS[n].optIn);
  for (const n of names) {
    if (!(n in SHOTS)) throw new Error(`unknown shot: ${n} (have: ${Object.keys(SHOTS).join(", ")})`);
  }

  await fsp.mkdir(args.outDir, { recursive: true });

  let srv = null, base = args.baseUrl;
  if (!base) {
    checkBuildFresh();
    srv = await startServer(BUILD_DIR);
    base = srv.url;
  }

  const executablePath = findChrome();
  console.log("chrome:  " + executablePath);
  console.log("serving: " + base + (srv ? ` (${path.relative(HERE, BUILD_DIR)})` : " (external)"));
  console.log("out:     " + args.outDir);

  const browser = await puppeteer.launch({
    executablePath,
    headless: true,
    args: [
      "--no-sandbox",
      // A synthetic capture device, so the voice shot exercises the real
      // getUserMedia/MediaRecorder path with no microphone and no permission
      // prompt.
      "--use-fake-device-for-media-stream",
      "--use-fake-ui-for-media-stream",
      "--use-file-for-fake-audio-capture=" + writeToneWav(),
      "--autoplay-policy=no-user-gesture-required",
      // Deterministic rasterisation across runs.
      "--force-color-profile=srgb",
      "--font-render-hinting=none",
      "--hide-scrollbars",
      // xterm's WebGL renderer, without a GPU.
      "--enable-unsafe-swiftshader",
      // Headless Chrome reports no input device at all, so `(hover: hover) and
      // (pointer: fine)` is false and the desktop shots would come out with the
      // phone's docked key bar instead of the key pill. These are Blink's
      // pointer/hover enums (fine = 4, hover = 2). Device emulation overrides
      // them per page, which is exactly what the phone shots want — their
      // isMobile viewport puts hover/pointer back to none.
      "--blink-settings=primaryPointerType=4,availablePointerTypes=4," +
        "primaryHoverType=2,availableHoverTypes=2",
    ],
  });
  const ctx = await browser.createBrowserContext();
  await ctx.overridePermissions(base, ["microphone"]);

  const made = [];
  try {
    for (const name of names) {
      for (const theme of ["dark", "light"]) {
        const { file, grid } = await capture(ctx, base, name, SHOTS[name], theme, args.outDir);
        const { width, height } = SHOTS[name];
        console.log(`  ${path.basename(file)}  ${width * SCALE}x${height * SCALE}` +
                    (grid ? `  term ${grid}` : ""));
        made.push(file);
      }
    }
  } finally {
    await browser.close();
    if (srv) srv.server.close();
  }
  console.log(`\n${made.length} files written to ${args.outDir}`);
}

main().catch((e) => { console.error(e); process.exit(1); });
