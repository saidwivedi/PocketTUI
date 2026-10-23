const { contextBridge, ipcRenderer } = require("electron");

// The Basic-auth prompt (auth.html). main.js answers the request that opened
// this window and no other.
contextBridge.exposeInMainWorld("pockettuiAuth", {
  info: () => ipcRenderer.invoke("pockettui:auth-info"),
  submit: (user, pass) => ipcRenderer.send("pockettui:auth-submit", String(user), String(pass)),
  cancel: () => ipcRenderer.send("pockettui:auth-cancel"),
});
