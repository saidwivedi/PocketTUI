"""functions/api/console.js, the key-protected aggregate API behind the stats page.

Run under node. D1 is either a fake that answers each statement with rows
picked by a substring of its (whitespace-collapsed) SQL, or a real SQLite
database (node:sqlite) built from migrations/0001_usage.sql and seeded per
case, so the SQL itself is checked against the schema. The Web Analytics call
goes to a stubbed global fetch. Nothing leaves the machine.
"""

import json
import re
import shutil
import subprocess

import pytest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FUNCTION = REPO / "functions" / "api" / "console.js"
MIGRATION = REPO / "migrations" / "0001_usage.sql"

RUNNER = r"""
import { readFileSync } from "node:fs";
const mod = await import(process.argv[2]);
const cases = JSON.parse(readFileSync(process.argv[3], "utf8"));
const migration = readFileSync(process.argv[4], "utf8");
const realNow = Date.now;
const out = [];
for (const c of cases) {
  const stmts = [];
  const fetches = [];
  let batches = 0;
  let db;
  if (c.sqlite) {
    const { DatabaseSync } = await import("node:sqlite");
    const s = new DatabaseSync(":memory:");
    s.exec(migration);
    for (const sql of c.sqlite) s.exec(sql);
    db = {
      prepare(sql) { return { bind(...binds) { stmts.push({ sql, binds }); return { sql, binds }; } }; },
      async batch(list) {
        batches++;
        return list.map((x) => ({ results: s.prepare(x.sql).all(...x.binds).map((r) => ({ ...r })) }));
      },
    };
  } else {
    db = {
      prepare(sql) { return { bind(...binds) { const st = { sql: sql.replace(/\s+/g, " "), binds }; stmts.push(st); return st; } }; },
      async batch(list) {
        batches++;
        if (c.dbFail) throw new Error("D1 down");
        return list.map((x) => {
          const hit = (c.rows || []).find(([sub]) => x.sql.includes(sub));
          return { results: hit ? hit[1] : [] };
        });
      },
    };
  }
  globalThis.fetch = async (url, init) => {
    fetches.push({ url, auth: init.headers.Authorization, body: JSON.parse(init.body) });
    const f = c.fetch || { status: 200, body: { data: { viewer: { accounts: [{ days: [], country: [], device: [], referrer: [] }] } } } };
    if (f === "reject") throw new TypeError("network down");
    if (f === "abort") { const e = new Error("aborted"); e.name = "AbortError"; throw e; }
    return new Response(JSON.stringify(f.body), { status: f.status });
  };
  Date.now = c.now ? () => c.now : realNow;
  const env = Object.assign({ STATS_KEY: "s3cret-key", CF_ANALYTICS_TOKEN: "cf-token", DB: db }, c.env || {});
  for (const k of c.unset || []) delete env[k];
  const headers = new Headers(c.headers || {});
  const request = new Request("https://pockettui.com/api/console" + (c.query || ""), { method: c.method || "GET", headers });
  const fn = c.method === "OPTIONS" ? mod.onRequestOptions : mod.onRequestGet;
  const r = await fn({ request, env });
  const text = await r.text();
  out.push({ status: r.status, headers: Object.fromEntries(r.headers), text, body: text ? JSON.parse(text) : null, stmts, fetches, batches });
}
console.log(JSON.stringify(out));
"""

KEY = {"Authorization": "Bearer s3cret-key"}
# 2026-10-03 12:00 UTC, a Saturday.
NOW = 1791028800000
HEX32 = re.compile(r"[0-9a-f]{32}")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
TOP = ["range", "landing", "installs", "app", "retention", "errors"]


def run_cases(tmp_path, cases):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH")
    runner = tmp_path / "runner.mjs"
    runner.write_text(RUNNER, encoding="utf-8")
    data = tmp_path / "cases.json"
    data.write_text(json.dumps(cases), encoding="utf-8")
    p = subprocess.run([node, "--no-warnings", str(runner), FUNCTION.as_uri(), str(data), str(MIGRATION)],
                       check=True, capture_output=True, text=True)
    return json.loads(p.stdout)


def one(tmp_path, **case):
    case.setdefault("now", NOW)
    (res,) = run_cases(tmp_path, [case])
    return res


def test_auth(tmp_path):
    none, wrong, short, right = run_cases(tmp_path, [
        dict(now=NOW),
        dict(now=NOW, headers={"Authorization": "Bearer s3cret-kez"}),
        dict(now=NOW, headers={"Authorization": "Bearer s"}),
        dict(now=NOW, headers=KEY),
    ])
    for r in (none, wrong, short):
        assert r["status"] == 401
        assert r["text"] == none["text"] == '{"error":"unauthorized"}'
        assert r["stmts"] == [] and r["fetches"] == []
    assert right["status"] == 200
    assert list(right["body"]) == TOP
    assert right["headers"]["content-type"] == "application/json"
    assert right["headers"]["cache-control"] == "private, max-age=60"
    # The token goes to Cloudflare and nowhere into the answer.
    assert right["fetches"][0]["auth"] == "Bearer cf-token"
    assert "cf-token" not in right["text"] and "s3cret" not in right["text"]


def test_not_configured(tmp_path):
    r = one(tmp_path, headers=KEY, unset=["STATS_KEY"])
    assert r["status"] == 503 and r["body"] == {"error": "not configured"}


def test_cors(tmp_path):
    cases = [
        dict(now=NOW, headers=dict(KEY, Origin="https://apps.saidwivedi.in")),
        dict(now=NOW, headers=dict(KEY, Origin="https://evil.example.net")),
        dict(now=NOW, headers=dict(KEY, Origin="http://localhost:8788")),
        dict(now=NOW, headers=dict(KEY, Origin="http://127.0.0.1:5500")),
        dict(now=NOW, headers=dict(KEY, Origin="http://localhost.example.net")),
        dict(now=NOW, headers=KEY),
        dict(now=NOW, headers={"Origin": "https://pockettui.com"}),
        dict(now=NOW, method="OPTIONS", headers={"Origin": "https://saidwivedi.in"}),
        dict(now=NOW, method="OPTIONS", headers={"Origin": "https://evil.example.net"}),
    ]
    ok, evil, local, loop, lookalike, same, unauth, opt_ok, opt_evil = run_cases(tmp_path, cases)
    for r, origin in ((ok, "https://apps.saidwivedi.in"), (local, "http://localhost:8788"),
                      (loop, "http://127.0.0.1:5500"), (unauth, "https://pockettui.com"),
                      (opt_ok, "https://saidwivedi.in")):
        h = r["headers"]
        assert h["access-control-allow-origin"] == origin
        assert h["vary"] == "Origin"
        assert h["access-control-allow-headers"] == "Authorization"
        assert h["access-control-allow-methods"] == "GET, OPTIONS"
        assert h["access-control-max-age"] == "86400"
    for r in (evil, lookalike, same, opt_evil):
        assert not any(k.startswith("access-control-") for k in r["headers"]), r["headers"]
        assert "vary" not in r["headers"]
    assert ok["status"] == evil["status"] == same["status"] == 200
    assert unauth["status"] == 401
    assert opt_ok["status"] == opt_evil["status"] == 204
    assert opt_ok["text"] == "" and opt_ok["stmts"] == []


def test_days_clamp(tmp_path):
    big, junk, week, default = run_cases(tmp_path, [
        dict(now=NOW, headers=KEY, query="?days=1000"),
        dict(now=NOW, headers=KEY, query="?days=abc"),
        dict(now=NOW, headers=KEY, query="?days=7"),
        dict(now=NOW, headers=KEY),
    ])
    assert big["body"]["range"]["days"] == 90
    assert junk["body"]["range"]["days"] == 28 == default["body"]["range"]["days"]
    assert week["body"]["range"] == {"from": "2026-09-27", "to": "2026-10-03", "days": 7}
    assert len(week["body"]["app"]["days"]) == 7
    assert [d["day"] for d in week["body"]["installs"]["days"]] == [
        "2026-09-27", "2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02", "2026-10-03"]
    # Every day-bounded statement uses the same inclusive window.
    binds = [s["binds"] for s in week["stmts"] if "day BETWEEN" in s["sql"]]
    assert binds and all("2026-09-27" in b or "2026-10-03" in b for b in binds)
    f = week["fetches"][0]["body"]["variables"]["f"]
    assert f["datetime_geq"] == "2026-09-27T00:00:00Z"
    assert f["datetime_leq"] == "2026-10-03T12:00:00Z"
    assert f["requestHost"] == "pockettui.com" and f["requestPath"] == "/"
    assert f["siteTag"] == "5a3a629a4c5f4a1fa4aa7a64f3df91a4"


FAKE_ROWS = [
    ["SELECT day, SUM(kind = 'install_sh')", [
        {"day": "2026-09-27", "attempts": 3, "installs": 2, "updates": 0, "version_checks": 5, "active_self": 2},
        {"day": "2026-09-29", "attempts": 1, "installs": 0, "updates": 1, "version_checks": 4, "active_self": 3},
    ]],
    ["SELECT SUM(kind = 'install_sh')", [
        {"attempts": 4, "installs": 2, "updates": 1, "version_checks": 9, "active_self": 3}]],
    ["COUNT(DISTINCT person)", [
        {"day": "2026-09-27", "people": 3, "sessions": 4, "consented_sessions": 2},
        {"day": "2026-10-01", "people": 2, "sessions": 1, "consented_sessions": 1},
    ]],
    ["SELECT e.day", [{"day": "2026-09-27", "new_installs": 1, "returning_installs": 1}]],
    ["installs_seen", [{"installs_seen": 2, "new_installs": 1, "returning_installs": 1}]],
    ["f_explorer > 0", [{"sessions": 5, "consented_sessions": 3, "explorer": 2, "browser": 0, "diff": 1,
                         "side2": 0, "voice": 4, "settings": 0, "reader": 0, "editor": 0, "viewer": 0,
                         "search": 0, "newsess": 1}]],
    ["SELECT secs", [{"secs": s} for s in [1000, 10, 40, 20, 30]]],
    ["SELECT os AS k", [{"k": "mac", "n": 3}, {"k": "ios", "n": 2}]],
    ["AS week", [{"week": "2026-09-21", "new": 2, "back1": 1, "back2": 0, "back3": 0, "back4": 0}]],
    ["AS month", [{"month": "2026-09", "new": 2, "back1": 1, "back2": 0}]],
]


def test_shapes_from_fake_rows(tmp_path):
    r = one(tmp_path, headers=KEY, query="?days=7", rows=FAKE_ROWS)
    assert r["status"] == 200
    b = r["body"]
    assert b["installs"]["totals"] == {"attempts": 4, "installs": 2, "updates": 1,
                                        "version_checks": 9, "active_self": 3}
    # Days with no rows are filled with zeros.
    assert b["installs"]["days"][1] == {"day": "2026-09-28", "attempts": 0, "installs": 0, "updates": 0,
                                        "version_checks": 0, "active_self": 0}
    app = b["app"]
    assert app["days"][0] == {"day": "2026-09-27", "people": 3, "sessions": 4, "consented_sessions": 2,
                              "new_installs": 1, "returning_installs": 1}
    assert app["totals"]["people_sum"] == sum(d["people"] for d in app["days"]) == 5
    assert app["totals"]["sessions"] == 5 and app["totals"]["installs_seen"] == 2
    # Nearest rank: median of five is the third value, p90 the fifth.
    assert app["len"]["median"] == 30 and app["len"]["p90"] == 1000
    assert app["len"]["hist"] == [{"le": 30, "n": 3}, {"le": 60, "n": 1}, {"le": 120, "n": 0},
                                  {"le": 300, "n": 0}, {"le": 900, "n": 0}, {"le": 3600, "n": 1},
                                  {"le": None, "n": 0}]
    assert app["feats"]["voice"] == {"sessions": 4, "share": 0.8}
    assert app["feats"]["browser"] == {"sessions": 0, "share": 0}
    assert sorted(app["feats"]) == sorted(["explorer", "browser", "diff", "side2", "voice", "settings",
                                           "reader", "editor", "viewer", "search", "newsess"])
    assert app["consent_share"] == 0.6
    assert app["os"] == [{"k": "mac", "n": 3}, {"k": "ios", "n": 2}]
    for k in ("versions", "srv", "layout", "shell", "country"):
        assert app[k] == []
    assert b["retention"]["weekly"] == FAKE_ROWS[8][1]
    assert b["retention"]["monthly"] == FAKE_ROWS[9][1]
    # Fetches by day and total, events by day, installs by day and total,
    # features, lengths, six group-bys, two retention tables.
    assert len(r["stmts"]) == 15 and r["batches"] == 1


def test_empty_window_has_no_division_by_zero(tmp_path):
    b = one(tmp_path, headers=KEY)["body"]
    assert b["app"]["consent_share"] == 0
    assert b["app"]["feats"]["explorer"] == {"sessions": 0, "share": 0}
    assert b["app"]["len"]["median"] is None and b["app"]["len"]["p90"] is None


def test_d1_failure_is_500(tmp_path):
    r = one(tmp_path, headers=KEY, dbFail=True)
    assert r["status"] == 500 and r["body"] == {"error": "query failed"}


RUM_OK = {"status": 200, "body": {"data": {"viewer": {"accounts": [{
    "days": [{"count": 12, "sum": {"visits": 7}, "dimensions": {"date": "2026-10-02"}},
             {"count": 3, "sum": {"visits": 2}, "dimensions": {"date": "2026-09-28"}}],
    "country": [{"count": 9, "sum": {"visits": 5}, "dimensions": {"countryName": "DE"}},
                {"count": 6, "sum": {"visits": 4}, "dimensions": {"countryName": "US"}}],
    "device": [{"count": 15, "sum": {"visits": 9}, "dimensions": {"deviceType": "desktop"}}],
    "referrer": [{"count": 4, "sum": {"visits": 3}, "dimensions": {"refererHost": ""}},
                 {"count": 8, "sum": {"visits": 6}, "dimensions": {"refererHost": "news.example.net"}}],
}]}}}}


def test_landing_failures_keep_200(tmp_path):
    cases = [
        dict(now=NOW, headers=KEY, fetch="reject"),
        dict(now=NOW, headers=KEY, fetch="abort"),
        dict(now=NOW, headers=KEY, fetch={"status": 500, "body": {}}),
        dict(now=NOW, headers=KEY, fetch={"status": 200, "body": {"data": None, "errors": [{"message": "bad filter"}]}}),
        dict(now=NOW, headers=KEY, unset=["CF_ANALYTICS_TOKEN"]),
    ]
    results = run_cases(tmp_path, cases)
    for r in results:
        assert r["status"] == 200
        assert r["body"]["landing"] is None
        assert r["body"]["errors"] and all(e.startswith("landing:") for e in r["body"]["errors"])
        assert r["body"]["installs"]["totals"]["attempts"] == 0
    assert "timed out" in results[1]["body"]["errors"][0]
    assert "500" in results[2]["body"]["errors"][0]
    assert "bad filter" in results[3]["body"]["errors"][0]
    assert results[4]["fetches"] == []


def test_landing_mapped(tmp_path):
    b = one(tmp_path, headers=KEY, query="?days=7", fetch=RUM_OK)["body"]
    assert b["errors"] == []
    land = b["landing"]
    days = {d["day"]: d for d in land["days"]}
    assert len(land["days"]) == 7
    assert days["2026-10-02"] == {"day": "2026-10-02", "views": 12, "visits": 7}
    assert days["2026-09-28"] == {"day": "2026-09-28", "views": 3, "visits": 2}
    assert days["2026-09-30"] == {"day": "2026-09-30", "views": 0, "visits": 0}
    assert land["referrer"] == [{"k": "news.example.net", "visits": 6}, {"k": "direct", "visits": 3}]
    assert land["country"] == [{"k": "DE", "visits": 5}, {"k": "US", "visits": 4}]
    assert land["device"] == [{"k": "desktop", "visits": 9}]


def ins(table, **cols):
    keys = ",".join(cols)
    vals = ",".join("NULL" if v is None else (str(v) if isinstance(v, int) else "'%s'" % v)
                    for v in cols.values())
    return "INSERT INTO %s (%s) VALUES (%s);" % (table, keys, vals)


def ev(day, person, install=None, secs=60, os="mac", **f):
    return ins("events", ts=1, day=day, person=person, install=install, app="0.9.210", srv="0.9.209",
               shell="zsh", os=os, layout="wide", country="DE", secs=secs, **f)


P = ["%032x" % (0xabc0 + i) for i in range(6)]
I_OLD = "11111111-2222-4333-8444-555555555555"   # first seen before the window
I_NEW = "66666666-7777-4888-9999-aaaaaaaaaaaa"   # first seen inside it


def test_real_sql_against_schema(tmp_path):
    seed = [
        ins("installs", id=I_OLD, first_day="2026-09-07", last_day="2026-10-01", events=3),
        ins("installs", id=I_NEW, first_day="2026-09-28", last_day="2026-09-30", events=2),
        ev("2026-09-08", P[0], I_OLD),                    # before the window, retention only
        ev("2026-09-28", P[0], I_OLD, secs=10, f_explorer=2),
        ev("2026-09-28", P[1], I_NEW, secs=20, f_voice=1),
        ev("2026-09-28", P[1], I_NEW, secs=30),
        ev("2026-09-28", P[2], None, secs=40, os="ios"),
        ev("2026-09-30", P[3], I_NEW, secs=1000, os="ios"),
        ev("2026-10-01", P[4], I_OLD, secs=5000),
        ins("fetches", ts=1, day="2026-09-28", kind="install_sh", agent="curl"),
        ins("fetches", ts=1, day="2026-09-28", kind="install_sh", agent="curl"),
        ins("fetches", ts=1, day="2026-09-28", kind="tarball", mode=None, agent="curl"),
        ins("fetches", ts=1, day="2026-09-28", kind="tarball", mode="install", agent="curl"),
        ins("fetches", ts=1, day="2026-09-29", kind="tarball", mode="update", agent="curl"),
        ins("fetches", ts=1, day="2026-09-28", kind="version", ref_host="hosted", agent="browser"),
        ins("fetches", ts=1, day="2026-09-28", kind="version", ref_host=P[5], agent="browser"),
        ins("fetches", ts=1, day="2026-09-29", kind="version", ref_host=P[5], agent="browser"),
        ins("fetches", ts=1, day="2026-09-29", kind="version", ref_host=None, agent="browser"),
        ins("fetches", ts=1, day="2026-09-20", kind="install_sh", agent="curl"),   # outside the window
    ]
    r = one(tmp_path, headers=KEY, query="?days=7", sqlite=seed, fetch=RUM_OK)
    assert r["status"] == 200, r["text"]
    b = r["body"]
    assert b["installs"]["totals"] == {"attempts": 2, "installs": 2, "updates": 1,
                                        "version_checks": 4, "active_self": 1}
    d28 = next(d for d in b["installs"]["days"] if d["day"] == "2026-09-28")
    assert d28 == {"day": "2026-09-28", "attempts": 2, "installs": 2, "updates": 0,
                   "version_checks": 2, "active_self": 1}
    app = b["app"]
    a28 = next(d for d in app["days"] if d["day"] == "2026-09-28")
    assert a28 == {"day": "2026-09-28", "people": 3, "sessions": 4, "consented_sessions": 3,
                   "new_installs": 1, "returning_installs": 1}
    a30 = next(d for d in app["days"] if d["day"] == "2026-09-30")
    assert a30["new_installs"] == 0 and a30["returning_installs"] == 1
    assert app["totals"] == {"people_sum": 5, "sessions": 6, "consented_sessions": 5,
                             "installs_seen": 2, "new_installs": 1, "returning_installs": 1}
    # secs in the window: 10 20 30 40 1000 5000
    assert app["len"]["median"] == 30 and app["len"]["p90"] == 5000
    assert [h["n"] for h in app["len"]["hist"]] == [3, 1, 0, 0, 0, 1, 1]
    assert app["feats"]["explorer"]["sessions"] == 1 and app["feats"]["voice"]["sessions"] == 1
    assert app["os"] == [{"k": "mac", "n": 4}, {"k": "ios", "n": 2}]
    assert app["versions"] == [{"k": "0.9.210", "n": 6}]
    assert app["consent_share"] == 5 / 6
    # I_OLD's cohort is the week of Mon 09-07; its 09-08 event is that same
    # week, and its next ones (09-28, 10-01) are three weeks on. I_NEW's
    # cohort is the week of 09-28, with no later week yet.
    weekly = {w["week"]: w for w in b["retention"]["weekly"]}
    assert weekly["2026-09-07"] == {"week": "2026-09-07", "new": 1, "back1": 0, "back2": 0,
                                    "back3": 1, "back4": 0}
    assert weekly["2026-09-28"] == {"week": "2026-09-28", "new": 1, "back1": 0, "back2": 0,
                                    "back3": 0, "back4": 0}
    monthly = {m["month"]: m for m in b["retention"]["monthly"]}
    assert monthly["2026-09"] == {"month": "2026-09", "new": 2, "back1": 1, "back2": 0}
    # No person hash, install id or ref_host pseudonym reaches the answer.
    assert not HEX32.search(r["text"]), r["text"]
    assert not UUID.search(r["text"]), r["text"]
    for s in r["stmts"]:
        sel = s["sql"].split("FROM")[0]
        assert not re.search(r"SELECT\s+(DISTINCT\s+)?(person|install|ref_host)\b", sel)
