// ---- commands -----------------------------------------------------------
// Minimal quote-aware split: enough for `echo "two words"`.
function demoSplit(line) {
  const out = [];
  const re = /"([^"]*)"|'([^']*)'|(\S+)/g;
  let m;
  while ((m = re.exec(line)) !== null) out.push(m[1] !== undefined ? m[1] : m[2] !== undefined ? m[2] : m[3]);
  return out;
}

function demoLs(args) {
  const flags = args.filter(a => a.startsWith("-")).join("");
  const rest = args.filter(a => !a.startsWith("-"));
  const all = flags.includes("a"), long = flags.includes("l");
  const target = demoResolve(rest[0] || ".");
  const node = demoNodeAt(target);
  if (node === undefined) { demoLine("ls: " + (rest[0] || ".") + ": No such file or directory"); return; }
  if (!demoIsDir(node)) { demoLine(rest[0]); return; }
  let names = Object.keys(node).filter(n => all || !n.startsWith("."));
  if (all) names = [".", ".."].concat(names);
  names.sort();
  if (!names.length) return;
  const paint = (n) => {
    if (n === "." || n === "..") return D.blu + D.b + n + D.r;
    if (demoIsDir(node[n])) return D.blu + D.b + n + D.r;
    if (demoIsExec(n)) return D.grn + n + D.r;
    return n;
  };
  if (long) {
    demoLine("total " + (names.length * 4));
    for (const n of names) {
      const dir = n === "." || n === ".." || demoIsDir(node[n]);
      const exec = !dir && demoIsExec(n);
      const mode = dir ? "drwxr-xr-x" : (exec ? "-rwxr-xr-x" : "-rw-r--r--");
      const size = dir ? 4096 : String(node[n]).length;
      const links = dir ? 3 : 1;
      demoLine(mode + " " + links + " " + DEMO_USER + "  " + DEMO_USER + " " +
        String(size).padStart(6) + " " + DEMO_STAMP + " " + paint(n));
    }
    return;
  }
  // Columns sized to the terminal so the listing does not wrap mid-name.
  const width = Math.max(1, (term && term.cols) || 40);
  const colw = Math.min(width, names.reduce((a, n) => Math.max(a, n.length), 0) + 2);
  const cols = Math.max(1, Math.floor(width / colw));
  for (let i = 0; i < names.length; i += cols) {
    const row = names.slice(i, i + cols)
      .map((n, j) => paint(n) + (j < cols - 1 ? " ".repeat(Math.max(1, colw - n.length)) : ""));
    demoLine(row.join("").trimEnd());
  }
}

const DEMO_STAMP = "Mar 14 09:22";

function demoTree(parts, prefix, depth) {
  const node = demoNodeAt(parts);
  if (!demoIsDir(node) || depth > 3) return;
  const names = Object.keys(node).filter(n => !n.startsWith(".")).sort();
  names.forEach((n, i) => {
    const last = i === names.length - 1;
    const dir = demoIsDir(node[n]);
    demoLine(prefix + (last ? "└── " : "├── ") +
      (dir ? D.blu + D.b + n + D.r : demoIsExec(n) ? D.grn + n + D.r : n));
    if (dir) demoTree(parts.concat(n), prefix + (last ? "    " : "│   "), depth + 1);
  });
}

function demoCat(args, mode) {
  if (!args.length) { demoLine(mode + ": missing file operand"); return; }
  for (const a of args) {
    const node = demoNodeAt(demoResolve(a));
    if (node === undefined) { demoLine(mode + ": " + a + ": No such file or directory"); continue; }
    if (demoIsDir(node)) { demoLine(mode + ": " + a + ": Is a directory"); continue; }
    let lines = String(node).replace(/\n$/, "").split("\n");
    if (mode === "head") lines = lines.slice(0, 10);
    if (mode === "tail") lines = lines.slice(-10);
    for (const l of lines) demoLine(l);
  }
}

// The commands table. Each entry runs synchronously and returns nothing; the
// prompt is reprinted by the caller unless the command took over the shell.
const DEMO_COMMANDS = {
  help: () => {
    demoLine("");
    demoLine(D.b + "Demo commands" + D.r + D.gry + " — a small hand-written subset" + D.r);
    demoLine("");
    const group = (title, items) => {
      demoLine("  " + D.yel + title + D.r);
      for (const [c, what] of items) {
        demoLine("    " + D.grn + c.padEnd(14) + D.r + D.gry + what + D.r);
      }
      demoLine("");
    };
    group("files", [
      ["ls [-l] [-a]", "list a directory"],
      ["cd <dir>", "change directory"],
      ["pwd", "print the working directory"],
      ["cat <file>", "print a file"],
      ["head / tail", "first or last ten lines"],
      ["tree", "the tree below here"],
      ["mkdir / touch", "create (in memory only)"],
      ["echo <text>", "print text"],
    ]);
    group("system", [
      ["whoami / date", "user and clock"],
      ["uname -a", "kernel line"],
      ["ps / top", "processes"],
      ["df -h", "disk usage"],
      ["tmux ls", "sessions"],
      ["curl / ping", "network"],
      ["history", "what you have typed"],
      ["man <cmd>", "a very short manual"],
    ]);
    group("git", [
      ["git status", "working tree"],
      ["git log", "recent commits"],
      ["git branch", "branches"],
    ]);
    group("agents", [
      ["claude", "simulated Claude Code interface"],
      ["codex", "simulated Codex interface"],
    ]);
    group("demo", [
      ["clear", "clear the screen"],
      ["help", "this list"],
      ["exit", "leave the demo"],
    ]);
  },

  ls: (a) => demoLs(a),
  ll: (a) => demoLs(["-l"].concat(a)),
  pwd: () => demoLine(demoPathStr(demoCwd).replace(/^~/, "/home/" + DEMO_USER)),
  cd: (a) => {
    const target = demoResolve(a[0] || "~");
    const node = demoNodeAt(target);
    if (node === undefined) { demoLine("cd: no such file or directory: " + a[0]); return; }
    if (!demoIsDir(node)) { demoLine("cd: not a directory: " + a[0]); return; }
    demoCwd = target;
  },
  cat: (a) => demoCat(a, "cat"),
  head: (a) => demoCat(a.filter(x => !x.startsWith("-")), "head"),
  tail: (a) => demoCat(a.filter(x => !x.startsWith("-")), "tail"),
  echo: (a) => demoLine(a.join(" ")),
  tree: () => {
    demoLine(D.blu + D.b + "." + D.r);
    demoTree(demoCwd, "", 1);
  },
  mkdir: (a) => {
    if (!a.length) { demoLine("mkdir: missing operand"); return; }
    for (const name of a) {
      const parts = demoResolve(name);
      const parent = demoNodeAt(parts.slice(0, -1));
      if (!demoIsDir(parent)) { demoLine("mkdir: cannot create '" + name + "': No such file or directory"); continue; }
      parent[parts[parts.length - 1]] = {};
    }
    demoNoteLine("(created in this page's memory only — it disappears when you leave)");
  },
  touch: (a) => {
    if (!a.length) { demoLine("touch: missing file operand"); return; }
    for (const name of a) {
      const parts = demoResolve(name);
      const parent = demoNodeAt(parts.slice(0, -1));
      if (!demoIsDir(parent)) { demoLine("touch: cannot touch '" + name + "': No such file or directory"); continue; }
      const leaf = parts[parts.length - 1];
      if (!(leaf in parent)) parent[leaf] = "";
    }
    demoNoteLine("(in this page's memory only — nothing was written to a disk)");
  },
  clear: () => demoWrite("\x1b[2J\x1b[H"),
  whoami: () => demoLine(DEMO_USER),
  date: () => demoLine(new Date().toString()),
  uname: (a) => {
    if (a.includes("-a")) {
      demoLine("Linux workstation 6.8.0-generic #1 SMP x86_64 GNU/Linux");
      demoFabricatedLine();
    } else demoLine("Linux");
  },
  ps: () => {
    demoLine("  PID TTY          TIME CMD");
    demoLine("  412 pts/0    00:00:00 zsh");
    demoLine(" 1180 pts/0    00:00:04 node");
    demoLine(" 1184 pts/0    00:00:01 vite");
    demoLine(" 2031 pts/0    00:00:00 python");
    demoLine(" 2104 pts/0    00:00:00 ps");
    demoFabricatedLine();
  },
  top: () => {
    demoLine("top - 09:41:02 up 3 days,  2:17,  1 user,  load average: 0.31, 0.44, 0.38");
    demoLine("Tasks: 218 total,   1 running, 217 sleeping");
    demoLine("%Cpu(s):  4.1 us,  1.2 sy, 94.5 id");
    demoLine("MiB Mem :  32011 total,  18422 free,   9140 used,   4449 buff/cache");
    demoLine("");
    demoLine("  PID USER      %CPU  %MEM COMMAND");
    demoLine(" 1180 " + DEMO_USER + "       6.2   1.8 node");
    demoLine(" 1184 " + DEMO_USER + "       2.0   0.9 vite");
    demoLine(" 2031 " + DEMO_USER + "       0.7   1.1 python");
    demoNoteLine("(one-shot snapshot of numbers that were made up — the real top is interactive)");
  },
  htop: () => {
    demoWrap("htop isn't available in the demo terminal.", "", "");
    demoLine(demoNote("try " + D.r + D.grn + "top" + D.r + D.gry + D.it + " or " + D.r + D.grn + "ps" + D.r + D.gry + D.it + " instead"));
  },
  df: () => {
    demoLine("Filesystem      Size  Used Avail Use% Mounted on");
    demoLine("/dev/nvme0n1p2  916G  318G  552G  37% /");
    demoLine("/dev/nvme0n1p1  511M   62M  450M  13% /boot/efi");
    demoLine("tmpfs            16G  184M   16G   2% /dev/shm");
    demoFabricatedLine();
  },
  tmux: (a) => {
    if (a[0] !== "ls" && a[0] !== "list-sessions") {
      demoLine("the demo only answers " + D.grn + "tmux ls" + D.r);
      return;
    }
    demoLine("iphone-webapp: 2 windows (created Mon Mar 10 08:14:31 2025) (attached)");
    demoLine("laptop-api: 1 windows (created Tue Mar 11 17:02:09 2025)");
    demoLine("scratch: 1 windows (created Wed Mar 12 11:40:55 2025)");
    demoNoteLine("(these sessions are invented — the real list is the screen you came from)");
  },
  history: () => {
    demoHist.forEach((h, i) => demoLine(String(i + 1).padStart(5) + "  " + h));
  },
  man: (a) => {
    const name = a[0];
    if (!name) { demoLine("What manual page do you want?"); return; }
    if (!(name in DEMO_COMMANDS)) { demoLine("No manual entry for " + name); return; }
    demoLine(D.b + name.toUpperCase() + "(1)" + D.r);
    demoLine("");
    demoLine("  " + D.b + "NAME" + D.r);
    demoWrap(name + " — a stub page for the demo terminal", "", "        ");
    demoLine("");
    demoLine("  " + D.b + "DESCRIPTION" + D.r);
    demoWrap("This is a stand-in page, not the real manual. Run 'help' to see " +
             "what this shell understands.", "", "        ");
    demoLine("");
  },
  curl: (a) => {
    const url = a.find(x => !x.startsWith("-")) || "http://localhost:8080/health";
    demoLine('{"status":"ok","service":"api-service","uptime":"3d2h"}');
    demoLine(demoNote("(made-up response — " + url + " was never contacted)"));
  },
  ping: (a) => {
    const host = a.find(x => !x.startsWith("-")) || "localhost";
    demoLine("PING " + host + " 56(84) bytes of data.");
    demoLine("64 bytes from " + host + ": icmp_seq=1 ttl=64 time=0.041 ms");
    demoLine("64 bytes from " + host + ": icmp_seq=2 ttl=64 time=0.038 ms");
    demoLine("64 bytes from " + host + ": icmp_seq=3 ttl=64 time=0.044 ms");
    demoLine("");
    demoLine("--- " + host + " ping statistics ---");
    demoLine("3 packets transmitted, 3 received, 0% packet loss");
    demoNoteLine("(nothing was pinged; those timings are decorative)");
  },
  git: (a) => {
    const sub = a[0];
    if (sub === "status") {
      demoLine("On branch " + D.grn + "feature/router-cache" + D.r);
      demoLine("Your branch is up to date with 'origin/feature/router-cache'.");
      demoLine("");
      demoLine("Changes not staged for commit:");
      demoLine("  (use \"git add <file>...\" to update what will be committed)");
      demoLine("        " + D.red + "modified:   src/router.js" + D.r);
      demoLine("        " + D.red + "modified:   src/utils.js" + D.r);
      demoLine("");
      demoLine("Untracked files:");
      demoLine("        " + D.red + "docs/caching.md" + D.r);
      demoLine("");
      demoLine('no changes added to commit (use "git add" and/or "git commit -a")');
      demoNoteLine("(there is no repository here — this transcript is written into the demo)");
      return;
    }
    if (sub === "log") {
      const commits = [
        ["4f2a1c8", "cache resolved routes for repeat lookups", "2 hours ago"],
        ["9b7e330", "add tests for the router fallback", "yesterday"],
        ["c14d095", "drop the unused query parser", "2 days ago"],
        ["81ff6a2", "bump vite to 5.1", "4 days ago"],
      ];
      for (const [sha, msg, when] of commits) {
        demoLine(D.yel + "commit " + sha + D.r);
        demoLine("Author: " + DEMO_USER + " <" + DEMO_USER + "@example.com>");
        demoLine("Date:   " + when);
        demoLine("");
        demoLine("    " + msg);
        demoLine("");
      }
      demoNoteLine("(invented history, generic on purpose)");
      return;
    }
    if (sub === "branch") {
      demoLine("  develop");
      demoLine("* " + D.grn + "feature/router-cache" + D.r);
      demoLine("  main");
      return;
    }
    if (sub === "diff" || sub === "show") {
      demoLine("diff --git a/src/router.js b/src/router.js");
      demoLine("index 3c1a9f2..7d40b81 100644");
      demoLine("--- a/src/router.js");
      demoLine("+++ b/src/router.js");
      demoLine("@@ -1,5 +1,7 @@");
      demoLine(" const routes = new Map();");
      demoLine(D.grn + "+const cache = new Map();" + D.r);
      demoLine("");
      demoLine(" export function createRouter() {");
      demoFabricatedLine();
      return;
    }
    demoLine("the demo answers " + D.grn + "git status" + D.r + ", " + D.grn + "git log" + D.r +
             ", " + D.grn + "git branch" + D.r + " and " + D.grn + "git diff" + D.r);
  },
  npm: (a) => {
    if (a[0] === "test") {
      demoLine("");
      demoLine("> webapp@0.4.2 test");
      demoLine("> vitest run");
      demoLine("");
      demoLine(D.grn + " ✓ tests/router.test.js (1 test) 4ms" + D.r);
      demoLine(D.grn + " ✓ tests/utils.test.js (1 test) 2ms" + D.r);
      demoLine("");
      demoLine(" Test Files  " + D.grn + "2 passed" + D.r + " (2)");
      demoLine("      Tests  " + D.grn + "2 passed" + D.r + " (2)");
      demoNoteLine("(no test runner was started — this is a canned transcript)");
      return;
    }
    demoLine("the demo only answers " + D.grn + "npm test" + D.r);
  },
  python: () => {
    demoLine("Python 3.12.3");
    demoNoteLine("(version string only — interactive REPLs aren't available in the demo terminal)");
  },
  python3: () => DEMO_COMMANDS.python([]),
  node: () => {
    demoLine("v20.11.1");
    demoNoteLine("(version string only — interactive REPLs aren't available in the demo terminal)");
  },
  vim: () => demoWrap("Editors aren't available in the demo terminal. Try 'cat' to read a file.", "", ""),
  nano: () => DEMO_COMMANDS.vim([]),
  vi: () => DEMO_COMMANDS.vim([]),
  claude: () => demoAgentStart("claude"),
  codex: () => demoAgentStart("codex"),
  exit: () => demoExit(),
  logout: () => demoExit(),
};

// Cheap edit distance, capped: only used to decide whether a typo is close
// enough to a real command to be worth suggesting.
function demoNear(a, b) {
  if (Math.abs(a.length - b.length) > 2) return 99;
  let prev = Array.from({ length: b.length + 1 }, (_, i) => i);
  for (let i = 1; i <= a.length; i++) {
    const cur = [i];
    for (let j = 1; j <= b.length; j++) {
      cur[j] = Math.min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
    }
    prev = cur;
  }
  return prev[b.length];
}

function demoRun(line) {
  const parts = demoSplit(line);
  const name = parts[0];
  const fn = DEMO_COMMANDS[name];
  if (fn) {
    fn(parts.slice(1));
    // Commands that take over the shell print their own prompt.
    if (name !== "exit" && name !== "logout" && !demoAgent) demoNewPrompt();
    return;
  }
  demoLine("zsh: command not found: " + name);
  const near = Object.keys(DEMO_COMMANDS)
    .map(c => [c, demoNear(name, c)])
    .filter(([, d]) => d <= 2)
    .sort((a, b) => a[1] - b[1])[0];
  if (near) demoLine(demoNote("did you mean ") + D.grn + near[0] + D.r + D.gry + D.it + "?" + D.r);
  else demoLine(demoNote("run ") + D.grn + "help" + D.r + D.gry + D.it + " to see what this demo understands" + D.r);
  demoNewPrompt();
}

