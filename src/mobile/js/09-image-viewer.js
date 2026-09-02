// ============================================================
// Image viewer zoom / pan
// ============================================================
// The page viewport is locked, so pinch-zoom is implemented here: the image
// carries transform: translate(x, y) scale(s) about its own centre, and every
// gesture is expressed as "keep the point under the fingers under the fingers".
(function viewerZoom() {
  const MIN_SCALE = 1, MAX_SCALE = 6, TAP_SCALE = 2.5;
  const img = $("viewer-img");
  let start = null;          // gesture anchor: scale/translate and touch geometry at touchstart
  let moved = false;         // any travel at all disqualifies this touch from being a tap
  let lastTapAt = 0, lastTapX = 0, lastTapY = 0;

  function apply() {
    img.style.transform = `translate(${zoomX}px, ${zoomY}px) scale(${zoomScale})`;
  }

  // At rest the image is letterboxed inside the overlay; panning is bounded by
  // how much of it actually overflows the box, so an edge always stays in view.
  function clamp() {
    // offsetWidth/Height are the layout (untransformed) box, so they stay put
    // while the transform is mid-gesture.
    const baseW = img.offsetWidth, baseH = img.offsetHeight;
    const maxX = Math.max(0, baseW * (zoomScale - 1) / 2);
    const maxY = Math.max(0, baseH * (zoomScale - 1) / 2);
    zoomX = Math.min(maxX, Math.max(-maxX, zoomX));
    zoomY = Math.min(maxY, Math.max(-maxY, zoomY));
  }

  // The image's centre with the current transform undone. Scaling happens about
  // the centre, so the painted rect's centre is just the rest centre plus the
  // translate; this is the fixed frame every gesture is measured against.
  function restCentre() {
    const r = img.getBoundingClientRect();
    return { x: r.left + r.width / 2 - zoomX, y: r.top + r.height / 2 - zoomY };
  }

  // Point under (cx, cy) expressed in the image's own unscaled coordinates:
  // a point p is painted at restCentre + translate + p * scale.
  function localPoint(cx, cy) {
    const c = restCentre();
    return { x: (cx - c.x - zoomX) / zoomScale, y: (cy - c.y - zoomY) / zoomScale };
  }

  function zoomAbout(cx, cy, scale) {
    const next = Math.min(MAX_SCALE, Math.max(MIN_SCALE, scale));
    const c = restCentre();
    const p = localPoint(cx, cy);
    // Solve translate so that p maps back to (cx, cy) at the new scale.
    zoomX = cx - c.x - p.x * next;
    zoomY = cy - c.y - p.y * next;
    zoomScale = next;
    clamp();
    apply();
  }

  function midpoint(touches) {
    return {
      x: (touches[0].clientX + touches[1].clientX) / 2,
      y: (touches[0].clientY + touches[1].clientY) / 2,
      d: Math.hypot(touches[0].clientX - touches[1].clientX,
                    touches[0].clientY - touches[1].clientY),
    };
  }

  img.addEventListener("touchstart", (e) => {
    img.style.transition = "";
    if (e.touches.length === 2) {
      const m = midpoint(e.touches);
      // Anchor in local space once, at gesture start: re-reading it every move
      // would chase the transform the pinch itself is producing.
      const p = localPoint(m.x, m.y);
      start = { kind: "pinch", d: m.d, mx: m.x, my: m.y, px: p.x, py: p.y,
                s: zoomScale, x: zoomX, y: zoomY };
      moved = true;
      gestureEndedAt = Date.now();
      e.preventDefault();
    } else if (e.touches.length === 1) {
      const t = e.touches[0];
      start = { kind: "pan", cx: t.clientX, cy: t.clientY, x: zoomX, y: zoomY };
      moved = false;
    }
  }, { passive: false });

  img.addEventListener("touchmove", (e) => {
    if (!start) return;
    if (start.kind === "pinch" && e.touches.length === 2) {
      const m = midpoint(e.touches);
      const next = Math.min(MAX_SCALE, Math.max(MIN_SCALE, start.s * (m.d / start.d)));
      // Put the anchor back under the (possibly drifted) finger midpoint, so a
      // pinch that also slides pans along with it.
      const c = restCentre();
      zoomX = m.x - c.x - start.px * next;
      zoomY = m.y - c.y - start.py * next;
      zoomScale = next;
      clamp();
      apply();
      e.preventDefault();
    } else if (start.kind === "pan" && e.touches.length === 1) {
      const t = e.touches[0];
      const dx = t.clientX - start.cx, dy = t.clientY - start.cy;
      if (!moved && Math.hypot(dx, dy) > 8) moved = true;
      // At fit size there is nothing to pan, and swallowing the drag would make
      // tap-outside-to-dismiss feel sticky.
      if (zoomScale <= MIN_SCALE) return;
      zoomX = start.x + dx;
      zoomY = start.y + dy;
      clamp();
      apply();
      if (moved) { gestureEndedAt = Date.now(); e.preventDefault(); }
    }
  }, { passive: false });

  // Fit <-> TAP_SCALE about the tapped point. This is the one transition worth
  // easing; every other change tracks a finger and must be immediate.
  function toggleZoom(cx, cy) {
    gestureEndedAt = Date.now();
    img.style.transition = "transform 180ms ease-out";
    if (zoomScale > MIN_SCALE) {
      zoomScale = 1; zoomX = 0; zoomY = 0;
      apply();
    } else {
      zoomAbout(cx, cy, TAP_SCALE);
    }
  }

  img.addEventListener("touchend", (e) => {
    if (start && start.kind === "pan" && !moved && e.changedTouches.length === 1) {
      const t = e.changedTouches[0];
      const now = Date.now();
      if (now - lastTapAt < 300 && Math.hypot(t.clientX - lastTapX, t.clientY - lastTapY) < 40) {
        lastTapAt = 0;
        toggleZoom(t.clientX, t.clientY);
      } else {
        lastTapAt = now; lastTapX = t.clientX; lastTapY = t.clientY;
      }
    }
    // A finger lifting out of a pinch leaves the other one down; treat what
    // remains as a fresh gesture rather than resuming the stale anchor.
    if (e.touches.length === 1) {
      const t = e.touches[0];
      start = { kind: "pan", cx: t.clientX, cy: t.clientY, x: zoomX, y: zoomY };
      moved = true;
    } else if (e.touches.length === 0) {
      if (moved) gestureEndedAt = Date.now();
      start = null;
    }
  }, { passive: false });

  img.addEventListener("touchcancel", () => { start = null; }, { passive: true });

  // Desktop equivalent of the double-tap. A touch double-tap also synthesizes a
  // dblclick, which would undo the toggle the touch handler just did, so only
  // act on a genuine mouse one (a synthesized dblclick lands within the
  // suppression window the touch toggle stamped).
  img.addEventListener("dblclick", (e) => {
    if (Date.now() - gestureEndedAt < 350) return;
    toggleZoom(e.clientX, e.clientY);
  });
})();

// A hardware keyboard's Escape puts the overlay away, and it does it by calling
// hideImage() — which is the close button's own handler, not a second teardown
// beside it. The overlay pushes no history entry (its back is whatever screen it
// opened over), so there is nothing here to unwind: one function is the whole
// close, and the key and the button share it rather than drift apart. It stands
// aside whenever something sits above the overlay — a sheet or a confirm is the
// top thing to dismiss and answers Escape itself, and a focused field is owed the
// key too. The terminal's own textarea is the one exception: the viewer opens
// over a live terminal with it still focused, which is exactly the case this is
// for. No media query and no pointer-type gate, for the reason the explorer's
// Escape gives: a phone keyboard never sends the key at all, and an iPad with one
// is who this is for.
// On capture, and stopping the event there: xterm reads keydown off its textarea
// and never consults defaultPrevented, so letting this one bubble would close the
// overlay and send an ESC into the shell underneath it in the same press.
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (!$("viewer").classList.contains("show")) return;
  if ($("sheet-scrim").classList.contains("show")) return;
  if (document.activeElement !== termInput() && typingFocus()) return;
  e.preventDefault();
  e.stopPropagation();
  hideImage();
}, true);

// The single choke point for every source of input — xterm's onData, the key bar
// and the physical-keyboard handler all arrive here, so the demo only has to
// intercept this one function to receive all of them.
function send(data) {
  // Any key the user sends answers whatever prompt the chips were offering.
  hideChips();
  if (demoMode) { demoInput(data); return; }
  if (sock && sock.readyState === WebSocket.OPEN) sock.send(data);
}
function sendResize() {
  if (!term || !sock || sock.readyState !== WebSocket.OPEN) return;
  sock.send(JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows, token: cfg.token, dev: cfg.devname }));
}
// Set when a hidden report could not be sent; cleared once one has been.
let missedHidden = false;
// Whether this client is on screen. The server holds this device's Web Push
// (and the ntfy topic) while it is — the chips and badges are the in-app
// signal — and resumes the moment hidden arrives or the socket dies.
function sendVisibility(visible) {
  if (!sock || sock.readyState !== WebSocket.OPEN) {
    // The report never reached the server. A *hidden* that goes missing is the
    // one that matters: the server keeps this device's last known visibility
    // across the linger, so without it the reconnect's visible=true is
    // true→true, no edge, and the window is never claimed back — the phone
    // comes out of a pocket to a laptop-sized screen. Remember the miss and
    // replay it on the next open (see onopen).
    if (!visible) missedHidden = true;
    return;
  }
  sock.send(JSON.stringify({ type: "visibility", visible: !!visible }));
  if (!visible) missedHidden = false;
}

// What the row count the terminal just fitted itself to was measured against.
// A fit that comes out short — the bottom rows missing after the iOS keyboard
// is dismissed — is only diagnosable from the numbers the phone itself saw, so
// they go out through dbg(), which costs nothing while debug is off.
function dbgFit() {
  const host = $("term-host");
  const scr = $("screen-term");
  let cell = "-";
  try {
    const h = term._core && term._core._renderService &&
              term._core._renderService.dimensions.css.cell.height;
    if (h) cell = Math.round(h * 10) / 10;
  } catch (e) {}
  const vv = window.visualViewport
    ? Math.round(window.visualViewport.height) : "-";
  dbg("fit", "rows=" + term.rows, "cols=" + term.cols,
      "host=" + Math.round(host.clientHeight), "cell=" + cell,
      "inner=" + window.innerHeight, "vv=" + vv,
      "scr=" + (scr.style.height || "auto"), "top=" + (scr.style.top || "0"));
}

let fitTimer = null;
function refit(delay=60) {
  clearTimeout(fitTimer);
  fitTimer = setTimeout(() => {
    if (!term || !$("screen-term").classList.contains("active")) return;
    try { fitAddon.fit(); } catch (e) {}
    dbgFit();
    sendResize();
  }, delay);
}

// The demo rides the normal terminal path — same xterm, same key bar, same back
// gesture — with the socket swapped out for the fake shell below.
const DEMO_SESSION = "__demo__";
// Set when the app is opened at ?demo=1; see the boot block for why the intent
// outlives the URL. Cleared on the way out of the demo.
const DEMO_INTENT = "pockettui_demo_intent";

function openDemo() {
  demoMode = true;
  openTerminal(DEMO_SESSION);
}

// ============================================================
// Per-session file views
// ============================================================
// A reader or an editor opened from a terminal belongs to that terminal's
// session. Switching sessions from the rail puts the whole view away — buffer,
// caret, undo history, the folder it was opened from — hands the pane to the
// session that was tapped, and brings back whatever that one had put away
// itself. Held in memory only: a reload drops a stashed view exactly as it
// drops an open one.
//
// A view is two things, and both have to be put away together: the screens'
// own state (edStash / readerStash / filesStash own their halves) and the
// history entries it pushed. The entries are spent before the switch, in the
// one coalesced go(-n) btn-files-term already relies on, and pushed again on
// the way back — so back unwinds a restored view exactly as it would have
// unwound one that never left.
//
// The map only ever holds sessions that are *off* screen: an entry is made
// when the rail leaves a session, and dropped the moment that session is
// opened again. So a view can never be closed normally while it is also
// stashed, and nothing has to clean up after closeEditor or closeReader.
const fileViews = new Map();

// The switch waiting on the go(-n) above: the single popstate it lands as is
// what resumes it (see the popstate handler below).
let pendingSwitch = null;

// The key is a session name, and a name outlives the session that wore it: a
// killed one is free for the next `tmux new`, and a rename hands it over on the
// spot. Either way the view has to move with the session rather than stay with
// the name, or a brand-new session opens onto a dead one's buffer. The list is
// where both are known, so it says so through these three (06-session-list.js)
// rather than reaching into the map.
function dropFileView(name) { fileViews.delete(name); }

function renameFileView(from, to) {
  if (!fileViews.has(from)) return;
  fileViews.set(to, fileViews.get(from));
  fileViews.delete(from);
}

// Sessions the server no longer lists are gone however they went — killed from
// another device, or from tmux itself — so their views go too. Called from the
// list's own prune of per-session state, off the same fresh payload: only a
// listing that actually arrived is allowed to declare anything missing.
function keepFileViews(names) {
  for (const name of fileViews.keys()) {
    if (!names.has(name)) fileViews.delete(name);
  }
}

// Which file view is holding the pane, or null for none. The explorer on its
// own is not one — it carries nothing the user would lose, and it closes.
function fileViewKind() {
  if ($("screen-editor").classList.contains("active")) return "editor";
  if ($("screen-reader").classList.contains("active")) return "reader";
  return null;
}

function stashFileView(session, kind) {
  // The browsing underneath is read before the screen above it is taken down,
  // and goes into the same entry: coming back to a file view that had lost its
  // folder would be half a restore.
  const view = { files: filesStash() };
  if (kind === "editor") view.editor = edStash();
  else view.reader = readerStash();
  filesTeardown();
  fileViews.set(session, view);
}

function restoreFileView(session) {
  const view = fileViews.get(session);
  fileViews.delete(session);
  filesRestore(view.files);
  // Every entry the view owned, in the order it was pushed and in the shape
  // its own push site uses: openExplorer's, one per level navigated into
  // (navigateDir's), then the single one the reader and the editor share.
  // Nothing reads these back — the unwind goes by filesStack and the screen
  // classes — but the count is what back spends, and it has to be exact.
  history.pushState({ files: true }, "", location.href);
  for (const path of view.files.stack.slice(1)) {
    history.pushState({ files: true, path: path }, "", location.href);
  }
  history.pushState(view.editor ? { editor: true } : { reader: true }, "", location.href);
  if (view.editor) edRestore(view.editor);
  else readerRestore(view.reader);
}

// `resumed` marks the second half of a switch that had a file view to put away
// first: its entries are spent and history already sits on the terminal's own.
function openTerminal(name, resumed) {
  // A rail tap while a switch is still spending entries retargets that switch
  // rather than starting a second one on top of it.
  if (pendingSwitch && !resumed) { pendingSwitch.name = name; return; }
  // Wide layouts reach here from the always-visible rail, possibly with the
  // explorer, reader or editor holding the main pane. Phone flows never arrive
  // with any of them up (the list is hidden behind them), so the isWideLayout()
  // gate keeps the deep-link edge cases exactly as they were.
  if (isWideLayout() && !resumed) {
    const kind = fileViewKind();
    // A view opened from a terminal is that session's own: put it away rather
    // than tearing it down, spend the entries it owns, and finish the switch
    // when their pop lands. Nothing is discarded, so nothing is asked.
    if (kind && filesOrigin === "screen-term" && currentSession) {
      // Tapping the row that is already open: the pane is this session's, and
      // so is the view on it. There is nothing to switch away from — and a
      // stash made here would be one for a session that is on screen, which
      // is the one thing the map must never hold.
      if (name === currentSession) return;
      pendingSwitch = { name: name, from: currentSession };
      history.go(-(filesEntryCount() + 1));
      return;
    }
    // A view opened from the session list belongs to no session — there is no
    // terminal under it to come back to — so it closes on the spot, with a
    // dirty buffer getting the same say its own back gives it.
    if (kind === "reader") closeReader();
    if ($("screen-editor").classList.contains("active")) {
      if (edDirty && !confirm("Discard your unsaved changes?")) return;
      closeEditor();
    }
    if ($("screen-files").classList.contains("active")) {
      // Not arriving via back — no pop happened — so the address field (if
      // open) just closes and the screen follows.
      closePathEdit();
      closeExplorer();
    }
  }
  // Whether this open is a switch inside an already-open terminal pane —
  // only the rail makes that possible; read before the classes move below.
  const wasOpen = $("screen-term").classList.contains("active");
  // Retire anything still open from a previous session, so socketLive() below
  // can never see a stale socket and skip the connect for this one.
  clearTimeout(retryTimer);
  // Leaving this view on purpose, so the server hears it the way it hears a
  // lock: the PTY lingers either way, and only a recorded hidden makes the
  // reopen's visible read as an edge and claim the window back. Without it,
  // switching away and straight back inside the linger window is true→true and
  // the terminal comes up at whatever size the other client left it.
  sendVisibility(false);
  sockGen++;
  cancelCoast();
  cancelTermSelection();
  if (sock) { sock.onclose = null; try { sock.close(); } catch (e) {} sock = null; }
  currentSession = name;
  retries = 0;
  hideConnBanner();  // a banner left up by the previous session is stale here
  hideChips();       // and so is a chips row — it named the old session's prompt
  // The pane comes forward for every switch that reaches here — whatever was
  // over it has been put away or closed above. A view this session put away
  // itself takes it back at the end, once the terminal underneath is its own.
  $("screen-list").classList.remove("active");
  $("screen-term").classList.add("active");
  syncChrome();
  ensureTerm();
  term.reset();
  $("demo-badge").classList.toggle("show", demoMode);
  // Whether the strip's mic is offered depends on this session: the demo has no
  // backend to transcribe against.
  recSyncMic();
  // One history entry per terminal visit, not per session viewed: a rail
  // switch replaces the entry, so back still means "close the pane" however
  // many sessions were clicked through. A resumed switch replaces too, and for
  // the same reason read from the other end: spending the view's entries has
  // just travelled back onto the pane's own, which is exactly the entry a rail
  // switch is entitled to overwrite. wasOpen is never true on the phone
  // outside the deep-link edge, and the gate keeps that edge on the old push.
  if (resumed || (wasOpen && isWideLayout())) {
    history.replaceState({ term: name }, "", location.href);
  } else {
    history.pushState({ term: name }, "", location.href);
  }
  markSelectedSession();
  clearUnread(name);   // opening is what marks a session read
  // Whatever this session had put away when the rail last left it, back on top
  // of the terminal it was opened over — which is live again underneath it.
  if (fileViews.has(name)) restoreFileView(name);
  requestAnimationFrame(() => {
    if (demoMode) {
      // Fit before the banner is written: demoStart() wraps its prose to
      // term.cols, and refit()'s timer would otherwise land after it, leaving
      // the text broken for the pre-fit width and re-wrapped by xterm.
      try { fitAddon.fit(); } catch (e) {}
      demoStart();
      term.focus();
    } else {
      refit(0);
      connect();
    }
  });
}

function closeTerminal(skipReload) {
  clearTimeout(retryTimer);
  let fromURL = false;
  try {
    fromURL = sessionStorage.getItem(DEMO_INTENT) === "1";
    sessionStorage.removeItem(DEMO_INTENT);
  } catch (e) {}
  // A demo opened from /demo has no app behind it — the setup sheet would be a
  // dead end for someone who only came to look. Leave for the landing page
  // instead, replacing the current entry so back from there exits the site
  // rather than bouncing into the demo again. Both exits land here with the
  // terminal's pushed entry already resolved: popstate has popped it, the edge
  // swipe never pushed a second one, so there is nothing left to unwind first.
  if (fromURL) { location.replace("/"); return; }
  demoStop();
  currentSession = null;
  // Same as the session switch above: an intentional close is a hidden, or
  // reopening within the linger window claims nothing.
  sendVisibility(false);
  // Bump the generation so any close still in flight is treated as superseded.
  sockGen++;
  cancelCoast();
  cancelTermSelection();
  if (sock) { sock.onclose = null; sock.close(); sock = null; }
  $("screen-term").classList.remove("active");
  $("screen-list").classList.add("active");
  syncChrome();
  markSelectedSession();
  releaseMods();
  // Leaving with the strip open would carry its keyboard onto the session list.
  setCompose(false);
  // A rejected pairing code reloads into the same 401; the caller shows the
  // setup sheet itself, so skip the doomed round-trip.
  if (skipReload) return;
  // A shell update that arrived mid-session reloads now, back on the list —
  // the reload refetches everything, so the list request would be wasted.
  if (applyPendingSwReload()) return;
  loadSessions();
}

// Single-flight: the server's attach uses `tmux attach -d`, which detaches
// whoever is already there. So a second socket for the same session does not
// just duplicate work — it kicks the first one, whose close handler reconnects
// and kicks the second, and the terminal flaps until the timing happens to
// settle. Exactly one socket may be in flight per session, and only the newest
// generation is allowed to react to a close.
let sockGen = 0;

function socketLive() {
  return sock && (sock.readyState === WebSocket.CONNECTING ||
                  sock.readyState === WebSocket.OPEN);
}

function connect() {
  if (!currentSession || demoMode) return;   // the demo has no machine to reach
  // Already connecting or connected to this session — nothing to do.
  if (socketLive()) return;
  clearTimeout(retryTimer);
  const gen = ++sockGen;
  const ws = new WebSocket(wsURL("ws/attach/" + encodeURIComponent(currentSession)));
  ws.binaryType = "arraybuffer";
  sock = ws;
  // Whether this connection has painted its first screenful. The old screen is
  // kept up during the connect (no blind reset — see scheduleReconnect), so the
  // first frame of the new connection is what wipes it: a replay control frame
  // when the server sends one, otherwise the first binary attach output. An
  // "adopted" frame says the wipe must not happen at all.
  let painted = false;

  ws.onopen = () => {
    dbg("ws open", "gen=" + gen);
    if (gen !== sockGen) { try { ws.close(); } catch (e) {} return; }
    retries = 0;
    hideConnBanner();
    // Size first, so tmux paints straight into the phone's geometry.
    sendResize();
    // A hidden that never made it out (the socket was already gone when the
    // screen locked) is replayed here, immediately ahead of the current state:
    // the server needs the False to be on record for the True to read as the
    // hidden→visible edge that claims the window back.
    if (missedHidden && document.visibilityState === "visible") sendVisibility(false);
    sendVisibility(document.visibilityState === "visible");
    term.focus();
  };
  ws.onmessage = (ev) => {
    if (gen !== sockGen) return;
    if (typeof ev.data === "string") {
      // Server→client text frames are JSON control messages; binary frames
      // stay raw PTY bytes. Anything unparseable falls through to the
      // terminal, which is what every text frame used to do.
      if (ev.data.startsWith("{")) {
        let ctl = null;
        try { ctl = JSON.parse(ev.data); } catch (e) {}
        // This reconnect took over the tmux client the dropped socket left
        // behind, so tmux saw no detach and will not re-initialise us: the
        // modes it turned on once — mouse tracking, bracketed paste,
        // application cursor keys — exist only in this terminal, and a reset
        // would drop them while tmux went on believing they were set (dead
        // scroll until the next fresh attach). Keep the screen and the
        // scrollback exactly as they are; the server's refresh-client repaint
        // is on its way to bring the visible rows up to date.
        if (ctl && ctl.type === "adopted") { dbg("ws adopted"); painted = true; return; }
        if (ctl && ctl.type === "replay") {
          dbg("ws replay", "lines=" + String(ctl.data || "").split("\n").length);
          // Scrollback from before the disconnect (and what arrived during
          // it). Paint it from the top, then park the cursor on the bottom row
          // and push the replayed tail up out of the viewport — tmux's repaint
          // addresses the screen absolutely, so it must land on blank rows
          // rather than over the history tail.
          term.reset();
          let data = ctl.data || "";
          if (!data.endsWith("\r\n")) data += "\r\n";
          term.write(data);
          const n = Math.min(data.split("\n").length, term.rows);
          term.write("\x1b[" + term.rows + ";1H" + "\r\n".repeat(n));
          painted = true;
          return;
        }
        // The pane watcher saying this session is waiting on a prompt (or,
        // with empty options and line, that it stopped waiting): the chips
        // row renders the offered answers.
        if (ctl && ctl.type === "prompt") { showPromptChips(ctl); return; }
        // A control frame this build does not know — a newer server's. Writing
        // raw JSON into the grid helps nobody; drop it.
        if (ctl) return;
      }
      term.write(ev.data);
      return;
    }
    // First attach output with no replay before it (fresh session, alt-screen
    // TUI, old server): the reset that used to happen before the connect
    // happens here instead, so the stale screen survives the retry wait.
    if (!painted) { term.reset(); painted = true; }
    term.write(new Uint8Array(ev.data));
  };
  ws.onerror = () => { dbg("ws error", "gen=" + gen); };
  ws.onclose = (ev) => {
    dbg("ws close", "code=" + ev.code, "gen=" + gen);
    // A superseded socket's close must never trigger a reconnect.
    if (gen !== sockGen) return;
    sock = null;
    // Nothing is listening for wheel reports until the reattach repaints, and a
    // coast still running would resume scrolling a screen the user never flicked.
    cancelCoast();
    if (!currentSession) return;
    if (ev.code === 4401) {
      closeTerminal(true);
      rejectToken();
      return;
    }
    // Any other application close code is the server refusing this session for
    // good — 4404 means the tmux session no longer exists. Reconnecting would
    // retry forever against an answer that will not change, so leave for the
    // list the same way the back gesture does.
    if (ev.code >= 4000 && ev.code < 5000) {
      toast(ev.code === 4404 ? "Session no longer exists" : "Session closed by the server");
      closeTerminal();
      return;
    }
    scheduleReconnect();
  };
}

// Persistent cousin of the "Reconnecting…" toast, for when the backend looks
// genuinely unreachable rather than momentarily away. The retry loop keeps
// running underneath it; it comes down on the first successful open.
function showConnBanner() { $("conn-banner").classList.add("show"); }
function hideConnBanner() { $("conn-banner").classList.remove("show"); }

$("btn-conn-retry").addEventListener("click", () => {
  // Impatience resets the backoff, so the follow-up attempts come fast again.
  retries = 0;
  clearTimeout(retryTimer);
  connect();
});
$("btn-conn-sessions").addEventListener("click", () => closeTerminal());

function scheduleReconnect() {
  // Nothing to reconnect *to* while the app is off screen: the socket would be
  // killed again within seconds, and every attempt that did land is churn the
  // server has to absorb. The visibilitychange handler below kicks connect()
  // on the way back, which puts the reconnect and this device's visible report
  // in the same moment — which is what claims the window for the phone the
  // user has just picked up.
  if (document.hidden) return;
  retries += 1;
  // 0.5s → 5s, capped; only nag with a toast once it's clearly not transient.
  const delay = Math.min(500 * Math.pow(1.7, retries - 1), 5000);
  if (retries === 3) toast("Reconnecting…");
  // Six straight failures is no longer a blip — put up the banner.
  if (retries >= 6) showConnBanner();
  clearTimeout(retryTimer);
  // No term.reset() here: the new connection's first frame (replay or attach
  // repaint) does the wiping, so the screen stays readable through the wait.
  retryTimer = setTimeout(() => {
    if (!currentSession) return;
    connect();
  }, delay);
}

window.addEventListener("popstate", (e) => {
  // The pop a rail switch was waiting on: the file view's entries are spent, so
  // history now sits on the terminal's own and the switch can finish.
  //
  // That pop is wholly the switch's, and saying so is load-bearing rather than
  // tidy. The switch can end with the tapped session's own view restored — the
  // very screens the explorer's handler answers for, back up before it runs
  // (this file's listener is registered first, so it is still ahead of it) —
  // and it would read this pop as a back out of the editor it has just been
  // handed, confirm over an unsaved buffer and close it again. Nothing else
  // listens for popstate, so cutting the rest of them off costs nothing.
  if (pendingSwitch) {
    e.stopImmediatePropagation();
    const p = pendingSwitch;
    pendingSwitch = null;
    stashFileView(p.from, fileViewKind());
    openTerminal(p.name, true);
    return;
  }
  if ($("screen-term").classList.contains("active")) closeTerminal();
});

// The badge is the demo's own way out; the back gesture and popstate reach the
// same closeTerminal(), which is what clears demoMode.
$("demo-badge").addEventListener("click", () => history.back());

// A backgrounded PWA gets its socket killed; wake it back up on return.
// iOS fires this on launch and on every return to the foreground, so it must not
// open a second socket alongside a healthy one — CONNECTING counts as healthy.
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState !== "visible") return;
  if (!currentSession) return;
  // The demo's screen is ours, not a repaint from tmux: resetting it would wipe
  // the transcript, and there is nothing to reconnect to.
  if (demoMode) { refit(); return; }
  if (socketLive()) { refit(); return; }
  retries = 0;
  // No reset — the reconnect's first frame replaces the screen; see connect().
  connect();
});

// The push gate's other half: every foreground/background flip goes to the
// server (a fresh connection reports in its onopen instead). pagehide fires
// where a backgrounding standalone PWA's visibilitychange does not on iOS —
// see the voice-capture note — and forcing hidden there is safe: a wrong
// "hidden" costs one redundant notification, a wrong "visible" swallows
// real ones.
document.addEventListener("visibilitychange", () => {
  sendVisibility(document.visibilityState === "visible");
});
window.addEventListener("pagehide", () => sendVisibility(false));

