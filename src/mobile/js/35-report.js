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
const REPORT_TIMEOUT = 15000;
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
// starting or ending, Settings opening — never on a timer. The key pill's ring
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

// What the <details> block shows and what the send carries — one function, so
// the two can never disagree about what is going out.
function reportDiagnostics() {
  const vv = window.visualViewport;
  return [
    "version: " + buildVersion(),
    "ua: " + navigator.userAgent,
    "platform: " + (a2hsPlatform() || "desktop"),
    "standalone: " + a2hsInstalled(),
    "shell: " + reportShell(),
    "viewport: " + window.innerWidth + "x" + window.innerHeight +
      "  vv=" + (vv ? Math.round(vv.height) : "-") + "  dpr=" + window.devicePixelRatio,
    "screen: " + screen.width + "x" + screen.height,
    "wide: " + isWideLayout(),
    "online: " + navigator.onLine,
    "debug log (last " + REPORT_DBG_LINES + " lines):",
    dbgOn ? dbgBuf.slice(-REPORT_DBG_LINES).join("\n")
          : "(off — enable Debug log in Settings, reproduce, then report)",
  ].join("\n");
}

function showReportError(text) {
  $("report-error").textContent = text;
  $("report-error").classList.add("show");
}

function openReport() {
  $("report-error").classList.remove("show");
  $("report-diag-text").textContent = reportDiagnostics();
  showSheet(true, "sheet-report");
}

$("btn-report").addEventListener("click", openReport);
$("btn-report-cancel").addEventListener("click", () => showSheet(false));

let reportSending = false;

async function sendReport() {
  if (reportSending) return;
  const message = $("report-msg").value.trim();
  if (!message) { $("report-msg").focus(); return; }
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
      body: JSON.stringify({
        message: message,
        email: $("report-email").value.trim(),
        // The block the sheet is showing, not a fresh one: what was on screen
        // is what the user agreed to send.
        diag: $("report-diag").checked ? $("report-diag-text").textContent : "",
        website: $("report-website").value,
      }),
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
    // A refusal, a dead network or the 15s timer, all one outcome here: the
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
