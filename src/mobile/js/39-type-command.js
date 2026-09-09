// ============================================================
// Type it for me
// ============================================================
// The failure card names a command to run on the computer, and so does the
// update notice when an install did not take. When the phone can still reach
// the server the command is about, it can put that command at a prompt instead
// of leaving the user to retype it on a phone keyboard.
//
// What it never does is press Enter. /api/session/type sends the text
// literally and the server strips every newline, so the line sits at the
// prompt until the person reading it decides to run it. That is the whole
// contract, and it is why the button can be offered on a screen that is
// already reporting that something is wrong: the worst it can do is put a line
// of text somewhere the user can see and delete it.
//
// hasCapStrict, not hasCap: a server too old for the route answers the POST
// with a 404, and a button that cannot work is worse than the command sitting
// there to be read and typed.
async function stageCommand(text) {
  if (!text || demoMode) return;
  if (!hasCapStrict("type")) { toast("This server can't type it for you yet"); return; }
  let name = currentSession;
  if (!name) {
    // Nothing open to type into. Make a session and show it: the point of the
    // button is to leave the user looking at the line they are about to run,
    // not to hide it in a session they would have to go find.
    let made = null;
    try {
      made = await createSessionNamed(defaultSessionName(), false);
    } catch (e) {}
    if (!made || !made.session) {
      toast(made && made.error ? made.error : "Couldn't create the session");
      return;
    }
    name = made.session;
    openTerminal(name);
    // The rail and the list have a session they have not heard about yet, and
    // quietly: the user is looking at the terminal, not at a refresh.
    loadSessions(false, true);
  }
  try {
    const r = await fetch(apiURL("api/session/type"), {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ name: name, dev: cfg.devname, text: text }),
    });
    if (!r.ok) {
      const data = await r.json().catch(() => null);
      toast(data && data.error ? data.error : "Couldn't type it");
      return;
    }
    toast("Typed. Press Enter to run it.", 3500);
  } catch (e) {
    toast("Couldn't type it");
  }
}

// The update row's copy of the button. The sheet has to go first: the command
// lands in the terminal underneath it, and a toast over a closed sheet is not
// where the user should have to take the shell's word for it.
$("btn-update-type").addEventListener("click", () => {
  showSheet(false);
  stageCommand("pockettui update");
});
