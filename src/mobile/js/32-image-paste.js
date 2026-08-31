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
// the capture phase below. The phone has no paste event to catch, so it comes
// through pasteFromClipboard() in 19-voice-capture.js, which reads the
// clipboard itself and hands an image blob to uploadPastedImage().

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
// the start nor tries again, so a phone with no signal reports once.
async function uploadPastedImage(blob, retried) {
  if (!blob) return;
  // Size and type only. The clipboard may be holding a screenshot of something
  // private, and the debug panel is on screen.
  dbg("image: uploading type=" + (blob.type || "?") + " bytes=" + blob.size);
  if (blob.size > IMG_MAX_BYTES) { toast("Image too large"); return; }
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
    if (r.status === 413) { toast("Image too large"); return; }
    // The server sniffed it and it was not an image after all — a clipboard
    // entry that claimed image/png and held something else.
    if (r.status === 422) { toast("Not an image"); return; }
    if (!r.ok) { dbg("image: upload failed status=" + r.status); toast("Image upload failed"); return; }
    const data = await r.json();
    const path = data && typeof data.path === "string" ? data.path : "";
    if (!path) { toast("Image upload failed"); return; }
    insertImagePath(path);
  } catch (e) {
    // The 60s timer fired, or fetch() rejected with no response at all. Only
    // the second is worth another go — a phone changing cells is back within
    // the second, a timeout would just be another minute of waiting.
    if (e && e.name !== "AbortError" && !retried) {
      setTimeout(() => uploadPastedImage(blob, true), IMG_RETRY_DELAY);
      return;
    }
    dbg("image: upload error:", e);
    toast("Image upload failed");
  } finally {
    clearTimeout(timer);
  }
}

// Where the staged path lands. With the strip open it is a thing to edit before
// sending, so it goes in at the caret like a transcript does; with the strip
// shut the terminal is what the user is looking at, so it goes there.
function insertImagePath(path) {
  if (composeOpen) {
    const ta = $("compose-text"), v = ta.value;
    // Unlike codeMicFinish's captured caret, this field was never hidden — the
    // live selection is the one the user is looking at. Focus is left alone:
    // a paste over the terminal with the strip open should not raise the
    // keyboard the user had put away.
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
  toast("Image attached");
}

// Desktop Ctrl+V. Capture phase so this is read before xterm's hidden textarea
// gets the event. Text aimed at that textarea goes through term.paste() here as
// well as images: xterm leaves the browser's default paste alive after sending
// the clipboard bytes, so the same text can remain inside its invisible field
// and be emitted again by a later IME-style key event (space is one route seen
// in agent TUIs). Owning the event keeps the textarea empty while preserving
// xterm's newline normalization and bracketed-paste framing. Real app fields
// are left alone and keep the browser's native paste behavior.
document.addEventListener("paste", (e) => {
  if (!currentSession) return;              // demo shell: nothing to upload to
  const items = e.clipboardData && e.clipboardData.items;
  if (!items) return;
  let image = null, hasText = false;
  for (let i = 0; i < items.length; i++) {
    const it = items[i];
    if (!image && it.kind === "file" && it.type && it.type.startsWith("image/")) image = it;
    else if (it.kind === "string" && it.type === "text/plain") hasText = true;
  }
  if (!image) {
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
  // A rich copy from a web page carries both the picture and its text. Which
  // one was meant depends on where the user is typing: in the composer they
  // are writing a sentence, so the text wins; over the terminal there is
  // nothing being written, so the picture is the only reason to paste at all.
  if (hasText && document.activeElement === $("compose-text")) return;
  e.preventDefault();
  e.stopPropagation();
  uploadPastedImage(image.getAsFile());
}, true);
