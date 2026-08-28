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

// Prose wraps, code scrolls sideways.
function edWraps(name) {
  return /\.(?:md|markdown|txt|text)$/i.test(name);
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

async function openEditor(path, opts) {
  const create = !!(opts && opts.create);
  let content = "", hash = "", lossy = false;
  if (!create) {
    let r, data;
    try {
      r = await fetch(apiURL("api/fs/read?path=" + encodeURIComponent(path)),
                      { cache: "no-store", headers: authHeaders() });
      data = await r.json().catch(() => null);
    } catch (e) { toast("Couldn't read the file"); return; }
    if (r.status === 401) { rejectToken(); return; }
    if (!r.ok) {
      const err = data && data.error;
      if (err === "binary_file" || err === "too_large") {
        const why = err === "binary_file" ? "isn't a text file"
                                          : "is too big to edit here";
        if (confirm(baseName(path) + " " + why + ". Download it instead?")) {
          downloadFile(path, baseName(path));
        }
      } else {
        toast("Couldn't read the file");
      }
      return;
    }
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

  $("screen-files").classList.remove("active");
  $("screen-editor").classList.add("active");
  history.pushState({ editor: true }, "", location.href);
  if (lossy) toast("Not valid UTF-8 — opened read-only");
}

function edBuild(content, path) {
  const CM = window.CM6;
  if (edView) edView.destroy();
  edThemeComp = new CM.Compartment();
  edView = new CM.EditorView({
    state: CM.EditorState.create({
      doc: content,
      extensions: [
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
        edWraps(path) ? CM.EditorView.lineWrapping : [],
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
