// ============================================================
// Long-press to select and copy
// ============================================================
// A phone has no right-click and no modifier to hold, so getting text out of the
// terminal is a long press: hold still for half a second and the word under the
// finger is selected, keep dragging and the selection follows, let go and a pill
// offers to copy it. Two places have no word to offer: blank space, and the row
// the cursor is on, where the user is typing rather than reading — both come up
// as a Paste pill instead. The whole gesture is ours from the moment the timer fires —
// termGesture keeps drag-to-scroll out of it, and preventDefault keeps iOS from
// answering the same press with its magnifier.
//
// Selection is done through xterm's public API only: cells in, string out. The
// touch point becomes a cell through the shared geometry above, so a font change
// or a pinch needs no bookkeeping here.
//
// Letting go does not end the selection: two handles stay on the grid at its
// ends, and dragging either one moves that end freely — growing or shrinking,
// across rows — the way selection works in any mobile text field. The selection
// only ends when the user asks it to, through Copy, Cancel, or a touch anywhere
// else on the terminal.
(function longPressSelect() {
  const host = $("term-host");
  const pill = $("sel-pill");
  const handles = { start: $("sel-handle-start"), end: $("sel-handle-end") };
  const HOLD_MS = 350;   // how long the finger has to stay put

  let timer = null;
  let startX = 0, startY = 0;
  // px of travel that cancels the hold, measured fresh at each touchstart because
  // a pinch changes it. One cell height is deliberate: drag-to-scroll claims the
  // touch at exactly that much travel, so there is no band where the hold has
  // been cancelled but no scroll has started and the touch decays into a tap.
  let holdSlop = 17;
  // The two ends of the selection, in absolute buffer coordinates. During the
  // opening long-press these are the seeded word's ends and only grow outward;
  // once the handles are up either one can be moved anywhere the other allows.
  let head = null, tail = null;
  // True from the moment the opening press lifts until the selection is
  // dismissed — the handles and pill are on screen and the terminal is in
  // selection mode rather than its normal tap/scroll self.
  let selActive = false;
  let dragging = null;   // "start" | "end" while a handle is being dragged
  let grabDX = 0, grabDY = 0;   // finger offset from the dragged handle's own cell
  // Set around our own term.select() calls so the onSelectionChange they fire
  // is not mistaken for the selection being cleared out from under us.
  let selfSelecting = false;

  function cancelHold() {
    clearTimeout(timer);
    timer = null;
  }

  // Every term.select() in this block goes through here, so the change event it
  // raises can be told apart from one the terminal raised on its own.
  function applySelection(col, row, length) {
    selfSelecting = true;
    try { term.select(col, row, Math.max(1, length)); } catch (e) {}
    selfSelecting = false;
  }

  // Back to normal: no selection, no handles, no pill, and the next touch is a
  // plain one.
  function exitSelection() {
    cancelHold();
    head = tail = null;
    selActive = false;
    dragging = null;
    pill.classList.remove("show");
    handles.start.classList.remove("show");
    handles.end.classList.remove("show");
    if (term) { selfSelecting = true; try { term.clearSelection(); } catch (e) {} selfSelecting = false; }
    // Only ever release the claim this gesture made: a pinch that dropped a
    // selection on its way in has already taken the terminal for itself.
    if (termGesture === "select") termGesture = null;
  }

  // The run of non-whitespace around a cell, as [startCol, length]. A length of
  // 0 means the press landed on blank space — there is no word there, and the
  // caller turns that into the Paste-only pill rather than selecting nothing.
  function wordAt(bufRow, col) {
    const line = term.buffer.active.getLine(bufRow);
    if (!line) return [col, 0];
    const text = line.translateToString(false);
    if (!text[col] || /\s/.test(text[col])) return [col, 0];
    let s = col, e = col;
    while (s > 0 && text[s - 1] && !/\s/.test(text[s - 1])) s--;
    while (e + 1 < term.cols && text[e + 1] && !/\s/.test(text[e + 1])) e++;
    return [s, e - s + 1];
  }

  // Cells from the top-left of the buffer, so two positions compare as numbers.
  function ordinal(p) { return p.row * term.cols + p.col; }

  // Push head/tail to the terminal.
  function drawSelection() {
    applySelection(head.col, head.row, ordinal(tail) - ordinal(head) + 1);
  }

  // The seed word, grown to wherever the finger is. This is the opening gesture
  // only: dragging past the word's end extends the tail, past its start extends
  // the head, and a finger still inside the word leaves the word intact. Once
  // the handles are up, moveHandle() takes over and both ends move freely.
  function extendTo(cell) {
    const at = { col: cell.col, row: cell.row + term.buffer.active.viewportY };
    if (ordinal(at) < ordinal(head)) head = at;
    else if (ordinal(at) > ordinal(tail)) tail = at;
    drawSelection();
  }

  // One end of the selection, dragged to wherever the finger is. Unlike the
  // opening gesture this shrinks as readily as it grows; the only limits are
  // that the ends may not cross and the selection may not vanish, so each end is
  // clamped to the other's own cell — a one-character selection, never none.
  //
  // The two handles mark different edges: the start sits on the left edge of the
  // first selected cell, so it maps straight onto head, while the end sits on
  // the right edge of the last one and is therefore a cell past tail.
  function moveHandle(which, cell) {
    const at = { col: cell.col, row: cell.row + term.buffer.active.viewportY };
    if (which === "start") {
      head = ordinal(at) > ordinal(tail) ? { col: tail.col, row: tail.row } : at;
    } else {
      // Back off the edge the handle draws on to get the last selected cell,
      // stepping to the end of the previous row when that lands before column 0.
      let end = at.col > 0 ? { col: at.col - 1, row: at.row }
                           : { col: term.cols - 1, row: at.row - 1 };
      tail = ordinal(end) < ordinal(head) ? { col: head.col, row: head.row } : end;
    }
    drawSelection();
  }

  // Both handles onto the grid: the start hangs off the left edge of the first
  // selected cell, the end off the right edge of the last, each just below its
  // row so the dot never covers the text it marks. Coordinates are the host's
  // own box, since that is what the handles are positioned inside.
  function placeHandles() {
    if (!head || !tail) return;
    const cell = termCellSize();
    const grid = host.querySelector(".xterm-screen") || host;
    const gbox = grid.getBoundingClientRect();
    const hbox = host.getBoundingClientRect();
    const vy = term.buffer.active.viewportY;
    const put = (el, bufRow, col) => {
      // Off the top or bottom of the viewport: the end is scrolled out of sight,
      // so its handle goes with it rather than sticking to the edge.
      const vrow = bufRow - vy;
      if (vrow < 0 || vrow >= term.rows) { el.classList.remove("show"); return; }
      const x = (gbox.left - hbox.left) + col * cell.w;
      const y = (gbox.top - hbox.top) + (vrow + 1) * cell.h;
      el.style.left = x + "px";
      el.style.top = y + "px";
      el.classList.add("show");
    };
    put(handles.start, head.row, head.col);
    put(handles.end, tail.row, tail.col + 1);
  }

  // Beside the selection, never under it: the pill sits above the selection's
  // first row where there is room, and below its last row when the selection
  // starts too near the top of the screen to fit one.
  function placePill() {
    if (!head || !tail) return;
    const cell = termCellSize();
    const grid = host.querySelector(".xterm-screen") || host;
    const gbox = grid.getBoundingClientRect();
    const vy = term.buffer.active.viewportY;
    pill.classList.add("show");
    const box = pill.getBoundingClientRect();
    const midCol = (head.row === tail.row) ? (head.col + tail.col + 1) / 2 : term.cols / 2;
    let left = gbox.left + midCol * cell.w - box.width / 2;
    left = Math.min(window.innerWidth - box.width - 8, Math.max(8, left));
    const topRowY = gbox.top + (head.row - vy) * cell.h;
    const botRowY = gbox.top + (tail.row - vy + 1) * cell.h;
    let top = topRowY - box.height - 10;
    if (top < 8) top = botRowY + 10;
    pill.style.left = left + "px";
    pill.style.top = top + "px";
  }

  // The resting state after any gesture that leaves a selection standing. There
  // is text under the finger, so the pill is Copy/Cancel: pasting has nothing to
  // do with the selection, and offering it here only invites pasting over it.
  // Both classes are set on every path so neither can survive the previous pill.
  function showSelectionUI() {
    selActive = true;
    pill.classList.remove("no-copy");
    pill.classList.add("no-paste");
    placeHandles();
    placePill();
  }

  function copySelection() {
    const text = term ? term.getSelection() : "";
    if (!text) { exitSelection(); return; }
    // Inside the tap, because iOS only grants the clipboard from a user gesture.
    // The hidden textarea is the fallback for the older path, where writeText is
    // either missing or rejects outright.
    const fallback = () => {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.setAttribute("readonly", "");
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      let ok = false;
      try { ok = document.execCommand("copy"); } catch (e) {}
      document.body.removeChild(ta);
      toast(ok ? "Copied" : "Clipboard blocked");
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(() => toast("Copied")).catch(fallback);
    } else {
      fallback();
    }
    exitSelection();
  }

  // Each handle owns touches that start on it. The listeners go on the handles
  // themselves rather than on the host, so a thumb landing on one is claimed
  // before the host's own selection/scroll handlers ever see it.
  for (const which of ["start", "end"]) {
    const el = handles[which];
    el.addEventListener("touchstart", (e) => {
      if (!selActive || e.touches.length !== 1) return;
      // The handle is the whole gesture: no scroll, no fresh long press, and no
      // synthetic mouse events from iOS on top of it.
      e.preventDefault();
      e.stopPropagation();
      cancelHold();
      abortDragScroll();
      dragging = which;
      termGesture = "select";
      // Where the finger sits relative to the cell the handle actually marks.
      // The dot hangs a stem's length below its row and the thumb lands
      // somewhere arbitrary on a 44px target, so without this the drag would
      // start by jumping the selection to wherever the thumb happened to be.
      // Carrying the offset for the whole drag is what keeps the handle under
      // the finger and the selection edge under the handle.
      const t = e.touches[0];
      const box = el.getBoundingClientRect();
      const cell = termCellSize();
      grabDX = t.clientX - (box.left + box.width / 2);
      // The handle's top edge is the bottom of its row, so the cell it marks is
      // half a row above that.
      grabDY = t.clientY - (box.top - cell.h / 2);
      // The pill would sit under the moving thumb and is meaningless mid-drag.
      pill.classList.remove("show");
    }, { passive: false });

    el.addEventListener("touchmove", (e) => {
      if (dragging !== which || e.touches.length !== 1) return;
      e.preventDefault();
      e.stopPropagation();
      const t = e.touches[0];
      // Back out the grab offset, so the cell under the handle's own point is
      // what moves rather than the cell under the fingertip.
      moveHandle(which, termCellAt(t.clientX - grabDX, t.clientY - grabDY));
      placeHandles();
    }, { passive: false });

    const done = (e) => {
      if (dragging !== which) return;
      e.preventDefault();
      e.stopPropagation();
      dragging = null;
      // The selection stands; put the furniture back around its new shape.
      selectEndedAt = Date.now();
      showSelectionUI();
    };
    el.addEventListener("touchend", done, { passive: false });
    el.addEventListener("touchcancel", done, { passive: false });
  }

  host.addEventListener("touchstart", (e) => {
    // A touch anywhere else while a selection is up dismisses it, and then this
    // touch goes on to behave completely normally — the tap raises the keyboard,
    // a drag scrolls. Nothing is stamped and nothing is claimed, so the handlers
    // that run after this one see an ordinary first touch.
    if (selActive) { exitSelection(); return; }
    cancelHold();
    if (edgeSwipe || termGesture || !term || e.touches.length !== 1) return;
    const t = e.touches[0];
    startX = t.clientX; startY = t.clientY;
    holdSlop = termCellSize().h || 17;
    timer = setTimeout(() => {
      timer = null;
      // The hold survived, so this touch is a selection from here on and the
      // scroll it may already have banked is abandoned.
      termGesture = "select";
      abortDragScroll();
      const cell = termCellAt(startX, startY);
      const buf = term.buffer.active;
      const bufRow = cell.row + buf.viewportY;
      // The row the cursor is on is where the user is typing, so a press there is
      // asking to paste into what they are typing, not to select the prompt text
      // they can already see. Same in vim's alt screen, where it is the line being
      // edited rather than a shell prompt.
      if (bufRow === buf.baseY + buf.cursorY) {
        head = tail = null;
        dbg("select: cursor row at", cell.col + "," + bufRow, "-> paste only");
        return;
      }
      const [col, len] = wordAt(bufRow, cell.col);
      if (len === 0) {
        // Empty space: nothing to select, but a long press here is still how you
        // ask for the clipboard, so the pill comes up as Paste alone.
        head = tail = null;
        dbg("select: empty cell at", cell.col + "," + bufRow, "-> paste only");
        return;
      }
      head = { col: col, row: bufRow };
      tail = { col: col + len - 1, row: bufRow };
      drawSelection();
      dbg("select: word at", cell.col + "," + bufRow, "len=" + len);
    }, HOLD_MS);
  }, { passive: true });

  host.addEventListener("touchmove", (e) => {
    if (timer) {
      // Still waiting on the hold: any real travel means this was a drag.
      const t = e.touches[0];
      if (Math.abs(t.clientX - startX) > holdSlop || Math.abs(t.clientY - startY) > holdSlop) cancelHold();
      return;
    }
    if (dragging) return;   // a handle owns this touch
    if (termGesture !== "select" || !head || e.touches.length !== 1) return;
    // Ours now: no scroll, no magnifier, no scroll chaining.
    e.preventDefault();
    extendTo(termCellAt(e.touches[0].clientX, e.touches[0].clientY));
  }, { passive: false });

  host.addEventListener("touchend", (e) => {
    cancelHold();
    if (dragging) return;
    if (termGesture !== "select") return;
    e.preventDefault();
    selectEndedAt = Date.now();
    // A press that found a word leaves it selected with handles on it; one that
    // selected nothing — empty space, or the cursor's own row — leaves the pill
    // alone, offering Paste.
    if (head && term.getSelection()) {
      showSelectionUI();
    } else {
      selActive = true;
      placePasteOnlyPill(e.changedTouches[0].clientX, e.changedTouches[0].clientY);
    }
  }, { passive: false });

  host.addEventListener("touchcancel", () => {
    cancelHold();
    if (termGesture === "select" && !dragging) exitSelection();
  }, { passive: true });

  // The Paste-only pill has no selection to sit beside, so it is placed against
  // the press itself, on the same above-then-below rule as the other one.
  function placePasteOnlyPill(clientX, clientY) {
    const cell = termCellSize();
    pill.classList.remove("no-paste");
    pill.classList.add("no-copy");
    pill.classList.add("show");
    const box = pill.getBoundingClientRect();
    let left = clientX - box.width / 2;
    left = Math.min(window.innerWidth - box.width - 8, Math.max(8, left));
    let top = clientY - cell.h - box.height - 8;
    if (top < 8) top = clientY + cell.h + 8;
    pill.style.left = left + "px";
    pill.style.top = top + "px";
  }

  $("btn-sel-copy").addEventListener("click", copySelection);
  // The click is the user gesture the clipboard read needs, so pasteFromClipboard()
  // is called from inside it — the same function the key bar's ctrl+shift+V uses,
  // which already frames the text for bracketed paste and toasts on refusal.
  $("btn-sel-paste").addEventListener("click", () => {
    pasteFromClipboard();
    exitSelection();
  });
  $("btn-sel-cancel").addEventListener("click", exitSelection);

  // The gesture only ever selects through applySelection(), so a change event
  // raised by one of our own calls is expected and must not tear the mode down.
  // Anything else clearing the selection — a reset, a repaint xterm resolves
  // that way — should take the handles and pill with it. The Paste-only pill has
  // no selection by design and is left alone.
  termSelectionCleared = () => {
    if (selfSelecting || dragging) return;
    if (selActive && head && term && !term.getSelection()) exitSelection();
  };
  // Leaving the terminal screen, or the socket dropping it, ends the gesture too.
  cancelTermSelection = exitSelection;
})();

