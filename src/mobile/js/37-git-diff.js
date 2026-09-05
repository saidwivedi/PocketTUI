// ============================================================
// Git diff pane — the changes beside the terminal
// ============================================================
// Ctrl+Shift+G splits the terminal screen: terminal on the left, the git
// changes of the folder the visible pane is sitting in on the right. What it
// answers is the question a session with an agent in it raises every few
// minutes — what has this actually changed — which otherwise costs the
// terminal itself, since asking `git diff` in the pane scrolls away the thing
// you were reading.
//
// Wide layouts only, and that gate is the stylesheet's (see GIT DIFF PANE):
// dropping below the breakpoint stops those rules applying and the split
// disappears without being forgotten, so a window dragged wide again brings it
// back. The markup lives inside #screen-term for the same reason — the
// explorer, the reader and the editor swap .active off that screen, which
// takes the pane down with it and puts it back on return, with no rule here.
//
// The list is VS Code's Source Control split: Changes is the worktree against
// the index, Staged is the index against HEAD, and a file part-staged sits in
// both. Each block of changed lines and each file carries the actions that
// scope earns — stage, revert, unstage, discard — as the tick and cross VS
// Code puts over an inline edit, so the pane is where a change is read and
// sorted, not only where it is looked at. Reverting one block asks nothing,
// the way VS Code asks nothing, and offers the block straight back in the
// pane's toast.
//
// Above those two lists sit two tabs, because the untracked half is a separate
// question with a separate price. Finding what has changed is a walk of the
// files git already knows; finding what it has never seen is a walk of the
// whole tree, and on a network checkout that is the difference between three
// seconds and four minutes. So Tracked and Untracked are two commands, only
// the one on screen is run, and the poll paces itself by how long the last
// answer took — a repo that needs four seconds is asked every sixteen, and a
// local one stays at two.
//
// Every read runs git with --no-optional-locks on the server, so the poll can
// never take the index lock from the user's own git in the terminal beside it;
// the actions take it for the moment `git apply` needs it, which is what
// staging anything costs.

// Narrower than this and a hunk header wraps, which is the point at which the
// diff stops being readable at all.
const DIFF_MIN = 300;
// What the terminal keeps whatever the drag asks for. 320px is roughly 40
// columns at the default size — a shell that is cramped but still a shell.
const DIFF_TERM_MIN = 320;
// Fast enough that a commit made in the terminal shows up before you look
// away, slow enough that a `git status` every two seconds is nothing.
const DIFF_POLL_MS = 2000;
// What a repo that answers slowly is asked instead: four times what its last
// answer cost, so the poll never spends more than a quarter of its time
// waiting on git, and never further apart than a minute — past that the pane
// stops being a live view of the repo beside you.
const DIFF_POLL_FACTOR = 4;
const DIFF_POLL_MAX_MS = 60000;

let diffOpen = false;
let diffWidth = 0;          // 0 until sized — see diffSetOpen()
let diffSession = null;     // whose changes are on screen; null before the first
let diffRoot = "";
let diffScope = "unstaged";  // which of the two lists the selection is in
let diffSelected = "";
let diffTextKey = "";       // the last /diff body, verbatim
let diffFetching = false;   // one poll at a time; a tick lands on this and leaves
let diffNextAt = 0;         // no scheduled poll before this clock time

// Which tab is on screen, and what each of them last heard. The answer is kept
// per tab rather than only compared: a tab coming back on screen has a list to
// draw at once, and waiting for its own fetch to land would blank the pane for
// as long as that repo takes. `key` is the body that list was drawn from, so
// a poll that changed nothing still costs no repaint; `every` is the pace this
// tab's own last answer earned.
let diffTab = cfg.diffTab;
const diffTabs = {
  tracked: { key: "", body: "", data: null, every: DIFF_POLL_MS },
  untracked: { key: "", body: "", data: null, every: DIFF_POLL_MS },
};

function diffTabState() { return diffTabs[diffTab]; }

// A row whose path ends in a slash is a folder the untracked walk collapsed
// into one entry — git says "everything under here is new" rather than listing
// it — so there is no diff to open and the actions act on the whole tree.
function diffIsDir(path) { return path.endsWith("/"); }

// The main pane is the window less the rail and the seam it is drawn on, read
// off the seam itself rather than recomputed from --sidebar-w: the rail's own
// geometry is the truth, and on a phone the handle is display:none and takes
// no width, which reads as "the window" without a branch.
function diffMainW() {
  const r = $("rail-resize").getBoundingClientRect();
  return window.innerWidth - (r.width ? r.right : 0);
}

// Clamped on every read, the way the rail's width is: a stored number is a
// number the window may since have grown or shrunk out from under.
function diffClampW(px) {
  const max = Math.max(DIFF_MIN, diffMainW() - DIFF_TERM_MIN);
  return Math.round(Math.min(max, Math.max(DIFF_MIN, px)));
}

function applyDiffWidth(px) {
  diffWidth = diffClampW(px);
  document.documentElement.style.setProperty("--diff-w", diffWidth + "px");
}

// The one way the split opens or closes: the class, the remembered state, the
// poll and the refit are the same fact, and nothing sets one without the rest.
// The refit is what carries the new width to xterm and on to tmux — the
// terminal has just changed shape, and it only learns that from a fit.
function diffSetOpen(open) {
  diffOpen = open;
  cfg.diffPane = open;
  $("screen-term").classList.toggle("diff-open", open);
  if (open) {
    // Half the main pane the first time, and whatever was dragged after that.
    applyDiffWidth(cfg.diffWidth || Math.round(diffMainW() / 2));
    // The list's height is restored the same way, but only once one has been
    // dragged: with nothing stored the list stays sized by the files in it,
    // which is what the pane has always opened as.
    if (cfg.diffListHeight) applyDiffListH(cfg.diffListHeight);
    diffPoll(true);
  }
  refit(0);
}

function toggleDiffPane() { diffSetOpen(!diffOpen); }

$("btn-diff-close").addEventListener("click", () => diffSetOpen(false));

// Dragging the seam resizes the pane live, the rail's railResize() mirrored:
// the width moves by the pointer's travel rather than jumping to it, the
// variable does the layout for free, and the fit rides its own debounce until
// the release forces one. Anchored to the right edge, so leftward travel is
// a wider pane — hence the sum where the rail takes a difference.
(function diffResize() {
  const handle = $("diff-gutter");
  let dragging = false, dragOff = 0;
  handle.addEventListener("pointerdown", (e) => {
    dragging = true;
    dragOff = diffWidth + e.clientX;
    handle.classList.add("dragging");
    handle.setPointerCapture(e.pointerId);
    e.preventDefault();
  });
  handle.addEventListener("pointermove", (e) => {
    if (!dragging) return;
    applyDiffWidth(dragOff - e.clientX);
    refit();
  });
  const finish = () => {
    if (!dragging) return;
    dragging = false;
    handle.classList.remove("dragging");
    cfg.diffWidth = diffWidth;
    refit(0);
  };
  handle.addEventListener("pointerup", finish);
  handle.addEventListener("pointercancel", finish);
})();

// Three rows of the list at the size the stylesheet sets it in — below that it
// stops reading as a list of files and becomes a strip.
const DIFF_LIST_MIN = 72;
// What the diff keeps whatever the drag asks for: a hunk header and enough
// lines under it to be worth looking at.
const DIFF_BODY_MIN = 120;

let diffListH = 0;          // 0 while the list is sized by its own rows

// The height the list and the diff share, measured off the two of them rather
// than worked out from the pane's padding and header: the safe-area insets and
// whatever the header ended up being are already in those edges.
function diffSplitSpan() {
  return $("diff-body").getBoundingClientRect().bottom -
         $("diff-files").getBoundingClientRect().top -
         $("diff-vsplit").offsetHeight;
}

// Clamped on every read, the way the width is: a remembered height is a height
// the window may since have grown or shrunk out from under.
function diffClampListH(px) {
  const span = diffSplitSpan();
  // Nothing to measure: the pane is off screen, which is where a height
  // restored at boot arrives — the split is put back before the terminal
  // screen is. Taking the measurement anyway would clamp the remembered
  // height to the minimum; the stylesheet holds it in range instead (see
  // .list-sized), and the first drag clamps it against real geometry.
  if (span <= 0) return Math.round(px);
  const max = Math.max(DIFF_LIST_MIN, span - DIFF_BODY_MIN);
  return Math.round(Math.min(max, Math.max(DIFF_LIST_MIN, px)));
}

// The class is what retires the stylesheet's content cap, so it goes on with
// the first height and stays: from here the list is as tall as it was dragged
// to and nothing else.
function applyDiffListH(px) {
  diffListH = diffClampListH(px);
  const pane = $("diff-pane");
  pane.classList.add("list-sized");
  pane.style.setProperty("--diff-list-h", diffListH + "px");
}

// The horizontal seam, diffResize() turned on its side: the list's height moves
// by the pointer's travel rather than jumping to it, and downward travel is a
// taller list — hence the sum where the vertical seam, anchored to the far
// edge, takes a difference. No refit: the terminal beside this has not changed
// shape, only the two boxes on this side of it.
(function diffSplitResize() {
  const handle = $("diff-vsplit");
  let dragging = false, dragOff = 0;
  handle.addEventListener("pointerdown", (e) => {
    dragging = true;
    // Off the element rather than diffListH: until the first drag the list is
    // sized by its rows and diffListH is 0, and what is on screen is what the
    // drag has to start from.
    dragOff = $("diff-files").getBoundingClientRect().height - e.clientY;
    handle.classList.add("dragging");
    handle.setPointerCapture(e.pointerId);
    e.preventDefault();
  });
  handle.addEventListener("pointermove", (e) => {
    if (!dragging) return;
    applyDiffListH(dragOff + e.clientY);
  });
  const finish = () => {
    if (!dragging) return;
    dragging = false;
    handle.classList.remove("dragging");
    cfg.diffListHeight = diffListH;
  };
  handle.addEventListener("pointerup", finish);
  handle.addEventListener("pointercancel", finish);
})();

// ------------------------------------------------------------
// The file list
// ------------------------------------------------------------

// The letter and the colour a porcelain XY code earns in one of the two lists.
// X is the index and Y the worktree, so each list reads its own half: a file
// staged and then edited again is "MM" and says M in both, but one added to the
// index and then deleted from disk is "AD" and truthfully says A above and D
// below. `??` is untracked and shows as U — the two question marks are git's
// shorthand for "I have never seen this", not a state anyone reads as one.
function diffBadge(scope, code) {
  if (code === "??") return { text: "U", kind: "new" };
  const c = (scope === "staged" ? code[0] : code[1]) || "?";
  return {
    text: c,
    kind: c === "A" ? "new" : c === "D" ? "del"
        : c === "R" || c === "C" ? "ren"
        : c === "U" ? "" : "mod",
  };
}

// The actions a file row or a block carries, as icon buttons: a green tick to
// stage, a red cross to revert or discard, a neutral curved arrow to unstage —
// VS Code's inline accept and reject, which is where a reader has already met
// this pair. The verb is the button's title and its aria-label rather than its
// face, so the tooltip still says it and a screen reader still reads it. Dim
// until the thing they belong to is hovered or focused rather than absent
// until then: a button that only exists under a pointer cannot be tabbed to at
// all, and a pane opened by a chord should not then need a mouse.
function diffActs(defs) {
  const box = el("span", { class: "diff-acts" });
  for (const [verb, icon, kind, run] of defs) {
    box.appendChild(el("button", {
      type: "button",
      class: "diff-act " + kind,
      title: verb,
      "aria-label": verb,
      onclick: (e) => { e.stopPropagation(); run(); },
    }, svgIcon(icon)));
  }
  return box;
}

// One row: the badge, then the path with everything but the file's own name
// stepped back, then the row's actions. Deep trees are how a repo actually
// looks, and a column of full paths reads as one grey block. The label is a
// button and the actions are buttons beside it rather than inside it — a
// button within a button is not a thing the DOM should be asked to hold.
function diffFileRow(scope, f) {
  // The last slash that is not the folder row's own trailing one, so a
  // collapsed folder reads as its own name with its parents stepped back
  // rather than as one dim path with nothing on the end of it.
  const cut = f.path.lastIndexOf("/", f.path.length - 2);
  const badge = diffBadge(scope, f.status);
  const label = el("button", {
    type: "button",
    class: "diff-file"
      + (scope === diffScope && f.path === diffSelected ? " selected" : ""),
    title: f.path,
    "data-path": f.path,
    "data-scope": scope,
    onclick: () => diffSelect(scope, f.path),
  }, el("span", { class: "badge " + badge.kind }, badge.text));
  const path = el("span", { class: "path" });
  const inner = el("span", {});
  if (cut >= 0) inner.appendChild(el("span", { class: "dir" }, f.path.slice(0, cut + 1)));
  inner.appendChild(document.createTextNode(f.path.slice(cut + 1)));
  path.appendChild(inner);
  label.appendChild(path);
  return el("div", { class: "diff-row" }, label, scope === "staged"
    ? diffActs([["Unstage", "i-undo", "undo",
        () => diffFileAct("unstage_file", f.path)]])
    : diffActs([["Stage", "i-check", "ok",
        () => diffFileAct("stage_file", f.path)],
        ["Discard", "i-x", "danger", () => diffDiscard(f)]]));
}

// A list with its own eyebrow and count, hidden entirely when it is empty:
// "Staged 0" is a row of furniture in a pane that is mostly diff.
function diffSection(scope, label, files) {
  const sec = el("div", { class: "diff-section" },
    el("div", { class: "diff-eyebrow" },
      el("span", {}, label), el("span", { class: "n" }, String(files.length))));
  for (const f of files) sec.appendChild(diffFileRow(scope, f));
  return sec;
}

function diffNote(text) {
  return el("div", { class: "diff-note" }, text);
}

// A no-op poll must leave the pane exactly as it found it — the same rows in
// the same scroll position, the same selection — so the whole answer is
// compared as text and an unchanged one returns before touching the DOM. The
// comparison is the tab's own: the two tabs are two answers, and the one that
// is not on screen must not be what the one that is gets measured against.
function renderChanges(body, data) {
  const tab = diffTabState();
  if (body === tab.key) return;
  tab.key = body;
  tab.body = body;
  tab.data = data;
  diffRoot = data.root || "";
  const files = data.files || [];
  const name = diffRoot ? diffRoot.slice(diffRoot.lastIndexOf("/") + 1) : "";
  $("diff-root").textContent = name || "Changes";
  $("diff-root").title = diffRoot;

  // The untracked tab is one list: git has never seen any of it, so there is
  // no index half for a row to also be in. It rides in the unstaged slot
  // because that is the scope its diff — the file against nothing — is asked
  // for under.
  const groups = diffTab === "untracked"
    ? { unstaged: files, staged: [] }
    : { unstaged: files.filter((f) => f.unstaged),
        staged: files.filter((f) => f.staged) };
  // A selection is a pair now, and survives a refresh as one: the same file in
  // the same list. A file that has left its list follows itself into the other
  // one where it is there — which is where staging all of it moves it — and
  // otherwise the pane falls to the first row it can find, Changes before
  // Staged, rather than sitting on a diff that is now empty. A collapsed
  // folder is the last thing it falls to: there is nothing to read in one.
  const has = (scope, path) => groups[scope].some((f) => f.path === path);
  const first = (rows) => (rows.find((f) => !diffIsDir(f.path)) || rows[0]).path;
  if (!has(diffScope, diffSelected)) {
    const other = diffScope === "staged" ? "unstaged" : "staged";
    if (diffSelected && has(other, diffSelected)) {
      diffScope = other;
    } else if (groups.unstaged.length) {
      diffScope = "unstaged";
      diffSelected = first(groups.unstaged);
    } else if (groups.staged.length) {
      diffScope = "staged";
      diffSelected = first(groups.staged);
    } else {
      diffSelected = "";
    }
    diffTextKey = "";
  }

  const list = $("diff-files");
  list.textContent = "";
  if (diffTab === "untracked") {
    if (files.length) list.appendChild(diffSection("unstaged", "Untracked", files));
  } else {
    if (groups.unstaged.length) {
      list.appendChild(diffSection("unstaged", "Changes", groups.unstaged));
    }
    if (groups.staged.length) {
      list.appendChild(diffSection("staged", "Staged", groups.staged));
    }
  }

  if (!files.length) {
    diffTextKey = "";
    $("diff-body").textContent = "";
    $("diff-body").appendChild(diffNote(
      data.error ? data.error
        : !diffRoot ? "Open a session to see its changes"
        : diffTab === "untracked" ? "Nothing untracked in " + name
        : "No changes in " + name));
  }
}

// Which list is on screen, and what that costs. Both tabs keep their last
// answer, so coming back to one draws it at once and its own fetch only
// corrects it — a repo that takes seconds to answer must not blank the pane
// for those seconds every time the tabs are touched.
function diffSetTab(tab) {
  if (tab === diffTab) return;
  diffTab = tab;
  cfg.diffTab = tab;
  syncDiffTabs();
  // The selection belongs to the list it was made in, and so does the diff
  // under it: both go rather than being carried into a list they are not in.
  diffScope = "unstaged";
  diffSelected = "";
  diffTextKey = "";
  $("diff-body").textContent = "";
  const st = diffTabState();
  if (st.data) {
    st.key = "";                    // drawn from the cache, not deduped away
    renderChanges(st.body, st.data);
  } else {
    diffShowLoading();
  }
  diffPoll(true);
}

// One writer for the two tabs and the pace beside them: which is pressed, and
// what the header says about how often this repo is being asked.
function syncDiffTabs() {
  for (const name of ["tracked", "untracked"]) {
    const btn = $("btn-diff-" + name);
    btn.setAttribute("aria-pressed", name === diffTab ? "true" : "false");
    btn.classList.toggle("on", name === diffTab);
  }
  const every = diffTabState().every;
  $("diff-every").textContent = every > DIFF_POLL_MS
    ? "refresh " + Math.round(every / 1000) + " s" : "";
}

// What the list shows while it has never had an answer for this repo: the
// first look at a tab, and every look after a session change. Idempotent, so
// the poll can say it every tick without rebuilding the row underneath.
function diffShowLoading() {
  const list = $("diff-files");
  if (list.firstElementChild && list.firstElementChild.classList.contains("loading")) return;
  list.textContent = "";
  list.appendChild(el("div", { class: "diff-note loading" }, "Loading…"));
  $("diff-body").textContent = "";
}

// A click is the one place the pane is ahead of its poll, so it renders its own
// selection immediately rather than waiting up to two seconds to be told.
//
// And it asks for that file's diff and nothing else. The row already carries
// the scope and the path the diff is asked for under, so putting the poll's
// status walk in front of it buys nothing and costs whatever that walk costs —
// on a network checkout ten seconds of an empty body for an answer that was one
// request away. The list is left exactly as it is: the click changed the
// selection, not the files, and the next paced poll is what corrects it.
function diffSelect(scope, path) {
  if (scope === diffScope && path === diffSelected) return;
  diffScope = scope;
  diffSelected = path;
  diffTextKey = "";
  for (const row of $("diff-files").querySelectorAll(".diff-file")) {
    row.classList.toggle("selected",
      row.dataset.scope === scope && row.dataset.path === path);
  }
  $("diff-body").scrollTop = 0;
  if (diffIsDir(path)) diffShowDir(path);
  else diffLoadFile(scope, path);
}

// ------------------------------------------------------------
// The diff
// ------------------------------------------------------------

// Which of the six kinds of line this is, and what the gutter shows beside it.
// Order matters: `---` and `+++` are file headers, and testing them after the
// bare `-` and `+` would paint them as a deletion and an addition.
function diffLineClass(line) {
  if (line.startsWith("@@")) return "hunk";
  if (line.startsWith("---") || line.startsWith("+++")
      || line.startsWith("diff ") || line.startsWith("index ")
      || line.startsWith("old mode") || line.startsWith("new mode")
      || line.startsWith("new file") || line.startsWith("deleted file")
      || line.startsWith("similarity index") || line.startsWith("dissimilarity index")
      || line.startsWith("rename ") || line.startsWith("copy ")
      || line.startsWith("\\ ")) return "meta";
  if (line.startsWith("+")) return "add";
  if (line.startsWith("-")) return "del";
  return "ctx";
}

// Inside a hunk the first character is the whole line's story — `+`, `-` or a
// space — so the patch logic reads it directly rather than through
// diffLineClass(), which is answering the other question, how to paint the
// line, and calls a removed line that itself begins `--` a file header.
function diffMark(line) {
  return line[0] === "+" || line[0] === "-" ? line[0] : " ";
}

// A block is a maximal run of removed and added lines inside a hunk — what a
// reader means by "this change", and the unit VS Code stages when it offers to
// stage one. Context lines are what separates two of them. Each entry is the
// inclusive span of the hunk's own line array, index 0 being its `@@`.
function diffBlocks(hunk) {
  const blocks = [];
  let run = null;
  for (let i = 1; i < hunk.lines.length; i++) {
    if (diffMark(hunk.lines[i]) === " ") { run = null; continue; }
    if (run) run.to = i;
    else blocks.push(run = { from: i, to: i });
  }
  return blocks;
}

// The one-block patch a block action sends: the header block git printed
// before its first `@@` — `diff --git`, `index`, `---`/`+++` and whatever mode
// or rename lines the change earned — then this hunk with every other block in
// it flattened into the state the action reads from.
//
// Which state that is, is the direction. Staging applies forward to the index,
// where the other blocks' removals are still present and their additions are
// not, so a `-` there becomes context and a `+` goes. Reverting and unstaging
// apply in reverse, against the new side, so it is the other way round. A hunk
// with one block in it comes out as the whole hunk, which is what it was.
//
// The `@@` counts are recounted from what survives. The server applies with
// --recount and would not read them, but a patch that says what it contains is
// one that can be pasted into `git apply` by hand and still work.
function diffBlockPatch(header, hunk, block, reverse) {
  const out = [];
  let a = 0, b = 0;             // the two sides' line counts, as they end up
  let dropped = false;          // whether the line a `\` note belongs to went
  for (let i = 1; i < hunk.lines.length; i++) {
    const line = hunk.lines[i];
    // `\ No newline at end of file` is not a line of either side but a note
    // about the one above it, so it follows that line's fate and counts for
    // neither.
    if (line.startsWith("\\")) { if (!dropped) out.push(line); continue; }
    const mark = diffMark(line);
    let text = line;
    if (mark !== " " && (i < block.from || i > block.to)) {
      if (mark === (reverse ? "+" : "-")) text = " " + line.slice(1);
      else { dropped = true; continue; }
    }
    dropped = false;
    out.push(text);
    if (diffMark(text) !== "+") a++;
    if (diffMark(text) !== "-") b++;
  }
  const m = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@(.*)$/.exec(hunk.lines[0]);
  const at = m ? "@@ -" + m[1] + "," + a + " +" + m[2] + "," + b + " @@" + m[3]
               : hunk.lines[0];
  return header.concat([at], out).join("\n") + "\n";
}

// The actions one block earns in the list it is in, floated over its first
// line by the stylesheet. Suppressed on a truncated diff: its last hunk is cut
// off mid-way, and --recount would happily apply the half of it that arrived.
function diffBlockActs(scope, path, header, hunk, block) {
  return scope === "staged"
    ? diffActs([["Unstage", "i-undo", "undo",
        () => diffBlockAct("unstage_hunk", path, header, hunk, block, true)]])
    : diffActs([["Stage", "i-check", "ok",
        () => diffBlockAct("stage_hunk", path, header, hunk, block, false)],
        ["Revert", "i-x", "danger",
          () => diffRevertBlock(path, header, hunk, block)]]);
}

// One row of the diff: the two gutter numbers, then the text. A blank context
// line comes over as "" rather than " "; without a character the row would
// collapse to nothing at all.
function diffLineEl(line, a, b) {
  return el("div", { class: "diff-line " + diffLineClass(line) },
    el("span", { class: "nos" }, el("span", {}, a), el("span", {}, b)),
    el("span", { class: "tx" }, line || " "));
}

// The left gutter's numbers come from the hunk headers and nothing else: a
// `@@ -12,7 +12,9 @@` says where both sides resume, and each line after it
// advances the side or sides it belongs to. A file with no hunks — a pure
// rename, a mode change — never sets them, and its header lines get blanks.
//
// The text is split into the preamble and its hunks before a row is drawn,
// because the actions belong to blocks and a block's patch is built from the
// whole hunk around it: the hunk has to exist as a thing before the row that
// acts on it can be made. Each block's own lines are then wrapped in a box of
// their own, which is what gives the group a corner to sit in and a run of
// lines to be hovered over.
function renderDiff(data) {
  const body = $("diff-body");
  body.textContent = "";
  if (data.binary) { body.appendChild(diffNote("Binary file")); return; }
  if (data.error) { body.appendChild(diffNote(data.error)); return; }
  const text = data.diff || "";
  if (!text) { body.appendChild(diffNote("No diff for this file")); return; }

  const frag = document.createDocumentFragment();
  const header = [];
  const hunks = [];
  const path = diffSelected, scope = diffScope;
  for (const line of text.replace(/\n$/, "").split("\n")) {
    if (line.startsWith("@@")) hunks.push({ lines: [line] });
    else if (hunks.length) hunks[hunks.length - 1].lines.push(line);
    else header.push(line);
  }

  for (const line of header) frag.appendChild(diffLineEl(line, "", ""));
  for (const hunk of hunks) {
    let oldNo = 0, newNo = 0;
    const m = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(hunk.lines[0]);
    if (m) { oldNo = +m[1]; newNo = +m[2]; }
    frag.appendChild(diffLineEl(hunk.lines[0], "", ""));
    const blocks = data.truncated ? [] : diffBlocks(hunk);
    let bi = 0, box = null;   // the block being filled, or null between blocks
    for (let i = 1; i < hunk.lines.length; i++) {
      const line = hunk.lines[i];
      const block = blocks[bi];
      if (block && i === block.from) {
        box = el("div", { class: "diff-block" },
          diffBlockActs(scope, path, header, hunk, block));
        frag.appendChild(box);
      }
      const kind = diffLineClass(line);
      let a = "", b = "";
      if (kind === "add") {
        b = newNo ? String(newNo++) : "";
      } else if (kind === "del") {
        a = oldNo ? String(oldNo++) : "";
      } else if (kind === "ctx") {
        a = oldNo ? String(oldNo++) : "";
        b = newNo ? String(newNo++) : "";
      }
      (box || frag).appendChild(diffLineEl(line, a, b));
      if (block && i === block.to) { box = null; bi++; }
    }
  }
  body.appendChild(frag);
  if (data.truncated) {
    body.appendChild(el("div", { class: "diff-notice" },
      "Diff truncated at 1 MB — the rest is in the terminal."));
  }
}

// ------------------------------------------------------------
// The actions
// ------------------------------------------------------------

// The pane's own toast rather than the app's: this one carries a button, and
// the app's is a line of text at the foot of the window, which is the wrong
// side of the screen for an Undo that belongs to the pane on the right. Six
// seconds is long enough to read the line and reach the button, and short
// enough to be gone before the next action.
const DIFF_TOAST_MS = 6000;
let diffToastTimer = 0;

function diffToast(text, actionLabel, run) {
  const box = $("diff-toast");
  box.textContent = "";
  box.appendChild(el("span", { class: "msg" }, text));
  if (actionLabel) {
    box.appendChild(el("span", { class: "sep" }, "·"));
    box.appendChild(el("button", {
      type: "button",
      class: "diff-act",
      onclick: () => { diffToastHide(); run(); },
    }, actionLabel));
  }
  box.classList.add("on");
  clearTimeout(diffToastTimer);
  diffToastTimer = setTimeout(diffToastHide, DIFF_TOAST_MS);
}

function diffToastHide() {
  clearTimeout(diffToastTimer);
  $("diff-toast").classList.remove("on");
}

// Every action ends the same way whether it worked or not: the pane is now
// behind the repo, so both comparison keys go and the poll runs at once rather
// than showing a stale list for up to two seconds. A refusal is git's own
// sentence — the file moved under the pane — and the re-poll behind the toast
// is what shows the state it moved to.
async function diffApply(payload) {
  try {
    const r = await fetch(apiURL("api/git/apply"), {
      method: "POST",
      cache: "no-store",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(Object.assign({ root: diffRoot }, payload)),
    });
    if (r.status === 401) { rejectToken(); return false; }
    if (!r.ok) {
      const data = await r.json().catch(() => null);
      diffToast((data && data.detail) || "git refused that change");
      return false;
    }
    return true;
  } catch (e) {
    diffToast("Could not reach the server");
    return false;
  } finally {
    // The pane is now behind the repo on both counts: the list it drew and the
    // diff under it are answers to a question that has since changed.
    diffTabState().key = "";
    diffTextKey = "";
    diffPoll(true);
  }
}

function diffBlockAct(action, path, header, hunk, block, reverse) {
  return diffApply({ path: path, action: action,
                     patch: diffBlockPatch(header, hunk, block, reverse) });
}

function diffFileAct(action, path) {
  return diffApply({ path: path, action: action });
}

// VS Code asks nothing before reverting one block, and neither does this. What
// stands in for the question is the patch itself: it is kept here and the toast
// offers it straight back, which is a truer undo than a confirmation is a
// safeguard — the block returns exactly as it was rather than being retyped.
async function diffRevertBlock(path, header, hunk, block) {
  const patch = diffBlockPatch(header, hunk, block, true);
  if (!await diffApply({ path: path, action: "revert_hunk", patch: patch })) return;
  diffToast("Reverted 1 block", "Undo",
    () => diffApply({ path: path, action: "apply_hunk", patch: patch }));
}

// The one action that asks first, because it is the one with nothing to undo:
// a tracked file's worktree goes back to the index and an untracked file is
// deleted outright, which the question says in as many words.
async function diffDiscard(f) {
  const gone = f.status === "??";
  const ok = await appConfirm(diffIsDir(f.path)
    ? "Discard " + f.path + "? It is untracked, so discarding deletes the "
      + "folder and everything in it."
    : gone
    ? "Discard " + f.path + "? It is untracked, so discarding deletes the file."
    : "Discard your changes to " + f.path + "? They cannot be brought back.",
    { confirmLabel: "Discard" });
  if (ok) await diffApply({ path: f.path, action: "discard_file" });
}

// ------------------------------------------------------------
// The poll
// ------------------------------------------------------------

// Counted rather than set, because a click's diff does not wait for the poll:
// the two overlap, and a boolean would have whichever finished first put the
// dot out while the other was still in flight.
let diffBusyN = 0;
function diffBusy(on) {
  diffBusyN = Math.max(0, diffBusyN + (on ? 1 : -1));
  $("diff-busy").classList.toggle("on", diffBusyN > 0);
}

async function diffFetchJSON(url) {
  const r = await fetch(apiURL(url), { cache: "no-store", headers: authHeaders() });
  if (r.status === 401) { rejectToken(); return null; }
  const data = await r.json().catch(() => null);
  if (!r.ok || !data) return null;
  return { body: JSON.stringify(data), data: data };
}

// A folder the untracked walk collapsed is not a file and has no diff: what the
// body carries is why, and the row's own actions are what acts on it.
function diffShowDir(path) {
  if (diffTextKey === "dir " + path) return;
  diffTextKey = "dir " + path;
  $("diff-body").textContent = "";
  $("diff-body").appendChild(
    diffNote("Untracked directory — stage or discard it whole."));
}

// One file's diff, fetched and drawn. The pair it is for is passed rather than
// read back off the globals when the answer lands: this is asked by the poll
// and by a click, either can be out while the other starts, and an answer that
// arrives for a row the user has since clicked away from would paint one file's
// text under another file's highlight. The number is the same guard for two
// answers for the same row landing out of order — only the last request asked
// for may draw.
let diffSeq = 0;
async function diffLoadFile(scope, path) {
  const seq = ++diffSeq;
  diffBusy(true);
  try {
    const diff = await diffFetchJSON("api/git/diff?root="
      + encodeURIComponent(diffRoot) + "&path=" + encodeURIComponent(path)
      + "&scope=" + scope);
    if (!diff || seq !== diffSeq) return;
    if (scope !== diffScope || path !== diffSelected) return;
    if (diff.body === diffTextKey) return;
    diffTextKey = diff.body;
    // A file being edited in the terminal changes under a reader who is partway
    // down it; keeping the offset is what lets them keep reading.
    const keep = $("diff-body").scrollTop;
    renderDiff(diff.data);
    $("diff-body").scrollTop = keep;
  } catch (e) {
    // Quiet, like the poll it shares: an unreachable backend already nags from
    // the terminal's banner.
  } finally {
    diffBusy(false);
  }
}

// One pass: the list of the tab on screen, then the selected file's diff. Both
// bodies are compared as text against the last pass, so a session that has not
// changed costs two requests and no repaint — the scroll position and the
// selection are what this is protecting, and both would be lost by a rebuild
// that changed nothing.
//
// `now` is what the pane's own moves pass: a tab pressed, an action applied and
// a tab coming back all change which files there are and are ahead of the
// schedule, so they must not wait for it. The ticks themselves pass nothing and
// are declined until the interval the last answer earned has gone by. A click
// is not among them — it asks for its own diff and leaves the list alone.
async function diffPoll(now) {
  if (diffFetching) return;
  if (!diffOpen || document.hidden || !isWideLayout()) return;
  if (demoMode || needsSetup() || !hasCap("git")) return;
  if (!$("screen-term").classList.contains("active")) return;
  if (!now && Date.now() < diffNextAt) return;

  const session = currentSession || "";
  // A different session is a different repo: nothing on screen belongs to it,
  // so the selection and every tab's cached answer go rather than being
  // reconciled, and the list says it is looking rather than showing the last
  // repo's files until this one answers.
  if (session !== diffSession) {
    diffSession = session;
    diffRoot = "";
    diffScope = "unstaged";
    diffSelected = "";
    diffTextKey = "";
    for (const st of Object.values(diffTabs)) {
      st.key = ""; st.body = ""; st.data = null; st.every = DIFF_POLL_MS;
    }
    syncDiffTabs();
  }
  if (!diffTabState().data) diffShowLoading();

  diffFetching = true;
  diffBusy(true);
  try {
    const changes = await diffFetchJSON("api/git/changes?session="
      + encodeURIComponent(session) + "&dev=" + encodeURIComponent(cfg.devname)
      + "&scope=" + diffTab);
    if (!changes) return;
    diffPace(changes.data.elapsed_ms);
    renderChanges(changes.body, changes.data);
    if (!diffRoot || !diffSelected) return;
    if (diffIsDir(diffSelected)) { diffShowDir(diffSelected); return; }
    await diffLoadFile(diffScope, diffSelected);
  } catch (e) {
    // Quiet, like the rail's own refresh: an unreachable backend already nags
    // from the terminal's banner, and once per two seconds is not a toast.
  } finally {
    diffFetching = false;
    diffBusy(false);
    diffNextAt = Date.now() + diffTabState().every;
  }
}

// How long until this tab is asked again, from how long its last answer took.
// A repo on a network mount charges seconds for a status, and asking it every
// two would mean a git that is always running and never finishing; four times
// the last answer keeps the pane current without the repo ever being the
// bottleneck. Shown in the header once it is no longer the plain two seconds,
// because a list refreshing every fifteen should say so rather than look
// stuck.
function diffPace(elapsedMs) {
  const st = diffTabState();
  const ms = Number(elapsedMs) || 0;
  st.every = Math.min(DIFF_POLL_MAX_MS,
                      Math.max(DIFF_POLL_MS, Math.round(ms * DIFF_POLL_FACTOR)));
  syncDiffTabs();
}

$("btn-diff-tracked").addEventListener("click", () => diffSetTab("tracked"));
$("btn-diff-untracked").addEventListener("click", () => diffSetTab("untracked"));

// A server older than these routes serves 404s to every one of them, so the
// pane says what to do about that instead of polling a computer that cannot
// answer, and the rail stops offering a chord that would open it. Called when
// the capability map lands — before that hasCap() answers yes, which is the
// map's own contract for a server too old to send one.
function syncGitCap() {
  const on = hasCap("git");
  const row = $("rk-diff");
  if (row) row.hidden = !on;
  if (on) return;
  $("diff-files").textContent = "";
  $("diff-body").textContent = "";
  $("diff-body").appendChild(diffNote(
    "This computer's pockettui is older than the app. "
    + "Run `pockettui update` there."));
}

// The ticks run forever and decline; diffPoll()'s own gates are the switch, so
// a pane closed, hidden, phone-width or covered by the explorer costs nothing.
setInterval(diffPoll, DIFF_POLL_MS);
// A tab coming back has been stale for as long as it was away, and the tick it
// would otherwise wait for is up to two seconds of showing that stale answer.
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) diffPoll(true);
});

// The tab the last session was left on, drawn before anything is fetched so
// the pane never opens with neither button pressed.
syncDiffTabs();

// Restored before anything opens, so a reload that lands straight in a terminal
// comes up split exactly as it was left. The poll declines until there is a
// terminal and a token to ask with.
if (cfg.diffPane) diffSetOpen(true);
