// ============================================================
// POST /api/report -> support@pockettui.com
// ============================================================
// The one endpoint the app talks to that is not the user's own computer. That
// is the whole point: the report worth sending is usually the one where their
// backend is unreachable, so it cannot be the thing that carries it.
//
// The origin is open on purpose. Self-hosted installs serve the same shell from
// their own tailnet address, so every one of them is a different origin and
// none of them can be listed. There is nothing behind here to protect but the
// mailbox, and the honeypot and the size caps are what guard that.

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
  "Cache-Control": "no-store",
};

const MAX_BODY = 16 * 1024;
const MAX_MESSAGE = 2000;
const MAX_EMAIL = 200;
const MAX_DIAG = 8000;
const MAX_SUBJECT = 60;

const SUPPORT = "support@pockettui.com";
// What the sender is told whenever the send itself failed. Deliberately the
// same string for a missing key, a refused key and a dead API: the person
// reading it can do exactly one thing about any of them, and it is this.
const SEND_FAILED = "Couldn't send. Email " + SUPPORT + " instead.";

function json(status, body) {
  return new Response(JSON.stringify(body), {
    status,
    headers: Object.assign({ "Content-Type": "application/json" }, CORS),
  });
}

export function onRequestOptions() {
  return new Response(null, { status: 204, headers: CORS });
}

export async function onRequestPost({ request, env }) {
  // Both halves matter: the header is what lets an oversized body be refused
  // before it is read, and the length is what catches one that lied.
  if (Number(request.headers.get("Content-Length") || 0) > MAX_BODY) {
    return json(413, { error: "That report is too large to send." });
  }
  const raw = await request.text();
  if (raw.length > MAX_BODY) {
    return json(413, { error: "That report is too large to send." });
  }
  let body;
  try { body = JSON.parse(raw); } catch (e) { body = null; }
  if (!body || typeof body !== "object") {
    return json(400, { error: "Couldn't read that report." });
  }

  // A field no human can see and no keyboard can reach. Answered 200 with
  // nothing sent: a bot told it failed learns which field gave it away.
  if (String(body.website || "").trim()) return json(200, { ok: true });

  const message = String(body.message || "").trim();
  if (!message) return json(400, { error: "Please describe what went wrong." });
  if (message.length > MAX_MESSAGE) {
    return json(400, { error: "Message is too long — 2000 characters at most." });
  }

  const email = String(body.email || "").trim();
  if (email && (email.length > MAX_EMAIL || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email))) {
    return json(400, { error: "That email address doesn't look right." });
  }

  // Truncated rather than refused: diagnostics are collected by the app, not
  // typed, so an oversized block is nothing the sender can act on.
  const diag = String(body.diag || "").slice(0, MAX_DIAG);

  const oneLine = message.replace(/\s+/g, " ");
  const subject = "[report] " + (oneLine.length > MAX_SUBJECT
    ? oneLine.slice(0, MAX_SUBJECT) + "…"
    : oneLine);

  // Plain text only, never html: every line below is untrusted input.
  const lines = [message, "", "-- ", "From: " + (email || "(not given)")];
  if (diag) lines.push("", diag);

  if (!env.RESEND_API_KEY) return json(500, { error: SEND_FAILED });

  const payload = {
    from: "PocketTUI Reports <" + SUPPORT + ">",
    to: [SUPPORT],
    subject,
    text: lines.join("\n"),
  };
  // Only when there is somewhere to reply to — an empty reply_to is a rejected
  // send, and an anonymous report is a supported way to use this.
  if (email) payload.reply_to = email;

  try {
    const r = await fetch("https://api.resend.com/emails", {
      method: "POST",
      headers: {
        "Authorization": "Bearer " + env.RESEND_API_KEY,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(payload),
    });
    // Resend's own error body can quote the request, so it is logged and never
    // returned.
    if (!r.ok) {
      console.log("resend failed status=" + r.status);
      return json(502, { error: SEND_FAILED });
    }
  } catch (e) {
    console.log("resend threw: " + (e && e.message));
    return json(502, { error: SEND_FAILED });
  }

  return json(200, { ok: true });
}
