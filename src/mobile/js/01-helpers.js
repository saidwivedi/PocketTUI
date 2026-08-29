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
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && confirmResolve) { e.preventDefault(); settleConfirm(false); }
});

