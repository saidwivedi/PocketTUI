// ============================================================
// POST /api/wave -> one anonymous row per app session
// ============================================================
// The shell sends this once as a session ends, with sendBeacon or a no-cors
// fetch, so the body arrives as text/plain or with no type at all and the
// answer is never read. Every failure past validation therefore answers 204:
// a 5xx would only show up in logs, and the client could do nothing with it.
//
// The origin is open on purpose, for the same reason as /api/report: self-served
// shells post from each install's own address, and none of them can be listed.
//
// Nothing here logs the body, the address or any header. What is stored is a
// fixed set of counters, a person hash keyed by a daily secret, and the
// install's random id when the client chose to send one.

import { utcDay, personHash, noContent, json, purgeMaybe } from "../_lib/usage.js";

const MAX_BODY = 2048;
const MAX_INT = 100000;
const MAX_SECS = 7 * 24 * 60 * 60;
// A real session sends one event; this only stops a loop or a script from
// filling the table under one person hash.
const DAILY_CAP = 500;

const SHELLS = ["hosted", "self", "other"];
const OSES = ["ios", "ipados", "android", "mac", "windows", "linux", "chromeos", "other"];
const LAYOUTS = ["phone", "desktop"];
const FEATURES = ["explorer", "browser", "diff", "side2", "voice", "settings",
  "reader", "editor", "viewer", "search", "newsess"];
const TOP = ["v", "app", "srv", "shell", "os", "layout", "pwa", "secs", "rc", "seen", "f", "id"];
const VERSION_RE = /^[0-9A-Za-z.\-]*$/;
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

const own = (o, k) => Object.prototype.hasOwnProperty.call(o, k);
const isObj = (o) => o !== null && typeof o === "object" && !Array.isArray(o);
const isCount = (n) => Number.isInteger(n) && n >= 0;
const isVersion = (s) => typeof s === "string" && s.length <= 20 && VERSION_RE.test(s);

// Returns the row to store, or null when anything is off. Nothing is coerced:
// a client that sends the wrong type is a bug worth seeing as a 400.
function validate(b) {
  if (!isObj(b)) return null;
  for (const k of Object.keys(b)) if (!TOP.includes(k)) return null;
  if (b.v !== 1) return null;
  if (!isVersion(b.app) || !isVersion(b.srv)) return null;
  if (!SHELLS.includes(b.shell) || !OSES.includes(b.os) || !LAYOUTS.includes(b.layout)) return null;
  if (b.pwa !== 0 && b.pwa !== 1) return null;
  if (!isCount(b.secs) || !isCount(b.rc) || !isCount(b.seen)) return null;
  if (!isObj(b.f)) return null;
  const f = {};
  for (const k of Object.keys(b.f)) {
    if (!FEATURES.includes(k) || !isCount(b.f[k])) return null;
  }
  for (const k of FEATURES) f[k] = own(b.f, k) ? Math.min(b.f[k], MAX_INT) : 0;
  if (own(b, "id") && !(typeof b.id === "string" && UUID_RE.test(b.id))) return null;
  return {
    app: b.app, srv: b.srv, shell: b.shell, os: b.os, layout: b.layout, pwa: b.pwa,
    secs: Math.min(b.secs, MAX_SECS), rc: Math.min(b.rc, MAX_INT), seen: Math.min(b.seen, MAX_INT),
    f, id: own(b, "id") ? b.id : null,
  };
}

export function onRequestOptions() {
  return new Response(null, {
    status: 204,
    headers: {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Methods": "POST, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type",
      "Access-Control-Max-Age": "86400",
      "Cache-Control": "no-store",
    },
  });
}

export async function onRequestPost(context) {
  const { request, env } = context;
  // The header refuses an oversized body before it is read; the length
  // catches one that lied.
  if (Number(request.headers.get("Content-Length") || 0) > MAX_BODY) {
    return json(413, { error: "too large" });
  }
  const raw = await request.text();
  if (raw.length > MAX_BODY) return json(413, { error: "too large" });
  let body;
  try { body = JSON.parse(raw); } catch (e) { return json(400, { error: "bad json" }); }
  const ev = validate(body);
  if (!ev) return json(400, { error: "bad event" });

  try {
    const db = env && env.DB;
    if (!db) throw new Error("DB binding missing");
    const now = Date.now();
    // The server's clock, never the client's: the day is what the person
    // hash is keyed by, and a client choosing it could pick its own key.
    const day = utcDay(now);
    const person = await personHash(env, day,
      request.headers.get("CF-Connecting-IP") || "", request.headers.get("User-Agent") || "");
    const country = String((request.cf && request.cf.country) ||
      request.headers.get("CF-IPCountry") || "").slice(0, 2);

    const seen = await db.prepare("SELECT COUNT(*) AS n FROM events WHERE day = ? AND person = ?")
      .bind(day, person).first();
    if (seen && seen.n >= DAILY_CAP) return noContent();

    const f = ev.f;
    const insert = db.prepare(
      "INSERT INTO events (ts, day, person, install, app, srv, shell, os, layout, pwa, country, " +
      "secs, rc, seen, f_explorer, f_browser, f_diff, f_side2, f_voice, f_settings, f_reader, " +
      "f_editor, f_viewer, f_search, f_newsess) " +
      "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")
      .bind(now, day, person, ev.id, ev.app, ev.srv, ev.shell, ev.os, ev.layout, ev.pwa, country,
        ev.secs, ev.rc, ev.seen, f.explorer, f.browser, f.diff, f.side2, f.voice, f.settings,
        f.reader, f.editor, f.viewer, f.search, f.newsess);
    if (ev.id) {
      const upsert = db.prepare(
        "INSERT INTO installs (id, first_day, last_day, events) VALUES (?, ?, ?, 1) " +
        "ON CONFLICT(id) DO UPDATE SET last_day = excluded.last_day, events = events + 1")
        .bind(ev.id, day, day);
      await db.batch([insert, upsert]);
    } else {
      await insert.run();
    }
    if (context.waitUntil) context.waitUntil(purgeMaybe(db));
  } catch (e) {
    console.error("wave: " + (e && e.message));
  }
  return noContent();
}
