// ============================================================
// Demo shell
// ============================================================
// A fake terminal for looking at the app without a computer behind it. It
// takes over send() — every input source already funnels through there — and
// writes back through term.write(), so the key bar, the physical keyboard and
// the theme all work exactly as they do against a real PTY.
//
// Everything below is invented. Nothing here reads a file, runs a process or
// calls a model, and the output says so wherever it could be mistaken for the
// real thing.
let demoMode = false;

const DEMO_USER = "demo", DEMO_HOST = "workstation";

// SGR shorthands. Colours come from whichever xterm theme is live, so the demo
// follows the app's light/dark toggle for free.
const D = {
  r: "\x1b[0m", b: "\x1b[1m", dim: "\x1b[2m", it: "\x1b[3m",
  red: "\x1b[31m", grn: "\x1b[32m", yel: "\x1b[33m", blu: "\x1b[34m",
  mag: "\x1b[35m", cyn: "\x1b[36m", gry: "\x1b[90m",
};

// A dim aside — used for the reminders that the output above was made up.
function demoNote(text) {
  return D.gry + D.it + text + D.r;
}

// Varied on purpose: the same sentence pasted under every command reads as a
// nag, and people stop seeing it.
const DEMO_FABRICATED = [
  "(made-up output — nothing was actually inspected)",
  "(demo data, not this device)",
  "(fabricated for the preview; no such processes exist)",
  "(invented figures — no real machine was queried)",
  "(sample output, not a live reading)",
  "(all of the above is fictional)",
];
let demoNoteAt = 0;
// Emit an aside broken to the terminal width. The longer ones run past a narrow
// phone, where an unbroken note wraps into a one-word orphan line.
function demoNoteLine(text) {
  demoWrap(text, D.gry + D.it, "");
}

function demoFabricatedLine() {
  demoNoteLine(DEMO_FABRICATED[demoNoteAt % DEMO_FABRICATED.length]);
  demoNoteAt++;
}

