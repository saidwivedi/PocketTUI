// ============================================================
// Image paths and URLs as links
// ============================================================
// Anchored at / or ~ so a bare "logo.png" in prose is not a link, and stopped by
// whitespace and the punctuation that usually brackets a path in output.
const IMAGE_PATH_RE = /(?:~|\/)[^\s"'`()\[\]{}<>:;,]*\.(?:png|jpe?g|gif|webp|svg|bmp|mp4|webm|mov)\b/gi;

// http(s) only. Everything up to whitespace or a quote/angle is taken, and the
// trailing junk a terminal wraps around a URL is trimmed off afterwards by
// trimUrl(), which needs to see the closers to balance them.
const URL_RE = /https?:\/\/[^\s"'`<>]+/gi;

// How far logicalLine() will walk in each direction, to bound the cost of the
// hit-test that runs on every hover and every tap.
const LOGICAL_MAX_ROWS = 8;

// True when a row fills its width edge to edge, which is how tmux's repaint looks
// for a wrapped line: it emits continuation rows as hard newlines, so isWrapped is
// false on them and fullness is the only signal left that the text runs on.
function rowIsFull(line, cols) {
  return !!line && line.translateToString(false, 0, cols).trimEnd().length === cols;
}

// A path printed at 45 columns almost always wraps, so matching one buffer row at
// a time would only ever see fragments. Walk out to the whole logical line and
// match that; the pieces are joined without trimming, so every row contributes
// exactly `cols` characters and an offset into the joined string maps back to a
// cell by division. Returns null when the run is not in the buffer (a row can be
// scrolled out between the hover and this call).
//
// Two rows join when the next one's isWrapped is set (a soft wrap, which is what
// locally-typed input produces) or when this one is full (tmux's hard-newline
// repaint). Joining too eagerly is harmless: the regex still needs a real /path.ext
// to produce a link, and it is anchored at / or ~ so a seam between two unrelated
// rows cannot invent one.
function logicalLine(buf, y, cols) {
  let first = y;
  for (let n = 0; n < LOGICAL_MAX_ROWS && first > 1; n++) {
    const prev = buf.getLine(first - 2), cur = buf.getLine(first - 1);
    if (!prev || !((cur && cur.isWrapped) || rowIsFull(prev, cols))) break;
    first--;
  }
  let text = "", last = first;
  for (;;) {
    const line = buf.getLine(last - 1);
    if (!line) return null;
    text += line.translateToString(false, 0, cols);
    const next = buf.getLine(last);
    if (!next || !(next.isWrapped || rowIsFull(line, cols))) break;
    if (last - first >= LOGICAL_MAX_ROWS) break;
    last++;
  }
  return { text, first, last };
}

// Offset in the joined string -> 1-based buffer cell. Exact because every row
// was translated untrimmed to the full column count.
function cellAt(offset, first, cols) {
  return { x: (offset % cols) + 1, y: first + Math.floor(offset / cols) };
}

// How many leading spaces a continuation row may hide at a seam. A program that
// wraps its own output (Claude Code's markdown, at pane width) indents the
// continuation by a couple of spaces, which lands mid-path and stops the regex.
const MAX_STITCH_INDENT = 4;

// The logical line rewritten for matching, with a map back to joined offsets.
// At each row seam whose previous row is edge-to-edge full and does not end in a
// space — the shape of a wrap, never of a sentence that merely filled the row —
// a leading run of spaces on the continuation row is dropped, so the two halves
// of a path meet. `map[i]` is the joined offset of stitched character i; the
// elided cells are exactly those the map skips, and the ranges built from it
// still cover them because they sit between two kept offsets.
function stitchedText(text, cols) {
  let out = "", map = [];
  for (let i = 0; i < text.length; i++) {
    if (i && i % cols === 0) {
      const rowEnd = text[i - 1];
      const full = text.slice(i - cols, i).trimEnd().length === cols;
      if (full && rowEnd !== " ") {
        let n = 0;
        while (n < MAX_STITCH_INDENT && text[i + n] === " ") n++;
        // All-space continuation rows are not indents; leave them alone.
        if (n && n < cols) i += n;
      }
    }
    out += text[i];
    map.push(i);
  }
  return { text: out, map };
}

// Every image path on the logical line through buffer row y, as {path, start, end}
// in 1-based buffer cells (end inclusive). Empty when the row is out of buffer.
//
// Matched over the joined text, then over the text cut back to each row boundary.
// The extra passes are what a wrong join costs: a path that ends exactly at a row
// edge is glued to whatever the next row starts with, and the trailing \b then
// rejects it ("…/plot.png" + "and then" reads as "…/plot.pnga"). Cutting at the
// boundary recovers that path, and a match the join found survives too, so a real
// wrapped path and a coincidentally-full row both come out right.
//
// The same passes run again over the stitched text, which is the only way an
// indent-wrapped path is ever whole. Matches from both come back as joined-offset
// ranges and compete: overlapping ones collapse to the longest, so the stitched
// full path swallows the bare "/000000.mp4" fragment its last row would match on
// its own, while a row that genuinely holds two paths keeps both.
function matchesOn(y, re, trim) {
  const buf = term.buffer.active, cols = term.cols;
  const logical = logicalLine(buf, y, cols);
  if (!logical) return [];

  const stitched = stitchedText(logical.text, cols);
  const found = [];
  const rows = logical.last - logical.first + 1;
  for (const src of [{ text: logical.text, map: null }, stitched]) {
    for (let r = rows; r >= 1; r--) {
      const text = src.text.slice(0, r * cols);
      re.lastIndex = 0;
      let m;
      while ((m = re.exec(text)) !== null) {
        const path = trim ? trim(m[0]) : m[0];
        if (!path) continue;
        const last = m.index + path.length - 1;
        found.push({
          path: path,
          from: src.map ? src.map[m.index] : m.index,
          to: src.map ? src.map[last] : last,
        });
      }
    }
  }

  // Longest first (by the cells it covers), then keep a match only where it does
  // not overlap one already kept.
  found.sort((a, b) => (b.to - b.from) - (a.to - a.from) || a.from - b.from);
  const kept = [];
  for (const f of found) {
    if (kept.some(k => f.from <= k.to && k.from <= f.to)) continue;
    kept.push(f);
  }

  return kept
    .sort((a, b) => a.from - b.from)
    .map(f => ({
      path: f.path,
      from: f.from,
      to: f.to,
      start: cellAt(f.from, logical.first, cols),
      // `to` is the last cell of the match, so the range is inclusive already.
      end: cellAt(f.to, logical.first, cols),
    }));
}

function imagePathsOn(y) {
  return matchesOn(y, IMAGE_PATH_RE, null);
}

function urlsOn(y) {
  return matchesOn(y, URL_RE, trimUrl);
}

// Closers and sentence punctuation the terminal wrapped around the URL rather
// than the URL carrying them. Trailing punctuation goes unconditionally; a
// closing bracket goes only when the URL has no matching opener, so a wiki link
// like /wiki/Foo_(bar) keeps its paren.
const URL_TRAIL_RE = /[.,;:!?'"]+$/;
const URL_CLOSERS = { ")": "(", "]": "[", "}": "{" };

function trimUrl(raw) {
  let url = raw;
  for (;;) {
    const trimmed = url.replace(URL_TRAIL_RE, "");
    const closer = URL_CLOSERS[trimmed.slice(-1)];
    if (closer) {
      const open = trimmed.split(closer).length - 1;
      const close = trimmed.split(trimmed.slice(-1)).length - 1;
      if (close > open) { url = trimmed.slice(0, -1); continue; }
    }
    if (trimmed === url) break;
    url = trimmed;
  }
  // Nothing past the scheme is not a link.
  return /^https?:\/\/[^\/]/i.test(url) ? url : "";
}

// Both kinds of link on a row, URLs first. A URL can hold a slash-anchored
// substring that IMAGE_PATH_RE also matches (https://x.com/a.png), so an image
// path inside a URL's cells is dropped and the whole URL wins; a real local path
// (~ or /) never overlaps one.
function linksOn(y) {
  const urls = urlsOn(y).map(f => ({ ...f, url: f.path }));
  const paths = imagePathsOn(y).filter(p => !urls.some(u => p.from <= u.to && u.from <= p.to));
  return urls.concat(paths).sort((a, b) => a.from - b.from);
}

function provideImageLinks(y, callback) {
  const links = linksOn(y)
    // xterm asks per row and drops what does not cover the row it asked about.
    .filter(f => f.start.y <= y && f.end.y >= y)
    .map(f => ({
      text: f.path,
      range: { start: f.start, end: f.end },
      activate: () => (f.url ? openUrl(f.url) : showImage(f.path)),
    }));
  callback(links.length ? links : undefined);
}

// The link at a 1-based cell, or null. A wrapped match spans rows, so the cell
// is compared as a linear position rather than per-row.
function linkAt(x, y) {
  const cols = term.cols;
  const pos = y * cols + x;
  return linksOn(y).find(f =>
    f.start.y * cols + f.start.x <= pos && pos <= f.end.y * cols + f.end.x) || null;
}

// A URL printed by a program running on the workstation names a host this phone
// cannot reach. cfg.backend, when set, is the phone's actual route to that
// workstation (e.g. a tailscale hostname), so it takes priority over the page's
// own host; loc is only the fallback for a same-origin deploy. The scheme is
// left alone: a dev server on cfg.backend may speak plain http while the page
// itself is https, and a top-level navigation to http is not blocked the way a
// subresource fetch would be. Port, path and query are the URL's own.
const LOCAL_HOSTS = ["localhost", "127.0.0.1", "[::1]", "::1", "0.0.0.0"];

function localizeUrl(raw, loc) {
  let u;
  try { u = new URL(raw); } catch (e) { return raw; }
  if (LOCAL_HOSTS.indexOf(u.hostname.toLowerCase()) === -1) return raw;
  let targetHost = loc.hostname;
  if (cfg.backend) {
    try { targetHost = new URL(cfg.backend).hostname; } catch (e) { targetHost = loc.hostname; }
  }
  if (!targetHost || LOCAL_HOSTS.indexOf(targetHost.toLowerCase()) !== -1) return raw;
  u.hostname = targetHost;
  return u.href;
}

// Opens in the phone's browser. Not window.open: Safari's noopener path opens
// a blank tab and returns null, so we go straight for a synchronous anchor
// click, which iOS still counts as a user gesture from this handler.
function openUrl(raw) {
  const url = localizeUrl(raw, location);
  const a = document.createElement("a");
  a.href = url;
  a.target = "_blank";
  a.rel = "noopener";
  a.style.display = "none";
  document.body.appendChild(a);
  a.click();
  a.remove();
}

// The overlay opens under the finger, so the click the browser synthesizes for
// the opening tap lands on it. Clicks up to this timestamp belong to that tap
// and must not be read as a dismiss.
let viewerOpenedAt = 0;

const VIDEO_EXT_RE = /\.(?:mp4|webm|mov)$/i;

function showImage(path) {
  const isVideo = VIDEO_EXT_RE.test(path);
  const img = $("viewer-img"), video = $("viewer-video");
  const src = apiURL("api/file?path=" + encodeURIComponent(path));
  viewerOpenedAt = Date.now();
  resetZoom();
  if (isVideo) {
    img.style.display = "none";
    img.removeAttribute("src");
    video.style.display = "block";
    // Re-opening the same path would otherwise reload and re-decode it.
    if (video.getAttribute("src") !== src) {
      video.onerror = () => { hideImage(); toast("Couldn't load video"); };
      video.src = src;
    }
  } else {
    video.style.display = "none";
    video.pause();
    video.removeAttribute("src");
    img.style.display = "";
    // Re-opening the same path (mouse users get both the link and the tap path)
    // would otherwise reload and re-decode the image.
    if (img.getAttribute("src") !== src) {
      img.onerror = () => { hideImage(); toast("Couldn't load image"); };
      img.src = src;
    }
  }
  $("viewer").classList.add("show");
}

function hideImage() {
  $("viewer").classList.remove("show");
  resetZoom();
  // Drop the decoded image rather than hold a screenshot's worth of memory for
  // the rest of the session.
  $("viewer-img").removeAttribute("src");
  // Pause and release the video decoder too, or it keeps running off-screen.
  const video = $("viewer-video");
  video.pause();
  video.removeAttribute("src");
}

$("btn-viewer-close").addEventListener("click", hideImage);
// Anywhere off the image/video itself closes; the media keeps its own taps (and
// the video its native controls) so interacting with it is not read as a dismiss.
$("viewer").addEventListener("click", (e) => {
  if (Date.now() - viewerOpenedAt < 500) return;
  if (Date.now() - gestureEndedAt < 350) return;
  if (e.target !== $("viewer-img") && e.target !== $("viewer-video")) hideImage();
});

// A pinch, a pan or a double-tap can end with a click the browser synthesizes on
// the overlay, which would otherwise read as a dismiss. A latch would be wrong:
// a multi-touch gesture emits no click at all, so the latch would survive to eat
// the next real one. Stamping the gesture's end and ignoring clicks just after it
// suppresses exactly the synthesized click and nothing else.
let gestureEndedAt = 0;
let zoomScale = 1, zoomX = 0, zoomY = 0;

function resetZoom() {
  zoomScale = 1; zoomX = 0; zoomY = 0;
  const img = $("viewer-img");
  img.style.transition = "";
  img.style.transform = "";
}

