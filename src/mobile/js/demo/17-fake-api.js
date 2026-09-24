// ---- the demo's backend -------------------------------------------------
// The explorer, the editor and the reader are plain clients of /api/fs/*, so
// the demo gives them one rather than a second set of screens: while the app
// is in the demo — a demo terminal is open, or the device is unpaired and the
// session list is the demo's — fetch() calls to the routes below are answered
// here, in memory, out of DEMO_FS. The shapes are app.py's own (api_fs_list,
// api_fs_read, api_fs_write, mkdir, rename, delete, session_cwd), so every
// screen runs its real code path against them. Writes change the tree the demo
// shell's ls and cat read too, for this visit only: a reload puts it back.
//
// Every other request goes to the network untouched, and once the device is
// paired and out of the demo nothing here runs at all.
const DEMO_HOME = "/home/" + DEMO_USER;

function demoApiOn() { return demoMode || needsSetup(); }

// The server deals in absolute paths and hands `home` back so the client can
// shorten them; DEMO_FS's root key "home" is mounted at DEMO_HOME. Returns the
// tree path as an array (["home", ...]), or null for anything outside it.
function demoApiParts(p) {
  let s = String(p || "").trim();
  if (!s || s === "~") s = DEMO_HOME;
  else if (s.startsWith("~/")) s = DEMO_HOME + s.slice(1);
  if (s !== DEMO_HOME && !s.startsWith(DEMO_HOME + "/")) return null;
  const out = ["home"];
  for (const seg of s.slice(DEMO_HOME.length).split("/")) {
    if (!seg || seg === ".") continue;
    if (seg === "..") { if (out.length > 1) out.pop(); continue; }
    out.push(seg);
  }
  return out;
}

function demoApiAbs(parts) {
  return parts.length > 1 ? DEMO_HOME + "/" + parts.slice(1).join("/") : DEMO_HOME;
}

// Fixed and spread by name rather than read off the clock, so the listing's
// "3h ago" column is a believable spread and not one repeated value. A write
// stamps the moment it happened.
const demoApiTouched = new Map();
function demoApiMtime(abs) {
  if (demoApiTouched.has(abs)) return demoApiTouched.get(abs);
  let h = 0;
  for (let i = 0; i < abs.length; i++) h = (h * 31 + abs.charCodeAt(i)) >>> 0;
  return Math.floor(Date.now() / 1000) - 3600 - (h % 240) * 1800;
}

// The write-conflict token. Content-derived like the server's sha256, but
// FNV-1a: crypto.subtle is missing on the plain-http origins a demo can be
// opened from, and nothing here needs more than "same text, same token".
function demoApiHash(text) {
  let h = 0x811c9dc5;
  for (let i = 0; i < text.length; i++) {
    h ^= text.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return "demo-" + h.toString(16);
}

function demoApiSize(text) { return new TextEncoder().encode(text).length; }

function demoApiReply(status, body) {
  return Promise.resolve(new Response(JSON.stringify(body), {
    status: status,
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
  }));
}

function demoApiList(q) {
  const parts = demoApiParts(q.get("path"));
  // A branch is a real repo's; the demo's folders have none.
  if (!parts || q.get("ref")) return demoApiReply(404, { error: "not_found" });
  const node = demoNodeAt(parts);
  if (node === undefined) return demoApiReply(404, { error: "not_found" });
  if (!demoIsDir(node)) return demoApiReply(400, { error: "not_a_directory" });
  const abs = demoApiAbs(parts);
  const entries = Object.keys(node).map((name) => {
    const v = node[name];
    return {
      name: name,
      type: demoIsDir(v) ? "dir" : "file",
      size: demoIsDir(v) ? 0 : demoApiSize(v),
      mtime: demoApiMtime(abs + "/" + name),
    };
  });
  entries.sort((a, b) => (a.type !== "dir") - (b.type !== "dir")
    || a.name.toLowerCase().localeCompare(b.name.toLowerCase())
    || a.name.localeCompare(b.name));
  return demoApiReply(200, { path: abs, home: DEMO_HOME, entries: entries });
}

function demoApiRead(q) {
  const parts = demoApiParts(q.get("path"));
  const node = parts && !q.get("ref") ? demoNodeAt(parts) : undefined;
  if (typeof node !== "string") return demoApiReply(404, { error: "not_found" });
  const abs = demoApiAbs(parts);
  return demoApiReply(200, {
    path: abs, content: node, hash: demoApiHash(node),
    size: demoApiSize(node), mtime: demoApiMtime(abs), lossy: false,
  });
}

// The folder a path would be created in, and its name there — or null where
// that folder is not in the tree.
function demoApiSlot(p) {
  const parts = demoApiParts(p);
  if (!parts || parts.length < 2) return null;
  const dir = demoNodeAt(parts.slice(0, -1));
  if (!demoIsDir(dir)) return null;
  return { dir: dir, name: parts[parts.length - 1], abs: demoApiAbs(parts) };
}

function demoApiWrite(b) {
  const slot = demoApiSlot(b.path);
  if (typeof b.content !== "string") return demoApiReply(400, { error: "bad_content" });
  if (!slot) return demoApiReply(404, { error: "not_found" });
  const cur = slot.dir[slot.name];
  if (demoIsDir(cur)) return demoApiReply(400, { error: "not_a_file" });
  const now = typeof cur === "string" ? demoApiHash(cur) : "";
  if (now !== String(b.hash || "")) return demoApiReply(409, { error: "conflict", hash: now });
  slot.dir[slot.name] = b.content;
  const t = Math.floor(Date.now() / 1000);
  demoApiTouched.set(slot.abs, t);
  return demoApiReply(200, { hash: demoApiHash(b.content), mtime: t });
}

function demoApiMkdir(b) {
  const slot = demoApiSlot(b.path);
  if (!slot) return demoApiReply(404, { error: "not_found" });
  if (slot.name in slot.dir) return demoApiReply(409, { error: "exists" });
  slot.dir[slot.name] = {};
  demoApiTouched.set(slot.abs, Math.floor(Date.now() / 1000));
  return demoApiReply(200, { path: slot.abs });
}

function demoApiRename(b) {
  const from = demoApiSlot(b.src), to = demoApiSlot(b.dst);
  if (!from || !(from.name in from.dir)) return demoApiReply(404, { error: "not_found" });
  if (!to) return demoApiReply(404, { error: "not_found" });
  if (to.name in to.dir) return demoApiReply(409, { error: "exists" });
  // A folder cannot be moved into itself: the OS refuses that too.
  if (to.abs.startsWith(from.abs + "/")) return demoApiReply(403, { error: "not_writable" });
  to.dir[to.name] = from.dir[from.name];
  delete from.dir[from.name];
  demoApiTouched.set(to.abs, Math.floor(Date.now() / 1000));
  return demoApiReply(200, { path: to.abs });
}

function demoApiDelete(b) {
  const slot = demoApiSlot(b.path);
  if (!slot || !(slot.name in slot.dir)) return demoApiReply(404, { error: "not_found" });
  const node = slot.dir[slot.name];
  // A folder only when it is empty, the route's own rule.
  if (demoIsDir(node) && Object.keys(node).length) return demoApiReply(409, { error: "not_empty" });
  delete slot.dir[slot.name];
  return demoApiReply(200, { deleted: slot.abs });
}

// Route -> answer. Anything else on these prefixes is a thing the demo cannot
// do (a thumbnail, a signed link, an upload, a branch list): answered 404 so
// the caller falls back the way it does against a server without the route.
// The user-facing ones say "Not in the demo" at their own entry points.
const DEMO_API_GET = {
  "fs/list": demoApiList,
  "fs/read": demoApiRead,
  "session_cwd": () => demoApiReply(200, { cwd: demoApiAbs(demoCwd) }),
};
const DEMO_API_POST = {
  "fs/write": demoApiWrite,
  "fs/mkdir": demoApiMkdir,
  "fs/rename": demoApiRename,
  "fs/delete": demoApiDelete,
};
const DEMO_API_PREFIXES = /^(?:fs\/|git\/|file_link$|session_cwd$|browse\/bookmarks$)/;

function demoApiAnswer(input, init) {
  let u;
  try { u = new URL(typeof input === "string" ? input : input.url, location.href); }
  catch (e) { return null; }
  const m = /\/api\/(.+)$/.exec(u.pathname);
  if (!m || !DEMO_API_PREFIXES.test(m[1])) return null;
  const method = String((init && init.method) || "GET").toUpperCase();
  if (method === "GET" && DEMO_API_GET[m[1]]) return DEMO_API_GET[m[1]](u.searchParams);
  if (method === "POST" && DEMO_API_POST[m[1]]) {
    let body = {};
    try { body = JSON.parse((init && init.body) || "{}") || {}; } catch (e) {}
    return DEMO_API_POST[m[1]](body);
  }
  return demoApiReply(404, { error: "not_in_demo" });
}

// The one seam: every screen above calls fetch(apiURL(...)) directly, so the
// demo stands in there rather than in each of them.
const demoApiFetch = window.fetch.bind(window);
window.fetch = function (input, init) {
  if (demoApiOn()) {
    const hit = demoApiAnswer(input, init);
    if (hit) return hit;
  }
  return demoApiFetch(input, init);
};

// The browser pane's side of the demo: one tab on an invented dev server, and
// a note for every other address. Both are documents written here and handed
// to the frame as srcdoc, so nothing is fetched; the frame's sandbox still
// applies, and neither page runs a script.
const DEMO_DEV_URL = "http://localhost:5173/";

// The style element's tags are spelled in halves: this script is inlined into
// the shell's one document, whose build checks count those tags, and the parser
// supplies the html, head and body elements a srcdoc leaves out.
const DEMO_STYLE_OPEN = "<" + "style>", DEMO_STYLE_CLOSE = "</" + "style>";

function demoPageShell(title, body) {
  return "<!doctype html><meta charset=\"utf-8\">" +
    "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">" +
    "<title>" + title + "</title>" + DEMO_STYLE_OPEN +
    "body{margin:0;font:15px/1.55 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;" +
    "background:#f7f7fb;color:#1d1d27}" +
    "main{max-width:640px;margin:0 auto;padding:48px 24px}" +
    "h1{font-size:28px;margin:0 0 6px;letter-spacing:-.01em}" +
    ".sub{color:#5b5b70;margin:0 0 28px}" +
    ".card{background:#fff;border:1px solid #e3e3ee;border-radius:12px;padding:18px 20px;margin:0 0 14px}" +
    ".card h2{font-size:15px;margin:0 0 6px}" +
    "code{font:13px ui-monospace,Menlo,monospace;background:#eeeef6;border-radius:5px;padding:1px 5px}" +
    ".row{display:flex;gap:10px;align-items:center}" +
    ".dot{width:9px;height:9px;border-radius:50%;background:#3fae6a}" +
    "button{font:inherit;border:1px solid #d4d4e4;background:#fff;border-radius:8px;padding:6px 14px}" +
    ".note{margin-top:28px;font-size:12.5px;color:#7a7a8e}" +
    DEMO_STYLE_CLOSE + "<main>" + body + "</main>";
}

function demoDevPage() {
  return demoPageShell("webapp",
    "<h1>webapp</h1>" +
    "<p class=\"sub\">Running on the dev server at <code>localhost:5173</code></p>" +
    "<div class=\"card\"><div class=\"row\"><span class=\"dot\"></span>" +
    "<h2 style=\"margin:0\">Router ready</h2></div>" +
    "<p style=\"margin:8px 0 0\">3 routes registered: <code>/</code>, " +
    "<code>/settings</code>, <code>*</code></p></div>" +
    "<div class=\"card\"><h2>Counter</h2><div class=\"row\">" +
    "<button type=\"button\">-</button><strong>0</strong>" +
    "<button type=\"button\">+</button></div></div>" +
    "<div class=\"card\"><h2>Edit <code>src/app.js</code> and save to reload.</h2>" +
    "<p style=\"margin:0;color:#5b5b70\">Hot module replacement is on.</p></div>" +
    "<p class=\"note\">An invented page for the PocketTUI demo. " +
    "No server is running and nothing was fetched.</p>");
}

function demoOffPage() {
  return demoPageShell("Demo",
    "<h1>This is the demo.</h1>" +
    "<p class=\"sub\">Connect your computer to browse the web through it.</p>" +
    "<div class=\"card\"><p style=\"margin:0\">With a computer connected, this " +
    "pane opens pages the way that computer sees them, its own " +
    "<code>localhost</code> dev servers included.</p></div>");
}

function demoBrowserPage(url) {
  return url === DEMO_DEV_URL ? demoDevPage() : demoOffPage();
}
