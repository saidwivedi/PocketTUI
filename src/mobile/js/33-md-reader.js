// ============================================================
// Markdown reader
// ============================================================
// A .md file opens rendered rather than in the editor: notes and READMEs are
// read far more often than they are changed, and Edit is one tap away.
//
// The renderer is hand-rolled and builds its output with createElement and
// textContent only — never innerHTML on a byte of the file. That is the whole
// sanitization story, and it has to stay that way: the pairing token lives on
// this origin, so a script tag written in someone's notes has to reach the page
// as the characters it is and nothing else.

// The file on screen, and what Edit hands the editor.
let readerPath = "";

// ---- block structure -------------------------------------------------------

const MD_FENCE = /^( {0,3})(```+|~~~+)[ \t]*([^\s`]*)/;
const MD_HEADING = /^ {0,3}(#{1,6})(?:[ \t]+(.*?))?[ \t]*#*[ \t]*$/;
const MD_RULE = /^ {0,3}(?:\*[ \t]*){3,}$|^ {0,3}(?:-[ \t]*){3,}$|^ {0,3}(?:_[ \t]*){3,}$/;
const MD_QUOTE = /^ {0,3}> ?/;
const MD_BULLET = /^([ \t]*)([-*+])([ \t]+)(.*)$/;
const MD_ORDERED = /^([ \t]*)(\d{1,9})([.)])([ \t]+)(.*)$/;
const MD_CODE_INDENT = /^(?: {4}|\t)/;
const MD_TASK = /^\[([ xX])\][ \t]+/;
// A "$$" at the head of a line opens display math, however far down the
// closing "$$" turns out to be.
const MD_MATH_OPEN = /^ {0,3}\$\$/;
// A fence's info string ends up in a class name and nowhere else, but the
// class is still built from file bytes, so only a plain word is taken.
const MD_LANG = /^[\w.+-]+$/;

// Renders a whole document into a fragment the caller appends in one go.
function mdRender(src) {
  const frag = document.createDocumentFragment();
  mdBlocks(src.replace(/\r\n?/g, "\n").split("\n"), frag);
  return frag;
}

// Drops up to n leading spaces — the dedent every nested construct needs, and
// the reason a nested list, a quoted quote and an indented fence all come back
// through mdBlocks as ordinary top-level markdown.
function mdStrip(line, n) {
  let i = 0;
  while (i < n && line[i] === " ") i++;
  return line.slice(i);
}

function mdIndent(line) {
  return line.length - line.replace(/^[ \t]+/, "").length;
}

function mdBlocks(lines, parent) {
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i++; continue; }

    const fence = line.match(MD_FENCE);
    if (fence) { i = mdFence(lines, i, fence, parent); continue; }

    const head = line.match(MD_HEADING);
    if (head) {
      parent.appendChild(mdInlineInto(el("h" + head[1].length), head[2] || ""));
      i++;
      continue;
    }

    if (MD_RULE.test(line)) { parent.appendChild(el("hr")); i++; continue; }
    if (MD_QUOTE.test(line)) { i = mdQuote(lines, i, parent); continue; }
    if (mdMarker(line)) { i = mdList(lines, i, parent); continue; }
    if (mdIsTable(lines, i)) { i = mdTable(lines, i, parent); continue; }
    if (MD_MATH_OPEN.test(line)) { i = mdMathBlock(lines, i, parent); continue; }
    if (MD_CODE_INDENT.test(line)) { i = mdCodeIndent(lines, i, parent); continue; }

    i = mdParagraph(lines, i, parent);
  }
}

// Everything that ends a paragraph by starting something of its own. Indented
// code is deliberately absent: a line indented under a running paragraph is a
// continuation of it, not a code block.
function mdStartsBlock(lines, i) {
  const line = lines[i];
  return MD_FENCE.test(line) || MD_HEADING.test(line) || MD_RULE.test(line)
      || MD_QUOTE.test(line) || !!mdMarker(line) || mdIsTable(lines, i)
      || MD_MATH_OPEN.test(line);
}

function mdCodeBlock(text, lang) {
  const code = el("code", lang && MD_LANG.test(lang) ? { class: "language-" + lang } : {});
  code.textContent = text;
  return el("pre", {}, code);
}

function mdFence(lines, i, m, parent) {
  const indent = m[1].length, marker = m[2];
  // A fence closes on a run of its own character at least as long as it is;
  // anything else, including the other fence character, is content.
  const close = new RegExp("^ {0,3}" + marker[0] + "{" + marker.length + ",}[ \t]*$");
  const body = [];
  let j = i + 1;
  for (; j < lines.length; j++) {
    if (close.test(lines[j])) { j++; break; }
    body.push(mdStrip(lines[j], indent));
  }
  parent.appendChild(mdCodeBlock(body.join("\n"), m[3]));
  return j;
}

function mdCodeIndent(lines, i, parent) {
  const body = [];
  let j = i, end = i;
  while (j < lines.length) {
    if (!lines[j].trim()) { body.push(""); j++; continue; }
    if (!MD_CODE_INDENT.test(lines[j])) break;
    body.push(mdStrip(lines[j].replace(/^\t/, "    "), 4));
    end = ++j;
  }
  // The blank lines past the last indented one belong to whatever follows.
  parent.appendChild(mdCodeBlock(body.slice(0, body.length - (j - end)).join("\n"), ""));
  return end;
}

function mdQuote(lines, i, parent) {
  const inner = [];
  let j = i;
  for (; j < lines.length && MD_QUOTE.test(lines[j]); j++) {
    inner.push(lines[j].replace(MD_QUOTE, ""));
  }
  const quote = el("blockquote");
  // Nesting costs nothing: a ">>" line arrives here as a ">" line.
  mdBlocks(inner, quote);
  parent.appendChild(quote);
  return j;
}

// ---- lists -----------------------------------------------------------------

// A list item's opening line, split into the parts the item's own body is
// measured against: where the marker starts, and how far in its content sits.
function mdMarker(line) {
  let m = line.match(MD_BULLET);
  if (m) {
    return { indent: m[1].length, bullet: true, text: m[4],
             content: m[1].length + 1 + m[3].length, start: 1 };
  }
  m = line.match(MD_ORDERED);
  if (m) {
    return { indent: m[1].length, bullet: false, text: m[5],
             content: m[1].length + m[2].length + 1 + m[4].length,
             start: parseInt(m[2], 10) };
  }
  return null;
}

function mdList(lines, i, parent) {
  const first = mdMarker(lines[i]);
  const base = first.indent;
  const list = el(first.bullet ? "ul" : "ol");
  if (!first.bullet && first.start !== 1) list.setAttribute("start", first.start);
  let j = i;
  while (j < lines.length) {
    const m = mdMarker(lines[j]);
    // A marker indented past this list's own is content of the item above,
    // and one indented less closes the list back out to its parent.
    if (!m || m.indent < base || m.indent > base + 3) break;
    j = mdItem(lines, j, m, base, list);
  }
  parent.appendChild(list);
  return j;
}

function mdItem(lines, i, m, base, list) {
  const body = [m.text];
  let j = i + 1;
  while (j < lines.length) {
    const line = lines[j];
    if (!line.trim()) {
      // A blank line only ends the item if nothing indented under it follows —
      // otherwise this is a loose list and the item runs on.
      let k = j;
      while (k < lines.length && !lines[k].trim()) k++;
      const next = k < lines.length ? mdMarker(lines[k]) : null;
      if (k >= lines.length
          || !(mdIndent(lines[k]) >= m.content || (next && next.indent >= base))) break;
      body.push("");
      j++;
      continue;
    }
    // Indentation decides before the marker does: a "-" indented to this
    // item's content column opens a nested list inside it, while the same
    // "-" a column short is the next item of this list or an outer one.
    if (mdIndent(line) >= m.content) { body.push(mdStrip(line, m.content)); j++; continue; }
    if (mdMarker(line)) break;
    // A line under-indented but still inside a running paragraph is the lazy
    // continuation every hand-wrapped list writes.
    if (!body[body.length - 1]) break;
    body.push(line.trim());
    j++;
  }

  const item = el("li");
  const task = body[0].match(MD_TASK);
  if (task) {
    body[0] = body[0].slice(task[0].length);
    item.className = "md-task";
    const box = el("input", { type: "checkbox", disabled: "" });
    if (task[1] !== " ") box.setAttribute("checked", "");
    item.appendChild(box);
  }

  const box = el("div");
  mdBlocks(body, box);
  // Tight by default: an item whose text parsed to a single paragraph hands
  // that text straight to the <li>, so a list of one-liners does not read as a
  // stack of paragraphs. An item with two of them keeps both.
  const kids = Array.prototype.slice.call(box.children);
  if (kids.length && kids[0].tagName === "P"
      && !kids.slice(1).some(k => k.tagName === "P")) {
    while (kids[0].firstChild) box.insertBefore(kids[0].firstChild, kids[0]);
    box.removeChild(kids[0]);
  }
  while (box.firstChild) item.appendChild(box.firstChild);
  list.appendChild(item);
  return j;
}

// ---- tables ----------------------------------------------------------------

function mdCells(line) {
  let s = line.trim();
  if (s.startsWith("|")) s = s.slice(1);
  if (s.endsWith("|")) s = s.slice(0, -1);
  return s.split("|").map(c => c.trim());
}

// The separator row is what makes a table a table — every cell dashes, with
// the colons that set a column's alignment. The "|" is required so a setext
// underline or a horizontal rule is never read as one.
function mdSepCells(line) {
  if (line.indexOf("|") < 0 || !/^[\s|:-]+$/.test(line) || line.indexOf("-") < 0) return null;
  const cells = mdCells(line);
  return cells.length && cells.every(c => /^:?-+:?$/.test(c)) ? cells : null;
}

// A header row (which has to carry a pipe of its own) over a separator row.
function mdIsTable(lines, i) {
  return lines[i].indexOf("|") >= 0 && !!mdSepCells(lines[i + 1] || "");
}

function mdAlign(cell) {
  const left = cell.startsWith(":"), right = cell.endsWith(":");
  if (left && right) return "center";
  return right ? "right" : (left ? "left" : "");
}

function mdRow(cells, aligns, tag) {
  const tr = el("tr");
  cells.forEach((text, n) => {
    const cell = mdInlineInto(el(tag), text);
    if (aligns[n]) cell.style.textAlign = aligns[n];
    tr.appendChild(cell);
  });
  return tr;
}

function mdTable(lines, i, parent) {
  const aligns = mdSepCells(lines[i + 1]).map(mdAlign);
  const table = el("table");
  const head = el("thead");
  head.appendChild(mdRow(mdCells(lines[i]), aligns, "th"));
  table.appendChild(head);
  const body = el("tbody");
  let j = i + 2;
  for (; j < lines.length && lines[j].trim() && lines[j].indexOf("|") >= 0; j++) {
    body.appendChild(mdRow(mdCells(lines[j]), aligns, "td"));
  }
  table.appendChild(body);
  // A wide table scrolls inside its own box; the page itself never does.
  parent.appendChild(el("div", { class: "md-table" }, table));
  return j;
}

// ---- math ------------------------------------------------------------------

// Math is parsed, never typeset, here: the node carries its TeX as text and
// mdTypeset hands it to KaTeX once the document is on screen. That makes the
// unrendered node the fallback too — if the vendor bundle never arrives the
// reader shows the source the file actually contains, which is a good deal
// better than the mangled emphasis it used to show.
function mdMathNode(tex, block) {
  const node = el(block ? "div" : "span", { class: block ? "md-math-block" : "md-math" });
  node.textContent = tex;
  return node;
}

// The closing "$$" may sit on the opening line, on a line of its own, or at
// the end of the last line of the equation. A run that never closes ends the
// document, the way an unclosed fence does.
function mdMathBlock(lines, i, parent) {
  const body = [];
  let j = i, rest = lines[i].replace(MD_MATH_OPEN, "");
  for (;;) {
    const end = rest.indexOf("$$");
    if (end >= 0) { body.push(rest.slice(0, end)); j++; break; }
    body.push(rest);
    if (++j >= lines.length) break;
    rest = lines[j];
  }
  parent.appendChild(mdMathNode(body.join("\n").trim(), true));
  return j;
}

function mdParagraph(lines, i, parent) {
  const buf = [];
  let j = i;
  while (j < lines.length) {
    if (!lines[j].trim()) { j++; break; }
    if (j > i && mdStartsBlock(lines, j)) break;
    buf.push(lines[j].trim());
    j++;
  }
  parent.appendChild(mdInlineInto(el("p"), buf.join(" ")));
  return j;
}

// ---- inline spans ----------------------------------------------------------

// An emphasis run opened with "_" must not start or end inside a word, or
// every snake_case_name in a README turns italic halfway through.
function mdWordSafe(m, text, i) {
  if (m[1][0] !== "_") return true;
  return !/\w/.test(text[i - 1] || "") && !/\w/.test(text[i + m[0].length] || "");
}

// Tried in this order at each position, first match wins. Code comes first
// because its content is literal, so nothing inside a span of it is markup.
// Math comes straight after it, ahead of everything that reads punctuation as
// markup: a subscript-heavy equation is nothing but the "_" and "*" the
// emphasis rules would otherwise eat.
// A label's bracket alternative is listed ahead of the plain-character one so
// that the badge shape a README opens with — an image inside a link — keeps
// its inner [..] together instead of ending the label at the first "]".
// Every builder answers with the node it made and how much source it ate;
// only the bare URL eats less than it matched, having handed back the
// punctuation the surrounding sentence wrapped around it.
const MD_SPANS = [
  { re: /(`+)([\s\S]+?)\1(?!`)/y,
    build: (m) => {
      const code = el("code");
      code.textContent = m[2];
      return { node: code, len: m[0].length };
    } },
  { re: /\$\$(?=[^\s$])([^\n$]*[^\s$])\$\$/y,
    build: (m) => ({ node: mdMathNode(m[1], true), len: m[0].length }) },
  // Pandoc's guards, and they earn their keep in prose: the opening "$" has to
  // be against its content and the closing one has to be too, and a closer
  // followed by a digit is a second price rather than the end of an equation.
  // That is what keeps "costs $5 and $10 total" a sentence.
  { re: /\$(?=[^\s$])([^\n$]*[^\s$])\$(?!\d)/y,
    build: (m) => ({ node: mdMathNode(m[1], false), len: m[0].length }) },
  { re: /!\[((?:\[[^\]]*\]|[^\]])*)\]\([ \t]*([^()\s]*(?:\([^()]*\)[^()\s]*)*)(?:[ \t]+"[^"]*")?[ \t]*\)/y,
    build: (m) => ({ node: mdImage(m[1], m[2]), len: m[0].length }) },
  { re: /\[((?:\[[^\]]*\]|[^\]])*)\]\([ \t]*([^()\s]*(?:\([^()]*\)[^()\s]*)*)(?:[ \t]+"[^"]*")?[ \t]*\)/y,
    build: (m) => ({ node: mdAnchor(m[1], m[2]), len: m[0].length }) },
  { re: /(\*\*|__)(?=\S)([\s\S]*?\S)\1/y, ok: mdWordSafe,
    build: (m) => ({ node: mdInlineInto(el("strong"), m[2]), len: m[0].length }) },
  { re: /(\*|_)(?=\S)([\s\S]*?\S)\1/y, ok: mdWordSafe,
    build: (m) => ({ node: mdInlineInto(el("em"), m[2]), len: m[0].length }) },
  { re: /~~(?=\S)([\s\S]*?\S)~~/y,
    build: (m) => ({ node: mdInlineInto(el("s"), m[1]), len: m[0].length }) },
  { re: /https?:\/\/[^\s<>"'`]+/y,
    build: (m) => {
      // trimUrl (08-links.js) is the terminal's own answer to a URL a
      // sentence closed a bracket around.
      const url = trimUrl(m[0]);
      if (!url) return null;
      const a = mdLink(url);
      // Its own label, and literally so: reading it back through mdInline
      // would only find this same span again.
      a.textContent = url;
      return { node: a, len: url.length };
    } },
];

function mdSpan(text, i) {
  for (const span of MD_SPANS) {
    span.re.lastIndex = i;
    const m = span.re.exec(text);
    if (!m || (span.ok && !span.ok(m, text, i))) continue;
    const hit = span.build(m);
    if (hit) return hit;
  }
  return null;
}

function mdInline(text, parent) {
  let plain = "";
  const flush = () => {
    if (plain) { parent.appendChild(document.createTextNode(plain)); plain = ""; }
  };
  let i = 0;
  while (i < text.length) {
    if (text[i] === "\\" && /[\\`*_~[\]()#+\-.!|>$]/.test(text[i + 1] || "")) {
      plain += text[i + 1];
      i += 2;
      continue;
    }
    const hit = mdSpan(text, i);
    if (!hit) { plain += text[i]; i++; continue; }
    flush();
    parent.appendChild(hit.node);
    i += hit.len;
  }
  flush();
}

function mdInlineInto(node, text) {
  mdInline(text, node);
  return node;
}

// A label that is not becoming a link still carries its own inline markup.
function mdText(label) {
  const frag = document.createDocumentFragment();
  mdInline(label, frag);
  return frag;
}

// The anchor both link forms share. It opens through openUrl() rather than
// the browser's own navigation, exactly as the terminal's links do.
function mdLink(href) {
  return el("a", {
    href: href,
    onclick: (ev) => { ev.preventDefault(); openUrl(href); },
  });
}

// Only http(s) becomes a link at all. Every other scheme — javascript: above
// all — stays text, so a file can never hand this page something to run. A
// relative link is text too for now: there is nothing to navigate to yet.
function mdAnchor(label, href) {
  if (!/^https?:\/\//i.test(href)) return mdText(label);
  return mdInlineInto(mdLink(href), label);
}

// An http(s) src the tag fetches itself. A relative one names a file beside
// the markdown, which needs the signed-URL detour below; anything else (a
// data: or javascript: src) is not an image this reader will load, so the alt
// text stands in for it.
function mdImage(alt, src) {
  if (/^[a-z][a-z0-9+.-]*:/i.test(src) && !/^https?:\/\//i.test(src)) return mdText(alt);
  const img = el("img", { alt: alt });
  if (/^https?:\/\//i.test(src)) img.src = src;
  else mdSignImage(img, src);
  return img;
}

// The markdown file's own directory as the root for a relative src.
function mdResolve(base, rel) {
  if (rel.startsWith("/")) return rel;
  const parts = base.split("/").slice(0, -1);
  for (const seg of rel.split("/")) {
    if (!seg || seg === ".") continue;
    if (seg === "..") { if (parts.length > 1) parts.pop(); continue; }
    parts.push(seg);
  }
  return parts.join("/") || "/";
}

// An <img> carries no pairing header, so a local image takes fillViewer's
// route: an authenticated mint hands back a short-lived signed URL the tag can
// fetch on its own. Started rather than awaited — the document renders now and
// each image arrives when its mint does. A failed mint says nothing: the alt
// text is already on screen, and a broken image in someone's notes is not
// worth a toast over the text they came to read.
async function mdSignImage(img, src) {
  try {
    const path = mdResolve(readerPath, src);
    const r = await fetch(apiURL("api/file_link?path=" + encodeURIComponent(path)),
                          { cache: "no-store", headers: authHeaders() });
    if (!r.ok) return;
    img.src = apiURL((await r.json()).url);
  } catch (e) { /* the alt text is what a missing image says */ }
}

// ---- typesetting -----------------------------------------------------------

let katexLoading = null;

// The same lazy-vendor arrangement the editor makes for CodeMirror: script and
// stylesheet go in once, on the first document that has an equation in it, and
// a README with no math never asks for either.
function ensureKaTeX() {
  if (window.katex) return Promise.resolve();
  if (katexLoading) return katexLoading;
  katexLoading = new Promise((resolve, reject) => {
    const css = document.createElement("link");
    css.rel = "stylesheet";
    css.href = "vendor/katex/katex.min.css?v=" + buildVersion();
    document.head.appendChild(css);
    const s = document.createElement("script");
    s.src = "vendor/katex/katex.min.js?v=" + buildVersion();
    s.onload = resolve;
    s.onerror = () => {
      katexLoading = null;
      s.remove();
      css.remove();
      reject(new Error("vendor load failed"));
    };
    document.head.appendChild(s);
  });
  return katexLoading;
}

// Started rather than awaited, like the images: the document is already
// readable and each equation replaces its own source when KaTeX is here.
// This is the one place markup the file influenced is written as markup, and
// it is KaTeX that writes it — from TeX it has parsed itself, with the default
// trust:false, so a \href or \htmlClass in someone's notes is refused rather
// than honoured. throwOnError:false keeps a typo to its own red equation
// instead of losing the rest of the document to it.
async function mdTypeset(root) {
  const nodes = root.querySelectorAll(".md-math, .md-math-block");
  if (!nodes.length) return;
  try { await ensureKaTeX(); } catch (e) { return; }
  nodes.forEach((node) => {
    katex.render(node.textContent, node, {
      throwOnError: false,
      displayMode: node.classList.contains("md-math-block"),
    });
  });
}

// ---- the reader screen -----------------------------------------------------

async function openReader(path) {
  const data = await fsReadText(path);
  if (!data) return;
  readerPath = path;
  $("reader-filename").textContent = baseName(path);
  const body = $("reader-body");
  // Emptying an element is the one thing innerHTML is allowed to do here; not
  // one byte of the file ever goes through it.
  body.innerHTML = "";
  body.appendChild(mdRender(data.content));
  mdTypeset(body);
  $("reader-scroll").scrollTop = 0;
  $("screen-files").classList.remove("active");
  $("screen-reader").classList.add("active");
  history.pushState({ reader: true }, "", location.href);
  if (data.lossy) toast("Not valid UTF-8 — some characters are missing");
}

function closeReader() {
  $("screen-reader").classList.remove("active");
  $("screen-files").classList.add("active");
  $("reader-body").innerHTML = "";
  readerPath = "";
}

// ---- putting the page away (see fileViews in 09-image-viewer.js) -----------
// A rail switch stashes the rendered document itself rather than the path to
// re-read: the file may be rewritten while the session is away, and a view put
// away and brought back has to be the page that was left — down to where it
// was scrolled to. Keeping the nodes also keeps whatever typesetting ran over
// them, which a re-render would have to do again.

function readerStash() {
  const body = $("reader-body");
  // Where the reader is scrolled to, read before the nodes move: taking them
  // out collapses the box, and the offset would already be back at zero.
  const scroll = $("reader-scroll").scrollTop;
  const page = document.createDocumentFragment();
  while (body.firstChild) page.appendChild(body.firstChild);
  const s = { path: readerPath, scroll: scroll, page: page };
  readerPath = "";
  $("screen-reader").classList.remove("active");
  return s;
}

function readerRestore(s) {
  readerPath = s.path;
  $("reader-filename").textContent = baseName(readerPath);
  const body = $("reader-body");
  body.innerHTML = "";
  body.appendChild(s.page);
  $("screen-files").classList.remove("active");
  $("screen-reader").classList.add("active");
  // After the class, and after a layout has actually been computed from it: the
  // screen was display:none a statement ago, and a scrollTop set against a box
  // that has no height yet is silently dropped.
  const box = $("reader-scroll");
  void box.scrollHeight;
  box.scrollTop = s.scroll;
}

$("btn-reader-back").addEventListener("click", () => history.back());

// The editor takes the screen the reader is holding, and its history entry
// with it: one entry serves whichever of the two is up, so back from the
// editor lands on the file list either way. The reader stays visible until
// the editor is actually there — openEditor can still fail on its own read,
// and a failure must not leave the screen empty.
$("btn-reader-edit").addEventListener("click", async () => {
  await openEditor(readerPath, { noHistory: true });
  if ($("screen-editor").classList.contains("active")) {
    $("screen-reader").classList.remove("active");
    $("reader-body").innerHTML = "";
    readerPath = "";
  }
});
