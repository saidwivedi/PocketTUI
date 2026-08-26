// ============================================================
// Terminal cell geometry
// ============================================================
// Cell size from the rendered grid: the screen element measures cols x rows,
// so dividing it needs no xterm internals and survives a font or zoom change.
// Shared by the scroll, selection and pinch gestures, all of which have to turn
// a touch point into a cell.
function termCellSize() {
  const screen = $("term-host").querySelector(".xterm-screen");
  if (screen && term && term.cols > 0 && term.rows > 0) {
    const r = screen.getBoundingClientRect();
    if (r.height > 0 && r.width > 0) {
      return { w: r.width / term.cols, h: r.height / term.rows };
    }
  }
  return { w: 8, h: 17 };
}

// 0-based viewport cell under a touch point, clamped into the grid.
function termCellAt(clientX, clientY) {
  const host = $("term-host");
  const grid = host.querySelector(".xterm-screen") || host;
  const box = grid.getBoundingClientRect();
  const cell = termCellSize();
  return {
    col: Math.min(term.cols - 1, Math.max(0, Math.floor((clientX - box.left) / cell.w))),
    row: Math.min(term.rows - 1, Math.max(0, Math.floor((clientY - box.top) / cell.h))),
  };
}

