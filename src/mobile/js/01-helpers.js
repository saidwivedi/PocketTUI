// ============================================================
// Helpers
// ============================================================
function $(id) { return document.getElementById(id); }
function el(tag, attrs={}, ...kids) {
  const e = document.createElement(tag);
  for (const [k,v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k === "html") e.innerHTML = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v);
  }
  for (const k of kids) {
    if (k == null) continue;
    e.appendChild(typeof k === "string" ? document.createTextNode(k) : k);
  }
  return e;
}
function svgIcon(id) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  const use = document.createElementNS(ns, "use");
  use.setAttributeNS("http://www.w3.org/1999/xlink", "xlink:href", "#" + id);
  use.setAttribute("href", "#" + id);
  svg.appendChild(use);
  return svg;
}
function toast(msg, ms=1800) {
  dbg("toast:", msg);
  const t = $("toast"); t.textContent = msg; t.classList.add("show");
  clearTimeout(toast._t); toast._t = setTimeout(()=>t.classList.remove("show"), ms);
}
// A themed stand-in for confirm(): the native dialog wears the OS's look, not
// the app's. The question rides the app's own modal idiom — a bottom sheet
// shown through showSheet(), whose single-sheet rule also closes whatever
// sheet asked it. One question at a time — the pending resolver doubles as
// the "open" flag. Closing the sheet any way but the OK button (Cancel, the
// scrim, Escape, another sheet taking over) answers no: showSheet() settles
// every hide, so no path can leave the promise dangling.
let confirmResolve = null;
function appConfirm(message, opts={}) {
  return new Promise((resolve) => {
    confirmResolve = resolve;
    $("confirm-msg").textContent = message;
    $("btn-confirm-ok").textContent = opts.confirmLabel || "OK";
    showSheet(true, "sheet-confirm");
    $("btn-confirm-cancel").focus();
  });
}
function settleConfirm(answer) {
  if (!confirmResolve) return;
  const resolve = confirmResolve;
  confirmResolve = null;   // before showSheet: its settle hook must not loop
  showSheet(false);
  resolve(answer);
}
$("btn-confirm-ok").addEventListener("click", () => settleConfirm(true));
$("btn-confirm-cancel").addEventListener("click", () => settleConfirm(false));


// ============================================================
// Reaching the server: naming the failure
// ============================================================
// One table for every way the phone can fail to reach the computer, so the
// list card, the terminal's banner and the toasts all say the same thing about
// the same verdict. A command here is text to read and type on the computer;
// nothing in the shell ever runs one.
const FAILURE_COPY = {
  offline: {
    title: "You're offline",
    hint: "This device has no network right now. It will pick up where it left off once one is back.",
  },
  mixed_content: {
    title: "Address must be https",
    hint: "This page is served over https, so the browser refuses to call an http address. Use the https form of the address in Settings.",
  },
  unreachable: {
    title: "Can't reach your computer",
    hint: "Nothing answered at that address. Check the computer is awake and on the same tailnet, and that the address in Settings is right.",
  },
  not_published: {
    title: "Published, but not under /pockettui",
    hint: "Something answers at that address, but not on this path. On the computer, run:",
    command: () => "tailscale serve --bg --set-path /pockettui " +
      (backendParts(cfg.backend).port || "5560"),
  },
  proxy_dead: {
    title: "The computer answered, but PocketTUI isn't running",
    hint: "The proxy is up and the server behind it is not. On the computer, run the command below, or on a Mac run launchctl kickstart -k gui/$(id -u)/com.pockettui.server.",
    command: "systemctl --user restart pockettui",
  },
  not_pockettui: {
    title: "Something else answers at this address",
    hint: "That address belongs to another service. Check the address in Settings.",
  },
  blocked: {
    title: "Blocked before it reached PocketTUI",
    hint: "Something in between refused the request. A tailnet access rule or a browser extension is the usual cause.",
  },
  server_error: {
    title: "PocketTUI hit an error",
    hint: "The server is running but the request failed. On the computer, see what it logged:",
    command: "journalctl --user -u pockettui -n 50 --no-pager",
  },
};

// The device's own answer about its network. Only a definite no counts:
// browsers report true whenever an interface is up, captive portal or not.
function netOffline() { return navigator.onLine === false; }

// One unauthenticated GET of the health descriptor, to tell apart the things a
// failed API call cannot: nothing listening, a proxy with nothing behind it, a
// proxy that does not route this path, and a server that answered fine (which
// makes the failure the API call's own). Never sends the pairing code — this
// runs precisely when the code may be the thing at fault.
async function probeServer(timeoutMs = 2500) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  let r;
  try {
    r = await fetch(apiURL(".well-known/pockettui"), { cache: "no-store", signal: ctrl.signal });
  } catch (e) {
    // A refused connection, a DNS miss, a blocked mixed-content request and
    // the timeout above all land here indistinguishably.
    return { kind: "unreachable", status: 0 };
  } finally {
    clearTimeout(timer);
  }
  const status = r.status;
  if (status === 502 || status === 503 || status === 504) return { kind: "proxy_dead", status };
  if (status === 403) return { kind: "blocked", status };
  if (status === 404) {
    // FastAPI's own 404 is JSON carrying a `detail` key: that is our server,
    // just older than this route. Any other 404 came from something in front
    // of it that does not route this path here.
    let body = null;
    try { body = await r.json(); } catch (e) {}
    const ours = !!body && typeof body.detail !== "undefined";
    return { kind: ours ? "old_server" : "not_published", status };
  }
  if (status === 200) {
    let body = null;
    try { body = await r.json(); } catch (e) {}
    if (!body || body.product !== "pockettui") return { kind: "not_pockettui", status };
    return { kind: "ok", status };
  }
  return { kind: "server_error", status };
}

// The verdict, from whatever the caller has: the thrown error, the response it
// got (when it got one), and the probe (null when there was no point asking).
// Order matters — the checks that need no network come first, so an offline
// device or an http address in an https page is named before any probe result
// that could only have been "unreachable" anyway. A 401 never arrives here;
// the caller has the server's own hint for that one.
function classifyFailure(err, response, probe) {
  let kind = "unreachable";
  if (netOffline()) kind = "offline";
  else if (location.protocol === "https:" && /^http:/i.test(cfg.backend || "")) kind = "mixed_content";
  else if (response && response.status === 403) kind = "blocked";
  else if (probe && FAILURE_COPY[probe.kind]) kind = probe.kind;
  else if (probe && (probe.kind === "ok" || probe.kind === "old_server")) kind = "server_error";
  const c = FAILURE_COPY[kind];
  return {
    kind: kind,
    title: c.title,
    hint: c.hint,
    command: typeof c.command === "function" ? c.command() : (c.command || ""),
  };
}
