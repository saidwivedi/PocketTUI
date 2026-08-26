// ---- fake filesystem ----------------------------------------------------
// Directories are plain objects, files are strings. Deliberately generic: this
// is a stand-in dev checkout, nothing more.
const DEMO_FS = {
  home: {
    "projects": {
      "webapp": {
        "README.md": "# webapp\n\nA small web application.\n\n## Getting started\n\n    npm install\n    npm run dev\n\nThe dev server listens on port 3000.\n\n## Layout\n\n- `src/` — application code\n- `tests/` — unit tests\n- `docs/` — notes and design sketches\n",
        "package.json": '{\n  "name": "webapp",\n  "version": "0.4.2",\n  "scripts": {\n    "dev": "vite",\n    "build": "vite build",\n    "test": "vitest run"\n  }\n}\n',
        ".env.example": "API_URL=http://localhost:8080\nLOG_LEVEL=info\n",
        "src": {
          "app.js": "import { createRouter } from './router.js';\nimport { mountUI } from './ui.js';\n\nconst router = createRouter();\n\nexport function start(root) {\n  mountUI(root, router);\n  router.resolve(location.pathname);\n}\n",
          "router.js": "const routes = new Map();\n\nexport function createRouter() {\n  return {\n    add(path, view) { routes.set(path, view); },\n    resolve(path) { return routes.get(path) || routes.get('*'); },\n  };\n}\n",
          "ui.js": "export function mountUI(root, router) {\n  root.innerHTML = '';\n  root.appendChild(document.createElement('main'));\n}\n",
          "utils.js": "export function debounce(fn, ms) {\n  let t = null;\n  return (...args) => {\n    clearTimeout(t);\n    t = setTimeout(() => fn(...args), ms);\n  };\n}\n",
        },
        "tests": {
          "router.test.js": "import { test, expect } from 'vitest';\nimport { createRouter } from '../src/router.js';\n\ntest('resolves a registered route', () => {\n  const r = createRouter();\n  r.add('/', 'home');\n  expect(r.resolve('/')).toBe('home');\n});\n",
          "utils.test.js": "import { test, expect } from 'vitest';\nimport { debounce } from '../src/utils.js';\n\ntest('debounce returns a function', () => {\n  expect(typeof debounce(() => {}, 10)).toBe('function');\n});\n",
        },
        "docs": {
          "notes.md": "# Notes\n\n- Router is intentionally tiny; no need for a framework yet.\n- Keep the bundle under 40kB.\n- Revisit caching once the API settles.\n",
        },
      },
      "api-service": {
        "README.md": "# api-service\n\nJSON API behind the webapp.\n\n    python -m venv .venv\n    .venv/bin/pip install -r requirements.txt\n    .venv/bin/python main.py\n",
        "main.py": "from server import create_app\n\napp = create_app()\n\nif __name__ == '__main__':\n    app.run(host='0.0.0.0', port=8080)\n",
        "server.py": "def create_app():\n    from flask import Flask\n    app = Flask(__name__)\n\n    @app.get('/health')\n    def health():\n        return {'status': 'ok'}\n\n    return app\n",
        "requirements.txt": "flask==3.0.0\nrequests==2.31.0\npytest==8.0.0\n",
      },
    },
    "notes.txt": "todo\n----\n- rotate the staging credentials\n- write up the caching decision\n- reply to the design review\n",
    "scratch.sh": "#!/bin/sh\n# quick helper, nothing important\necho \"hello from the demo\"\n",
  },
};

// Executables get a green ls; matching on extension is enough for a fake tree.
function demoIsExec(name) {
  return /\.(?:sh|py)$/.test(name);
}

let demoCwd = ["home", "projects", "webapp"];

// Absolute-ish display form of a path array.
function demoPathStr(parts) {
  if (parts[0] !== "home") return "/" + parts.join("/");
  return parts.length === 1 ? "~" : "~/" + parts.slice(1).join("/");
}

function demoNodeAt(parts) {
  let node = DEMO_FS;
  for (const p of parts) {
    if (!node || typeof node !== "object" || !(p in node)) return undefined;
    node = node[p];
  }
  return node;
}

// Resolve a user-typed path against the cwd. Returns the path array, or null if
// it escapes the tree.
function demoResolve(arg) {
  let parts;
  if (!arg || arg === "~") parts = ["home"];
  else if (arg.startsWith("~/")) parts = ["home"].concat(arg.slice(2).split("/"));
  else if (arg.startsWith("/")) parts = arg.split("/").filter(Boolean);
  else parts = demoCwd.concat(arg.split("/"));
  const out = [];
  for (const p of parts) {
    if (!p || p === ".") continue;
    if (p === "..") { if (out.length) out.pop(); continue; }
    out.push(p);
  }
  return out;
}

function demoIsDir(node) { return node && typeof node === "object"; }

