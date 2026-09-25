# PocketTUI agent contract

For an LLM assistant (or any script) running inside a tmux session on the computer where PocketTUI's backend (`app.py`) runs. Verified against `app.py`, `install.sh`, `src/mobile/js/08-links.js` and `src/mobile/js/28-file-explorer.js` at commit 9cabc3f.

Most of what an agent can do for the user needs no HTTP call at all: it is how the agent prints output (section 3). The HTTP routes in section 4 are for reading state or staging something for the user, and every one of them acts on the computer, not on the user's screen.

---

## 1. Finding the backend from inside a tmux session

| What | Where | Notes |
|---|---|---|
| Install dir | `~/pockettui` (or `$POCKETTUI_DIR` at install time) | `install.sh:37` |
| Port | default `5560`; the running value is in `~/pockettui/runtime.json` | `runtime.json` = `{version, pid, host, port, started_at}`, written at start (mode 0644) and removed on a clean exit; a `kill -9` leaves it behind, so check that `pid` is alive (`app.py:11169-11196`). The wrapper `~/.local/bin/pockettui` also carries `PORT="..."` (`install.sh:903`). |
| Bind address | `0.0.0.0` by default (`app.py:11241`); the service unit passes only `--port` (`install.sh:1381`) | From the same machine, use `http://127.0.0.1:<port>`. The `tailscale serve` path (`https://<host>.<tailnet>/pockettui`) is for remote devices only. |
| Token | `~/pockettui/.token`, 10 base32 chars + newline, mode 0600 (`app.py:76`, `app.py:257-267`, `install.sh:1265-1298`) | Readable by the agent: tmux is spawned by `app.py` (or already runs as the same user), so the pane runs under the same uid that owns the file. No env var carries it into sessions (`new-session` sets only `ZDOTDIR`, `app.py:3161`). |
| Header | `X-PocketTUI-Token: <token>` on every `/api/*` request (`app.py:154`, `507-539`) | No loopback exemption. A server started with `--no-auth` (loopback bind only) accepts requests without it. |

Rules for the token:
- Read it only when a route in section 4 is needed, and never print it to the pane, a log, or a commit. Every device attached to the session sees the pane, and the code is the only thing guarding a full shell.
- A wrong token answers `401 {error:"bad token", hint}` and counts toward the per-IP backoff (5 free tries, then doubling delays up to 300 s, `app.py:296-368`). Do not retry in a loop.
- Mutating session routes share a limit of 20 calls per 60 s per IP (`RATE_SESSION_MUTATE`, `app.py:410`).
- Routes that take `dev` resolve to that device's own grouped view. An agent has no device name, so leave `dev` out and the base session is used.

Minimal probe:
```sh
PORT=$(python3 -c 'import json;print(json.load(open("'"$HOME"'/pockettui/runtime.json"))["port"])' 2>/dev/null || echo 5560)
curl -s http://127.0.0.1:$PORT/.well-known/pockettui
curl -s -H "X-PocketTUI-Token: $(cat ~/pockettui/.token)" http://127.0.0.1:$PORT/api/version
```

## 2. Discovery

`GET /.well-known/pockettui`: no token. Returns `{"product":"pockettui","version":"<VERSION or "">"}` and nothing else (no hostname, port, paths or capabilities). The phone and the installer use it as a reachability probe (`app.py:1268-1283`).

`GET /api/version`: token. Returns `{"version": str, "host": socket.gethostname(), "capabilities": {flag: bool}}` (`app.py:1463-1490`). The map is built once at import (`server_capabilities()`, `app.py:1378-1462`), so a Chromium or ffmpeg installed later shows up only after a service restart. A flag that is absent or false means the feature is not there.

| flag | meaning | value |
|---|---|---|
| `fs` | `/api/fs/*` explorer and editor | always true |
| `image_paste` | `/api/image` | true |
| `upload` | `/api/upload` (composer "+") | true |
| `voice` | `/api/transcribe`, `/api/voice_status` (the live engine is voice_status's answer) | true |
| `learned` | `/api/learned` | true |
| `push` | Web Push available | false without `pywebpush` |
| `dbg` | `/api/dbg` | true |
| `git` | `/api/git/changes|diff|apply` | true |
| `git_ref` | `/api/git/branches` and `ref=` on `/api/fs/list|read` | true |
| `search` | `/api/search` | true |
| `ping` | attach socket answers `{"type":"ping"}` | true |
| `update` | `/api/update` | false with no `pockettui` wrapper or no tmux |
| `pair_qr` | `/api/pair_qr.svg` | true |
| `update_status` | `/api/update_status` | true |
| `type` | `/api/session/type` | true |
| `relay` | `/api/relay` | true |
| `browse` | `/b/<tok>/...` proxy | false without `httpx` |
| `browse_tab` | `POST /api/browse {mode:"tab"}` | true |
| `bookmarks` | `/api/browse/bookmarks` | true |
| `zip_dir` | a folder at `/api/fs/download` streams a zip | true |
| `thumbs` | `/api/fs/thumb` | false without ffmpeg |
| `browser_full` | `/ws/browser/<pane>`, `/api/browser/status` | false without a Chromium |
| `pdf_thumbs` | PDF page-one thumbnails | false without pdftoppm, gs or sips |
| `upload_dirs` | `?mkdirs=1` on `/api/fs/upload` makes a folder upload's subfolders | true |

## 3. Terminal-side conventions

Nothing in this section is an API call: it is how the agent's printed output behaves on the user's device. Printing something never opens it; the user taps it.

### 3.1 Local paths (`08-links.js:13`, `trimPath` `:225`)
```
LOCAL_PATH_RE = /(?:^|[\s"'`(\[{<=:,])((?:~\/|\/)[^\s"'`()\[\]{}<>:;,]+)/g
```
- The path must start with `/` or `~/`, at the start of a line or right after whitespace, a quote, a backtick, `(`, `[`, `{`, `<`, `=`, `:` or `,`. A bare `logo.png` or a relative `src/app.py` is not a link.
- The path ends at whitespace, a quote, a backtick, any bracket, `<`, `>`, `:`, `;` or `,`. So `app.py:42` links only `app.py`, and a path containing spaces is cut at the first space. Trailing `. ! ? ' " $ % #` are trimmed, and a bare `/` is dropped.
- Wrapped paths are rejoined across up to 8 rows, including continuation rows indented by up to 4 spaces (`logicalLine`, `stitchedText`).
- A `https?://` URL on the same row takes precedence over any path inside it.

What a tap opens (`activateLink` `08-links.js:240`, `openEntry` `28-file-explorer.js:1840-1866`):

| printed path | opens |
|---|---|
| `.png .jpg .jpeg .gif .webp .svg .bmp .mp4 .webm .mov` (`FILES_MEDIA_RE`) | full-screen image/video viewer |
| `.pdf` | PDF view in the docked pane (laptop) or a new browser tab (phone) |
| `.md .markdown` | rendered Markdown reader (tables, task lists, `$…$`/`$$…$$` KaTeX); Edit swaps to the editor |
| `.html .htm .xhtml` | rendered page in a new tab from a signed, sandboxed address; relative links to sibling files resolve |
| a directory | file explorer at that folder |
| any other file | CodeMirror editor (read-write; `.vimrc` honoured in Vim mode) |

A path that does not exist gets a "Couldn't find" toast. Media and PDF paths are not previewed at a git ref.

### 3.2 URLs, `http://localhost:PORT` and the relay (`08-links.js:18`, `:320-408`)
- ``URL_RE = /https?:\/\/[^\s"'`<>]+/gi``; trailing `. , ; : ! ? ' "` and unbalanced closing brackets are trimmed.
- **Laptop (wide layout) with the `browse` capability:** every http(s) URL opens in the in-app browser pane, fetched by the computer, so `http://localhost:PORT` works there directly with no relay.
- **Phone (or a laptop without `browse`):** the URL opens in a new browser tab. If the host is `localhost`, `127.0.0.1`, `[::1]`, `::1` or `0.0.0.0`, the phone first rewrites it to the computer's address as the phone knows it, then fires `POST /api/relay {host, port}` without waiting. The backend binds that port number on that address and forwards it to the loopback listener. It answers `relayed`, `direct` (something already listens there) or refuses (`not_listening` if nothing is on loopback at that port, `bad_host` for a loopback or wildcard target).
- What the agent should do: print `http://localhost:<port>` after the server is actually listening. It needs no API call. The relay binds on tap, not on print, and a server bound to `0.0.0.0` is reachable as printed.

### 3.3 Pane title and cwd
- **Session row label:** set the pane title with OSC 2 (or OSC 0): `printf '\033]2;%s\033\\' 'Running tests'`. The list strips leading non-alphanumeric glyphs and shows the rest. A title of the form `user@host: /path`, or equal to the hostname, is ignored and the folder name is shown instead (`06-session-list.js:250-263`, `app.py:711-716`).
- **Explorer "open at this pane's folder" and docked-pane follow:** for a local pane this is tmux `pane_current_path`, the cwd of the pane's foreground process (`pane_cwd`, `app.py:786-829`). A `cd` typed at the shell moves it; a `cd` inside an agent's own tool subprocess does not. To point the user at a folder, print its absolute path.
- **ssh panes only:** when the pane's foreground command is `ssh`, the title is read as `user@host: /abs/path` (everything after the first colon; `~` paths refused). A global tmux hook (`pane-title-changed`, `TITLE_HOOK`, `app.py:741-775`) copies every title that matches `*@*: /*` in an ssh pane into the pane option `@ptui_remote_cwd`, so a TUI that later overwrites the title does not erase it. The path is used only if it exists on this machine, directly or through `POCKETTUI_PATH_REWRITES`. For this to work, the remote shell must have drawn such a title in that folder before the TUI started.

### 3.4 How "waiting", "ready" and "idle" are derived (`app.py:10170-10780`)
The watcher polls every 2 s. A session is `active` while its newest output is less than 6 s old (`POCKETTUI_NOTIFY_IDLE_S`). At the busy-to-idle edge it reads the visible pane once, if the busy episode lasted 10 s or more or a non-shell program still holds the pane. `detect_prompt` then checks the last 5 non-empty lines above an empty composer, in this order:

1. `[y/n]`, `(y/n)` or `yes/no` → **waiting**, chips `y` `n`.
2. Two or more adjacent lines matching `^\s*│?\s*(❯\s*)?\d+[.)]\s` (numbered menu, box border tolerated) → **waiting**, chips are the first four digits.
3. Exactly one line `❯ <text>` with a non-empty line under it (unnumbered chooser) → **waiting**, no answer chips.
4. `do you want | would you like | proceed? | continue? | are you sure` → **waiting**, chips `y` `n`.
5. The cursor line is a composer (`>` or `❯`, optionally after `│`) that is empty, or holds only dim (SGR 2) placeholder text → **ready**. Non-dim text in it → drafting, shown as **idle** and never notified.
6. The cursor line ends in `?` → **waiting**, no answer chips.
7. Otherwise → **idle**.

Chips always add Enter (`\r`) and Esc. A chip tap sends only that key, with no Enter after `y` or a digit, and the chips come down as soon as the pane prints again.

Notifications (Web Push and/or ntfy) go out only for sessions whose `@notify` is `on` or `quiet`. At most one goes out per 30 s per session, and a repeat of the same text is suppressed:
- waiting: at once;
- ready: only after 30 s or more of work (`POCKETTUI_NOTIFY_READY_BUSY_S`);
- "<cmd> finished": a run of 10 s or more that returned to a shell;
- "went quiet — may need input": a program went silent after 10 s or more of work;
- **terminal bell**: `printf '\a'` notifies immediately, with no text guard, provided tmux sets the window's bell flag (tmux may not flag a bell in a window a client is currently viewing).

For an agent to be detected correctly:
- Ask with a literal `(y/n)` or a numbered list (`1. …`, `2. …`), then **stop printing**. Spinners, clocks or status lines that repaint keep the session `active` and defer detection.
- Keep the question within the last 5 non-empty lines above the input line.
- When done, return to an empty prompt/composer; a plain shell prompt after a long command reads as finished.
- Keystrokes the user sends from a phone reset the repeat guard; typing at the physical keyboard does not.
- There is **no route that marks a session waiting or sends a push**. `POST /api/notify` only stores the per-session preference.

### 3.5 Files that arrive from the user
- Pasted image: `~/.pockettui/images/paste-<stamp>-<hex>.<ext>`. The type is sniffed from the bytes and the newest 30 are kept (`app.py:8660-8735`). The absolute path is typed at the user's cursor.
- Attached file: `~/.pockettui/uploads/<name>-<stamp>-<hex>.<ext>`, newest 30 kept (`app.py:8766-8837`).
- Browser-pane upload for a streamed page: `~/.pockettui/uploads/browser/<hex>/` (`app.py:8126`). Streamed downloads: `~/.pockettui/downloads`.

## 4. Documented HTTP routes an agent may use

All need `X-PocketTUI-Token` except `/.well-known/pockettui`. None of them changes what is on the user's screen.

```
GET  /.well-known/pockettui   (no token)                    -> {product:"pockettui", version}
GET  /api/version             -                             -> {version, host, capabilities:{<flag>: bool}}
GET  /api/sessions            -                             -> {sessions:[{name, created, attached, windows, command, title, cwd, alias, notify, state: active|waiting|ready|idle, last_activity}]}
POST /api/session             {name, dir?}                  -> {session} | 400 {error} (name required, <=60 chars, no "." or ":", unique; dir defaults to ~)
POST /api/alias               {session, alias}              -> {session, alias}   (display name only; "" clears)
POST /api/session/rename      {session, name}               -> {session}           (real tmux rename; drops every device view)
POST /api/session/type        {name, dev?, text}            -> {session, chars} | 400 empty | 404 | 413 too_long (>4096)  (typed with send-keys -l, \r and \n stripped, never submitted)
POST /api/notify              {session, mode: off|on|quiet} -> {session, notify}  (preference only; sends nothing)
POST /api/session/kill        {session}                     -> {killed}           (whole group; only on explicit user request)
GET  /api/update_status       -                             -> {state: <install.sh update-state.json> | null, session_alive: bool}
```
Prefer the CLI where one exists: `pockettui update | version | status | browser install|check|status`. Prefer plain tmux for reading your own pane.

## 5. Every route in app.py, classified

| # | method | route | class | reason |
|---|---|---|---|---|
| 1 | GET | `/` | infrastructure | serves the self-hosted app shell |
| 2 | GET | `/.well-known/pockettui` | documented integration | unauthenticated reachability and version probe |
| 3 | GET | `/manifest.json` | infrastructure | PWA manifest for home screen |
| 4 | GET | `/sw.js` | infrastructure | service worker, cache-busted per start |
| 5 | GET | `/icon.svg` | infrastructure | static app icon file served |
| 6 | GET | `/icon-{size}.png` | infrastructure | static PNG icons for PWA |
| 7 | GET | `/vendor/{name}` | infrastructure | bundled third-party JS libraries |
| 8 | GET | `/api/version` | documented integration | version, hostname and capability map |
| 9 | GET | `/api/pair_qr.svg` | internal transport | QR embeds token; device-only |
| 10 | GET | `/api/sessions` | documented integration | session list with watcher states |
| 11 | GET | `/api/session_cwd` | internal transport | explorer asks device view cwd |
| 12 | POST | `/api/alias` | documented integration | set shared display name safely |
| 13 | POST | `/api/session/kill` | documented integration | kill group; explicit request only |
| 14 | POST | `/api/session/rename` | documented integration | real rename; drops device views |
| 15 | GET | `/api/voice_status` | internal transport | dictation settings read engines |
| 16 | POST | `/api/transcribe` | internal transport | phone audio to local STT |
| 17 | POST | `/api/learn` | internal transport | composer records dictation edits |
| 18 | GET | `/api/learned` | internal transport | Settings lists learned corrections |
| 19 | POST | `/api/learned_delete` | internal transport | Settings deletes learned corrections |
| 20 | POST | `/api/session` | documented integration | create detached session by name |
| 21 | POST | `/api/session/type` | documented integration | stage command, never submits |
| 22 | POST | `/api/update` | internal transport | app button; agents use CLI |
| 23 | GET | `/api/update_status` | documented integration | read last update verdict safely |
| 24 | GET | `/api/file` | infrastructure | header-authed media bytes by path |
| 25 | GET | `/api/fs/list` | internal transport | explorer listing; agent has shell |
| 26 | GET | `/api/fs/read` | internal transport | editor load; agent has filesystem |
| 27 | POST | `/api/fs/write` | internal transport | editor save with hash guard |
| 28 | POST | `/api/fs/mkdir` | internal transport | explorer new-folder action only |
| 29 | POST | `/api/fs/rename` | internal transport | explorer rename and drag-move |
| 30 | POST | `/api/fs/delete` | internal transport | explorer delete, non-recursive dirs |
| 31 | POST | `/api/fs/upload` | internal transport | device file into folder |
| 32 | GET | `/api/fs/download` | internal transport | file or zip to device |
| 33 | GET | `/api/fs/download_link` | internal transport | mints signed download URL |
| 34 | GET | `/api/fs/signed_download` | infrastructure | signed link, no token header |
| 35 | GET | `/api/file_link` | internal transport | mints signed media viewer URL |
| 36 | GET | `/api/signed_file` | infrastructure | signed media link for tags |
| 37 | GET | `/api/fs/thumb` | internal transport | grid thumbnails via ffmpeg/poppler |
| 38 | GET | `/api/fs/render_link` | internal transport | mints sandboxed HTML page URL |
| 39 | GET | `/api/fs/site/{exp}/{sig}/{root}/{rest}` | infrastructure | signed sandboxed rendered pages |
| 40 | GET | `/api/git/changes` | internal transport | diff pane's changed-file list |
| 41 | GET | `/api/git/branches` | internal transport | explorer branch picker entries |
| 42 | GET | `/api/git/diff` | internal transport | one file's diff for pane |
| 43 | POST | `/api/git/apply` | internal transport | stage/revert blocks from pane |
| 44 | POST | `/api/search` | internal transport | find bar drives copy-mode |
| 45 | POST | `/api/relay` | internal transport | fired by tap; print URL |
| 46 | POST | `/api/browse` | internal transport | mints browser pane proxy token |
| 47 | GET | `/api/browse/bookmarks` | internal transport | pane bookmarks bar contents |
| 48 | PUT | `/api/browse/bookmarks` | internal transport | replaces whole bookmark list |
| 49 | GET | `/api/browser/status` | internal transport | streamed Chrome state for Settings |
| 50 | POST | `/api/browser/reset` | internal transport | Settings clears Chrome profile |
| 51 | POST | `/api/browser/upload` | internal transport | streamed page file-input upload |
| 52 | GET/POST/… | `/b/{tok}/{sch}/{hostport}/{rest}` | infrastructure | reverse proxy for pane pages |
| 53 | GET/POST/… | `/b/{tok}/{sch}/{hostport}` | infrastructure | reverse proxy, bare host form |
| 54 | GET | `/b/{tok}/enter` | infrastructure | tab entry clearing site data |
| 55 | WS | `/b/{tok}/{sch}/{hostport}/{rest}` | infrastructure | proxied page WebSockets, pathed |
| 56 | WS | `/b/{tok}/{sch}/{hostport}` | infrastructure | proxied page WebSockets, bare |
| 57 | POST | `/api/image` | internal transport | clipboard image staging for paste |
| 58 | POST | `/api/upload` | internal transport | composer attachment staging for paste |
| 59 | POST | `/api/dbg` | internal transport | device debug lines to log |
| 60 | WS | `/ws/browser/{pane}` | internal transport | streamed Chrome frames and input |
| 61 | WS | `/ws/attach/{session_name}` | internal transport | the terminal PTY bridge itself |
| 62 | GET | `/api/push/status` | internal transport | push setup question from app |
| 63 | POST | `/api/push/subscribe` | internal transport | stores browser push subscription |
| 64 | POST | `/api/push/unsubscribe` | internal transport | drops browser push subscription |
| 65 | POST | `/api/notify` | documented integration | sets preference, sends nothing |

Middlewares (not routes): `require_token` (`app.py:507`, gates `/api/*` except signed paths; also 403s `/api/*` calls whose Referer is a proxied `/b/` page), CORS `*` with private-network access (`app.py:545-573`).

### Attach socket, for reference only
`WS /ws/attach/{session_name}?fresh=0|1` (`app.py:9811`). The first text frame must arrive within 5 s: `{"token","dev","type":"resize","cols","rows"}`. A bad token closes with 4401 and a missing session with 4404. Server → client: `{"type":"replay","data"}`, `{"type":"adopted"}`, `{"type":"pong"}`, `{"type":"prompt","options":[...],"line":...}`. Client → server: `{"type":"resize"|"ping"|"visibility",...}`; any other `{"type":...}` frame is dropped, never typed. Every other frame is raw keystrokes written to the PTY. An agent inside the session has no reason to open this socket: it would create another grouped device view.
