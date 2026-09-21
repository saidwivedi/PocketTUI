// ============================================================
// Image paste — clipboard bytes to a staged path on the prompt line
// ============================================================
// Claude Code reads an image when it is given the path to one, so a pasted
// screenshot takes the long way round: the bytes go up to /api/image, the
// server sniffs and stages them, and what comes back is an absolute path that
// goes into the composer or straight at the prompt. The upload is the raw blob
// with the context in the query string, the shape /api/transcribe already
// takes — no multipart, no form parser on the backend.
//
// Two ways in. The desktop's Ctrl+V arrives here as a paste event, caught in
// the capture phase below, and it carries whatever the clipboard holds: a
// screenshot takes the route above, a file copied in Finder or Explorer the one
// the "+" uses. The phone has no paste event to catch, so it comes through
// pasteFromClipboard() in 19-voice-capture.js, which reads the clipboard itself
// and hands an image blob to uploadImage().
// A third arrives from the "+" on the composer strip, which picks files rather
// than reading a clipboard. It and a paste of anything but a lone picture meet
// at attachFiles(), which sends pictures the sniffing way and everything else
// to /api/upload. See the attach section at the foot of this file.

// The server's own cap (MAX_IMAGE_BYTES, app.py), checked here so a 12-megapixel
// screenshot on a phone link is refused in a millisecond rather than after a
// minute of uploading it for a 413.
const IMG_MAX_BYTES = 15 * 1024 * 1024;
// Longer than the recorder's 30s: this is up to 15MB over a tailnet from a
// phone, where the transcribe upload is a few hundred kilobytes of audio.
const IMG_TIMEOUT = 60000;
const IMG_RETRY_DELAY = 1500;

// Same context transcribe sends, for the same reason: the server places the
// file identically whoever pasted it, and these are for its log line.
function imageUploadURL() {
  return apiURL("api/image") +
    "?session=" + encodeURIComponent(currentSession) +
    "&dev=" + encodeURIComponent(cfg.devname);
}

// One upload, and one silent retry if the network — rather than the server —
// is what went wrong. `retried` marks the second attempt: it neither re-toasts
// the start nor tries again, so a phone with no signal reports once. `target`
// is carried from the gesture that started this to the path that comes back,
// the upload being the one hop long enough for the caret to have moved.
// Answers true when a path landed in the box, so a batch of attachments can
// count what got through.
async function uploadImage(blob, target, retried) {
  if (!blob) return false;
  // Size and type only. The clipboard may be holding a screenshot of something
  // private, and the debug panel is on screen.
  dbg("image: uploading type=" + (blob.type || "?") + " bytes=" + blob.size);
  if (blob.size > IMG_MAX_BYTES) { toast("Image too large"); return false; }
  if (!retried) toast("Uploading image…");
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), IMG_TIMEOUT);
  try {
    const r = await fetch(imageUploadURL(), {
      method: "POST",
      // Whatever the browser called it. The server sniffs the bytes and
      // ignores this, so a clipboard with no type on it is no obstacle.
      headers: authHeaders({ "Content-Type": blob.type || "application/octet-stream" }),
      body: blob,
      signal: ctl.signal,
    });
    if (r.status === 413) { toast("Image too large"); return false; }
    // The server sniffed it and it was not an image after all — a clipboard
    // entry that claimed image/png and held something else.
    if (r.status === 422) { toast("Not an image"); return false; }
    if (!r.ok) { dbg("image: upload failed status=" + r.status); toast("Image upload failed"); return false; }
    const data = await r.json();
    const path = data && typeof data.path === "string" ? data.path : "";
    if (!path) { toast("Image upload failed"); return false; }
    insertImagePath(path, target);
    return true;
  } catch (e) {
    // The 60s timer fired, or fetch() rejected with no response at all. Only
    // the second is worth another go — a phone changing cells is back within
    // the second, a timeout would just be another minute of waiting.
    if (e && e.name !== "AbortError" && !retried) {
      // Awaited rather than left on a timer, so a caller counting attachments
      // is told how the second attempt went.
      await new Promise(r => setTimeout(r, IMG_RETRY_DELAY));
      return uploadImage(blob, target, true);
    }
    dbg("image: upload error:", e);
    toast("Image upload failed");
    return false;
  } finally {
    clearTimeout(timer);
  }
}

// Where a staged path lands, whichever route staged it, on the rule
// insertPastedText() follows: the caret decides. With it in the box the path is
// a thing to edit before sending, so it goes in there like a transcript does;
// anywhere else the terminal is what the user is looking at, so it goes there.
// The strip being open says nothing about this — on touch it is always open.
// A `target` overrules the caret where the gesture belongs to one surface: the
// long-press pill is the terminal's own, the "+" on the strip the composer's,
// and the picker the "+" opens hands focus back to the page body rather than
// to the box it was pressed on.
function insertPathIntoCompose(path, target) {
  if (pasteGoesToCompose(target)) {
    const ta = $("compose-text"), v = ta.value;
    // Unlike codeMicFinish's captured caret, this field was never hidden — the
    // live selection is the one the user is looking at. Focus is left alone:
    // a paste over the terminal must not raise the keyboard the user had put
    // away.
    const start = Math.min(typeof ta.selectionStart === "number" ? ta.selectionStart : v.length, v.length);
    const end = Math.min(typeof ta.selectionEnd === "number" ? ta.selectionEnd : start, v.length);
    // One space on whichever side is jammed against a word, none where there
    // already is one and none at either boundary of the box.
    const before = v.slice(0, start), after = v.slice(end);
    const lead = before && !/\s$/.test(before) ? " " : "";
    const trail = after && !/^\s/.test(after) ? " " : "";
    ta.value = before + lead + path + trail + after;
    const caret = start + lead.length + path.length;
    try { ta.setSelectionRange(caret, caret); } catch (e) {}
    // Where the Send button learns it has something to send.
    composeGrow();
  } else if (term) {
    // A trailing space so the next word does not jam against the path.
    // term.paste() brackets this when the running app asked for it, and the
    // path survives unbracketed too: the server builds it without spaces or
    // colons. (A $HOME containing a space would defeat that — out of scope.)
    term.paste(path + " ");
  }
}

// The paste's own word for it. A batch from the "+" says its piece once at the
// end instead, so the line is here rather than in the splice above.
function insertImagePath(path, target) {
  insertPathIntoCompose(path, target);
  toast("Image attached");
}

// Desktop Ctrl+V. Capture phase so this is read before xterm's hidden textarea
// gets the event. Text aimed at that textarea goes through term.paste() here as
// well as files: xterm leaves the browser's default paste alive after sending
// the clipboard bytes, so the same text can remain inside its invisible field
// and be emitted again by a later IME-style key event (space is one route seen
// in agent TUIs). Owning the event keeps the textarea empty while preserving
// xterm's newline normalization and bracketed-paste framing. Real app fields
// are left alone and keep the browser's native paste behavior.
document.addEventListener("paste", (e) => {
  if (!currentSession) return;              // demo shell: nothing to upload to
  const items = e.clipboardData && e.clipboardData.items;
  if (!items) return;
  const files = [];
  let hasText = false;
  for (let i = 0; i < items.length; i++) {
    const it = items[i];
    if (it.kind === "file") {
      // getAsFile() answers null for an entry the browser cannot hand over,
      // and a null in the batch would be counted as a failed attachment.
      const f = it.getAsFile();
      if (f) files.push(f);
    } else if (it.kind === "string" && it.type === "text/plain") hasText = true;
  }
  if (!files.length) {
    // Only take the paste that is actually aimed at xterm's private input.
    // Compose, search, settings, the file path field and CodeMirror all need
    // their own selection/caret-aware browser behavior.
    const terminalInput = term && term.textarea;
    if (!terminalInput || e.target !== terminalInput) return;
    const text = e.clipboardData.getData("text/plain");
    if (!text) return;
    e.preventDefault();
    e.stopPropagation();
    dbg("paste: terminal event chars=" + text.length);
    term.paste(text);
    return;
  }
  // One picture and nothing else is the pasted screenshot this file was
  // written for, and it keeps the direct route, toasts and all. Anything else —
  // a document, a pile of files, a mix — goes the way the "+" sends it, with no
  // target, so the caret decides where the paths land.
  const lonePicture = files.length === 1 && attachIsImage(files[0]);
  // A rich copy from a web page carries both the picture and its text. Which
  // one was meant depends on where the user is typing: in the composer they
  // are writing a sentence, so the text wins; over the terminal there is
  // nothing being written, so the picture is the only reason to paste at all.
  // Only the picture has this argument to lose: a web page puts no document on
  // the clipboard, and the text beside a file copied out of a file manager is
  // its bare name, which is worse than the staged path.
  if (lonePicture && hasText && document.activeElement === $("compose-text")) return;
  e.preventDefault();
  e.stopPropagation();
  if (lonePicture) uploadImage(files[0]);
  else attachFiles(files);
}, true);

// ============================================================
// Attach: the "+" on the composer strip
// ============================================================
// The same errand as a paste, started from the picker instead: on a phone the
// thing to attach is in Photos or Files, and there is no clipboard step worth
// making the user take. A desktop paste arrives here too, with whatever files
// it found on the clipboard. Pictures keep the sniffing route above, since an
// agent wants them staged the way a pasted screenshot is; everything else goes
// to /api/upload, which keeps the name and asks no questions about the bytes.

// MAX_UPLOAD_BYTES in app.py, checked here for the reason IMG_MAX_BYTES is: a
// video picked by mistake is refused before it spends a minute on the wire.
const ATTACH_MAX_BYTES = 50 * 1024 * 1024;
// What the picker hands over with no type on it is still recognisable by its
// name, which is the only other thing there is to go on.
const ATTACH_IMAGE_RE = /\.(png|jpe?g|gif|webp)$/i;

// Mirrors /api/image's query, plus the one thing that route has no use for:
// the picker's own name for the file, which the server keeps as far as it can.
function attachUploadURL(name) {
  return apiURL("api/upload") +
    "?name=" + encodeURIComponent(name) +
    "&session=" + encodeURIComponent(currentSession) +
    "&dev=" + encodeURIComponent(cfg.devname);
}

function attachIsImage(f) {
  return !!((f.type && f.type.startsWith("image/")) || ATTACH_IMAGE_RE.test(f.name || ""));
}

// Whether the computer on the other end serves either route. hasCapStrict
// rather than hasCap: a server old enough to send no map at all has neither,
// and a "+" that can only fail is worse than no "+".
function syncComposeAttach() {
  const on = hasCapStrict("image_paste") || hasCapStrict("upload");
  $("compose").classList.toggle("no-attach", !on);
}

// One file up, and the same single retry the image route takes, for the same
// reason. Answers true when a path landed in the box.
async function uploadAttachment(f, target, retried) {
  dbg("attach: uploading type=" + (f.type || "?") + " bytes=" + f.size);
  if (f.size > ATTACH_MAX_BYTES) { toast("Too large to attach (50 MB max)"); return false; }
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), IMG_TIMEOUT);
  try {
    const r = await fetch(attachUploadURL(f.name || "file"), {
      method: "POST",
      headers: authHeaders({ "Content-Type": f.type || "application/octet-stream" }),
      body: f,
      signal: ctl.signal,
    });
    if (r.status === 413) { toast("Too large to attach (50 MB max)"); return false; }
    if (!r.ok) {
      dbg("attach: upload failed status=" + r.status);
      toast("Couldn't attach " + f.name);
      return false;
    }
    const data = await r.json();
    const path = data && typeof data.path === "string" ? data.path : "";
    if (!path) { toast("Couldn't attach " + f.name); return false; }
    insertPathIntoCompose(path, target);
    return true;
  } catch (e) {
    if (e && e.name !== "AbortError" && !retried) {
      await new Promise(r => setTimeout(r, IMG_RETRY_DELAY));
      return uploadAttachment(f, target, true);
    }
    dbg("attach: upload error:", e);
    toast("Couldn't attach " + f.name);
    return false;
  } finally {
    clearTimeout(timer);
  }
}

// A pick, one file at a time: the paths have to land in the box in the order
// they were chosen, and a pile of parallel uploads over a phone link would
// finish in whatever order the network decided. `target` is the surface the
// gesture belongs to — the "+" is the composer's — and a paste leaves it out so
// the caret decides, the way a pasted screenshot always has.
async function attachFiles(files, target) {
  const canImage = hasCapStrict("image_paste"), canFile = hasCapStrict("upload");
  if (!canImage && !canFile) { toast("This server can't take attachments yet"); return; }
  let done = 0, failed = 0;
  for (let i = 0; i < files.length; i++) {
    const f = files[i];
    // Held rather than timed: the line has to stand for as long as the upload
    // takes, and the outcome below is what replaces it.
    holdToast(files.length > 1 ? "Attaching " + (i + 1) + " of " + files.length + "…"
                               : "Attaching…");
    let ok;
    if (attachIsImage(f) && canImage) ok = await uploadImage(f, target);
    else if (canFile) ok = await uploadAttachment(f, target);
    else ok = false;      // a picture-only server, handed something else
    if (ok) done++; else failed++;
  }
  // One line for the batch. Nothing through at all is the one case with no
  // summary: every failure above already said what went wrong, and a count
  // would only wipe the reason off the screen.
  if (!done) return;
  if (!failed) toast(files.length > 1 ? "Attached " + done + " files" : "Attached");
  else toast("Attached " + done + ", " + failed + " failed");
}

(function bindComposeAttach() {
  const btn = $("compose-attach"), input = $("compose-attach-input");
  // The strip's rule for every button on it: a tap must not pull focus out of
  // the textarea, or the keyboard drops while the picker is opening.
  btn.addEventListener("pointerdown", e => e.preventDefault());
  btn.addEventListener("mousedown", e => e.preventDefault());
  btn.addEventListener("click", () => {
    // The demo has no machine to stage anything on, and a picker that leads
    // nowhere is worse than a word saying so.
    if (demoMode) { toast("Attachments need a real computer"); return; }
    // Inside the tap: iOS opens a file picker only from the gesture itself.
    input.click();
  });
  input.addEventListener("change", (ev) => {
    // The FileList empties with the input, so the pick is copied out before the
    // reset that lets the same file fire change a second time.
    const files = Array.from(ev.target.files || []);
    ev.target.value = "";
    if (files.length) attachFiles(files, "compose");
  });
})();
