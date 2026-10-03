// ============================================================
// Shared pieces of the anonymous usage counts
// ============================================================
// Imported by the /api/wave Function, the download-counting middleware and
// /api/console. Web Crypto only: this runs in the Workers runtime, which has
// no Node crypto.
//
// No IP address is ever stored or logged. A row carries a person hash keyed
// by a secret that changes every day, so the same visitor counts once per day
// and cannot be followed from one day to the next, nor recovered from the
// hash by trying addresses without the salt.

// Rows older than this are deleted; long enough to compare a month with the
// same month last year.
const RETENTION_MS = 400 * 24 * 60 * 60 * 1000;
// Purging on a share of writes rather than on a schedule keeps this to Pages
// Functions alone, with no Cron Worker to deploy beside them.
const PURGE_CHANCE = 0.02;
const HOSTED = "pockettui.com";

const enc = new TextEncoder();

function hex(buf) {
  return Array.from(new Uint8Array(buf), (b) => b.toString(16).padStart(2, "0")).join("");
}

async function hmacHex(secret, msg) {
  const key = await crypto.subtle.importKey(
    "raw", enc.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return hex(await crypto.subtle.sign("HMAC", key, enc.encode(msg)));
}

function salt(env) {
  const s = env && env.USAGE_SALT;
  // Without the salt every hash would be a plain digest anyone could
  // reproduce from a guessed address, so callers must skip the write instead.
  if (!s) throw new Error("USAGE_SALT is not set; refusing to hash usage data");
  return s;
}

export function utcDay(ts) {
  return new Date(ts).toISOString().slice(0, 10);
}

export async function dayKey(env, day) {
  return hmacHex(salt(env), day);
}

export async function personHash(env, day, ip, ua) {
  const k = await dayKey(env, day);
  const digest = await crypto.subtle.digest(
    "SHA-256", enc.encode(k + "|" + (ip || "") + "|" + (ua || "")));
  return hex(digest).slice(0, 32);
}

// Which self-hosted install asked for version.txt, as a pseudonym that stays
// the same across days on purpose: the point is to count distinct installs
// over a month, and a daily key would make every install new each morning.
// The port is kept because two installs on one host differ only by it.
export async function refHostHash(env, host) {
  if (!host) return null;
  const h = String(host).toLowerCase();
  if (h === HOSTED) return "hosted";
  return (await hmacHex(salt(env), "ref|" + h)).slice(0, 32);
}

export function agentClass(ua) {
  const s = String(ua || "");
  if (/^(curl|Wget)\//i.test(s)) return "curl";
  if (s.includes("Mozilla/")) return "browser";
  return "other";
}

export function noContent() {
  return new Response(null, {
    status: 204,
    headers: { "Access-Control-Allow-Origin": "*", "Cache-Control": "no-store" },
  });
}

export function json(status, body) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "Content-Type": "application/json",
      "Access-Control-Allow-Origin": "*",
      "Cache-Control": "no-store",
    },
  });
}

// Resolves either way: a failed purge only means the rows wait for the next
// lucky write, and must never fail the request that triggered it.
// rand is a number in [0, 1) or a function returning one, so tests can pin it.
// installs goes too: a returning-user id whose last event is past the cutoff
// would otherwise outlive the summaries it links, against the 400-day promise.
// last_day is a UTC day string, so the cutoff is compared as one.
export async function purgeMaybe(db, now = Date.now(), rand = Math.random) {
  const r = typeof rand === "function" ? rand() : rand;
  if (!(r < PURGE_CHANCE)) return;
  const cutoff = now - RETENTION_MS;
  try {
    await db.prepare("DELETE FROM events WHERE ts < ?").bind(cutoff).run();
    await db.prepare("DELETE FROM fetches WHERE ts < ?").bind(cutoff).run();
    await db.prepare("DELETE FROM installs WHERE last_day < ?").bind(utcDay(cutoff)).run();
  } catch (e) {
    // Swallowed on purpose; see above.
  }
}
