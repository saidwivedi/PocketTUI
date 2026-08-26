// ---- output plumbing ----------------------------------------------------
// Every write goes through here so nothing reaches a terminal that has been
// reset or handed back to a real session.
function demoWrite(s) {
  if (!demoMode || !term) return;
  term.write(s);
}
function demoLine(s) { demoWrite((s || "") + "\r\n"); }

// Prose is written as whole sentences and broken here, against the width the
// terminal actually has. Hard-coding the breaks instead would pick one phone
// width and stagger every narrower one, since xterm then wraps the overflow a
// second time.
//
// Takes plain text and a colour rather than a pre-coloured string: escapes must
// not count toward the measured width, and each emitted line has to carry its
// own colour rather than leaving one open across a break.
function demoWrapWidth(indent) {
  const cols = (term && term.cols) || 40;
  // One column short of the edge: wrapping into the last cell makes xterm emit
  // a phantom wrap of its own.
  return Math.max(8, cols - indent.length - 1);
}

function demoWrapLines(text, width) {
  const out = [];
  for (const para of String(text).split("\n")) {
    let line = "";
    for (const word of para.split(/\s+/).filter(Boolean)) {
      if (!line) line = word;
      else if (line.length + 1 + word.length <= width) line += " " + word;
      else { out.push(line); line = word; }
    }
    out.push(line);
  }
  return out;
}

function demoWrap(text, colour, indent) {
  const pre = indent || "";
  const col = colour || "";
  for (const line of demoWrapLines(text, demoWrapWidth(pre))) {
    demoLine(line ? pre + col + line + D.r : "");
  }
}

function demoPrompt() {
  return D.grn + D.b + DEMO_USER + "@" + DEMO_HOST + D.r + ":" +
         D.blu + D.b + demoPathStr(demoCwd) + D.r + "$ ";
}

