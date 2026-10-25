// ============================================================
// Editor
// ============================================================
// CodeMirror 6, injected from vendor/ on the first file opened — the bundle is
// ~600 KB and the terminal's boot path must not carry it. The write-conflict
// token is the content hash /api/fs/read handed over: Save sends it back, and
// a 409 means the file moved underneath the editor.

// Every live view is a rec from edMakeView, held in edLive so that the theme,
// Wrap and Vim, all global, reach each of them. The editor screen
// (#screen-editor) owns one rec, edRec; the globals below mirror it for the
// callers that predate the factory (09-image-viewer.js among them).
const edLive = new Set();
let edRec = null;
let edView = null;        // edRec.view
let edPath = "";
let edDirty = false;
let edReadOnly = false;   // a lossy (non-UTF-8) read must never be written back
// What the editor hands the screen back to when it closes, {show()}, or null for
// the listing it was opened from (openEditor's opts.returnTo).
let edReturn = null;

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
    case "py": return [CM.python(), edPyMarksExt()];
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

function edWrapExt(path) {
  const CM = window.CM6;
  return (edWraps(path) || cfg.editorWrapOn)
    ? [CM.EditorView.lineWrapping, edHangExt()] : [];
}

// A line's left padding inside the content, the theme's and the hanging
// indent's alike: the two have to agree or a wrapped line's first row shifts.
const ED_LINE_PAD = 8;

// Wrapped rows hang under the line's own indentation instead of restarting at
// the content's left edge: an indented line is padded in by its indent and its
// first row pulled back out by the same amount (text-indent), so that row stays
// where it was and every continuation row starts under the code. Columns, with
// tabs expanded, and ch, which is one column of a monospace font.
let edHangPlugin = null;
function edHangExt() {
  if (edHangPlugin) return edHangPlugin;
  const CM = window.CM6;
  const build = (view) => {
    const b = new CM.RangeSetBuilder();
    const tab = view.state.tabSize;
    // Ranges must go in sorted, and two visible ranges can share a line.
    let next = 0;
    for (const { from, to } of view.visibleRanges) {
      for (let pos = Math.max(from, next); pos <= to;) {
        const line = view.state.doc.lineAt(pos);
        const lead = /^[ \t]*/.exec(line.text)[0];
        // A blank line has no code to hang anything under.
        if (lead.length && lead.length < line.length) {
          let cols = 0;
          for (const ch of lead) cols = ch === "\t" ? cols + tab - cols % tab : cols + 1;
          b.add(line.from, line.from, CM.Decoration.line({ attributes: {
            style: `padding-left: calc(${cols}ch + ${ED_LINE_PAD}px); text-indent: -${cols}ch`,
          } }));
        }
        pos = next = line.to + 1;
      }
    }
    return b.finish();
  };
  edHangPlugin = CM.ViewPlugin.fromClass(class {
    constructor(view) { this.decorations = build(view); }
    update(u) {
      if (u.docChanged || u.viewportChanged) this.decorations = build(u.view);
    }
  }, { decorations: (v) => v.decorations });
  return edHangPlugin;
}

// Docstrings and decorators, which lang-python tags as a plain string and a
// bare "@": a docstring is a string statement standing first in a module, class
// or function body, and reads as the comment it is; a decorator is coloured
// from its "@" to the end of its name, its arguments left to the highlighter.
let edPyPlugin = null;
function edPyMarksExt() {
  if (edPyPlugin) return edPyPlugin;
  const CM = window.CM6;
  const doc = CM.Decoration.mark({ class: "cm-ptui-doc" });
  const deco = CM.Decoration.mark({ class: "cm-ptui-deco" });
  const firstInBody = (stmt) => {
    let prev = stmt.prevSibling;
    while (prev && prev.name === "Comment") prev = prev.prevSibling;
    if (stmt.parent.name === "Script") return !prev;
    return stmt.parent.name === "Body" && !!prev && prev.name === ":";
  };
  const build = (view) => {
    const b = new CM.RangeSetBuilder();
    const tree = CM.syntaxTree(view.state);
    let last = 0;
    const add = (from, to, mark) => {
      if (from >= last) { b.add(from, to, mark); last = to; }
    };
    for (const { from, to } of view.visibleRanges) {
      tree.iterate({ from, to, enter: (n) => {
        if (n.name === "Decorator") {
          const text = view.state.sliceDoc(n.from, n.to);
          const end = n.from + text.search(/[(\n]|$/);
          if (end > n.from) add(n.from, end, deco);
          return false;
        }
        if (n.name !== "String") return;
        const stmt = n.node.parent;
        if (stmt && stmt.name === "ExpressionStatement" && stmt.parent
            && stmt.from === n.from && firstInBody(stmt)) {
          add(n.from, n.to, doc);
        }
        return false;
      } });
    }
    return b.finish();
  };
  edPyPlugin = CM.ViewPlugin.fromClass(class {
    constructor(view) { this.decorations = build(view); }
    update(u) {
      if (u.docChanged || u.viewportChanged
          || CM.syntaxTree(u.startState) !== CM.syntaxTree(u.state)) {
        this.decorations = build(u.view);
      }
    }
  }, { decorations: (v) => v.decorations });
  return edPyPlugin;
}

function edVimExt() {
  return cfg.editorVimOn ? window.CM6.vim() : [];
}

// The editor wears the palette the terminal does (currentTermTheme(),
// 07-terminal.js): its background and foreground, its ANSI slots for the
// syntax, and the chrome derivation's accent and hairline (chromeTokensFor,
// boot-theme.js) for the cursor, the selection and the gutter's rule. Every
// colour is held to the 4.5 ratio xterm is given (minimumContrastRatio), since
// someone else's scheme is free to pair a yellow with a cream background.
function edThemeExt() {
  const CM = window.CM6, t = CM.tags;
  const p = currentTermTheme();
  const bg = p.background;
  const dark = paletteIsDark(p);
  const chrome = chromeTokensFor(p);
  const text = chrome["--ink"];
  const accent = chrome["--umber"];
  const slot = (name) => pickInk(p[name],
    p["bright" + name[0].toUpperCase() + name.slice(1)], bg, 4.5);
  const muted = ensureContrast(mixHex(text, bg, 0.45), bg, 4.5);
  const lineTint = rgbaHex(p.foreground, dark ? 0.05 : 0.04);
  const c = {
    keyword: slot("magenta"), string: slot("green"), literal: slot("red"),
    fn: slot("blue"), type: slot("yellow"), op: slot("cyan"),
  };
  const highlight = CM.HighlightStyle.define([
    { tag: [t.keyword, t.modifier, t.self], color: c.keyword },
    { tag: [t.string, t.special(t.string), t.regexp, t.character], color: c.string },
    { tag: [t.number, t.bool, t.null, t.atom], color: c.literal },
    { tag: [t.function(t.variableName), t.function(t.propertyName), t.macroName],
      color: c.fn },
    { tag: [t.className, t.typeName, t.namespace], color: c.type },
    { tag: [t.operator, t.escape, t.meta], color: c.op },
    // The dot of a.b is an operator to the parser and punctuation to a reader.
    { tag: t.derefOperator, color: text },
    { tag: t.comment, color: muted, fontStyle: "italic" },
    { tag: t.invalid, color: slot("red") },
    // Markdown and the markup languages.
    { tag: t.heading, color: c.fn, fontWeight: "bold" },
    { tag: t.strong, fontWeight: "bold" },
    { tag: t.emphasis, fontStyle: "italic" },
    { tag: t.strikethrough, textDecoration: "line-through" },
    { tag: [t.link, t.url], color: c.fn, textDecoration: "underline" },
    { tag: t.monospace, color: c.string },
    { tag: [t.quote, t.contentSeparator, t.processingInstruction], color: muted },
    { tag: t.tagName, color: c.keyword },
    { tag: t.attributeName, color: c.type },
    { tag: t.angleBracket, color: muted },
  ]);
  const theme = CM.EditorView.theme({
    // The terminal's stored font size is the user's one stated preference
    // about reading monospace on this phone; the editor honours it on open.
    "&": { color: text, backgroundColor: bg,
           fontSize: storedFontSize() + "px", height: "100%" },
    ".cm-scroller": { fontFamily: 'Menlo, ui-monospace, "SF Mono", monospace' },
    ".cm-content": { caretColor: accent },
    ".cm-line": { padding: `0 2px 0 ${ED_LINE_PAD}px` },
    ".cm-cursor, .cm-dropCursor": { borderLeftColor: accent },
    "&.cm-focused > .cm-scroller > .cm-selectionLayer .cm-selectionBackground, .cm-selectionBackground, .cm-content ::selection":
      { backgroundColor: rgbaHex(accent, dark ? 0.30 : 0.22) },
    // Translucent, because it is laid over the selection layer.
    ".cm-activeLine": { backgroundColor: lineTint },
    // The gutter is the page, set off by the chrome's own hairline.
    ".cm-gutters": { backgroundColor: bg, color: ensureContrast(mixHex(text, bg, 0.55), bg, 3),
                     border: "none", borderRight: "1px solid " + chrome["--hair"] },
    ".cm-lineNumbers .cm-gutterElement": { padding: "0 10px 0 12px" },
    ".cm-activeLineGutter": { backgroundColor: lineTint, color: text },
    // The lines a caller asked to be shown (edRangeExt): the accent, quieter
    // than the selection's so a selection inside the range still reads as one.
    // After the active line, which sits on the same element, so that the
    // range's first line, where the caret lands, is tinted like the rest.
    ".cm-hc-range": { backgroundColor: rgbaHex(accent, dark ? 0.14 : 0.10) },
    "&.cm-focused .cm-matchingBracket": {
      backgroundColor: rgbaHex(accent, 0.18), outline: "1px solid " + rgbaHex(accent, 0.5),
    },
    "&.cm-focused .cm-nonmatchingBracket": { backgroundColor: rgbaHex(slot("red"), 0.2) },
    // edPyMarksExt's two marks. The span inside is the highlighter's string
    // colour, which the docstring's has to outrank.
    ".cm-ptui-doc, .cm-ptui-doc *": { color: muted, fontStyle: "italic" },
    ".cm-ptui-deco, .cm-ptui-deco *": { color: c.op },
    ".cm-panels": { backgroundColor: chrome["--card"], color: text },
  }, { dark });
  return [theme, CM.syntaxHighlighting(highlight)];
}

function edRetheme() {
  if (!edLive.size) return;
  const ext = edThemeExt();
  for (const rec of edLive) {
    rec.view.dispatch({ effects: rec.comps.theme.reconfigure(ext) });
  }
}

// The theme button lives on the session list, but its toggle must still reach
// an editor that is merely hidden behind it. Watching the attribute keeps this
// fragment out of 04-theme.js entirely. A palette swapped for another of the
// same lightness leaves the attribute alone, so applyTermTheme()'s event
// (07-terminal.js) is the second way in.
new MutationObserver(edRetheme).observe(document.documentElement,
  { attributes: true, attributeFilter: ["data-theme"] });
window.addEventListener("ptui-termtheme", edRetheme);

function edSetDirty(on) {
  edDirty = on;
  if (edRec) edRec.dirty = on;
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
// is spelled out. One file per view, so the all-buffer forms mean the same
// thing as the plain ones.
//
// The map is global, so each command finds the view it was typed in: the one
// with focus, else the editor screen's. A rec's own onSave/onQuit stand in
// for the screen's Save button and back arrow.
function edVimRec() {
  return [...edLive].find((r) => r.view.hasFocus) || edRec;
}

function edRecSave(rec) {
  return rec.onSave ? rec.onSave(rec) : edViewSave(rec);
}

function edRecQuit(rec, bang) {
  if (rec.onQuit) rec.onQuit(rec, bang);
  else editorLeave();
}

let edVimExDone = false;
function edDefineVimEx() {
  if (edVimExDone) return;
  edVimExDone = true;
  const V = window.CM6.Vim;
  // ":q!" arrives as command "q" with "!" as its argument.
  const bang = (params) => /^\s*!/.test((params && params.argString) || "");
  const quit = (cm, params) => {
    const rec = edVimRec();
    if (!rec) return;
    // Discarding is dropping the dirty flag before the pop: editorPopped's
    // confirm is what a bare :q is meant to hit and a :q! is meant to skip.
    const discard = bang(params);
    if (discard) { rec.dirty = false; rec.onDirty(rec, false); }
    edRecQuit(rec, discard);
  };
  const saveQuit = async () => {
    const rec = edVimRec();
    if (!rec) return;
    // A save clears dirty only on a write that landed, and a read-only buffer
    // has nothing to save — either way, closing is what did not fail.
    if (!rec.readOnly) await edRecSave(rec);
    if (!rec.dirty) edRecQuit(rec, false);
  };
  // The filename argument is ignored: a view has exactly one file open.
  V.defineEx("write", "w", () => { const rec = edVimRec(); if (rec) edRecSave(rec); });
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
//
// opts.ref and opts.root read the file as it stands at a commit of the repo at
// root rather than the way the explorer's branch would (fsReadTextAt,
// 28-file-explorer.js), read-only like any file read at a ref. A commit the repo
// no longer has — rebased away, or never fetched into this clone — is not a
// reason to open nothing: the file is read off the disk instead, editable, and
// one toast names the commit that was asked for.
//
// opts.line and opts.lineEnd, 1-based and inclusive, are a place in the file to
// be shown: the caret goes to the start of `line`, which is scrolled to the
// middle of the view, and the lines through lineEnd are tinted (edRangeExt).
// The tint is the buffer's own, so the next file opened has none.
//
// opts.returnTo, {show()}, is the screen the editor was opened over, put back
// when the editor closes in place of the listing (closeEditor).
async function openEditor(path, opts) {
  usageCount("editor");
  const create = !!(opts && opts.create);
  const ref = (opts && opts.ref) || "";
  let content = "", hash = "", lossy = false, atRef = false;
  if (!create) {
    let data = ref ? await fsReadTextAt(path, ref, opts.root || "")
                   : await fsReadText(path);
    if (data && data.error) {
      toast(edShortRef(ref) + " is not in this repo, so this is the file as it is now");
      data = await fsReadTextAt(path, "", "");
    }
    if (!data) return;
    content = data.content; hash = data.hash; lossy = !!data.lossy;
    // Read at a git ref rather than off the disk: there is a file here to read
    // and none here to write, since /api/fs/write only ever writes the working
    // tree. The explorer says which branch; the missing Save says the rest.
    atRef = !!data.readonly;
  }
  try { await ensureCM(); } catch (e) { toast("Couldn't load the editor"); return; }

  edPath = path;
  edReadOnly = lossy || atRef;
  $("editor-filename").textContent = baseName(path);
  // Read-only has nothing to save; hiding the button says so louder than
  // disabling it would.
  $("btn-editor-save").style.display = edReadOnly ? "none" : "";
  $("btn-editor-download").style.display = canDownload(path) ? "" : "none";
  const range = opts && opts.line ? { line: opts.line, lineEnd: opts.lineEnd } : null;
  edBuild(content, path, range);
  edRec.hash = hash;
  edReturn = (opts && opts.returnTo) || null;
  edSetDirty(false);
  edSyncWrapButton();
  edSyncVimButton();
  if (cfg.editorVimOn) { edDefineVimEx(); edLoadVimrc(); }

  // In the pane if that is where the file was tapped, over the whole window
  // otherwise (dockFileView, 28-file-explorer.js). Docked it pushes no entry:
  // every other interaction in the pane pushes none either, and back beside a
  // live terminal is the terminal's.
  const docked = dockFileView($("screen-editor"), opts && opts.pane);
  $("screen-editor").classList.add("active");
  if (!docked && !(opts && opts.noHistory)) {
    history.pushState({ editor: true }, "", location.href);
  }
  // Once the screen is showing: a view that is not displayed has no heights to
  // measure, and a scroll asked of it would land nowhere.
  if (range) edShowLine(range.line);
  if (lossy) toast("Not valid UTF-8 — opened read-only");
}

// A commit as a toast can name it: a full hash is shortened the way git's own
// output does, anything else (a branch, a short hash) is already short.
function edShortRef(ref) {
  return /^[0-9a-f]{12,}$/i.test(ref) ? ref.slice(0, 7) : ref;
}

// A line number from outside the editor, held to the lines the file has: the
// file may have shrunk since the number was taken.
function edClampLine(doc, n) {
  return Math.min(Math.max(1, Math.floor(Number(n)) || 1), doc.lines);
}

// The tint over a range of lines, in a compartment of its own so that it is a
// thing the buffer either carries or does not: every open builds a new state,
// and one opened without a range gets the compartment empty. A StateField
// rather than a plugin because the range belongs to the document, not to what
// is on screen, and an edit above it maps it down with the text it covers.
// The workspace's editor tabs carry the same compartment (47-workspace.js),
// and a tab already open takes a new range through it (edRangeField).
let edRangeComp = null;
function edRangeExt(range) {
  const CM = window.CM6;
  if (!edRangeComp) edRangeComp = new CM.Compartment();
  return edRangeComp.of(range ? edRangeField(range) : []);
}

function edRangeField(range) {
  const CM = window.CM6;
  const mark = CM.Decoration.line({ class: "cm-hc-range" });
  return CM.StateField.define({
    create(state) {
      const doc = state.doc;
      const a = edClampLine(doc, range.line);
      const b = Math.max(a, edClampLine(doc, range.lineEnd || range.line));
      const marks = [];
      for (let n = a; n <= b; n++) marks.push(mark.range(doc.line(n).from));
      return CM.Decoration.set(marks);
    },
    update: (marks, tr) => marks.map(tr.changes),
    provide: (f) => CM.EditorView.decorations.from(f),
  });
}

// The caret to the start of a line, and the line to the middle of the view:
// the editor screen's, or `view`, a workspace tab's.
function edShowLine(n, view) {
  view = view || edView;
  const pos = view.state.doc.line(edClampLine(view.state.doc, n)).from;
  view.dispatch({
    selection: { anchor: pos },
    effects: window.CM6.EditorView.scrollIntoView(pos, { y: "center" }),
  });
}

// One CodeMirror view in `host`. With `content` the state is built here; with
// `state` and `comps` it is a stashed buffer coming back (edRestore), whose
// extensions already live in the state and only need bringing up to date.
// onDirty(rec, dirty) hears every flip of rec.dirty; onSave(rec) and
// onQuit(rec, bang) are what vim's :w and :q do in this view (edDefineVimEx).
// `extensions` are added to a new state after the editor's own.
function edMakeView(opts) {
  const CM = window.CM6;
  const rec = {
    view: null, path: opts.path, hash: "", dirty: false, readOnly: !!opts.readOnly,
    comps: opts.comps || {
      theme: new CM.Compartment(),   // swapped when the app theme flips
      wrap: new CM.Compartment(),    // swapped when the Wrap button is tapped
      vim: new CM.Compartment(),     // swapped when the Vim button is tapped
    },
    onDirty: opts.onDirty, onSave: opts.onSave, onQuit: opts.onQuit,
  };
  if (opts.state) {
    rec.view = new CM.EditorView({ state: opts.state, parent: opts.host });
    // The theme, the wrap setting and vim can all have moved while the buffer
    // was away: the observer above and the buttons only reach live views.
    // Reconfiguring is not a document change, so dirty stays the buffer's own.
    rec.view.dispatch({ effects: [
      rec.comps.theme.reconfigure(edThemeExt()),
      rec.comps.wrap.reconfigure(edWrapExt(rec.path)),
      rec.comps.vim.reconfigure(edVimExt()),
    ] });
  } else {
    rec.view = new CM.EditorView({
      state: CM.EditorState.create({
        doc: opts.content || "",
        extensions: [
          // First, before every other keymap: vim reads keys ahead of the
          // default bindings or it never sees the ones they already claim.
          rec.comps.vim.of(edVimExt()),
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
          edLanguage(rec.path),
          rec.comps.wrap.of(edWrapExt(rec.path)),
          rec.comps.theme.of(edThemeExt()),
          rec.readOnly
            ? [CM.EditorState.readOnly.of(true), CM.EditorView.editable.of(false)]
            : [],
          CM.EditorView.updateListener.of((u) => {
            if (!u.docChanged) return;
            // Found by its view rather than closed over: the listener lives in
            // the state, and a restored state comes back under a new rec.
            for (const r of edLive) {
              if (r.view === u.view && !r.dirty) { r.dirty = true; r.onDirty(r, true); }
            }
          }),
          // A caller's own, last: the workspace's change marks (47-workspace.js).
          opts.extensions || [],
        ],
      }),
      parent: opts.host,
    });
  }
  edLive.add(rec);
  return rec;
}

function edDropView(rec) {
  rec.view.destroy();
  edLive.delete(rec);
}

// The editor screen's rec: its dirty flag drives the screen's marks, and its
// save is the Save button's, which disables the button while it runs.
function edScreenView(opts) {
  if (edRec) edDropView(edRec);
  edRec = edMakeView(Object.assign({
    host: $("editor-host"),
    onDirty: (rec, on) => edSetDirty(on),
    onSave: () => editorSave(),
  }, opts));
  edView = edRec.view;
}

function edBuild(content, path, range) {
  edScreenView({ path, content, readOnly: edReadOnly, extensions: edRangeExt(range) });
}

// ---- putting the buffer away (see fileViews in 09-image-viewer.js) ---------
// A rail switch takes the editor off screen without asking about unsaved work,
// because nothing is discarded: CodeMirror's EditorState is an immutable value
// holding the document, the selection and the undo history, so keeping a
// reference to it is the whole capture — no serialization, and what comes back
// is the buffer itself rather than a copy of its text.

function edStash() {
  const s = {
    state: edView.state, path: edPath, hash: edRec.hash,
    dirty: edDirty, readOnly: edReadOnly,
    // The compartments travel with the state they live in. They are handles
    // identified by object, so the toggles and the theme observer must
    // reconfigure these ones or they would address a compartment the rebuilt
    // view's state has never heard of.
    comps: edRec.comps,
  };
  // Only the view goes; the state above outlives it.
  edDropView(edRec);
  edRec = null;
  edView = null;
  edPath = "";
  // The screen it was opened over goes too. That screen is put away by the same
  // switch, and what comes back on the return is the editor alone, as any
  // restored editor does: closing it lands on the listing, and the brief or
  // whatever it was is opened again from there.
  edReturn = null;
  edSetDirty(false);
  $("screen-editor").classList.remove("active");
  // The shape goes with the screen rather than into the record: the restore
  // asks the pane it comes back to, which is the pane this session left.
  $("screen-editor").classList.remove("docked");
  return s;
}

function edRestore(s) {
  edPath = s.path;
  edReadOnly = s.readOnly;
  $("editor-filename").textContent = baseName(edPath);
  $("btn-editor-save").style.display = edReadOnly ? "none" : "";
  $("btn-editor-download").style.display = canDownload(edPath) ? "" : "none";
  edScreenView({ path: s.path, state: s.state, comps: s.comps, readOnly: s.readOnly });
  edRec.hash = s.hash;
  edSetDirty(s.dirty);
  edSyncWrapButton();
  edSyncVimButton();
  dockFileView($("screen-editor"));
  $("screen-editor").classList.add("active");
  // Deliberately not focused: a freshly opened editor is not either, and a
  // restore that raised the soft keyboard would be a tablet's rude surprise.
  // The caret is in the state, so it is already back where it was left.
}

// Writes a rec's buffer back. True when the write landed; false on any other
// outcome, the dirty flag then still saying what it said.
async function edViewSave(rec, baseHash) {
  if (rec.readOnly) return false;
  try {
    const r = await fetch(apiURL("api/fs/write"), {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({
        path: rec.path,
        content: rec.view.state.doc.toString(),
        hash: baseHash === undefined ? rec.hash : baseHash,
      }),
    });
    if (r.status === 401) { rejectToken(); return false; }
    const data = await r.json().catch(() => null);
    if (r.status === 409) {
      // The file on disk is not what this buffer was read from. Overwrite is
      // a resend against the hash the 409 reported; reload drops the edits.
      const overwrite = await appConfirm(
        baseName(rec.path) + " changed on disk since you opened it. Overwrite "
        + "replaces it with your version; Cancel reloads the file and drops "
        + "your edits.",
        { title: "File changed on disk", confirmLabel: "Overwrite" });
      if (overwrite) return edViewSave(rec, (data && data.hash) || "");
      await edViewReload(rec);
      return false;
    }
    if (!r.ok || !data) { toast("Couldn't save"); return false; }
    rec.hash = data.hash;
    rec.dirty = false;
    rec.onDirty(rec, false);
    toast("Saved");
    return true;
  } catch (e) {
    toast("Couldn't save");
    return false;
  }
}

async function edViewReload(rec) {
  try {
    const r = await fetch(apiURL("api/fs/read?path=" + encodeURIComponent(rec.path)),
                          { cache: "no-store", headers: authHeaders() });
    const data = await r.json().catch(() => null);
    if (!r.ok || !data) throw new Error("HTTP " + r.status);
    rec.hash = data.hash;
    rec.view.dispatch({
      changes: { from: 0, to: rec.view.state.doc.length, insert: data.content },
    });
    // After the dispatch, whose docChanged just marked the buffer dirty.
    rec.dirty = false;
    rec.onDirty(rec, false);
    toast("Reloaded from disk");
  } catch (e) {
    toast("Couldn't reload");
  }
}

async function editorSave() {
  if (!edRec || edReadOnly) return;
  const btn = $("btn-editor-save");
  btn.disabled = true;
  try {
    await edViewSave(edRec);
  } finally {
    btn.disabled = false;
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
  const el = $("screen-editor");
  el.classList.remove("active");
  // Opened over another screen (opts.returnTo), it is that screen that comes
  // back rather than the listing: full screen the listing stays as it was,
  // under that screen, and docked only the pane's own hold on the editor is let
  // go, which brings no listing up anyway.
  const back = edReturn;
  edReturn = null;
  if (!back || el.classList.contains("docked")) undockFileView(el);
  if (back) back.show();
  if (edRec) { edDropView(edRec); edRec = null; }
  edView = null;
  edSetDirty(false);
  edPath = "";
  // A save may have changed what the list shows — sizes, a file born on save —
  // in the listing this file was opened from (filesActive, 28-file-explorer.js).
  const dir = filesActivePath();
  if (dir) loadDir(dir);
}

// Docked, the view pushed no entry, so there is no pop to answer: the question
// about unsaved work is asked here instead and a "stay" is the whole of what
// the press does. False says the editor is staying, which every caller has to
// stand down for.
function editorCloseDocked() {
  if (edDirty && !confirm("Discard your unsaved changes?")) return false;
  closeEditor();
  return true;
}

// The one way out, whichever shape the editor is in: full screen it spends the
// entry it owns and editorPopped answers the pop, docked there is no entry and
// the close happens here. The back arrow and vim's :q share it.
function editorLeave() {
  if ($("screen-editor").classList.contains("docked")) editorCloseDocked();
  else history.back();
}

$("btn-editor-back").addEventListener("click", editorLeave);
$("btn-editor-save").addEventListener("click", () => editorSave());
// The file as it is on the disk, not the buffer: an unsaved edit is not what
// gets saved to the phone. No size to hand over here, so downloadFile() takes
// the browser's way round.
$("btn-editor-download").addEventListener("click", () => {
  if (edPath) downloadFile(edPath, baseName(edPath));
});
// Applies immediately, same as the key-bar toggles in Settings: reconfigure
// the compartment rather than rebuilding the buffer, so the cursor, selection
// and undo history all survive the tap.
$("btn-editor-wrap").addEventListener("click", () => {
  cfg.editorWrapOn = !cfg.editorWrapOn;
  for (const rec of edLive) {
    rec.view.dispatch({ effects: rec.comps.wrap.reconfigure(edWrapExt(rec.path)) });
  }
  edSyncWrapButton();
});
$("btn-editor-vim").addEventListener("click", () => {
  cfg.editorVimOn = !cfg.editorVimOn;
  for (const rec of edLive) {
    rec.view.dispatch({ effects: rec.comps.vim.reconfigure(edVimExt()) });
  }
  edSyncVimButton();
  if (cfg.editorVimOn) { edDefineVimEx(); edLoadVimrc(); }
});
