// ============================================================
// Voice capture
// ============================================================
// Phone dictation hears prose. "pie test on tests slash test underscore camera
// h m r dot py" is a perfectly good transcription of what was said and a useless
// command line. So the mic key records real audio instead and hands it to the
// backend, which transcribes it against the vocabulary actually on screen and in
// the working directory, and answers with text fit to paste. The result lands in
// the compose box for review — never straight into the terminal.
//
// Nothing here is load-bearing. A refused microphone, a browser without
// MediaRecorder, an old backend with no /api/transcribe, a slow or broken one —
// each falls back to the phone's own dictation, which is what this app did
// before and always works.
const REC_TIMEOUT = 30000;   // ms before an upload is abandoned
// Must match MAX_AUDIO_SECONDS in app.py: the server's ffmpeg decode silently
// drops anything past that cap, so the client stops the take itself rather
// than let the user keep talking into audio that will never be transcribed.
const REC_MAX_SECONDS = 90;
// How long before the cap the elapsed label switches to a countdown, so the
// stop is never a surprise.
const REC_WARN_SECONDS = 10;
// iOS gives audio/mp4 (AAC) and nothing else; Chrome and Firefox give webm/opus.
// Preference order rather than a single guess, because MediaRecorder throws on a
// type it cannot produce and the default is unnamed in the response headers.
const REC_MIMES = ["audio/mp4", "audio/webm;codecs=opus", "audio/webm"];
// How long a finished recording's microphone is kept open for a quick re-record
// before the idle timer releases it. Long enough to cover "read it, clear it,
// say it again", short enough that a forgotten strip does not hold the mic.
const REC_IDLE_RELEASE = 45000;
// Below this, a finished recording is silence-shaped rather than audio: even a
// half-second of AAC or Opus carries more than this in container overhead alone.
// Only consulted where there is no analyser to ask instead.
const REC_MIN_BLOB = 2048;
// How often the analyser is sampled while recording. Fast enough that the level
// bar tracks speech rather than lagging behind it, slow enough to be free.
const REC_LEVEL_MS = 100;
// Below this RMS the samples are not quiet, they are zero. Real silence in a
// room still carries preamp noise two or three orders of magnitude above it;
// what sits under it is the muted-capture failure writing literal zeroes.
const REC_DEAD_RMS = 1e-4;
// How long a capture may show nothing at all before the recovery attempts run
// (resume the audio session, then bounce the track). The muted-track failure is
// visible immediately, so this only has to outlast the first analyser frames.
const REC_REVIVE_MS = 700;
// ...and how long before the user is warned on screen, so a long dictation is
// never spoken into a dead microphone. After the recovery attempts have had
// their turn and still produced nothing.
const REC_WARN_MS = 1500;
// How long after the tap that stopped a recording the same key stops meaning
// "cancel". The mic key is the compose key is the stop key, so the tap that ends
// a take lands on the button that is about to mean "abandon the upload" — and
// iOS follows a touchend with a synthesized click of its own, which arrives as a
// second tap nobody made. Long enough to absorb that ghost and the reflexive
// double-tap behind it, short enough that a user who genuinely changes their
// mind about a slow upload is not made to wait.
const REC_CANCEL_GRACE = 700;
// How long a failed upload waits before its one automatic retry. Only the
// network failure gets that retry — a phone that lost the tailnet for a second
// while walking between rooms is back a moment later, and asking the user to tap
// for something that fixes itself is worse than the second's delay.
const REC_RETRY_DELAY = 1500;
// Stamped onto the upload URL so a server log says which shell build sent the
// audio. Declared here rather than read from SW_VERSION, which is defined at the
// very bottom of this script.
const REC_BUILD = "__CACHE_VERSION__";

let recorder = null;         // the live MediaRecorder, null when not recording
// The MediaStream, kept alive between recordings. iOS hands back a MUTED track
// from a second getUserMedia() in a standalone PWA, so the stream that already
// works is reused rather than re-requested; recReleaseTimer is what eventually
// closes it. Non-null does NOT mean recording.
let recStream = null;
let recReleaseTimer = null;  // pending idle release of the retained stream
let recChunks = null;        // dataavailable payloads, joined into one blob
let recTimer = null;         // the elapsed-seconds ticker
let recStarted = 0;          // performance.now() at the first chunk of audio
let recBusy = false;         // an upload is in flight
let recStarting = false;     // tapped, waiting on the microphone grant
let recAbort = null;         // its AbortController
// performance.now() at the tap that stopped the recording. What REC_CANCEL_GRACE
// is measured from, so the ghost click that follows that tap is ignored rather
// than read as a cancel. Zero when no upload is in flight.
let recStoppedAt = 0;
// Set when the capture is being torn down without an upload, so the recorder's
// own onstop knows to drop what it collected rather than send it.
let recCancelled = false;
// One hard failure is enough: after it the mic key goes straight to the phone's
// dictation rather than asking for the microphone again on every tap.
let recBroken = false;
// A computer without the voice assets can only answer "not installed", and it
// can answer that to an empty body — so the first tap of the take asks before it
// records, and the user hears what to do instead of losing a sentence into a
// failed upload. recVoiceChecked says the ask has happened this session (a
// reload re-asks, which is what a fresh setup_voice.sh run needs);
// recVoiceNotSetup is only ever set by a confirmed not_setup answer, so anything
// ambiguous leaves it false and the recording goes ahead.
let recVoiceChecked = false;
let recVoiceNotSetup = false;
// What GET /api/voice_status last said: which engines the backend can run and
// which one it is configured to prefer. null until asked, and back to null on
// any failure — an unreachable backend proves nothing about what is installed on
// it, and the Settings picker says so rather than guessing.
let voiceStatus = null;
let voiceStatusChecked = false;
// Set when the chosen local engine has failed for this session — not installed,
// a 503, a refused microphone. Every subsequent tap goes to phone dictation
// instead, because a mic key that dead-ends is worse than one that hears prose.
// Deliberately not written back to the stored setting: run setup_voice.sh,
// reload, and the choice the user made is still their choice.
let voiceForcedPhone = false;
// What the user is told in that case, from both the probe and a real upload that
// comes back the same way.
const REC_NO_VOICE_MSG =
  "Local voice-to-text is not installed — run setup_voice.sh in PocketTUI's folder";
// What the retry hint says, which depends on how the take came to be sitting
// there. A failed upload is the user's news to act on; an interrupted recording
// is news they missed while they were away, and calling that one "failed" would
// have them looking for a fault that was only a phone call.
const REC_RETRY_FAILED = "Transcription failed — tap to retry";
const REC_RETRY_INTERRUPTED = "Recording interrupted — tap to transcribe";
// Where the transcript goes. Read off the textarea at the tap that starts the
// take — the field is blurred for the whole recording, and a blurred textarea
// does not report a trustworthy selection. null means there was never a caret to
// speak of, which resolves to the end of the text.
let recCaretStart = null, recCaretEnd = null;
// Which backend engine the take in hand was started for. Fixed at the tap so a
// setting changed while the user is still speaking cannot redirect the audio.
let recEngine = "";

// A recorded take that never reached a transcript: an upload that failed in a
// way saying nothing about the engine — a timeout, a dropped network, one
// server error — or a recording the phone interrupted by going away mid-take.
// The audio is the expensive part and it is still in hand, so it is kept with
// everything the insertion needs (which engine it was spoken to, where in the
// box it was meant to land) and offered back as a one-tap retry rather than
// thrown away. `reason` is the sentence the hint bar shows, because "failed" and
// "interrupted" are not the same news.
//
// One slot only. A second failed take replaces the first: the user has moved on
// and spoken again, and a queue of stale audio is not something anyone wants to
// arbitrate. Cleared on a successful insert, on an explicit dismiss, and
// whenever the session latches to the phone — a pending take for an engine the
// session has given up on can never be retried.
let recPending = null;
// Set on the stop() that a backgrounding fires, so the recorder's own onstop
// knows to park the take rather than upload it. Distinct from recCancelled,
// which throws the audio away; this one keeps it. Cleared the moment onstop has
// read it, and again at the top of every take, because an onstop that never
// arrives — iOS is under no obligation to run one while the page is hidden —
// must not leave the next stop reading a stale instruction.
let recSalvage = false;
// Whether the take in hand has already spent its one automatic network retry.
// Per take, not per session: the next recording deserves the same second chance.
let recNetRetried = false;
// Consecutive 5xx answers from the transcribe route. One is a transcription that
// fell over — a bad decode, a model that faulted on this particular audio — and
// the next take usually works. Two in a row is the backend, so the session
// latches to the phone at that point rather than eating every take. Any success
// resets it.
let recServerFails = 0;

// The single code-mode transcript inserted since the last send, as it was
// inserted. null means none; "" means more than one, which is not a usable
// signal — see codeMicFinish(). Reset on every send and every close.
let learnHeard = null;

// Tell the backend what it proposed and what the user actually sent, so a word
// it mishears the same way twice becomes part of this user's vocabulary.
//
// Fire and forget, in every sense: no await, no error surfaced, no UI. The send
// has already happened by the time this runs and nothing here may delay or
// disturb it. A failure costs one missed lesson, which the user cannot see.
function learnSend(heard, sent) {
  try {
    fetch(apiURL("api/learn"), {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ heard: heard, sent: sent }),
    }).catch(() => {});
  } catch (e) { /* learning is never worth an exception */ }
}

// --- Level monitoring -----------------------------------------------------
// The muted-track failure hands over a track that reports readyState "live" and
// muted false while delivering nothing but zero samples, so the flags cannot be
// trusted. What can be trusted is the audio itself: the stream is tapped through
// an AnalyserNode and its RMS watched. That serves three jobs at once — it shows
// the user a live level so a dead mic is visible while speaking, it refuses to
// upload a recording that never carried a signal, and creating/resuming the
// AudioContext inside the tap is itself the documented way to wake the iOS audio
// session, which frequently un-mutes the capture outright.
let recCtx = null;           // the one AudioContext, kept across recordings
let recSource = null;        // MediaStreamAudioSourceNode for the live stream
let recAnalyser = null;
let recLevelBuf = null;      // reused time-domain sample buffer
let recLevelTimer = null;    // the ~100ms RMS sampler
let recPeak = 0;             // loudest RMS seen in the current recording
let recRevived = false;      // the recovery attempts have already been spent
let recWarned = false;       // the "no signal" label is already up
// Whether the analyser really ran for this take. A browser with no Web Audio,
// or one that refused the source node, leaves recPeak at zero for reasons that
// have nothing to do with the microphone — so the silence gate only applies
// when there was actually something listening.
let recMonitored = false;

function recording() {
  return recorder !== null;
}

// Whether a tap arriving now is the stop tap's own echo rather than a new
// instruction. Both taps land on the same key — the one that just stopped the
// take — so the upload's first moments cannot tell a deliberate cancel from the
// synthesized click iOS fires after touchend, and the safe reading of an
// ambiguous tap is the one that does not throw away audio already on the wire.
function recCancelEcho() {
  return recStoppedAt > 0 &&
    performance.now() - recStoppedAt < REC_CANCEL_GRACE;
}

// Whether the capture path is worth trying at all. The demo has no backend to
// send audio to, and a session is what the server needs to build its prompt.
function recAvailable() {
  return !recBroken && !demoMode && !!currentSession &&
    typeof window.MediaRecorder === "function" &&
    !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
}

// Which engine this tap goes to. The stored choice when there is one, otherwise
// whichever engine the backend says it is running — a computer with voice
// installed should work out of the box, and one without has only the phone to
// offer anyway. Everything the local path needs and might not have is checked
// here rather than at three call sites: the demo, a missing MediaRecorder, a
// latched failure, the session-long fallback.
function resolveVoiceEngine() {
  const want = settledVoiceEngine();
  if (want === "phone") return "phone";
  if (!recAvailable()) return "phone";
  return want;
}

// The same resolution minus everything that is only true of the tap in hand.
// The Settings picker asks this one: it is describing the setting, not a
// recording, and recAvailable() is false for the whole of the sheet's life —
// there is no session open behind it — so asking resolveVoiceEngine() there
// would paint "Phone dictation" on a computer that is running Parakeet.
function settledVoiceEngine() {
  if (voiceForcedPhone) return "phone";
  return configuredVoiceEngine();
}

// What the setting resolves to with the session's failure set aside — the
// engine the user would be getting if nothing had gone wrong. Only worth asking
// separately to know whether the latch is actually overriding something: a user
// who chose the phone anyway is not being overridden by a fallback to it, and
// telling them their engine failed would be news about a choice they never made.
function configuredVoiceEngine() {
  let want = cfg.voiceEngine;
  if (!want && cfg.legacyLocalVoiceOff) want = "phone";
  if (!want) want = (voiceStatus && voiceStatus.active) || "phone";
  if (want === "phone") return "phone";
  // Only a status we actually have may veto: a backend that never answered has
  // said nothing about what it can run, and the upload's own 503 is the check
  // that catches it. Failing closed here would silently strand the setting on
  // every device whose first status fetch happened to miss.
  if (voiceStatus && !voiceStatus.engines[want]) return "phone";
  return want;
}

// Whether the session's fallback is currently costing the user the engine they
// asked for. The latch alone is not enough to report: it is also set on a device
// whose setting resolves to the phone regardless, where "using phone dictation
// instead" describes no change at all. Both the mic key's dot and the Settings
// note ask this, so the two can never disagree about whether to speak up.
function voiceLatchVisible() {
  return voiceForcedPhone && configuredVoiceEngine() !== "phone";
}

// Ask the backend which engines it has and which it prefers. Cached for the
// session like the transcribe probe, and for the same reason: a fresh
// setup_voice.sh run is picked up by a reload, which is what the user does
// anyway. Any failure clears the answer rather than recording a negative — an
// unreachable computer has not told us its voice assets are missing.
async function fetchVoiceStatus(force) {
  if (voiceStatusChecked && !force) return voiceStatus;
  voiceStatusChecked = true;
  if (demoMode) { voiceStatus = null; return null; }
  try {
    const r = await fetch(apiURL("api/voice_status"), {
      cache: "no-store", headers: authHeaders(),
    });
    if (!r.ok) { voiceStatus = null; return null; }
    const d = await r.json();
    const e = (d && d.engines) || {};
    voiceStatus = {
      engines: { parakeet: !!e.parakeet, whisper: !!e.whisper },
      active: typeof d.active === "string" ? d.active : "",
    };
  } catch (err) {
    dbg("voice status failed:", err);
    voiceStatus = null;
  }
  return voiceStatus;
}

// The local engine cannot serve this take. Route the rest of the session to the
// phone's recogniser and say why once — not recFallback(), which latches
// recBroken and is about the capture hardware; this is about the backend.
function voiceFallToPhone(msg) {
  voiceForcedPhone = true;
  // Whatever audio was being held for a retry is unretryable now: the retry
  // would go to the same engine this session has just given up on.
  recDropPending();
  cancelRecording();
  if (msg) toast(msg);
}

function recMime() {
  if (typeof MediaRecorder.isTypeSupported !== "function") return "";
  for (const m of REC_MIMES) {
    if (MediaRecorder.isTypeSupported(m)) return m;
  }
  return "";  // let the browser pick; the blob still carries its own type
}

// Wakes the audio session. Must run straight from the tap — a suspended
// AudioContext only resumes inside a user gesture on iOS, and that resume is
// what un-mutes a capture that would otherwise deliver zeroes. One context for
// the life of the page: they are a limited resource and creating one per
// recording is how a browser ends up refusing to make any more.
function recAudioWake() {
  const AC = window.AudioContext || window.webkitAudioContext;
  if (typeof AC !== "function") return null;
  try {
    if (!recCtx || recCtx.state === "closed") recCtx = new AC();
    if (recCtx.state === "suspended") recCtx.resume();
  } catch (e) {
    recCtx = null;
  }
  return recCtx;
}

// Taps the recording stream for level only: source -> analyser and nothing
// onward, so nothing is ever played back through the speaker.
function recMonitor(stream) {
  recUnmonitor();
  if (!recAudioWake()) return;
  try {
    recSource = recCtx.createMediaStreamSource(stream);
    recAnalyser = recCtx.createAnalyser();
    recAnalyser.fftSize = 2048;
    recLevelBuf = new Float32Array(recAnalyser.fftSize);
    recSource.connect(recAnalyser);
  } catch (e) {
    recUnmonitor();
  }
}

// Drops the analyser graph. The context itself stays: it is shared, and closing
// it would mean building a new one outside a gesture on the next take.
function recUnmonitor() {
  clearInterval(recLevelTimer);
  recLevelTimer = null;
  if (recSource) { try { recSource.disconnect(); } catch (e) {} }
  if (recAnalyser) { try { recAnalyser.disconnect(); } catch (e) {} }
  recSource = null;
  recAnalyser = null;
  recLevelBuf = null;
}

// Root-mean-square of one analyser frame, 0..1 of full scale. Float samples are
// already normalised, so this is directly comparable to REC_DEAD_RMS.
function recRMS() {
  if (!recAnalyser || !recLevelBuf) return 0;
  try {
    recAnalyser.getFloatTimeDomainData(recLevelBuf);
  } catch (e) {
    return 0;
  }
  let sum = 0;
  for (let i = 0; i < recLevelBuf.length; i++) sum += recLevelBuf[i] * recLevelBuf[i];
  return Math.sqrt(sum / recLevelBuf.length);
}

// The dot's brightness and the bar's width, from an RMS that spends most of its
// range near zero. Speech at a phone's distance sits around 0.01–0.1 RMS, so the
// scale is logarithmic: -60dB is empty, -10dB is full.
function recLevelFill(rms) {
  if (!(rms > 0)) return 0;
  const db = 20 * Math.log10(rms);
  return Math.max(0, Math.min(1, (db + 60) / 50));
}

// Paints the live level into the indicator. Kept to two style writes so the
// 10Hz tick costs nothing.
function recPaintLevel(rms) {
  const rec = $("compose-rec");
  const fill = recLevelFill(rms);
  const bar = rec.querySelector(".level-fill");
  if (bar) bar.style.transform = "scaleX(" + fill.toFixed(3) + ")";
  const dot = rec.querySelector(".dot");
  // The dot carries the same signal for reduced-motion users, who get no
  // pulse: dim when nothing is arriving, full umber when the mic is alive.
  if (dot) dot.style.opacity = (0.25 + 0.75 * fill).toFixed(2);
}

// The recovery ladder, run once per recording when the first frames show
// nothing. Both steps are cheap and neither interrupts the MediaRecorder, which
// keeps running throughout — a revived mic just means the take opens on a
// moment of silence, which nobody notices.
function recRevive() {
  if (recRevived) return;
  recRevived = true;
  // 1. The audio session may have gone back to sleep behind our backs.
  try { if (recCtx && recCtx.state !== "running") recCtx.resume(); } catch (e) {}
  // 2. Bouncing the track is what clears a stuck mute on the WebKit side.
  const t = recStream && recStream.getAudioTracks()[0];
  if (t) {
    try { t.enabled = false; t.enabled = true; } catch (e) {}
  }
}

// The ~100ms sampler that runs for the length of a recording: it drives the
// level display, remembers the loudest frame for the silence check at stop, and
// escalates from recovery to an on-screen warning when nothing ever arrives.
function recLevelTick() {
  const rms = recRMS();
  if (rms > recPeak) recPeak = rms;
  recPaintLevel(rms);
  if (recPeak > REC_DEAD_RMS) return;   // alive; nothing to escalate
  const age = performance.now() - recStarted;
  if (age >= REC_REVIVE_MS) recRevive();
  if (age >= REC_WARN_MS && !recWarned) {
    recWarned = true;
    recSetLabel("No signal — mic muted?");
  }
}

// The one place the microphone is actually released. A track left live is a red
// pill in the iOS status bar for the rest of the session, so every way out of the
// app — strip closed, session left, tab hidden, page going away, idle timeout —
// ends here. Deliberately separate from the indicator teardown below: the
// microphone can be done while the strip is still showing "Transcribing…".
function recRelease() {
  clearTimeout(recReleaseTimer);
  recReleaseTimer = null;
  recUnmonitor();
  // Suspended rather than closed: a closed context can only be replaced by a new
  // one, and building that outside a tap is exactly what iOS refuses. Suspending
  // costs nothing while idle and the next tap resumes it inside its gesture.
  if (recCtx && recCtx.state === "running") {
    try { recCtx.suspend(); } catch (e) {}
  }
  if (recStream) {
    for (const t of recStream.getTracks()) {
      try { t.stop(); } catch (e) {}
    }
    recStream = null;
  }
  recorder = null;
  clearInterval(recTimer);
  recTimer = null;
}

// What a finished recording does instead of releasing. Stopping the tracks after
// every take is what breaks the second one: a fresh getUserMedia() in an iOS
// standalone PWA frequently returns a live-but-MUTED track, and the recorder
// built on it captures silence the backend's gate throws away. So the stream is
// left open for a quick re-record and only released once the user has clearly
// moved on. The recorder itself is always dropped — a stopped MediaRecorder is
// never restarted, startRecording() builds a new one on the retained stream.
function recRetire() {
  recorder = null;
  // The analyser tap belongs to the take that just ended; the context stays
  // running so the re-record this retains the stream for starts hot.
  recUnmonitor();
  clearInterval(recTimer);
  recTimer = null;
  clearTimeout(recReleaseTimer);
  recReleaseTimer = recStream ? setTimeout(recRelease, REC_IDLE_RELEASE) : null;
}

// Whether the retained stream is still worth recording on. Only "ended"
// disqualifies it: a track that has ended cannot be revived, and one that is
// merely flagged muted usually is not — iOS sets that flag on any track nothing
// is currently consuming, so testing it here would throw away a working stream
// on every second take and force the fresh getUserMedia that actually fails.
// A retained track that really is dead is caught by the analyser instead.
function recStreamUsable() {
  if (!recStream) return false;
  const t = recStream.getAudioTracks()[0];
  return !!t && t.readyState === "live";
}

// Puts the indicator away and returns it to the state a fresh recording expects
// to find, so the next one never opens on "Transcribing…" with a hidden dot.
function recClearUI() {
  $("compose").classList.remove("recording");
  recSyncMic();
  recSetLabel("Recording…");
  const rec = $("compose-rec");
  const dot = rec.querySelector(".dot");
  dot.style.visibility = "";
  dot.style.opacity = "";
  const level = rec.querySelector(".level");
  if (level) level.style.visibility = "";
  const fill = rec.querySelector(".level-fill");
  if (fill) fill.style.transform = "";
}

// Abandon the capture without uploading: the strip closed, Send was tapped, the
// tab went away. Safe to call when nothing is recording.
function cancelRecording() {
  if (recAbort) { recAbort.abort(); recAbort = null; }
  recBusy = false;
  recStarting = false;
  recStoppedAt = 0;
  recCancelled = true;
  // A cancel outranks a salvage: whatever the take was being kept for, it is
  // being abandoned now, and a flag left standing would park the next one.
  recSalvage = false;
  if (recorder) {
    const r = recorder;
    // Cleared before stop(): onstop must see no live recorder and take the
    // cancelled path rather than uploading.
    recorder = null;
    try { r.stop(); } catch (e) {}
  }
  recRelease();
  recClearUI();
}

// The deliberate cancel of an upload already on the wire, as opposed to the
// teardowns cancelRecording() serves — the strip closing, Send, the tab going
// away — which are all a consequence of something the user did somewhere else
// and need no announcement. This one is the whole of the user's instruction, and
// an upload that vanishes without a word is indistinguishable from one that hung.
// So the strip is torn down first and the word said after: cancelRecording() ends
// in recClearUI(), which resets the label, and a "Canceled" written before that
// would be wiped by it.
function cancelUpload() {
  cancelRecording();
  toast("Canceled");
}

function recSetLabel(text) {
  $("compose-rec").querySelector(".label").textContent = text;
}

function recTick() {
  const s = Math.floor((performance.now() - recStarted) / 1000);
  if (s >= REC_MAX_SECONDS) { stopRecording(); return; }
  const remaining = REC_MAX_SECONDS - s;
  $("compose-rec").querySelector(".elapsed").textContent =
    remaining <= REC_WARN_SECONDS
      ? "stops in " + remaining + "s"
      : Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
}

// Something in the capture path failed. Say so briefly, then behave exactly as
// the app did before audio existed: open the strip so the keyboard's own mic key
// is one tap away, or drive the browser recogniser if there is one.
function recFallback(msg) {
  recBroken = true;
  cancelRecording();
  if (msg) toast(msg);
  startDictation();
}

// Exactly one getUserMedia per take, and nothing between it and the recorder
// starting.
//
// This used to wait up to 700ms for the fresh track to report itself unmuted,
// and on a timeout drop the grant and ask again. That was backwards. On iOS a
// just-granted track reports muted=true until something starts consuming it —
// the "unmute" event fires *because* recording began, not before it. So the wait
// always timed out, on perfectly good streams, and threw away the one grant that
// worked in favour of a second one that really was dead. It broke the first take
// as reliably as the second.
//
// Muted is therefore not consulted here at all. Whether audio is flowing is a
// question only the analyser can answer, and only once the recorder is running.
function recAcquire() {
  return navigator.mediaDevices.getUserMedia({ audio: true });
}

// Must be called straight from the tap: iOS grants the microphone and resumes
// an AudioContext only from inside a user gesture, so neither call can be
// deferred behind any await of ours. Nothing else is interposed either — grant,
// then start, with no wait in between, because a wait before the recorder
// starts is what broke this before.
function startRecording() {
  // recording() cannot answer for the gap between the tap and the grant — the
  // recorder does not exist yet — and that gap is exactly when the blur this
  // tap caused comes due. Set synchronously, cleared on both exits.
  recStarting = true;
  recCancelled = false;
  // A salvage whose onstop never ran — the page went away and came back without
  // the browser ever firing it — would otherwise park this take the moment it
  // stops. The take in hand is the only one this flag may ever speak for.
  recSalvage = false;
  recPeak = 0;
  recRevived = false;
  recWarned = false;
  recNetRetried = false;
  // A take is starting, so the hint has to come down even though its slot lives
  // on: recStarting is a state recSyncMic() is never told about.
  recSyncHint();
  // Before anything can await: this is inside the tap, and both creating and
  // resuming an AudioContext need that gesture. It is also the fix itself as
  // often as it is the measurement — an iOS audio session woken here hands over
  // a capture that actually carries samples.
  recAudioWake();
  // A new take cancels the pending release: the microphone is wanted again.
  clearTimeout(recReleaseTimer);
  recReleaseTimer = null;
  // The retained stream is only good while its track is live. Anything else is
  // thrown away whole — including any recorder built on it.
  const reuse = recStreamUsable();
  if (!reuse) recRelease();
  const acquire = reuse ? Promise.resolve(recStream) : recAcquire();
  acquire.then((s) => {
    recStarting = false;
    // The gesture may have been spent by the time permission came back — the
    // user closed the strip, or tapped the key again. Release and stand down.
    // A cancel that arrived mid-acquire has already run recRelease(), so the
    // grant now in hand is untracked and has to be stopped on its own.
    if (recCancelled) {
      for (const t of s.getTracks()) { try { t.stop(); } catch (e) {} }
      if (recStream === s) recStream = null;
      return;
    }
    const mime = recMime();
    // Always a new MediaRecorder, even on a reused stream: a stopped one cannot
    // be restarted, and reusing it is how the second take ends up empty.
    const r = mime ? new MediaRecorder(s, { mimeType: mime }) : new MediaRecorder(s);
    recChunks = [];
    r.ondataavailable = (e) => {
      if (e.data && e.data.size) recChunks.push(e.data);
    };
    r.onerror = () => recFallback("Recording failed — using phone dictation");
    r.onstop = () => {
      // The backgrounding stop, taken before anything else so a take the phone
      // interrupted is parked rather than read as an abandoned one. Its own
      // teardown is a full release, not recRetire(): the app is going away, and
      // nothing is coming back for a quick re-record.
      if (recSalvage) {
        recSalvage = false;
        salvageStopped(r);
        return;
      }
      // recorder is nulled by both stop paths; only the uploading one leaves
      // recBusy set, so that is what tells the two apart. The microphone is kept
      // open either way, for the re-record that usually follows.
      recRetire();
      // recBusy tells the uploading stop from the cancelling one; recCancelled
      // catches the cancel that landed in the gap between stop() and this, when
      // recBusy was already set and there was no recorder left to clear it. A
      // take the user abandoned is dropped either way, and its blob with it.
      if (!recBusy || recCancelled) { recClearUI(); return; }
      const blob = new Blob(recChunks, { type: r.mimeType || "audio/webm" });
      recChunks = null;
      // The decisive check, and the only one that catches the failure as it
      // actually presents: 62KB of well-formed AAC whose every sample is zero.
      // Size and the muted flag both called that recording healthy. The audio
      // is the only witness that does not, so nothing under the floor is sent.
      if (recMonitored) {
        if (recPeak <= REC_DEAD_RMS) { recDeadCapture(); return; }
      } else if (blob.size < REC_MIN_BLOB) {
        // No analyser to ask — a browser without Web Audio. All that is left is
        // the shape of the blob: too small to hold audio means it holds none.
        recDeadCapture();
        return;
      }
      // The indicator stays up, now reading "Transcribing…", until this lands.
      uploadRecording(blob);
    };
    recorder = r;
    recStream = s;
    r.start();
    recStarted = performance.now();
    // Tapped after start() so the analyser and the recorder see the same audio
    // from the same moment. recMonitored gates the silence check at stop: with
    // no analyser there is no evidence either way, and the old blob-size
    // heuristics are all this take gets.
    recMonitor(s);
    recMonitored = !!recAnalyser;
    if (recMonitored) recLevelTimer = setInterval(recLevelTick, REC_LEVEL_MS);
    // The strip has to be visible to show the indicator, but quiet: focusing the
    // textarea raises the keyboard over the terminal, which is exactly what
    // talking to it is meant to avoid.
    if (!composeOpen) setCompose(true, true);
    $("compose").classList.add("recording");
    recSyncMic();
    recSetLabel("Recording…");
    recTick();
    recTimer = setInterval(recTick, 1000);
  }).catch((e) => {
    recStarting = false;
    // Whatever partial state the attempt left — a retained stream we were
    // reusing, a grant from a failed retry — goes away with it.
    recRelease();
    const denied = e && (e.name === "NotAllowedError" || e.name === "SecurityError");
    recFallback(denied ? "Microphone blocked — using phone dictation"
                       : "Recording unavailable — using phone dictation");
  });
}

// The microphone was granted but never delivered audio — the iOS standalone-PWA
// muted-track failure, which outlives the page and clears only on a real relaunch.
// Nothing here is retryable — asking for the microphone again is what deepens
// it, not what clears it — so the user is told the one thing that does.
//
// Called once, from onstop, on the verdict of whichever evidence that take had:
// the analyser's peak RMS, or the blob's size when there was no analyser.
function recDeadCapture() {
  recBusy = false;
  recFallback("Mic is muted by iOS — close and reopen the app");
}

// Second tap: stop the capture and let onstop hand the blob to the upload.
function stopRecording() {
  if (!recorder) return;
  const r = recorder;
  recorder = null;             // onstop must not see a live recorder
  recBusy = true;              // ...but must know this stop is the uploading one
  // What the grace window is measured from. Stamped here rather than at the
  // upload, because the tap being absorbed is this one and the echo can arrive
  // before onstop has even built the blob.
  recStoppedAt = performance.now();
  recSyncMic();
  clearInterval(recTimer);
  recTimer = null;
  // One last read before the sampler stops: a very short take can finish inside
  // a single tick, and the peak it saw is what the silence gate rules on.
  if (recMonitored) {
    const rms = recRMS();
    if (rms > recPeak) recPeak = rms;
  }
  clearInterval(recLevelTimer);
  recLevelTimer = null;
  recSetLabel("Transcribing…");
  const rec = $("compose-rec");
  rec.querySelector(".dot").style.visibility = "hidden";
  const bar = rec.querySelector(".level");
  if (bar) bar.style.visibility = "hidden";
  try {
    r.stop();
  } catch (e) {
    recBusy = false;
    recFallback("Recording failed — using phone dictation");
  }
}

// The captured audio, as raw body with the context the server needs in the query
// string. Raw rather than multipart so the backend needs no form parser.
//
// How a failure ends decides what happens to the audio, because the failures are
// not the same failure. A 404 or a not_setup 503 is the engine saying it is not
// there, which the next tap would hit just as hard — those latch the session to
// the phone, as they always have. Everything else is circumstantial: a slow
// model, a phone that walked out of tailnet range, one transcription that fell
// over. Those keep the blob, because re-recording a sentence the microphone
// already captured perfectly is the one thing the user should never be asked to
// do, and offer it back through the retry hint.
async function uploadRecording(blob) {
  const ctl = new AbortController();
  recAbort = ctl;
  const timer = setTimeout(() => ctl.abort(), REC_TIMEOUT);
  // Whether this attempt handed the take on to a second one rather than ending
  // it. Read by the teardown below, which must not tidy away state the attempt
  // it scheduled still needs.
  let retrying = false;
  try {
    const url = recTranscribeURL(recEngine);
    const r = await fetch(url, {
      method: "POST",
      headers: authHeaders({ "Content-Type": blob.type || "application/octet-stream" }),
      body: blob,
      signal: ctl.signal,
    });
    // Cancelled while we waited: the user has moved on, and writing into the box
    // now would land text on a composition they already abandoned.
    if (!recBusy) return;
    // One non-200 is worth naming: the chosen engine is not installed, which the
    // user can fix in a minute. The probe usually catches it first, but the two
    // can disagree — the probe raced a config change, answered for a different
    // engine, or answered before this session's backend was the one being asked
    // — so clear the checked flag and let the next tap ask again rather than
    // trusting a stale verdict. Not recFallback(): the capture path is fine, and
    // recBroken is about the microphone. The rest of the session goes to the
    // phone so the key still hears something, and the stored choice is left
    // alone — setup_voice.sh and a reload restore it.
    if (r.status === 503) {
      let err = "";
      try { const d = await r.json(); err = d && d.error; } catch (e) { err = ""; }
      if (!recBusy) return;
      if (err === "not_setup") {
        recVoiceChecked = false;
        recVoiceNotSetup = true;
        voiceForcedPhone = true;
        voiceStatusChecked = false;   // the picker re-asks next time it opens
        recBusy = false;
        recAbort = null;
        // Nothing kept: a retry would go to the engine that just said it does
        // not exist.
        recDropPending();
        recClearUI();
        toast(REC_NO_VOICE_MSG);
        return;
      }
    }
    // An old backend with no transcribe route at all. Every later take would get
    // the same 404, so the session goes to the phone exactly as not_setup does.
    if (r.status === 404) {
      voiceFallToPhone("Voice server unavailable — using phone dictation");
      return;
    }
    if (!r.ok) {
      // A server that answered, badly. The first one is treated as this take's
      // bad luck — the audio is kept and offered back — and only a second in a
      // row is read as the backend itself being unwell.
      recServerFails++;
      if (recServerFails >= 2) {
        voiceFallToPhone("Voice server unavailable — using phone dictation");
      } else {
        recKeepPending(blob, "Transcription failed");
      }
      return;
    }
    const data = await r.json();
    if (!recBusy) return;
    const text = data && typeof data.text === "string" ? data.text.trim() : "";
    recBusy = false;
    recAbort = null;
    // An answer, which is what the counter was counting the absence of.
    recServerFails = 0;
    recDropPending();
    codeMicFinish(text, !!(data && data.truncated));
  } catch (e) {
    if (!recBusy) return;      // our own abort unwinding
    // The 30s timer fired. The audio is fine and the server may simply be slow
    // on a long take, so this neither latches nor retries by itself — a second
    // automatic 30s wait is a minute of nothing. The user decides.
    if (e && e.name === "AbortError") {
      recKeepPending(blob, "Transcription timed out");
      return;
    }
    // fetch() rejecting for any other reason is the network: no response ever
    // arrived, so this says nothing about the engine. Worth exactly one silent
    // retry — a phone changing cells or waking its radio is back within the
    // second — and after that it is the user's call.
    if (!recNetRetried) {
      recNetRetried = true;
      retrying = true;
      setTimeout(() => {
        // Still the same take, still wanted: a cancel, a new recording or a
        // teardown in the meantime all clear recBusy, and the retry must not
        // resurrect audio the user has already walked away from.
        if (!recBusy) return;
        uploadRecording(blob);
      }, REC_RETRY_DELAY);
      return;
    }
    recKeepPending(blob, "Voice server unreachable");
  } finally {
    clearTimeout(timer);
    if (recAbort === ctl) recAbort = null;
    // The upload this stop tap started has settled, so the window it opened is
    // spent — however it ended. Left standing it would only ever shorten the
    // grace the next take is owed. The automatic network retry is the one
    // ending that is not an ending: a no-network fetch rejects in a millisecond
    // or two, well inside the grace, and clearing the window there would hand
    // the stop tap's own ghost click a live cancel to fire.
    if (!retrying) recStoppedAt = 0;
  }
}

// Park a take that failed for a reason the engine is not to blame for, and put
// the hint up offering it back. Everything the insertion would have used is
// stored with the blob: the engine, so a setting changed in the meantime cannot
// redirect audio the user already spoke to Parakeet, and the caret, so the retry
// lands where the take was meant to land rather than at the end of whatever has
// been typed since.
//
// recBusy is cleared before the teardown, because recClearUI() ends in
// recSyncMic() and the hint it raises there must see a screen that is already
// quiet — a bar that only appeared on the next state change would leave the
// failure toast as the sole trace of a take still sitting in hand.
function recKeepPending(blob, why, reason) {
  recBusy = false;
  recAbort = null;
  recPending = {
    blob: blob,
    engine: recEngine,
    caretStart: recCaretStart,
    caretEnd: recCaretEnd,
    reason: reason || REC_RETRY_FAILED,
  };
  // recClearUI() ends in recSyncMic(), which is what actually raises the hint.
  recClearUI();
  if (why) toast(why + " — tap Retry to try again");
}

// Forget the parked take. Called on every ending that makes it meaningless: a
// transcript landed, the user dismissed it, the session gave up on the engine.
function recDropPending() {
  recPending = null;
  recSyncHint();
}

// The app is going away mid-recording — a phone call, a notification tapped, a
// switch to look something up. That used to throw the audio away, which is the
// worst reading of the situation available: the user was talking, something
// interrupted them, and the sentence they had already said was the casualty. So
// the take is stopped and parked instead, and the hint offers it back on return.
//
// Nothing is uploaded. A transcript landing in the box while the user is on a
// call is a surprise at best, and at worst it lands after they have typed
// something else; one tap on return is the whole of the transaction.
//
// requestData() first: MediaRecorder only guarantees a dataavailable at stop for
// what it has buffered, and asking for it explicitly is the documented way to be
// sure the take's audio is in hand before the page loses its turn to run.
function salvageRecording() {
  if (!recorder) return false;
  const r = recorder;
  recorder = null;             // onstop must not see a live recorder
  recSalvage = true;
  clearInterval(recTimer);
  recTimer = null;
  // The same last read the stop tap takes, for the same reason: the silence gate
  // rules on the peak, and a short take can finish inside a single tick.
  if (recMonitored) {
    const rms = recRMS();
    if (rms > recPeak) recPeak = rms;
  }
  clearInterval(recLevelTimer);
  recLevelTimer = null;
  try {
    r.requestData();
  } catch (e) { /* nothing buffered, or already inactive; stop() still fires */ }
  try {
    r.stop();
  } catch (e) {
    // No onstop is coming, so nothing will park the take and nothing will run
    // the teardown. Fall back to the release that backgrounding wanted anyway.
    recSalvage = false;
    recRelease();
    recClearUI();
    return false;
  }
  return true;
}

// The salvaged take, once the recorder has handed its audio over. The same gates
// a normal stop applies — a recording that carried no signal is not worth
// keeping, whatever interrupted it — and then the full release, because the app
// is on its way out and the microphone must not still be open behind it.
//
// A dead capture here is dropped silently rather than run through
// recDeadCapture(): that latches recBroken and opens the phone's dictation, and
// neither belongs on a page the user is not looking at, decided by a take they
// never chose to end. The next real take diagnoses itself.
function salvageStopped(r) {
  const blob = new Blob(recChunks || [], { type: r.mimeType || "audio/webm" });
  recChunks = null;
  const dead = recMonitored ? recPeak <= REC_DEAD_RMS : blob.size < REC_MIN_BLOB;
  if (!dead) {
    recPending = {
      blob: blob,
      engine: recEngine,
      caretStart: recCaretStart,
      caretEnd: recCaretEnd,
      reason: REC_RETRY_INTERRUPTED,
    };
  }
  // recRelease() rather than recRetire(): backgrounding is the clearest possible
  // sign that no quick re-record is coming, and a retained track is a red pill
  // in the status bar for the rest of the session.
  recRelease();
  recClearUI();
}

// Send the parked audio again. The whole take is restored first — the engine it
// was spoken to, the caret it was aimed at — so this is the original upload
// running a second time rather than a new one that happens to carry old audio.
function retryPendingTake() {
  if (!recPending || recBusy || recording()) return;
  const p = recPending;
  recPending = null;
  recEngine = p.engine;
  recCaretStart = p.caretStart;
  recCaretEnd = p.caretEnd;
  // A fresh take's worth of allowances: this attempt gets its own network retry.
  // recServerFails is deliberately not reset — a retry that comes back 5xx again
  // is the second consecutive one, which is exactly what should latch.
  recNetRetried = false;
  // Set before the UI, so the recSyncMic() below takes the hint down with it and
  // the mic key reads as the cancel it now is.
  recBusy = true;
  // The same busy indicator a stop tap raises, in the same order: strip up but
  // quiet so the terminal stays visible, the label saying what is happening, and
  // the live-capture ornaments hidden because nothing is being captured.
  if (!composeOpen) setCompose(true, true);
  $("compose").classList.add("recording");
  recSyncMic();
  recSetLabel("Transcribing…");
  const rec = $("compose-rec");
  rec.querySelector(".dot").style.visibility = "hidden";
  const bar = rec.querySelector(".level");
  if (bar) bar.style.visibility = "hidden";
  // No take is running, so the counter left over from the one that failed would
  // be a stopped clock sitting beside a live label.
  rec.querySelector(".elapsed").textContent = "";
  uploadRecording(p.blob);
}

// The hint is offered only when there is nothing else going on. While a take is
// recording or an upload is in flight the strip is already saying what is
// happening, and a second bar underneath it offering to retry a different take
// would be one thing too many to read at a glance. The slot survives either way,
// so this puts the hint back the moment the screen is quiet again.
function recSyncHint() {
  const bar = $("voice-retry");
  if (!bar) return;
  const show = !!recPending && !recBusy && !recStarting && !recording();
  // Written before the visibility check, not after it: an interrupted take can
  // replace a failed one while the bar is already up, and a bar whose text only
  // changed when it next opened would offer the wrong news about the audio in
  // hand. Cheap either way — the label is the same string most times through.
  if (show) {
    const go = $("voice-retry-go");
    if (go && go.textContent !== recPending.reason) go.textContent = recPending.reason;
  }
  if (bar.classList.contains("show") === show) return;
  bar.classList.toggle("show", show);
  // A flex sibling of the terminal, so raising it takes rows away from the grid
  // the same way the compose strip does.
  refit(0);
}

(function bindVoiceRetry() {
  const bar = $("voice-retry");
  if (!bar) return;
  // Same treatment as every key-bar control: the tap must not pull focus out of
  // the compose field, which would drop the keyboard the user is typing under.
  for (const ev of ["pointerdown", "mousedown"]) {
    bar.addEventListener(ev, e => e.preventDefault());
  }
  $("voice-retry-go").addEventListener("click", () => retryPendingTake());
  $("voice-retry-dismiss").addEventListener("click", () => recDropPending());
})();

// The transcript arrives as a proposal, not a command: it goes into the box for
// the user to read, fix and send, exactly like something thumb-typed.
//
// Inserted at the caret rather than appended, because the errand this button
// serves is mid-sentence: prose comes from the keyboard's own dictation, and the
// user stops in the middle of it to say a path or a flag. Appending would send
// that to the end of a line it does not belong to. The caret used is the one
// recorded at the tap — the field has been blurred and hidden behind the
// indicator ever since, so it cannot have moved, and reading it back off a
// blurred textarea is not reliable across engines.
function codeMicFinish(text, truncated) {
  recClearUI();
  if (!text) { toast("Nothing heard"); return; }
  const ta = $("compose-text"), v = ta.value;
  // No captured caret means nothing was ever focused: end of text, the old
  // behaviour, which is also right for an empty box.
  const start = recCaretStart === null ? v.length : Math.min(recCaretStart, v.length);
  const end = recCaretEnd === null ? v.length : Math.min(recCaretEnd, v.length);
  recCaretStart = recCaretEnd = null;
  // One space on whichever side is jammed against a word, none where the text
  // already has one and none at either boundary of the box.
  const before = v.slice(0, start), after = v.slice(end);
  const lead = before && !/\s$/.test(before) ? " " : "";
  const trail = after && !/^\s/.test(after) ? " " : "";
  ta.value = before + lead + text + trail + after;
  // What the backend proposed, remembered for composeSend() to compare against
  // what actually gets sent. Two transcripts in one message make the comparison
  // ambiguous — there is no telling which one an edit belongs to — so the
  // second one poisons the slot rather than replacing it.
  learnHeard = learnHeard === null ? text : "";
  // The keyboard comes up now, and only now: reviewing is a typing job, and this
  // is the point where the terminal no longer has to stay visible. The caret is
  // left at the end of what was just inserted, so resuming prose dictation
  // carries on from there rather than from the end of the line.
  ta.focus();
  const caret = start + lead.length + text.length;
  try { ta.setSelectionRange(caret, caret); } catch (e) {}
  composeGrow();
  // The server cuts the decode at MAX_AUDIO_SECONDS and drops the rest without
  // saying so in the transcript itself — the only way the user learns their
  // recording was cut short is this toast.
  if (truncated) toast("Recording was cut at 90 seconds");
}

// The pre-audio behaviour, kept whole as the fallback: the browser's own
// recogniser when it works, and otherwise just the strip open and focused so the
// keyboard's dictation key is one tap away.
function startDictation() {
  if (!SpeechRec || recogBroken) {
    if (!composeOpen) setCompose(true);
    else $("compose-text").focus();
    return;
  }
  startListening();
}

// The key bar's mic reflects the capture state machine and nothing else, so
// every path that changes it — start, stop, cancel, a failure, an engine change
// in Settings — lands here rather than reaching for the classes itself. The key
// is never hidden: it is the compose key too, and an engine that cannot record
// still has the phone's dictation to fall through to.
function recSyncMic() {
  const btn = $("keybar").querySelector(".k-compose");
  if (!btn) return;
  const live = recording();
  btn.classList.toggle("listening", live || listening());
  btn.classList.toggle("busy", recBusy);
  // A corner dot for the whole of a session that has fallen back: the toast that
  // announced the failure is long gone by the third tap, and without this the
  // only sign the local engine is being skipped is that the transcripts read
  // differently. Settings says the rest — this just says look there.
  btn.classList.toggle("forced-phone", voiceLatchVisible());
  // The face swaps with the state, so the spoken label has to as well — a key
  // reading "stop" to the eye and "compose" to a screen reader is worse than
  // either alone.
  btn.setAttribute("aria-label",
    live ? "Stop recording and transcribe"
    : recBusy ? "Cancel transcription"
    : "Show or hide compose bar");
  // Every path that moves the capture state machine already lands here, which
  // makes it the one place the retry hint has to be told the screen got busy or
  // quiet again — rather than each of those paths remembering to say so.
  recSyncHint();
}

// Ask the backend whether it can transcribe at all, before a word is spoken.
// An empty body is enough: the route checks its assets before it looks at what
// it was sent, so a computer without them answers not_setup to nothing at all.
//
// Fails open in every other case. A 404 from an old backend, a timeout, a
// network error, a body that will not parse — none of those prove voice is
// missing, and none of them may swallow the tap that was meant to start a take.
async function recProbeVoice(engine) {
  recVoiceChecked = true;
  try {
    const url = recTranscribeURL(engine);
    const r = await fetch(url, { method: "POST", headers: authHeaders({}), body: "" });
    if (r.status !== 503) return;
    const data = await r.json();
    if (data && data.error === "not_setup") recVoiceNotSetup = true;
  } catch (e) {
    dbg("voice probe failed:", e);
  }
}

// The upload and the probe address the same route with the same context, so the
// URL is built once. `engine` names which of the backend's engines is wanted;
// an old backend ignores the parameter and answers with whatever it has.
function recTranscribeURL(engine) {
  return apiURL("api/transcribe") +
    "?session=" + encodeURIComponent(currentSession) +
    "&dev=" + encodeURIComponent(cfg.devname) +
    "&engine=" + encodeURIComponent(engine) +
    // Which shell build sent this. The server ignores unknown query params;
    // its access log is the point, so a report of bad audio can be pinned to
    // a specific deployed page rather than guessed at.
    "&v=" + encodeURIComponent(REC_BUILD);
}

// Starts a local take, or ends the one running. The mic key routes here when the
// resolved engine is a backend one; everything about the capture itself is the
// same machinery the strip's old "code" button drove.
async function startLocalRecording(engine) {
  // Same reading as toggleCompose(): a tap during the upload cancels it, unless
  // it is still the stop tap echoing, in which case it means nothing at all.
  if (recBusy) { if (!recCancelEcho()) cancelUpload(); return; }
  if (recording()) { stopRecording(); return; }
  // The phone's recogniser and this one must never run at once — two captures of
  // one sentence, arriving at different times, into the same box. Stopping it
  // keeps what it heard: switching engines mid-sentence is not a discard. The
  // reverse direction discards instead — see startListening().
  if (listening()) stopListening();
  const ta = $("compose-text");
  // The transcript lands at the caret, and this is the moment to read it: the
  // field is about to be hidden behind the recording indicator, and a textarea
  // that is not focused does not report its selection dependably. Nothing
  // focused means nothing to preserve, which resolves to the end of the text.
  if (document.activeElement === ta) {
    recCaretStart = ta.selectionStart;
    recCaretEnd = ta.selectionEnd;
  } else {
    recCaretStart = recCaretEnd = null;
  }
  // Nothing to record into on a computer that cannot transcribe. Not
  // recFallback(): that latches recBroken, which is about the microphone, and
  // this verdict is about the backend — run setup_voice.sh, reload, and the key
  // records again. The tap still has to produce dictation rather than nothing,
  // so the session routes to the phone and starts there straight away.
  if (!recVoiceChecked) await recProbeVoice(engine);
  if (recVoiceNotSetup) {
    voiceForcedPhone = true;
    // Before the handover, not after: startDictation() only reaches recSyncMic()
    // on the branch that has a recogniser to start, and the phone without one is
    // exactly the device where the dot has the most to say.
    recSyncMic();
    toast(REC_NO_VOICE_MSG);
    startDictation();
    return;
  }
  // Which engine this take belongs to, read back by the upload. Held rather than
  // re-resolved at stop, so changing the setting mid-recording cannot send the
  // audio somewhere the user was not speaking to.
  recEngine = engine;
  startRecording();
}

(function bindCompose() {
  const ta = $("compose-text"), btn = $("compose-send");
  ta.addEventListener("input", composeGrow);
  ta.addEventListener("blur", composeBlurred);
  // Tapping into the text to edit it hands over to the keyboard: dictating and
  // typing into the same field at once would fight over the caret.
  ta.addEventListener("focus", () => { if (listening()) stopListening(); });
  // Same trick as the key bar's: the tap must not pull focus out of the textarea,
  // so no blur fires mid-tap and composeSend() owns the close outright.
  btn.addEventListener("pointerdown", e => e.preventDefault());
  btn.addEventListener("mousedown", e => e.preventDefault());
  btn.addEventListener("click", composeSend);
  // Same trick again for Clear, for the same reason: emptying the box must not
  // also drop the keyboard the user is about to carry on typing with.
  const clear = $("compose-clear");
  clear.addEventListener("pointerdown", e => e.preventDefault());
  clear.addEventListener("mousedown", e => e.preventDefault());
  clear.addEventListener("click", composeClear);
})();

// A backgrounded tab keeps the microphone hot otherwise — true of the recogniser
// and doubly so of a MediaRecorder, which iOS marks with a status-bar pill. This
// also covers the stream retained between recordings: leaving the app is the
// clearest possible sign that no quick re-record is coming.
//
// The microphone goes in every case; what goes with it is the separate question,
// and there the answer is as little as possible. A take in progress is salvaged
// rather than discarded, and an upload already on the wire is left to run: the
// fetch may well finish while the phone is on a call, and aborting it throws
// away a transcript the server has already done the work for. Coming back is
// what tidies up — the hint bar for the salvaged take, the upload's own
// completion for the one in flight.
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) {
    // Returning. Whatever was parked while the app was away is offered now: the
    // salvage ran with the page hidden and its recSyncMic() painted a screen
    // nobody was looking at.
    recSyncHint();
    return;
  }
  stopListening();
  if (recording()) { salvageRecording(); return; }
  // An upload in flight has already been through recRetire(), which leaves the
  // track live for a re-record that is plainly not coming now. Released on its
  // own rather than through cancelRecording(), which would abort the fetch —
  // the microphone is what has to go, not the transcript being worked on.
  if (recBusy) { recRelease(); return; }
  cancelRecording();
});

// Backgrounding a standalone PWA does not reliably fire visibilitychange on iOS,
// and a page frozen into the back/forward cache with a live track wakes up
// holding a microphone it cannot record from. pagehide is the one that fires.
//
// Both events land on the same backgrounding, in either order, so this has to be
// the second half of whatever the first one did rather than a second opinion: a
// salvaged take and a live upload both still need the microphone let go, and
// neither wants the abort-and-discard that cancelRecording() would bring. The
// tracks are all that is taken in that case.
//
// A recording still live here is one visibilitychange never spoke for — the
// standalone PWA where only this event fires — and it is salvaged for the same
// reason it would have been there. The pending slot is deliberately not consulted:
// a take parked ten minutes ago says nothing about what this backgrounding is
// interrupting, and reading it as one would leave a live microphone open.
window.addEventListener("pagehide", () => {
  stopListening();
  if (recording()) { salvageRecording(); return; }
  if (recBusy || recSalvage) { recRelease(); return; }
  cancelRecording();
});

// xterm reads keystrokes through a hidden textarea; focusing it is what raises
// the iOS keyboard and blurring it is the only way to put the keyboard away.
function termInput() {
  return term ? term.textarea : null;
}
// The compose textarea raises the same keyboard, so it counts as up too. Only
// applyViewport reads this now, as its stand-in before a full viewport height has
// been measured; tapping the terminal is what raises the keyboard, and iOS or the
// compose close routine is what puts it away.
function keyboardUp() {
  const ta = termInput();
  return composeOpen || (!!ta && document.activeElement === ta);
}

// One-shot modifiers: a tap arms the modifier (lit), the next key consumes it and
// it releases itself. Tapping an armed modifier again disarms it without sending
// anything. Both compose, so ctrl+shift+key is two taps then the key.
const mods = { ctrl: false, shift: false };
const modButtons = {};

function setMod(name, on) {
  mods[name] = on;
  const b = modButtons[name];
  if (b) b.classList.toggle("sticky-on", on);
}
function releaseMods() {
  setMod("ctrl", false);
  setMod("shift", false);
}

// Auto-repeat while an arrow is held, the way a real keyboard does: a pause to
// tell a tap from a hold, a steady rate, then acceleration once it is clearly a
// deliberate hold.
const REPEAT_DELAY = 350;   // ms before the first repeat
const REPEAT_RATE = 110;    // ms between repeats at first
const REPEAT_FAST = 45;     // ms between repeats once accelerated
const REPEAT_RAMP = 1500;   // ms of holding before reaching REPEAT_FAST

// A key's escape sequence, with any armed one-shot modifier applied. Arrows only
// carry ctrl/shift meaningfully as a CSI parameter, which tmux and readline both
// understand: ESC [ 1 ; <mod> <final>.
function seqWithMods(seq) {
  const m = (mods.ctrl ? 4 : 0) + (mods.shift ? 1 : 0);
  if (!m) return seq;
  const csi = seq.match(/^\x1b\[([A-D])$/);
  if (!csi) return seq;
  return "\x1b[1;" + (m + 1) + csi[1];
}

// Send a key-bar key, consuming any armed one-shot modifiers.
function sendKey(seq) {
  send(seqWithMods(seq));
  if (mods.ctrl || mods.shift) releaseMods();
}

(function buildKeybar() {
  const bar = $("keybar");
  for (const k of KEYS) {
    // Two icons means a key that swaps face with its state (the mic becoming a
    // stop square while a local take runs). Each face gets its own span so CSS
    // can do the swapping, rather than the state machine rewriting the DOM
    // mid-recording.
    const faces = k.icon2
      ? [el("span", { class: "idle" }, svgIcon(k.icon)),
         el("span", { class: "stop" }, svgIcon(k.icon2))]
      : [k.icon ? svgIcon(k.icon) : k.label];
    const b = el("button", { type: "button" }, ...faces);
    if (k.narrow) b.classList.add("narrow");
    if (k.aria) b.setAttribute("aria-label", k.aria);
    if (k.icon) b.classList.add("glyph-key");
    if (k.cls) b.classList.add(...k.cls.split(" "));
    if (k.only) b.classList.add(k.only + "-only");
    if (k.mod) modButtons[k.mod] = b;
    // Every key but the focusing ones must leave focus exactly where it is:
    // stealing it would drop the soft keyboard, and handing it back would raise
    // one the user had just dismissed. pointerdown fires before focus moves.
    if (!k.focusing) b.addEventListener("pointerdown", e => e.preventDefault());
    b.addEventListener("mousedown", e => e.preventDefault());

    if (k.repeat) {
      let timer = null, startedAt = 0;

      const stop = () => {
        clearTimeout(timer);
        timer = null;
        b.classList.remove("held");
      };
      const tick = () => {
        // The first send already happened on pointerdown; every tick here is a
        // repeat, so the modifiers are long since released.
        send(k.seq);
        // Ramp measured from the first repeat, so the rate starts at
        // REPEAT_RATE and reaches REPEAT_FAST after REPEAT_RAMP of repeating.
        const held = Date.now() - startedAt - REPEAT_DELAY;
        const t = Math.min(1, Math.max(0, held) / REPEAT_RAMP);
        timer = setTimeout(tick, REPEAT_RATE + (REPEAT_FAST - REPEAT_RATE) * t);
      };

      b.addEventListener("pointerdown", (e) => {
        e.preventDefault();
        stop();
        startedAt = Date.now();
        sendKey(k.seq);              // the tap itself, modifiers applied
        b.classList.add("held");
        timer = setTimeout(tick, REPEAT_DELAY);
      });
      for (const ev of ["pointerup", "pointercancel", "pointerleave"]) {
        b.addEventListener(ev, stop);
      }
      // pointerdown already sent the key; the click that follows must not repeat
      // it, whether or not the press turned into a hold.
      b.addEventListener("click", e => e.preventDefault());
      bar.appendChild(b);
      continue;
    }

    b.addEventListener("click", () => {
      if (k.compose) { toggleCompose(); return; }
      if (k.arrows) { setArrows(true); return; }
      if (k.collapse) { setArrows(false); return; }
      if (k.mod) { setMod(k.mod, !mods[k.mod]); return; }
      sendKey(k.seq);
    });
    bar.appendChild(b);
  }
})();

// Paste needs the keypress itself as the user gesture — iOS only grants clipboard
// reads from inside the handler, so this cannot be deferred to a promise chain
// started later. term.paste() applies bracketed-paste framing when tmux asked for it.
function pasteFromClipboard() {
  dbg("paste: reading clipboard");
  if (!navigator.clipboard || !navigator.clipboard.readText) {
    dbg("paste: no clipboard API");
    toast("Clipboard unavailable");
    return;
  }
  navigator.clipboard.readText().then(text => {
    // Length only, never the text: a clipboard on this device may well be holding
    // a password, and the panel is on screen.
    dbg("paste: got " + (text ? text.length : 0) + " chars");
    if (text && term) term.paste(text);
  }).catch(err => { dbg("paste failed:", err); toast("Clipboard blocked"); });
}

document.addEventListener("keydown", (e) => {
  if (!currentSession) return;
  if (!mods.ctrl && !mods.shift) return;
  if (e.key.length !== 1) return;   // let real editing keys through untouched
  e.preventDefault();
  e.stopPropagation();

  const ctrl = mods.ctrl, shift = mods.shift;
  const upper = e.key.toUpperCase();

  // Past the early returns, so this key is definitely ours. Which branch takes it
  // is the whole question when a key bar combination comes out wrong.
  const latched = (ctrl ? "ctrl" : "") + (ctrl && shift ? "+" : "") + (shift ? "shift" : "");

  if (ctrl && shift && upper === "V") {
    dbg("key:", e.key, "mods=" + latched, "-> paste");
    pasteFromClipboard();
  } else if (ctrl) {
    // 0x40-0x5f covers the letters plus @ [ \ ] ^ _ — the whole C0 range. A
    // terminal carries no shift bit here, so ctrl+shift+X is just ctrl+X.
    const c = upper.charCodeAt(0);
    dbg("key:", e.key, "mods=" + latched, "-> ctrl-seq");
    send((c >= 64 && c <= 95) ? String.fromCharCode(c & 0x1f) : e.key);
  } else {
    dbg("key:", e.key, "mods=" + latched, "-> shift-upper");
    send(upper);
  }
  releaseMods();
}, true);

// Tapping the terminal raises the keyboard; drags scroll or go back instead.
// A selection gesture also ends in a click the browser synthesizes, and the tap
// that dismisses the pill is a dismiss and nothing more — both stamp
// selectEndedAt, and a click just after one is not a request for the keyboard.
$("term-host").addEventListener("click", () => {
  if (Date.now() - selectEndedAt < 350) return;
  if (term && !dragScrolled && !edgeSwipe && !termGesture) term.focus();
});

