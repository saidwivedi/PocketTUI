const { contextBridge, ipcRenderer } = require("electron");

// The app version comes in on the command line (main.js additionalArguments):
// a sandboxed preload has no app module to ask.
const flag = "--pockettui-desktop-version=";
const arg = process.argv.find((a) => a.startsWith(flag));

contextBridge.exposeInMainWorld("pockettuiDesktop", {
  version: arg ? arg.slice(flag.length) : "",
  platform: process.platform,
  // Shows and focuses this window (a notification click; see 30-notify.js).
  focus: () => ipcRenderer.send("pockettui:focus"),
  // The browser pane's <webview> tabs (46-desktop-browser.js): a guest's link
  // meant for a new tab, and clearing the pane's own session.
  browser: {
    onOpenTab: (cb) => ipcRenderer.on("pockettui:browser-open", (e, msg) => cb(msg)),
    clearBrowsingData: () => ipcRenderer.invoke("pockettui:browser-clear"),
  },
});
