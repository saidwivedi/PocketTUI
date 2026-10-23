const { contextBridge } = require("electron");

// The app version comes in on the command line (main.js additionalArguments):
// a sandboxed preload has no app module to ask.
const flag = "--pockettui-desktop-version=";
const arg = process.argv.find((a) => a.startsWith(flag));

contextBridge.exposeInMainWorld("pockettuiDesktop", {
  version: arg ? arg.slice(flag.length) : "",
  platform: process.platform,
});
