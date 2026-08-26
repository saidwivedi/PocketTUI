// ============================================================
// Drag to scroll
// ============================================================
// tmux is in mouse mode (the server sets it on this device's view session), so scrolling
// means feeding it wheel events. One SGR wheel report per cell of vertical travel,
// at the cell the finger is over, in the natural direction: dragging down pulls
// older content into view, exactly like flicking a native terminal.
//
// Reports are not sent from touchmove itself. Whole cells of travel are banked
// and drained once per frame, so a burst of moves inside one frame costs one
// flush instead of a send() per event, and the coast after a flick can share the
// same drain.
let dragScrolled = false;
let cancelCoast = () => {};
// Set by the selection and pinch gestures below for as long as they own the
// touch. A drag-to-scroll already in flight when one of them takes over is
// abandoned through abortDragScroll(), so no banked travel leaks out as a stray
// wheel report; the next fresh touch arms the scroll again.
let termGesture = null;   // null | "select" | "pinch"
let abortDragScroll = () => {};
// Set by the selection gesture below: the way anything else — a pinch, a reset,
// leaving the screen — puts the terminal back to no selection and no pill.
let cancelTermSelection = () => {};
let termSelectionCleared = () => {};
// When a selection gesture last let go, so the click the browser synthesizes
// afterwards is not read as a plain tap on the terminal.
let selectEndedAt = 0;
(function dragScroll() {
  const host = $("term-host");
  let lastY = null, accum = 0, col = 1, row = 1;

  // One frame of a fast flick is worth a few cells; past that the finger is
  // moving faster than tmux can repaint, and queueing the excess only makes the
  // scroll keep running after the finger stops. Overflow is dropped.
  const MAX_STEPS_PER_FRAME = 10;
  // Coast tuning, in cells per frame at 60fps. 0.95 a frame is the usual iOS-ish
  // figure, but it is applied per elapsed 16.7ms rather than per callback: a
  // 120Hz phone fires rAF twice as often, and a per-callback decay would coast
  // for twice as long there. The floor is half a cell — below it a frame could
  // not emit a step anyway, so the coast is over.
  const COAST_DECAY = 0.95;
  const COAST_MIN = 0.5;
  const FRAME_MS = 1000 / 60;
  // A flick has to be moving at least this fast (cells/frame) at liftoff to
  // coast at all, so a slow careful drag just stops where it was let go.
  const COAST_START = 1.0;
  // Velocity is averaged over this window, which is short enough to track the
  // last flick but long enough to survive one jittery move event.
  const VELOCITY_WINDOW = 80;   // ms

  let pending = 0;              // whole cells banked for the next flush
  let frame = null;             // rAF handle for the drain
  let coastVel = 0;             // cells/frame at 60fps, signed; 0 when not coasting
  let coastAt = 0;              // timestamp of the last coast tick
  let moves = [];               // recent {t, y} for the liftoff velocity

  // Positive steps = finger moved down = scroll back through history (64=up).
  function flush(now) {
    frame = null;
    if (coastVel) {
      // Elapsed frames since the last coast tick, so travel and decay both track
      // wall time rather than how often rAF happens to fire. Clamped, because a
      // backgrounded tab resumes with a huge gap that would otherwise land as
      // one enormous jump.
      const frames = Math.min((now - coastAt) / FRAME_MS, 3) || 1;
      coastAt = now;
      // The coast banks its own travel, then decays. Rounding toward zero means
      // a sub-cell frame contributes nothing and simply carries into the next.
      const cell = cellSize();
      accum += coastVel * frames * cell.h;
      const steps = Math.trunc(accum / cell.h);
      accum -= steps * cell.h;
      pending += steps;
      coastVel *= Math.pow(COAST_DECAY, frames);
      if (Math.abs(coastVel) < COAST_MIN) coastVel = 0;
    }
    let steps = pending;
    pending = 0;
    if (steps) {
      const seq = "\x1b[<" + (steps > 0 ? 64 : 65) + ";" + col + ";" + row + "M";
      for (let i = Math.min(Math.abs(steps), MAX_STEPS_PER_FRAME); i > 0; i--) send(seq);
    }
    if (coastVel) schedule();
  }

  function schedule() {
    if (frame === null) frame = requestAnimationFrame(flush);
  }

  function stopCoast() {
    coastVel = 0;
    moves = [];
  }
  // A new touch anywhere on the terminal, and a lost socket, both kill the coast:
  // wheel reports arriving after either one land somewhere the user did not aim.
  cancelCoast = stopCoast;

  const cellSize = termCellSize;

  host.addEventListener("touchstart", (e) => {
    // Catching the terminal mid-coast stops it dead, the way it does natively —
    // before the edge-swipe check, so grabbing the edge strip stops it too.
    stopCoast();
    // A left-edge touch belongs to the back gesture for its whole life, and a
    // touch that lands while a selection is up belongs to the selection.
    if (edgeSwipe || termGesture || !term || e.touches.length !== 1) { lastY = null; return; }
    const t = e.touches[0];
    const grid = host.querySelector(".xterm-screen") || host;
    const box = grid.getBoundingClientRect();
    const cell = cellSize();
    // 1-based cell coordinates, clamped into the grid.
    col = Math.min(term.cols, Math.max(1, Math.floor((t.clientX - box.left) / cell.w) + 1));
    row = Math.min(term.rows, Math.max(1, Math.floor((t.clientY - box.top) / cell.h) + 1));
    lastY = t.clientY;
    accum = 0;
    pending = 0;
    moves = [{ t: e.timeStamp, y: t.clientY }];
    dragScrolled = false;
  }, { passive: true });

  host.addEventListener("touchmove", (e) => {
    if (edgeSwipe || termGesture || lastY === null || e.touches.length !== 1) return;
    const y = e.touches[0].clientY;
    accum += y - lastY;
    lastY = y;
    // Kept for the liftoff velocity, trimmed to the window so the list stays a
    // couple of entries long however long the drag runs.
    moves.push({ t: e.timeStamp, y: y });
    while (moves.length > 2 && e.timeStamp - moves[0].t > VELOCITY_WINDOW) moves.shift();
    const cell = cellSize();
    let steps = Math.trunc(accum / cell.h);
    if (!steps) return;
    // Past the first cell of travel this is a scroll, not a tap.
    e.preventDefault();
    dragScrolled = true;
    accum -= steps * cell.h;
    pending += steps;
    schedule();
  }, { passive: false });

  // A tap on an image path opens the viewer, a tap on a URL opens the browser.
  // xterm's own linkifier resolves the link under the pointer from mousemove,
  // which a touch never delivers, so the tap is read here from the cell the
  // touch started on.
  host.addEventListener("touchend", () => {
    if (lastY === null || dragScrolled || edgeSwipe || termGesture || !term) return;
    const hit = linkAt(col, row + term.buffer.active.viewportY);
    if (!hit) return;
    if (hit.url) openUrl(hit.url);
    else showImage(hit.path);
  }, { passive: true });

  // Let go mid-flick and the scroll keeps going, decaying, like a native list.
  // Only a real drag coasts: a tap never set dragScrolled, and a touch the edge
  // gesture claimed is not ours to finish.
  function startCoast(e) {
    if (!dragScrolled || edgeSwipe || moves.length < 2) return;
    const first = moves[0], last = moves[moves.length - 1];
    const ms = (e.timeStamp || last.t) - first.t;
    // A stale sample means the finger was already resting at liftoff.
    if (ms <= 0 || ms > VELOCITY_WINDOW * 2) return;
    // px/ms -> cells per frame at 60fps, which is what the decay is tuned in.
    const v = ((last.y - first.y) / ms) * (1000 / 60) / cellSize().h;
    if (Math.abs(v) < COAST_START) return;
    coastVel = v;
    coastAt = performance.now();
    schedule();
  }

  const end = () => { lastY = null; accum = 0; moves = []; };
  // A cancelled touch was taken away rather than let go, so it never coasts.
  host.addEventListener("touchend", (e) => { if (termGesture) { end(); return; } startCoast(e); end(); }, { passive: true });
  host.addEventListener("touchcancel", () => { stopCoast(); end(); }, { passive: true });

  // Another gesture claiming this touch mid-drag. Dropping lastY makes every
  // remaining handler for it a no-op, and the banked travel goes with it rather
  // than flushing as wheel reports after the fact.
  abortDragScroll = () => { stopCoast(); pending = 0; end(); };
})();

