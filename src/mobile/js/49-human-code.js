// ============================================================
// Human code — a human_code/brief.json read as the brief it is
// ============================================================
// The human-code skill writes a repository down as short pseudocode units, one
// per decision the code makes, each line pointing at the lines of source it
// stands for, all pinned to the commit it was written at. Its viewer is a plain
// script the skill also inlines into its own index.html; here it is vendored
// (vendor/human-code.js, loaded on the first brief opened, never at boot) and
// handed what only the app has: the files themselves, read at the brief's
// commit or off the disk; the editor, for a tap on a line; the composer or the
// terminal's prompt, for a question about one; and the repo's history since the
// commit, which says which units the code has moved under and how far behind
// the brief is.
//
// The screen is the reader's (33-md-reader.js) in every way the explorer sees:
// full screen on a phone with a history entry of its own, seated in the docked
// pane on a laptop with none, put away and brought back with the session that
// had it (fileViews, 09-image-viewer.js). One difference, which is why it has
// an owner of its own (hcOwner, 28-file-explorer.js): the editor a tap opens
// goes into the other row of the column, beside the brief rather than over it.
//
// The expanded explorer's workspace shows a brief as a tab (47-workspace.js),
// with the files a tap opens in the group beside it. Both hosts hold what they
// know about their brief in one record (hcRecord) and hand it to the same
// functions: the reads, the taps, the questions, the refresh and the poll.
//
// Nothing here runs while the bundle loads: the screen's own keys are wired on
// the first open (hcWire), and everything else is reached from a press.

const HC_POLL_MS = 15000;
const HC_REFRESH_DEFAULT = 'claude "/human-code"';

let hcView = null;       // the mounted viewer, or null
let hcCur = null;        // the screen's brief (hcRecord), or null
let hcTimer = 0;
let hcGen = 0;           // every open and close; an answer for an older one is dropped
let hcEdRow = null;      // {id, made}: the row the editor a tap opened went into
let hcLoading = null;
let hcWired = false;

// The viewer comes in on first use, the way three.js does (ensureThree,
// 48-mesh-viewer.js): same relative root as the boot-time vendor tags and the
// same ?v= the service worker manages.
function ensureHumanCode() {
  if (window.HumanCode) return Promise.resolve();
  if (hcLoading) return hcLoading;
  hcLoading = new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = "vendor/human-code.js?v=" + buildVersion();
    s.onload = resolve;
    s.onerror = () => { hcLoading = null; s.remove(); reject(new Error("vendor load failed")); };
    document.head.appendChild(s);
  });
  return hcLoading;
}

// ---- the answers, as pure functions ----------------------------------------

// The repository a brief describes is the folder its human_code/ sits in.
function hcRootOf(path) {
  return path.replace(/\/human_code\/brief\.json$/, "") || "/";
}

// Every file the brief points into, which is what the route is asked about.
function hcFiles(brief) {
  const out = new Set();
  const add = (src) => { if (src && src.file) out.add(src.file); };
  for (const c of (brief && brief.components) || []) {
    for (const u of c.units || []) {
      add(u.src);
      for (const l of u.lines || []) add(l.src);
    }
  }
  return [...out].sort().slice(0, 500);
}

// What the pill under the brief's meta line says, or null for no pill: only
// when there is something to refresh, a brief behind its branch or pinned to a
// commit this clone does not have.
function hcPillText(data, commit) {
  if (!data || data.error) return null;
  if (data.base_found === false) {
    return String(commit || "").slice(0, 7) + " is not in this repo · Refresh";
  }
  const n = Number(data.behind) || 0;
  if (n > 0) return n + (n === 1 ? " commit" : " commits") + " behind · Refresh";
  return null;
}

// The line Refresh types. Never with Enter (stageCommand): the user reads it at
// the prompt and runs it. The skill works on the repo it is run in, so a shell
// that is somewhere else is taken there first.
function hcRefreshCommand(cwd, root, c) {
  const cmd = (c && c.hcRefreshCmd) || HC_REFRESH_DEFAULT;
  const top = root.replace(/\/+$/, "") || "/";
  if (cwd && (cwd === top || cwd.startsWith(top === "/" ? "/" : top + "/"))) return cmd;
  const word = /^[\w@%+=:,./~-]+$/.test(top) ? top : "'" + top.replace(/'/g, "'\\''") + "'";
  return "cd " + word + " && " + cmd;
}

// A question about one line, as the start of a message: the line, where it
// points, and a colon for the user to finish after.
function hcAskText(line, src) {
  const text = "`" + String(line || "").trim() + "`";
  if (!src || !src.file) return text + ": ";
  const at = src.end == null || src.end === src.start ? src.start : src.start + "-" + src.end;
  return text + " (" + src.file + ":" + at + "): ";
}

// Where Pinned/Now starts: Now when the code on the disk is the code the brief
// was written against — HEAD is its commit and none of its files has changed —
// since then the two open the same bytes and Now is the one that can be edited.
// Pinned otherwise, and whenever the answer cannot vouch for that (a missing
// commit, a cut-short diff, no answer at all).
function hcDefaultAt(commit, data) {
  if (!data || data.error || data.base_found === false || data.truncated) return "pinned";
  const a = String(commit || "").toLowerCase(), b = String(data.head || "").toLowerCase();
  if (!a || !b || !(a.startsWith(b) || b.startsWith(a))) return "pinned";
  for (const f of Object.values(data.files || {})) {
    if (f.gone || f.to || (f.hunks && f.hunks.length)) return "pinned";
  }
  return "now";
}

// The file and lines a pointer opens at, 1-based and inclusive. At Now the
// range is carried through the file's hunks (the viewer's remap), and a renamed
// file opens under its new name; a deleted one has nothing to open at Now and
// answers null.
function hcRange(src, at, data, remap) {
  const plain = { file: src.file, line: src.start, lineEnd: src.end };
  const f = at === "now" && data && !data.error && data.files ? data.files[src.file] : null;
  if (!f) return plain;
  if (f.gone) return null;
  const hunks = f.hunks || [];
  const line = remap(hunks, src.start);
  return { file: f.to || src.file, line: line, lineEnd: Math.max(line, remap(hunks, src.end)) };
}

// ---- one brief, wherever it is shown ----------------------------------------

// What a brief parses to when it is one this app can draw.
function hcIsBrief(brief) {
  return !!brief && typeof brief === "object" && Array.isArray(brief.components);
}

// A brief written by a newer skill than the viewer here knows (ensureHumanCode
// first).
function hcTooNew(brief) {
  return (Number(brief.schema) || 0) > HumanCode.SCHEMA;
}

// One brief being shown, by the screen or by a workspace tab. `at` is which
// bytes a tap opens: "pinned" is the file at the brief's commit, read-only;
// "now" the file on the disk, with the range carried through what changed
// since. Settled once, by the first answer about the repo or by a press of the
// toggle, and only a press moves it after that. `stale` is the last
// /api/git/hunks answer, `nextAt` the time before which it is not asked again
// however the tick falls, and `dead` is set when the host lets the brief go, so
// an answer still on its way lands nowhere. `onAt` is the host's, called when
// `at` moves.
function hcRecord(path, brief) {
  return { path: path, brief: brief, root: hcRootOf(path), at: "pinned", settled: false,
           stale: null, nextAt: 0, busy: false, missingSaid: false, dead: false, onAt: null };
}

// The viewer in `host`, by either host: the record's reads, its taps through
// the host's `open`, and the app's question and refresh. A brief `beside` the
// terminal (docked) puts a question's caret in the box at once.
function hcMountIn(host, b, open, beside) {
  return HumanCode.mount(host, b.brief, hcViewOpts(b, open, beside));
}

function hcViewOpts(b, open, beside) {
  return {
    sources: { read: (file) => hcReadSource(b, file) },
    open: open,
    ask: (text, src) => hcAsk(text, src, beside()),
    refresh: () => hcRefresh(b.root),
    theme: "host",
    route: "state",
  };
}

// The Pinned/Now pair says what the record says.
function hcSyncAt(box, b) {
  const commit = String((b && b.brief.commit) || "").slice(0, 7);
  for (const btn of box.querySelectorAll("[data-at]")) {
    btn.setAttribute("aria-checked", b && btn.dataset.at === b.at ? "true" : "false");
    btn.title = btn.dataset.at === "pinned"
      ? "Open code as it was at " + commit + ", read-only"
      : "Open code as it is now";
  }
}

// A press on the pair, which settles it for good.
function hcPickAt(b, e) {
  const btn = e.target.closest("[data-at]");
  if (!btn || !b) return;
  b.at = btn.dataset.at;
  b.settled = true;
  if (b.onAt) b.onAt();
}

// The pair for a host with no bar of its own (a workspace tab): the screen's
// #hc-at, made here and following `b`.
function hcAtPair(b) {
  const pair = el("div", { class: "hc-at", role: "radiogroup",
                           "aria-label": "Which code a line opens" },
    el("button", { type: "button", role: "radio", "data-at": "pinned" }, "Pinned"),
    el("button", { type: "button", role: "radio", "data-at": "now" }, "Now"));
  pair.addEventListener("click", (e) => hcPickAt(b, e));
  b.onAt = () => hcSyncAt(pair, b);
  b.onAt();
  return pair;
}

// ---- the screen -------------------------------------------------------------

// `pane` is the listing the brief was tapped in; `back` is a rail switch's
// record (hcStash) when this is the brief coming back with its session.
async function openHumanCode(path, pane, back) {
  if (hcCur) closeHumanCode();
  const gen = ++hcGen;
  const data = await fsReadText(path);
  if (!data || gen !== hcGen) return;
  let brief = null;
  try { brief = JSON.parse(data.content); } catch (e) {}
  // Not a brief after all, or one this app cannot draw: the file as it is, in
  // the editor, which is where any other .json opens.
  if (!hcIsBrief(brief)) {
    openEditor(path, { pane: pane });
    return;
  }
  try { await ensureHumanCode(); } catch (e) {
    toast("Couldn't load the brief viewer");
    openEditor(path, { pane: pane });
    return;
  }
  if (gen !== hcGen) return;
  if (hcTooNew(brief)) {
    toast("This brief needs a newer PocketTUI");
    openEditor(path, { pane: pane });
    return;
  }
  hcWire();
  hcCur = hcRecord(path, brief);
  hcCur.at = back && back.at ? back.at : "pinned";
  hcCur.settled = !!(back && back.at);
  hcCur.onAt = hcSyncToggle;
  hcEdRow = null;
  $("hc-name").textContent = baseName(hcCur.root);
  $("hc-name").title = path;
  $("hc-note").hidden = true;
  const el = $("screen-humancode");
  // In the pane if that is where it was tapped, over the whole window
  // otherwise, and docked it pushes no entry: the reader's rule. A brief
  // coming back with its session has had its entry pushed for it.
  const docked = dockFileView(el, pane);
  el.classList.add("active");
  if (!docked && !back) history.pushState({ humancode: true }, "", location.href);
  // Mounted once the screen is showing: the viewer lays itself out from the
  // size of the box it is given.
  hcMount(back && back.state);
  hcSyncToggle();
  clearInterval(hcTimer);
  hcTimer = setInterval(hcPoll, HC_POLL_MS);
  hcPoll();
}

function hcMount(state) {
  if (hcView) hcView.destroy();
  hcView = hcMountIn($("hc-host"), hcCur, hcOpenSource,
    () => $("screen-humancode").classList.contains("docked"));
  if (state) hcView.restore(state);
  hcPaint();
}

function closeHumanCode() {
  hcGen++;
  clearInterval(hcTimer);
  hcTimer = 0;
  hcShowMore(false);
  const el = $("screen-humancode");
  el.classList.remove("active");
  undockFileView(el);
  hcDrop();
}

// What a close and a stash both let go of.
function hcDrop() {
  if (hcView) { hcView.destroy(); hcView = null; }
  if (hcCur) hcCur.dead = true;
  hcCur = null;
  hcEdRow = null;
}

// ---- reading, opening, asking -----------------------------------------------

// The viewer's own source view is never shown here (open is given), but the
// provider is what it reads through if it ever is: at the brief's commit while
// the toggle says Pinned, off the disk at Now.
async function hcReadSource(b, file) {
  const ref = b.at === "pinned" ? String(b.brief.commit || "") : "";
  const data = await fsReadTextAt(joinPath(b.root, file), ref, b.root);
  if (b.dead) return null;
  if (data && data.error === "bad_ref") { hcCommitMissing(b); return hcReadSource(b, file); }
  return data ? { text: data.content, at: ref ? "pinned" : "now" } : null;
}

// The brief's commit is not in this clone (a rebase, a gc, a fresh clone):
// there is nothing to read at it, so the toggle goes to Now, once, and says so.
function hcCommitMissing(b) {
  b.at = "now";
  b.settled = true;
  if (b.onAt) b.onAt();
  if (b.missingSaid) return;
  b.missingSaid = true;
  toast(String(b.brief.commit || "").slice(0, 7) + " is not in this repo, so code opens as it is now", 3500);
}

// The file and the editor's options a pointer opens at, by either host, or
// null for a pointer to nothing.
function hcTarget(b, src) {
  if (!src || !src.file || !b) return null;
  let at = b.at === "pinned" && b.brief.commit ? "pinned" : "now";
  let r = hcRange(src, at, b.stale, HumanCode.remap);
  if (!r) {
    toast(baseName(src.file) + " has been deleted since " + String(b.brief.commit).slice(0, 7));
    at = "pinned";
    r = hcRange(src, at, null, HumanCode.remap);
  }
  const opts = { line: r.line, lineEnd: r.lineEnd };
  if (at === "pinned") { opts.ref = String(b.brief.commit); opts.root = b.root; }
  return { path: joinPath(b.root, r.file), opts: opts };
}

function hcOpenSource(src) {
  const t = hcTarget(hcCur, src);
  if (t) hcOpenEditor(t.path, t.opts);
}

// Where the editor goes. Seated in a pane, into the other row of the column:
// one already holding a listing with nothing over it, or one made for it, which
// goes again when the editor closes. A column with no files row to spare (the
// brief beside the browser or the changes) takes the fallback: the editor over
// the brief in the brief's own row, and the brief back when it closes. A made
// row shows its listing for a moment before the editor lands on it.
// Full screen (a phone), the editor covers the brief and back brings it back.
async function hcOpenEditor(path, opts) {
  const el = $("screen-humancode");
  const edEl = $("screen-editor");
  if (edEl.classList.contains("active") && edDirty
      && !confirm("Discard your unsaved changes?")) return;
  const gen = hcGen;
  if (el.classList.contains("docked")) {
    const row = await hcOtherRow(path);
    if (gen !== hcGen) return;
    if (row) {
      hcEdRow = row;
      opts.pane = filesPanes[row.id];
      if (row.made) opts.returnTo = { show: () => hcDropRow(row.id) };
      await openEditor(path, opts);
      return;
    }
  }
  const state = hcView ? hcView.state() : null;
  if (el.classList.contains("docked")) opts.pane = filesPanes[hcOwner];
  opts.returnTo = { show: () => hcShowAgain(state) };
  await openEditor(path, opts);
  if (gen === hcGen && edEl.classList.contains("active")) el.classList.remove("active");
}

async function hcOtherRow(path) {
  if (!isWideLayout()) return null;
  const ed = $("screen-editor");
  // The row the last tap's editor is still in.
  if (hcEdRow && filesPanes[hcEdRow.id] && filesViewOwner === hcEdRow.id
      && ed.classList.contains("docked") && ed.classList.contains("active")) return hcEdRow;
  for (const id of sideRows) {
    if (sideType(id) !== "files" || id === hcOwner || filesViewOwner === id) continue;
    return { id: id, made: false };
  }
  if (sideRows.length >= 2) return null;
  const id = sideCreate("files");
  if (!id || id === hcOwner) return null;
  const ok = await filesPanes[id].open(path.slice(0, path.lastIndexOf("/")) || "/", { keep: hcOwner });
  if (!ok || !filesPanes[id].isDocked()) return null;
  return { id: id, made: true };
}

// A row made for the editor goes with it. After the close has finished rather
// than inside it: the close may itself be that row's own cross, still running.
function hcDropRow(id) {
  Promise.resolve().then(() => {
    const pane = filesPanes[id];
    if (pane && pane.isOpen() && filesViewOwner !== id) closeDockedFiles(id);
  });
}

// The brief again, after the editor that covered it closes. Its scroll went
// while it was not displayed, so the place it was left at is put back.
function hcShowAgain(state) {
  if (!hcCur) return;
  $("screen-humancode").classList.add("active");
  sideLayout();
  if (hcView && state) hcView.restore(state);
}

// A long press (touch) or a right-click on a line. On a touch screen it goes
// into the composer, where the user finishes the question by typing or by
// voice: dictation lands at the caret and keeps what is already in the box
// (codeMicFinish, 19-voice-capture.js; the phone recogniser starts from the
// box's text too), so the line put in first is followed by the words spoken.
// With a keyboard it is typed at the terminal's prompt, never with Enter.
function hcAsk(text, src, beside) {
  const line = hcAskText(text, src);
  if (!touchOnly()) { stageCommand(line); return; }
  const ta = $("compose-text");
  ta.value = ta.value && !/\s$/.test(ta.value) ? ta.value + " " + line : ta.value + line;
  setCompose(true, true);
  composeGrow();
  composeArm();
  // Beside the terminal the box is on screen and takes the caret; full screen
  // (or a workspace tab, over the terminal) it is under the brief, waiting for
  // the way back.
  if (beside) {
    ta.focus();
    ta.setSelectionRange(ta.value.length, ta.value.length);
  } else {
    toast("In the message box, for when you go back to the terminal", 3000);
  }
}

// Where the terminal's shell is, asked the way the explorer's folder key asks
// (fetchPaneCwd, through the markup's own pane).
async function hcRefresh(root) {
  const cwd = await filesPanes.files.paneCwd();
  if (cwd === null || !root) return;
  stageCommand(hcRefreshCommand(cwd, root, cfg));
}

// ---- what changed since the brief's commit ----------------------------------

// On open and on every tick while the brief is on screen, spaced by how long
// the repo took to answer last time (the diff pane's pacing, diffPace in
// 37-git-diff.js): a repo on a network mount is not asked again before it has
// had four times its last answer to rest. A server without the route gets no
// marks and no pill, and nothing is asked. Says whether an answer came, for
// the host to paint (hcPaintInto); whether its brief is on screen is the
// host's to check before asking.
async function hcAskHunks(b) {
  if (!b || b.dead || b.busy || document.hidden || Date.now() < b.nextAt) return false;
  if (!hasCapStrict("git_hunks") || demoApiOn() || !b.brief.commit) return false;
  b.busy = true;
  let data = null;
  try {
    const r = await fetch(apiURL("api/git/hunks"), {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ root: b.root, base: String(b.brief.commit), files: hcFiles(b.brief) }),
    });
    if (r.status === 401) rejectToken();
    else if (r.ok) data = await r.json().catch(() => null);
  } catch (e) {
  } finally {
    b.busy = false;
  }
  if (b.dead || !data) return false;
  b.nextAt = Date.now() + Math.max(HC_POLL_MS, Math.round((Number(data.elapsed_ms) || 0) * DIFF_POLL_FACTOR));
  b.stale = data.error ? null : data;
  if (b.stale && b.stale.base_found === false) hcCommitMissing(b);
  else if (b.stale && !b.settled) {
    b.at = hcDefaultAt(b.brief.commit, b.stale);
    b.settled = true;
    if (b.onAt) b.onAt();
  }
  return true;
}

async function hcPoll() {
  if (!hcCur || !$("screen-humancode").classList.contains("active")) return;
  if (await hcAskHunks(hcCur)) hcPaint();
}

// The marks and the pill from the last answer, on `view`, and the host's
// `note` line. A diff cut short reads every file past the cut as unchanged,
// so it marks nothing and says why instead.
function hcPaintInto(b, view, note) {
  const d = b.stale;
  const marks = !!d && !d.truncated && d.base_found !== false;
  view.setStale(marks ? Object.assign({}, d, { base: String(b.brief.commit || "").slice(0, 7) }) : null);
  view.setPill(hcPillText(d, b.brief.commit));
  note.hidden = !(d && d.truncated);
  note.textContent = d && d.truncated
    ? "Too much has changed since " + String(b.brief.commit).slice(0, 7) + " to mark it here"
    : "";
}

function hcPaint() {
  if (hcView && hcCur) hcPaintInto(hcCur, hcView, $("hc-note"));
}

// ---- the bar ----------------------------------------------------------------

function hcSyncToggle() {
  hcSyncAt($("hc-at"), hcCur);
}

function hcShowMore(on) {
  $("hc-more-wrap").classList.toggle("open", on);
  $("btn-hc-more").setAttribute("aria-expanded", on ? "true" : "false");
}

function hcWire() {
  if (hcWired) return;
  hcWired = true;
  // Docked the brief pushed no entry, so its back is the close: the reader's
  // arrangement.
  $("btn-hc-back").addEventListener("click", () => {
    if ($("screen-humancode").classList.contains("docked")) closeHumanCode();
    else history.back();
  });
  $("hc-at").addEventListener("click", (e) => hcPickAt(hcCur, e));
  $("btn-hc-more").addEventListener("click", () => {
    hcShowMore(!$("hc-more-wrap").classList.contains("open"));
  });
  // Any press off the menu puts it away, on capture so the press still does
  // what it does underneath (the split menu's rule, 26-side-pane.js).
  document.addEventListener("pointerdown", (e) => {
    if ($("hc-more-wrap").classList.contains("open") && !$("hc-more-wrap").contains(e.target)) {
      hcShowMore(false);
    }
  }, true);
  $("hc-more-refresh").addEventListener("click", async () => {
    hcShowMore(false);
    const v = await appPrompt("What Refresh types at the prompt",
                              { value: cfg.hcRefreshCmd || HC_REFRESH_DEFAULT, confirmLabel: "Save" });
    if (v === null) return;
    const t = v.trim();
    cfg.hcRefreshCmd = t === HC_REFRESH_DEFAULT ? "" : t;
  });
}

// ---- putting the brief away (see fileViews in 09-image-viewer.js) -----------
// The record is the path and where the viewer was, and the brief is read again
// on the way back: a brief is rewritten by a refresh, and the one that comes
// back should be the one on the disk. Under an editor in its own row the brief
// is not on screen, and a stash lets it go (the editor's own stash drops the
// way back to it, edStash). Every stash moves hcGen on, so a brief still being
// read for this session does not land in the next one.

function hcStash() {
  hcGen++;
  clearInterval(hcTimer);
  hcTimer = 0;
  hcShowMore(false);
  if (!hcCur) return null;
  const el = $("screen-humancode");
  const s = el.classList.contains("active")
    ? { path: hcCur.path, state: hcView ? hcView.state() : null, owner: hcOwner,
        at: hcCur.settled ? hcCur.at : null }
    : null;
  hcDrop();
  // The shape goes with the screen rather than into the record, as the
  // reader's does; the owner is in the record for the row it goes back to.
  el.classList.remove("active", "docked");
  hcOwner = null;
  return s;
}

function hcRestore(s) {
  return openHumanCode(s.path, s.owner ? filesPanes[s.owner] : null, s);
}

// Entries a rail switch has to spend for the brief beyond the one it counts for
// the view on top: one when the full-screen editor is over the full-screen brief.
function hcCoveredEntries() {
  const el = $("screen-humancode"), ed = $("screen-editor");
  return hcCur && !el.classList.contains("active") && !el.classList.contains("docked")
    && ed.classList.contains("active") && !ed.classList.contains("docked") ? 1 : 0;
}
