// ============================================================
// File explorer
// ============================================================
// A phone-first view over /api/fs/*: reached from the session list's folder
// button (opens at $HOME) and from the terminal key bar's folder key (opens at
// the pane's cwd). One history entry per visit — directory taps navigate in
// place and the crumbs walk back up, so back (and the edge swipe) closes the
// whole view rather than replaying every cd.

let filesPath = "";        // the directory currently listed
let filesHome = "";        // $HOME as the backend reports it, for ~ crumbs
let filesOrigin = null;    // the screen to restore on close
let filesSelected = null;  // the entry the action sheet is about
// A finished long-press ends in a click the browser synthesizes on the same
// row; stamping the moment lets that click be ignored (the viewer's
// viewerOpenedAt idea).
let filesPressedAt = 0;

// Mirrors app.py's MEDIA_TYPES allowlist: these open in the existing viewer
// over /api/file rather than in the editor.
const FILES_MEDIA_RE = /\.(?:png|jpe?g|gif|webp|svg|bmp|mp4|webm|mov)$/i;

function joinPath(dir, name) {
  return (dir === "/" ? "" : dir) + "/" + name;
}
function baseName(p) {
  const parts = p.replace(/\/+$/, "").split("/");
  return parts[parts.length - 1] || "/";
}
function fmtSize(n) {
  if (n < 1024) return n + " B";
  let v = n;
  for (const u of ["KB", "MB", "GB"]) {
    v /= 1024;
    if (v < 1024) return (v < 10 ? v.toFixed(1) : Math.round(v)) + " " + u;
  }
  return Math.round(v / 1024) + " TB";
}

function openExplorer(path) {
  // Nothing to browse yet — prompt instead of failing against the static host.
  if (needsSetup()) { openSettings(true); return; }
  if (!$("screen-files").classList.contains("active")) {
    filesOrigin = $("screen-term").classList.contains("active")
      ? "screen-term" : "screen-list";
    $(filesOrigin).classList.remove("active");
    $("screen-files").classList.add("active");
    syncChrome();
    history.pushState({ files: true }, "", location.href);
  }
  loadDir(path);
}

function closeExplorer() {
  $("screen-files").classList.remove("active");
  const back = filesOrigin || "screen-list";
  filesOrigin = null;
  $(back).classList.add("active");
  syncChrome();
  // The terminal kept its socket while we were away; it only needs its size
  // re-checked, not a reconnect.
  if (back === "screen-term") refit(0);
}

// The terminal entry point. The pane's cwd is asked for at tap time — it moves
// with every cd — and $HOME quietly stands in when tmux cannot say.
async function openFilesAtCwd() {
  if (demoMode) { toast("No files in the demo"); return; }
  let cwd = "";
  try {
    const r = await fetch(apiURL("api/session_cwd?session="
        + encodeURIComponent(currentSession || "")
        + "&dev=" + encodeURIComponent(cfg.devname)),
      { cache: "no-store", headers: authHeaders() });
    if (r.status === 401) { rejectToken(); return; }
    const data = await r.json().catch(() => null);
    if (r.ok && data) cwd = data.cwd || "";
  } catch (e) {}
  openExplorer(cwd);
}

async function loadDir(path) {
  try {
    const q = path ? "?path=" + encodeURIComponent(path) : "";
    const r = await fetch(apiURL("api/fs/list" + q),
                          { cache: "no-store", headers: authHeaders() });
    if (r.status === 401) { rejectToken(); return; }
    const data = await r.json().catch(() => null);
    if (!r.ok || !data) throw new Error((data && data.error) || "HTTP " + r.status);
    filesPath = data.path;
    filesHome = data.home || "";
    renderCrumbs(data.path);
    renderEntries(data.entries || []);
    $("files-error").style.display = "none";
  } catch (e) {
    dbg("fs list failed:", e);
    $("files-list").innerHTML = "";
    $("files-empty").style.display = "none";
    $("files-error").style.display = "block";
  }
}

function crumbBtn(label, target, current) {
  return el("button", {
    type: "button",
    class: "crumb" + (current ? " current" : ""),
    onclick: () => { if (!current) loadDir(target); },
  }, label);
}

function renderCrumbs(path) {
  const wrap = $("files-crumbs");
  wrap.innerHTML = "";
  let parts, prefix, rootLabel;
  if (filesHome && (path === filesHome || path.startsWith(filesHome + "/"))) {
    parts = path.slice(filesHome.length).split("/").filter(Boolean);
    prefix = filesHome; rootLabel = "~";
  } else {
    parts = path.split("/").filter(Boolean);
    prefix = ""; rootLabel = "/";
  }
  wrap.appendChild(crumbBtn(rootLabel, prefix || "/", parts.length === 0));
  parts.forEach((seg, i) => {
    prefix += "/" + seg;
    wrap.appendChild(el("span", { class: "crumb-sep" }, "›"));
    wrap.appendChild(crumbBtn(seg, prefix, i === parts.length - 1));
  });
  // The tail is the current folder — that is the segment to keep in view.
  wrap.scrollLeft = wrap.scrollWidth;
}

function renderEntries(entries) {
  const list = $("files-list");
  list.innerHTML = "";
  $("files-empty").style.display = entries.length ? "none" : "block";
  for (const e of entries) list.appendChild(fileRow(e));
}

function fileRow(e) {
  const meta = e.type === "file" ? fmtSize(e.size) + " · " + relTime(e.mtime)
             : e.type === "link" ? "link" : "";
  const row = el("div", { class: "file-row" },
    el("span", { class: "file-ic " + e.type },
       svgIcon(e.type === "dir" ? "i-folder" : "i-file")),
    el("div", { class: "file-name" }, e.name),
    el("div", { class: "file-meta" }, meta),
  );
  wireRow(row, e);
  return row;
}

// Tap opens; a long press (or a desktop right-click) opens the action sheet.
function wireRow(row, entry) {
  let timer = null, sx = 0, sy = 0;
  const cancel = () => { clearTimeout(timer); timer = null; };
  row.addEventListener("touchstart", (ev) => {
    cancel();
    if (ev.touches.length !== 1) return;
    sx = ev.touches[0].clientX; sy = ev.touches[0].clientY;
    timer = setTimeout(() => {
      timer = null;
      filesPressedAt = Date.now();
      openFileActions(entry);
    }, 500);
  }, { passive: true });
  row.addEventListener("touchmove", (ev) => {
    if (timer && Math.hypot(ev.touches[0].clientX - sx,
                            ev.touches[0].clientY - sy) > 10) cancel();
  }, { passive: true });
  row.addEventListener("touchend", cancel, { passive: true });
  row.addEventListener("touchcancel", cancel, { passive: true });
  row.addEventListener("contextmenu", (ev) => {
    ev.preventDefault();
    filesPressedAt = Date.now();
    openFileActions(entry);
  });
  row.addEventListener("click", () => {
    if (Date.now() - filesPressedAt < 500) return;  // the long-press's own click
    openEntry(entry);
  });
}

function openEntry(e) {
  const full = joinPath(filesPath, e.name);
  if (e.type === "dir") { loadDir(full); return; }
  // A broken link or a special file: nothing to enter or edit, so the sheet
  // (whose Download is the only sensible offer) is the whole answer.
  if (e.type === "link") { openFileActions(e); return; }
  if (FILES_MEDIA_RE.test(e.name)) { showImage(full); return; }
  openEditor(full);
}

function openFileActions(entry) {
  filesSelected = entry;
  $("file-actions-title").textContent = entry.name;
  $("btn-file-download").style.display = entry.type === "dir" ? "none" : "";
  showSheet(true, "sheet-file-actions");
}

// ---- the action sheet ------------------------------------------------------

async function fsPost(route, body) {
  const r = await fetch(apiURL(route), {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify(body),
  });
  if (r.status === 401) { rejectToken(); return null; }
  const data = await r.json().catch(() => null);
  return { ok: r.ok, status: r.status, data };
}

$("btn-file-rename").addEventListener("click", async () => {
  const e = filesSelected;
  if (!e) return;
  showSheet(false);
  const next = prompt("Rename " + e.name, e.name);
  if (next === null || !next.trim() || next.trim() === e.name) return;
  if (next.includes("/")) { toast("Names can't contain /"); return; }
  const res = await fsPost("api/fs/rename", {
    src: joinPath(filesPath, e.name),
    dst: joinPath(filesPath, next.trim()),
  });
  if (!res) return;
  if (!res.ok) {
    toast(res.status === 409 ? "Something with that name already exists"
                             : "Couldn't rename");
    return;
  }
  loadDir(filesPath);
});

$("btn-file-download").addEventListener("click", () => {
  const e = filesSelected;
  if (!e) return;
  showSheet(false);
  downloadFile(joinPath(filesPath, e.name), e.name);
});

$("btn-file-delete").addEventListener("click", async () => {
  const e = filesSelected;
  if (!e) return;
  showSheet(false);
  const what = e.type === "dir" ? "folder " + e.name : e.name;
  if (!confirm("Delete " + what + "?")) return;
  const res = await fsPost("api/fs/delete", { path: joinPath(filesPath, e.name) });
  if (!res) return;
  if (!res.ok) {
    // The backend never deletes a non-empty folder — the shell is a tap away
    // for anyone who really means rm -r.
    toast(res.data && res.data.error === "not_empty"
      ? "That folder isn't empty" : "Couldn't delete");
    return;
  }
  toast("Deleted");
  loadDir(filesPath);
});

// Through fetch rather than a plain link: /api/* only answers to the token
// header, which a navigation cannot carry.
async function downloadFile(path, name) {
  toast("Downloading…");
  try {
    const r = await fetch(apiURL("api/fs/download?path=" + encodeURIComponent(path)),
                          { headers: authHeaders() });
    if (r.status === 401) { rejectToken(); return; }
    if (!r.ok) throw new Error("HTTP " + r.status);
    const url = URL.createObjectURL(await r.blob());
    const a = document.createElement("a");
    a.href = url;
    a.download = name || baseName(path);
    document.body.appendChild(a);
    a.click();
    a.remove();
    // Not straight away: Safari needs the URL alive until its save sheet is done.
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  } catch (e) {
    toast("Couldn't download");
  }
}

// ---- the + sheet -----------------------------------------------------------

$("btn-files-add").addEventListener("click", () => showSheet(true, "sheet-files-add"));

$("btn-files-newfile").addEventListener("click", () => {
  showSheet(false);
  const name = prompt("New file name");
  if (name === null || !name.trim()) return;
  if (name.includes("/")) { toast("Names can't contain /"); return; }
  // Nothing is written yet: the editor opens empty and the first Save (hash "")
  // is what creates the file, so an abandoned name leaves no husk behind.
  openEditor(joinPath(filesPath, name.trim()), { create: true });
});

$("btn-files-newfolder").addEventListener("click", async () => {
  showSheet(false);
  const name = prompt("New folder name");
  if (name === null || !name.trim()) return;
  if (name.includes("/")) { toast("Names can't contain /"); return; }
  const res = await fsPost("api/fs/mkdir", { path: joinPath(filesPath, name.trim()) });
  if (!res) return;
  if (!res.ok) {
    toast(res.status === 409 ? "Something with that name already exists"
                             : "Couldn't create the folder");
    return;
  }
  loadDir(filesPath);
});

$("btn-files-upload").addEventListener("click", () => {
  showSheet(false);
  $("files-upload-input").click();
});

$("files-upload-input").addEventListener("change", (ev) => {
  const f = ev.target.files && ev.target.files[0];
  ev.target.value = "";  // picking the same file again must still fire change
  if (f) uploadFile(f, false);
});

async function uploadFile(f, overwrite) {
  const target = joinPath(filesPath, f.name);
  try {
    const q = "?path=" + encodeURIComponent(target) + (overwrite ? "&overwrite=1" : "");
    // The body is the file itself — the raw-body shape /api/fs/upload shares
    // with /api/transcribe.
    const r = await fetch(apiURL("api/fs/upload" + q),
                          { method: "POST", headers: authHeaders(), body: f });
    if (r.status === 401) { rejectToken(); return; }
    if (r.status === 409 && !overwrite) {
      if (confirm(f.name + " already exists here. Replace it?")) uploadFile(f, true);
      return;
    }
    if (r.status === 413) { toast("Too large to upload (50 MB max)"); return; }
    if (!r.ok) throw new Error("HTTP " + r.status);
    toast("Uploaded " + f.name);
    loadDir(filesPath);
  } catch (e) {
    toast("Couldn't upload");
  }
}

// ---- navigation chrome -----------------------------------------------------

$("btn-files").addEventListener("click", () => openExplorer(""));
$("btn-files-back").addEventListener("click", () => history.back());

// Explorer and editor sit on the history stack the way the terminal does, so
// back unwinds them one screen at a time. The terminal's own popstate handler
// ignores these pops — #screen-term is not active while either screen is up.
window.addEventListener("popstate", () => {
  if ($("screen-editor").classList.contains("active")) { editorPopped(); return; }
  if ($("screen-files").classList.contains("active")) closeExplorer();
});

// The terminal's left-edge back gesture (20-edge-swipe.js), re-armed for the
// screens this feature adds. A copy rather than a share: the terminal's
// binding also feeds the global edgeSwipe flag its scroll code reads, and
// entangling that is a worse trade than repeating two dozen lines.
function attachEdgeSwipe(scr, onBack) {
  let sx = 0, sy = 0, armed = false, committed = false;
  scr.addEventListener("touchstart", (e) => {
    if (e.touches.length !== 1) { armed = false; return; }
    const t = e.touches[0];
    armed = t.clientX <= EDGE_ZONE;
    sx = t.clientX; sy = t.clientY;
    committed = false;
  }, { passive: true, capture: true });
  scr.addEventListener("touchmove", (e) => {
    if (!armed || e.touches.length !== 1) return;
    const dx = e.touches[0].clientX - sx, dy = e.touches[0].clientY - sy;
    if (dx < 0 || Math.abs(dy) > Math.abs(dx)) {
      if (Math.abs(dy) > 12) armed = false;
      return;
    }
    committed = dx >= EDGE_TRIGGER;
  }, { passive: true, capture: true });
  const finish = () => {
    const go = armed && committed;
    armed = false; committed = false;
    if (go) onBack();
  };
  scr.addEventListener("touchend", finish, { passive: true });
  scr.addEventListener("touchcancel", finish, { passive: true });
}
attachEdgeSwipe($("screen-files"), () => history.back());
attachEdgeSwipe($("screen-editor"), () => history.back());

// Pull-down to refresh — the session list's own pattern.
(function() {
  let startY = null;
  const scr = $("screen-files");
  scr.addEventListener("touchstart", (e) => {
    startY = window.scrollY <= 0 ? e.touches[0].clientY : null;
  }, { passive: true });
  scr.addEventListener("touchend", (e) => {
    if (startY !== null && e.changedTouches[0].clientY - startY > 70 && filesPath) {
      loadDir(filesPath);
    }
    startY = null;
  }, { passive: true });
})();
