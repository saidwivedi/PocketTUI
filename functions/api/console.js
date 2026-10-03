// ============================================================
// GET /api/console -> aggregate usage counts for the stats page
// ============================================================
// The only reader of the usage tables. It answers with counts and shares,
// never a row: the person hash and the install id stay inside the SQL, which
// only ever counts them.
//
// Unlike /api/report this is not open to every origin. It is behind a key, and
// the page that calls it lives on a short list of hosts, so only those get the
// CORS headers; a same-origin call needs none and works regardless.

import { utcDay } from "../_lib/usage.js";

const ALLOWED_ORIGINS = ["https://pockettui.com", "https://apps.saidwivedi.in", "https://saidwivedi.in"];
// Any port on loopback, so the console page can be developed against prod
// from a local static server.
const LOCAL_ORIGIN = /^http:\/\/(localhost|127\.0\.0\.1)(:\d{1,5})?$/;

const DAY_CHOICES = [7, 28, 90];
const DEFAULT_DAYS = 28;
const DAY_MS = 24 * 60 * 60 * 1000;
// Session lengths are pulled as rows and ranked here; at this volume that is
// cheaper to read than percentile SQL, and the cap bounds the worst case.
const SECS_CAP = 50000;
const LEN_BUCKETS = [30, 60, 120, 300, 900, 3600];
const TOP_N = 20;
const RUM_TIMEOUT_MS = 8000;

// Cloudflare Web Analytics ids for pockettui.com. Neither is a secret: the
// account tag appears in dashboard URLs and the site tag ships in the beacon
// snippet on every page. The token that reads them is CF_ANALYTICS_TOKEN.
const CF_ACCOUNT_TAG = "9a7a1de39162fb189ad956c6559fb28d";
const CF_SITE_TAG = "5a3a629a4c5f4a1fa4aa7a64f3df91a4";
const CF_GRAPHQL = "https://api.cloudflare.com/client/v4/graphql";

const FEATURES = ["explorer", "browser", "diff", "side2", "voice", "settings",
  "reader", "editor", "viewer", "search", "newsess"];
// Columns that may be grouped on; the names are interpolated into SQL, so
// they come from this list only, never from the request.
const GROUPS = { versions: "app", srv: "srv", os: "os", layout: "layout", shell: "shell", country: "country" };
// Daily totals copied from Cloudflare's analytics by pull_cf_stats.py into
// external_daily, so they outlast Cloudflare's own ~30-day window. Zone
// counts are Cloudflare's sampled estimates; *_ips are distinct per UTC day.
const HISTORY = ["landing_visits", "app_visits", "installer_runs", "installer_ips",
  "tarball_fetches", "tarball_ips", "version_checks", "version_ips"];

function corsHeaders(request) {
  const origin = request.headers.get("Origin");
  if (!origin || !(ALLOWED_ORIGINS.includes(origin) || LOCAL_ORIGIN.test(origin))) return {};
  return {
    "Access-Control-Allow-Origin": origin,
    "Vary": "Origin",
    "Access-Control-Allow-Headers": "Authorization",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Max-Age": "86400",
  };
}

function reply(request, status, body) {
  return new Response(JSON.stringify(body), {
    status,
    headers: Object.assign({
      "Content-Type": "application/json",
      "Cache-Control": "private, max-age=60",
    }, corsHeaders(request)),
  });
}

async function sha256(s) {
  return new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s)));
}

// Both sides are hashed first so the comparison runs over 32 bytes whatever
// the lengths, and the loop never exits early on the first differing byte.
async function keyMatches(given, expected) {
  const [a, b] = await Promise.all([sha256(given), sha256(expected)]);
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a[i] ^ b[i];
  return diff === 0;
}

function bearer(request) {
  const h = request.headers.get("Authorization") || "";
  const m = /^Bearer\s+(.+)$/.exec(h);
  return m ? m[1].trim() : "";
}

export function parseDays(raw) {
  const n = Number.parseInt(raw, 10);
  if (!Number.isFinite(n)) return DEFAULT_DAYS;
  // Clamped to the nearest choice at or above the ask, so 1000 lands on 90.
  for (const d of DAY_CHOICES) if (n <= d) return d;
  return DAY_CHOICES[DAY_CHOICES.length - 1];
}

function addDays(day, n) {
  return utcDay(Date.parse(day + "T00:00:00Z") + n * DAY_MS);
}

function dayRange(from, to) {
  const out = [];
  for (let d = from; d <= to; d = addDays(d, 1)) out.push(d);
  return out;
}

// ISO week start (Monday) of a YYYY-MM-DD day.
function monday(day) {
  const dow = new Date(day + "T00:00:00Z").getUTCDay();
  return addDays(day, -((dow + 6) % 7));
}

function addMonths(month, n) {
  const [y, m] = month.split("-").map(Number);
  const d = new Date(Date.UTC(y, m - 1 + n, 1));
  return d.toISOString().slice(0, 7);
}

const num = (v) => Number(v) || 0;

// The same Monday-of-week expression SQLite-side; strftime('%w') is 0 on
// Sunday, so (w + 6) % 7 is the number of days back to Monday.
const WEEK_OF = (col) =>
  `date(${col}, '-' || ((CAST(strftime('%w', ${col}) AS INTEGER) + 6) % 7) || ' days')`;

function buildStatements(db, from, to, weekFrom, monthFrom) {
  const q = (sql, ...binds) => db.prepare(sql).bind(...binds);
  const stmts = {};

  // installs: one row per day with any fetch. A tarball fetch with no mode
  // came from an installer too old to say, which only ever installed.
  const fetchCols = `
      SUM(kind = 'install_sh') AS attempts,
      SUM(kind = 'tarball' AND COALESCE(mode, 'install') <> 'update') AS installs,
      SUM(kind = 'tarball' AND mode = 'update') AS updates,
      SUM(kind = 'version') AS version_checks,
      COUNT(DISTINCT CASE WHEN kind = 'version' AND ref_host <> 'hosted' THEN ref_host END) AS active_self`;
  stmts.fetchDays = q(`SELECT day, ${fetchCols}
    FROM fetches WHERE day BETWEEN ? AND ? GROUP BY day`, from, to);
  // Totals are their own query because active_self is a distinct count: the
  // same install checking on ten days is one install, not ten.
  stmts.fetchTotals = q(`SELECT ${fetchCols}
    FROM fetches WHERE day BETWEEN ? AND ?`, from, to);

  // app: per-day people and sessions. people is distinct per day only, since
  // the person hash is keyed by a secret that changes daily.
  stmts.eventDays = q(`SELECT day,
      COUNT(DISTINCT person) AS people,
      COUNT(*) AS sessions,
      SUM(install IS NOT NULL) AS consented_sessions
    FROM events WHERE day BETWEEN ? AND ? GROUP BY day`, from, to);
  stmts.installDays = q(`SELECT e.day,
      COUNT(DISTINCT CASE WHEN i.first_day = e.day THEN e.install END) AS new_installs,
      COUNT(DISTINCT CASE WHEN i.first_day < e.day THEN e.install END) AS returning_installs
    FROM events e JOIN installs i ON i.id = e.install
    WHERE e.day BETWEEN ? AND ? GROUP BY e.day`, from, to);
  // Window totals count each install once: new if its first day falls in the
  // window, returning if it was first seen before it.
  stmts.installTotals = q(`SELECT
      COUNT(DISTINCT e.install) AS installs_seen,
      COUNT(DISTINCT CASE WHEN i.first_day >= ? THEN e.install END) AS new_installs,
      COUNT(DISTINCT CASE WHEN i.first_day < ? THEN e.install END) AS returning_installs
    FROM events e JOIN installs i ON i.id = e.install
    WHERE e.day BETWEEN ? AND ?`, from, from, from, to);

  stmts.feats = q(`SELECT COUNT(*) AS sessions, SUM(install IS NOT NULL) AS consented_sessions,
      ${FEATURES.map((f) => `SUM(f_${f} > 0) AS ${f}`).join(", ")}
    FROM events WHERE day BETWEEN ? AND ?`, from, to);

  // The newest rows when the cap bites, so a long window shows recent use.
  stmts.secs = q(`SELECT secs FROM events WHERE day BETWEEN ? AND ?
    ORDER BY ts DESC LIMIT ${SECS_CAP}`, from, to);

  for (const [name, col] of Object.entries(GROUPS)) {
    stmts["group_" + name] = q(`SELECT ${col} AS k, COUNT(*) AS n
      FROM events WHERE day BETWEEN ? AND ?
      GROUP BY ${col} ORDER BY n DESC, k LIMIT ${TOP_N}`, from, to);
  }

  // Retention: cohorts by the week (or month) an install was first seen, and
  // for each cohort the installs that sent anything in the Nth period after.
  // Activity is reduced to distinct (install, period) pairs before the join
  // so a busy install does not multiply rows.
  stmts.weekly = q(`WITH c AS (
      SELECT id, ${WEEK_OF("first_day")} AS wk FROM installs WHERE first_day BETWEEN ? AND ?
    ), a AS (
      SELECT DISTINCT install, ${WEEK_OF("day")} AS wk FROM events
      WHERE install IS NOT NULL AND day BETWEEN ? AND ?
    )
    SELECT c.wk AS week, COUNT(DISTINCT c.id) AS new,
      ${[1, 2, 3, 4].map((n) =>
        `COUNT(DISTINCT CASE WHEN a.wk = date(c.wk, '+${7 * n} days') THEN c.id END) AS back${n}`).join(",\n      ")}
    FROM c LEFT JOIN a ON a.install = c.id
    GROUP BY c.wk ORDER BY c.wk`, weekFrom, to, weekFrom, to);
  stmts.monthly = q(`WITH c AS (
      SELECT id, strftime('%Y-%m', first_day) AS mo FROM installs WHERE first_day BETWEEN ? AND ?
    ), a AS (
      SELECT DISTINCT install, strftime('%Y-%m', day) AS mo FROM events
      WHERE install IS NOT NULL AND day BETWEEN ? AND ?
    )
    SELECT c.mo AS month, COUNT(DISTINCT c.id) AS new,
      ${[1, 2].map((n) =>
        `COUNT(DISTINCT CASE WHEN a.mo = strftime('%Y-%m', c.mo || '-01', '+${n} months') THEN c.id END) AS back${n}`).join(",\n      ")}
    FROM c LEFT JOIN a ON a.install = c.id
    GROUP BY c.mo ORDER BY c.mo`, monthFrom + "-01", to, monthFrom + "-01", to);

  stmts.history = q(`SELECT day, metric, SUM(value) AS v FROM external_daily
    WHERE day BETWEEN ? AND ? AND key = '' AND metric IN (${HISTORY.map((m) => `'${m}'`).join(", ")})
    GROUP BY day, metric`, from, to);

  return stmts;
}

// Nearest-rank percentile: the smallest value with at least p of the sample
// at or below it. The median of five values is the third.
function percentile(sorted, p) {
  if (!sorted.length) return null;
  return sorted[Math.max(0, Math.ceil(p * sorted.length) - 1)];
}

function lengths(rows) {
  const secs = rows.map((r) => num(r.secs)).sort((a, b) => a - b);
  // Buckets are not cumulative: le 60 holds 30 < secs <= 60; le null holds
  // everything past the last edge.
  const hist = LEN_BUCKETS.map((le) => ({ le, n: 0 })).concat([{ le: null, n: 0 }]);
  for (const s of secs) {
    const i = LEN_BUCKETS.findIndex((le) => s <= le);
    hist[i === -1 ? LEN_BUCKETS.length : i].n++;
  }
  return { median: percentile(secs, 0.5), p90: percentile(secs, 0.9), hist };
}

function byKey(rows, key) {
  const m = new Map();
  for (const r of rows) m.set(r[key], r);
  return m;
}

function shapeD1(res, days) {
  const fd = byKey(res.fetchDays, "day");
  const FETCH = ["attempts", "installs", "updates", "version_checks", "active_self"];
  const pick = (r, cols) => Object.fromEntries(cols.map((c) => [c, num(r && r[c])]));
  const installs = {
    days: days.map((day) => Object.assign({ day }, pick(fd.get(day), FETCH))),
    totals: pick(res.fetchTotals[0], FETCH),
  };

  const ed = byKey(res.eventDays, "day");
  const id = byKey(res.installDays, "day");
  const appDays = days.map((day) => Object.assign({ day },
    pick(ed.get(day), ["people", "sessions", "consented_sessions"]),
    pick(id.get(day), ["new_installs", "returning_installs"])));

  const f = res.feats[0] || {};
  const sessions = num(f.sessions);
  const consented = num(f.consented_sessions);
  const it = res.installTotals[0] || {};
  const share = (n) => (sessions ? n / sessions : 0);

  const app = {
    days: appDays,
    totals: {
      // A sum of daily distinct counts: one person on three days is three.
      // That over-count is by design; the daily key leaves nothing to join on.
      people_sum: appDays.reduce((s, d) => s + d.people, 0),
      sessions,
      consented_sessions: consented,
      installs_seen: num(it.installs_seen),
      new_installs: num(it.new_installs),
      returning_installs: num(it.returning_installs),
    },
    len: lengths(res.secs),
    feats: Object.fromEntries(FEATURES.map((k) =>
      [k, { sessions: num(f[k]), share: share(num(f[k])) }])),
  };
  for (const name of Object.keys(GROUPS)) {
    app[name] = res["group_" + name].map((r) => ({ k: r.k, n: num(r.n) }));
  }
  app.consent_share = share(consented);

  const retention = {
    weekly: res.weekly.map((r) => ({
      week: r.week, new: num(r.new),
      back1: num(r.back1), back2: num(r.back2), back3: num(r.back3), back4: num(r.back4) })),
    monthly: res.monthly.map((r) => ({
      month: r.month, new: num(r.new), back1: num(r.back1), back2: num(r.back2) })),
  };
  const hist = new Map();
  for (const r of res.history) {
    if (!hist.has(r.day)) hist.set(r.day, {});
    hist.get(r.day)[r.metric] = num(r.v);
  }
  const history = { days: days.map((day) => Object.assign({ day }, pick(hist.get(day), HISTORY))) };
  return { installs, app, retention, history };
}

async function queryD1(db, from, to) {
  // Four weeks (and two months) of cohorts before the window, so the first
  // weeks of the window already have back1..back4 to show.
  const weekFrom = addDays(monday(from), -28);
  const monthFrom = addMonths(from.slice(0, 7), -2);
  const stmts = buildStatements(db, from, to, weekFrom, monthFrom);
  const names = Object.keys(stmts);
  const out = await db.batch(names.map((n) => stmts[n]));
  const res = {};
  names.forEach((n, i) => { res[n] = (out[i] && out[i].results) || []; });
  return res;
}

const RUM_QUERY = `query($a: String!, $f: AccountRumPageloadEventsAdaptiveGroupsFilter_InputObject!) {
  viewer { accounts(filter: {accountTag: $a}) {
    days: rumPageloadEventsAdaptiveGroups(limit: 1000, filter: $f) { count sum { visits } dimensions { date } }
    country: rumPageloadEventsAdaptiveGroups(limit: 1000, filter: $f) { count sum { visits } dimensions { countryName } }
    device: rumPageloadEventsAdaptiveGroups(limit: 1000, filter: $f) { count sum { visits } dimensions { deviceType } }
    referrer: rumPageloadEventsAdaptiveGroups(limit: 1000, filter: $f) { count sum { visits } dimensions { refererHost } }
  } }
}`;

// The landing page's Web Analytics numbers. Throws a short message on any
// failure; the caller turns it into landing: null and an errors entry.
async function queryRum(env, from, to, now, days) {
  if (!env.CF_ANALYTICS_TOKEN) throw new Error("landing: CF_ANALYTICS_TOKEN is not set");
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), RUM_TIMEOUT_MS);
  let body;
  try {
    const r = await fetch(CF_GRAPHQL, {
      method: "POST",
      headers: { "Content-Type": "application/json", "Authorization": "Bearer " + env.CF_ANALYTICS_TOKEN },
      body: JSON.stringify({
        query: RUM_QUERY,
        variables: {
          a: CF_ACCOUNT_TAG,
          f: {
            siteTag: CF_SITE_TAG,
            datetime_geq: from + "T00:00:00Z",
            datetime_leq: new Date(now).toISOString().replace(/\.\d{3}Z$/, "Z"),
            requestHost: "pockettui.com",
            requestPath: "/",
          },
        },
      }),
      signal: ctl.signal,
    });
    if (!r.ok) throw new Error("landing: analytics API answered " + r.status);
    body = await r.json();
  } catch (e) {
    if (e && e.name === "AbortError") throw new Error("landing: analytics API timed out");
    if (String(e && e.message).startsWith("landing:")) throw e;
    throw new Error("landing: analytics API unreachable");
  } finally {
    clearTimeout(timer);
  }
  if (body && Array.isArray(body.errors) && body.errors.length) {
    throw new Error("landing: " + String(body.errors[0].message || "GraphQL error").slice(0, 200));
  }
  const acc = body && body.data && body.data.viewer && body.data.viewer.accounts && body.data.viewer.accounts[0];
  if (!acc) throw new Error("landing: analytics API returned no account");

  const visits = (x) => num(x.sum && x.sum.visits);
  const sum = (rows, keyOf) => {
    const m = new Map();
    for (const x of rows || []) {
      const k = keyOf(x.dimensions || {});
      m.set(k, (m.get(k) || 0) + visits(x));
    }
    return [...m].map(([k, v]) => ({ k, visits: v })).sort((a, b) => b.visits - a.visits || String(a.k).localeCompare(String(b.k)));
  };
  const byDay = new Map();
  for (const x of acc.days || []) {
    const d = (x.dimensions || {}).date;
    const cur = byDay.get(d) || { views: 0, visits: 0 };
    cur.views += num(x.count);
    cur.visits += visits(x);
    byDay.set(d, cur);
  }
  return {
    days: days.map((day) => Object.assign({ day }, byDay.get(day) || { views: 0, visits: 0 })),
    country: sum(acc.country, (d) => d.countryName || "unknown"),
    device: sum(acc.device, (d) => d.deviceType || "unknown"),
    referrer: sum(acc.referrer, (d) => d.refererHost || "direct"),
  };
}

export function onRequestOptions({ request }) {
  return new Response(null, { status: 204, headers: corsHeaders(request) });
}

export async function onRequestGet({ request, env }) {
  if (!env.STATS_KEY || !env.DB) return reply(request, 503, { error: "not configured" });
  if (!(await keyMatches(bearer(request), env.STATS_KEY))) {
    return reply(request, 401, { error: "unauthorized" });
  }

  const url = new URL(request.url);
  const days = parseDays(url.searchParams.get("days"));
  const now = Date.now();
  const to = utcDay(now);
  const from = utcDay(now - (days - 1) * DAY_MS);
  const dayList = dayRange(from, to);

  const errors = [];
  const [d1, landing] = await Promise.all([
    queryD1(env.DB, from, to).catch(() => null),
    queryRum(env, from, to, now, dayList).catch((e) => {
      errors.push(String(e && e.message));
      return null;
    }),
  ]);
  if (!d1) return reply(request, 500, { error: "query failed" });

  return reply(request, 200, Object.assign(
    { range: { from, to, days }, landing }, shapeD1(d1, dayList), { errors }));
}
