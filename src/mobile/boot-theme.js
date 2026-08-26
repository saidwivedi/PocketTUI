<script>
// Resolve theme before paint to avoid flash. The status-bar tint and the root
// background must be set here too — they track the app's resolved theme, not the
// OS preference, so a media-query meta tag would be wrong whenever they differ.
// One-time rename migration (agents_* -> pockettui_*). Runs before any key is
// read so an existing install keeps its backend and theme instead of falling
// back to the first-run sheet. Drop once no old installs are left.
(function() {
  ["backend", "theme"].forEach(function(k) {
    var old = localStorage.getItem("agents_" + k);
    if (old !== null && localStorage.getItem("pockettui_" + k) === null) {
      localStorage.setItem("pockettui_" + k, old);
    }
    if (old !== null) localStorage.removeItem("agents_" + k);
  });
})();

(function() {
  var pref = localStorage.getItem("pockettui_theme") || "auto";
  var dark = pref === "dark" || (pref === "auto" && window.matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
  var bg = dark ? "#16140f" : "#FAF8F3";
  document.documentElement.style.backgroundColor = bg;
  document.getElementById("meta-theme-color").content = bg;
})();
</script>
