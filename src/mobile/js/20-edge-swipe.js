// ============================================================
// Left-edge swipe back
// ============================================================
// The iOS back gesture, since the terminal screen has no chrome to put a button
// on. A touch that starts inside the edge strip is claimed here for the whole of
// its life — edgeSwipe stays true until touchend — so the same drag can never
// also be read as a terminal scroll.
const EDGE_ZONE = 24;    // px from the left edge that arms the gesture
const EDGE_TRIGGER = 70; // px of rightward travel that commits to going back
let edgeSwipe = false;
(function swipeBack() {
  const scr = $("screen-term");
  let startX = 0, startY = 0, armed = false, committed = false;

  function reset(animate) {
    armed = false; committed = false;
    scr.style.transition = animate ? "transform 0.18s var(--ease-out)" : "";
    scr.style.transform = "";
    if (animate) setTimeout(() => { scr.style.transition = ""; }, 200);
  }

  // Capture phase: #term-host's own touchstart bubbles first otherwise, and it
  // must already see edgeSwipe set for this touch.
  scr.addEventListener("touchstart", (e) => {
    if (e.touches.length !== 1) { edgeSwipe = false; armed = false; return; }
    const t = e.touches[0];
    armed = t.clientX <= EDGE_ZONE;
    edgeSwipe = armed;
    startX = t.clientX; startY = t.clientY;
    committed = false;
  }, { passive: true, capture: true });

  scr.addEventListener("touchmove", (e) => {
    if (!armed || e.touches.length !== 1) return;
    const dx = e.touches[0].clientX - startX;
    const dy = e.touches[0].clientY - startY;
    // Mostly-horizontal and rightward, else hand the touch back to the terminal.
    if (dx < 0 || Math.abs(dy) > Math.abs(dx)) {
      if (Math.abs(dy) > 12 && Math.abs(dy) > Math.abs(dx)) { armed = false; edgeSwipe = false; reset(false); }
      return;
    }
    e.preventDefault();
    committed = dx >= EDGE_TRIGGER;
    // Cheap affordance: the screen trails the finger, damped and capped.
    scr.style.transform = "translateX(" + Math.min(dx * 0.35, 40) + "px)";
  }, { passive: false, capture: true });

  const finish = () => {
    const go = armed && committed;
    reset(true);
    edgeSwipe = false;
    if (go) closeTerminal();
  };
  scr.addEventListener("touchend", finish, { passive: true });
  scr.addEventListener("touchcancel", finish, { passive: true });
})();

