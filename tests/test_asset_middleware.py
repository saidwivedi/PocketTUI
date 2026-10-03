"""functions/_middleware.js, the download count in front of three static files.

Run under node with fake ASSETS, D1 and next(): each case is one request, and
the runner prints the response the middleware returned, the Request the asset
store was asked for, every statement D1 saw, and whether next() ran.
"""

import json
import re
import shutil
import subprocess

import pytest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MIDDLEWARE = REPO / "functions" / "_middleware.js"

RUNNER = """
import { readFileSync } from "node:fs";
const mod = await import(process.argv[2]);
const cases = JSON.parse(readFileSync(process.argv[3], "utf8"));
const out = [];
for (const c of cases) {
  const seen = { assets: [], db: [], next: 0 };
  const asset = c.asset || {};
  const env = {
    ASSETS: {
      async fetch(req) {
        seen.assets.push({ url: req.url, method: req.method,
                           range: req.headers.get("Range") });
        const body = req.method === "HEAD" ? null : (asset.body || "the file");
        return new Response(body, { status: asset.status || 200,
                                    headers: asset.headers || {} });
      },
    },
  };
  if (c.db !== "none") {
    env.DB = {
      prepare(sql) {
        const call = { sql, binds: null };
        seen.db.push(call);
        return { bind(...b) {
          call.binds = b;
          return { run: async () => { if (c.db === "throw") throw new Error("D1 down"); } };
        } };
      },
    };
  }
  if (c.salt !== false) env.USAGE_SALT = "0123456789abcdef0123456789abcdef";
  const pending = [];
  const request = new Request("https://pockettui.example.net" + c.path,
                              { method: c.method || "GET", headers: c.headers || {} });
  if (c.cf) request.cf = c.cf;
  const context = {
    request, env,
    waitUntil(p) { pending.push(p); },
    async next() { seen.next += 1; return new Response("from next", { status: 201 }); },
  };
  let res, error = null;
  try {
    const r = await mod.onRequest(context);
    res = { status: r.status, body: await r.text(),
            cacheControl: r.headers.get("Cache-Control") };
  } catch (e) {
    error = String(e && e.message);
  }
  const settled = await Promise.allSettled(pending);
  out.push({ res, error, seen, rejected: settled.filter((s) => s.status === "rejected").length });
}
console.log(JSON.stringify(out));
"""

HEX32 = re.compile(r"[0-9a-f]{32}")
CURL = {"User-Agent": "curl/8.4.0"}
BROWSER = {"User-Agent": "Mozilla/5.0 (Macintosh)"}
INSERT = ("INSERT INTO fetches (ts, day, kind, mode, agent, ref_host, country) "
          "VALUES (?, ?, ?, ?, ?, ?, ?)")


def run(tmp_path, cases):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH")
    runner = tmp_path / "runner.mjs"
    runner.write_text(RUNNER, encoding="utf-8")
    data = tmp_path / "cases.json"
    data.write_text(json.dumps(cases), encoding="utf-8")
    p = subprocess.run([node, str(runner), MIDDLEWARE.as_uri(), str(data)],
                       check=True, capture_output=True, text=True)
    out = json.loads(p.stdout)
    for r in out:
        assert r["error"] is None, r
        assert r["rejected"] == 0, r
    return out


def one(tmp_path, **case):
    (r,) = run(tmp_path, [case])
    return r


def row(r):
    """The binds of the single INSERT, minus ts and day, as a dict."""
    (call,) = r["seen"]["db"]
    assert call["sql"] == INSERT
    ts, day, kind, mode, agent, ref_host, country = call["binds"]
    assert isinstance(ts, int) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", day)
    return dict(kind=kind, mode=mode, agent=agent, ref_host=ref_host, country=country)


def test_tarball_update_is_counted_and_served_without_the_query(tmp_path):
    r = one(tmp_path, path="/pockettui.tar.gz?m=update", headers=CURL, cf={"country": "DE"})
    assert r["res"]["status"] == 200 and r["res"]["body"] == "the file"
    (a,) = r["seen"]["assets"]
    assert a["url"] == "https://pockettui.example.net/pockettui.tar.gz"
    assert row(r) == dict(kind="tarball", mode="update", agent="curl",
                          ref_host=None, country="DE")
    assert r["seen"]["next"] == 0


def test_install_sh_from_a_browser(tmp_path):
    r = one(tmp_path, path="/install.sh", headers=dict(BROWSER, **{"CF-IPCountry": "FRA"}))
    assert r["res"]["status"] == 200
    assert row(r) == dict(kind="install_sh", mode=None, agent="browser",
                          ref_host=None, country="FR")


def test_wrapper_refetch_of_install_sh_counts_as_update(tmp_path):
    r = one(tmp_path, path="/install.sh?m=update", headers=CURL)
    assert row(r)["mode"] == "update"
    assert r["seen"]["assets"][0]["url"] == "https://pockettui.example.net/install.sh"


def test_version_referer_is_stored_only_as_a_pseudonym(tmp_path):
    r = one(tmp_path, path="/version.txt",
            headers=dict(BROWSER, Referer="https://box.example.net:5560/"))
    got = row(r)
    assert HEX32.fullmatch(got["ref_host"])
    (call,) = r["seen"]["db"]
    assert not any("box.example.net" in str(b) for b in call["binds"])


def test_version_referer_from_the_hosted_app(tmp_path):
    r = one(tmp_path, path="/version.txt", headers=dict(BROWSER, Referer="https://pockettui.com/app/"))
    assert row(r)["ref_host"] == "hosted"


@pytest.mark.parametrize("headers", [CURL, dict(CURL, Referer="not a url")])
def test_version_without_a_usable_referer(tmp_path, headers):
    r = one(tmp_path, path="/version.txt", headers=headers)
    got = row(r)
    assert got["ref_host"] is None and got["kind"] == "version"


def test_ref_host_is_only_for_version(tmp_path):
    r = one(tmp_path, path="/install.sh", headers=dict(CURL, Referer="https://box.example.net/"))
    assert row(r)["ref_host"] is None


def test_unknown_mode_is_dropped(tmp_path):
    r = one(tmp_path, path="/pockettui.tar.gz?m=evil", headers=CURL)
    assert row(r)["mode"] is None


def test_other_paths_go_to_next_untouched(tmp_path):
    r = one(tmp_path, path="/api/report", method="POST", headers=CURL)
    assert r["seen"]["next"] == 1
    assert r["seen"]["db"] == [] and r["seen"]["assets"] == []
    assert r["res"]["status"] == 201 and r["res"]["body"] == "from next"


def test_near_miss_paths_are_not_counted(tmp_path):
    for path in ("/install.sh/", "/app/version.txt", "/pockettui.tar.gz.sig"):
        r = one(tmp_path, path=path, headers=CURL)
        assert r["seen"]["next"] == 1 and r["seen"]["db"] == [], path


def test_a_failing_insert_still_serves_the_file(tmp_path):
    r = one(tmp_path, path="/pockettui.tar.gz?m=install", headers=CURL, db="throw")
    assert r["res"]["status"] == 200 and r["res"]["body"] == "the file"
    assert len(r["seen"]["db"]) == 1


def test_no_database_binding_still_serves_the_file(tmp_path):
    r = one(tmp_path, path="/pockettui.tar.gz", headers=CURL, db="none")
    assert r["res"]["status"] == 200 and r["res"]["body"] == "the file"


def test_no_salt_serves_the_file_and_writes_nothing(tmp_path):
    r = one(tmp_path, path="/version.txt", headers=dict(BROWSER, Referer="https://box.example.net/"),
            salt=False)
    assert r["res"]["status"] == 200 and r["res"]["body"] == "the file"
    assert r["seen"]["db"] == []


def test_a_missing_asset_is_passed_through_and_not_counted(tmp_path):
    r = one(tmp_path, path="/pockettui.tar.gz?m=install", headers=CURL,
            asset={"status": 404, "body": "nope"})
    assert r["res"]["status"] == 404 and r["res"]["body"] == "nope"
    assert r["seen"]["db"] == []


def test_head_and_range_reach_the_asset_store(tmp_path):
    r = one(tmp_path, path="/pockettui.tar.gz", method="HEAD", headers=CURL)
    assert r["seen"]["assets"][0]["method"] == "HEAD"
    assert r["res"]["status"] == 200
    r = one(tmp_path, path="/pockettui.tar.gz?m=update", headers=dict(CURL, Range="bytes=0-9"),
            asset={"status": 206, "body": "0123456789"})
    assert r["seen"]["assets"][0]["range"] == "bytes=0-9"
    assert r["res"]["status"] == 206
    assert row(r)["mode"] == "update"


def test_version_gets_no_store_only_without_its_own_cache_control(tmp_path):
    r = one(tmp_path, path="/version.txt", headers=CURL)
    assert r["res"]["cacheControl"] == "no-store"
    assert r["res"]["body"] == "the file"
    r = one(tmp_path, path="/version.txt", headers=CURL,
            asset={"headers": {"Cache-Control": "max-age=60"}})
    assert r["res"]["cacheControl"] == "max-age=60"
    r = one(tmp_path, path="/pockettui.tar.gz", headers=CURL)
    assert r["res"]["cacheControl"] is None
