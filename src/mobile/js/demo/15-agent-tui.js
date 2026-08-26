// ---- agent TUI ----------------------------------------------------------
// A stand-in for the shape of an agent CLI, not for its behaviour: every reply
// is the same canned transcript, and it says so in the body.
const DEMO_AGENTS = {
  claude: { name: "Claude Code", version: "2.0.14", accent: D.mag, dot: "⏺" },
  codex:  { name: "Codex CLI",   version: "0.31.0", accent: D.cyn, dot: "●" },
};

const DEMO_SLASH = ["/help", "/clear", "/exit"];

function demoAgentPrompt() {
  return DEMO_AGENTS[demoAgent].accent + "> " + D.r;
}

function demoAgentStart(which) {
  demoAgent = which;
  const a = DEMO_AGENTS[which];
  const width = Math.min(56, Math.max(30, (term && term.cols ? term.cols - 2 : 40)));
  const line = (text) => {
    const pad = Math.max(0, width - 2 - text.replace(/\x1b\[[0-9;]*m/g, "").length);
    return a.accent + "│" + D.r + " " + text + " ".repeat(pad) + a.accent + "│" + D.r;
  };
  demoLine("");
  demoLine(a.accent + "╭" + "─".repeat(width - 2) + "╮" + D.r);
  demoLine(line(D.b + a.name + D.r + D.gry + "  v" + a.version + D.r));
  demoLine(line(D.gry + "cwd: " + demoPathStr(demoCwd) + D.r));
  demoLine(line(D.yel + "simulated — no model is connected" + D.r));
  demoLine(a.accent + "╰" + "─".repeat(width - 2) + "╯" + D.r);
  demoLine("");
  demoWrap("Type anything — you'll get a canned reply, not a real answer.", D.gry, "  ");
  demoLine(D.gry + "  " + D.r + D.grn + "/help" + D.r + D.gry + "  " + D.r + D.grn + "/clear" + D.r +
           D.gry + "  " + D.r + D.grn + "/exit" + D.r + D.gry + "   (or ctrl-d to leave)" + D.r);
  demoLine("");
  demoNewPrompt();
}

function demoAgentExit() {
  const a = DEMO_AGENTS[demoAgent];
  demoAgent = null;
  demoLine("");
  demoLine(D.gry + a.name + " session closed." + D.r);
  demoNewPrompt();
}

function demoAgentSubmit(text) {
  if (!text) { demoNewPrompt(); return; }
  if (text === "/exit" || text === "exit" || text === "quit" || text === "/quit") { demoAgentExit(); return; }
  if (text === "/clear") { demoWrite("\x1b[2J\x1b[H"); demoNewPrompt(); return; }
  if (text === "/help") {
    const a = DEMO_AGENTS[demoAgent];
    demoLine("");
    demoLine("  " + D.b + a.name + " — demo slash commands" + D.r);
    demoLine("    " + D.grn + "/help " + D.r + D.gry + "  this list" + D.r);
    demoLine("    " + D.grn + "/clear" + D.r + D.gry + "  clear the screen" + D.r);
    demoLine("    " + D.grn + "/exit " + D.r + D.gry + "  back to the shell" + D.r);
    demoLine("");
    demoNewPrompt();
    return;
  }
  demoAgentRespond(text);
}

// The canned reply, played out as a script of steps so a keystroke can cut it
// short the way ctrl-c does against the real CLI.
function demoAgentRespond(text) {
  const a = DEMO_AGENTS[demoAgent];
  demoBusy = true;
  demoLine("");

  const SPIN = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];
  let spinAt = 0, spinTimer = null;
  const spinner = (label) => (done) => {
    if (done) {
      clearInterval(spinTimer);
      demoTimers = demoTimers.filter(t => t !== spinTimer);
      spinTimer = null;
      demoWrite("\r\x1b[K");
      return;
    }
    spinTimer = setInterval(() => {
      if (!demoMode || !demoBusy) { clearInterval(spinTimer); return; }
      demoWrite("\r\x1b[K" + a.accent + SPIN[spinAt++ % SPIN.length] + D.r + D.gry + " " + label + D.r);
    }, 90);
    demoTimers.push(spinTimer);
  };
  const spin = spinner("Thinking…");

  // Each step is [delay before it, what it writes]. Text steps are typed out a
  // few characters at a time so the reply arrives the way a streamed one does.
  const steps = [
    [0, () => spin(false)],
    [1100, () => { spin(true); demoLine(a.accent + a.dot + D.r + " Read(" + D.b + "src/router.js" + D.r + ")"); }],
    [260, () => demoLine("  " + D.gry + "⎿  Read 42 lines" + D.r)],
    [340, () => demoLine(a.accent + a.dot + D.r + " Bash(" + D.b + "npm test" + D.r + ")")],
    [620, () => demoLine("  " + D.gry + "⎿  2 passed in 0.31s" + D.r)],
    [300, () => demoLine("")],
  ];

  // Broken to the terminal's width up front. Streaming the raw paragraph would
  // leave the wrapping to xterm, which breaks mid-word at the right edge.
  const body = demoWrapLines(
    "This is a simulated response. The demo terminal isn't connected to a " +
    "model or to any machine — it replays one canned transcript so you can see " +
    "how an agent session renders on a phone: tool calls, wrapped prose, colours " +
    "and the footer below.\n\n" +
    "Your message was: " + JSON.stringify(text.length > 80 ? text.slice(0, 80) + "…" : text) + "\n\n" +
    "Connect your own computer and the terminal becomes real — this agent view " +
    "stays a script either way.",
    demoWrapWidth("")
  ).join("\n");

  let i = 0;
  const runStep = () => {
    if (!demoBusy) return;
    if (i >= steps.length) { streamBody(0); return; }
    const [delay, fn] = steps[i++];
    demoTimeout(() => { if (!demoBusy) return; fn(); runStep(); }, delay);
  };

  const CHUNK = 3;
  const streamBody = (at) => {
    if (!demoBusy) return;
    if (at >= body.length) {
      demoLine("");
      demoLine("");
      demoLine(D.gry + "  ⏱ 2.4s · 1,284 tokens · simulated, nothing was billed" + D.r);
      demoLine("");
      demoBusy = false;
      demoNewPrompt();
      return;
    }
    const piece = body.slice(at, at + CHUNK);
    demoWrite(piece.replace(/\n/g, "\r\n"));
    demoTimeout(() => streamBody(at + CHUNK), 12);
  };

  // Cutting the stream short has to stop the spinner too, whichever step it died on.
  demoInterrupt = () => {
    demoBusy = false;
    if (spinTimer) { clearInterval(spinTimer); spinTimer = null; demoWrite("\r\x1b[K"); }
    demoClearTimers();
    demoLine("");
    demoLine(D.red + "  ⎿  Interrupted by user" + D.r);
    demoLine("");
    demoNewPrompt();
  };

  runStep();
}

// Replaced for the duration of each streamed response; the default is a no-op so
// an interrupt with nothing running cannot throw.
let demoInterrupt = () => {};

