// ============================================================
// Report a problem
// ============================================================
// The one call in the app that does not go to the user's own computer. It posts
// to pockettui.com, which mails support@ — and that is the point, because the
// report worth sending is usually the one where the backend is unreachable, and
// a dialog that needed the backend would be silent exactly then.
//
// Nothing identifying rides along. No authHeaders(): the pairing code belongs to
// that computer and this is a different origin entirely. The diagnostics block
// is optional, it is rendered in the sheet before Send is pressed, and the one
// thing in it that could name a private machine — the shell's own hostname — is
// reduced to "self-hosted" rather than sent.
const REPORT_URL = "https://pockettui.com/api/report";
// Long enough for a quarter-megabyte debug log to go up over a poor phone link.
const REPORT_TIMEOUT = 30000;
// The endpoint's own limits (functions/api/report.js): the body in bytes, the
// log in chars. The log is fitted under both here, oldest lines first, so the
// endpoint never has to refuse a report or cut the log's header off it.
const REPORT_MAX_BODY = 320 * 1024;
const REPORT_MAX_LOG = 256 * 1024;
// Shown when the send failed with nothing to say for itself. The endpoint's own
// wording for the same case, so a network failure and a refused send read alike.
const REPORT_SEND_FAILED = "Couldn't send. Email support@pockettui.com instead.";
const REPORT_DBG_LINES = 30;

// Set by the first session list that comes back. Reporting is offered only to
// an install that has actually reached a backend: before that there is nothing
// to report about but the setup sheet in front of the user, and the demo is not
// their machine at all.
let sessionsEverLoaded = false;

function reportAvailable() {
  return sessionsEverLoaded && !demoMode && !needsSetup();
}

// Both entry points at once, so neither can be showing while the other is not.
// Called wherever that answer can have changed — a list that loaded, the demo
// starting or ending, Settings opening — never on a timer. The key pill's bubble
// is looked up rather than held: buildKeybar() replaces that button whenever
// Settings touches the bar, and on the very first call there may be no bar yet.
function syncReportEntry() {
  const on = reportAvailable();
  $("report-row").classList.toggle("show", on);
  const key = $("keybar").querySelector(".k-report");
  if (key) key.classList.toggle("show", on);
}

// The public shell names itself; anything else says only that it is one of the
// self-hosted ones. The hostname there is a machine on someone's tailnet, which
// is the one thing this app is built never to publish.
function reportShell() {
  const h = location.hostname;
  return h === "pockettui.com" || h === "pockettui.pages.dev" ? "pockettui.com" : "self-hosted";
}

// Checked in order, because the later tokens are the ones every browser copies:
// Edge and Opera both say Chrome, and everything on iOS says Safari.
const REPORT_BROWSERS = [
  [/Edg\/(\d+)/, "Edge"],
  [/OPR\/(\d+)/, "Opera"],
  [/SamsungBrowser\/(\d+)/, "Samsung"],
  [/(?:Firefox|FxiOS)\/(\d+)/, "Firefox"],
  [/CriOS\/(\d+)/, "Chrome"],
  [/Chrome\/(\d+)/, "Chrome"],
  [/Version\/(\d+).*Safari\//, "Safari"],
];

// The browser's own version is the only version the UA still tells the truth
// about. Its OS and CPU tokens are frozen: every Mac claims "Intel Mac OS X
// 10_15_7" whatever the silicon, and Windows has said "NT 10.0" since Windows
// 10 — so the OS here is a family name and nothing more. A real OS version and
// architecture, when a browser will say them, come from Client Hints below.
// Pure, so the parsing can be exercised on a UA string that isn't this one's.
function parseUA(ua, maxTouchPoints) {
  let browser = "unknown";
  for (const [re, name] of REPORT_BROWSERS) {
    const m = re.exec(ua);
    if (m) { browser = name + " " + m[1]; break; }
  }
  // Same iPadOS rule as a2hsPlatform(): it reports as a Mac, and a real Mac has
  // no touch screen.
  let os = "unknown";
  if (/iPhone|iPad|iPod/.test(ua)) os = "iOS";
  else if (/Mac/.test(ua) && maxTouchPoints > 1) os = "iPadOS";
  else if (/Android/.test(ua)) os = "Android";
  else if (/Mac/.test(ua)) os = "macOS";
  else if (/Windows/.test(ua)) os = "Windows";
  else if (/CrOS/.test(ua)) os = "ChromeOS";
  else if (/Linux/.test(ua)) os = "Linux";
  return { browser: browser, os: os };
}

// Client Hints are the one place a Chromium browser will still name the real
// machine, and only when asked, asynchronously. Fetched once when the sheet
// opens so that the block rendered and the block sent are the same string.
let uaHints = null;
async function loadUAHints() {
  const d = navigator.userAgentData;
  if (uaHints || !d || !d.getHighEntropyValues) return;
  try {
    uaHints = await d.getHighEntropyValues(["architecture", "platformVersion", "bitness"]);
  } catch (e) {
    dbg("report: ua hints unavailable", e);
  }
}

// Null when nothing answered — the line is left out rather than guessed at.
// The platform version is printed exactly as returned: on Windows it is a
// kernel-ish number ("15.0.0" is Windows 11) that only Microsoft can decode.
function reportArch() {
  if (!uaHints) return null;
  let bits = uaHints.architecture || "";
  // The conventional spellings: x86_64 carries the underscore, arm64 does not.
  if (bits && uaHints.bitness) bits = (bits === "x86" ? bits + "_" : bits) + uaHints.bitness;
  const plat = [navigator.userAgentData.platform, uaHints.platformVersion]
    .filter(Boolean).join(" ");
  return [bits, plat].filter(Boolean).join(", ") || null;
}

// What the <details> block shows and what the send carries — one function, so
// the two can never disagree about what is going out.
function reportDiagnostics() {
  const vv = window.visualViewport;
  const ua = parseUA(navigator.userAgent, navigator.maxTouchPoints);
  const arch = reportArch();
  // Page zoom, near enough, and only a desktop notion: the same ratio on a
  // phone is browser chrome and would read as a fault that isn't there.
  const zoom = !a2hsPlatform() && window.innerWidth > 0 && window.outerWidth > 0
    ? "  zoom=" + Math.round(window.outerWidth / window.innerWidth * 100) + "%" : "";
  return [
    "version: " + buildVersion(),
    // The two releases behind that cache stamp. Either can be "unknown" — a
    // shell built from a checkout, a server too old to carry a VERSION file —
    // and a report where they disagree is a report about the skew.
    "app: " + (appVersion() || "unknown"),
    "server: " + (serverVersion || "unknown"),
    "browser: " + ua.browser,
    "os: " + ua.os,
    arch ? "arch: " + arch : null,
    "platform: " + (a2hsPlatform() || "desktop"),
    "standalone: " + a2hsInstalled(),
    "shell: " + reportShell(),
    "viewport: " + window.innerWidth + "x" + window.innerHeight +
      "  vv=" + (vv ? Math.round(vv.height) : "-") + "  dpr=" + window.devicePixelRatio + zoom,
    "screen: " + screen.width + "x" + screen.height,
    "wide: " + isWideLayout(),
    "online: " + navigator.onLine,
    "ua (raw, os/cpu tokens frozen by browsers): " + navigator.userAgent,
    "debug log (last " + REPORT_DBG_LINES + " lines):",
    dbgOn ? dbgBuf.slice(-REPORT_DBG_LINES).join("\n")
          : "(off — enable Debug log in Settings, reproduce, then report)",
  ].filter(line => line !== null).join("\n");
}

// The top of a debug recording (dbgRecStart, 02-debug-log.js): when it began
// and on what, so a log read on its own still says which build and browser it
// came from. Only ever called from the Settings tap, after every fragment has
// run; Client Hints are left out because they are fetched asynchronously.
function reportLogHeader() {
  const ua = parseUA(navigator.userAgent, navigator.maxTouchPoints);
  return [
    "PocketTUI debug log",
    "started: " + new Date().toISOString(),
    "version: " + buildVersion(),
    "app: " + (appVersion() || "unknown"),
    "server: " + (serverVersion || "unknown"),
    "browser: " + ua.browser,
    "os: " + ua.os,
    "platform: " + (a2hsPlatform() || "desktop"),
    "shell: " + reportShell(),
    "",
  ];
}

// The recording the open sheet would attach, as {head, lines}, or null. Taken
// when the sheet opens, like the diagnostics block: what the row counted is
// what goes out.
let reportLogRec = null;

function reportLogKB(text) {
  return Math.max(1, Math.round(new TextEncoder().encode(text).length / 1024));
}

// Turning Debug log off (05-settings.js) lands here with the recording already
// written out and the setting already off. Nothing recorded past the header is
// nothing to ask about.
async function offerDebugLog() {
  const rec = dbgRecLoad();
  const n = rec ? rec.lines.length : 0;
  if (!n) { dbgRecDiscard(); return; }
  const yes = await appConfirm(
    (n === 1 ? "1 line was" : n + " lines were") +
    " logged since Debug log was turned on. The log can include file paths," +
    " session names and your computer's address.",
    { title: "Send the debug log to support?", confirmLabel: "Send log…", danger: false });
  if (!yes) { dbgRecDiscard(); return; }
  openReport({ fromOff: true });
}

// The report body as it will be posted. A log that would take it over either
// limit loses its oldest lines, the header kept and the cut marked in it. Lines
// are dropped by their UTF-8 size against the byte excess and by their length
// against the char excess; JSON escaping can leave a little over, which the
// next pass takes.
function reportBody(fields, rec) {
  if (!rec) return JSON.stringify(fields);
  const enc = new TextEncoder();
  let lines = rec.lines;
  let dropped = 0;
  for (;;) {
    const text = rec.head.concat(
      dropped ? ["[" + dropped + " earlier lines dropped to fit the report]"] : [],
      lines).join("\n");
    const body = JSON.stringify(Object.assign({}, fields, { log: text }));
    const overBytes = enc.encode(body).length - REPORT_MAX_BODY;
    const overChars = text.length - REPORT_MAX_LOG;
    if ((overBytes <= 0 && overChars <= 0) || !lines.length) return body;
    // 64 past each excess leaves room for the marker line to grow.
    let cut = 0, bytes = 0, chars = 0;
    while (cut < lines.length && (bytes < overBytes + 64 || chars < overChars + 64)) {
      bytes += enc.encode(lines[cut]).length + 1;
      chars += lines[cut++].length + 1;
    }
    lines = lines.slice(cut);
    dropped += cut;
  }
}

// Sent in place of a description when the user attached a debug log and wrote
// nothing: the endpoint requires a message, and the log is the report here.
const REPORT_LOG_ONLY_MESSAGE = "Debug log sent from Settings (no description given).";

// The message to post, or "" when there is nothing to send.
function reportMessage(typed, logAttached) {
  const message = typed.trim();
  return message || (logAttached ? REPORT_LOG_ONLY_MESSAGE : "");
}

function showReportError(text) {
  $("report-error").textContent = text;
  $("report-error").classList.add("show");
}

// `fromOff` is the turn-off question's yes: the recording is attached, and is
// dropped with the sheet unless Send takes it. Opened the ordinary way while
// debug is still on, the sheet attaches what has been recorded so far and
// leaves the recording running.
async function openReport(opts) {
  const fromOff = !!(opts && opts.fromOff);
  $("report-error").classList.remove("show");
  await loadUAHints();
  $("report-diag-text").textContent = reportDiagnostics();
  const rec = fromOff || dbgOn ? dbgRecCurrent() : null;
  reportLogRec = rec && rec.lines.length
    ? { head: rec.head.slice(), lines: rec.lines.slice() } : null;
  $("report-log-row").hidden = !reportLogRec;
  $("report-msg-opt").hidden = !reportLogRec;
  $("report-log").checked = true;
  if (reportLogRec) {
    const n = reportLogRec.lines.length;
    $("report-log-label").textContent = "Attach debug log (" + n +
      (n === 1 ? " line, " : " lines, ") + reportLogKB(dbgRecText(reportLogRec)) + " KB)";
  }
  dbgRecHeld = fromOff && !!reportLogRec;
  showSheet(true, "sheet-report");
}

$("btn-report").addEventListener("click", () => openReport());
$("btn-report-cancel").addEventListener("click", () => showSheet(false));

let reportSending = false;

async function sendReport() {
  if (reportSending) return;
  const withLog = !$("report-log-row").hidden && $("report-log").checked;
  const message = reportMessage($("report-msg").value, withLog);
  if (!message) {
    showReportError("Please describe what went wrong.");
    $("report-msg").focus();
    return;
  }
  const btn = $("btn-report-send");
  reportSending = true;
  btn.disabled = true;
  btn.textContent = "Sending…";
  $("report-error").classList.remove("show");
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), REPORT_TIMEOUT);
  try {
    const r = await fetch(REPORT_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: reportBody({
        message: message,
        email: $("report-email").value.trim(),
        // The block the sheet is showing, not a fresh one: what was on screen
        // is what the user agreed to send.
        diag: $("report-diag").checked ? $("report-diag-text").textContent : "",
        website: $("report-website").value,
      }, $("report-log").checked ? reportLogRec : null),
      signal: ctl.signal,
    });
    const data = await r.json().catch(() => null);
    if (!r.ok) {
      showReportError(data && data.error ? data.error : REPORT_SEND_FAILED);
      return;
    }
    $("report-msg").value = "";
    $("report-email").value = "";
    showSheet(false);
    toast("Sent. Thanks!");
  } catch (e) {
    // A refusal, a dead network or the 30s timer, all one outcome here: the
    // sheet stays up with the text still in it, because a failed send must not
    // be the thing that loses the report. The mailto line below is the way out.
    dbg("report: send failed", e);
    showReportError(REPORT_SEND_FAILED);
  } finally {
    clearTimeout(timer);
    reportSending = false;
    btn.disabled = false;
    btn.textContent = "Send";
  }
}

$("btn-report-send").addEventListener("click", sendReport);
$("report-msg").addEventListener("input", () => $("report-error").classList.remove("show"));
