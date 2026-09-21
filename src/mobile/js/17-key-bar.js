// ============================================================
// Key bar
// ============================================================
// One row of keys collapsed; tapping the arrows key opens a second row and the
// four directions take over the inverted-T. `cls` carries the grid placement for
// the expanded state, `only` which state a key appears in — the DOM survives an
// arrows/collapse toggle untouched, so armed modifiers and in-flight auto-repeat
// survive it too (buildKeybar() in 19-voice-capture.js does rebuild the bar
// wholesale, but only from Settings, whose scrim blocks the bar meanwhile).
// Order here is the collapsed order and the grid's auto-placement order.
// `swipe` is a key's swipe-up alternate, Termux-style: `seq` is what an upward
// swipe sends instead of the tap. Toggles (arrows, mic, close) carry none —
// they have no seq to trade.
const KEYS = [
  { label: "esc",   seq: "\x1b",  cls: "span-2", swipe: { seq: "`" } },
  { label: "tab",   seq: "\t",    cls: "span-2", swipe: { seq: "|" } },
  { label: "ctrl",  mod: "ctrl",  cls: "span-2", swipe: { seq: "~" } },
  { label: "shift", mod: "shift", cls: "span-2", swipe: { seq: "_" } },
  // Meta, for the line editors: alt+b/f/. and friends arrive as ESC-prefixed
  // characters, composed in seqWithMods and the typed-character hook exactly
  // like ctrl and shift are. Gated on cfg.altKeyOn — off by default, like the
  // search key above. Always built (see buildKeybar()) rather than skipped:
  // the expanded grid's later columns are pinned by absolute number, so a
  // missing word-key button would leave its column an empty gap rather than
  // closing up. The .k-alt class is instead display:none'd by the .no-alt
  // grid variant in styles.css, which also renumbers those later columns.
  { label: "alt",   mod: "alt",   cls: "span-2 k-alt", swipe: { seq: "/" } },
  // A terminal's Enter is a carriage return; \n would send a bare line feed that
  // readline and tmux both read as ctrl+J instead. No repeat — a held Enter
  // firing a shell command over and over is never what a thumb meant. narrow,
  // so the glyph keys — enter, backspace, arrows, mic — all share one width.
  { label: "⏎",     seq: "\r",    narrow: true, cls: "span-2 k-enter", aria: "Enter",
    swipe: { seq: "-" } },
  { label: "⌫",     seq: "\x7f",  narrow: true, repeat: true, cls: "span-2 k-bs", aria: "Backspace",
    swipe: { seq: "\x1b[3~" } },
  { icon: "i-arrows", arrows: true, narrow: true, only: "collapsed", aria: "Show arrow keys" },
  // Focus is the point of this key, not a side effect — it opens the compose
  // strip and puts the caret in it, so it shares the keyboard toggle's exemption
  // from the focus-preserving preventDefault below. It keeps the mic face while
  // a take runs: it is what the microphone being open looks like, while ending
  // the take belongs to the strip's Send button. Not built at all on touch (see
  // buildKeybar()): there the strip is always up and its own button is the mic.
  // First in the pill, so the row reads mic, folder, browser, report.
  { icon: "i-mic", compose: true, focusing: true, narrow: true,
    cls: "k-compose", aria: "Show or hide the message bar" },
  // The terminal's way into the file explorer, at the pane's own cwd.
  // Collapsed-row only, so the expanded grid's hand-placed columns stay as
  // they are — arrow work and file browsing are different errands anyway.
  { icon: "i-folder", files: true, narrow: true, only: "collapsed",
    cls: "k-files", aria: "Browse files in the session's folder" },
  // Pill only, like the report key below and hidden by the same CSS: the pane
  // wants the width of a two-pane layout, so the phone does not carry a way
  // into it (founder, 2026-09-21). Shown only once a server says it can proxy.
  { icon: "i-globe", browser: true, narrow: true, only: "collapsed",
    cls: "k-browser", aria: "Open a browser" },
  // The arrows' alternates are the nav keys that live beside them on a real
  // keyboard: Home/End across, PgUp/PgDn along.
  { label: "←",     seq: "\x1b[D", narrow: true, repeat: true, cls: "k-left",  only: "expanded",
    swipe: { seq: "\x1b[H" } },
  { label: "↓",     seq: "\x1b[B", narrow: true, repeat: true, cls: "k-down",  only: "expanded",
    swipe: { seq: "\x1b[6~" } },
  { label: "↑",     seq: "\x1b[A", narrow: true, repeat: true, cls: "k-up",    only: "expanded",
    swipe: { seq: "\x1b[5~" } },
  { label: "→",     seq: "\x1b[C", narrow: true, repeat: true, cls: "k-right", only: "expanded",
    swipe: { seq: "\x1b[F" } },
  // Pill only, and only once there is a backend to report about — the phone
  // never shows this key, because the Settings row is its way in and the
  // docked row has no width to spare. Collapsed-row only for the reason the
  // folder key is.
  { icon: "i-report", report: true, narrow: true, only: "collapsed",
    cls: "k-report", aria: "Report a problem", title: "Report a problem" },
  { icon: "i-close", collapse: true, narrow: true, cls: "k-close", only: "expanded", aria: "Hide arrow keys" },
];

// Session-only, deliberately not persisted. Expanding is a per-task gesture — you
// open the arrows to move through a file and want the row back for the height as
// soon as you are typing again — so restoring it on every launch would cost rows
// the user did not ask to spend. cfg's persisted settings are all preferences;
// this is a transient mode.
let arrowsOpen = false;

function setArrows(open) {
  if (open === arrowsOpen) return;
  arrowsOpen = open;
  $("keybar").classList.toggle("expanded", open);
  // The bar is a flex sibling of #term-host, so its new height takes rows away
  // from the terminal — xterm only learns that from a fit.
  refit(0);
}

// Transient like arrowsOpen, and for the same reason: dictating is a per-task
// gesture, not a preference to restore on launch.
let composeOpen = false;
let composeBlurTimer = null;
// Whether what is in the box came out of a microphone. Typed text is sent with
// the keyboard's own send key, so the button stays the microphone while a thumb
// is writing — turning it into an arrow there would leave a typed box with no
// way into dictation at all. A transcript is the other case: it arrives with no
// keyboard on screen to send it with, so the button becomes that arrow the
// moment one lands, and stays it while the transcript is edited. Set at the two
// landing points (stopListening, codeMicFinish) and cleared in composeArm() the
// moment the box is empty again, whoever emptied it.
let composeDictated = false;

// Whether the strip is furniture rather than a mode right now. On touch it is
// the terminal's typing surface — a tap on the grid puts the caret in it and it
// stays whatever the keyboard then does — so nothing on that screen may take it
// away; beside a real keyboard it is still summoned by the mic key and leaves
// with the blur. Leaving the terminal screen is the one close that stands, and
// it needs no exception: closeTerminal() drops the screen's class before it
// asks, so this answers false by then.
function composeDocked() {
  return touchOnly() && $("screen-term").classList.contains("active");
}

// Whether the caret is in the box right now. Docked, the strip is furniture and
// composeOpen is true for the whole session, so being open no longer says
// anything about where an unaddressed paste belongs — only focus does. See
// pasteGoesToCompose() for the gestures that answer the question themselves.
function composeFocused() {
  return composeOpen && document.activeElement === $("compose-text");
}

// Where a paste belongs, for the two routines that insert one. A gesture that
// belongs to a surface names it and the caret has no say: the long-press pill is
// the terminal's own ("term"), the "+" on the strip is the composer's
// ("compose"), and a picker that hands focus back to the page body must not
// turn the second into the first. A clipboard key or a paste event belongs to
// neither, names nothing, and the caret answers for it. "compose" still asks
// whether the strip is open at all, the question the whole rule used to be:
// beside a real keyboard it closes, and a path written into a box nobody can
// see would be lost.
function pasteGoesToCompose(target) {
  if (target === "term") return false;
  if (target === "compose") return composeOpen;
  return composeFocused();
}

// The single close routine — every path in and out of the strip goes through it.
// Closing keeps whatever is typed: dismissing the keyboard is not a discard, and
// reopening should show the half-dictated sentence again. Only Send clears.
// `quiet` opens the strip without focusing it. Speech recognition wants the strip
// visible but no keyboard — the whole point is to see the terminal while talking.
function setCompose(open, quiet) {
  // A deliberate toggle settles the question a pending blur was about to answer.
  clearTimeout(composeBlurTimer);
  composeBlurTimer = null;
  // Docked, a close is at most a request to put the keyboard away: the strip
  // stays, with whatever is in it. The one guard every close path inherits,
  // rather than each of them asking on its own. What the close would otherwise
  // have torn down — a running take — now stands down at its own call site; see
  // composeDiscardCapture().
  if (!open && composeDocked()) { $("compose-text").blur(); return; }
  if (open === composeOpen) return;
  composeOpen = open;
  const strip = $("compose"), ta = $("compose-text");
  strip.classList.toggle("open", open);
  // Focusing is what raises the keyboard with its dictation key; blurring is the
  // only way to put that keyboard away again. Reopening can bring back text kept
  // from last time, so the button is told again rather than assumed empty.
  if (open) {
    composeArm();
    if (!quiet) ta.focus();
  } else {
    ta.blur();
  }
  // Closing is the last word on listening: every close path — mic key, blur, Send,
  // leaving the terminal — inherits the stop from here rather than repeating it.
  if (!open) stopListening();
  // Same for a recording: the strip going away is the user abandoning it, so the
  // capture is dropped rather than left running behind a closed bar.
  if (!open) cancelRecording();
  // A transcript the user walked away from teaches nothing: whatever they do
  // with the box next is a new errand, and comparing it against a proposal from
  // the last one would invent an edit nobody made.
  if (!open) learnHeard = null;
  // The strip is a flex sibling of #term-host, so showing it takes rows away
  // from the terminal — xterm only learns that from a fit.
  refit(0);
}

// The button's three faces and the one order that settles them: a capture in
// flight owns it outright — the tap ends the take, or cancels the upload it
// turned into, whatever is in the box — then a transcript waiting to go, and
// otherwise the microphone. Both engines count as a capture, or the phone
// recogniser's interim words would turn the stop back into a send mid-sentence.
// Typed text is deliberately not the send case: see composeDictated. The cross
// inside the box is read off the plainer question, and still means what it
// always did — there is something in the box to clear.
//
// Every path that moves either the box (composeGrow, setCompose) or the capture
// state machine (recSyncMic, which every start, stop, cancel and failure already
// lands on) comes here, so no two of them can leave the button saying different
// things.
function composeArm() {
  const has = $("compose-text").value.length > 0;
  // An empty box has no transcript in it, however it came to be empty — sent,
  // cleared, or backspaced away by hand. One place to clear the flag, since one
  // place is where every one of those paths ends up.
  if (!has) composeDictated = false;
  const send = $("compose-send");
  const stopping = composeStopping() || listening();
  const sending = has && composeDictated && !stopping && !recBusy;
  send.classList.toggle("stop", stopping);
  send.classList.toggle("busy", recBusy);
  send.classList.toggle("mic", !stopping && !recBusy && !sending);
  send.classList.toggle("armed", sending);
  // The engine the session fell back to is the button's footnote on a phone,
  // where the key that used to carry it is not built.
  send.classList.toggle("forced-phone", voiceLatchVisible());
  $("compose").classList.toggle("has-text", has);
  // What this tap does, in the words of the state it lands in.
  send.setAttribute("aria-label",
    stopping ? "Stop recording and keep what was heard"
    : recBusy ? "Cancel transcription"
    : sending ? "Send to the terminal"
    : "Start dictation");
}

// How many lines the box may grow to before it scrolls instead. Five is about
// where a message stops being a line and starts being a screen, and the rows it
// costs the terminal are rows the user is looking away from anyway.
const COMPOSE_MAX_LINES = 5;

// The field starts at one line and grows with what is typed, up to the ceiling
// above, after which the browser's own textarea scrolling takes over and keeps
// the caret in view. Measured rather than counted: the height is released to
// "auto" first, or scrollHeight would report the height already set rather than
// what the text needs. Runs on every input, which is also where the Send button
// and the cross learn whether there is anything to act on.
function composeGrow() {
  const ta = $("compose-text");
  composeArm();
  ta.style.height = "auto";
  const cs = getComputedStyle(ta);
  const line = parseFloat(cs.lineHeight) || 22;
  const pad = parseFloat(cs.paddingTop) + parseFloat(cs.paddingBottom);
  // Everything in this app is border-box, so scrollHeight is content plus
  // padding and the border is what has to be added back to get a height to set.
  const border = parseFloat(cs.borderTopWidth) + parseFloat(cs.borderBottomWidth);
  const max = Math.round(COMPOSE_MAX_LINES * line + pad + border);
  const want = Math.round(ta.scrollHeight + border);
  ta.style.height = Math.min(want, max) + "px";
  ta.style.overflowY = want > max ? "auto" : "hidden";
  refit(0);
}

// The whole point of the strip: the composed text reaches the terminal once, as
// a paste, so xterm's IME handling never gets to re-send it. term.paste() also
// applies bracketed-paste framing when the running app asked for it, which is
// what keeps a multi-line dictation from executing line by line. A \r follows
// the paste, so the tap that sends is also the tap that submits: dictation is
// spoken as a finished instruction, and asking for the key bar's ⏎ on top of
// Send was an extra step. The \r goes through send() rather than the paste,
// so it is never framed as pasted text and the docked explorer still sees it
// as an Enter. Under bracketed paste the app receives the message whole and
// then the Enter, which is the same sequence a typed paste plus ⏎ produces.
// `keepCaret` is the keyboard send saying so: Enter was pressed in the box, the
// keyboard is up, and the caret belongs back in the emptied field for the next
// line. A tap on the button never asks for it — after a dictation there is no
// keyboard on screen, and focusing a field inside a tap is exactly what raises
// one, which is the thing talking to the terminal is meant to avoid.
function composeSend(keepCaret) {
  // A capture in flight owns this button before any of that: while a take runs
  // it is the stop control, and while its upload runs it is the cancel. Neither
  // tap sends, and neither closes the strip — the transcript is on its way into
  // the box the user is looking at.
  if (composeStopTap()) return;
  // The phone's recogniser is the other capture that owns this button: while it
  // runs the button wears the same stop face, and the tap that ends the take
  // leaves what it heard in the box to read before sending it.
  if (listening()) { stopListening(); return; }
  const ta = $("compose-text");
  const text = ta.value;
  if (!text || !term) return;
  term.paste(text);
  send("\r");
  // The edit the user made to a dictated transcript before sending it is the
  // only evidence this app ever gets about what the microphone gets wrong, and
  // this is the one moment it exists. Read here, between the paste and the
  // clear, because `text` is the outgoing message and learnHeard is about to be
  // reset — and deliberately not before the paste, since nothing about learning
  // may stand between the user's tap and their text reaching the terminal.
  const heard = learnHeard;
  learnHeard = null;
  if (heard && text !== heard) learnSend(heard, text);
  // Blur before clearing, not after. Dictated text leaves iOS holding an open
  // composition on this field — the keyboard's mic key does it directly, and the
  // code button's focus()/setSelectionRange() at the end of codeMicFinish() puts
  // the field back under the same keyboard. Assigning "" to a field WebKit still
  // considers under composition does not stick: the composition buffer reconciles
  // afterwards and writes its text back, so the message sends but the box stays
  // full. Blurring ends the composition first, which discards that buffer and
  // makes the clear final. Typed text has no pending composition and does not
  // care either way, so the ordering costs the working path nothing.
  //
  // iOS only, though: nothing else has that composition buffer, and Android's
  // keyboard reads the blur as the field going away and closes — which the
  // refocus below would then reopen, a whole keyboard animation on every
  // message sent. Everywhere else the plain assignment sticks.
  if (a2hsPlatform() === "ios") ta.blur();
  ta.value = "";
  recogText = "";
  composeGrow();
  // The strip stays for the next message, the way any messaging app's does:
  // sending one line is rarely the end of the errand, and closing here would
  // cost a tap on the terminal and a keyboard animation to say the next one.
  // The caret goes straight back into the emptied box, for a keyboard send;
  // see keepCaret.
  if (keepCaret) ta.focus();
  // The blur above scheduled a close, on the reading that a keyboard leaving is
  // the strip's cue to go. The refocus is the answer that pending close was
  // waiting for, and it is the same cancel setCompose() makes for a deliberate
  // toggle.
  clearTimeout(composeBlurTimer);
  composeBlurTimer = null;
  // The microphone goes with the message: sending is the clearest sign the take
  // is over, and a stream retained for a quick re-record keeps iOS's recording
  // indicator lit until the idle timer gets round to it.
  recRelease();
}

// Empty the box and stay in it. What Send does minus the paste and minus the
// close: a mis-heard transcript is thrown away here and said again, which is the
// errand this button exists for, and closing the strip would make that two taps.
// But an empty box has nothing left to clear — a second tap here (or a tap right
// after the first one emptied it) means "put the strip away", same as the key
// bar's own empty-box tap at toggleCompose(), so it hands off to setCompose(false)
// rather than repeating that close logic.
function composeClear() {
  // A take in flight is what there is to throw away, and it is all this tap
  // throws away: the audio goes, the box comes back exactly as it was before
  // the microphone opened, and the strip stays up to speak or type into again.
  if (recInFlight()) { cancelRecording(); return; }
  const ta = $("compose-text");
  if (!ta.value.trim()) { setCompose(false); return; }
  // Same reason as composeSend(): a live recogniser is mid-utterance and would
  // write its interim tail back into the field we just emptied.
  stopListening();
  // Blur before clearing, for the reason spelled out at length in composeSend():
  // iOS holds an open composition on a dictated field, and assigning "" to it
  // does not stick until the blur has ended that composition. Focus is handed
  // straight back afterwards — the point of clearing is to type or speak again.
  // And iOS only, for the reason given there as well: elsewhere the round trip
  // would flap the keyboard for nothing, so the caret simply stays where it is.
  const ios = a2hsPlatform() === "ios";
  const refocus = ios && document.activeElement === ta;
  if (ios) ta.blur();
  ta.value = "";
  recogText = "";
  // Nothing was sent, so there is no edit to learn from — and what the backend
  // proposed is now gone from the box, so comparing the next send against it
  // would invent one. Dropped without posting to /api/learn.
  learnHeard = null;
  if (refocus) ta.focus();
  composeGrow();
}

// The strip exists to hold a keyboard's dictation; without one it is just a box
// covering the terminal. So whatever drops the keyboard — the iOS dismiss key, a
// swipe down, a tap into the terminal — closes the strip too.
//
// Deferred a tick because blur fires before the click that caused it. Tapping the
// compose key while the strip is open would otherwise close it here and then have
// the key's own handler see composeOpen === false and reopen it. Anything that
// legitimately refocuses within the same tick (or closes the strip outright)
// cancels the pending close.
//
// A local recording is the exception. The indicator takes the textarea's place
// by hiding it, and hiding a focused field blurs it — so a recording started
// from a box the user was typing in would blur its way straight into closing the
// strip it needs. The capture outranks the missing keyboard. recStarting covers
// the gap the other two cannot: the microphone grant is still pending, so there
// is no recorder yet to report itself recording.
function composeBlurred() {
  clearTimeout(composeBlurTimer);
  composeBlurTimer = setTimeout(() => {
    composeBlurTimer = null;
    if (recStarting || recording() || recBusy) return;
    // And on touch the strip is not the keyboard's antechamber but the terminal's
    // typing surface, so a dropped keyboard leaves it exactly where it is.
    if (composeDocked()) return;
    if (composeOpen && document.activeElement !== $("compose-text")) setCompose(false);
  }, 0);
}
