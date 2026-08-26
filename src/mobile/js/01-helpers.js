// ============================================================
// Helpers
// ============================================================
function $(id) { return document.getElementById(id); }
function el(tag, attrs={}, ...kids) {
  const e = document.createElement(tag);
  for (const [k,v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k === "html") e.innerHTML = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v);
  }
  for (const k of kids) {
    if (k == null) continue;
    e.appendChild(typeof k === "string" ? document.createTextNode(k) : k);
  }
  return e;
}
function svgIcon(id) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  const use = document.createElementNS(ns, "use");
  use.setAttributeNS("http://www.w3.org/1999/xlink", "xlink:href", "#" + id);
  use.setAttribute("href", "#" + id);
  svg.appendChild(use);
  return svg;
}
function toast(msg, ms=1800) {
  dbg("toast:", msg);
  const t = $("toast"); t.textContent = msg; t.classList.add("show");
  clearTimeout(toast._t); toast._t = setTimeout(()=>t.classList.remove("show"), ms);
}

