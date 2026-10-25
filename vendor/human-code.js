/*! human-code viewer 2 — copied from the human-code skill's assets/viewer.js; to update: cp, then put the new sha256 in tests/test_build.py */
/* human-code viewer: renders a human_code/<repo>.humancode brief (the skill's references/schema.md is the format).
 *
 * Plain script, no modules: inlined into index.html by build.py, loaded by a host app as a file,
 * and run under node's vm for tests (only the pure helpers run there; nothing touches the DOM
 * until mount()).
 *
 *   HumanCode.mount(host, brief, opts) -> view
 *     opts.sources  { read(file, {ref, start, end}) -> Promise<{text, at: "pinned"|"now", first?} | null> }
 *                   first: line number of text's first line when text is an excerpt (absent = 1, whole file)
 *     opts.open     (src, ctx) -> void    absent: the built-in source aside (sheet on narrow hosts)
 *     opts.ask      (text, src, ctx) -> void   absent: no long-press / context-menu gesture
 *     opts.refresh  () -> void            absent: no refresh pill
 *     opts.theme    "toggle" | "host"     "host" draws no theme button
 *     opts.route    "hash" | "state"      "state" never touches location
 *     opts.footer   string                one quiet line under the overview
 *   view.setStale(data | null), view.setPill(text | null), view.go("#cid/uid"),
 *   view.state() / view.restore(state), view.destroy()
 *   HumanCode.embedded(map), .stale(brief, files), .remap(hunks, line), .tokens, .dagModel, .lede
 *
 * Everything the module draws lives under one .hc element and is styled by one shared <style
 * id="hc-style">, with every colour and font a --hc-* token so a host can map them to its own.
 */
(function (root) {
  'use strict';
  const HC = {};
  HC.VERSION = '2';
  HC.SCHEMA = 1;

  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

  // ---- tokeniser (pseudocode and Python)
  const KW = 'def return if elif else for in not and or while is None True False with as raise try except lambda yield pass';
  const PYKW = KW + ' class import from global nonlocal assert del break continue finally async await';
  const NUM = String.raw`-?\d+(?:,\d{3})*(?:\.\d+)?(?:e-?\d+)?(?:\s?(?:frames?|steps?|fps|deg|rad|GB|MB|mm|cm|ms|px|%|m|s|k|M)(?![\w%])|(?!(?!x ?\d)[A-Za-z_]))`;
  const OPS = String.raw`(?<=\d ?)x(?= ?\d)|->|\+=|-=|<=|>=|==|!=|[=:<>/*+()\[\]{}]`;
  const RX = {
    pseudo: new RegExp([
      String.raw`(?<s>(?<!\w)'[^'\n]*'(?!\w)|"[^"\n]*")`,
      String.raw`(?<k>\b(?:${KW.replace(/ /g, '|')})\b)`,
      String.raw`(?<f>[A-Za-z_][\w.]*(?=\())`,
      String.raw`(?<n>(?<![\w.](?<!\dx))${NUM})`,
      String.raw`(?<o>${OPS})`,
      String.raw`(?<i>[A-Za-z_]\w*)`,
    ].join('|'), 'g'),
    py: new RegExp([
      String.raw`(?<t>[rRbBuUfF]{0,2}(?:"""|'''))`,
      String.raw`(?<s>[rRbBuUfF]{0,2}(?:'(?:[^'\\\n]|\\.)*'|"(?:[^"\\\n]|\\.)*"))`,
      String.raw`(?<c>#.*)`,
      String.raw`(?<d>^\s*@[\w.]+)`,
      String.raw`(?<k>\b(?:${PYKW.replace(/ /g, '|')})\b)`,
      String.raw`(?<f>[A-Za-z_]\w*(?=\())`,
      String.raw`(?<n>(?<![\w.])\d[\d_]*(?:\.\d+)?(?:e-?\d+)?)`,
      String.raw`(?<i>[A-Za-z_]\w*)`,
    ].join('|'), 'g'),
    hash: new RegExp([
      String.raw`(?<c>(?:^|(?<=\s))#.*)`,
      String.raw`(?<s>'[^'\n]*'|"[^"\n]*")`,
      String.raw`(?<n>(?<![\w.-])-?\d+(?:\.\d+)?(?:e-?\d+)?(?![\w.]))`,
    ].join('|'), 'g'),
  };
  // Returns [[text, cls], ...]; st carries the Python triple-quote state across lines.
  function tokens(line, mode, st) {
    const out = [];
    let pos = 0;
    if (mode === 'plain') return [[line, '']];
    if (mode === 'py' && st && st.tq) {
      const j = line.indexOf(st.tq);
      if (j < 0) return [[line, 's']];
      out.push([line.slice(0, j + 3), 's']); pos = j + 3; st.tq = null;
    }
    let comment = '';
    if (mode === 'pseudo') {
      if (/^\s*#/.test(line)) return [[line, 'c']];
      const m = /\s(?:\s#|#\s)/.exec(line);
      if (m) {
        const before = line.slice(0, m.index);
        if ((before.split('"').length - 1) % 2 === 0) { comment = line.slice(m.index); line = before; }
      }
    }
    const rx = RX[mode];
    let afterDef = false;
    rx.lastIndex = pos;
    let m;
    while ((m = rx.exec(line))) {
      if (m[0] === '') { rx.lastIndex++; continue; }
      if (m.index > pos) out.push([line.slice(pos, m.index), '']);
      const g = m.groups;
      let cls = '';
      if (g.t) {
        const q = g.t.slice(-3), j = line.indexOf(q, m.index + g.t.length);
        const end = j < 0 ? line.length : j + 3;
        out.push([line.slice(m.index, end), 's']);
        if (j < 0 && st) st.tq = q;
        pos = rx.lastIndex = end; afterDef = false; continue;
      }
      if (g.s) cls = 's'; else if (g.c) cls = 'c'; else if (g.d) cls = 'd';
      else if (g.k) cls = 'k'; else if (g.f) cls = 'f'; else if (g.n) cls = 'n'; else if (g.o) cls = 'o';
      else if (g.i) cls = afterDef ? 'f' : '';
      afterDef = !!(g.k && (g.k === 'def' || g.k === 'class'));
      out.push([m[0], cls]);
      pos = m.index + m[0].length;
    }
    if (pos < line.length) out.push([line.slice(pos), '']);
    if (comment) out.push([comment, 'c']);
    return out;
  }
  // tokens -> html, with optional [a, b) character ranges wrapped in <mark>.
  function paintTokens(toks, marks) {
    let h = '', off = 0;
    const ms = marks || [];
    toks.forEach(([t, c]) => {
      const a0 = off, b0 = off + t.length;
      let cuts = [a0, b0];
      ms.forEach(([a, b]) => { if (a > a0 && a < b0) cuts.push(a); if (b > a0 && b < b0) cuts.push(b); });
      cuts = [...new Set(cuts)].sort((x, y) => x - y);
      for (let k = 0; k < cuts.length - 1; k++) {
        const piece = esc(t.slice(cuts[k] - a0, cuts[k + 1] - a0));
        const inMark = ms.some(([a, b]) => cuts[k] >= a && cuts[k + 1] <= b);
        const span = c ? '<span class="k-' + c + '">' + piece + '</span>' : piece;
        h += inMark ? '<mark>' + span + '</mark>' : span;
      }
      off = b0;
    });
    return h;
  }
  const modeOf = (f) => {
    const e = (String(f).split('.').pop() || '').toLowerCase();
    if (e === 'py' || e === 'pyi') return 'py';
    if (['yaml', 'yml', 'sh', 'bash', 'sub', 'toml', 'cfg', 'ini', 'txt', 'conf'].includes(e)) return 'hash';
    return 'plain';
  };

  // ---- overview lede: the summary, else the first sentence or two of `what`
  function lede(O) {
    O = O || {};
    if (O.summary) return { text: O.summary, more: !!O.what, full: false };
    const what = O.what || '';
    const sents = what.split(/(?<=[.!?])\s+(?=[A-Z(])/);
    let t = sents[0] || '';
    for (let i = 1; i < sents.length && i < 2 && (t + ' ' + sents[i]).length <= 240; i++) t += ' ' + sents[i];
    return { text: t, more: t.length < what.length, full: true };
  }

  // ---- pipeline layout: stages -> layers, rows and phase bands
  function dagModel(stages) {
    const n = stages.length;
    const hasFrom = stages.some((s) => Array.isArray(s.from));
    const ids = stages.map((s, i) => s.id || '#' + i);
    const at = {}; ids.forEach((id, i) => { if (!(id in at)) at[id] = i; });
    const par = stages.map((s, i) => hasFrom
      ? [...new Set((s.from || []).map((f) => at[f]).filter((j) => j !== undefined && j !== i))]
      : (i ? [i - 1] : []));
    let last = '';
    const ph = stages.map((s) => (last = s.phase || last));
    const hasPhase = stages.some((s) => s.phase);
    const kids = stages.map(() => []);
    par.forEach((ps, i) => ps.forEach((p) => kids[p].push(i)));
    // 1. pure dependency layers (a cycle in the brief is cut where it closes)
    const dl = new Array(n).fill(null), busy = new Array(n).fill(false);
    function dep(i) {
      if (dl[i] != null) return dl[i];
      if (busy[i]) return -1;
      busy[i] = true;
      let L = 0;
      par[i].forEach((p) => { L = Math.max(L, dep(p) + 1); });
      busy[i] = false;
      return (dl[i] = L);
    }
    for (let i = 0; i < n; i++) dep(i);
    // 2. phase order: a phase comes after the phases that feed it; ties and cycles go to the lower mean dependency layer
    const phases = [...new Set(ph)];
    const mean = {};
    phases.forEach((q) => { const xs = dl.filter((_, i) => ph[i] === q); mean[q] = xs.reduce((a, b) => a + b, 0) / xs.length; });
    const feeds = {}; phases.forEach((q) => { feeds[q] = new Set(); });
    par.forEach((ps, i) => ps.forEach((p) => { if (ph[p] !== ph[i]) feeds[ph[i]].add(ph[p]); }));
    const order = [], left = new Set(phases);
    const byMean = (a, b) => (mean[a] - mean[b]) || (phases.indexOf(a) - phases.indexOf(b));
    while (left.size) {
      const all = [...left].sort(byMean);
      const ready = all.filter((q) => [...feeds[q]].every((f) => !left.has(f)));
      const q = (ready.length ? ready : all)[0];
      order.push(q); left.delete(q);
    }
    // 3. lay out phase by phase: a phase starts below every row of the phases before it, and a stage sits
    //    below all its parents; a parent from a later phase is placed in the phase that needs it.
    const layer = new Array(n).fill(null), region = new Array(n).fill(0);
    const regionEnd = [];
    let start = 0;
    const topo = [...Array(n).keys()].sort((a, b) => (dl[a] - dl[b]) || (a - b));
    order.forEach((q, r) => {
      const lay = (i) => {
        if (layer[i] != null) return layer[i];
        layer[i] = start; region[i] = r;
        let L = start;
        par[i].forEach((p) => { if (dl[p] < dl[i]) L = Math.max(L, lay(p) + 1); });
        return (layer[i] = L);
      };
      topo.forEach((i) => { if (ph[i] === q) lay(i); });
      regionEnd[r] = Math.max(start - 1, ...layer.filter((L, i) => L != null && region[i] === r));
      start = regionEnd[r] + 1;
    });
    // move a stage down toward its children, inside its own band, when that leaves fewer edges skipping a row
    // (a start stage moves whenever it does not add one)
    const longAt = (i, L) => par[i].filter((p) => L - layer[p] > 1).length + kids[i].filter((k) => layer[k] - L > 1).length;
    topo.slice().reverse().forEach((i) => {
      if (!kids[i].length) return;
      const to = Math.min(Math.min(...kids[i].map((k) => layer[k])) - 1, regionEnd[region[i]]);
      if (to <= layer[i]) return;
      const now = longAt(i, layer[i]), then = longAt(i, to);
      if (then < now || (!par[i].length && then <= now)) layer[i] = to;
    });
    const used = [...new Set(layer)].sort((a, b) => a - b);
    const remap = {}; used.forEach((L, k) => { remap[L] = k; });
    const layers = used.map(() => []);
    for (let i = 0; i < n; i++) { layer[i] = remap[layer[i]]; layers[layer[i]].push(i); }
    const posIn = new Array(n).fill(0);
    layers.forEach((row, L) => {
      if (L > 0) {
        const bc = (i) => (par[i].length ? par[i].reduce((a, p) => a + posIn[p], 0) / par[i].length : Infinity);
        row.sort((a, b) => (bc(a) - bc(b)) || (a - b));
      }
      row.forEach((i, k) => { posIn[i] = k; });
    });
    const band = layers.map((row) => order[region[row[0]]]);
    return { ids, par, kids, layer, layers, ph, band, hasPhase };
  }

  // ---- stale marks: which units and lines a diff from the pinned commit touches
  // Hunks are [os, oe, ns, ne] in old/new line numbers; a pure insertion after old line a is [a+1, a, ...]
  // (empty old span), and counts only when it lands strictly between two lines of the range.
  function touches(src, hunks) {
    return hunks.some(([os, oe]) => (oe < os ? src.start < os && os <= src.end : os <= src.end && oe >= src.start));
  }
  function stale(brief, files) {
    files = files || {};
    const out = { units: new Set(), lines: new Map(), gone: new Set(), moved: new Map() };
    Object.keys(files).forEach((rel) => { if (files[rel] && files[rel].to) out.moved.set(rel, files[rel].to); });
    const hunksOf = (rel) => {
      const f = files[rel];
      if (f.hunks) return f.hunks;
      const g = f.to && files[f.to];
      return (g && g.hunks) || [];
    };
    const hit = (src) => {
      if (!src || !src.file || !files[src.file]) return false;
      return !!files[src.file].gone || touches(src, hunksOf(src.file));
    };
    ((brief && brief.components) || []).forEach((c) => (c.units || []).forEach((u) => {
      const key = c.id + '/' + u.id;
      const lines = new Set();
      (u.lines || []).forEach((l, i) => { if (hit(l.src)) lines.add(i); });
      if (lines.size) out.lines.set(key, lines);
      if (lines.size || hit(u.src)) out.units.add(key);
      if (u.src && files[u.src.file] && files[u.src.file].gone) out.gone.add(key);
    }));
    return out;
  }
  // Old-side line -> new-side line through a file's hunks; a line inside a changed hunk maps to the hunk's new start.
  function remap(hunks, line) {
    let d = 0;
    const hs = (hunks || []).slice().sort((a, b) => a[0] - b[0]);
    for (const [os, oe, ns, ne] of hs) {
      if (line < os) break;
      if (line <= oe) return ns;
      d += (ne - ns + 1) - (oe - os + 1);
    }
    return line + d;
  }

  // Sources embedded in the page (level one): a file is a whole-file string or a list of {first, text}
  // excerpts; a range is answered by the excerpt that holds its first line (a long unit range is
  // embedded only as far as the panel shows it), else null.
  function embedded(map) {
    map = map || {};
    return { read: (file, o) => {
      const v = Object.prototype.hasOwnProperty.call(map, file) ? map[file] : null;
      if (typeof v === 'string') return Promise.resolve({ text: v, at: 'pinned' });
      if (!Array.isArray(v)) return Promise.resolve(null);
      const s = (o && o.start) || 1;
      const seg = v.find((g) => g.first <= s && s <= g.first + g.text.split('\n').length - 1);
      return Promise.resolve(seg ? { text: seg.text, at: 'pinned', first: seg.first } : null);
    } };
  }

  // ---- styles
  const DARK = `
    --hc-bg: #0f1115; --hc-bg-2: #161a21; --hc-fg: #e6e9ee; --hc-fg-2: #a3abb6; --hc-fg-3: #8d97a2;
    --hc-line: #262c35; --hc-line-2: #3a424d; --hc-accent: #6ea0ff; --hc-accent-bg: rgba(110,160,255,.14);
    --hc-warn: #e0b25c; --hc-tk-kw: #c3a6ff; --hc-tk-str: #86d59a; --hc-tk-fn: #9fc4ff; --hc-tk-comment: #8d97a2; --hc-hs: color-mix(in srgb, #6ea0ff 14%, #161a21);
    --hc-shadow: 0 8px 24px rgba(0,0,0,.5); --hc-backdrop: rgba(0,0,0,.5); --hc-comp-s: 50%; --hc-comp-l: 68%; color-scheme: dark;`;
  // Component colours: eleven hues, saturation and lightness from the theme tokens.
  const HUES = [214, 168, 292, 12, 136, 246, 192, 330, 84, 268, 24];
  const CSS = `
.hc {
  --hc-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  --hc-mono: ui-monospace, "SF Mono", Menlo, "Cascadia Mono", Consolas, "Liberation Mono", monospace;
  --hc-bg: #ffffff; --hc-bg-2: #f6f7f9; --hc-fg: #1b1f24; --hc-fg-2: #57606a; --hc-fg-3: #5f6871;
  --hc-line: #e3e6ea; --hc-line-2: #c9cfd6; --hc-accent: #2461d5; --hc-accent-bg: rgba(47,111,237,.10);
  --hc-warn: #8e5c0e; --hc-tk-kw: #7c4dbd; --hc-tk-num: var(--hc-accent); --hc-tk-str: #187733; --hc-tk-fn: #0a3f7a;
  --hc-tk-comment: #5f6871; --hc-hs: color-mix(in srgb, #2f6fed 10%, #f6f7f9); --hc-shadow: 0 8px 24px rgba(0,0,0,.12); --hc-backdrop: rgba(0,0,0,.35);
  --hc-comp-s: 42%; --hc-comp-l: 46%;
  color-scheme: light;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme=light]) .hc {${DARK}
  }
}
:root[data-theme=dark] .hc {${DARK}
}
.hc { position: relative; isolation: isolate; container: hc / inline-size; height: 100%; display: flex; flex-direction: column; overflow: hidden;
  background: var(--hc-bg); color: var(--hc-fg); font: 15px/1.5 var(--hc-sans); }
.hc:focus { outline: none; }
${HUES.map((h, i) => '.hc .k' + i + ' { --hc-comp: hsl(' + h + ' var(--hc-comp-s) var(--hc-comp-l)); }').join('\n')}
.hc, .hc * { box-sizing: border-box; }
.hc a { color: var(--hc-accent); text-decoration: none; }
.hc button { font: inherit; color: inherit; background: none; border: 0; padding: 0; cursor: pointer; }
.hc :focus-visible { outline: 2px solid var(--hc-accent); outline-offset: 2px; }
.hc .mono { font-family: var(--hc-mono); }
.hc svg.i { width: 16px; height: 16px; fill: none; stroke: currentColor; stroke-width: 1.6; stroke-linecap: round; stroke-linejoin: round; display: block; }
.hc .ib { width: 28px; height: 28px; border-radius: 6px; display: inline-flex; align-items: center; justify-content: center; color: var(--hc-fg-2); flex: none; }
.hc .ib:hover { background: var(--hc-bg-2); color: var(--hc-fg); }
.hc .txtbtn { color: var(--hc-accent); font-size: 13px; font-weight: 500; }
.hc .txtbtn:hover { text-decoration: underline; }
.hc .label { font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: .06em; color: var(--hc-fg-3); margin: 0 0 12px; }

/* header */
.hc header { height: 52px; flex: none; display: flex; align-items: center; gap: 8px; padding: 0 12px 0 16px; border-bottom: 1px solid var(--hc-line); background: var(--hc-bg); position: relative; z-index: 5; }
.hc .hc-menu { display: none; margin-left: -6px; }
.hc .htitle { font-size: 15px; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; min-width: 0; }
.hc .htitle span { color: var(--hc-fg-3); font-weight: 400; margin-left: 6px; }
.hc .hsp { flex: 1; }
.hc .hc-search { font: 13.5px var(--hc-sans); color: var(--hc-fg); background: var(--hc-bg-2); border: 1px solid var(--hc-line); border-radius: 6px; height: 30px; padding: 0 10px; width: 240px; }
.hc .hc-search::placeholder { color: var(--hc-fg-3); }
.hc .hc-search:focus { outline: none; border-color: var(--hc-accent); }
.hc .hc-searchbtn { display: none; }

/* columns */
.hc .cols { flex: 1; min-height: 0; display: flex; }
.hc nav { width: 240px; flex: none; overflow: auto; border-right: 1px solid var(--hc-line); padding: 16px 8px 40px; font-size: 13.5px; background: var(--hc-bg); }
.hc nav a { display: flex; align-items: center; gap: 10px; color: var(--hc-fg); padding: 6px 8px; border-radius: 6px; min-height: 32px; }
.hc nav a:hover { background: var(--hc-bg-2); }
.hc nav .ov { font-weight: 500; margin-bottom: 12px; }
.hc nav .ov.active { color: var(--hc-accent); }
.hc nav .label { padding: 0 8px; margin-bottom: 4px; }
.hc nav .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--hc-comp); flex: none; }
.hc nav .nm { flex: 1; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.hc nav .ct { color: var(--hc-fg-3); font-size: 12px; font-variant-numeric: tabular-nums; }
.hc nav .nc.active .nm { font-weight: 600; }
.hc nav .nus { margin: 2px 0 8px; }
.hc nav .nu { padding: 5px 8px 5px 26px; font-size: 13px; color: var(--hc-fg-2); min-height: 28px; border-radius: 0 6px 6px 0; }
.hc nav .nu span { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.hc nav .nu.active { color: var(--hc-accent); box-shadow: inset 2px 0 0 var(--hc-accent); }
.hc .hc-scrim { display: none; }
.hc main { flex: 1; min-width: 0; overflow: auto; padding: 32px 32px 96px; scroll-padding-top: 24px; }

/* overview */
.hc h2 { font-size: 24px; line-height: 1.2; font-weight: 600; letter-spacing: -0.01em; margin: 0; }
.hc .meta { color: var(--hc-fg-3); font-size: 12.5px; margin-top: 6px; }
.hc .lede { font-size: 16px; line-height: 1.55; max-width: 62ch; margin: 16px 0 0; }
.hc .whatfull { font-size: 15px; line-height: 1.55; max-width: 62ch; margin: 12px 0 0; color: var(--hc-fg-2); }
.hc .hc-more { margin-top: 8px; }
.hc .ovgrid { display: grid; grid-template-columns: minmax(0, 1fr); gap: 40px; margin-top: 40px; }
@container hc (min-width: 1200px) { .hc .ovgrid { grid-template-columns: minmax(0, 1fr) 320px; } }
.hc details.fold { margin-top: 24px; border-top: 1px solid var(--hc-line); }
.hc details.fold > summary { list-style: none; cursor: pointer; display: flex; align-items: center; gap: 8px; padding: 12px 0; font-size: 13px; font-weight: 500; color: var(--hc-fg-2); min-height: 44px; }
.hc details.fold > summary::-webkit-details-marker { display: none; }
.hc details.fold > summary::before { content: ''; width: 6px; height: 6px; border-right: 1.5px solid currentColor; border-bottom: 1.5px solid currentColor; transform: rotate(-45deg); margin: 0 4px 0 2px; transition: transform .15s; }
.hc details.fold[open] > summary::before { transform: rotate(45deg); }
.hc details.fold > summary:hover { color: var(--hc-fg); }
.hc details.fold .n { color: var(--hc-fg-3); font-weight: 400; }
.hc ol.stages { margin: 0 0 8px; padding: 0 0 0 28px; font-size: 14px; color: var(--hc-fg-2); max-width: 72ch; }
.hc ol.stages li { padding: 4px 0; }
.hc ol.stages li::marker { color: var(--hc-fg-3); font-variant-numeric: tabular-nums; font-size: 12.5px; }
.hc ol.stages a { color: var(--hc-fg); }
.hc ol.stages a:hover { color: var(--hc-accent); }
.hc .scopetext { font-size: 14px; color: var(--hc-fg-2); max-width: 72ch; margin: 0 0 8px; }
.hc .hc-footer { margin: 32px 0 0; font-size: 12.5px; color: var(--hc-fg-3); }

/* refresh pill (only with opts.refresh) */
.hc .hc-opill:empty, .hc .hc-npill:empty { display: none; }
.hc .hc-opill { margin-top: 10px; }
.hc .hc-npill { padding: 0 8px; margin-bottom: 12px; }
.hc .hc-rpill { font-size: 12.5px; color: var(--hc-warn); border: 1px solid var(--hc-warn); border-radius: 999px; padding: 2px 12px; max-width: 100%; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; text-align: left; }
.hc .hc-rpill:hover { background: var(--hc-bg-2); }

/* DAG */
.hc .dag { position: relative; }
.hc .dag svg.edges { position: absolute; left: 0; top: 0; width: 100%; height: 100%; overflow: visible; pointer-events: none; z-index: 0; }
.hc .dag .row { position: relative; z-index: 1; display: flex; justify-content: center; flex-wrap: wrap; gap: 12px 24px; }
.hc .dag .layer + .layer, .hc .dag .term + .layer, .hc .dag .layer + .term { margin-top: 40px; }
.hc .dag .phase { display: flex; align-items: center; gap: 12px; margin: 30px 0 14px; position: relative; z-index: 1; }
.hc .dag .term + .phase { margin-top: 30px; }
.hc .dag > svg + .phase { margin-top: 0; }
.hc .dag .phase span { font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: .06em; color: var(--hc-fg-3); white-space: nowrap; }
.hc .dag .phase::after { content: ''; flex: 1; height: 1px; background: var(--hc-line); }
.hc .node { flex: 1 1 150px; max-width: 200px; min-width: 0; display: flex; flex-direction: column; gap: 2px; padding: 10px 12px; border-radius: 8px; border: 1px solid var(--hc-line); border-top: 3px solid var(--hc-comp); background: var(--hc-bg-2); color: var(--hc-fg); }
.hc .node:hover, .hc .node:focus-visible { border-color: var(--hc-accent); border-top-color: var(--hc-comp); outline: none; }
.hc .node:focus-visible { box-shadow: 0 0 0 1px var(--hc-accent); }
.hc .node b { font-size: 13px; line-height: 1.35; font-weight: 500; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.hc .node i { font-style: normal; font-size: 11.5px; color: var(--hc-fg-3); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.hc .pill { font-size: 12.5px; color: var(--hc-fg-2); border: 1px dashed var(--hc-line-2); border-radius: 999px; padding: 4px 14px; background: var(--hc-bg); max-width: 100%; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.hc .edges path { fill: none; stroke: var(--hc-line-2); stroke-width: 1.5; stroke-linecap: round; stroke-linejoin: round; }
.hc .edges marker path { fill: var(--hc-line-2); stroke: none; }
.hc .edges text { font: 11px var(--hc-sans); fill: var(--hc-fg-3); paint-order: stroke; stroke: var(--hc-bg); stroke-width: 4px; stroke-linejoin: round; }

/* worth knowing + components */
.hc .wk { border-top: 1px solid var(--hc-line); }
.hc .sr { display: grid; grid-template-columns: 20px minmax(0, 1fr) 16px; column-gap: 4px; padding: 8px 0; border-bottom: 1px solid var(--hc-line); }
.hc .sr .sm { color: var(--hc-warn); font-weight: 700; font-size: 14px; line-height: 20px; }
.hc .sr .st, .hc .sr .sx { font-size: 14px; line-height: 20px; color: var(--hc-fg); text-align: left; }
.hc .sr .clamp { display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.hc .sr .st { display: block; width: 100%; font-weight: 500; }
.hc .sr .st:hover, .hc .sr .sx[data-file]:hover { color: var(--hc-accent); }
.hc .sr .sx[data-file] { cursor: pointer; }
.hc .sr .sbody { font-size: 13px; line-height: 1.45; color: var(--hc-fg-2); margin: 4px 0 2px; }
.hc .sr .sw { font: 11.5px var(--hc-mono); color: var(--hc-fg-3); display: inline-block; margin-top: 2px; }
.hc .sr .sw:hover { color: var(--hc-accent); }
.hc .sr .sdot { width: 16px; height: 20px; display: flex; align-items: center; justify-content: flex-end; position: relative; }
.hc .sr .sdot::before { content: ''; width: 8px; height: 8px; border-radius: 50%; background: var(--hc-comp); }
.hc .sr .sdot::after { content: ''; position: absolute; inset: -12px -8px -12px -16px; }
.hc .hc-alls { margin-top: 12px; }
.hc .cards { display: grid; grid-template-columns: minmax(0, 1fr); gap: 8px; }
@container hc (min-width: 760px) and (max-width: 1199px) { .hc .cards { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px 24px; } }
.hc .card { display: block; color: var(--hc-fg); padding: 10px 12px 10px 14px; border-radius: 8px; box-shadow: inset 3px 0 0 var(--hc-comp); background: var(--hc-bg); border: 1px solid var(--hc-line); border-left: 0; }
.hc .card:hover { background: var(--hc-bg-2); }
.hc .card .cn { display: flex; align-items: baseline; gap: 8px; }
.hc .card .cn b { font-size: 14px; font-weight: 500; flex: 1; min-width: 0; }
.hc .card .cs { font-size: 12px; color: var(--hc-fg-3); white-space: nowrap; font-variant-numeric: tabular-nums; }
.hc .card .cs .w { color: var(--hc-warn); font-weight: 600; }
.hc .card .cp { font-size: 13px; color: var(--hc-fg-2); line-height: 1.45; margin-top: 2px; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.hc .side > section + section { margin-top: 40px; }

/* component + unit */
.hc .chead h2 { display: flex; align-items: center; gap: 12px; }
.hc .chead h2::before { content: ''; width: 10px; height: 10px; border-radius: 50%; background: var(--hc-comp); flex: none; }
.hc .cpath { font: 12.5px var(--hc-mono); color: var(--hc-fg-3); margin-top: 8px; overflow-wrap: anywhere; }
.hc .purpose { font-size: 16px; line-height: 1.55; color: var(--hc-fg-2); max-width: 62ch; margin: 12px 0 0; }
.hc .unit { margin-top: 40px; }
.hc .chead + .unit { margin-top: 32px; }
.hc .uhead { display: flex; flex-wrap: wrap; align-items: baseline; column-gap: 12px; row-gap: 2px; margin-bottom: 10px; max-width: 92ch; }
.hc .uhead h3 { flex: 1 1 auto; min-width: 0; font-size: 17px; line-height: 1.3; font-weight: 600; margin: 0; }
.hc .chip { font-size: 10.5px; font-weight: 600; text-transform: uppercase; letter-spacing: .04em; color: var(--hc-accent); border: 1px solid var(--hc-accent); border-radius: 4px; padding: 0 5px; line-height: 16px; }
.hc .ucount { font-size: 13px; color: var(--hc-warn); font-weight: 600; white-space: nowrap; }
.hc .uacts { display: flex; align-items: baseline; gap: 12px; margin-left: auto; min-width: 0; max-width: 100%; }
.hc .range { font: 12px var(--hc-mono); color: var(--hc-fg-3); white-space: nowrap; min-width: 0; overflow: hidden; text-overflow: ellipsis; direction: rtl; }
.hc .range:hover { color: var(--hc-accent); }
.hc .shownotes { display: none; font-size: 12.5px; }

.hc .code { position: relative; background: var(--hc-bg-2); border: 1px solid var(--hc-line); border-radius: 8px; padding: 8px 0; font: 13px/1.6 var(--hc-mono); max-width: 92ch; overflow: visible; tab-size: 4; }
.hc main.wide .code { margin-right: 256px; max-width: min(92ch, calc(100% - 256px)); }
.hc main.wide .uhead { max-width: min(92ch, calc(100% - 256px)); }
.hc main.wide .code::after { content: ''; position: absolute; top: 8px; bottom: 8px; left: calc(100% + 8px); width: 1px; background: var(--hc-line); }
.hc .ln { position: relative; display: grid; grid-template-columns: 40px 14px minmax(0, 1fr); padding: 2px 12px 2px 0; }
.hc .ln .gut { color: var(--hc-fg-3); font-size: 11.5px; text-align: right; padding-right: 10px; user-select: none; -webkit-user-select: none; font-variant-numeric: tabular-nums; }
.hc .ln .mk { color: var(--hc-warn); font-weight: 700; text-align: center; user-select: none; -webkit-user-select: none; }
.hc .ln .tx { white-space: pre-wrap; overflow-wrap: anywhere; padding-left: calc(var(--hc-ind, 0ch) + 2ch); text-indent: -2ch; min-width: 0; }
.hc .ln.surprise { box-shadow: inset 2px 0 0 var(--hc-warn); }
.hc .ln.flow .tx { color: var(--hc-fg-2); }
.hc .ln.gap .tx, .hc .ln.comment .tx { color: var(--hc-tk-comment); }
.hc .ln.src { cursor: pointer; }
.hc .ln.src:hover { background: var(--hc-accent-bg); }
.hc .ln.open { background: var(--hc-accent-bg); box-shadow: inset 2px 0 0 var(--hc-accent); }
.hc .ln .where { display: none; position: absolute; right: 0; top: 2px; font: 11px/20.8px var(--hc-mono); color: var(--hc-fg-3); background: linear-gradient(to right, transparent, var(--hc-hs) 16px); padding: 0 12px 0 24px; pointer-events: none; }
@media (hover: hover) { @container hc (min-width: 900px) { .hc .ln.src:hover .where { display: block; } } }
.hc .k-k { color: var(--hc-tk-kw); font-weight: 500; }
.hc .k-n { color: var(--hc-tk-num); }
.hc .k-s { color: var(--hc-tk-str); }
.hc .k-f { color: var(--hc-tk-fn); }
.hc .k-o { color: var(--hc-fg-2); }
.hc .k-c { color: var(--hc-tk-comment); }
.hc .k-d { color: var(--hc-tk-fn); }
.hc .ln.gap .tx span, .hc .ln.comment .tx span { color: inherit; font-weight: inherit; }
.hc .note { font: 12.5px/1.4 var(--hc-sans); color: var(--hc-fg-2); text-indent: 0; white-space: normal; }
.hc main.wide .note { position: absolute; left: calc(100% + 16px); width: 240px; top: 3px; }
.hc main:not(.wide) .note { display: none; grid-column: 3; margin: 4px 0 6px; padding: 2px 0 2px 10px; border-left: 2px solid var(--hc-line-2); }
.hc main:not(.wide) .ln.shownote .note { display: block; }
.hc .nchip { display: none; position: relative; font: 10.5px/14px var(--hc-sans); color: var(--hc-fg-3); border: 1px solid var(--hc-line-2); border-radius: 4px; padding: 0 4px; margin-left: 8px; text-indent: 0; vertical-align: 1px; white-space: nowrap; }
.hc .nchip::before { content: ''; position: absolute; inset: -15px -8px; }
.hc .nchip:hover { color: var(--hc-fg); }
.hc main:not(.wide) .nchip { display: inline-block; }
.hc main:not(.wide) .shownotes { display: inline; }
.hc .ln.shownote .nchip { color: var(--hc-fg); border-color: var(--hc-fg-3); }

/* stale marks (only with view.setStale) */
.hc nav .nc.stale .dot { box-shadow: 0 0 0 2px var(--hc-bg), 0 0 0 3.5px var(--hc-warn); }
.hc nav .hc-sc { color: var(--hc-warn); font-size: 12px; font-weight: 600; font-variant-numeric: tabular-nums; }
.hc .chip.hc-stalechip { color: var(--hc-warn); border-color: var(--hc-warn); text-transform: none; letter-spacing: 0; font-weight: 500; }
.hc .ln.stale .gut { color: var(--hc-warn); box-shadow: inset -2px 0 0 var(--hc-warn); }
@media (pointer: coarse) { .hc.hc-askable .ln { -webkit-touch-callout: none; -webkit-user-select: none; user-select: none; } }

/* search */
.hc .results { margin-top: 20px; max-width: 92ch; }
.hc .res { display: block; padding: 10px 0; border-bottom: 1px solid var(--hc-line); color: var(--hc-fg); }
.hc .res:hover .rt { color: var(--hc-accent); }
.hc .res .rc { font-size: 12px; color: var(--hc-fg-3); }
.hc .res .rt { font: 13px/1.5 var(--hc-mono); white-space: pre-wrap; overflow-wrap: anywhere; margin-top: 2px; }
.hc .res .rn { font-size: 13px; color: var(--hc-fg-2); margin-top: 2px; }
.hc mark { background: none; color: inherit; text-decoration: underline var(--hc-accent) 2px; text-underline-offset: 3px; }
.hc .empty { color: var(--hc-fg-3); padding: 24px 0; }

/* source panel */
.hc .hc-src { display: none; flex-direction: column; min-height: 0; background: var(--hc-bg); }
.hc .hc-src.on { display: flex; }
.hc .hc-src .shead { height: 40px; flex: none; display: flex; align-items: center; gap: 2px; padding: 0 6px 0 14px; border-bottom: 1px solid var(--hc-line); }
.hc .hc-src .sfile { flex: 1; min-width: 0; display: flex; align-items: baseline; font: 12.5px var(--hc-mono); white-space: nowrap; }
.hc .hc-src .sfile .p { direction: rtl; text-align: left; overflow: hidden; text-overflow: ellipsis; min-width: 0; color: var(--hc-fg); }
.hc .hc-src .sfile .r { color: var(--hc-fg-3); flex: none; }
.hc .hc-src .trace { flex: none; background: var(--hc-bg-2); border-bottom: 1px solid var(--hc-line); padding: 8px 14px; font: 12.5px/1.5 var(--hc-mono); color: var(--hc-fg-2); }
.hc .hc-src .trace span { display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; white-space: pre-wrap; overflow-wrap: anywhere; }
.hc .hc-src .trace[hidden] { display: none; }
.hc .hc-src .srcbody { flex: 1; min-height: 0; overflow: auto; overscroll-behavior: contain; }
.hc .hc-src .handle { display: none; }
.hc pre.sv { margin: 0; padding: 8px 0 24px; font: 12.5px/1.55 var(--hc-mono); min-width: max-content; }
.hc pre.sv .l { display: flex; padding-right: 16px; }
.hc pre.sv .n { width: 48px; flex: none; text-align: right; padding-right: 12px; color: var(--hc-fg-3); user-select: none; -webkit-user-select: none; font-variant-numeric: tabular-nums; }
.hc pre.sv .c { white-space: pre; }
.hc pre.sv .l.hit { background: var(--hc-accent-bg); box-shadow: inset 3px 0 0 var(--hc-accent); }
.hc pre.sv .more { display: flex; justify-content: center; padding: 4px 0; position: sticky; left: 0; width: var(--hc-bw, 100%); }
.hc pre.sv .more button { font: 12px var(--hc-sans); color: var(--hc-fg-2); border-radius: 6px; padding: 4px 10px; }
.hc pre.sv .more button:hover { background: var(--hc-bg-2); color: var(--hc-fg); }
.hc .srcnote { color: var(--hc-fg-3); font: 13px var(--hc-sans); padding: 24px 16px; }
.hc pre.sv .excerpt { color: var(--hc-fg-3); font: 12px var(--hc-sans); padding: 6px 12px 2px; position: sticky; left: 0; width: var(--hc-bw, 100%); box-sizing: border-box; }
@container hc (min-width: 900px) {
  .hc .hc-src { width: 44%; max-width: 760px; flex: none; border-left: 1px solid var(--hc-line); }
  .hc.navoff nav { position: absolute; top: 0; left: 0; bottom: 0; z-index: 20; transform: translateX(-100%); transition: transform .15s; box-shadow: var(--hc-shadow); }
  .hc.navoff nav.open { transform: none; }
  .hc.navoff .hc-menu { display: inline-flex; }
}

@container hc (max-width: 899px) {
  .hc .hc-menu { display: inline-flex; }
  .hc nav { position: absolute; top: 0; left: 0; bottom: 0; width: min(80%, 320px); z-index: 20; transform: translateX(-100%); transition: transform .15s; box-shadow: var(--hc-shadow); }
  .hc nav.open { transform: none; }
  .hc nav a { min-height: 44px; }
  .hc nav .nu { min-height: 40px; }
  .hc .hc-src { position: absolute; left: 0; right: 0; bottom: 0; height: 72%; z-index: 30; border-top: 1px solid var(--hc-line); border-radius: 12px 12px 0 0; box-shadow: var(--hc-shadow); }
  .hc .hc-src .handle { display: block; width: 36px; height: 4px; border-radius: 2px; background: var(--hc-line-2); margin: 8px auto 0; flex: none; }
  .hc .hc-src .shead { height: 44px; }
  .hc .hc-src .ib { width: 44px; height: 44px; }
  .hc pre.sv { font-size: 12px; }
  .hc .ib { width: 44px; height: 44px; }
}
.hc .hc-scrim.on, .hc .hc-backdrop.on { display: block; position: absolute; inset: 0; background: var(--hc-backdrop); z-index: 15; }
.hc .hc-backdrop { display: none; }
.hc .hc-backdrop.on { z-index: 25; }
@container hc (max-width: 760px) {
  .hc header { padding: 0 4px 0 8px; gap: 2px; }
  .hc .hc-search { display: none; }
  .hc .hc-searchbtn { display: inline-flex; }
  .hc.searching main { padding-top: 72px; }
  .hc.searching .hc-search { display: block; position: absolute; left: 8px; right: 8px; top: 52px; width: auto; height: 40px; font-size: 16px; box-shadow: var(--hc-shadow); background: var(--hc-bg); }
  .hc main { padding: 20px 16px 96px; }
  .hc .code { font-size: 12.5px; margin-left: -4px; margin-right: -4px; }
  .hc .ln { grid-template-columns: 30px 12px minmax(0, 1fr); padding-right: 10px; }
  .hc .ln .gut { padding-right: 6px; }
  .hc .uacts { flex-basis: 100%; margin-left: 0; justify-content: space-between; }
  .hc .node { flex: 0 1 calc(50% - 8px); max-width: none; min-width: 0; }
  .hc .dag .row { gap: 12px 16px; }
  .hc h2 { font-size: 22px; }
}
`;
  function injectStyle(doc) {
    if (doc.getElementById('hc-style')) return;
    const st = doc.createElement('style');
    st.id = 'hc-style';
    st.textContent = CSS;
    doc.head.appendChild(st);
  }

  const ICON = {
    menu: '<svg class="i" viewBox="0 0 16 16"><path d="M2.5 4.5h11M2.5 8h11M2.5 11.5h11"/></svg>',
    search: '<svg class="i" viewBox="0 0 16 16"><circle cx="7" cy="7" r="4.5"/><path d="M10.5 10.5l3 3"/></svg>',
    copy: '<svg class="i" viewBox="0 0 16 16"><rect x="5.5" y="5.5" width="8" height="8" rx="1.5"/><path d="M10.5 5.5v-2a1 1 0 0 0-1-1h-6a1 1 0 0 0-1 1v6a1 1 0 0 0 1 1h2"/></svg>',
    close: '<svg class="i" viewBox="0 0 16 16"><path d="M4 4l8 8M12 4l-8 8"/></svg>',
    system: '<svg class="i" viewBox="0 0 16 16"><circle cx="8" cy="8" r="5.5"/><path d="M8 2.5v11a5.5 5.5 0 0 0 0-11z" fill="currentColor" stroke="none"/></svg>',
    light: '<svg class="i" viewBox="0 0 16 16"><circle cx="8" cy="8" r="3"/><path d="M8 1.5v1.5M8 13v1.5M1.5 8H3M13 8h1.5M3.4 3.4l1 1M11.6 11.6l1 1M3.4 12.6l1-1M11.6 4.4l1-1"/></svg>',
    dark: '<svg class="i" viewBox="0 0 16 16"><path d="M13 9.5A5.5 5.5 0 0 1 6.5 3a5.5 5.5 0 1 0 6.5 6.5z"/></svg>',
    file: '<svg class="i" viewBox="0 0 16 16"><path d="M4 1.5h5l3.5 3.5v9a.5.5 0 0 1-.5.5H4a.5.5 0 0 1-.5-.5v-12a.5.5 0 0 1 .5-.5z"/><path d="M9 1.5V5h3.5"/></svg>',
    range: '<svg class="i" viewBox="0 0 16 16"><path d="M3 3.5h10M3 12.5h10M5.5 6.5h5M5.5 9.5h5"/></svg>',
  };

  let mounts = 0;

  function mount(host, brief, opts) {
    opts = opts || {};
    const D = brief || {};
    const doc = host.ownerDocument, win = doc.defaultView;
    const sources = opts.sources || embedded(D.sources);
    const useHash = opts.route !== 'state';
    const markerId = 'hc-ah-' + (++mounts);
    injectStyle(doc);

    const comps = D.components || [];
    const O = D.overview || {};
    const kOf = (c) => 'k' + (comps.indexOf(c) % HUES.length);
    const unitById = {};
    comps.forEach((c) => { (c.units || []).forEach((u) => { unitById[c.id + '/' + u.id] = { c, u }; }); });
    const base = (f) => String(f).split('/').pop();
    const nSurprise = (u) => (u.lines || []).filter((l) => l.kind === 'surprise').length;

    host.innerHTML =
      '<div class="hc" tabindex="-1"><header>' +
      '<button class="ib hc-menu" aria-label="Menu">' + ICON.menu + '</button>' +
      '<div class="htitle hc-title"></div><div class="hsp"></div>' +
      '<input class="hc-search" type="search" placeholder="Search  /" autocomplete="off" spellcheck="false">' +
      '<button class="ib hc-searchbtn" aria-label="Search">' + ICON.search + '</button>' +
      (opts.theme === 'host' ? '' : '<button class="ib hc-themebtn" aria-label="Theme"></button>') +
      '</header><div class="cols">' +
      '<nav aria-label="Components"></nav><div class="hc-scrim"></div><main></main><div class="hc-backdrop"></div>' +
      '<aside class="hc-src" aria-label="Source"><div class="handle"></div><div class="shead">' +
      '<div class="sfile"><span class="p"><bdi class="hc-srcpath"></bdi></span><span class="r hc-srcrange"></span></div>' +
      '<button class="ib hc-copy" title="Copy path">' + ICON.copy + '</button>' +
      '<button class="ib hc-whole" title="Whole file"></button>' +
      '<button class="ib hc-close" title="Close">' + ICON.close + '</button>' +
      '</div><div class="trace hc-trace"></div><div class="srcbody"></div></aside></div></div>';
    const R = host.firstChild;
    const q = (sel) => R.querySelector(sel);
    const nav = q('nav'), main = q('main'), search = q('.hc-search'), srcEl = q('.hc-src'), srcBody = q('.srcbody');
    const scrim = q('.hc-scrim'), backdrop = q('.hc-backdrop'), wholeBtn = q('.hc-whole');
    const phone = () => R.clientWidth <= 899;
    const cleanups = [];
    const listen = (el, type, fn, o) => { el.addEventListener(type, fn, o); cleanups.push(() => el.removeEventListener(type, fn, o)); };

    q('.hc-title').innerHTML = esc(D.repo || 'Human code') + (D.repo ? '<span>human code</span>' : '');

    // ---- theme (level one only; a host with its own theme passes theme: "host")
    const themeBtn = q('.hc-themebtn');
    if (themeBtn) {
      const de = doc.documentElement;
      let theme = de.dataset.theme || 'system';
      const paintTheme = () => {
        themeBtn.innerHTML = ICON[theme];
        themeBtn.title = 'Theme: ' + theme[0].toUpperCase() + theme.slice(1);
      };
      themeBtn.onclick = () => {
        theme = { system: 'light', light: 'dark', dark: 'system' }[theme];
        if (theme === 'system') delete de.dataset.theme; else de.dataset.theme = theme;
        try { if (theme === 'system') win.localStorage.removeItem('hc-theme'); else win.localStorage.setItem('hc-theme', theme); } catch (e) {}
        paintTheme();
      };
      paintTheme();
    }

    // ---- navigation
    let curComp = null;
    let pill = null;
    let staleView = null;
    function renderNav(cid, uid) {
      let h = opts.refresh ? '<div class="hc-npill"></div>' : '';
      h += '<a class="ov' + (!cid ? ' active' : '') + '" href="#overview">Overview</a><div class="label">Components</div>';
      comps.forEach((c) => {
        const on = c.id === cid;
        h += '<a class="nc ' + kOf(c) + (on ? ' active' : '') + '" data-c="' + esc(c.id) + '" href="#' + esc(c.id) + '"><span class="dot"></span><span class="nm">' + esc(c.name) + '</span><span class="ct">' + (c.units || []).length + '</span></a>';
        if (on) {
          h += '<div class="nus">';
          (c.units || []).forEach((u) => { h += '<a class="nu' + (u.id === uid ? ' active' : '') + '" data-u="' + esc(u.id) + '" href="#' + esc(c.id) + '/' + esc(u.id) + '"><span>' + esc(u.name) + '</span></a>'; });
          h += '</div>';
        }
      });
      nav.innerHTML = h;
      paintPill();
      paintStale();
    }
    function setActiveUnit(uid) {
      nav.querySelectorAll('.nu').forEach((a) => a.classList.toggle('active', a.dataset.u === uid));
    }
    function openNav() { nav.classList.add('open'); scrim.classList.add('on'); }
    function closeNav() { nav.classList.remove('open'); scrim.classList.remove('on'); }
    q('.hc-menu').onclick = () => (nav.classList.contains('open') ? closeNav() : openNav());
    scrim.onclick = closeNav;
    listen(nav, 'click', (e) => { if (e.target.closest('a')) closeNav(); });

    // ---- shared bits
    function srcAttr(src, trace) {
      if (!src || !src.file) return '';
      return ' data-file="' + esc(src.file) + '" data-start="' + src.start + '" data-end="' + src.end + '"' +
        (src.match != null ? ' data-match="' + esc(src.match) + '"' : '') + (trace ? ' data-trace="' + esc(trace) + '"' : '');
    }

    // ---- overview
    let dagState = null;
    function dagHtml(stages) {
      const M = dagModel(stages);
      let h = '<div class="dag"><svg class="edges" aria-hidden="true"></svg>';
      if (O.in) h += '<div class="row term" data-term="in"><span class="pill" title="' + esc(O.in) + '">' + esc(O.in) + '</span></div>';
      let prevPh = null;
      M.layers.forEach((row, L) => {
        const p = M.hasPhase ? M.band[L] : null;
        if (p && p !== prevPh) h += '<div class="phase"><span>' + esc(p) + '</span></div>';
        prevPh = p;
        h += '<div class="row layer" data-l="' + L + '">';
        row.forEach((i) => {
          const s = stages[i];
          const hit = unitById[s.unit];
          const label = s.short || s.stage || '';
          h += '<a class="node' + (hit ? ' ' + kOf(hit.c) : '') + '" data-n="' + i + '"' + (hit ? ' href="#' + esc(s.unit) + '"' : ' tabindex="0"') +
            ' title="' + esc(s.stage || '') + '"><b>' + esc(label) + '</b><i>' + esc(hit ? hit.c.name : '') + '</i></a>';
        });
        h += '</div>';
      });
      if (O.out) h += '<div class="row term" data-term="out"><span class="pill" title="' + esc(O.out) + '">' + esc(O.out) + '</span></div>';
      h += '</div>';
      dagState = { M, stages };
      return h;
    }

    function drawDag() {
      const dag = main.querySelector('.dag');
      if (!dag || !dagState) return;
      const { M, stages } = dagState;
      const svg = dag.querySelector('svg');
      const DR = dag.getBoundingClientRect();
      const box = (el) => { const r = el.getBoundingClientRect(); return { l: r.left - DR.left, r: r.right - DR.left, t: r.top - DR.top, b: r.bottom - DR.top, cx: (r.left + r.right) / 2 - DR.left }; };
      const nodeEl = {}; dag.querySelectorAll('.node').forEach((el) => { nodeEl[el.dataset.n] = el; });
      const rows = [...dag.children].filter((el) => el.tagName.toLowerCase() !== 'svg');
      // next/prev row geometry, to place turns and labels in the clear gaps
      const gapBelow = (el) => { const k = rows.indexOf(el); const nx = rows[k + 1]; const b = box(el).b; return nx ? [b, box(nx).t] : [b, b + 40]; };
      const gapAbove = (el) => { const k = rows.indexOf(el); const pv = rows[k - 1]; const t = box(el).t; return pv ? [box(pv).b, t] : [t - 40, t]; };
      const layerRow = (L) => dag.querySelector('.layer[data-l="' + L + '"]');
      // virtual nodes: in = layer -1, out = layer nLayers
      const NL = M.layers.length;
      const pillIn = dag.querySelector('[data-term=in] .pill'), pillOut = dag.querySelector('[data-term=out] .pill');
      const edges = [];
      stages.forEach((s, i) => M.par[i].forEach((p) => edges.push([p, i])));
      // terminals join the flow's first and last rows; a start stage further down (configs, a model build) is not fed by the inputs
      if (pillIn) M.layers[0].forEach((i) => { if (M.kids[i].length) edges.push(['in', i]); });
      if (pillOut) M.layers[NL - 1].forEach((i) => { if (M.par[i].length) edges.push([i, 'out']); });
      if (pillIn && pillOut && stages.length === 0) edges.push(['in', 'out']);
      const geo = (v) => {
        if (v === 'in') return { b: box(pillIn), L: -1, row: pillIn.parentNode };
        if (v === 'out') return { b: box(pillOut), L: NL, row: pillOut.parentNode };
        return { b: box(nodeEl[v]), L: M.layer[v], row: layerRow(M.layer[v]) };
      };
      // lanes on the right for edges that skip a layer; one lane per parent, reused when spans do not overlap
      const long = edges.filter(([p, c]) => geo(c).L - geo(p).L > 1);
      const spanOf = {};
      long.forEach(([p, c]) => { const k = String(p), a = geo(p).L, b = geo(c).L; spanOf[k] = spanOf[k] ? [Math.min(spanOf[k][0], a), Math.max(spanOf[k][1], b)] : [a, b]; });
      const laneEnds = [], laneOf = {};
      Object.keys(spanOf).sort((x, y) => spanOf[x][0] - spanOf[y][0]).forEach((k) => {
        const [a, b] = spanOf[k];
        let j = laneEnds.findIndex((e) => e < a);
        if (j < 0) { j = laneEnds.length; laneEnds.push(b); } else laneEnds[j] = b;
        laneOf[k] = j;
      });
      const nLanes = laneEnds.length;
      const pad = nLanes ? 14 + 10 * nLanes : 0;
      if (dag.style.paddingRight !== pad + 'px') { dag.style.paddingRight = pad + 'px'; return drawDag(); }
      const W = DR.width;
      const laneX = (j) => W - 10 - 10 * j;
      const paths = [];
      const labels = [];
      const labelled = new Set();
      const rad = 6;
      edges.forEach(([p, c]) => {
        const P = geo(p), C = geo(c);
        const x0 = P.b.cx, y0 = P.b.b, x1 = C.b.cx, y1 = C.b.t;
        const [gb0, gb1] = gapBelow(P.row);
        if (C.L - P.L > 1) {
          const X = laneX(laneOf[String(p)]);
          const yA = gb1 - 10;
          const [ga0, ga1] = gapAbove(C.row);
          const yB = Math.max(ga0 + 10, (ga0 + ga1) / 2);
          paths.push('M' + x0 + ' ' + y0 + 'V' + (yA - rad) + 'Q' + x0 + ' ' + yA + ' ' + (x0 + rad) + ' ' + yA +
            'H' + (X - rad) + 'Q' + X + ' ' + yA + ' ' + X + ' ' + (yA + rad) +
            'V' + (yB - rad) + 'Q' + X + ' ' + yB + ' ' + (X - rad) + ' ' + yB +
            'H' + (x1 + rad) + 'Q' + x1 + ' ' + yB + ' ' + x1 + ' ' + (yB + rad) + 'V' + (y1 - 1));
        } else {
          const h = (y1 - y0) / 2;
          paths.push('M' + x0 + ' ' + y0 + 'C' + x0 + ' ' + (y0 + h) + ' ' + x1 + ' ' + (y1 - h) + ' ' + x1 + ' ' + (y1 - 1));
        }
        // a node on an upper wrapped line has no clear gap under it; its label would sit on the next line's node
        if (typeof p === 'number' && !labelled.has(p) && stages[p].yields && P.b.b >= box(P.row).b - 1) {
          labelled.add(p);
          labels.push([x0, Math.min(y0 + 13, (gb0 + gb1) / 2), stages[p].yields]);
        }
      });
      svg.innerHTML = '<defs><marker id="' + markerId + '" viewBox="0 0 6 6" refX="5" refY="3" markerWidth="6" markerHeight="6" orient="auto"><path d="M0 0L6 3L0 6z"/></marker></defs>' +
        paths.map((d) => '<path d="' + d + '" marker-end="url(#' + markerId + ')"/>').join('') +
        labels.map(([x, y, t]) => '<text x="' + x + '" y="' + y + '" text-anchor="middle" dominant-baseline="central">' + esc(t) + '</text>').join('');
    }
    let dagRO = null, dagTimer = 0;
    function watchDag() {
      if (dagRO) dagRO.disconnect();
      dagRO = null;
      const dag = main.querySelector('.dag');
      if (!dag) return;
      drawDag();
      dagRO = new win.ResizeObserver(() => { clearTimeout(dagTimer); dagTimer = setTimeout(drawDag, 100); });
      dagRO.observe(dag);
    }

    function surpriseRow(s, i) {
      const src = s.src || null;
      const hit = s.unit && unitById[s.unit];
      let h = '<div class="sr"' + (hit ? ' data-unit="' + esc(s.unit) + '"' : '') + '><span class="sm">!</span><div>';
      if (s.title) {
        h += '<button class="st" data-tog="' + i + '">' + esc(s.title) + '</button><div class="sbody" hidden>' + esc(s.text) + '</div>';
      } else {
        h += '<div class="sx clamp"' + srcAttr(src, s.text) + '>' + esc(s.text) + '</div>';
      }
      if (src) h += '<a class="sw" href="#"' + srcAttr(src, s.title || s.text) + '>' + esc(base(src.file) + ':' + src.start) + '</a>';
      h += '</div>';
      h += hit ? '<a class="sdot ' + kOf(hit.c) + '" href="#' + esc(s.unit) + '" title="' + esc(hit.c.name + ' › ' + hit.u.name) + '" aria-label="' + esc(hit.u.name) + '"></a>' : '<span></span>';
      return h + '</div>';
    }

    function renderOverview() {
      curComp = null;
      const L = lede(O);
      const st = O.pipeline || [];
      const sp = O.surprises || [];
      let h = '<h2>' + esc(D.repo || 'Overview') + '</h2>';
      const meta = [D.commit ? '<span class="mono">' + esc(D.commit) + '</span>' : '', esc(D.date || '')].filter(Boolean).join(' · ');
      if (meta) h += '<div class="meta">' + meta + '</div>';
      if (opts.refresh) h += '<div class="hc-opill"></div>';
      if (L.text) h += '<p class="lede">' + esc(L.text) + '</p>';
      if (L.more) h += '<button class="txtbtn hc-more" data-full="' + (L.full ? 1 : 0) + '">More</button>';
      h += '<div class="ovgrid"><div class="ovmain">';
      if (st.length) {
        h += '<div class="label">Pipeline</div>' + dagHtml(st);
        h += '<details class="fold"><summary>Stage details <span class="n">' + st.length + '</span></summary><ol class="stages">';
        st.forEach((p) => { h += '<li>' + (unitById[p.unit] ? '<a href="#' + esc(p.unit) + '">' + esc(p.stage) + '</a>' : esc(p.stage)) + '</li>'; });
        h += '</ol></details>';
      }
      h += '</div><div class="side">';
      if (sp.length) {
        h += '<section><div class="label">Worth knowing</div><div class="wk">';
        sp.forEach((s, i) => { if (i < 6) h += surpriseRow(s, i); });
        h += '</div>' + (sp.length > 6 ? '<button class="txtbtn hc-alls">All ' + sp.length + '</button>' : '') + '</section>';
      }
      h += '<section><div class="label">Components</div><div class="cards">';
      comps.forEach((c) => {
        const nu = (c.units || []).length, ns = (c.units || []).reduce((a, u) => a + nSurprise(u), 0);
        h += '<a class="card ' + kOf(c) + '" href="#' + esc(c.id) + '"><div class="cn"><b>' + esc(c.name) + '</b><span class="cs">' + nu + ' unit' + (nu === 1 ? '' : 's') +
          (ns ? ' · <span class="w">' + ns + ' !</span>' : '') + '</span></div>' + (c.purpose ? '<div class="cp" title="' + esc(c.purpose) + '">' + esc(c.purpose) + '</div>' : '') + '</a>';
      });
      h += '</div></section></div></div>';
      if (D.scope) h += '<details class="fold"><summary>Scope</summary><p class="scopetext">' + esc(D.scope) + '</p></details>';
      if (opts.footer) h += '<p class="hc-footer">' + esc(opts.footer) + '</p>';
      main.innerHTML = h;
      main.scrollTop = 0;
      paintPill();
      watchDag();
    }

    // ---- component page
    function lineHtml(l, n, i) {
      const kind = l.kind || 'decision';
      const raw = String(l.text || '');
      const ind = raw.length - raw.trimStart().length;
      const t = raw.trimStart();
      const body = (kind === 'gap' || kind === 'comment') ? esc(t) : paintTokens(tokens(t, 'pseudo'));
      const src = l.src && l.src.file ? l.src : null;
      return '<div class="ln ' + esc(kind) + (src ? ' src' : '') + '"' + (i != null ? ' data-i="' + i + '"' : '') + (src ? ' tabindex="0"' + srcAttr(src, raw) : '') + (ind ? ' style="--hc-ind:' + ind + 'ch"' : '') + '>' +
        '<span class="gut">' + n + '</span><span class="mk">' + (kind === 'surprise' ? '!' : '') + '</span>' +
        '<span class="tx">' + body + (l.note ? '<button class="nchip" aria-label="Show note">note</button>' : '') + '</span>' +
        (src ? '<span class="where">' + esc(base(src.file) + ':' + src.start) + '</span>' : '') +
        (l.note ? '<span class="note">' + esc(l.note) + '</span>' : '') + '</div>';
    }
    function unitHtml(c, u) {
      const ns = nSurprise(u);
      const hasNotes = (u.lines || []).some((l) => l.note);
      let h = '<section class="unit" data-key="' + esc(c.id + '/' + u.id) + '" data-u="' + esc(u.id) + '"><div class="uhead"><h3>' + esc(u.name) + '</h3>';
      if (u.status) h += '<span class="chip">' + esc(u.status) + '</span>';
      if (ns) h += '<span class="ucount">! ' + ns + '</span>';
      h += '<span class="uacts">' + (hasNotes ? '<button class="txtbtn shownotes">Show notes</button>' : '');
      if (u.src && u.src.file) h += '<a class="range" href="#"' + srcAttr(u.src, u.signature || u.name) + '><bdi>' + esc(u.src.file + ':' + u.src.start + '-' + u.src.end) + '</bdi></a>';
      h += '</span></div><div class="code">';
      let n = 1;
      if (u.signature) h += lineHtml({ text: u.signature, kind: 'flow', src: u.src }, n++, null);
      (u.lines || []).forEach((l, i) => { h += lineHtml(l, n++, i); });
      return h + '</div></section>';
    }
    function renderComponent(c, uid) {
      curComp = c.id;
      let h = '<div class="chead ' + kOf(c) + '"><h2>' + esc(c.name) + '</h2>';
      if (c.path) h += '<div class="cpath">' + esc(c.path) + '</div>';
      if (c.purpose) h += '<p class="purpose">' + esc(c.purpose) + '</p>';
      h += '</div>';
      (c.units || []).forEach((u) => { h += unitHtml(c, u); });
      main.innerHTML = h;
      paintStale();
      layoutNotes();
      scrollToUnit(c, uid);
    }
    function unitEl(key) {
      return [...main.querySelectorAll('.unit')].find((s) => s.dataset.key === key) || null;
    }
    function scrollToUnit(c, uid) {
      const el = uid && unitEl(c.id + '/' + uid);
      if (el) main.scrollTop = el.offsetTop - main.offsetTop - 24; else main.scrollTop = 0;
      setActiveUnit(uid || ((c.units || [])[0] || {}).id);
    }

    // margin notes: wide main gets the margin; walk notes in order and push down on overlap
    // Keep the reader's place across reflows: the open line, else the first line in view.
    function anchorOf() {
      const top = main.getBoundingClientRect().top;
      let el = main.querySelector('.ln.open');
      if (!el) el = [...main.querySelectorAll('.ln, .unit, h2')].find((x) => x.getBoundingClientRect().bottom > top) || null;
      return el ? { el, top: el.getBoundingClientRect().top } : null;
    }
    function keepAnchor(a) {
      if (a && R.contains(a.el)) main.scrollTop += a.el.getBoundingClientRect().top - a.top;
    }
    function layoutNotes() {
      const a = anchorOf();
      layoutNotesNow();
      keepAnchor(a);
    }
    function layoutNotesNow() {
      const wide = main.clientWidth - 64 >= 840;
      main.classList.toggle('wide', wide);
      const notes = [...main.querySelectorAll('.note')];
      notes.forEach((n) => { n.style.top = ''; });
      if (!wide) return;
      let prev = -Infinity;
      notes.forEach((n) => {
        const r = n.getBoundingClientRect();
        if (r.top < prev + 6) { n.style.top = (3 + prev + 6 - r.top) + 'px'; prev = prev + 6 + r.height; } else prev = r.bottom;
      });
    }
    let mainW = 0, noteTimer = 0;
    const mainRO = new win.ResizeObserver(() => {
      const w = main.clientWidth;
      if (w === mainW) return;
      mainW = w;
      clearTimeout(noteTimer); noteTimer = setTimeout(layoutNotes, 60);
    });
    mainRO.observe(main);

    listen(main, 'scroll', () => {
      if (!curComp) return;
      const top = main.getBoundingClientRect().top + 80;
      let cur = null;
      main.querySelectorAll('.unit').forEach((s) => { if (s.getBoundingClientRect().top <= top) cur = s.dataset.u; });
      if (cur) setActiveUnit(cur);
    }, { passive: true });

    // ---- search
    function renderSearch(qs) {
      curComp = null;
      const needle = qs.toLowerCase();
      const mk = (s) => {
        const t = String(s); const i = t.toLowerCase().indexOf(needle);
        return i < 0 ? esc(t) : esc(t.slice(0, i)) + '<mark>' + esc(t.slice(i, i + qs.length)) + '</mark>' + esc(t.slice(i + qs.length));
      };
      let h = '<h2>Search</h2><div class="meta">“' + esc(qs) + '”</div><div class="results">';
      let n = 0;
      comps.forEach((c) => (c.units || []).forEach((u) => {
        const href = '#' + esc(c.id) + '/' + esc(u.id);
        const crumb = '<div class="rc">' + esc(c.name) + ' › ' + esc(u.name) + '</div>';
        if ((u.name + ' ' + (u.signature || '')).toLowerCase().includes(needle)) {
          n++; h += '<a class="res" href="' + href + '">' + crumb + '<div class="rt">' + mk((u.signature || u.name).trim()) + '</div></a>';
        }
        (u.lines || []).forEach((l) => {
          const inText = String(l.text).toLowerCase().includes(needle), inNote = String(l.note || '').toLowerCase().includes(needle);
          if (!inText && !inNote) return;
          n++;
          h += '<a class="res" href="' + href + '">' + crumb + '<div class="rt">' + mk(String(l.text).trim()) + '</div>' + (inNote ? '<div class="rn">' + mk(l.note) + '</div>' : '') + '</a>';
        });
      }));
      h += '</div>' + (n ? '' : '<p class="empty">Nothing matches.</p>');
      main.innerHTML = h;
      main.scrollTop = 0;
    }
    function leaveSearch() {
      if (search.value) { search.value = ''; R.classList.remove('searching'); curComp = null; }
    }
    listen(search, 'input', (e) => {
      const qs = e.target.value.trim();
      if (qs.length >= 2) renderSearch(qs); else route();
    });
    listen(search, 'keydown', (e) => { if (e.key === 'Escape') { e.target.value = ''; e.target.blur(); R.classList.remove('searching'); route(); } });
    q('.hc-searchbtn').onclick = () => {
      const on = R.classList.toggle('searching');
      if (on) search.focus(); else { search.value = ''; route(); }
    };
    listen(R, 'keydown', (e) => {
      const t = e.target;
      if (e.key === '/' && !/INPUT|TEXTAREA|SELECT/.test(t.tagName) && !t.isContentEditable) { e.preventDefault(); R.classList.add('searching'); search.focus(); }
      if (e.key === 'Escape' && srcEl.classList.contains('on') && t !== search) closeSource();
    });

    // ---- routing: location.hash ("hash") or a private route string ("state")
    let stateRoute = '#overview';
    const currentRoute = () => (useHash ? win.location.hash || '#overview' : stateRoute);
    function route() {
      let r = currentRoute().slice(1);
      try { r = decodeURIComponent(r); } catch (e) {}
      const [cid, uid] = r.split('/');
      const c = comps.find((x) => x.id === cid);
      if (!c) { renderNav(null); renderOverview(); return; }
      if (curComp === c.id && main.querySelector('.unit')) { scrollToUnit(c, uid); return; }
      renderNav(c.id, uid);
      renderComponent(c, uid);
    }
    function onHash() {
      leaveSearch();
      closeSource();
      route();
    }
    if (useHash) listen(win, 'hashchange', onHash);
    function go(h) {
      h = String(h || '#overview');
      if (h[0] !== '#') h = '#' + h;
      if (useHash) {
        if (win.location.hash === h) onHash(); else win.location.hash = h;
        return;
      }
      stateRoute = h;
      onHash();
    }
    // In state mode in-page links must not reach location.
    listen(R, 'click', (e) => {
      const rp = e.target.closest('.hc-rpill');
      if (rp) { opts.refresh(); return; }
      if (useHash || e.defaultPrevented) return;
      const a = e.target.closest('a[href^="#"]');
      if (!a) return;
      e.preventDefault();
      if (a.getAttribute('href').length > 1) go(a.getAttribute('href'));
    });

    // ---- what a click or a press on a pointer means
    function pointerOf(el) {
      const src = el.dataset.file ? { file: el.dataset.file, start: +el.dataset.start, end: +el.dataset.end, match: el.dataset.match } : null;
      const sec = el.closest('.unit'), sr = el.closest('.sr');
      const key = sec ? sec.dataset.key : (sr && sr.dataset.unit) || '';
      const slash = key.indexOf('/');
      const unit = key ? { cid: key.slice(0, slash), uid: key.slice(slash + 1) } : null;
      const ln = el.closest('.ln');
      const line = ln && ln.dataset.i != null ? +ln.dataset.i : null;
      return { src, ctx: { trace: el.dataset.trace || '', unit, line } };
    }
    let askedAt = 0;
    function pointAt(el) {
      if (Date.now() - askedAt < 700) return;  // the release after a long press
      if (opts.open) { const p = pointerOf(el); opts.open(p.src, p.ctx); } else openSource(el);
    }

    // ---- clicks in main
    listen(main, 'click', (e) => {
      const chip = e.target.closest('.nchip');
      if (chip) { e.stopPropagation(); chip.closest('.ln').classList.toggle('shownote'); return; }
      const sn = e.target.closest('.shownotes');
      if (sn) {
        const lns = sn.closest('.unit').querySelectorAll('.ln');
        const on = sn.textContent === 'Show notes';
        lns.forEach((l) => { if (l.querySelector('.note')) l.classList.toggle('shownote', on); });
        sn.textContent = on ? 'Hide notes' : 'Show notes';
        return;
      }
      if (e.target.classList.contains('hc-more')) {
        const b = e.target, open = b.textContent === 'More';
        const ld = main.querySelector('.lede'), wf = main.querySelector('.whatfull');
        if (b.dataset.full === '1') ld.textContent = open ? O.what : lede(O).text;
        else if (open) ld.insertAdjacentHTML('afterend', '<p class="whatfull">' + esc(O.what) + '</p>');
        else if (wf) wf.remove();
        b.textContent = open ? 'Less' : 'More';
        return;
      }
      if (e.target.classList.contains('hc-alls')) {
        main.querySelector('.wk').innerHTML = (O.surprises || []).map(surpriseRow).join('');
        e.target.remove();
        return;
      }
      const tog = e.target.closest('[data-tog]');
      if (tog) { const x = tog.nextElementSibling; x.hidden = !x.hidden; return; }
      const t = e.target.closest('[data-file]');
      if (!t || e.target.closest('.note')) return;
      e.preventDefault();
      pointAt(t);
    });
    listen(main, 'keydown', (e) => {
      if (e.key === 'Enter' && e.target.classList && e.target.classList.contains('ln') && e.target.dataset.file) { e.preventDefault(); pointAt(e.target); }
      if (e.key === 'Enter' && e.target.classList && e.target.classList.contains('node') && !e.target.getAttribute('href')) e.preventDefault();
    });

    // ---- ask about a line: long press on touch, context menu with a mouse
    if (opts.ask) {
      R.classList.add('hc-askable');
      let timer = 0, sx = 0, sy = 0;
      const cancel = () => { clearTimeout(timer); timer = 0; };
      const ask = (ln) => {
        // Android raises contextmenu for the same long press our timer catches; take the first.
        if (Date.now() - askedAt < 800) return;
        askedAt = Date.now();
        const sec = ln.closest('.unit');
        const hit = sec && unitById[sec.dataset.key];
        if (!hit) return;
        const l = ln.dataset.i != null ? hit.u.lines[+ln.dataset.i] : { text: hit.u.signature, src: hit.u.src };
        const p = pointerOf(ln);
        opts.ask(String(l.text || '').trim(), p.src, p.ctx);
      };
      listen(main, 'pointerdown', (e) => {
        if (e.pointerType !== 'touch') return;
        const ln = e.target.closest('.ln');
        if (!ln) return;
        sx = e.clientX; sy = e.clientY;
        cancel();
        timer = setTimeout(() => { timer = 0; ask(ln); }, 500);
      });
      listen(main, 'pointermove', (e) => { if (timer && Math.hypot(e.clientX - sx, e.clientY - sy) > 8) cancel(); });
      listen(main, 'pointerup', cancel);
      listen(main, 'pointercancel', cancel);
      listen(main, 'contextmenu', (e) => {
        const ln = e.target.closest('.ln');
        if (!ln) return;
        e.preventDefault();
        cancel();
        ask(ln);
      });
    }

    // ---- source view (built in; a host with opts.open never shows it)
    let cur = null;
    let savedScroll = 0;
    // Per file, the texts read so far: [{first, lines, part}]; a whole file (first absent) answers any range.
    const fileLines = {};
    const chunkFor = (file, s) => (fileLines[file] || []).find((c) => !c.part || (c.first <= s && s < c.first + c.lines.length));
    function srcHtml() {
      const { file, before, after, whole, all, match } = cur;
      const ch = chunkFor(file, cur.start);
      if (!ch) return '<div class="srcnote">' + esc(file) + (cur.failed ? ' could not be read.' : ' is not embedded in this page.') + '</div>';
      const lines = ch.lines, F = ch.first, N = F + lines.length - 1;
      // a provider reading another version of the file may hand back fewer lines than the pinned range
      const start = Math.max(F, Math.min(cur.start, N)), end = Math.max(F, Math.min(cur.end, N));
      const a = whole ? F : Math.max(F, start - before);
      const b = whole ? N : Math.min(N, end + after);
      // an excerpt may end inside a long range: the cap is decided on the whole range, "show all" only when it is all here
      const hitN = (ch.part ? cur.end : end) - start + 1;
      const cap = !whole && !all && hitN > 80;
      const capEnd = cap ? start + 39 : end;
      const marks = {};
      if (match) {
        const joined = lines.slice(start - F, end - F + 1).join('\n');
        const k = joined.indexOf(match);
        if (k >= 0) {
          let off = 0;
          for (let i = start; i <= end; i++) {
            const len = lines[i - F].length;
            const a0 = Math.max(k, off), b0 = Math.min(k + match.length, off + len);
            if (b0 > a0) marks[i] = [[a0 - off, b0 - off]];
            off += len + 1;
          }
        }
      }
      const mode = modeOf(file);
      const st = { tq: null };
      if (mode === 'py') for (let i = F; i < a; i++) tokens(lines[i - F], mode, st);
      let h = '<pre class="sv">';
      const cut = cap && ch.part && cur.end > N;
      if (ch.part) h += '<div class="excerpt">' + (cut ? 'First 40 of ' + hitN + ' lines are in the page' : 'Lines ' + F + '–' + N + ' of this file are in the page') + '</div>';
      if (a > F) h += '<div class="more"><button data-more="before">… ' + (a - F) + ' lines above</button></div>';
      const lastShown = cap ? capEnd : b;
      for (let i = a; i <= lastShown; i++) {
        h += '<div class="l' + (i >= start && i <= end ? ' hit' : '') + '"><span class="n">' + i + '</span><span class="c">' + paintTokens(tokens(lines[i - F], mode, st), marks[i]) + '</span></div>';
      }
      if (cap && !cut) h += '<div class="more"><button data-more="all">Showing 40 of ' + hitN + ' selected lines · show all</button></div>';
      else if (b < N) h += '<div class="more"><button data-more="after">… ' + (N - b) + ' lines below</button></div>';
      return h + '</pre>';
    }
    function paintSource(scroll) {
      srcBody.innerHTML = srcHtml();
      srcBody.style.setProperty('--hc-bw', srcBody.clientWidth + 'px');
      wholeBtn.innerHTML = cur.whole ? ICON.range : ICON.file;
      const ch = chunkFor(cur.file, cur.start);
      wholeBtn.title = cur.whole ? 'Show the range' : ch && ch.part ? 'All embedded lines' : 'Whole file';
      if (scroll) {
        const hit = srcBody.querySelector('.hit');
        srcBody.scrollTop = hit && cur.whole ? Math.max(0, hit.offsetTop - srcBody.clientHeight / 3) : 0;
      }
    }
    // Paint now when the file is already read, else when the provider answers (if this is still the open source).
    function loadAndPaint() {
      const want = cur;
      if (chunkFor(want.file, want.start)) { paintSource(true); return; }
      srcBody.innerHTML = '';
      Promise.resolve()
        .then(() => sources.read(want.file, { ref: D.commit || '', start: want.start, end: want.end }))
        .then((r) => {
          if (!r || typeof r.text !== 'string') return;
          const part = typeof r.first === 'number';
          const c = { first: part ? r.first : 1, lines: r.text.split('\n'), part };
          // a whole file replaces whatever was read before; an excerpt joins the others
          fileLines[want.file] = part ? (fileLines[want.file] || []).filter((x) => x.part).concat(c) : [c];
        }, () => { want.failed = true; })
        .then(() => { if (cur === want) paintSource(true); });
    }
    function openSource(el) {
      const a = cur ? null : anchorOf();
      if (!cur) savedScroll = main.scrollTop;
      R.querySelectorAll('.ln.open').forEach((x) => x.classList.remove('open'));
      const ln = el.closest('.ln');
      if (ln) ln.classList.add('open');
      const lnTop = ln ? ln.getBoundingClientRect().top : 0;
      cur = { file: el.dataset.file, start: +el.dataset.start, end: +el.dataset.end, match: el.dataset.match, before: 4, after: 4, whole: false, all: false };
      q('.hc-srcpath').textContent = cur.file;
      q('.hc-srcrange').textContent = ':' + cur.start + (cur.end !== cur.start ? '-' + cur.end : '');
      const tr = q('.hc-trace');
      tr.innerHTML = '<span>' + esc((el.dataset.trace || '').trim()) + '</span>';
      tr.hidden = !el.dataset.trace;
      srcEl.classList.add('on');
      if (phone()) backdrop.classList.add('on');
      else if (main.clientWidth < 480) R.classList.add('navoff');
      loadAndPaint();
      if (ln && phone()) {
        // the sheet covers the lower 72%: bring the clicked line under the header
        const RR = R.getBoundingClientRect();
        const want = main.getBoundingClientRect().top + 12, r = ln.getBoundingClientRect();
        if (r.top < want || r.bottom > RR.top + RR.height * 0.28) main.scrollTop += r.top - want;
      } else if (ln) main.scrollTop += ln.getBoundingClientRect().top - lnTop;
      else keepAnchor(a);
    }
    function closeSource() {
      if (!cur) return;
      const wasPhone = phone();
      const a = wasPhone ? null : anchorOf();
      srcEl.classList.remove('on');
      backdrop.classList.remove('on');
      R.classList.remove('navoff');
      closeNav();
      if (wasPhone) main.scrollTop = savedScroll; else keepAnchor(a);
      R.querySelectorAll('.ln.open').forEach((x) => x.classList.remove('open'));
      cur = null;
    }
    listen(srcBody, 'click', (e) => {
      const m = e.target.closest('[data-more]');
      if (!m || !cur) return;
      const k = m.dataset.more;
      const keep = srcBody.scrollTop, hBefore = srcBody.scrollHeight;
      if (k === 'before') cur.before += 30; else if (k === 'after') cur.after += 30; else cur.all = true;
      paintSource(false);
      if (k === 'before') srcBody.scrollTop = keep + (srcBody.scrollHeight - hBefore);
    });
    wholeBtn.onclick = () => { if (cur) { cur.whole = !cur.whole; paintSource(true); } };
    q('.hc-close').onclick = closeSource;
    const bodyRO = new win.ResizeObserver(() => { if (cur) srcBody.style.setProperty('--hc-bw', srcBody.clientWidth + 'px'); });
    bodyRO.observe(srcBody);
    backdrop.onclick = closeSource;
    q('.hc-copy').onclick = () => {
      if (!cur) return;
      const t = cur.file + ':' + cur.start;
      try { win.navigator.clipboard.writeText(t).catch(() => {}); } catch (e) {}
    };

    // ---- host-driven marks
    function paintPill() {
      R.querySelectorAll('.hc-opill, .hc-npill').forEach((slot) => {
        slot.innerHTML = pill ? '<button class="hc-rpill" title="' + esc(pill) + '">' + esc(pill) + '</button>' : '';
      });
    }
    function paintStale() {
      R.querySelectorAll('.hc-sc, .hc-stalechip').forEach((x) => x.remove());
      R.querySelectorAll('.stale').forEach((x) => x.classList.remove('stale'));
      if (!staleView) return;
      const S = staleView.res;
      nav.querySelectorAll('.nc').forEach((a) => {
        const n = [...S.units].filter((k) => k.slice(0, k.indexOf('/')) === a.dataset.c).length;
        if (!n) return;
        a.classList.add('stale');
        a.querySelector('.ct').insertAdjacentHTML('beforebegin', '<span class="hc-sc" title="' + n + ' unit' + (n === 1 ? '' : 's') + ' changed">' + n + '</span>');
      });
      main.querySelectorAll('.unit').forEach((sec) => {
        const key = sec.dataset.key;
        if (!S.units.has(key)) return;
        const text = S.gone.has(key) ? 'file removed' : 'changed since ' + staleView.base;
        sec.querySelector('.uhead h3').insertAdjacentHTML('afterend', '<span class="chip hc-stalechip">' + esc(text) + '</span>');
        const lines = S.lines.get(key);
        if (lines) sec.querySelectorAll('.ln[data-i]').forEach((ln) => { if (lines.has(+ln.dataset.i)) ln.classList.add('stale'); });
      });
    }

    if (useHash) R.focus({ preventScroll: true });  // the page is ours: "/" works before the first click
    route();

    let dead = false;
    return {
      setStale(data) {
        if (dead) return;
        staleView = data && data.base_found !== false && data.files
          ? { res: stale(D, data.files), base: data.base || String(D.commit || '').slice(0, 7) }
          : null;
        paintStale();
      },
      setPill(text) {
        if (dead) return;
        pill = text || null;
        paintPill();
      },
      go(h) { if (!dead) go(h); },
      state() { return { route: currentRoute(), scroll: main.scrollTop }; },
      restore(s) {
        if (dead || !s) return;
        if (useHash) { if (win.location.hash !== s.route) win.history.replaceState(win.history.state, '', s.route); onHash(); } else go(s.route);
        main.scrollTop = s.scroll || 0;
      },
      destroy() {
        if (dead) return;
        dead = true;
        closeSource();
        cleanups.forEach((f) => f());
        if (dagRO) dagRO.disconnect();
        mainRO.disconnect();
        bodyRO.disconnect();
        clearTimeout(dagTimer); clearTimeout(noteTimer);
        host.innerHTML = '';
      },
    };
  }

  HC.mount = mount;
  HC.embedded = embedded;
  HC.stale = stale;
  HC.remap = remap;
  HC.tokens = tokens;
  HC.dagModel = dagModel;
  HC.lede = lede;
  root.HumanCode = HC;
})(typeof window !== 'undefined' ? window : globalThis);
