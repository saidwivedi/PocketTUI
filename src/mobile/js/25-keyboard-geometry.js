// ============================================================
// Soft-keyboard geometry
// ============================================================
// visualViewport shrinks when the iOS keyboard opens. Pin the terminal screen to
// the visible rectangle so the key bar rides just above the keyboard and the
// terminal refits to what's actually on screen.
//
// Two ways iOS raises a keyboard, and they report differently. For the terminal
// it just overlays: innerHeight holds still and vv.height shrinks. For a visible
// input at the page bottom it also SCROLLS the field into view, panning the
// layout viewport up (vv.offsetTop > 0) and shrinking innerHeight to match — so
// innerHeight - vv.height - vv.offsetTop collapses to 0 and the old formula read
// "no keyboard" at the exact moment one was covering half the screen.
//
// So the full height is tracked instead of inferred: the tallest visible viewport
// seen while nothing is focused. It is a measurement, never a stored constant,
// and it is re-derived whenever the device geometry itself changes.
let fullViewH = 0;
// How much viewport a rounding artefact may swallow before it counts as a
// keyboard. Not a device measurement — it is one CSS line-height, the smallest
// shrink that could hide anything, read off the root's own type rather than
// picked. Any real keyboard clears it by an order of magnitude.
const KB_NOISE_PX = (() => {
  const lh = parseFloat(getComputedStyle(document.documentElement).lineHeight);
  return Number.isFinite(lh) ? Math.ceil(lh) : 24;
})();
// The tallest the visible viewport has been, which is what "no keyboard" looks
// like. Focus is not the test — xterm keeps its hidden textarea focused for the
// whole session with no keyboard up — so the only frames rejected are the ones a
// pan has already distorted. A keyboard can only ever shrink this, so taking the
// maximum is what makes it self-correcting rather than a stored constant.
function noteFullHeight() {
  const vv = window.visualViewport;
  if (!vv || vv.offsetTop > 0) return;
  fullViewH = Math.max(fullViewH, Math.round(vv.height));
}

// How much of the layout viewport is hidden below the visible rectangle. Anything
// position:fixed anchors to the layout viewport, so this is what a bottom-anchored
// element has to lift itself by to clear the keyboard.
function occludedBottom(vv) {
  return Math.max(0, Math.round(window.innerHeight - (vv.offsetTop + vv.height)));
}
// Published for the stylesheet, because a toast can fire from any screen — the
// settings sheet raises a keyboard over the session list too, and applyViewport's
// terminal geometry never runs there. The full keyboard-up heuristic below needs
// measurements only the terminal screen collects, so off that screen the test is
// the plain one: something typeable is focused and the viewport is short.
function setKbInset(px) {
  document.documentElement.style.setProperty("--kb-inset", px + "px");
}
function typingFocus() {
  const a = document.activeElement;
  if (!a) return false;
  if (a.isContentEditable) return true;  // the editor's CodeMirror surface
  const tag = a.tagName;
  return tag === "TEXTAREA" || (tag === "INPUT" && !/^(button|checkbox|radio|submit)$/i.test(a.type));
}

function applyViewport() {
  const vv = window.visualViewport;
  const scr = $("screen-term");
  if (!vv) return;
  if (!scr.classList.contains("active")) {
    setKbInset(typingFocus() ? occludedBottom(vv) : 0);
    return;
  }
  noteFullHeight();
  // The app is a fixed full-viewport UI, so any pan is always wrong. Undoing it
  // is what puts the key bar back on screen; the pinned root above should stop it
  // happening at all, and this is the backstop for when it slips through.
  if (vv.offsetTop > 0 || window.scrollY > 0 || window.scrollX > 0) {
    window.scrollTo(0, 0);
  }
  // Keyboard-up is decided by the one number the pan cannot forge: how much of
  // the full viewport is still visible. Once a full height has been measured that
  // answer is authoritative — a field can stay focused with the keyboard already
  // dismissed, so focus only stands in before the first measurement exists.
  const measured = fullViewH > 0;
  const kb = measured ? Math.round(fullViewH - vv.height) : 0;
  // A soft keyboard is never a small event: it claims a large fraction of the
  // screen on any device, where everything else that shortens the viewport — a
  // one-pixel fullViewH latch, the installed app's stuck-short reading after a
  // dismissal, a tab's URL bar — stays in the tens of pixels. A fixed floor can
  // sit between those bands on one phone and inside one of them on another, so
  // the floor is a fraction of the measured full height (with the line-height
  // rounding tolerance as the lower bound while fullViewH is still small).
  // Deciding by proportion also covers the case focus cannot: xterm keeps its
  // textarea focused for the whole session, so a stuck-short reading with the
  // keyboard already dismissed looks focused too — but it never looks big.
  const shrunk = measured && kb > Math.max(KB_NOISE_PX, fullViewH * 0.2);
  // A shrunken viewport is necessary evidence of a keyboard but not sufficient:
  // iOS is documented to leave vv.height stuck short of the real visible area
  // after a keyboard interaction in the installed app, and a stuck reading looks
  // exactly like a small keyboard. Focus is what tells them apart — not as proof
  // a keyboard IS up (one can be dismissed with the field still focused, which is
  // why shrink still has to agree) but as proof one CANNOT be: nothing focused,
  // nothing to type into, so the shrink is the viewport lying and the geometry
  // belongs back with the stylesheet. That is a fact about keyboards rather than
  // about any device.
  const up = measured ? (shrunk && keyboardUp()) : keyboardUp();
  // The explicit size exists solely to hold the screen above the keyboard. With
  // no keyboard there is nothing to hold it above, and vv.height is then a
  // liability: in the installed PWA it reports short of what is actually visible,
  // so pinning the screen to it stops the key bar early and leaves a blank strip
  // beneath. (In a tab the same shortfall is the URL bar — real occlusion — which
  // is why the tab case looked right.) Handing the geometry back to the
  // stylesheet's inset:0/100dvh is what makes the bar sit on the true bottom.
  if (up) {
    scr.style.height = vv.height + "px";
    // offsetTop is what is left of the pan after the reset above; adding it keeps
    // the screen aligned with the visible rectangle either way.
    scr.style.top = vv.offsetTop + "px";
    // Remembered so the poll below can tell what the screen is pinned to from what
    // the viewport currently reads.
    pinnedH = Math.round(vv.height);
    pinnedTop = Math.round(vv.offsetTop);
  } else {
    scr.style.height = "";
    scr.style.top = "";
  }
  // With the keyboard up there is no home-indicator gap to reserve.
  scr.style.setProperty("--kb-safe", up ? "0px" : "");
  const inset = up ? occludedBottom(vv) : 0;
  setKbInset(inset);
  // Transitions only: this runs on every viewport event, and a line per event
  // would bury everything else in the panel within seconds.
  if (up !== kbWasUp) {
    kbWasUp = up;
    if (up) startKbPoll(); else stopKbPoll();
    dbg("kb", up ? "up" : "down", "full=" + fullViewH, "vv=" + Math.round(vv.height), "inset=" + inset);
  }
  refit();
}
let kbWasUp = false;
// What the screen is currently pinned to, kept beside the styles that set it.
let pinnedH = 0, pinnedTop = 0;
// The dismissal iOS does not announce. Every other path back through
// applyViewport is an event — a viewport resize or scroll, a focusout, a
// visibility change, a window resize — and iOS can restore the viewport with none
// of them firing. Focus cannot fill the gap either: xterm holds its hidden
// textarea focused for the whole session. So while the keyboard is believed up
// the viewport is read rather than waited for, and the screen is only unpinned
// when the two disagree. The interval is cadence, not geometry, and it exists
// only between the keyboard arriving and leaving, so an idle session runs
// nothing. The comparison is what keeps it cheap: applyViewport ends in refit(),
// which fits xterm and sends a resize frame to the host, so a tick that sees the
// geometry it pinned does nothing at all.
let kbPollTimer = null;
function startKbPoll() {
  if (kbPollTimer) return;
  kbPollTimer = setInterval(() => {
    const vv = window.visualViewport;
    const scr = $("screen-term");
    if (!vv || !scr || !scr.classList.contains("active")) { stopKbPoll(); return; }
    if (Math.round(vv.height) !== pinnedH || Math.round(vv.offsetTop) !== pinnedTop) applyViewport();
  }, 250);
}
function stopKbPoll() {
  if (!kbPollTimer) return;
  clearInterval(kbPollTimer);
  kbPollTimer = null;
}
if (window.visualViewport) {
  window.visualViewport.addEventListener("resize", applyViewport);
  window.visualViewport.addEventListener("scroll", applyViewport);
}
// The screen is sized from the visible rectangle, so it has to be re-measured
// after the keyboard goes away as well as when it arrives. iOS does not always
// report the dismissal as a visualViewport resize — it can restore the viewport
// silently — which left the bar pinned at the keyboard-up height with a gap
// beneath it. These are the other moments the geometry is known to have settled.
const settleViewport = () => {
  applyViewport();
  // One re-read after the keyboard's animation, because the event that prompted
  // this can arrive before the viewport finishes moving. Cadence, not geometry.
  setTimeout(applyViewport, 300);
};
// Focusing the compose field is the moment iOS decides to scroll it into view.
// Catching the focus itself undoes the pan on the same turn rather than leaving
// the bar off screen until some later viewport event.
document.addEventListener("focusin", (e) => {
  if (e.target === $("compose-text")) applyViewport();
});
// Blur is the usual precursor to the keyboard leaving.
document.addEventListener("focusout", settleViewport);
// Returning to a resumed app: the keyboard is normally gone by now, and nothing
// else will say so.
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) settleViewport();
});
// Rotating changes what "full" means, so the tracked height is thrown away and
// measured again rather than carried across the turn.
window.addEventListener("orientationchange", () => setTimeout(() => {
  fullViewH = 0;
  settleViewport();
  refit(160);
}, 250));
// A window resize means the layout viewport itself changed — the screen needs
// re-sizing, not just a refit of what is inside it.
window.addEventListener("resize", settleViewport);

// A viewport event says the keyboard moved. It does not say the geometry that
// implies has finished moving, and on iOS one part of it is reliably late:
// env(safe-area-inset-bottom) reads 0 while the keyboard is covering the home
// indicator and only returns to its real value some frames after the viewport is
// already back at full height. The key bar's bottom padding is built on that
// inset, so the refit above measures a #term-host that is still a home indicator
// too tall, hands xterm a row more than the box will end up showing, and then the
// inset lands, the bar grows into the grid, and nothing asks again — the bottom
// row, which is where an agent TUI keeps its status line, is left under the bar.
//
// Guessing that delay with another timer only moves the guess. The box itself is
// watched instead, so whatever moves it — the safe area settling, the key bar
// expanding, a rotation, the compose strip opening — arrives as the same one
// correction rather than as a list of events each carrying its own delay.
// refit()'s existing debounce is what collapses a burst of settling layout into a
// single fit, and it is the only fit path either way.
//
// This cannot chase its own tail. #term-host is the flex column's one growable
// child, so its height is whatever the screen has left over after the key bar and
// never what xterm renders inside it: a fit resizes the grid within the box and
// leaves the box exactly where it was. (Beside the desktop pill the bar is out of
// the column altogether and the host simply fills the pane.) The remembered size
// is the second lock — an observation that reports the size already fitted at is
// dropped before it can reach the timer.
if (typeof ResizeObserver !== "undefined") {
  const host = $("term-host");
  let seenW = 0, seenH = 0;
  new ResizeObserver(() => {
    if (!term || demoMode || !$("screen-term").classList.contains("active")) return;
    const w = Math.round(host.clientWidth), h = Math.round(host.clientHeight);
    if (w === seenW && h === seenH) return;
    seenW = w; seenH = h;
    refit();
  }).observe(host);
}
