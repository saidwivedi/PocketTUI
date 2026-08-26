// ============================================================
// Speech recognition
// ============================================================
// The mic key talks to the browser's own recogniser when there is one, so the
// keyboard never has to come up and the terminal stays visible while dictating.
// Everything here is best-effort: iOS ships the API but has historically failed
// to start it inside a standalone PWA, so every failure path lands back on the
// keyboard's dictation key, which always works.
const SpeechRec = window.SpeechRecognition || window.webkitSpeechRecognition;
let recog = null;              // the live recogniser, null when not listening
let recogText = "";            // what has been committed, minus the interim tail
// One failure is enough: after it, the mic key goes straight to the keyboard
// rather than prompting again every tap. Session-only, like the other modes.
let recogBroken = false;

function listening() {
  return recog !== null;
}

// A textarea cannot style part of its own value, so the interim tail is plain
// text appended to the committed text and replaced wholesale as results firm up.
// The alternative — a contenteditable overlay — would rewrite the whole strip for
// a cue the recogniser already makes obvious by correcting itself as you speak.
function renderTranscript(interim) {
  const ta = $("compose-text");
  ta.value = recogText + interim;
  composeGrow();
}

function stopListening() {
  if (!recog) return;
  const r = recog;
  recog = null;              // cleared first: onend must not re-enter this
  recSyncMic();
  try { r.stop(); } catch (e) {}
}

// Falling back means: behave exactly as the app did before speech existed —
// open the strip and focus it, so the keyboard's own mic key is one tap away.
function recogFallback(msg) {
  recogBroken = true;
  stopListening();
  if (msg) toast(msg);
  if (!composeOpen) setCompose(true);
  else $("compose-text").focus();
}

// Must be called straight from the tap: iOS only grants the microphone from
// inside a user gesture, the same constraint pasteFromClipboard() works under.
function startListening() {
  if (!SpeechRec || recogBroken) { recogFallback(); return; }
  // The two capture modes are exclusive, and this is the choke point every
  // caller passes through. One mic key means the engines no longer contend at
  // the tap, but a mid-take fallback still lands here — and turning to the
  // phone's recogniser abandons the local take rather than finishing it: the
  // words are about to be said again, and an upload landing behind them would
  // insert a duplicate mid-sentence. The other direction keeps its transcript —
  // see startLocalRecording().
  if (recording() || recBusy || recStarting) cancelRecording();

  let r;
  try {
    r = new SpeechRec();
  } catch (e) {
    recogFallback("Dictation unavailable");
    return;
  }
  r.lang = navigator.language || "en-US";
  r.continuous = true;
  r.interimResults = true;

  // Whether anything was actually heard. An end with no result after a start is
  // the silent failure mode: no error fires, the recogniser just stops.
  let gotResult = false;

  r.onresult = (e) => {
    gotResult = true;
    let interim = "";
    for (let i = e.resultIndex; i < e.results.length; i++) {
      const res = e.results[i];
      if (res.isFinal) {
        // Keep one space between utterances without doubling an existing one.
        const chunk = res[0].transcript.trim();
        if (chunk) recogText = recogText ? recogText.replace(/\s*$/, " ") + chunk : chunk;
      } else {
        interim += res[0].transcript;
      }
    }
    renderTranscript(interim);
  };

  r.onerror = (e) => {
    const err = e && e.error;
    // "aborted" is our own stop() unwinding; "no-speech" just means silence.
    if (err === "aborted" || err === "no-speech") return;
    const denied = err === "not-allowed" || err === "service-not-allowed";
    recogFallback(denied ? "Microphone blocked — use the keyboard mic"
                         : "Dictation unavailable");
  };

  r.onend = () => {
    // Ending without ever producing a result means it never really ran — the
    // standalone-PWA failure. Ending after results is just the user stopping.
    const wasLive = recog === r;
    if (wasLive && !gotResult) { recogFallback("Dictation unavailable"); return; }
    if (wasLive) stopListening();
  };

  // The strip must be visible to show the transcript, but quiet: focusing it
  // would raise the keyboard and cover the terminal, which is what talking to it
  // is meant to avoid.
  if (!composeOpen) setCompose(true, true);
  recogText = $("compose-text").value;

  try {
    r.start();
  } catch (e) {
    recogFallback("Dictation unavailable");
    return;
  }
  recog = r;
  recSyncMic();
}

// The mic key's whole job, and the one place an engine is chosen. There used to
// be two microphones on screen — this key for the phone's recogniser, a "code"
// chip on the strip for the backend's — and the user had to know which was which
// before speaking. Now the key is the only one, and Settings says what it talks
// to. In-flight states come first, because while a capture is running the key is
// a stop button and nothing else.
function toggleCompose() {
  if (recBusy) { cancelRecording(); return; }   // a tap during the upload is a cancel
  if (recording()) { stopRecording(); return; }
  // Tapped again while the microphone grant is still outstanding. There is no
  // recorder yet to stop, and asking for a second grant on top of the first is
  // what hands iOS back a muted track — so this tap stands the take down and
  // the acquire that lands after it releases what it was given.
  if (recStarting) { cancelRecording(); return; }
  if (listening()) {
    if (!$("compose-text").value.trim()) setCompose(false);  // nothing captured: put the strip away
    else stopListening();                                    // keep the transcript up for editing/sending
    return;
  }
  const engine = resolveVoiceEngine();
  if (engine !== "phone") {
    // The status fetch is lazy and this is the first thing that needs it, so a
    // tap before it lands resolves without it — the answer only ever vetoes an
    // engine, and the upload's own 503 catches what it would have caught. Warmed
    // here for the taps after this one, and for the picker.
    if (!voiceStatusChecked) fetchVoiceStatus();
    // Must be reached straight from the tap: iOS grants the microphone only
    // inside the gesture, so nothing of ours may await before startRecording().
    startLocalRecording(engine);
    return;
  }
  if (!SpeechRec || recogBroken) { setCompose(!composeOpen); return; }
  if (composeOpen && document.activeElement === $("compose-text")) {
    // Already typing: the tap means "put this away", not "start talking".
    setCompose(false);
    return;
  }
  startListening();
}

