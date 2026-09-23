#!/usr/bin/env node
// Screenshots for the features guide ("For you" tab of render_features.py), taken
// from the real built shell (mobile_build/index.html) on the WIDE layout.
//
//   node feature_shots.mjs [--only id,id] [--theme dark|light] [--debug DIR] [--list]
//
// Mechanics copied from landing_shots.mjs (static server, headless Chrome with
// a fine pointer, dark + light at deviceScaleFactor 2). Unlike the landing
// script, these shots run as a PAIRED client against a stub backend that lives
// in this file: it serves the session list, the attach WebSocket (canned
// terminal output instead of tmux), /api/fs/* over an invented project tree,
// git, search, version/capabilities, voice, bookmarks and a tiny proxied page
// for the browser pane. Every state is reached by clicking and typing in the
// UI; the only direct state set-up is localStorage (theme, profiles, font),
// the same way the landing harness seeds it.
//
// Output: assets/feature_shots/<id>-{dark,light}.webp (gitignored, regenerated)
// + src/features/manifest.json, and the `shot` field of src/features/catalog.json.
// PNGs are written to a temp dir and converted with Pillow (python3), so none
// land in assets/feature_shots/.
// Invented data only: dev-laptop, alice@example.net, ~/projects/webapp.

import http from "node:http";
import fs from "node:fs";
import fsp from "node:fs/promises";
import path from "node:path";
import os from "node:os";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import puppeteer from "puppeteer-core";
import { WebSocketServer } from "ws";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const BUILD_DIR = path.join(HERE, "mobile_build");
const SRC_DIR = path.join(HERE, "src", "mobile");
const OUT_DIR = path.join(HERE, "assets", "feature_shots");
const FEATURES_DIR = path.join(HERE, "src", "features");
const SCALE = 2;
const FAKE_TOKEN = "ABCDE23456";
const FONT = 14;
const HOME = "/home/alice";
const HOST = "dev-laptop";
const SERVER_VERSION = "0.9.172";
const LATEST_VERSION = "0.9.175";

// The address the page and the backend are reached at. Chrome resolves it to
// the stub (--host-resolver-rules), so the Connection tab, the pairing QR and
// the diagnostics show a plausible machine rather than 127.0.0.1:<random>.
const PUBLIC = "http://" + HOST + ":5560";

const M = { width: 1160, height: 620 };   // the landing hero size
const L = { width: 1440, height: 800 };   // two stacked panes / wide sheets

// ------------------------------------------------------------ invented data
const ESC = "\x1b[";
const c = (code, s) => `${ESC}${code}m${s}${ESC}0m`;
// Short enough that a line still fits the terminal beside a docked pane.
const prompt = (dir) => `${c("1;34", dir)} ${c("1;32", "$")} `;

const TRANSCRIPTS = {
  webapp: [
    prompt("~/projects/webapp") + "git status --short",
    ` ${c("31", "M")} src/router.js`,
    ` ${c("31", "M")} src/ui.js`,
    `${c("31", "??")} docs/diagram.png`,
    prompt("~/projects/webapp") + "npm test",
    "",
    "> webapp@0.4.2 test",
    "> vitest run",
    "",
    ` ${c("32", "✓")} tests/router.test.js (3 tests) 4ms`,
    ` ${c("32", "✓")} tests/utils.test.js (1 test) 2ms`,
    "",
    ` Test Files  ${c("1;32", "2 passed")} (2)`,
    `      Tests  ${c("1;32", "4 passed")} (4)`,
    "",
    prompt("~/projects/webapp") + "node scripts/report.js",
    "wrote ~/projects/webapp/docs/notes.md",
    prompt("~/projects/webapp") + "npm run dev",
    "",
    `  ${c("1;32", "VITE v5.4.2")}  ready in 312 ms`,
    "",
    `  ${c("32", "➜")}  ${c("1", "Local")}:   ${c("36", "http://localhost:5173/")}`,
    `  ${c("32", "➜")}  ${c("2", "Network: use --host to expose")}`,
    "",
  ],
  api: [
    prompt("~/projects/api-service") + ".venv/bin/python main.py",
    " * Serving Flask app 'server'",
    " * Debug mode: off",
    " * Running on http://127.0.0.1:8080",
    `${c("33", "Press CTRL+C to quit")}`,
    '127.0.0.1 - - "GET /health HTTP/1.1" 200 -',
    '127.0.0.1 - - "GET /api/items HTTP/1.1" 200 -',
    '127.0.0.1 - - "POST /api/items HTTP/1.1" 201 -',
    "",
  ],
  agent: [
    prompt("~/projects/webapp") + "./scripts/migrate.sh",
    "Reading migrations from db/migrations",
    `  ${c("32", "✓")} 0007_add_sessions.sql`,
    `  ${c("32", "✓")} 0008_index_user_email.sql`,
    `  ${c("33", "•")} 0009_drop_legacy_tokens.sql`,
    "",
    "This drops the table legacy_tokens (1,204 rows).",
    `${c("1", "Apply 0009_drop_legacy_tokens.sql? [y/n]")} `,
  ],
};
const WAITING_PROMPT = { type: "prompt", options: ["y", "n"], line: "Apply 0009_drop_legacy_tokens.sql? [y/n]" };

function fakeSessions(n) {
  const now = Math.floor(Date.now() / 1000);
  // The first answer has the agent session still running; every later one has
  // it waiting on its human — so a reload marks it unread, as a real one would.
  const agentState = n <= 1 ? "active" : "waiting";
  return [
    { name: "webapp", command: "bash", title: "", cwd: HOME + "/projects/webapp", windows: 1,
      created: now - 40 * 60, attached: true, alias: "", notify: "sound", state: "idle", last_activity: now },
    { name: "agent", command: "bash", title: "", cwd: HOME + "/projects/webapp", windows: 1,
      created: now - 18 * 60, attached: false, alias: "db migration", notify: "sound", state: agentState, last_activity: now },
    { name: "api", command: "python3", title: "", cwd: HOME + "/projects/api-service", windows: 2,
      created: now - 2 * 3600, attached: false, alias: "", notify: "off", state: "active", last_activity: now },
    { name: "docs", command: "claude", title: "✳ Update the setup guide", cwd: HOME + "/projects/webapp/docs", windows: 1,
      created: now - 3 * 3600, attached: false, alias: "", notify: "silent", state: "ready", last_activity: now },
    { name: "notes", command: "nvim", title: "", cwd: HOME, windows: 1,
      created: now - 26 * 3600, attached: false, alias: "", notify: "off", state: "idle", last_activity: now },
  ];
}

const CAPABILITIES = {
  fs: true, image_paste: true, upload: true, voice: true, learned: true, push: true, dbg: true,
  git: true, git_ref: true, search: true, ping: true, update: true, pair_qr: true, update_status: true,
  type: true, relay: true, browse: true, browse_tab: true, bookmarks: true, zip_dir: true,
  thumbs: true, pdf_thumbs: true, browser_full: true, mode: true,
};

const NOTES_MD = [
  "# Caching notes",
  "",
  "The API answers in about 40 ms; the page budget is 200 ms. A cache in front",
  "of `/api/items` pays off once the hit rate $h$ satisfies",
  "",
  "$$",
  "t_{\\text{avg}} = h\\,t_{\\text{cache}} + (1 - h)\\,t_{\\text{api}} < \\tfrac{1}{2}\\,t_{\\text{api}}",
  "$$",
  "",
  "With $t_{\\text{cache}} = 2$ ms that is $h > 0.53$.",
  "",
  "## To do",
  "",
  "- [x] Measure the current p95",
  "- [x] Pick a TTL for item lists",
  "- [ ] Invalidate on `POST /api/items`",
  "- [ ] Write up the decision",
  "",
].join("\n");

const DIFF_ROUTER = [
  "diff --git a/src/router.js b/src/router.js",
  "index 3b1c2d0..9f4e7a1 100644",
  "--- a/src/router.js",
  "+++ b/src/router.js",
  "@@ -1,8 +1,13 @@",
  " const routes = new Map();",
  "+const notFound = () => 'not-found';",
  " ",
  " export function createRouter() {",
  "   return {",
  "     add(path, view) { routes.set(path, view); },",
  "-    resolve(path) { return routes.get(path) || routes.get('*'); },",
  "+    resolve(path) {",
  "+      const clean = path.replace(/\\/+$/, '') || '/';",
  "+      return routes.get(clean) || routes.get('*') || notFound;",
  "+    },",
  "+    size() { return routes.size; },",
  "   };",
  " }",
  "",
].join("\n");
const DIFF_UI = [
  "diff --git a/src/ui.js b/src/ui.js",
  "index 71aa0b2..c02d9e5 100644",
  "--- a/src/ui.js",
  "+++ b/src/ui.js",
  "@@ -1,4 +1,6 @@",
  " export function mountUI(root, router) {",
  "   root.innerHTML = '';",
  "-  root.appendChild(document.createElement('main'));",
  "+  const main = document.createElement('main');",
  "+  main.dataset.routes = String(router.size());",
  "+  root.appendChild(main);",
  " }",
  "",
].join("\n");

// ---------------------------------------------------------- the file tree
// The demo's own tree (read out of 11-fake-fs.js like the landing harness),
// plus the files these shots need: images for the grid and the viewer, a
// Markdown file with math and a task list. Buffers are binary files.
function loadDemoFs() {
  const src = fs.readFileSync(path.join(SRC_DIR, "js", "demo", "11-fake-fs.js"), "utf8");
  const start = src.indexOf("const DEMO_FS = ");
  const end = src.indexOf("\n};", start);
  return new Function("return " + src.slice(start + "const DEMO_FS = ".length, end + 2))();
}

// Invented pictures, drawn by Pillow so the script carries no binary asset.
function makeImages(dir) {
  const py = String.raw`
import sys, os
from PIL import Image, ImageDraw
d = sys.argv[1]
def save(name, img): img.save(os.path.join(d, name))
W, H = 960, 600
# a login screen mock
im = Image.new("RGB", (W, H), (236, 240, 247)); g = ImageDraw.Draw(im)
g.rectangle([0, 0, W, 56], fill=(37, 99, 235))
g.rounded_rectangle([330, 150, 630, 450], 14, fill=(255, 255, 255), outline=(210, 216, 226))
for i, y in enumerate((210, 270)):
    g.rounded_rectangle([360, y, 600, y + 38], 6, fill=(245, 247, 250), outline=(200, 206, 216))
g.rounded_rectangle([360, 340, 600, 382], 8, fill=(37, 99, 235))
g.ellipse([455, 165, 505, 200], fill=(37, 99, 235))
save("screenshot-login.png", im)
# a dashboard mock
im = Image.new("RGB", (W, H), (250, 250, 250)); g = ImageDraw.Draw(im)
g.rectangle([0, 0, 180, H], fill=(30, 41, 59))
for i in range(5): g.rounded_rectangle([20, 80 + i * 44, 160, 108 + i * 44], 5, fill=(51, 65, 85))
vals = [120, 220, 180, 300, 260, 380, 330, 420]
for i, v in enumerate(vals):
    g.rectangle([240 + i * 80, 520 - v, 290 + i * 80, 520], fill=(16, 185, 129) if i % 2 else (59, 130, 246))
g.line([220, 520, 920, 520], fill=(120, 120, 120), width=2)
save("dashboard.png", im)
# an architecture diagram
im = Image.new("RGB", (W, H), (255, 255, 255)); g = ImageDraw.Draw(im)
boxes = [(80, 240, 260, 340, (254, 243, 199)), (390, 120, 570, 220, (219, 234, 254)),
         (390, 360, 570, 460, (220, 252, 231)), (700, 240, 880, 340, (243, 232, 255))]
for x0, y0, x1, y1, col in boxes: g.rounded_rectangle([x0, y0, x1, y1], 12, fill=col, outline=(90, 90, 90), width=2)
for a, b in (((260, 290), (390, 170)), ((260, 290), (390, 410)), ((570, 170), (700, 290)), ((570, 410), (700, 290))):
    g.line([a, b], fill=(90, 90, 90), width=3)
save("diagram.png", im)
# a logo with transparency
im = Image.new("RGBA", (512, 512), (0, 0, 0, 0)); g = ImageDraw.Draw(im)
g.rounded_rectangle([56, 56, 456, 456], 90, fill=(184, 92, 56, 255))
g.polygon([(170, 170), (300, 256), (170, 342)], fill=(255, 250, 240, 255))
g.rectangle([300, 320, 380, 350], fill=(255, 250, 240, 255))
save("logo.png", im)
# a photo-like gradient
im = Image.new("RGB", (W, H)); px = im.load()
for y in range(H):
    for x in range(0, W):
        px[x, y] = (int(40 + 150 * y / H), int(90 + 80 * x / W), int(160 - 60 * y / H))
g = ImageDraw.Draw(im); g.ellipse([640, 90, 780, 230], fill=(255, 214, 120))
g.polygon([(0, H), (260, 330), (520, H)], fill=(40, 60, 50)); g.polygon([(300, H), (620, 280), (960, H)], fill=(30, 48, 40))
im.save(os.path.join(d, "team-offsite.jpg"), quality=88)
`;
  execFileSync("python3", ["-c", py, dir]);
  const read = (n) => fs.readFileSync(path.join(dir, n));
  return {
    "screenshot-login.png": read("screenshot-login.png"),
    "dashboard.png": read("dashboard.png"),
    "diagram.png": read("diagram.png"),
    "logo.png": read("logo.png"),
    "team-offsite.jpg": read("team-offsite.jpg"),
  };
}

function buildTree(imgDir) {
  const tree = loadDemoFs().home;
  const img = makeImages(imgDir);
  const web = tree.projects.webapp;
  web.docs["notes.md"] = NOTES_MD;
  web.docs["diagram.png"] = img["diagram.png"];
  web.assets = {
    "dashboard.png": img["dashboard.png"],
    "logo.png": img["logo.png"],
    "screenshot-login.png": img["screenshot-login.png"],
    "team-offsite.jpg": img["team-offsite.jpg"],
    "favicon.svg": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"><rect width="16" height="16" rx="3" fill="#b85c38"/></svg>\n',
    "styles.css": "body { font: 16px system-ui; margin: 0; }\nmain { max-width: 60rem; margin: 0 auto; }\n",
  };
  web.scripts = { "snapshot.js": "// takes a screenshot of the login page\n", "migrate.sh": "#!/bin/sh\n" };
  web.db = { migrations: { "0009_drop_legacy_tokens.sql": "DROP TABLE legacy_tokens;\n" } };
  web.src["router.js"] = web.src["router.js"].replace(
    "    resolve(path) { return routes.get(path) || routes.get('*'); },\n",
    "    resolve(path) {\n      const clean = path.replace(/\\/+$/, '') || '/';\n      return routes.get(clean) || routes.get('*') || notFound;\n    },\n    size() { return routes.size; },\n",
  ).replace("const routes = new Map();\n", "const routes = new Map();\nconst notFound = () => 'not-found';\n");
  return tree;
}

function resolvePath(p) {
  let s = (p || "~").trim();
  if (s === "~" || s === "") s = HOME;
  else if (s.startsWith("~/")) s = HOME + "/" + s.slice(2);
  const out = [];
  for (const seg of s.split("/").filter(Boolean)) {
    if (seg === ".") continue;
    if (seg === "..") { out.pop(); continue; }
    out.push(seg);
  }
  return "/" + out.join("/");
}
function nodeAt(tree, abs) {
  if (abs === HOME) return tree;
  if (!abs.startsWith(HOME + "/")) return undefined;
  let node = tree;
  for (const seg of abs.slice(HOME.length + 1).split("/")) {
    if (!node || typeof node !== "object" || Buffer.isBuffer(node) || !(seg in node)) return undefined;
    node = node[seg];
  }
  return node;
}
const isDir = (n) => n && typeof n === "object" && !Buffer.isBuffer(n);
function mtimeOf(name) {
  let h = 0;
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0;
  return Math.floor(Date.now() / 1000) - 600 - (h % 72) * 3600;
}
function fsList(tree, reqPath) {
  const abs = resolvePath(reqPath);
  const node = nodeAt(tree, abs);
  if (node === undefined) return [404, { error: "not_found" }];
  if (!isDir(node)) return [400, { error: "not_a_directory" }];
  const entries = Object.entries(node).map(([name, v]) => ({
    name, type: isDir(v) ? "dir" : "file",
    size: isDir(v) ? 0 : (Buffer.isBuffer(v) ? v.length : Buffer.byteLength(v, "utf8")),
    mtime: mtimeOf(name),
  }));
  entries.sort((a, b) => (a.type !== "dir") - (b.type !== "dir") ||
    a.name.toLowerCase().localeCompare(b.name.toLowerCase()));
  return [200, { path: abs, home: HOME, entries }];
}

// ------------------------------------------------------------- the stub
const MIME = {
  ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8", ".json": "application/json; charset=utf-8",
  ".png": "image/png", ".jpg": "image/jpeg", ".svg": "image/svg+xml",
  ".woff2": "font/woff2", ".woff": "font/woff", ".ico": "image/x-icon", ".md": "text/markdown",
};
function sendJSON(res, code, body) {
  res.writeHead(code, { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" });
  res.end(JSON.stringify(body));
}
function readBody(req) {
  return new Promise((r) => { let b = ""; req.on("data", (d) => (b += d)); req.on("end", () => r(b)); });
}

const PAIR_QR = (() => {
  // A QR-looking pattern (not a real code: it encodes nothing).
  let s = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 29 29" shape-rendering="crispEdges"><rect width="29" height="29" fill="#fff"/>';
  let seed = 7;
  const rnd = () => ((seed = (seed * 1103515245 + 12345) >>> 0) >>> 16) & 1;
  const finder = (x, y) => `<path d="M${x} ${y}h7v7h-7zM${x + 1} ${y + 1}v5h5v-5zM${x + 2} ${y + 2}h3v3h-3z" fill="#000" fill-rule="evenodd"/>`;
  for (let y = 1; y < 28; y++) for (let x = 1; x < 28; x++) {
    const inF = (x < 9 && y < 9) || (x > 19 && y < 9) || (x < 9 && y > 19);
    if (!inF && rnd()) s += `<rect x="${x}" y="${y}" width="1" height="1" fill="#000"/>`;
  }
  return s + finder(1, 1) + finder(21, 1) + finder(1, 21) + "</svg>";
})();

const PAGE_HTML = `<!doctype html><html><head><meta charset="utf-8"><title>webapp — Items</title>
<style>body{margin:0;font:15px/1.5 system-ui,sans-serif;color:#1f2937;background:#f8fafc}
header{background:#2563eb;color:#fff;padding:14px 24px;display:flex;gap:24px;align-items:center}
header b{font-size:18px}header a{color:#dbeafe;text-decoration:none}
main{max-width:760px;margin:24px auto;padding:0 24px}h1{font-size:24px;margin:0 0 12px}
table{width:100%;border-collapse:collapse;background:#fff;border:1px solid #e5e7eb}
td,th{padding:8px 12px;border-bottom:1px solid #e5e7eb;text-align:left}th{background:#f1f5f9}
.pill{background:#dcfce7;color:#166534;border-radius:99px;padding:1px 8px;font-size:12px}
button{background:#2563eb;color:#fff;border:0;border-radius:6px;padding:7px 14px;font:inherit}</style></head>
<body><header><b>webapp</b><a href="#">Items</a><a href="#">Users</a><a href="#">Settings</a></header>
<main><h1>Items</h1><p>Served by <code>npm run dev</code> on this computer.</p>
<table><tr><th>Name</th><th>Owner</th><th>Status</th></tr>
<tr><td>Onboarding checklist</td><td>alice</td><td><span class="pill">live</span></td></tr>
<tr><td>Billing export</td><td>bob</td><td><span class="pill">live</span></td></tr>
<tr><td>Search filters</td><td>carol</td><td>draft</td></tr>
<tr><td>Dark mode</td><td>alice</td><td>draft</td></tr></table>
<p style="margin-top:16px"><button>New item</button></p></main></body></html>`;

function startServer(tree) {
  const state = { sessionsAsked: 0, made: [], sockets: new Map(), updateStatus: null };
  const server = http.createServer(async (req, res) => {
    const url = new URL(req.url, "http://127.0.0.1");
    const p = url.pathname;
    const q = (k) => url.searchParams.get(k) || "";
    if (p === "/.well-known/pockettui") return sendJSON(res, 200, { app: "pockettui", version: SERVER_VERSION });
    // Like the real server: every /api/ route wants the pairing code.
    if (p.startsWith("/api/") && !p.startsWith("/api/signed_file") && req.headers["x-pockettui-token"] !== FAKE_TOKEN) {
      return sendJSON(res, 401, { error: "unauthorized" });
    }
    if (p === "/api/sessions") {
      state.sessionsAsked++;
      return sendJSON(res, 200, { sessions: [...state.made, ...fakeSessions(state.sessionsAsked)] });
    }
    if (p === "/api/session" && req.method === "POST") {
      const body = JSON.parse((await readBody(req)) || "{}");
      const now = Math.floor(Date.now() / 1000);
      state.made.unshift({ name: body.name, command: "bash", title: "", cwd: HOME, windows: 1, created: now,
        attached: true, alias: "", notify: "off", state: "idle", last_activity: now });
      return sendJSON(res, 200, { session: body.name });
    }
    if (p === "/api/version") return sendJSON(res, 200, { version: SERVER_VERSION, host: HOST, capabilities: CAPABILITIES });
    if (p === "/api/voice_status") return sendJSON(res, 200, { engines: { parakeet: true, whisper: true }, active: "parakeet" });
    if (p === "/api/transcribe") return sendJSON(res, 200, { text: "" });
    if (p === "/api/learned") {
      const now = Math.floor(Date.now() / 1000);
      return sendJSON(res, 200, { entries: [
        { wrong: "view", right: "vite", count: 3, utterances: 3, last_ts: now - 3600, promoted: true },
        { wrong: "get hub", right: "GitHub", count: 1, utterances: 1, last_ts: now - 86400, promoted: false },
      ] });
    }
    if (p === "/api/update_status") return sendJSON(res, 200, { state: state.updateStatus, session_alive: false });
    if (p === "/api/push/status") return sendJSON(res, 200, { push: true, subscribed: false });
    if (p === "/api/pair_qr.svg") { res.writeHead(200, { "Content-Type": "image/svg+xml" }); return res.end(PAIR_QR); }
    if (p === "/api/session_cwd") {
      const s = fakeSessions(2).find((x) => x.name === q("session"));
      return sendJSON(res, 200, { cwd: s ? s.cwd : HOME });
    }
    if (p === "/api/fs/list") { const [code, body] = fsList(tree, q("path")); return sendJSON(res, code, body); }
    if (p === "/api/fs/read") {
      const abs = resolvePath(q("path"));
      const n = nodeAt(tree, abs);
      if (typeof n !== "string") return sendJSON(res, 404, { error: "not_found" });
      return sendJSON(res, 200, { path: abs, content: n, hash: "h" + n.length, size: Buffer.byteLength(n), mtime: mtimeOf(abs), lossy: false });
    }
    if (p === "/api/fs/thumb" || p === "/api/signed_file" || p === "/api/fs/download") {
      const abs = resolvePath(q("path"));
      const n = nodeAt(tree, abs);
      if (n === undefined || isDir(n)) return sendJSON(res, 404, { error: "not_found" });
      if (p === "/api/fs/thumb" && !Buffer.isBuffer(n)) return sendJSON(res, 415, { error: "unsupported" });
      res.writeHead(200, { "Content-Type": MIME[path.extname(abs)] || "application/octet-stream", "Cache-Control": "no-store" });
      return res.end(n);
    }
    if (p === "/api/file_link" || p === "/api/fs/download_link" || p === "/api/fs/render_link") {
      const abs = resolvePath(q("path"));
      if (nodeAt(tree, abs) === undefined) { res.writeHead(404); return res.end(); }
      return sendJSON(res, 200, { url: "api/signed_file?path=" + encodeURIComponent(abs) + "&exp=9999999999&sig=x", expires_in: 300 });
    }
    if (p === "/api/git/branches") {
      return sendJSON(res, 200, { root: HOME + "/projects/webapp", current: "feature/router-cleanup",
        branches: ["feature/router-cleanup", "main", "release/0.4", "origin/main", "origin/feature/router-cleanup"] });
    }
    if (p === "/api/git/changes") {
      const root = HOME + "/projects/webapp";
      if (q("scope") === "untracked") {
        return sendJSON(res, 200, { root, elapsed_ms: 8, files: [
          { path: "docs/diagram.png", status: "??", staged: false, unstaged: true },
          { path: "scripts/snapshot.js", status: "??", staged: false, unstaged: true },
        ] });
      }
      return sendJSON(res, 200, { root, elapsed_ms: 6, files: [
        { path: "src/router.js", status: " M", staged: false, unstaged: true },
        { path: "src/ui.js", status: "M ", staged: true, unstaged: false },
      ] });
    }
    if (p === "/api/git/diff") {
      const f = q("path");
      if (f === "src/router.js") return sendJSON(res, 200, { diff: DIFF_ROUTER, truncated: false });
      if (f === "src/ui.js") return sendJSON(res, 200, { diff: DIFF_UI, truncated: false });
      const n = nodeAt(tree, HOME + "/projects/webapp/" + f);
      if (typeof n === "string") {
        const lines = n.replace(/\n$/, "").split("\n");
        return sendJSON(res, 200, { diff: `diff --git a/${f} b/${f}\nnew file mode 100644\n--- /dev/null\n+++ b/${f}\n@@ -0,0 +1,${lines.length} @@\n` + lines.map((l) => "+" + l).join("\n") + "\n" });
      }
      return sendJSON(res, 200, { diff: "", binary: true });
    }
    if (p === "/api/search") {
      const body = JSON.parse((await readBody(req)) || "{}");
      const query = String(body.query || "");
      const sess = String(body.session || "");
      if (body.action === "cancel" || !query) { repaint(sess, null); return sendJSON(res, 200, { in_mode: false, present: null, count: null, partial: null, match: "" }); }
      const count = repaint(sess, query);
      return sendJSON(res, 200, { in_mode: true, present: count > 0, count, partial: false, match: query });
    }
    if (p === "/api/browse/bookmarks") {
      return sendJSON(res, 200, { bookmarks: [
        { url: "http://localhost:5173/", title: "webapp", added: 1 },
        { url: "http://localhost:8080/health", title: "api health", added: 2 },
        { url: "http://grafana.lan:3000/", title: "Grafana", added: 3 },
        { url: "https://developer.mozilla.org/", title: "MDN", added: 4 },
      ] });
    }
    if (p === "/api/browser/status") return sendJSON(res, 200, { found: { path: "/usr/bin/google-chrome", version: "128.0.6613.84" }, capMb: 1024 });
    if (p === "/api/browse") {
      const body = JSON.parse((await readBody(req)) || "{}");
      const u = new URL(String(body.url || "http://localhost:5173/"));
      const hp = u.port ? u.hostname + ":" + u.port : u.hostname;
      return sendJSON(res, 200, { token: "tok123", prefix: body.prefix || "", expires: Math.floor(Date.now() / 1000) + 3600,
        url: `/b/tok123/${u.protocol.replace(":", "")}/${hp}${u.pathname}${u.search}` });
    }
    if (p.startsWith("/b/")) {
      res.writeHead(200, { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" });
      return res.end(PAGE_HTML);
    }
    if (p.startsWith("/api/")) return sendJSON(res, 404, { error: "not_found" });

    const rel = p === "/" ? "index.html" : decodeURIComponent(p).replace(/^\/+/, "");
    const file = path.join(BUILD_DIR, rel);
    if (!file.startsWith(BUILD_DIR)) { res.writeHead(403); return res.end(); }
    fs.readFile(file, (err, buf) => {
      if (err) { res.writeHead(404); return res.end("not found"); }
      res.writeHead(200, { "Content-Type": MIME[path.extname(file)] || "application/octet-stream", "Cache-Control": "no-store" });
      res.end(buf);
    });
  });

  // The attach socket: the session's transcript as PTY bytes, a prompt frame
  // for the one waiting on its human. Input is dropped (nothing runs).
  const wss = new WebSocketServer({ noServer: true });
  const screenFor = (name, query) => {
    let lines = TRANSCRIPTS[name] || [prompt("~")];
    let count = 0;
    if (query) {
      // What tmux's copy mode paints with the app's match styles: every hit in
      // the soft yellow, the one the cursor is on in orange, and the position
      // indicator top-right.
      const re = new RegExp(query.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "g");
      const plain = lines.map((l) => l.replace(/\x1b\[[0-9;]*m/g, ""));
      const total = plain.reduce((n, l) => n + (l.match(re) || []).length, 0);
      let seen = 0;
      lines = plain.map((l) => l.replace(re, (m) => {
        seen++;
        return seen === total ? `\x1b[48;2;255;180;60;38;2;26;26;26m${m}\x1b[0m` : `\x1b[48;2;255;224;120;38;2;26;26;26m${m}\x1b[0m`;
      }));
      count = total;
    }
    let out = "\x1b[H\x1b[2J\x1b[3J" + lines.join("\r\n");
    if (query) out += `\x1b[1;1H\x1b[999C\x1b[10D\x1b[43;30m[${count}/${count}]\x1b[0m`;
    return { out, count };
  };
  function repaint(name, query) {
    const { out, count } = screenFor(name, query);
    for (const ws of state.sockets.get(name) || []) ws.send(Buffer.from(out, "utf8"));
    return count;
  }
  server.on("upgrade", (req, sock, head) => {
    const url = new URL(req.url, "http://127.0.0.1");
    const m = /^\/ws\/attach\/([^/]+)$/.exec(url.pathname);
    if (!m) { sock.destroy(); return; }
    const name = decodeURIComponent(m[1]);
    wss.handleUpgrade(req, sock, head, (ws) => {
      if (!state.sockets.has(name)) state.sockets.set(name, new Set());
      state.sockets.get(name).add(ws);
      ws.on("close", () => state.sockets.get(name).delete(ws));
      ws.on("message", (data, isBinary) => {
        if (isBinary) return;
        const s = data.toString();
        if (s.startsWith("{") && /"ping"/.test(s)) ws.send(JSON.stringify({ type: "pong" }));
      });
      setTimeout(() => {
        ws.send(Buffer.from(screenFor(name, null).out, "utf8"));
        if (name === "agent") setTimeout(() => ws.send(JSON.stringify(WAITING_PROMPT)), 150);
      }, 60);
    });
  });
  return new Promise((resolve) => server.listen(0, "127.0.0.1", () =>
    resolve({ server, state, url: `http://127.0.0.1:${server.address().port}` })));
}

// ---------------------------------------------------------------- chrome
function findChrome() {
  const cache = path.join(os.homedir(), ".cache", "puppeteer", "chrome");
  let dirs = [];
  try { dirs = fs.readdirSync(cache); } catch (e) {}
  const v = dirs.map((d) => {
    const m = /^linux-(\d+)\.(\d+)\.(\d+)\.(\d+)$/.exec(d);
    const bin = path.join(cache, d, "chrome-linux64", "chrome");
    return m && fs.existsSync(bin) ? { bin, key: m.slice(1).map(Number) } : null;
  }).filter(Boolean).sort((a, b) => { for (let i = 0; i < 4; i++) if (a.key[i] !== b.key[i]) return b.key[i] - a.key[i]; return 0; });
  if (v.length) return v[0].bin;
  for (const p of ["/usr/bin/google-chrome", "/usr/bin/google-chrome-stable", "/usr/bin/chromium"]) if (fs.existsSync(p)) return p;
  throw new Error("no Chrome found");
}

// ------------------------------------------------------------- UI driving
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function makeUI(page) {
  const ui = {
    page,
    async settle() {
      await page.evaluate(async () => {
        await document.fonts.ready;
        if (typeof term !== "undefined" && term) await new Promise((r) => term.write("", r));
        await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
      });
    },
    async wait(sel, timeout = 10000) {
      await page.waitForFunction((s) => {
        const e = document.querySelector(s);
        if (!e) return false;
        const r = e.getBoundingClientRect();
        return r.width > 0 && r.height > 0 && getComputedStyle(e).visibility !== "hidden";
      }, { timeout, polling: 100 }, sel).catch((e) => { throw new Error("wait " + sel + ": " + e.message); });
    },
    async waitFn(fn, arg, timeout = 10000) {
      await page.waitForFunction(fn, { timeout, polling: 100 }, arg).catch((e) => { throw new Error("waitFn " + String(fn).slice(0, 90) + ": " + e.message); });
    },
    async click(sel, opts = {}) { await ui.wait(sel); await page.click(sel, opts); await sleep(opts.after ?? 250); },
    async rightClick(sel) { await ui.wait(sel); await page.click(sel, { button: "right" }); await sleep(300); },
    async hover(sel) { await ui.wait(sel); await page.hover(sel); await sleep(250); },
    async key(combo) {
      const parts = combo.split("+");
      const k = parts.pop();
      for (const m of parts) await page.keyboard.down(m);
      await page.keyboard.press(k);
      for (const m of parts.reverse()) await page.keyboard.up(m);
      await sleep(300);
    },
    async type(sel, text) { await ui.click(sel); await page.keyboard.type(text, { delay: 15 }); await sleep(300); },
    // The terminal's grid geometry is read (not changed) so a hover can land on a cell.
    async cellOf(text) {
      return page.evaluate((t) => {
        const buf = term.buffer.active;
        for (let y = 0; y < buf.length; y++) {
          const s = buf.getLine(y).translateToString(true);
          const i = s.indexOf(t);
          if (i >= 0) {
            const scr = $("term-host").querySelector(".xterm-screen").getBoundingClientRect();
            const cw = scr.width / term.cols, ch = scr.height / term.rows;
            const vy = y - buf.viewportY;
            return { x: scr.left + (i + Math.min(t.length, 6) / 2) * cw, y: scr.top + (vy + 0.5) * ch };
          }
        }
        return null;
      }, text);
    },
    async openSession(name) {
      await ui.click(`#list .item[data-name="${name}"]`);
      await ui.waitFn((n) => typeof term !== "undefined" && term && currentSession === n &&
        term.buffer.active.getLine(0) && term.buffer.active.getLine(0).translateToString(true).length > 0, name);
      await ui.remeasure();
      await ui.settle();
    },
    async remeasure() {
      await page.evaluate(() => {
        term.options.cursorBlink = false;
        const f = term.options.fontSize;
        term.options.fontSize = f + 1;
        term.options.fontSize = f;
        refit(0);
      });
      await page.waitForFunction(() => {
        const cv = $("term-host").querySelector(".xterm-screen canvas");
        return cv && Math.abs(cv.width - cv.clientWidth * devicePixelRatio) < 2;
      }, { timeout: 10000, polling: 100 }).catch(() => {});
      await sleep(250);
    },
    async openSettings(tab) {
      await ui.click("#btn-settings");
      await ui.wait("#sheet-settings.show, #sheet-settings.open, #sheet-settings[open]").catch(() => {});
      if (tab) await ui.click(`#settings-tabs [data-tab="${tab}"]`);
      await sleep(300);
    },
    async pill(cls) { await ui.click(`#keybar .${cls}`); },
    async findText(sel, text) {
      await page.waitForFunction((s, t) => [...document.querySelectorAll(s)].some((e) => e.getBoundingClientRect().width > 0 &&
        e.textContent.trim().startsWith(t)), { timeout: 10000, polling: 100 }, sel, text)
        .catch((e) => { throw new Error("findText " + sel + " " + text + ": " + e.message); });
      const hs = await page.$$(sel);
      for (const h of hs) {
        const ok = await h.evaluate((e, t) => e.getBoundingClientRect().width > 0 && e.textContent.trim().startsWith(t), text);
        if (ok) return h;
      }
      throw new Error("no " + sel + " with " + text);
    },
    async clickText(sel, text, opts = {}) { const h = await ui.findText(sel, text); await h.click(opts); await sleep(opts.after ?? 500); },
  };
  return ui;
}

// ------------------------------------------------------------- the shots
// run(ui) drives the page into the state; features = catalog ids; caption is
// the image's alt text on the features page.
const RAIL_CLOSED = { pockettui_rail_keys: "closed" };
const inSession = async (ui, name = "webapp") => { await ui.openSession(name); };
const dockFiles = async (ui) => {
  await inSession(ui);
  await ui.pill("k-files");
  await ui.waitFn(() => document.querySelectorAll("#files-list .file-row, #files-list .file-tile").length > 3);
  await ui.settle();
};
const dockBrowser = async (ui, url = "localhost:5173") => {
  await inSession(ui);
  await ui.pill("k-browser");
  await ui.wait("#browser-url");
  await ui.type("#browser-url", url);
  await ui.page.keyboard.press("Enter");
  await ui.waitFn(() => { const f = document.querySelector("#browser-wrap iframe"); return f && f.src && f.src.includes("/b/"); });
  await sleep(900);
};
const openDiff = async (ui) => {
  await inSession(ui);
  await ui.key("Control+Shift+KeyG");
  await ui.waitFn(() => document.querySelectorAll("#diff-files > *").length > 0);
  await sleep(600);
};

const SHOTS = {
  "rail-states": {
    ...M, seed: RAIL_CLOSED,
    features: ["sessions.list", "sessions.row-title", "notify.state-badges", "notify.unread", "notify.bell-toggle", "sessions.refresh", "sessions.multi-device", "sessions.jump-shortcut"],
    caption: "The session sidebar: every session with its program and folder, Running, Needs input and Done badges, an unread session in bold, and a bell per row.",
    async run(ui) { await ui.click("#btn-reload", { after: 800 }); await inSession(ui); },
  },
  "rail-shortcuts": {
    ...M,
    features: ["keys.shortcut-list", "terminal.shift-enter", "terminal.ctrl-c-copy", "terminal.drag-select", "terminal.copy-shortcut", "terminal.right-click-copy"],
    caption: "The Shortcuts card in the sidebar lists the keyboard shortcuts: copy on release, paste, interrupt or copy, newline, new session, changes and search.",
    async run(ui) { await inSession(ui); },
  },
  "new-session": {
    ...M, seed: RAIL_CLOSED,
    features: ["sessions.new"],
    caption: "Starting a new session: the name field shows the name it will get if left empty.",
    async run(ui) { await ui.click("#btn-new", { after: 500 }); },
  },
  "new-session-key": {
    ...M, seed: RAIL_CLOSED,
    features: ["sessions.new-shortcut"],
    caption: "Ctrl+Shift+L made a new session with a dated name and opened it.",
    async run(ui) { await inSession(ui); await ui.key("Control+Shift+KeyL"); await sleep(1200); await ui.remeasure(); },
  },
  "session-sheet": {
    ...M, seed: RAIL_CLOSED,
    features: ["sessions.rename-alias", "sessions.kill"],
    caption: "The rename sheet for a session: a display name, the tmux name, and Kill.",
    async run(ui) { await inSession(ui); await ui.click('#list .item[data-name="api"] .btn-alias', { after: 500 }); },
  },
  "search": {
    ...M, seed: RAIL_CLOSED,
    features: ["sessions.scrollback-search"],
    caption: "Searching the terminal's history with Ctrl+Shift+F: matches are highlighted and counted.",
    async run(ui) {
      await inSession(ui);
      await ui.key("Control+Shift+KeyF");
      await ui.page.keyboard.type("test", { delay: 30 });
      await ui.waitFn(() => /match/.test($("search-count").textContent));
      await sleep(400);
    },
  },
  "key-pill": {
    ...M, seed: RAIL_CLOSED,
    features: ["links.paths"], also: ["links.urls"],
    caption: "A path printed in the terminal becomes a link under the pointer; the key pill at the top right holds the mic, files, browser and report keys.",
    async run(ui) {
      await inSession(ui);
      const at = await ui.cellOf("~/projects/webapp/docs/notes.md");
      await ui.page.mouse.move(at.x + 40, at.y);
      await sleep(600);
    },
  },
  "prompt-chips": {
    ...M, seed: RAIL_CLOSED,
    features: ["notify.prompt-chips"],
    caption: "A session waiting on a yes/no question: one-tap answer chips appear above the terminal.",
    async run(ui) { await ui.click("#btn-reload", { after: 600 }); await inSession(ui, "agent"); await ui.wait("#chips.show"); await ui.remeasure(); },
  },
  "settings-connection": {
    ...M, seed: RAIL_CLOSED,
    features: ["connect.add-computer", "connect.switch-computer", "connect.pairing-code", "connect.rename-computer", "connect.device-name", "connect.port-field", "connect.forget-computer"],
    caption: "Settings, Connection: the computer chooser open with two computers and Add another computer, above the address, pairing code and names.",
    async run(ui) { await ui.openSettings("connection"); await ui.click("#btn-profile-pick", { after: 400 }); },
  },
  "settings-pair": {
    ...M, seed: RAIL_CLOSED,
    features: ["connect.pair-qr"],
    caption: "Settings, Connection: the QR code another phone or laptop scans to pair with this computer.",
    async run(ui) { await ui.openSettings("connection"); await ui.page.evaluate(() => $("sheet-pair").scrollIntoView({ block: "center" })); await sleep(400); },
  },
  "settings-browser": {
    ...M, seed: RAIL_CLOSED,
    features: ["browser.stream-all", "browser.clear-data"],
    caption: "Settings, Connection, Browser: stream every tab from the computer's Chrome, and clear its browsing data.",
    async run(ui) { await ui.openSettings("connection"); await ui.page.evaluate(() => $("browser-setting").scrollIntoView({ block: "center" })); await sleep(500); },
  },
  "settings-dictation": {
    ...M, seed: RAIL_CLOSED,
    features: ["dictation.engine", "dictation.learned", "dictation.phone-dictation", "dictation.auto-fallback"],
    caption: "Settings, Dictation: the speech engine choice and the corrections dictation has learned.",
    async run(ui) { await ui.openSettings("dictation"); await ui.waitFn(() => $("learned-rows").children.length > 0).catch(() => {}); },
  },
  "settings-keys": {
    ...M, seed: RAIL_CLOSED,
    features: ["keys.text-size"], also: ["keys.shortcut-list"],
    caption: "Settings, Keys: text size buttons for the terminal and sidebar, and every keyboard shortcut.",
    async run(ui) { await ui.openSettings("keys"); },
  },
  "settings-appearance": {
    ...M, seed: RAIL_CLOSED,
    features: ["theme.palettes", "theme.import", "theme.min-contrast"], also: ["theme.app-theme"],
    caption: "Settings, Appearance: app theme, terminal colour schemes and the box to paste your own.",
    async run(ui) { await ui.openSettings("appearance"); },
  },
  "settings-about": {
    ...M, seed: RAIL_CLOSED,
    features: ["connect.version", "connect.update", "hood.about", "hood.debug-log"], also: ["hood.report"],
    caption: "Settings, About: the app and server versions, the Update server button, the debug log switch and Report a problem.",
    async run(ui) { await ui.openSettings("about"); },
  },
  "update-failed": {
    ...M, seed: RAIL_CLOSED, updateFailed: true,
    features: ["connect.type-it", "connect.update-resume"],
    caption: "After an update that did not take, Settings, About offers Type it for me to put the retry command in a terminal.",
    async run(ui) { await ui.openSettings("about"); await ui.page.evaluate(() => $("btn-update-type").scrollIntoView({ block: "center" })); await sleep(300); },
  },
  "theme-menu": {
    ...M, seed: RAIL_CLOSED,
    features: ["theme.app-theme"],
    caption: "The theme button in the header: light, dark or follow the system.",
    async run(ui) { await inSession(ui); await ui.click("#btn-theme", { after: 400 }); },
  },
  "report": {
    ...M, seed: RAIL_CLOSED,
    features: ["hood.report", "hood.report-diagnostics"],
    caption: "Report a problem, with the diagnostics that will be sent shown before sending.",
    async run(ui) {
      await inSession(ui);
      await ui.pill("k-report");
      await ui.wait("#report-msg");
      await ui.type("#report-msg", "The file pane stopped following my cd into ~/projects/api-service.");
      await ui.click("#report-diag-details summary", { after: 400 }).catch(() => {});
    },
  },
  "first-run": {
    ...M, unpaired: true,
    features: ["connect.first-run", "connect.install-tailscale"],
    caption: "First run: step one is the command to run on the computer, with the Tailscale steps above it.",
    async run(ui) { await sleep(800); },
  },
  "explorer": {
    ...M, seed: RAIL_CLOSED,
    features: ["files.explorer", "panes.side-column", "files.follow-cwd"],
    caption: "The file browser docked beside the terminal, opened on the session's own folder.",
    async run(ui) { await dockFiles(ui); },
  },
  "explorer-grid": {
    ...M, seed: RAIL_CLOSED,
    features: ["files.thumbnails"], also: ["files.view-sort"],
    caption: "Grid view in the file browser, with picture previews for images.",
    async run(ui) {
      await dockFiles(ui);
      await ui.click("#btn-files-view");
      await ui.click('#files-view-menu [data-view="grid"]');
      await ui.clickText("#files-list .file-tile, #files-list .file-row", "assets");
      await ui.waitFn(() => document.querySelectorAll("#files-list img.file-thumb").length >= 3);
      await sleep(500);
    },
  },
  "explorer-view-menu": {
    ...M, seed: RAIL_CLOSED,
    features: ["files.view-sort"],
    caption: "The layout pop-up: list or grid, sorted by name, date or size.",
    async run(ui) { await dockFiles(ui); await ui.click("#btn-files-view", { after: 400 }); },
  },
  "explorer-branch": {
    ...M, seed: RAIL_CLOSED,
    features: ["files.branch-browse"],
    caption: "The branch pop-up in the path bar: read another branch's files without checking it out.",
    async run(ui) { await dockFiles(ui); await ui.click("#btn-files-ref", { after: 400 }); },
  },
  "explorer-path": {
    ...M, seed: RAIL_CLOSED,
    features: ["files.path-bar"],
    caption: "Typing a path in the file browser's path bar, with folder suggestions.",
    async run(ui) {
      await dockFiles(ui);
      await ui.click("#btn-files-edit-path");
      await ui.page.keyboard.down("Control"); await ui.page.keyboard.press("KeyA"); await ui.page.keyboard.up("Control");
      await ui.page.keyboard.type("~/projects/", { delay: 30 });
      await sleep(800);
    },
  },
  "explorer-more": {
    ...M, seed: RAIL_CLOSED,
    features: ["panes.more-menu"], also: ["panes.two-stacked", "panes.same-type-twice"],
    caption: "The More menu in a docked pane: open another pane in this column, including a second of the same kind.",
    async run(ui) { await dockFiles(ui); await ui.click("#screen-files .dock-split", { after: 400 }); },
  },
  "explorer-actions": {
    ...M, seed: RAIL_CLOSED,
    features: ["files.rename", "files.delete", "files.download", "files.download-folder"],
    caption: "Right-clicking a folder in the file browser: Download, Rename and Delete.",
    async run(ui) { await dockFiles(ui); await ui.clickText("#files-list .file-row", "docs", { button: "right" }); await sleep(400); },
  },
  "explorer-add": {
    ...M, seed: RAIL_CLOSED,
    features: ["files.new", "files.upload"],
    caption: "The + menu in the file browser: new file, new folder, upload.",
    async run(ui) { await dockFiles(ui); await ui.click("#btn-files-add", { after: 400 }); },
  },
  "explorer-drag": {
    ...M, seed: RAIL_CLOSED,
    features: ["files.drag-move"],
    caption: "Dragging a file onto a folder in the file browser to move it there.",
    async run(ui) {
      await dockFiles(ui);
      const src = await ui.findText("#files-list .file-row", "README.md");
      const dst = await ui.findText("#files-list .file-row", "docs");
      const data = await src.drag(dst);
      await dst.dragEnter(data);
      await dst.dragOver(data);
      await sleep(300);
    },
  },
  "editor": {
    ...L, seed: RAIL_CLOSED,
    features: ["files.editor", "files.docked-views", "files.wrap", "editor.vim", "files.save-conflict"],
    caption: "A file open in the editor beside the terminal, with Wrap, Vim and Save in its bar.",
    async run(ui) {
      await dockFiles(ui);
      await ui.clickText("#files-list .file-row", "src");
      await ui.clickText("#files-list .file-row", "router.js");
      await ui.waitFn(() => !!document.querySelector("#screen-editor .cm-content"), null, 20000);
      await sleep(600);
    },
  },
  "markdown": {
    ...M, seed: RAIL_CLOSED,
    features: ["files.markdown"],
    caption: "A Markdown file in the reader: formatted text, a formula and a task list.",
    async run(ui) {
      await dockFiles(ui);
      await ui.clickText("#files-list .file-row", "docs");
      await ui.clickText("#files-list .file-row", "notes.md");
      await ui.waitFn(() => !!document.querySelector("#reader-body .katex"), null, 20000);
      await sleep(600);
    },
  },
  "viewer": {
    ...M, seed: RAIL_CLOSED,
    features: ["files.media-viewer"],
    caption: "A picture opened from the file browser, with the viewer expanded over the terminal; pinch, drag or double-tap to zoom.",
    async run(ui) {
      await dockFiles(ui);
      await ui.clickText("#files-list .file-row", "assets");
      await ui.clickText("#files-list .file-row", "screenshot-login.png");
      await ui.waitFn(() => { const i = $("viewer-img"); return i && i.complete && i.naturalWidth > 0; }, null, 15000);
      await ui.click("#viewer .dock-expand", { after: 900 });
    },
  },
  "diff": {
    ...M, seed: RAIL_CLOSED,
    features: ["git.pane", "git.block-actions", "git.stage-file", "git.discard-file", "git.resize-list"],
    caption: "The Changes pane (Ctrl+Shift+G): changed files with stage and discard buttons, and the diff with a keep or undo control on each block.",
    async run(ui) {
      await openDiff(ui);
      await ui.clickText("#diff-files *", "src/router.js").catch(() => {});
      await sleep(600);
      const hunk = await ui.page.$("#diff-body .hunk, #diff-body [class*=hunk]");
      if (hunk) { await hunk.hover(); await sleep(300); }
    },
  },
  "diff-untracked": {
    ...M, seed: RAIL_CLOSED,
    features: ["git.tracked-untracked"],
    caption: "The Untracked tab of the Changes pane: files git has never seen, listed apart from tracked changes.",
    async run(ui) {
      await openDiff(ui);
      await ui.click("#btn-diff-untracked", { after: 900 });
      await ui.clickText("#diff-files *", "scripts/snapshot.js").catch(() => {});
      await sleep(600);
    },
  },
  "browser": {
    ...M, seed: RAIL_CLOSED,
    features: ["browser.open", "browser.tabs", "browser.bookmarks", "browser.address-search", "links.wide-to-pane", "browser.open-outside", "browser.stream", "browser.local-network"],
    caption: "The browser pane showing a dev server on the computer: tabs, the address field, bookmarks bar, and the stream and network keys.",
    async run(ui) { await dockBrowser(ui); },
  },
  "browser-more": {
    ...M, seed: RAIL_CLOSED,
    features: ["browser.zoom"],
    caption: "The browser pane's More menu: another pane in this column, and zoom in, out or actual size.",
    async run(ui) { await dockBrowser(ui); await ui.click("#screen-browser .dock-split", { after: 400 }); },
  },
  "two-panes": {
    ...L, seed: RAIL_CLOSED,
    features: ["panes.two-stacked", "panes.resize-rows", "panes.swap", "panes.resize-column"],
    caption: "Two tools stacked beside the terminal: the file browser above the browser, with the seam between them.",
    async run(ui) {
      await dockFiles(ui);
      await ui.click("#screen-files .dock-split");
      await ui.clickText("#screen-files .dock-split-menu .view-row", "Browser");
      await ui.wait("#browser-url");
      await ui.type("#browser-url", "localhost:5173");
      await ui.page.keyboard.press("Enter");
      await sleep(1200);
    },
  },
  "two-explorers": {
    ...L, seed: RAIL_CLOSED,
    features: ["panes.same-type-twice", "files.two-explorers-sync"],
    caption: "Two file browsers in one column, each on its own folder.",
    async run(ui) {
      await dockFiles(ui);
      await ui.click("#screen-files .dock-split");
      await ui.clickText("#screen-files .dock-split-menu .view-row", "Files");
      await ui.clickText("#screen-files-2 .file-row", "src");
      await sleep(900);
    },
  },
};

// ------------------------------------------------------------- capture
async function capture(ctx, srv, id, spec, theme, outPng) {
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  await page.setRequestInterception(true);
  page.on("request", (req) => {
    const u = req.url();
    if (u.startsWith(PUBLIC) || u.startsWith("data:") || u.startsWith("blob:")) return req.continue();
    if (u.startsWith("https://pockettui.com/version.txt")) return req.respond({ status: 200, contentType: "text/plain", body: LATEST_VERSION + "\n", headers: { "Access-Control-Allow-Origin": "*" } });
    req.abort();
  });
  await page.setViewport({ width: spec.width, height: spec.height, deviceScaleFactor: SCALE, isMobile: false, hasTouch: false });
  srv.state.sessionsAsked = 0;
  srv.state.made = [];
  srv.state.updateStatus = spec.updateFailed ? { status: "failed", ts: Math.floor(Date.now() / 1000) - 600, from: SERVER_VERSION, to: LATEST_VERSION } : null;
  const profiles = [
    { id: "p-dev", name: "", host: HOST, backend: PUBLIC, token: FAKE_TOKEN },
    { id: "p-build", name: "build-box", host: "build-box", backend: "http://build-box.lan:5560", token: "QRSTU67ABC" },
  ];
  const seed = {
    pockettui_theme: theme,
    pockettui_a2hs_dismissed: "1",
    pockettui_fontsize: String(FONT),
    pockettui_devname: "alice-laptop",
    ...(spec.unpaired ? {} : {
      pockettui_profiles: JSON.stringify(profiles),
      pockettui_profile: "p-dev",
      pockettui_backend: PUBLIC,
      pockettui_token: FAKE_TOKEN,
    }),
    ...(spec.seed || {}),
  };
  await page.evaluateOnNewDocument((s) => { for (const [k, v] of Object.entries(s)) localStorage.setItem(k, v); }, seed);
  await page.goto(PUBLIC + "/", { waitUntil: "load" });
  const ui = makeUI(page);
  if (!spec.unpaired) await page.waitForFunction(() => $("list").querySelectorAll(".item[data-name]").length > 0, { timeout: 10000 });
  await ui.settle();
  try {
    await spec.run(ui);
  } catch (e) {
    await page.screenshot({ path: outPng.replace(/\.png$/, ".FAIL.jpg") }).catch(() => {});
    await page.close();
    throw e;
  }
  // Opening a pane or a sheet re-fits the terminal onto the emulator's wrong
  // pixel ratio (see landing_shots.mjs remeasureTerm): measure once more, last.
  if (await page.evaluate(() => typeof term !== "undefined" && !!term && $("screen-term").classList.contains("active"))) await ui.remeasure();
  await sleep(spec.pause ?? 300);
  await ui.settle();
  await page.screenshot({ path: outPng, captureBeyondViewport: false });
  await page.close();
  if (errors.length) console.warn(`  ! ${id} ${theme}: page errors: ${errors.join(" | ")}`);
}

function parseArgs(argv) {
  const out = { only: null, themes: ["dark", "light"], debug: null };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--only") out.only = argv[++i].split(",").map((s) => s.trim()).filter(Boolean);
    else if (a === "--theme") out.themes = [argv[++i]];
    else if (a === "--debug") out.debug = path.resolve(argv[++i]);
    else if (a === "--list") out.list = true;
    else throw new Error("unknown argument: " + a);
  }
  return out;
}

// catalog.json gets one `shot` per record: the shot that lists it under
// `features` (its primary illustration; `also` entries are only in the
// manifest). Only shots present in the manifest count; everything else null.
// No other field is touched, and the file keeps its one-space indent.
function writeCatalogShots(man) {
  const catPath = path.join(FEATURES_DIR, "catalog.json");
  const cat = JSON.parse(fs.readFileSync(catPath, "utf8"));
  const primary = new Map();
  for (const [id, spec] of Object.entries(SHOTS)) {
    if (!(id in man)) continue;
    for (const f of spec.features) {
      if (primary.has(f)) console.warn(`  ! ${f} is primary in both ${primary.get(f)} and ${id}`);
      else primary.set(f, id);
    }
  }
  const ids = new Set(cat.map((r) => r.id));
  for (const f of primary.keys()) if (!ids.has(f)) console.warn(`  ! unknown catalog id in shots: ${f}`);
  for (const r of cat) r.shot = primary.get(r.id) || null;
  fs.writeFileSync(catPath, JSON.stringify(cat, null, 1));
  console.log(`catalog.json: ${cat.filter((r) => r.shot).length} of ${cat.length} records have a shot`);
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.list) { console.log(Object.keys(SHOTS).join("\n")); return; }
  const names = args.only || Object.keys(SHOTS);
  for (const n of names) if (!(n in SHOTS)) throw new Error("unknown shot: " + n);
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "feature-shots-"));
  const pngDir = args.debug || path.join(tmp, "png");
  await fsp.mkdir(pngDir, { recursive: true });
  const tree = buildTree(tmp);
  const srv = await startServer(tree);
  const browser = await puppeteer.launch({
    executablePath: findChrome(), headless: true,
    args: ["--no-sandbox", "--force-color-profile=srgb", "--font-render-hinting=none", "--hide-scrollbars",
      "--enable-unsafe-swiftshader",
      `--host-resolver-rules=MAP ${HOST}:5560 127.0.0.1:${new URL(srv.url).port}`,
      "--blink-settings=primaryPointerType=4,availablePointerTypes=4,primaryHoverType=2,availableHoverTypes=2"],
  });
  const made = [];
  const failed = [];
  try {
    for (const id of names) {
      for (const theme of args.themes) {
        const png = path.join(pngDir, `${id}-${theme}.png`);
        try {
          // A fresh context per capture: localStorage (layout, sort, pane
          // widths) must not carry from one shot into the next.
          const ctx = await browser.createBrowserContext();
          try { await capture(ctx, srv, id, SHOTS[id], theme, png); } finally { await ctx.close(); }
          made.push(png);
          console.log(`  ${id}-${theme}`);
        } catch (e) {
          failed.push(id);
          console.warn(`  x ${id} ${theme}: ${e.message.split("\n")[0]}\n${(e.stack || "").split("\n").slice(1, 4).join("\n")}`);
        }
      }
    }
  } finally {
    await browser.close();
    srv.server.close();
  }
  if (args.debug) { console.log(`${made.length} PNGs in ${pngDir}`); return; }
  await fsp.mkdir(OUT_DIR, { recursive: true });
  execFileSync("python3", ["-c", String.raw`
import sys, os
from PIL import Image
src, dst = sys.argv[1], sys.argv[2]
for f in sorted(os.listdir(src)):
    if f.endswith(".png"):
        Image.open(os.path.join(src, f)).convert("RGB").save(os.path.join(dst, f[:-4] + ".webp"), "WEBP", quality=82, method=6)
`, pngDir, OUT_DIR]);
  const manPath = path.join(FEATURES_DIR, "manifest.json");
  let man = {};
  try { man = JSON.parse(fs.readFileSync(manPath, "utf8")); } catch (e) {}
  for (const id of names) {
    if (failed.includes(id)) { delete man[id]; continue; }
    const s = SHOTS[id];
    man[id] = { width: s.width * SCALE, height: s.height * SCALE, features: [...s.features, ...(s.also || [])], caption: s.caption };
  }
  // Shots no longer defined are dropped with their files.
  for (const id of Object.keys(man)) if (!(id in SHOTS)) delete man[id];
  fs.writeFileSync(manPath, JSON.stringify(man, null, 1) + "\n");
  writeCatalogShots(man);
  fs.rmSync(tmp, { recursive: true, force: true });
  console.log(`${made.length} images -> ${OUT_DIR}; failed: ${[...new Set(failed)].join(", ") || "none"}`);
}

main().catch((e) => { console.error(e); process.exit(1); });
