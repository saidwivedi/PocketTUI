// ============================================================
// Pinch to zoom the font
// ============================================================
// Two fingers on the terminal scale the font the way they scale an image, then
// the grid refits and the new cols/rows go to the backend through the same
// resize path every other geometry change uses. The size is a preference, so it
// is remembered; the gesture itself is live, one apply per frame, because a
// terminal that only redraws on release gives the pinch nothing to aim at.
(function pinchZoom() {
  const host = $("term-host");
  let startDist = 0, startSize = 0, pendingSize = 0, frame = null;
  let lastFitAt = 0;

  // Fingers travel a shorter distance than a font has range: pinching a phone
  // one-handed covers maybe half the span the linear mapping would need to get
  // from 8 to 24, which is what made the gesture feel like it had to be repeated.
  // Raising the ratio to a power gives the same finger travel more effect, and
  // symmetrically in both directions since the exponent acts about 1.0.
  const PINCH_GAIN = 1.7;
  // Setting fontSize makes xterm rebuild its glyph atlas and fit() reflows the
  // grid — together a good chunk of a frame on a phone. Both are worth doing
  // live, but the reflow does not have to happen at frame rate to look smooth,
  // so it is throttled and touchend does the final authoritative one.
  const FIT_MS = 150;

  function distance(touches) {
    const dx = touches[0].clientX - touches[1].clientX;
    const dy = touches[0].clientY - touches[1].clientY;
    return Math.hypot(dx, dy);
  }

  // One apply per frame, and inside that only the work the frame actually needs:
  // a pinch delivers touchmove far faster than xterm can rebuild its glyph atlas,
  // and most frames land on the same integer size as the last one.
  function applySize(force) {
    frame = null;
    if (!term) return;
    const changed = term.options.fontSize !== pendingSize;
    if (!changed && !force) return;
    if (changed) term.options.fontSize = pendingSize;
    if (!$("screen-term").classList.contains("active")) return;
    // The refit is the expensive half, so during the gesture it runs at FIT_MS
    // rather than every frame; the release forces the last one so the grid and
    // the backend always end up agreeing with what is on screen.
    const now = performance.now();
    if (!force && now - lastFitAt < FIT_MS) return;
    lastFitAt = now;
    try { fitAddon.fit(); } catch (e) {}
    sendResize();
  }

  host.addEventListener("touchstart", (e) => {
    // The image viewer runs its own pinch over the top of the terminal; while it
    // is open this one stays out of the way entirely.
    if (!term || e.touches.length !== 2 || $("viewer").classList.contains("show")) return;
    // A second finger landing mid-drag ends the drag rather than blending with
    // it, and a selection in progress is dropped for the same reason.
    cancelTermSelection();
    abortDragScroll();
    termGesture = "pinch";
    startDist = distance(e.touches);
    startSize = term.options.fontSize || FONT_DEFAULT;
    pendingSize = startSize;
    // The first frame of the gesture is allowed its refit rather than inheriting
    // the throttle from whenever the last pinch happened to end.
    lastFitAt = 0;
  }, { passive: true });

  host.addEventListener("touchmove", (e) => {
    if (termGesture !== "pinch" || e.touches.length !== 2 || !startDist) return;
    e.preventDefault();
    const size = Math.round(startSize * Math.pow(distance(e.touches) / startDist, PINCH_GAIN));
    pendingSize = Math.min(FONT_MAX, Math.max(FONT_MIN, size));
    if (frame === null) frame = requestAnimationFrame(() => applySize(false));
  }, { passive: false });

  // Ends when the pinch drops below two fingers. The size that ends up on screen
  // is the one that is remembered, so a gesture cut short by a cancel still
  // persists what the user actually left there.
  function finish(e) {
    if (termGesture !== "pinch" || e.touches.length >= 2) return;
    termGesture = null;
    startDist = 0;
    if (frame !== null) { cancelAnimationFrame(frame); frame = null; }
    // Forced: the throttle above may have skipped the last refit, and the grid
    // and the backend have to end the gesture agreeing with the rendered size.
    applySize(true);
    try { localStorage.setItem("pockettui_fontsize", String(pendingSize)); } catch (err) {}
    dbg("pinch: fontSize=" + pendingSize);
  }
  host.addEventListener("touchend", finish, { passive: true });
  host.addEventListener("touchcancel", finish, { passive: true });
})();

