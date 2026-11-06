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
  const part = (p) => {
    const cut = p.lastIndexOf("/") + 1;
    return [el("span", { class: "sc-dir" }, p.slice(0, cut)), p.slice(cut)];
  };
  const kids = old ? [...part(old), el("span", { class: "sc-arrow" }, " → "), ...part(path)]
                   : part(path);
  return el("span", { class: "sc-path", title: old ? old + " → " + path : path }, ...kids);
}

// git's decoration list, as the pills a row wears. "HEAD -> main" is the branch
// HEAD is on, and the branch is the name worth reading; a detached HEAD stays
// as itself. Tags lose their "tag: " and keep a class of their own.
function scRefPills(refs) {
  const out = [];
  for (const raw of refs || []) {
    for (let name of String(raw).split(", ")) {
      name = name.trim();
      if (!name) continue;
      let tag = false;
      if (name.startsWith("HEAD -> ")) name = name.slice(8);
      else if (name.startsWith("tag: ")) { name = name.slice(5); tag = true; }
      out.push(el("span", { class: "sc-pill" + (tag ? " tag" : ""), title: name }, name));
    }
  }
  return out;
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
    for (const c of commits) list.appendChild(commitEl(c, root));
    count += commits.length;
    more = !!d.more;
    paintFoot();
  }

  function commitEl(c, root) {
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
        el("span", {}, c.time ? relTime(c.time) : ""),
        el("span", { class: "sc-dot" }, " · "),
        el("span", { class: "sc-sha" }, c.short || String(c.sha).slice(0, 7))));
    row.addEventListener("click", () => {
      const open = files.hidden;
      files.hidden = !open;
      row.setAttribute("aria-expanded", open ? "true" : "false");
      row.parentNode.classList.toggle("open", open);
      if (open && !files.firstChild) fillFiles(files, c, root);
    });
    return el("div", { class: "sc-commit" }, row, files);
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

function scCommitDiffView(ctx, info) {
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
      el("span", { class: "sc-sub" },
        el("span", { class: "sc-sha" }, info.short || String(info.sha).slice(0, 7)),
        el("span", { class: "sc-dot" }, " · "),
        el("span", { class: "sc-diff-subj" }, info.subject || ""))));
  const box = el("div", { class: "sc-diff" }, head, body);
  let seq = 0;

  async function load() {
    const mine = ++seq;
    body.textContent = "";
    body.appendChild(diffNote("Loading…"));
    const r = await scGet("api/git/diff", {
      root: ctx.root(), path: info.path, sha: info.sha, old: info.old || "",
    });
    if (mine !== seq) return;
    const data = r.ok ? r.data
      : { diff: "", error: (r.data && (r.data.detail || r.data.error)) || "Couldn't read the diff" };
    diffRender(body, data, "commit", info.path, null);
  }

  return { el: box, info: info, load: load };
}

// The server gate: a backend without /api/git/log must not offer a key that
// opens an empty list. Called by 36-server-version.js beside syncRefCap.
function syncScCap() {
  for (const pane of Object.values(filesPanes)) pane.syncScCap();
}
