// ---- lifecycle ----------------------------------------------------------
function demoStart() {
  demoCwd = ["home", "projects", "webapp"];
  demoHist = []; demoHistAt = 0;
  demoBuf = ""; demoCur = 0;
  demoPending = "";
  demoAgent = null;
  demoBusy = false;
  demoNoteAt = 0;
  demoClearTimers();

  // Less one column for the leading space the rule is printed with.
  const rule = D.gry + "─".repeat(Math.min(44, Math.max(20, ((term && term.cols) || 40) - 1))) + D.r;
  demoLine("");
  demoLine(" " + D.b + D.yel + "Demo terminal" + D.r + D.gry + " — simulated, offline" + D.r);
  demoLine(" " + rule);
  demoWrap("This demo shows how a terminal feels on your phone. Everything " +
           "below is simulated and runs entirely offline — no machine is " +
           "attached.", D.gry, " ");
  demoLine("");
  demoWrap("When you connect your own computer, this same terminal sends your " +
           "keys to a small program running there, which passes them to tmux. " +
           "Nothing sits in between.", D.gry, " ");
  demoLine("");
  demoLine(" " + D.gry + "Try:" + D.r);
  // Two columns so the whole hint block clears a phone screen above the prompt.
  const hints = [
    ["ls", "git status"],
    ["cd src", "tmux ls"],
    ["cat README.md", "claude"],
    ["help", "codex"],
  ];
  for (const [a, b] of hints) {
    demoLine("   " + D.grn + a.padEnd(16) + D.r + D.grn + b + D.r);
  }
  demoLine("");
  demoNewPrompt();
}

function demoStop() {
  if (!demoMode) return;
  demoClearTimers();
  demoBusy = false;
  demoAgent = null;
  demoPending = "";
  demoInterrupt = () => {};
  demoMode = false;
  syncReportEntry();
  $("demo-badge").classList.remove("show");
}

// Leaving from inside the shell takes the same route as the back gesture, so
// there is one teardown path rather than two.
function demoExit() {
  demoLine(D.gry + "leaving the demo…" + D.r);
  history.back();
}

