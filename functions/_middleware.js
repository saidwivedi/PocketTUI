// ============================================================
// Download counts for install.sh, the tarball and version.txt
// ============================================================
// _routes.json sends only these three static files (and /api/*) through
// Functions, so this middleware sees each download, notes one anonymous row
// and hands the request to the static asset unchanged. Counting must never
// cost a download: every failure on the recording side is swallowed.
//
// Nothing that identifies a person is kept or logged: no IP, no user agent
// beyond its coarse class, and the Referer only as a keyed pseudonym of its
// host, so the raw address of a self-hosted install never reaches the table.

import { utcDay, refHostHash, agentClass } from "./_lib/usage.js";

const KINDS = {
  "/install.sh": "install_sh",
  "/pockettui.tar.gz": "tarball",
  "/version.txt": "version",
};
const MODES = new Set(["install", "update"]);

async function buildRow(env, request, url, kind) {
  const m = url.searchParams.get("m");
  let refHost = null;
  if (kind === "version") {
    const referer = request.headers.get("Referer");
    let host = "";
    try {
      host = referer ? new URL(referer).host : "";
    } catch (e) {
      host = "";
    }
    refHost = await refHostHash(env, host);
  }
  const ts = Date.now();
  return {
    ts,
    day: utcDay(ts),
    kind,
    mode: MODES.has(m) ? m : null,
    agent: agentClass(request.headers.get("User-Agent")),
    ref_host: refHost,
    country: String((request.cf && request.cf.country) ||
      request.headers.get("CF-IPCountry") || "").slice(0, 2),
  };
}

async function record(env, request, url, kind) {
  // Without the salt the referer pseudonym would be reproducible by anyone,
  // so a deploy missing either binding counts nothing rather than half.
  if (!env || !env.DB || !env.USAGE_SALT) return;
  const r = await buildRow(env, request, url, kind);
  await env.DB.prepare(
    "INSERT INTO fetches (ts, day, kind, mode, agent, ref_host, country) " +
    "VALUES (?, ?, ?, ?, ?, ?, ?)")
    .bind(r.ts, r.day, r.kind, r.mode, r.agent, r.ref_host, r.country)
    .run();
}

export async function onRequest(context) {
  const { request, env } = context;
  const url = new URL(request.url);
  const kind = KINDS[url.pathname];
  if (!kind) return context.next();

  // The query exists only for the count; the edge asset lookup is keyed on
  // the bare path. Building from the original request keeps the method and
  // headers, so HEAD and Range on the tarball behave as they do statically.
  const response = await env.ASSETS.fetch(new Request(url.origin + url.pathname, request));

  // Only a delivered file counts: a 404 or a 304 revalidation is not a fetch.
  if (response.status === 200 || response.status === 206) {
    try {
      context.waitUntil(record(env, request, url, kind).catch(() => {}));
    } catch (e) {
      // A runtime without waitUntil still serves the file.
    }
  }

  if (kind === "version" && !response.headers.has("Cache-Control")) {
    // version.txt is how an install learns an update exists; a cached copy
    // would hide a release. An explicit header from the asset config wins.
    const out = new Response(response.body, response);
    out.headers.set("Cache-Control", "no-store");
    return out;
  }
  return response;
}
