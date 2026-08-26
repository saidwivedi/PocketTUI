// ---- line editor --------------------------------------------------------
let demoBuf = "", demoCur = 0;          // current line and cursor offset into it
let demoHist = [], demoHistAt = 0;      // command history, index == length means "new line"
let demoPending = "";                   // a partial escape sequence split across send() calls
let demoTimers = [];                    // every timer the demo owns, cleared on exit
let demoBusy = false;                   // a streamed response is running
let demoAgent = null;                   // non-null while inside the fake claude/codex TUI

function demoTimeout(fn, ms) {
  const id = setTimeout(() => {
    demoTimers = demoTimers.filter(t => t !== id);
    if (demoMode) fn();
  }, ms);
  demoTimers.push(id);
  return id;
}
function demoClearTimers() {
  for (const id of demoTimers) clearTimeout(id);
  demoTimers = [];
}

// Redraw the edited line in place: back to the prompt, clear to end of screen,
// reprint, then walk the cursor back to where it belongs. Clearing to the end of
// the *screen* rather than the line is what keeps a line that has wrapped from
// leaving its tail behind.
function demoRedraw() {
  const prompt = demoAgent ? demoAgentPrompt() : demoPrompt();
  demoWrite("\r\x1b[J" + prompt + demoBuf);
  const back = demoBuf.length - demoCur;
  if (back > 0) demoWrite("\x1b[" + back + "D");
}

function demoNewPrompt() {
  demoBuf = ""; demoCur = 0;
  demoHistAt = demoHist.length;
  demoWrite("\r\x1b[J" + (demoAgent ? demoAgentPrompt() : demoPrompt()));
}

// Feed one chunk of input. Escape sequences can arrive whole, several at once,
// or split across chunks — demoPending carries the tail of a partial one.
function demoInput(data) {
  if (!demoMode) return;
  let s = demoPending + data;
  demoPending = "";
  let i = 0;
  while (i < s.length) {
    const c = s[i];
    if (c === "\x1b") {
      // Mouse reports from the drag-to-scroll gesture are addressed to a program
      // that is in mouse mode; this shell is not, so they are dropped rather than
      // typed into the line.
      const mouse = /^\x1b\[<\d+;\d+;\d+[Mm]/.exec(s.slice(i));
      if (mouse) { i += mouse[0].length; continue; }
      const csi = /^\x1b\[[0-9;]*[A-Za-z~]/.exec(s.slice(i));
      if (csi) { demoKey(csi[0]); i += csi[0].length; continue; }
      const ss3 = /^\x1bO[A-Za-z]/.exec(s.slice(i));
      if (ss3) { demoKey(ss3[0]); i += ss3[0].length; continue; }
      // Could still be the start of a sequence whose rest has not arrived. A
      // lone ESC that never completes is delivered on the next chunk.
      if (i === s.length - 1 || /^\x1b(\[[0-9;]*|O)?$/.test(s.slice(i))) {
        demoPending = s.slice(i);
        return;
      }
      demoKey("\x1b");
      i++;
      continue;
    }
    demoKey(c);
    i++;
  }
}

function demoKey(k) {
  // While a response is streaming only the interrupts are live; anything else
  // would interleave with the output being written.
  if (demoBusy) {
    if (k === "\x03" || k === "\x1b") demoInterrupt();
    return;
  }

  switch (k) {
    case "\r": case "\n":
      demoWrite("\r\n");
      demoSubmit(demoBuf);
      return;
    case "\x7f": case "\b":
      if (demoCur > 0) {
        demoBuf = demoBuf.slice(0, demoCur - 1) + demoBuf.slice(demoCur);
        demoCur--;
        demoRedraw();
      }
      return;
    case "\x03":   // ctrl-c
      demoWrite("^C\r\n");
      demoNewPrompt();
      return;
    case "\x04":   // ctrl-d — leaves the agent, or the demo from a bare prompt
      if (demoAgent) { demoAgentExit(); return; }
      if (!demoBuf) { demoLine(""); demoExit(); }
      return;
    case "\x0c":   // ctrl-l
      demoWrite("\x1b[2J\x1b[H");
      demoRedraw();
      return;
    case "\x01": demoCur = 0; demoRedraw(); return;               // ctrl-a
    case "\x05": demoCur = demoBuf.length; demoRedraw(); return;  // ctrl-e
    case "\x15":                                                  // ctrl-u
      demoBuf = demoBuf.slice(demoCur); demoCur = 0; demoRedraw(); return;
    case "\t":
      demoComplete();
      return;
    case "\x1b[A": case "\x1bOA": demoHistory(-1); return;
    case "\x1b[B": case "\x1bOB": demoHistory(1); return;
    case "\x1b[C": case "\x1bOC":
      if (demoCur < demoBuf.length) { demoCur++; demoWrite("\x1b[C"); }
      return;
    case "\x1b[D": case "\x1bOD":
      if (demoCur > 0) { demoCur--; demoWrite("\x1b[D"); }
      return;
    case "\x1b[H": case "\x1b[1~": demoCur = 0; demoRedraw(); return;
    case "\x1b[F": case "\x1b[4~": demoCur = demoBuf.length; demoRedraw(); return;
    case "\x1b[3~":   // delete
      if (demoCur < demoBuf.length) {
        demoBuf = demoBuf.slice(0, demoCur) + demoBuf.slice(demoCur + 1);
        demoRedraw();
      }
      return;
    case "\x1b":
      return;
  }
  // Anything else printable goes into the line; other control bytes are ignored.
  if (k.length === 1 && k >= " ") {
    demoBuf = demoBuf.slice(0, demoCur) + k + demoBuf.slice(demoCur);
    demoCur++;
    // Appending at the end is the common case and needs no repaint.
    if (demoCur === demoBuf.length) demoWrite(k);
    else demoRedraw();
  }
}

function demoHistory(dir) {
  if (!demoHist.length) return;
  const next = Math.min(demoHist.length, Math.max(0, demoHistAt + dir));
  if (next === demoHistAt) return;
  demoHistAt = next;
  demoBuf = next === demoHist.length ? "" : demoHist[next];
  demoCur = demoBuf.length;
  demoRedraw();
}

// Tab completion: the first word completes against command names, everything
// after it against the names in the directory being typed.
function demoComplete() {
  const head = demoBuf.slice(0, demoCur);
  const m = /(\S*)$/.exec(head);
  const frag = m[1];
  const isFirst = head.trim() === frag && !demoAgent;
  let cands;
  if (demoAgent) {
    cands = DEMO_SLASH.filter(c => c.startsWith(frag));
  } else if (isFirst) {
    cands = Object.keys(DEMO_COMMANDS).filter(c => c.startsWith(frag)).sort();
  } else {
    const slash = frag.lastIndexOf("/");
    const dirPart = slash >= 0 ? frag.slice(0, slash + 1) : "";
    const namePart = slash >= 0 ? frag.slice(slash + 1) : frag;
    const node = demoNodeAt(demoResolve(dirPart || "."));
    if (!demoIsDir(node)) return;
    cands = Object.keys(node).filter(n => n.startsWith(namePart))
      .map(n => dirPart + n + (demoIsDir(node[n]) ? "/" : "")).sort();
  }
  if (!cands.length) return;

  // Extend by the longest prefix every candidate shares, then list the rest.
  let common = cands[0];
  for (const c of cands) {
    let i = 0;
    while (i < common.length && i < c.length && common[i] === c[i]) i++;
    common = common.slice(0, i);
  }
  if (common.length > frag.length) {
    const add = common.slice(frag.length);
    demoBuf = demoBuf.slice(0, demoCur) + add + demoBuf.slice(demoCur);
    demoCur += add.length;
    if (cands.length === 1 && !common.endsWith("/")) {
      demoBuf = demoBuf.slice(0, demoCur) + " " + demoBuf.slice(demoCur);
      demoCur++;
    }
    demoRedraw();
    return;
  }
  if (cands.length > 1) {
    demoWrite("\r\n");
    demoLine(cands.join("  "));
    demoRedraw();
  }
}

function demoSubmit(line) {
  const cmd = line.trim();
  if (cmd) {
    // Same as a shell's HISTCONTROL=ignoredups: no point walking back through a
    // run of identical entries.
    if (demoHist[demoHist.length - 1] !== cmd) demoHist.push(cmd);
  }
  demoBuf = ""; demoCur = 0; demoHistAt = demoHist.length;
  if (demoAgent) { demoAgentSubmit(cmd); return; }
  if (!cmd) { demoNewPrompt(); return; }
  demoRun(cmd);
}

