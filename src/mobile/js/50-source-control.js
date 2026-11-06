// ============================================================
// Source control — a branch's commits and what each one changed
// ============================================================
// The explorer's branch picker reads a tree at a ref; this is the other half of
// that question, which commits put the tree there. A list of the branch's
// commits, newest first, each one opening in place to the files it touched, and
// a file opening to the diff that commit made to it, painted by the diff pane's
// own renderer (diffRender, 37-git-diff.js) with no actions: a commit is
// history, and there is nothing in it to stage or revert.
//
// The pieces are handed a ctx by whichever host shows them — today the explorer
// pane's History key (28-file-explorer.js), which swaps its listing for the list
// — so nothing here knows which pane it is in or how that pane goes back.
// ctx: {root() -> the repo's top level, openCommitDiff(info), back?()}.
//
// Nothing runs while the bundle loads: everything below is a function, and the
// one server gate is called by 36-server-version.js once the caps are known.

// One GET against the backend, answered as {ok, status, data}. The history
// wants git's own sentence on a failure (a bad ref, a repo that went away), so
// unlike diffFetchJSON a refusal still carries its body.
async function scGet(path, params) {
  const qs = Object.entries(params)
    .filter(([, v]) => v !== undefined && v !== null && v !== "")
    .map(([k, v]) => k + "=" + encodeURIComponent(v)).join("&");
  try {
    const r = await fetch(apiURL(path + "?" + qs), { cache: "no-store", headers: authHeaders() });
    if (r.status === 401) { rejectToken(); return { ok: false, status: 401, data: null }; }
    const data = await r.json().catch(() => null);
    return { ok: r.ok && !!data, status: r.status, data: data };
  } catch (e) {
    return { ok: false, status: 0, data: null };
  }
}

// The same answer for a POST with a JSON body: the commit route's refusals
// are git's own words (and a hook's), which the toast passes on.
async function scPost(path, body) {
  try {
    const r = await fetch(apiURL(path), {
      method: "POST", cache: "no-store",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(body),
    });
    if (r.status === 401) { rejectToken(); return { ok: false, status: 401, data: null }; }
    const data = await r.json().catch(() => null);
    return { ok: r.ok && !!data, status: r.status, data: data };
  } catch (e) {
    return { ok: false, status: 0, data: null };
  }
}

// A commit's file status as the diff pane's letter and colour. diffBadge reads
// porcelain XY codes, and a commit's one-letter status is the X half of one, so
// it is asked as the staged side; T (a mode or type change) has no letter of
// its own in the pane and reads as a modification.
function scBadge(status) {
  const s = status === "T" ? "M" : (status || "M");
  const b = diffBadge("staged", s + " ");
  return el("span", { class: "sc-badge " + b.kind, "aria-label": scStatusWord(status) }, b.text);
}

function scStatusWord(status) {
  return { A: "added", D: "deleted", R: "renamed", C: "copied", T: "type changed" }[status]
    || "modified";
}

// A path as the explorer's rows show one: the folders dim, the name in full
// ink. A rename says where it came from, so the row is the whole move.
function scPathEl(path, old) {
  // An untracked folder comes as "dir/" and reads as its own name, slash kept.
  const part = (p) => {
    const cut = p.replace(/\/$/, "").lastIndexOf("/") + 1;
    return [el("span", { class: "sc-dir" }, p.slice(0, cut)), p.slice(cut)];
  };
  const kids = old ? [...part(old), el("span", { class: "sc-arrow" }, " → "), ...part(path)]
                   : part(path);
  return el("span", { class: "sc-path", title: old ? old + " → " + path : path }, ...kids);
}

// git's decoration list, as the pills a row wears, coloured by kind the way a
// coloured `git log --decorate` is: the branch HEAD is on filled in the accent
// ("HEAD -> main" reads as main; a detached HEAD stays as itself), other local
// branches green, remote-tracking ones blue, tags amber without their "tag: ".
// %D spells a remote branch as "origin/main", which a local branch can also
// be called, so a name counts as remote when its first part is a remote: the
// usual two, or one whose HEAD the same list shows ("origin/HEAD").
function scRefPills(refs) {
  const names = [];
  for (const raw of refs || []) {
    for (const name of String(raw).split(", ")) if (name.trim()) names.push(name.trim());
  }
  const remotes = new Set(["origin", "upstream"]);
  for (const n of names) if (/^[^/\s]+\/HEAD$/.test(n)) remotes.add(n.split("/")[0]);
  return names.map((name) => {
    let kind = "branch";
    if (name.startsWith("HEAD -> ")) { name = name.slice(8); kind = "head"; }
    else if (name === "HEAD") kind = "head";
    else if (name.startsWith("tag: ")) { name = name.slice(5); kind = "tag"; }
    else if (name.includes("/") && remotes.has(name.split("/")[0])) kind = "remote";
    return el("span", { class: "sc-pill " + kind, title: name }, name);
  });
}

// ---- the commit list ---------------------------------------------------------
// One page at a time, PAGE commits a page, and a Load more row while the
// server says there is more. Every load names the root and ref it was asked
// for and carries a sequence number: a branch picked while the previous one is
// still answering must not have the older list painted over it.

function scHistory(ctx) {
  const PAGE = 50;
  const list = el("div", { class: "sc-list" });
  const foot = el("div", { class: "sc-foot" });
  const box = el("div", { class: "sc-history" }, list, foot);
  let ref = "HEAD";
  let count = 0;           // commits drawn, which is where the next page skips to
  let more = false;
  let seq = 0;
  let busy = false;
  // A commit's files are the same answer every time it is opened, so each is
  // asked once per repo; the promise is kept, so a second tap while the first
  // is in flight waits on the same request.
  const records = new Map();

  function note(text) { foot.textContent = ""; foot.appendChild(diffNote(text)); }

  function paintFoot(errorText) {
    foot.textContent = "";
    if (errorText) { note(errorText); return; }
    if (busy) { note(count ? "Loading…" : "Loading commits…"); return; }
    if (more) {
      foot.appendChild(el("button", {
        type: "button", class: "sc-more", onclick: () => api.more(),
      }, "Load more"));
    }
  }

  async function fetchPage() {
    const mine = ++seq;
    const root = ctx.root();
    busy = true;
    paintFoot();
    const r = await scGet("api/git/log", { root: root, ref: ref, skip: count, n: PAGE });
    if (mine !== seq) return;
    busy = false;
    const d = r.data;
    if (!r.ok) {
      paintFoot((d && (d.detail || d.error)) || "Couldn't read the history");
      return;
    }
    if (d.error) { paintFoot(d.error); return; }
    const commits = d.commits || [];
    if (!count && !commits.length) {
      paintFoot(d.unborn ? "No commits yet" : "No commits");
      return;
    }
    commits.forEach((c, i) => list.appendChild(commitEl(c, root, !count && !i)));
    count += commits.length;
    more = !!d.more;
    paintFoot();
  }

  // The row's left edge is the graph of the branch being walked: one rail in
  // the accent through every commit, a ring on each, filled on the tip; the
  // rail starts at the tip and stops at a root commit. One line, not lanes: a
  // merge's other parents are not drawn.
  function commitEl(c, root, tip) {
    const files = el("div", { class: "sc-files", hidden: "" });
    const row = el("button", {
      type: "button", class: "sc-row", "aria-expanded": "false",
      title: c.subject + (c.body ? "\n\n" + c.body.trim() : ""),
    },
      el("span", { class: "sc-line" },
        el("span", { class: "sc-subj" }, c.subject || "(no message)"),
        ...scRefPills(c.refs)),
      el("span", { class: "sc-sub" },
        el("span", { class: "sc-author" }, c.author || ""),
        el("span", { class: "sc-dot" }, " · "),
        el("span", { class: "sc-time" }, c.time ? relTime(c.time) : ""),
        el("span", { class: "sc-dot" }, " · "),
        el("span", { class: "sc-sha" }, c.short || String(c.sha).slice(0, 7))));
    row.addEventListener("click", () => {
      const open = files.hidden;
      files.hidden = !open;
      row.setAttribute("aria-expanded", open ? "true" : "false");
      row.parentNode.classList.toggle("open", open);
      if (open && !files.firstChild) fillFiles(files, c, root);
    });
    const isRoot = Array.isArray(c.parents) && !c.parents.length;
    return el("div", { class: "sc-commit" + (tip ? " tip" : "") + (isRoot ? " root" : "") },
      el("span", { class: "sc-graph", "aria-hidden": "true" }), row, files);
  }

  async function fillFiles(files, c, root) {
    files.appendChild(diffNote("Loading…"));
    const key = root + "\n" + c.sha;
    if (!records.has(key)) {
      records.set(key, scGet("api/git/commit", { root: root, sha: c.sha }));
    }
    const r = await records.get(key);
    files.textContent = "";
    if (!r.ok || !r.data || r.data.error) {
      records.delete(key);         // a failure is worth asking again next time
      files.appendChild(diffNote((r.data && (r.data.detail || r.data.error))
                                 || "Couldn't read this commit"));
      return;
    }
    const rec = r.data;
    // A merge is shown against its first parent, which is the branch it was
    // merged into: what the merge brought in, not what it reconciled.
    if (rec.merge) files.appendChild(el("div", { class: "sc-files-note" },
                                        "Merge — changes against the first parent"));
    if (!(rec.files || []).length) {
      files.appendChild(diffNote("No file changes"));
      return;
    }
    for (const f of rec.files) files.appendChild(fileEl(f, c));
  }

  function fileEl(f, c) {
    const counts = f.binary
      ? el("span", { class: "sc-count" }, el("span", { class: "sc-bin" }, "binary"))
      : el("span", { class: "sc-count" },
          f.adds ? el("span", { class: "add" }, "+" + f.adds) : null,
          f.dels ? el("span", { class: "del" }, "−" + f.dels) : null);
    return el("button", {
      type: "button", class: "sc-file",
      onclick: () => ctx.openCommitDiff({
        sha: c.sha, short: c.short, subject: c.subject,
        path: f.path, old: f.old || "", status: f.status,
      }),
    }, scBadge(f.status), scPathEl(f.path, f.old), counts);
  }

  function reset() {
    seq++;
    busy = false;
    count = 0;
    more = false;
    list.textContent = "";
    foot.textContent = "";
  }

  const api = {
    el: box,
    get ref() { return ref; },
    load(r) {
      reset();
      ref = r || "HEAD";
      // Files are a commit's, whatever branch reached it, but the root can
      // differ from one load to the next; the key carries it.
      return fetchPage();
    },
    more() { if (!busy && more) return fetchPage(); },
    reload() { return api.load(ref); },
    destroy() { reset(); records.clear(); box.remove(); },
  };
  return api;
}

// ---- one file's diff in one commit -------------------------------------------
// The diff pane's rendering with h = null, which draws the blocks without
// their actions. `info` is what the list's row knew: sha, short, subject,
// path, old (for a rename) and status.
//
// The workspace's Staged list opens the same view with `info.scope` "staged":
// what is in the index for the file, asked as the diff pane asks it, and drawn
// with the actions that list earns (Unstage on each block), sent through
// ctx.h. A staged file's question is "what did I stage", which is a diff.

function scCommitDiffView(ctx, info) {
  const staged = info.scope === "staged";
  const body = el("div", { class: "sc-diff-body" });
  const head = el("div", { class: "sc-diff-head" },
    ctx.back ? el("button", {
      type: "button", class: "icon-btn sc-back",
      "aria-label": "Back to the commits", title: "Back to the commits",
      onclick: () => ctx.back(),
    }, svgIcon("i-m-chev-left")) : null,
    scBadge(info.status),
    el("span", { class: "sc-diff-what" },
      scPathEl(info.path, info.old),
      staged
        ? el("span", { class: "sc-sub" }, "Staged changes")
        : el("span", { class: "sc-sub" },
            el("span", { class: "sc-sha" }, info.short || String(info.sha).slice(0, 7)),
            el("span", { class: "sc-dot" }, " · "),
            el("span", { class: "sc-diff-subj" }, info.subject || ""))));
  const box = el("div", { class: "sc-diff" }, head, body);
  let seq = 0;

  // `quiet` is a reload under a diff already drawn (after an unstage, or a
  // status poll that saw the file move): no "Loading…" in between, so the
  // view and its scroll stay put while the answer is out.
  async function load(quiet) {
    const mine = ++seq;
    if (!quiet || !body.firstChild) {
      body.textContent = "";
      body.appendChild(diffNote("Loading…"));
    }
    const r = await scGet("api/git/diff", staged
      ? { root: ctx.root(), path: info.path, scope: "staged" }
      : { root: ctx.root(), path: info.path, sha: info.sha, old: info.old || "" });
    if (mine !== seq) return;
    const data = r.ok ? r.data
      : { diff: "", error: (r.data && (r.data.detail || r.data.error)) || "Couldn't read the diff" };
    if (staged) diffRender(body, data, "staged", info.path, ctx.h);
    else diffRender(body, data, "commit", info.path, null);
  }

  return { el: box, info: info, load: load };
}

// ---- the workspace's Source control view ---------------------------------------
// VS Code's panel of the same name, in the expanded explorer's left column
// (47-workspace.js swaps it with the tree): a message box and Commit on top,
// then what is staged, what changed and what git has never seen, each file
// with the diff pane's own letter and actions. The lists are the workspace's
// status poll drawn again (paint), so nothing here asks git on a timer.
//
// ctx: {root(), ref(), branch(), apply(payload) -> Promise<bool>,
// toast(text, ms), kickStatus(), openFile(rel, opts), openStagedDiff(f),
// openFolder(rel), openCommitDiff(info), commit(message) -> scPost's answer}.

// One changed file: the letter, the path with its folders dim, and the
// actions its list earns (diffFileActs: Unstage on the staged side, Stage and
// Discard on the other). The label and the actions are siblings rather than a
// button holding buttons, the diff pane's row (diffFileRow) again.
function scFileRow(f, scope, h, onOpen) {
  const b = diffBadge(scope, f.status);
  const label = el("button", { type: "button", class: "sc-frow-open" },
    el("span", { class: "sc-badge " + b.kind }, b.text), scPathEl(f.path));
  label.addEventListener("click", () => onOpen(f));
  return el("div", { class: "sc-frow" }, label, h ? diffFileActs(scope, f, h) : null);
}

// What a refused commit says, in a line or two: git's own words (and a hook's)
// when it gave any. A timeout is the one where git's words are not the story:
// the process was stopped while a hook may still have been doing something.
function scCommitError(r) {
  const d = r.data || {};
  if (d.error === "timeout") {
    return "The commit took too long and was stopped; a hook may have half-run";
  }
  // The first line, and git's own fatal or error line when there is one: the
  // identity refusal's second line is a bare "Run" ahead of the commands to.
  const lines = String(d.detail || "").split("\n").map((s) => s.trim()).filter(Boolean);
  const fatal = lines.find((s) => /^(fatal|error):/.test(s));
  const said = (fatal && fatal !== lines[0] ? [lines[0], fatal] : lines.slice(0, 2)).join(" ");
  if (said) return said;
  return {
    nothing_to_commit: "Nothing to commit",
    identity: "git doesn't know who you are",
    busy: "Another git command is using the repository",
    empty_message: "Write a message first",
    bad_root: "Not a git repository",
  }[d.error] || (r.status ? "The commit failed" : "Could not reach the server");
}

function scPanel(ctx) {
  const msg = el("textarea", {
    class: "sc-msg", rows: "1", placeholder: "Message",
    "aria-label": "Commit message", spellcheck: "true",
  });
  const key = el("button", { type: "button", class: "sc-commit-key",
                             title: "Commit (Ctrl+Enter)" }, "Commit");
  const compose = el("div", { class: "sc-compose" }, msg, key);
  // Untracked is folded by default: a build's output or a scratch folder can
  // be hundreds of rows, and nothing in them is waiting on a decision.
  const sections = {
    staged: scSection("Staged", false),
    changes: scSection("Changes", false),
    untracked: scSection("Untracked", true),
  };
  const note = el("div", { class: "sc-note" });
  const lists = el("div", { class: "sc-lists" },
    sections.staged.el, sections.changes.el, sections.untracked.el);
  // The branch's commits under the lists, scHistory as the explorer's History
  // key shows it, without a back key: a file opens as a read-only diff tab
  // (ctx.openCommitDiff). It is not a scroller of its own; the column scrolls
  // the box, the lists and the history as one, so a long history never leaves
  // a short scroll box under a short list.
  const history = scHistory({ root: ctx.root, openCommitDiff: ctx.openCommitDiff });
  const histRef = el("span", { class: "sc-hist-ref" });
  const histSec = el("section", { class: "sc-hist" },
    el("div", { class: "sc-eyebrow sc-hist-head" }, el("span", {}, "History"), histRef),
    history.el);
  let histKey = "";
  const box = el("div", { class: "sc-panel" }, compose, note, lists, histSec);

  let files = new Map();
  let known = false;       // a status answer has been painted
  let busy = false;        // a commit is out
  // Rows by list and path, with what they were drawn from: a poll that brings
  // the same row back keeps its element, so the lists do not flash, a row
  // under the pointer keeps its hover, and the column keeps its scroll.
  const rows = new Map();

  const h = {
    apply: (p) => ctx.apply(p),
    toast: (text) => ctx.toast(text),
  };

  function scSection(title, folded) {
    const count = el("span", { class: "n" });
    const body = el("div", { class: "sc-sec-body" });
    let head;
    if (folded) {
      head = el("button", { type: "button", class: "sc-eyebrow sc-fold",
                            "aria-expanded": "false" },
        svgIcon("i-m-chev-right"), el("span", {}, title), count);
      body.hidden = true;
      head.addEventListener("click", () => {
        body.hidden = !body.hidden;
        head.setAttribute("aria-expanded", body.hidden ? "false" : "true");
      });
    } else {
      head = el("div", { class: "sc-eyebrow" }, el("span", {}, title), count);
    }
    return { el: el("section", { class: "sc-sec" }, head, body), body, count };
  }

  function syncKey() {
    key.disabled = !msg.value.trim() || busy || !ctx.root() || !!ctx.ref()
                   || !hasCapStrict("git_commit");
  }

  // One row to six, then the box scrolls.
  function grow() {
    msg.style.height = "auto";
    msg.style.height = msg.scrollHeight + "px";
  }

  async function commit() {
    syncKey();
    if (key.disabled) return;
    busy = true;
    syncKey();
    const r = await ctx.commit(msg.value);
    busy = false;
    if (r.ok && r.data.ok) {
      msg.value = "";
      grow();
      ctx.toast("Committed " + r.data.short);
      if (api.history) api.history.reload();
      ctx.kickStatus();
    } else {
      // The message stays: it is the one thing here that took typing.
      ctx.toast(scCommitError(r), 5000);
    }
    syncKey();
  }

  msg.addEventListener("input", () => { grow(); syncKey(); });
  msg.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      e.stopPropagation();
      commit();
    }
  });
  key.addEventListener("click", commit);

  function open(scope, f) {
    if (scope === "staged") ctx.openStagedDiff(f);
    else if (/\/$/.test(f.path)) ctx.openFolder(f.path);
    else ctx.openFile(f.path, scope === "changes" ? { marks: true } : {});
  }

  function fillSection(name, scope, list) {
    const s = sections[name];
    s.count.textContent = String(list.length);
    s.el.hidden = !list.length;
    let at = s.body.firstChild;
    for (const f of list) {
      const k = name + "\n" + f.path;
      const sig = f.status + (f.staged ? "s" : "") + (f.unstaged ? "u" : "");
      let r = rows.get(k);
      if (!r || r.sig !== sig) {
        if (r) r.el.remove();
        r = { sig: sig, el: scFileRow(f, scope, h, (x) => open(name, x)), seen: 0 };
        rows.set(k, r);
      }
      r.seen = paintSeq;
      if (r.el !== at) s.body.insertBefore(r.el, at);
      else at = at.nextSibling;
    }
  }

  // The history follows the repo and the ref; asked again only when either
  // moved, since every status answer comes through here.
  function syncHistory(root, ref) {
    histSec.hidden = !root;
    histRef.textContent = ref || ctx.branch() || "HEAD";
    const k = root ? root + "\n" + ref : "";
    if (k === histKey) return;
    histKey = k;
    if (root) history.load(ref || "HEAD");
  }

  let paintSeq = 0;
  function render() {
    const ref = ctx.ref();
    const root = ctx.root();
    syncKey();
    syncHistory(known || ref ? root : "", ref);
    note.textContent = "";
    let say = "";
    if (ref) say = "Browsing " + ref + "; changes are read on the disk";
    else if (!known) say = "Reading the repository…";
    else if (!root) say = "Not a git repository";
    note.hidden = !say;
    if (say) note.textContent = say;
    compose.hidden = !!say;
    lists.hidden = !!say;
    if (say) return;

    paintSeq++;
    const all = [...files.values()];
    fillSection("staged", "staged", all.filter((f) => f.staged));
    fillSection("changes", "unstaged", all.filter((f) => f.unstaged && f.status !== "??"));
    fillSection("untracked", "unstaged", all.filter((f) => f.status === "??"));
    for (const [k, r] of rows) {
      if (r.seen !== paintSeq) { r.el.remove(); rows.delete(k); }
    }
    if (!all.length) {
      note.hidden = false;
      note.textContent = "No changes";
    }
  }

  const api = {
    el: box,
    history: history,
    paint(gitFiles, gitRoot) {
      files = gitFiles || new Map();
      known = true;
      render();
    },
    setRef() { render(); },
    syncCaps() { syncKey(); },
    destroy() {
      if (api.history) api.history.destroy();
      rows.clear();
      box.remove();
    },
  };
  render();
  return api;
}

// The server gate: a backend without /api/git/log must not offer a key that
// opens an empty list. Called by 36-server-version.js beside syncRefCap.
function syncScCap() {
  for (const pane of Object.values(filesPanes)) pane.syncScCap();
}
