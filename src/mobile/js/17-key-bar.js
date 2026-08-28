// ============================================================
// Key bar
// ============================================================
// One row of keys collapsed; tapping the arrows key opens a second row and the
// four directions take over the inverted-T. `cls` carries the grid placement for
// the expanded state, `only` which state a key appears in — the DOM is built once
// and never rebuilt, so armed modifiers and in-flight auto-repeat survive a
// toggle. Order here is the collapsed order and the grid's auto-placement order.
const KEYS = [
  { label: "esc",   seq: "\x1b",  cls: "span-2" },
  { label: "tab",   seq: "\t",    cls: "span-2" },
  { label: "ctrl",  mod: "ctrl",  cls: "span-2" },
  { label: "shift", mod: "shift", cls: "span-2" },
  // A terminal's Enter is a carriage return; \n would send a bare line feed that
  // readline and tmux both read as ctrl+J instead. No repeat — a held Enter
  // firing a shell command over and over is never what a thumb meant. narrow,
  // so the glyph keys — enter, backspace, arrows, mic — all share one width.
  { label: "⏎",     seq: "\r",    narrow: true, cls: "span-2 k-enter", aria: "Enter" },
  { label: "⌫",     seq: "\x7f",  narrow: true, repeat: true, cls: "span-2 k-bs", aria: "Backspace" },
  { icon: "i-arrows", arrows: true, narrow: true, only: "collapsed", aria: "Show arrow keys" },
  // Collapsed-only, like the arrows toggle above: the expanded row is a full
  // 10-column grid with every cell already spoken for (see .keybar.expanded
  // below), and search is a scrollback errand rather than something the
  // arrow-editing workflow needs beside it. focusing for the same reason the
  // compose key is: opening search means putting the caret in its field.
  { icon: "i-search", search: true, focusing: true, narrow: true, only: "collapsed",
    aria: "Find in scrollback" },
  { label: "←",     seq: "\x1b[D", narrow: true, repeat: true, cls: "k-left",  only: "expanded" },
  { label: "↓",     seq: "\x1b[B", narrow: true, repeat: true, cls: "k-down",  only: "expanded" },
  { label: "↑",     seq: "\x1b[A", narrow: true, repeat: true, cls: "k-up",    only: "expanded" },
  { label: "→",     seq: "\x1b[C", narrow: true, repeat: true, cls: "k-right", only: "expanded" },
  // Focus is the point of this key, not a side effect — it opens the compose
  // strip and puts the caret in it, so it shares the keyboard toggle's exemption
  // from the focus-preserving preventDefault below. icon2 is the face it wears
  // while a local recording runs: this one key drives whichever engine Settings
  // names, so it has to be able to say "stop" as well as "speak".
  { icon: "i-mic", icon2: "i-stop", compose: true, focusing: true, narrow: true,
    cls: "k-compose", aria: "Show or hide compose bar" },
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

// The single close routine — every path in and out of the strip goes through it.
// Closing keeps whatever is typed: dismissing the keyboard is not a discard, and
// reopening should show the half-dictated sentence again. Only Send clears.
// `quiet` opens the strip without focusing it. Speech recognition wants the strip
// visible but no keyboard — the whole point is to see the terminal while talking.
function setCompose(open, quiet) {
  // A deliberate toggle settles the question a pending blur was about to answer.
  clearTimeout(composeBlurTimer);
  composeBlurTimer = null;
  if (open === composeOpen) return;
  composeOpen = open;
  const strip = $("compose"), ta = $("compose-text");
  strip.classList.toggle("open", open);
  // Focusing is what raises the keyboard with its dictation key; blurring is the
  // only way to put that keyboard away again. Reopening can bring back text kept
  // from last time, so the button is told again rather than assumed empty.
  if (open) {
    $("compose-send").classList.toggle("armed", ta.value.length > 0);
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

// The field's height is fixed in CSS now (matches the actions column), so
// there is nothing left to grow — longer text scrolls inside the box via the
// browser's own textarea scrolling, which also keeps the caret in view as you
// type. Runs on every input regardless, since it is where the Send button
// learns whether it has anything to send — presentation only, the empty
// button is still there and still a no-op.
function composeGrow() {
  const ta = $("compose-text");
  $("compose-send").classList.toggle("armed", ta.value.length > 0);
  refit(0);
}

// The whole point of the strip: the composed text reaches the terminal once, as
// a paste, so xterm's IME handling never gets to re-send it. term.paste() also
// applies bracketed-paste framing when the running app asked for it, which is
// what keeps a multi-line dictation from executing line by line. No trailing
// \r — the user reviews it in the terminal and submits with the key bar's ⏎.
function composeSend() {
  // Stop before reading: the recogniser is mid-utterance and whatever is already
  // in the box is what the user meant to send.
  stopListening();
  // A recording, though, is discarded rather than waited for: Send means "this
  // text, now", and the audio has not become text yet.
  if (recording() || recBusy) cancelRecording();
  const ta = $("compose-text");
  const text = ta.value;
  if (!text || !term) return;
  term.paste(text);
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
  ta.blur();
  ta.value = "";
  recogText = "";
  composeGrow();
  // Sending is the end of the errand: the strip closes and takes the keyboard
  // with it, so the terminal gets its rows back to show what just landed.
  setCompose(false);
}

// Empty the box and stay in it. What Send does minus the paste and minus the
// close: a mis-heard transcript is thrown away here and said again, which is the
// errand this button exists for, and closing the strip would make that two taps.
// But an empty box has nothing left to clear — a second tap here (or a tap right
// after the first one emptied it) means "put the strip away", same as the key
// bar's own empty-box tap at toggleCompose(), so it hands off to setCompose(false)
// rather than repeating that close logic.
function composeClear() {
  const ta = $("compose-text");
  if (!ta.value.trim()) { setCompose(false); return; }
  // Same reasons as composeSend(): a live recogniser is mid-utterance and would
  // write its interim tail back into the field we just emptied, and a recording
  // has no text yet to keep.
  stopListening();
  if (recording() || recBusy) cancelRecording();
  // Blur before clearing, for the reason spelled out at length in composeSend():
  // iOS holds an open composition on a dictated field, and assigning "" to it
  // does not stick until the blur has ended that composition. Focus is handed
  // straight back afterwards — the point of clearing is to type or speak again.
  const refocus = document.activeElement === ta;
  ta.blur();
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
    if (composeOpen && document.activeElement !== $("compose-text")) setCompose(false);
  }, 0);
}

