#!/usr/bin/env python3
"""Render the "What PocketTUI can do" guide from src/features/{catalog,manifest}.json, contract.md
and agent-intro.md.

Writes two files into --out: index.html, the standalone page (two tabs: the
gallery for people, the contract for agents), and features.md, the agent tab as
Markdown, opening with the same hand-over intro as the tab. Screenshots are
referenced as relative shots/<id>-{light,dark}.webp; the deploy copies them
there from assets/feature_shots/ (feature_shots.mjs regenerates them)."""
import argparse
import html
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "src" / "features"

# For-you card order within a group: pictured cards first, then the text-only "Also" list;
# inside each part, this order (most visual/important first), then catalog order.
ORDER = [
    "sessions.multi-device", "sessions.scrollback-search", "sessions.row-title", "sessions.rename-alias",
    "sessions.kill", "sessions.new-shortcut", "sessions.jump-shortcut", "sessions.pane-memory",
    "dictation.learned", "terminal.shift-enter", "dictation.dictate", "dictation.resolver", "compose.composer",
    "keys.key-bar", "keys.swipe-alternates", "paste.image", "paste.attach-file", "paste.desktop-files",
    "compose.paste-routing",
    "panes.side-column", "panes.two-stacked", "files.editor", "git.pane", "files.markdown", "files.thumbnails",
    "files.branch-browse", "git.block-actions", "links.paths", "files.drag-move", "files.media-viewer",
    "files.explorer", "files.upload", "files.rename", "files.follow-cwd", "files.new", "files.download-folder",
    "files.pdf",
    "browser.open", "browser.stream", "links.localhost-relay", "preview.html-file",
    "connect.install-tailscale", "connect.pair-qr", "connect.add-computer", "connect.update", "connect.type-it",
    "connect.install-lan", "connect.cli",
    "notify.prompt-chips", "notify.state-badges", "notify.bell-toggle", "notify.unread", "notify.push",
    "theme.palettes", "theme.import", "editor.vim", "keys.shortcut-list", "keys.alt-toggle",
    "hood.report", "hood.privacy-direct", "hood.pwa", "hood.demo",
]

INTROS = {
    "Stay attached to your work": "Sessions that survive, on every device you own.",
    "Type and talk from a phone": "A keyboard, a mic and gestures made for a terminal on a small screen.",
    "Files and code": "Browse, edit and review what is on the computer without leaving the session.",
    "Preview and browse": "Open what your programs print: pages, dev servers, documents.",
    "Connect computers and devices": "Pair phones and laptops, and switch between computers.",
    "Know when it needs you": "Badges and notifications when a session is waiting on you.",
    "Make it yours": "Themes, text size and keys set to your taste.",
    "Under the hood": "Diagnostics, reports and the plumbing that keeps it running.",
}

FIG_MIN = 900  # px: below this the screenshots are not rendered at all

MD_URL = "https://pockettui.com/features/features.md"
AGENT_PROMPT = "Read %s and tell me whether PocketTUI can do this, and how: " % MD_URL

SURFACE_NOTE = {"wide": "Laptop only.", "phone": "Phone only."}
GATE_WORDS = {
    "desktop": "Needs a keyboard and mouse.",
    "iOS+Android": "On iPhone and Android.",
    "iOS+Safari": "On iPhone Safari.",
    "Tailscale install": "With the Tailscale install.",
    "LAN install": "With the same-network install (no Tailscale).",
}

HOW_TO_ANSWER = (
    "You are answering a person's question about PocketTUI. Answer from the list below: say whether it can be "
    "done, name the exact place or key (the Where part), and say when a feature is laptop only or phone only. "
    "When an entry says it needs a server feature, this computer's server may lack it: the flag must be true in "
    "the `capabilities` map of `GET /api/version` (section 2 below shows how to call it), and \"needs Chromium\", "
    "\"needs httpx\" or \"needs ffmpeg\" mean that program must be installed on the computer. If the list does "
    "not have it, say it is not a feature of PocketTUI; do not guess one.")

CSS = r"""
:root{
--paper:#FAF8F3;--paper-rec:#F4EFE3;--card:#FFFDF7;--card-2:#FBF7EB;--hair:#ECE6D8;--hair-strong:#DDD4BE;
--ink:#1a1814;--ink-2:#3d362d;--secondary:#6b6357;--tertiary:#8a8073;--umber:#b85c38;--umber-soft:rgba(184,92,56,.10);
--good:#5b6e4e;--warn:#a86a3a;
--serif:ui-serif,"Iowan Old Style","Apple Garamond",Georgia,serif;
--sans:-apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI",Roboto,sans-serif;
--mono:ui-monospace,"SF Mono",Menlo,monospace;
--hdr:112px;
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;
--paper:#16140f;--paper-rec:#1a1813;--card:#1f1c16;--card-2:#25211a;--hair:#2b2720;--hair-strong:#3a3428;
--ink:#f3ede0;--ink-2:#d8d1c0;--secondary:#a89f8d;--tertiary:#857c6c;--umber:#d0724a;--umber-soft:rgba(208,114,74,.14);
--good:#8aa37a;--warn:#c98a5a;}}
:root[data-theme="dark"]{color-scheme:dark;
--paper:#16140f;--paper-rec:#1a1813;--card:#1f1c16;--card-2:#25211a;--hair:#2b2720;--hair-strong:#3a3428;
--ink:#f3ede0;--ink-2:#d8d1c0;--secondary:#a89f8d;--tertiary:#857c6c;--umber:#d0724a;--umber-soft:rgba(208,114,74,.14);
--good:#8aa37a;--warn:#c98a5a;}
*{box-sizing:border-box}
[hidden]{display:none!important}
html{scroll-padding-top:calc(var(--hdr) + 12px)}
body{margin:0;background:var(--paper);color:var(--ink);font:15px/1.5 var(--sans);overflow-x:hidden}
h1,h2,h3,h4{font-family:var(--serif);font-weight:600;text-wrap:balance;margin:0;line-height:1.25}
p{margin:0}
a{color:var(--umber);text-decoration:none}
a:hover{text-decoration:underline}
:focus-visible{outline:2px solid var(--umber);outline-offset:2px;border-radius:3px}
button{font:inherit;color:inherit;background:none;border:0;padding:0;cursor:pointer}
code,kbd,pre{font-family:var(--mono)}
code{font-size:13px;background:var(--paper-rec);padding:1px 4px;border-radius:4px;overflow-wrap:anywhere}
kbd{font-size:12px;border:1px solid var(--hair-strong);border-radius:4px;padding:0 4px;background:var(--card);white-space:nowrap}
.sep{color:var(--tertiary);padding:0 2px}
.eyebrow{font:600 11px/1.4 var(--sans);text-transform:uppercase;letter-spacing:.06em;color:var(--secondary)}
.hdr{position:sticky;top:env(safe-area-inset-top,0px);z-index:5;background:var(--paper);border-bottom:1px solid var(--hair);
 padding:10px 16px 12px;display:grid;gap:10px}
.bar{display:flex;flex-wrap:wrap;align-items:center;gap:8px 20px;max-width:1000px;margin:0 auto;width:100%}
.bar h1{font-size:20px;flex:1 1 auto}
.tabs{display:flex;gap:18px;order:3;flex-basis:100%}
.tabs button{font-family:var(--serif);font-size:16px;color:var(--secondary);padding:4px 0 3px;border-bottom:2px solid transparent}
.tabs button[aria-selected="true"]{color:var(--ink);border-bottom-color:var(--umber)}
.close{order:2;width:28px;height:28px;display:grid;place-items:center;color:var(--ink);opacity:.62}
@media (min-width:640px){.tabs{order:2;flex-basis:auto}.close{order:3}}
#q{width:100%;max-width:1000px;margin:0 auto;display:block;border:0;border-radius:8px;background:var(--paper-rec);color:var(--ink);
 font:15px/1.4 var(--sans);padding:9px 12px}
#q::placeholder{color:var(--tertiary)}
.panel{max-width:1000px;margin:0 auto;padding:16px 16px 40px;display:grid;gap:20px}
.panel>nav{display:flex;gap:8px;overflow-x:auto;padding-bottom:4px;scrollbar-width:thin}
.panel>nav a{flex:none;font:13px/1.3 var(--sans);color:var(--ink-2);border:1px solid var(--hair-strong);border-radius:999px;padding:4px 10px;white-space:nowrap}
.panel>nav a.cur{color:var(--umber);border-color:var(--umber)}
.panel>nav a span{color:var(--tertiary);margin-left:6px}
.main{min-width:0;max-width:720px;display:grid;grid-template-columns:minmax(0,1fr);gap:36px}
#p-you .main{max-width:none;gap:44px}
@media (min-width:900px){
 .panel{grid-template-columns:220px minmax(0,720px);column-gap:40px;align-items:start}
 .panel>nav{position:sticky;top:calc(var(--hdr) + 16px);flex-direction:column;gap:2px;overflow:visible}
 .panel>nav a{border:0;border-radius:0;padding:5px 0;display:flex;justify-content:space-between;white-space:normal}
 .panel>nav a.cur{color:var(--umber)}
 .bar,#q{max-width:1320px}
 .panel{max-width:1320px;grid-template-columns:200px minmax(0,1fr)}
 .main{max-width:none}
 .agent-main{max-width:720px}
}
.g{display:grid;grid-template-columns:minmax(0,1fr);gap:4px}
.g>header{display:grid;gap:4px;padding-bottom:6px}
.g>header h2{font-size:24px}
.g>header p{color:var(--secondary)}
.f{border-top:1px solid var(--hair);padding:14px 0 14px 14px;margin-left:-14px;border-left:2px solid transparent;display:grid;grid-template-columns:minmax(0,1fr);gap:5px}
.f.sel{border-left-color:var(--umber)}
.l1{display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 10px}
.l1 h3{font-size:18px;flex:1 1 14ch}
.agent .l1 h3{font-size:17px}
.l1 h3 a{color:var(--ink)}
.tags{display:flex;flex-wrap:wrap;gap:4px;margin-left:auto}
.oc{color:var(--ink-2);max-width:68ch}
#p-you .g{gap:16px}
.cards,.also ul{display:grid;grid-template-columns:minmax(0,1fr);gap:20px 24px}
@media (min-width:640px){.cards,.also ul{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media (min-width:1200px){.cards,.also ul{grid-template-columns:repeat(3,minmax(0,1fr))}}
.c{min-width:0;display:grid;grid-template-columns:minmax(0,1fr);align-content:start;gap:5px}
.c h3{font-size:17px}
.c h3 a{color:var(--ink)}
.c.sel h3 a{color:var(--umber)}
.shot{margin:0 0 5px;width:100%;aspect-ratio:16/8.6;border:1px solid var(--hair-strong);border-radius:8px;overflow:hidden;background:var(--card)}
.shot .pic{display:block;width:100%;height:100%}
.shot img{display:block;width:100%;height:100%;object-fit:cover;object-position:top left}
.shot img.d{display:none}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]) .shot img.l{display:none}
:root:not([data-theme="light"]) .shot img.d{display:block}}
:root[data-theme="dark"] .shot img.l{display:none}
:root[data-theme="dark"] .shot img.d{display:block}
.wh{font:12px/1.45 var(--sans);color:var(--secondary)}
.wh kbd{font-size:11px}
.also{display:grid;gap:4px}
.also ul{list-style:none;margin:0;padding:0;row-gap:0}
.also .c{border-top:1px solid var(--hair);padding:10px 0 12px;gap:3px}
.also .c h3{font-size:16px}
.kind{font:11px/1.5 var(--mono);text-transform:uppercase;letter-spacing:.06em;color:var(--secondary);border:1px solid var(--hair-strong);border-radius:4px;padding:0 6px}
.ctr{background:var(--paper-rec);border-radius:8px;display:flex;align-items:flex-start;gap:8px;padding:8px 10px}
.ctr pre{margin:0;flex:1;min-width:0;overflow-x:auto;font-size:13px}
.copy{color:var(--umber);font:13px/1.4 var(--sans);flex:none;padding:2px 4px}
.guide{display:grid;grid-template-columns:minmax(0,1fr);gap:14px;max-width:68ch}
.guide h2{font-size:24px}
.guide h3{font-size:20px;padding-top:10px}
.guide h4{font-size:17px}
.guide ul,.guide ol{margin:0;padding-left:1.3em;display:grid;gap:4px}
.guide pre,.tbl{background:var(--paper-rec);border-radius:8px;padding:10px 12px;overflow-x:auto;margin:0;font-size:13px}
.guide pre code{background:none;padding:0;overflow-wrap:normal}
.tbl{padding:0}
table{border-collapse:collapse;font-size:13px}
th,td{text-align:left;vertical-align:top;padding:6px 10px;border-bottom:1px solid var(--hair)}
th{font-weight:600}
td code,th code{background:none;padding:0;overflow-wrap:normal}
.tbl.routes table{font:12px/1.4 var(--mono)}
.tbl.routes td{white-space:nowrap}
.cpyrow{display:flex;flex-wrap:wrap;align-items:center;gap:8px 14px}
.btn{border:1px solid var(--hair-strong);border-radius:8px;padding:6px 12px;font:14px/1.3 var(--sans);color:var(--umber);background:var(--card)}
.note{font:13px/1.45 var(--sans);color:var(--secondary);max-width:60ch;flex:1 1 30ch}
.handover{background:var(--card);border:1px solid var(--hair-strong);border-radius:10px;padding:16px 18px 18px;display:grid;gap:12px}
.handover h2{font-size:24px}
.hrow{display:grid;gap:4px}
.hrow label{font:13px/1.4 var(--sans);color:var(--secondary)}
.hbox{display:flex;align-items:flex-start;gap:10px}
.hbox pre{margin:0;flex:1;min-width:0;background:var(--paper-rec);border-radius:8px;padding:8px 10px;font-size:13px;line-height:1.45;
 white-space:pre-wrap;overflow-wrap:anywhere;user-select:all;-webkit-user-select:all}
.hbox .btn{flex:none}
.handover .note{max-width:none}
.handover .ex{font:13px/1.45 var(--sans);color:var(--secondary)}
.all{display:grid;gap:14px;max-width:68ch}
.all h2{font-size:24px}
.all .g h3{font-size:17px;padding-top:6px}
.all ul{list-style:none;margin:0;padding:0;display:grid}
.all li{font:14px/1.45 var(--sans);color:var(--ink-2);border-top:1px solid var(--hair);padding:6px 0}
.all li strong{color:var(--ink);font-weight:600}
.gains{display:grid;gap:10px;max-width:68ch}
.gains h2{font-size:24px}
.gains ul{margin:0;padding-left:1.3em;display:grid;gap:4px}
details{border-top:1px solid var(--hair);padding-top:14px}
details.ref{display:grid;gap:0}
details.ref>summary{font-size:22px}
details.ref>.ref-body{display:grid;grid-template-columns:minmax(0,1fr);gap:36px}
@media (max-width:899px){#p-agent>nav{display:none}}
summary{font-family:var(--serif);font-size:18px;cursor:pointer}
details[open] summary{margin-bottom:12px}
.empty{color:var(--secondary)}
footer{font:12px/1.5 var(--sans);color:var(--tertiary);max-width:1000px;margin:0 auto;padding:0 16px 32px}
#toast{position:fixed;left:50%;bottom:calc(20px + env(safe-area-inset-bottom,0px));transform:translateX(-50%);max-width:calc(100% - 32px);
 background:var(--ink);color:var(--paper);font:14px/1.4 var(--sans);padding:9px 14px;border-radius:8px;z-index:9;transition:opacity .2s}
.shot{cursor:zoom-in}
#lb{position:fixed;inset:0;z-index:20;background:color-mix(in srgb,var(--ink) 88%,transparent);display:flex;flex-direction:column;
 align-items:center;justify-content:center;gap:10px;padding:24px;cursor:zoom-out;opacity:0;transition:opacity .12s}
#lb.on{opacity:1}
#lb img{display:block;width:auto;height:auto;max-width:calc(100vw - 48px);max-height:calc(100vh - 96px);border-radius:8px;cursor:zoom-out}
#lb p{font:13px/1.45 var(--sans);color:var(--paper);max-width:min(68ch,calc(100vw - 48px));text-align:center}
#lb p strong{font-weight:600}
#lb .x{position:absolute;top:12px;right:12px;width:32px;height:32px;display:grid;place-items:center;color:var(--paper)}
@media (prefers-reduced-motion:reduce){*{transition:none!important;scroll-behavior:auto!important}}
"""

JS = r"""
(function(){
var MD = __MD__;
var reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
var hdr = document.querySelector('.hdr');
function sizeHdr(){ document.documentElement.style.setProperty('--hdr', hdr.offsetHeight + 'px'); }
sizeHdr(); addEventListener('resize', sizeHdr);
var tabs = [].slice.call(document.querySelectorAll('.tabs button'));
var toastEl = document.getElementById('toast'), toastT;
var selected = null;
var ref = document.getElementById('ref');
if (ref && matchMedia('(max-width: 899px)').matches) ref.open = false;
function inRef(el){ if (ref && el && ref.contains(el)) ref.open = true; }
function toast(msg){ toastEl.textContent = msg; toastEl.hidden = false; clearTimeout(toastT);
  toastT = setTimeout(function(){ toastEl.hidden = true; }, 2000); }
function panel(name){ return document.getElementById('p-' + name); }
function current(){ return tabs.filter(function(b){ return b.getAttribute('aria-selected') === 'true'; })[0].dataset.tab; }
function showTab(name){
  tabs.forEach(function(b){ var on = b.dataset.tab === name; b.setAttribute('aria-selected', on); b.tabIndex = on ? 0 : -1;
    panel(b.dataset.tab).hidden = !on; });
}
function art(id, tab){ return document.getElementById(tab === 'agent' ? id + '-agent' : id); }
function select(id){
  selected = id;
  document.querySelectorAll('.sel').forEach(function(a){ a.classList.remove('sel'); });
  [art(id, 'you'), art(id, 'agent')].forEach(function(a){ if (a) a.classList.add('sel'); });
  history.replaceState(null, '', '#' + id);
}
function go(id, tab){
  var a = art(id, tab); if (!a) return;
  if (current() !== tab) showTab(tab);
  if (a.hidden) { q.value = ''; filter(); }
  inRef(a);
  select(id);
  a.scrollIntoView({block: 'start', behavior: reduce ? 'auto' : 'smooth'});
}
tabs.forEach(function(b){
  b.addEventListener('click', function(){ showTab(b.dataset.tab);
    var a = selected && art(selected, b.dataset.tab);
    if (a && !a.hidden) a.scrollIntoView({block: 'start'}); else scrollTo(0, 0); });
  b.addEventListener('keydown', function(e){
    if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
    var n = tabs[(tabs.indexOf(b) + 1) % tabs.length]; n.click(); n.focus(); });
});
document.addEventListener('click', function(e){
  var t = e.target.closest('[data-copy],.f h3 a,.c h3 a,#copy-md');
  if (!t) return;
  if (t.id === 'copy-md') copy(MD, null, t);
  else if (t.dataset.copy) copy(t.dataset.copy, t.previousElementSibling, t);
  else { e.preventDefault(); var a = t.closest('.f,.c'); select(a.dataset.for || a.id); }
});
function copy(text, pre, btn){
  var label = btn.textContent, p;
  try { p = navigator.clipboard.writeText(text); } catch (err) { p = Promise.reject(err); }
  p.then(function(){
    btn.textContent = 'Copied'; setTimeout(function(){ btn.textContent = label; }, 1500);
  }).catch(function(){
    var r = document.createRange(), s = getSelection();
    if (pre) { r.selectNodeContents(pre); s.removeAllRanges(); s.addRange(r); toast('Selected: press copy on your keyboard'); }
    else toast('Copy was blocked by the browser');
  });
}
// search
var q = document.getElementById('q');
function filter(){
  var terms = q.value.toLowerCase().trim().split(/\s+/).filter(Boolean);
  if (terms.length && ref) ref.open = true;
  ['you', 'agent'].forEach(function(tab){
    var p = panel(tab), total = 0;
    p.querySelectorAll('section.g').forEach(function(sec){
      var n = 0;
      sec.querySelectorAll('[data-s]').forEach(function(a){
        var hay = a.dataset.s, ok = terms.every(function(t){ return hay.indexOf(t) >= 0; });
        a.hidden = !ok; if (ok) n++; });
      sec.querySelectorAll('.cards,.also').forEach(function(b){ b.hidden = !b.querySelector('[data-s]:not([hidden])'); });
      sec.hidden = n === 0; total += n;
      var c = p.querySelector('nav a[href="#' + sec.id + '"]');
      if (c) { c.hidden = n === 0; c.querySelector('span').textContent = n; }
    });
    p.querySelector('.empty').hidden = total > 0;
  });
}
q.addEventListener('input', filter);
// nav highlight
var io = new IntersectionObserver(function(entries){
  entries.forEach(function(en){
    if (!en.isIntersecting) return;
    var nav = en.target.closest('.panel').querySelector('nav');
    nav.querySelectorAll('a').forEach(function(a){ a.classList.toggle('cur', a.getAttribute('href') === '#' + en.target.id); });
  });
}, {rootMargin: '-120px 0px -65% 0px'});
document.querySelectorAll('section.g').forEach(function(s){ io.observe(s); });
document.querySelectorAll('.panel nav a').forEach(function(a){
  a.addEventListener('click', function(e){ e.preventDefault();
    var t = document.getElementById(a.getAttribute('href').slice(1)); inRef(t);
    t.scrollIntoView({block: 'start', behavior: reduce ? 'auto' : 'smooth'}); });
});
// lightbox
var lb = document.getElementById('lb'), lbImg = lb.querySelector('img'), lbCap = lb.querySelector('p'), lbFrom = null, lbT;
var outside = [].slice.call(document.querySelectorAll('.hdr,.panel,footer'));
function lbOpen(fig){
  var img = [].filter.call(fig.querySelectorAll('img'), function(i){ return i.offsetParent !== null; })[0]
            || fig.querySelector('img'), c = fig.closest('.c'), t = c && c.querySelector('h3');
  lbFrom = fig; clearTimeout(lbT);
  lbImg.src = img.currentSrc || img.src; lbImg.alt = img.alt;
  lbCap.innerHTML = ''; var st = document.createElement('strong'); st.textContent = t ? t.textContent : '';
  lbCap.appendChild(st); if (img.alt) lbCap.appendChild(document.createTextNode(' — ' + img.alt));
  lb.hidden = false; document.body.style.overflow = 'hidden';
  outside.forEach(function(el){ el.setAttribute('aria-hidden', 'true'); });
  requestAnimationFrame(function(){ requestAnimationFrame(function(){ lb.classList.add('on'); }); });
  lb.querySelector('.x').focus();
}
function lbClose(){
  if (lb.hidden) return;
  lb.classList.remove('on'); document.body.style.overflow = '';
  outside.forEach(function(el){ el.removeAttribute('aria-hidden'); });
  var done = function(){ lb.hidden = true; lbImg.removeAttribute('src'); };
  if (reduce) done(); else lbT = setTimeout(done, 120);
  if (lbFrom) lbFrom.focus();
}
document.querySelectorAll('.c .shot').forEach(function(fig){
  fig.tabIndex = 0; fig.setAttribute('role', 'button'); fig.setAttribute('aria-label', 'Expand screenshot');
  fig.addEventListener('click', function(){ lbOpen(fig); });
  fig.addEventListener('keydown', function(e){
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); lbOpen(fig); } });
});
lb.addEventListener('click', lbClose);
document.addEventListener('keydown', function(e){
  if (lb.hidden) return;
  if (e.key === 'Escape') { e.preventDefault(); lbClose(); }
  else if (e.key === 'Tab') { e.preventDefault(); lb.querySelector('.x').focus(); }
});
// Framed by the app, the cross asks the app to take the frame down; opened on
// its own, there is nothing to close into, so the cross is not shown.
var framed = window.parent !== window;
var closeBtn = document.querySelector('.hdr .close');
function closeSheet(){ if (framed) window.parent.postMessage({type: 'pockettui-features-close'}, '*'); }
closeBtn.hidden = !framed;
closeBtn.addEventListener('click', closeSheet);
// Last in line: the lightbox answers its own Escape above, and a search field
// with text in it clears itself.
document.addEventListener('keydown', function(e){
  if (e.key !== 'Escape' || !lb.hidden || e.defaultPrevented) return;
  if (e.target === q && q.value) return;
  closeSheet();
});
// hash on load
var h = decodeURIComponent(location.hash.slice(1));
if (h) {
  if (/-agent$/.test(h) && art(h.replace(/-agent$/, ''), 'agent')) go(h.replace(/-agent$/, ''), 'agent');
  else if (art(h, 'you')) go(h, 'you');
}
})();
"""

KEY_RE = re.compile(
    r"\b((?:(?:Ctrl|Cmd|Shift|Alt|Option)\+)+(?:[A-Z0-9]\b|Enter|Tab|Esc|Escape|Space)|(?:Shift|Option)(?=\+drag)|Enter\b|Esc\b)")


def e(s):
    return html.escape(s, quote=True)


def where_html(text):
    parts = text.split(" > ")
    out = []
    for p in parts:
        chunks, pos = [], 0
        for m in KEY_RE.finditer(p):
            chunks.append(e(p[pos:m.start()]))
            chunks.append("<kbd>%s</kbd>" % e(m.group(0)))
            pos = m.end()
        chunks.append(e(p[pos:]))
        out.append("".join(chunks))
    return '<span class="sep" aria-hidden="true">›</span>'.join(out)


# ---- minimal markdown ----

def inline(s):
    codes = []

    def stash(m):
        body = m.group(2).strip() if len(m.group(1)) == 2 else m.group(2)
        codes.append("<code>%s</code>" % e(body))
        return "\x00%d\x00" % (len(codes) - 1)

    t = e(re.sub(r"(``?)(.+?)\1", stash, s))
    t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
    return re.sub(r"\x00(\d+)\x00", lambda m: codes[int(m.group(1))], t)


def split_row(line):
    cells, cur, code = [], [], False
    for ch in line.strip().strip("|"):
        if ch == "`":
            code = not code
        if ch == "|" and not code:
            cells.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    cells.append("".join(cur).strip())
    return cells


def table_html(rows, cls="tbl"):
    head, body = rows[0], rows[2:]
    t = ['<div class="%s"><table><thead><tr>' % cls]
    t += ["<th>%s</th>" % inline(c) for c in split_row(head)]
    t.append("</tr></thead><tbody>")
    for r in body:
        t.append("<tr>" + "".join("<td>%s</td>" % inline(c) for c in split_row(r)) + "</tr>")
    t.append("</tbody></table></div>")
    return "".join(t)


def md_html(md, shift=1):
    lines, out, i = md.split("\n"), [], 0
    while i < len(lines):
        ln = lines[i]
        if not ln.strip() or ln.strip() == "---":
            i += 1
        elif ln.startswith("```"):
            j = i + 1
            while j < len(lines) and not lines[j].startswith("```"):
                j += 1
            out.append("<pre><code>%s</code></pre>" % e("\n".join(lines[i + 1:j])))
            i = j + 1
        elif ln.startswith("#"):
            lvl = len(ln) - len(ln.lstrip("#"))
            out.append("<h{0}>{1}</h{0}>".format(min(lvl + shift, 6), inline(ln[lvl:].strip())))
            i += 1
        elif ln.startswith("|"):
            j = i
            while j < len(lines) and lines[j].startswith("|"):
                j += 1
            out.append(table_html(lines[i:j]))
            i = j
        elif re.match(r"(- |\d+\. )", ln):
            ordered = not ln.startswith("- ")
            pat = r"\d+\. " if ordered else r"- "
            items = []
            while i < len(lines) and re.match(pat, lines[i]):
                items.append(re.sub("^" + pat, "", lines[i]))
                i += 1
                while i < len(lines) and lines[i].startswith("  ") and lines[i].strip():
                    items[-1] += " " + lines[i].strip()
                    i += 1
            tag = "ol" if ordered else "ul"
            out.append("<%s>%s</%s>" % (tag, "".join("<li>%s</li>" % inline(t) for t in items), tag))
        else:
            para = []
            while i < len(lines) and lines[i].strip() and not re.match(r"(#|```|\||- |\d+\. |---)", lines[i]):
                para.append(lines[i].strip())
                i += 1
            out.append("<p>%s</p>" % inline(" ".join(para)))
    return "\n".join(out)


def sections(md):
    """Split contract.md on '## N.' headings -> {N: text including heading}."""
    parts = {}
    for m in re.finditer(r"^## (\d+)\..*?(?=^## \d+\.|\Z)", md, re.S | re.M):
        parts[int(m.group(1))] = m.group(0).strip()
    return parts


# ---- page ----

def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def tags_html(r):
    t = ['<span class="kind">%s</span>' % e(r["agent"]["kind"])]
    if r["needs"]:
        t.append('<span class="kind">needs %s</span>' % e(r["needs"]))
    return '<span class="tags">%s</span>' % "".join(t)


def search_text(r, with_id=True):
    s = " ".join([r["title"], r["outcome"], r["where"]] + ([r["id"]] if with_id else []))
    return e(s.lower())


def figure_html(shot, man):
    m = man[shot]
    # Both pictures, and the stylesheet shows the one for the theme in force: the
    # page's theme can be the app's rather than the OS's (THEME_STAMP), which a
    # <source media> cannot follow. A lazy image that is not displayed is never
    # fetched, so each visit downloads one of the pair.
    img = ('<img class="{t}" src="shots/{id}-{theme}.webp" width="{w}" height="{h}" '
           'loading="lazy" decoding="async" alt="{alt}">')
    fmt = dict(id=e(shot), w=m["width"], h=m["height"], alt=e(m["caption"]))
    return ('<figure class="shot"><span class="pic">%s%s</span></figure>'
            % (img.format(t="l", theme="light", **fmt), img.format(t="d", theme="dark", **fmt)))


def group_you(rs, man):
    """Pictured cards in a grid, then the text-only ones as a compact "Also" list."""
    rank = {i: n for n, i in enumerate(ORDER)}
    rs = sorted(rs, key=lambda r: rank.get(r["id"], len(ORDER)))
    pics = [r for r in rs if r.get("shot") in man]
    rest = [r for r in rs if r.get("shot") not in man]
    out = []
    if pics:
        out.append('<div class="cards">%s</div>' % "".join(card_you(r, man) for r in pics))
    if rest:
        out.append('<div class="also">%s<ul>%s</ul></div>' % (
            '<span class="eyebrow">Also</span>' if pics else "", "".join(card_you(r, None, "li") for r in rest)))
    return "".join(out)


def card_you(r, man, tag="article"):
    w = r["where"]
    return ('<{tag} id="{id}" class="c" data-s="{s}">{fig}'
            '<h3><a href="#{id}">{t}</a></h3>'
            '<p class="oc">{o}</p>{wh}</{tag}>').format(
        tag=tag, id=e(r["id"]), s=search_text(r, False), fig=figure_html(r["shot"], man) if man else "",
        t=e(r["title"]), o=e(r["outcome"]),
        wh="" if w.startswith("Automatic") else '<p class="wh">%s</p>' % where_html(w))


def entry_agent(r):
    a = r["agent"]
    ctr = ""
    if a.get("contract"):
        ctr = ('<div class="ctr"><pre>%s</pre><button type="button" class="copy" data-copy="%s">Copy</button></div>'
               % (e(a["contract"]), e(a["contract"])))
    s = search_text(r) + " " + e(a["text"].lower())
    return ('<article id="{id}-agent" class="f agent" data-for="{id}" data-group="{g}" data-s="{s}">'
            '<div class="l1"><h3><a href="#{id}">{t}</a></h3>{tags}</div>'
            '<p class="oc">{x}</p>{ctr}'
            '</article>').format(id=e(r["id"]), g=e(r["group"]), s=s, t=e(r["title"]), tags=tags_html(r),
                                 x=e(a["text"]), ctr=ctr)


def nav_html(groups, suffix):
    return "<nav aria-label=\"Groups\">%s</nav>" % "".join(
        '<a href="#g-%s%s">%s<span>%d</span></a>' % (slug(g), suffix, e(g), n) for g, n in groups)


def handover_html():
    rows = [("ho-url", "Address", MD_URL, ""),
            ("ho-prompt", "Ask your agent", AGENT_PROMPT,
             "Finish the sentence with your question, for example: open a file on my laptop from my phone.")]
    out = ['<section class="handover" aria-labelledby="ho-h"><h2 id="ho-h">Give this to your agent</h2>']
    for i, label, text, ex in rows:
        out.append('<div class="hrow"><label for="%s">%s</label><div class="hbox"><pre id="%s" tabindex="0">%s</pre>'
                   '<button type="button" class="btn" data-copy="%s">Copy</button></div>%s</div>'
                   % (i, e(label), i, e(text), e(text), '<p class="ex">%s</p>' % e(ex) if ex else ""))
    out.append('<p class="note">Claude Code, Codex and similar assistants fetch the address themselves. It is plain '
               'Markdown, and https://pockettui.com/llms.txt points to it.</p></section>')
    return "".join(out)


def intro_md():
    return "\n\n".join(["# PocketTUI: what it can do and how", "Address: %s" % MD_URL,
                         "Ask your agent: `%s`" % AGENT_PROMPT, "## How to answer", HOW_TO_ANSWER])


def notes(r):
    """The trailing notes of an Everything entry: surface, gates in words, server flag."""
    out = [SURFACE_NOTE[r["surface"]]] if r["surface"] in SURFACE_NOTE else []
    g = r["gates"]
    if g:
        key = "+".join(g)
        if key in GATE_WORDS:
            out.append(GATE_WORDS[key])
        else:
            out += [x[0].upper() + x[1:] + "." if x.startswith("needs ") else "Applies to %s." % x for x in g]
    return out


def all_md(order, recs):
    out = ["## Everything PocketTUI can do"]
    for g in order:
        lines = []
        for r in recs:
            if r["group"] != g:
                continue
            t = "- **%s** — %s Where: %s." % (r["title"], r["outcome"], r["where"])
            rest = notes(r) + (["Needs server feature `%s`." % r["needs"]] if r["needs"] else [])
            lines.append(" ".join([t] + rest))
        out.append("### %s\n\n%s" % (g, "\n".join(lines)))
    return "\n\n".join(out)


def all_html(order, recs):
    out = ['<div class="all"><h2>Everything PocketTUI can do</h2>']
    for g in order:
        items = []
        for r in recs:
            if r["group"] != g:
                continue
            rest = notes(r)
            tail = " ".join(e(x) for x in rest)
            if r["needs"]:
                tail += (" " if tail else "") + "Needs server feature <code>%s</code>." % e(r["needs"])
            s = search_text(r) + " " + e(" ".join(rest + [r["needs"] or ""]).lower())
            items.append('<li data-s="%s"><strong>%s</strong> — %s Where: %s.%s</li>' % (
                s, e(r["title"]), e(r["outcome"]), e(r["where"]), " " + tail if tail else ""))
        out.append('<section class="g" id="g-%s-all" data-group="%s"><h3>%s</h3><ul>%s</ul></section>' % (
            slug(g), e(g), e(g), "".join(items)))
    return "".join(out) + "</div>"


def build(recs, contract_md, man, gains_md):
    """-> (page body, the agent tab as Markdown)."""
    order = []
    for r in recs:
        if r["group"] not in order:
            order.append(r["group"])
    by_group = {g: sorted([r for r in recs if r["group"] == g], key=lambda r: (-r["hidden"], r["title"].lower()))
                for g in order}

    # tab 1: only the curated records (show: true)
    shown = {g: [r for r in by_group[g] if r.get("show")] for g in order}
    you_groups = [(g, len(shown[g])) for g in order if shown[g]]
    you = []
    for g, _ in you_groups:
        you.append('<section class="g" id="g-%s" data-group="%s"><header><h2>%s</h2><p>%s</p></header>%s</section>' % (
            slug(g), e(g), e(g), e(INTROS.get(g, "")), group_you(shown[g], man)))
    you.append('<p class="empty" hidden>Nothing matches.</p>')

    # tab 2
    sec = sections(contract_md)
    guide_md = "\n\n".join(sec[k] for k in (1, 2, 3, 4) if k in sec)
    head = [handover_html(),
            '<div class="gains">%s</div>' % md_html(gains_md, shift=0),
            '<div class="cpyrow"><button type="button" class="btn" id="copy-md">Copy the whole reference as Markdown</button>'
            '<span class="note">The same text is at '
            '<a href="features.md" target="_blank" rel="noopener">pockettui.com/features/features.md</a>.</span></div>']
    agent = [all_html(order, recs),
             '<div class="guide"><h2>How to work with PocketTUI</h2>%s</div>' % md_html(guide_md, shift=1)]
    md_out = [intro_md(), all_md(order, recs), gains_md.strip(), "# How to work with PocketTUI", guide_md]
    agent_groups = []
    for g in order:
        rs = [r for r in by_group[g] if r["agent"]]
        if not rs:
            continue
        agent_groups.append((g, len(rs)))
        agent.append('<section class="g" id="g-%s-agent" data-group="%s"><header><h2>%s</h2></header>%s</section>' % (
            slug(g), e(g), e(g), "".join(entry_agent(r) for r in rs)))
        md_out.append("## " + g)
        for r in rs:
            block = "### %s\n%s" % (r["title"], r["agent"]["text"])
            if r["agent"].get("contract"):
                block += "\n`%s`" % r["agent"]["contract"]
            md_out.append(block)
    agent.append('<p class="empty" hidden>Nothing matches.</p>')
    routes = ""
    if 5 in sec:
        rows = [l for l in sec[5].split("\n") if l.startswith("|")]
        routes = table_html(rows, "tbl routes")
    agent.append('<details><summary>Every route on the server, classified</summary>%s</details>' % routes)
    agent = head + ['<details class="ref" id="ref" open><summary>Full reference</summary><div class="ref-body">%s</div></details>'
                    % "".join(agent)]

    md_text = "\n\n".join(md_out) + "\n"
    md_js = json.dumps(md_text).replace("</", "<\\/")
    close_svg = ('<svg width="15" height="15" viewBox="0 0 15 15" fill="none" stroke="currentColor" stroke-width="1.6" '
                 'stroke-linecap="round" aria-hidden="true"><path d="M3 3l9 9M12 3l-9 9"/></svg>')
    return "\n".join([
        "<style>%s</style>" % CSS.strip(),
        '<div class="hdr"><div class="bar"><h1>What PocketTUI can do</h1>'
        '<div class="tabs" role="tablist">'
        '<button type="button" role="tab" data-tab="you" aria-controls="p-you" aria-selected="true">For you</button>'
        '<button type="button" role="tab" data-tab="agent" aria-controls="p-agent" aria-selected="false" tabindex="-1">For your agent</button>'
        '</div><button type="button" class="close" aria-label="Close" hidden>%s</button></div>' % close_svg,
        '<input id="q" type="search" autocomplete="off" placeholder="Search %d things…" aria-label="Search features"></div>' % sum(n for _, n in you_groups),
        '<div class="panel" id="p-you" role="tabpanel">%s<div class="main">%s</div></div>' % (nav_html(you_groups, ""), "".join(you)),
        '<div class="panel" id="p-agent" role="tabpanel" hidden>%s<div class="main agent-main">%s</div></div>' % (
            nav_html(agent_groups, "-agent"), "".join(agent)),
        '<footer>Rendered from catalog.json, %d features.</footer>' % len(recs),
        '<div id="toast" role="status" hidden></div>',
        '<div id="lb" role="dialog" aria-modal="true" aria-label="Screenshot" hidden>'
        '<button type="button" class="x" aria-label="Close">%s</button><img alt=""><p></p></div>' % close_svg,
        "<script>%s</script>" % JS.replace("__MD__", md_js).strip(),
        "",
    ]), md_text


STANDALONE_HEAD = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>What PocketTUI can do</title>
<meta name="description" content="Everything PocketTUI does, with screenshots, and the contract an agent in one of its terminals can rely on.">
__THEME_STAMP__
<style>
  :root { padding-top: env(safe-area-inset-top, 0px); padding-bottom: env(safe-area-inset-bottom, 0px); }
  body { margin: 0; font: 14px -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
  img { max-width: 100%; }
  [hidden] { display: none !important; }
</style>
</head>
<body>
"""


# Framed by the app, the page takes the app's theme rather than the OS's:
# ?theme=light|dark is stamped on the root as data-theme before anything paints,
# which the stylesheet's two dark blocks and the screenshots' pick both key off,
# and the app posts {type: "pockettui-theme"} to restamp when its theme changes
# while the page is open (44-features-guide.js). Opened on its own with no
# param, nothing is stamped and the OS decides, as before.
THEME_STAMP = """<script>
(function(){
  var root = document.documentElement;
  function stamp(t){ if (t === "light" || t === "dark") root.setAttribute("data-theme", t); }
  try { stamp(new URLSearchParams(location.search).get("theme")); } catch (e) {}
  window.addEventListener("message", function(e){
    var d = e.data;
    if (e.source === window.parent && d && d.type === "pockettui-theme") stamp(d.theme);
  });
})();
</script>"""
STANDALONE_HEAD = STANDALONE_HEAD.replace("__THEME_STAMP__", THEME_STAMP)


def standalone(fragment):
    return STANDALONE_HEAD + fragment + "</body>\n</html>\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", required=True, help="directory to write index.html and features.md into")
    ap.add_argument("--catalog", default=str(SRC / "catalog.json"))
    ap.add_argument("--contract", default=str(SRC / "contract.md"))
    ap.add_argument("--manifest", default=str(SRC / "manifest.json"))
    ap.add_argument("--intro", default=str(SRC / "agent-intro.md"), help="the tab's 'What your agent gains' list")
    a = ap.parse_args()
    out = Path(a.out).resolve()
    # The prototype lives in explore/ and is not this script's to overwrite.
    if (HERE / "explore") in [out, *out.parents]:
        ap.error("--out must not be inside explore/")
    recs = json.loads(Path(a.catalog).read_text(encoding="utf-8"))
    man = json.loads(Path(a.manifest).read_text(encoding="utf-8"))
    frag, md = build(recs, Path(a.contract).read_text(encoding="utf-8"), man,
                     Path(a.intro).read_text(encoding="utf-8"))
    out.mkdir(parents=True, exist_ok=True)
    for name, text in (("index.html", standalone(frag)), ("features.md", md)):
        (out / name).write_text(text, encoding="utf-8")
        print("wrote %s (%d bytes)" % (out / name, len(text.encode("utf-8"))))


if __name__ == "__main__":
    main()
