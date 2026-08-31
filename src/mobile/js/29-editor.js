// ============================================================
// Editor
// ============================================================
// CodeMirror 6, injected from vendor/ on the first file opened — the bundle is
// ~600 KB and the terminal's boot path must not carry it. The write-conflict
// token is the content hash /api/fs/read handed over: Save sends it back, and
// a 409 means the file moved underneath the editor.

let edView = null;        // the live EditorView, one at a time
let edPath = "";
let edHash = "";          // the hash this buffer was read at ("" = creating)
let edDirty = false;
let edReadOnly = false;   // a lossy (non-UTF-8) read must never be written back
let edThemeComp = null;   // swapped when the app theme flips
let edWrapComp = null;    // swapped when the Wrap button is tapped
let edVimComp = null;     // swapped when the Vim button is tapped

let cmLoading = null;
function ensureCM() {
  if (window.CM6) return Promise.resolve();
  if (cmLoading) return cmLoading;
  cmLoading = new Promise((resolve, reject) => {
    const s = document.createElement("script");
    // Same relative root as the boot-time vendor tags, and the same ?v=
    // cache-busting scheme the service worker already manages.
    s.src = "vendor/codemirror.js?v=" + buildVersion();
    s.onload = resolve;
    s.onerror = () => { cmLoading = null; s.remove(); reject(new Error("vendor load failed")); };
    document.head.appendChild(s);
  });
  return cmLoading;
}

function edLanguage(name) {
  const CM = window.CM6;
  const ext = (name.match(/\.([a-z0-9]+)$/i) || [, ""])[1].toLowerCase();
  switch (ext) {
    case "py": return CM.python();
    case "js": case "mjs": case "cjs": return CM.javascript();
    case "jsx": return CM.javascript({ jsx: true });
    case "ts": return CM.javascript({ typescript: true });
    case "tsx": return CM.javascript({ typescript: true, jsx: true });
    case "json": return CM.json();
    case "md": case "markdown": return CM.markdown();
    case "sh": case "bash": case "zsh": return CM.StreamLanguage.define(CM.shell);
    case "yaml": case "yml": return CM.yaml();
    case "html": case "htm": return CM.html();
    case "css": return CM.css();
  }
  return [];
}

// Prose wraps, code scrolls sideways — the Wrap button (cfg.editorWrapOn) adds
// wrapping on top of this for whatever type is open, it never removes it.
function edWraps(name) {
  return /\.(?:md|markdown|txt|text)$/i.test(name);
}

function edWrapExt() {
  const CM = window.CM6;
  return (edWraps(edPath) || cfg.editorWrapOn) ? CM.EditorView.lineWrapping : [];
}

function edVimExt() {
  return cfg.editorVimOn ? window.CM6.vim() : [];
}

function edThemeExt() {
  const CM = window.CM6;
  // The terminal's stored font size is the user's one stated preference about
  // reading monospace on this phone; the editor honours it on open.
  const size = CM.EditorView.theme({
    "&": { fontSize: storedFontSize() + "px", height: "100%" },
    ".cm-scroller": { fontFamily: 'Menlo, ui-monospace, "SF Mono", monospace' },
  });
  return resolvedDark()
    ? [CM.oneDark, size]
    : [CM.syntaxHighlighting(CM.defaultHighlightStyle), size];
}

// The theme button lives on the session list, but its toggle must still reach
// an editor that is merely hidden behind it. Watching the attribute keeps this
// fragment out of 04-theme.js entirely.
new MutationObserver(() => {
  if (edView && edThemeComp) {
    edView.dispatch({ effects: edThemeComp.reconfigure(edThemeExt()) });
  }
}).observe(document.documentElement,
           { attributes: true, attributeFilter: ["data-theme"] });

function edSetDirty(on) {
  edDirty = on;
  $("editor-dirty").classList.toggle("show", on);
  $("btn-editor-save").classList.toggle("armed", on && !edReadOnly);
}

// Paints the Wrap button from cfg. A prose file wraps regardless of the
// setting (edWraps(edPath) above), so the button still reads "on" for one —
// turning it off there would say something the editor is not doing.
function edSyncWrapButton() {
  $("btn-editor-wrap").setAttribute("aria-pressed",
    edWraps(edPath) || cfg.editorWrapOn ? "true" : "false");
}

function edSyncVimButton() {
  $("btn-editor-vim").setAttribute("aria-pressed",
    cfg.editorVimOn ? "true" : "false");
}

// ~/.vimrc, the useful subset of it. A desktop vimrc is mostly plugins,
// autocmds and functions that mean nothing in a browser buffer, so this reads
// the two kinds of line that do carry over — options and key mappings — and
// steps over everything else rather than approximating it.
const ED_VIM_MODES = { n: "normal", v: "visual", i: "insert", o: "operatorPending" };

// "\<Space>" and friends: a quoted vimscript string as a mapleader.
function edVimStr(s) {
  return s.trim().replace(/^["']|["']$/g, "").replace(/\\<Space>/gi, " ");
}

function edParseVimrc(text) {
  const rules = [];
  let leader = "\\";
  let inFunc = false;
  for (const raw of text.split("\n")) {
    const line = raw.trim();
    if (!line || line[0] === '"') continue;
    // A function body is arbitrary vimscript; nothing inside it is a mapping.
    if (/^fu(n|nc|nction)?!?\s/.test(line)) { inFunc = true; continue; }
    if (/^endf/.test(line)) { inFunc = false; continue; }
    if (inFunc) continue;

    let m = line.match(/^let\s+mapleader\s*=\s*(.+)$/);
    if (m) { leader = edVimStr(m[1]); continue; }

    m = line.match(/^set\s+(\S+)/);
    if (m) {
      // Only the first word: `set list listchars=...` sets two options, and
      // the second is a display detail CodeMirror has no say in anyway.
      const eq = m[1].indexOf("=");
      rules.push({
        kind: "set",
        name: eq < 0 ? m[1] : m[1].slice(0, eq),
        value: eq < 0 ? undefined : m[1].slice(eq + 1),
      });
      continue;
    }

    m = line.match(/^([nvio])?(nore)?map\s+(.+)$/);
    if (m) {
      const parts = m[3].split(/\s+/);
      // Anything else is a mapping with arguments (<silent>, <buffer>) or an
      // ex command carrying spaces — neither survives a naive translation.
      if (parts.length !== 2) continue;
      // <Plug> targets and :call both name something only the plugin defines.
      if (/<Plug>/i.test(parts[1]) || /^:call\b/i.test(parts[1])) continue;
      rules.push({
        kind: "map",
        nore: !!m[2],
        mode: m[1] ? ED_VIM_MODES[m[1]] : undefined,
        lhs: parts[0].replace(/<leader>/gi, leader),
        rhs: parts[1],
      });
    }
  }
  return rules;
}

function edApplyVimrc(rules) {
  const V = window.CM6.Vim;
  for (const r of rules) {
    // Per rule: an option this build has never heard of must not cost the
    // mappings that follow it.
    try {
      if (r.kind === "set") V.setOption(r.name, r.value);
      else if (r.nore) V.noremap(r.lhs, r.rhs, r.mode);
      else V.map(r.lhs, r.rhs, r.mode);
    } catch (e) { /* unsupported line, skipped */ }
  }
}

// Once per page load — the mappings land in vim's global state, not the view's,
// so re-reading on every open would only re-apply them. Not having a ~/.vimrc
// is the normal case, so a failed read says nothing.
let edVimrcRead = null;
function edLoadVimrc() {
  if (edVimrcRead) return edVimrcRead;
  edVimrcRead = (async () => {
    try {
      const r = await fetch(apiURL("api/fs/read?path=" + encodeURIComponent("~/.vimrc")),
                            { cache: "no-store", headers: authHeaders() });
      if (!r.ok) return;
      const data = await r.json();
      if (data && typeof data.content === "string") {
        edApplyVimrc(edParseVimrc(data.content));
      }
    } catch (e) { /* no vimrc, or unreadable — vim mode works without one */ }
  })();
  return edVimrcRead;
}

// :w and :q are muscle memory, and the extension ships neither — its ex command
// list has `write` (which writes to a textarea that does not exist here) and no
// quit at all. These wire the two words to the buttons they mean. Registered
// once per page load, like the vimrc: they land in vim's global command map.
//
// Matching is by prefix of the full name — ":q" reaches "quit", but ":qa" and
// ":wqa" reach nothing unless registered in their own right, so each *all form
// is spelled out. One file per editor, so the all-buffer forms mean the same
// thing as the plain ones.
let edVimExDone = false;
function edDefineVimEx() {
  if (edVimExDone) return;
  edVimExDone = true;
  const V = window.CM6.Vim;
  // ":q!" arrives as command "q" with "!" as its argument.
  const bang = (params) => /^\s*!/.test((params && params.argString) || "");
  const quit = (cm, params) => {
    // Discarding is dropping the dirty flag before the pop: editorPopped's
    // confirm is what a bare :q is meant to hit and a :q! is meant to skip.
    if (bang(params)) edSetDirty(false);
    history.back();
  };
  const saveQuit = async () => {
    // editorSave clears dirty only on a write that landed, and no-ops on a
    // read-only buffer — either way, closing is what did not fail.
    if (!edReadOnly) await editorSave();
    if (!edDirty) history.back();
  };
  // The filename argument is ignored: this editor has exactly one file open.
  V.defineEx("write", "w", () => { editorSave(); });
  V.defineEx("quit", "q", quit);
  V.defineEx("qall", "qa", quit);
  V.defineEx("wq", "wq", saveQuit);
  V.defineEx("wqall", "wqa", saveQuit);
  V.defineEx("xit", "x", saveQuit);
  V.defineEx("xall", "xa", saveQuit);
}

// opts.create opens an empty buffer for a file that does not exist yet;
// opts.noHistory says the entry this screen unwinds on is already on the stack
// — the markdown reader's Edit hands over the one it pushed, so the two views
// of a file cost one back between them.
async function openEditor(path, opts) {
  const create = !!(opts && opts.create);
  let content = "", hash = "", lossy = false;
  if (!create) {
    const data = await fsReadText(path);
    if (!data) return;
    content = data.content; hash = data.hash; lossy = !!data.lossy;
  }
  try { await ensureCM(); } catch (e) { toast("Couldn't load the editor"); return; }

  edPath = path;
  edHash = hash;
  edReadOnly = lossy;
  $("editor-filename").textContent = baseName(path);
  // Read-only has nothing to save; hiding the button says so louder than
  // disabling it would.
  $("btn-editor-save").style.display = lossy ? "none" : "";
  edBuild(content, path);
  edSetDirty(false);
  edSyncWrapButton();
  edSyncVimButton();
  if (cfg.editorVimOn) { edDefineVimEx(); edLoadVimrc(); }

  $("screen-files").classList.remove("active");
  $("screen-editor").classList.add("active");
  if (!(opts && opts.noHistory)) history.pushState({ editor: true }, "", location.href);
  if (lossy) toast("Not valid UTF-8 — opened read-only");
}

function edBuild(content, path) {
  const CM = window.CM6;
  if (edView) edView.destroy();
  edThemeComp = new CM.Compartment();
  edWrapComp = new CM.Compartment();
  edVimComp = new CM.Compartment();
  edView = new CM.EditorView({
    state: CM.EditorState.create({
      doc: content,
      extensions: [
        // First, before every other keymap: vim reads keys ahead of the
        // default bindings or it never sees the ones they already claim.
        edVimComp.of(edVimExt()),
        CM.lineNumbers(),
        CM.highlightActiveLineGutter(),
        CM.highlightSpecialChars(),
        CM.history(),
        CM.drawSelection(),
        CM.dropCursor(),
        CM.indentOnInput(),
        CM.bracketMatching(),
        CM.highlightActiveLine(),
        CM.keymap.of([...CM.defaultKeymap, ...CM.historyKeymap, CM.indentWithTab]),
        edLanguage(path),
        edWrapComp.of(edWrapExt()),
        edThemeComp.of(edThemeExt()),
        edReadOnly
          ? [CM.EditorState.readOnly.of(true), CM.EditorView.editable.of(false)]
          : [],
        CM.EditorView.updateListener.of((u) => {
          if (u.docChanged && !edDirty) edSetDirty(true);
        }),
      ],
    }),
    parent: $("editor-host"),
  });
}

// ---- putting the buffer away (see fileViews in 09-image-viewer.js) ---------
// A rail switch takes the editor off screen without asking about unsaved work,
// because nothing is discarded: CodeMirror's EditorState is an immutable value
// holding the document, the selection and the undo history, so keeping a
// reference to it is the whole capture — no serialization, and what comes back
// is the buffer itself rather than a copy of its text.

function edStash() {
  const s = {
    state: edView.state, path: edPath, hash: edHash,
    dirty: edDirty, readOnly: edReadOnly,
    // The compartments travel with the state they live in. They are handles
    // identified by object, so the toggles and the theme observer must
    // reconfigure these ones or they would address a compartment the rebuilt
    // view's state has never heard of.
    comps: { theme: edThemeComp, wrap: edWrapComp, vim: edVimComp },
  };
  // Only the view goes; the state above outlives it.
  edView.destroy();
  edView = null;
  edPath = "";
  edSetDirty(false);
  $("screen-editor").classList.remove("active");
  return s;
}

function edRestore(s) {
  edPath = s.path;
  edHash = s.hash;
  edReadOnly = s.readOnly;
  edThemeComp = s.comps.theme;
  edWrapComp = s.comps.wrap;
  edVimComp = s.comps.vim;
  $("editor-filename").textContent = baseName(edPath);
  $("btn-editor-save").style.display = edReadOnly ? "none" : "";
  if (edView) edView.destroy();
  edView = new window.CM6.EditorView({ state: s.state, parent: $("editor-host") });
  // The theme, the wrap setting and vim can all have moved while the buffer was
  // away: the observer above only reaches a live view, and the buttons only
  // reconfigure the one that is up. Reconfiguring is not a document change, so
  // the dirty flag below is still the buffer's own.
  edView.dispatch({ effects: [
    edThemeComp.reconfigure(edThemeExt()),
    edWrapComp.reconfigure(edWrapExt()),
    edVimComp.reconfigure(edVimExt()),
  ] });
  edSetDirty(s.dirty);
  edSyncWrapButton();
  edSyncVimButton();
  $("screen-files").classList.remove("active");
  $("screen-editor").classList.add("active");
  // Deliberately not focused: a freshly opened editor is not either, and a
  // restore that raised the soft keyboard would be a tablet's rude surprise.
  // The caret is in the state, so it is already back where it was left.
}

async function editorSave(baseHash) {
  if (!edView || edReadOnly) return;
  const btn = $("btn-editor-save");
  btn.disabled = true;
  try {
    const r = await fetch(apiURL("api/fs/write"), {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({
        path: edPath,
        content: edView.state.doc.toString(),
        hash: baseHash === undefined ? edHash : baseHash,
      }),
    });
    if (r.status === 401) { rejectToken(); return; }
    const data = await r.json().catch(() => null);
    if (r.status === 409) {
      // The file on disk is not what this buffer was read from. Overwrite is
      // a resend against the hash the 409 reported; reload drops the edits.
      if (confirm("This file changed on disk since you opened it.\n\n"
                  + "OK overwrites it with your version; Cancel reloads the "
                  + "file and drops your edits.")) {
        editorSave((data && data.hash) || "");
      } else {
        editorReload();
      }
      return;
    }
    if (!r.ok || !data) { toast("Couldn't save"); return; }
    edHash = data.hash;
    edSetDirty(false);
    toast("Saved");
  } catch (e) {
    toast("Couldn't save");
  } finally {
    btn.disabled = false;
  }
}

async function editorReload() {
  try {
    const r = await fetch(apiURL("api/fs/read?path=" + encodeURIComponent(edPath)),
                          { cache: "no-store", headers: authHeaders() });
    const data = await r.json().catch(() => null);
    if (!r.ok || !data) throw new Error("HTTP " + r.status);
    edHash = data.hash;
    edView.dispatch({
      changes: { from: 0, to: edView.state.doc.length, insert: data.content },
    });
    // After the dispatch, whose docChanged just marked the buffer dirty.
    edSetDirty(false);
    toast("Reloaded from disk");
  } catch (e) {
    toast("Couldn't reload");
  }
}

// The pop has already happened by the time a dirty buffer gets its say, so
// "stay" means pushing the entry back rather than cancelling anything.
function editorPopped() {
  if (edDirty && !confirm("Discard your unsaved changes?")) {
    history.pushState({ editor: true }, "", location.href);
    return;
  }
  closeEditor();
}

function closeEditor() {
  $("screen-editor").classList.remove("active");
  $("screen-files").classList.add("active");
  if (edView) { edView.destroy(); edView = null; }
  edSetDirty(false);
  edPath = "";
  // A save may have changed what the list shows — sizes, a file born on save.
  if (filesPath) loadDir(filesPath);
}

$("btn-editor-back").addEventListener("click", () => history.back());
$("btn-editor-save").addEventListener("click", () => editorSave());
// Applies immediately, same as the key-bar toggles in Settings: reconfigure
// the compartment rather than rebuilding the buffer, so the cursor, selection
// and undo history all survive the tap.
$("btn-editor-wrap").addEventListener("click", () => {
  cfg.editorWrapOn = !cfg.editorWrapOn;
  if (edView) edView.dispatch({ effects: edWrapComp.reconfigure(edWrapExt()) });
  edSyncWrapButton();
});
$("btn-editor-vim").addEventListener("click", () => {
  cfg.editorVimOn = !cfg.editorVimOn;
  if (edView) edView.dispatch({ effects: edVimComp.reconfigure(edVimExt()) });
  edSyncVimButton();
  if (cfg.editorVimOn) { edDefineVimEx(); edLoadVimrc(); }
});
