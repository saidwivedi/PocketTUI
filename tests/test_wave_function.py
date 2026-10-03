"""functions/api/wave.js, the Pages Function that stores one anonymous event per app session.

Run under node with a fake D1, so what is checked is every statement the
function would send and the answer it gives. No database is touched.
"""

import json
import re
import shutil
import subprocess
from datetime import datetime, timezone

import pytest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FUNCTION = REPO / "functions" / "api" / "wave.js"

# Each case is posted as a raw body with optional headers. The fake D1 records
# every bound statement and how it ran; `first` sets what the cap query sees,
# `throws` makes run()/batch() fail, `noDb`/`noSalt` drop those from the env,
# `cf` sets request.cf, and `now` pins Date.now for the case.
RUNNER = """
import { readFileSync } from "node:fs";
const mod = await import(process.argv[2]);
const cases = JSON.parse(readFileSync(process.argv[3], "utf8"));
const realNow = Date.now;
const out = [];
for (const c of cases) {
  Date.now = c.now !== undefined ? () => c.now : realNow;
  const stmts = [];
  const db = {
    prepare(sql) {
      const st = { sql, binds: null, how: null };
      stmts.push(st);
      const bound = {
        st,
        run: async () => { st.how = "run"; if (c.throws) throw new Error("d1 down"); return {}; },
        first: async () => { st.how = "first"; return c.first !== undefined ? c.first : { n: 0 }; },
        all: async () => { st.how = "all"; return { results: [] }; },
      };
      return { bind(...b) { st.binds = b; return bound; } };
    },
    async batch(list) {
      for (const b of list) b.st.how = "batch";
      if (c.throws) throw new Error("d1 down");
      return list.map(() => ({}));
    },
  };
  const env = {};
  if (!c.noSalt) env.USAGE_SALT = "0123456789abcdef0123456789abcdef";
  if (!c.noDb) env.DB = db;
  const headers = new Headers(c.headers || {});
  if (c.contentLength !== undefined) headers.set("Content-Length", String(c.contentLength));
  const request = { method: c.method || "POST", headers, text: async () => c.raw };
  if (c.cf) request.cf = c.cf;
  const waits = [];
  const context = { request, env, waitUntil(p) { waits.push(p); } };
  const r = c.method === "OPTIONS" ? mod.onRequestOptions(context) : await mod.onRequestPost(context);
  await Promise.all(waits);
  out.push({
    status: r.status,
    headers: Object.fromEntries(r.headers.entries()),
    text: await r.text(),
    stmts,
  });
}
Date.now = realNow;
console.log(JSON.stringify(out));
"""

IP_HEADERS = {"CF-Connecting-IP": "203.0.113.7", "User-Agent": "Mozilla/5.0 (iPhone)",
              "CF-IPCountry": "DE", "Content-Type": "text/plain"}
INSTALL_ID = "3f2b8c1e-4a5d-4e6f-9a7b-0c1d2e3f4a5b"
EVENT = {
    "v": 1, "app": "0.9.205", "srv": "0.9.204", "shell": "self", "os": "ios",
    "layout": "phone", "pwa": 1, "secs": 754, "rc": 2, "seen": 3,
    "f": {"explorer": 4, "browser": 0, "diff": 1, "voice": 7, "newsess": 2},
    "id": INSTALL_ID,
}
FEATURES = ["explorer", "browser", "diff", "side2", "voice", "settings",
            "reader", "editor", "viewer", "search", "newsess"]
HEX32 = re.compile(r"[0-9a-f]{32}")


def run_cases(tmp_path, cases):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH")
    runner = tmp_path / "runner.mjs"
    runner.write_text(RUNNER, encoding="utf-8")
    data = tmp_path / "cases.json"
    data.write_text(json.dumps(cases), encoding="utf-8")
    p = subprocess.run([node, str(runner), FUNCTION.as_uri(), str(data)],
                       check=True, capture_output=True, text=True)
    return json.loads(p.stdout)


def post(fields, headers=IP_HEADERS, **extra):
    return dict(raw=json.dumps(fields), headers=dict(headers), **extra)


def without(d, key):
    return {k: v for k, v in d.items() if k != key}


def inserts(res, table):
    return [s for s in res["stmts"] if s["sql"].startswith("INSERT INTO " + table)]


def event_binds(res):
    (st,) = inserts(res, "events")
    cols = re.search(r"\(([^)]*)\)", st["sql"]).group(1).split(", ")
    assert len(cols) == len(st["binds"]) == 25
    return dict(zip(cols, st["binds"]))


def test_valid_event_with_id_stores_row_and_upserts_install(tmp_path):
    (res,) = run_cases(tmp_path, [post(EVENT)])
    assert res["status"] == 204
    assert res["headers"]["cache-control"] == "no-store"
    assert res["headers"]["access-control-allow-origin"] == "*"
    row = event_binds(res)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert row["day"] == today
    assert isinstance(row["ts"], int)
    assert HEX32.fullmatch(row["person"])
    assert row["install"] == INSTALL_ID
    assert row["country"] == "DE"
    for k in ["app", "srv", "shell", "os", "layout", "pwa", "secs", "rc", "seen"]:
        assert row[k] == EVENT[k], k
    for k in FEATURES:
        assert row["f_" + k] == EVENT["f"].get(k, 0), k
    (up,) = inserts(res, "installs")
    assert "ON CONFLICT(id) DO UPDATE SET last_day = excluded.last_day, events = events + 1" in up["sql"]
    assert up["binds"] == [INSTALL_ID, today, today]
    # Both writes went through one batch.
    assert {s["how"] for s in inserts(res, "")} == {"batch"}
    (cap,) = [s for s in res["stmts"] if s["sql"].startswith("SELECT COUNT(*)")]
    assert cap["binds"] == [today, row["person"]]


def test_event_without_id_has_null_install_and_no_upsert(tmp_path):
    (res,) = run_cases(tmp_path, [post(without(EVENT, "id"))])
    assert res["status"] == 204
    assert event_binds(res)["install"] is None
    assert inserts(res, "installs") == []
    assert inserts(res, "events")[0]["how"] == "run"


def test_invalid_payloads_are_refused_with_nothing_written(tmp_path):
    bad = [
        dict(EVENT, v=2),
        dict(EVENT, os="beos"),
        dict(EVENT, shell="hosted "),
        dict(EVENT, layout="tablet"),
        dict(EVENT, id="3f2b8c1e-4a5d-1e6f-9a7b-0c1d2e3f4a5b"),  # v1, not v4
        dict(EVENT, id=INSTALL_ID.upper()),
        dict(EVENT, extra=1),
        dict(EVENT, f=dict(EVENT["f"], tetris=1)),
        dict(EVENT, f=[1, 2]),
        dict(EVENT, secs="5"),
        dict(EVENT, secs=1.5),
        dict(EVENT, rc=-1),
        dict(EVENT, f=dict(EVENT["f"], diff=-3)),
        dict(EVENT, pwa=True),
        dict(EVENT, pwa=2),
        dict(EVENT, app="0.9.205; drop"),
        dict(EVENT, srv="x" * 21),
        dict(EVENT, app=None),
        without(EVENT, "secs"),
        [EVENT],
    ]
    results = run_cases(tmp_path, [post(b) for b in bad])
    for b, r in zip(bad, results):
        assert r["status"] == 400, b
        assert r["stmts"] == [], b


def test_oversized_and_unparsable_bodies(tmp_path):
    big = post(dict(EVENT, pad="x" * 3000))
    assert len(big["raw"]) > 2048
    results = run_cases(tmp_path, [
        big,                                     # no header: caught on the length read
        dict(big, contentLength=len(big["raw"])),  # refused on the header
        dict(post(EVENT), contentLength=5000),   # header alone is enough
        dict(raw="not json {", headers=IP_HEADERS),
        dict(raw="", headers={}),
    ])
    assert [r["status"] for r in results] == [413, 413, 413, 400, 400]
    assert json.loads(results[0]["text"]) == {"error": "too large"}
    assert all(r["stmts"] == [] for r in results)
    assert all(r["headers"]["cache-control"] == "no-store" for r in results)


def test_no_content_type_is_accepted(tmp_path):
    (res,) = run_cases(tmp_path, [post(EVENT, headers=without(IP_HEADERS, "Content-Type"))])
    assert res["status"] == 204
    assert len(inserts(res, "events")) == 1


def test_daily_cap_answers_204_without_insert(tmp_path):
    (res,) = run_cases(tmp_path, [post(EVENT, first={"n": 500})])
    assert res["status"] == 204
    assert inserts(res, "") == []
    (under,) = run_cases(tmp_path, [post(EVENT, first={"n": 499})])
    assert len(inserts(under, "events")) == 1


def test_backend_failures_still_answer_204(tmp_path):
    results = run_cases(tmp_path, [
        post(EVENT, throws=True),
        post(without(EVENT, "id"), throws=True),
        post(EVENT, noDb=True),
        post(EVENT, noSalt=True),
    ])
    assert [r["status"] for r in results] == [204, 204, 204, 204]
    assert all(r["headers"]["cache-control"] == "no-store" for r in results)
    # Without the salt nothing may be written, not even with an unkeyed hash.
    assert inserts(results[3], "") == []


def test_person_hash_follows_address_and_day(tmp_path):
    day1 = int(datetime(2026, 10, 3, 12, tzinfo=timezone.utc).timestamp() * 1000)
    day1_late = int(datetime(2026, 10, 3, 23, 59, tzinfo=timezone.utc).timestamp() * 1000)
    day2 = int(datetime(2026, 10, 4, 0, 1, tzinfo=timezone.utc).timestamp() * 1000)
    other_ip = dict(IP_HEADERS, **{"CF-Connecting-IP": "198.51.100.9"})
    other_ua = dict(IP_HEADERS, **{"User-Agent": "Mozilla/5.0 (Android)"})
    results = run_cases(tmp_path, [
        post(EVENT, now=day1),
        post(EVENT, now=day1_late),
        post(EVENT, now=day2),
        post(EVENT, headers=other_ip, now=day1),
        post(EVENT, headers=other_ua, now=day1),
    ])
    rows = [event_binds(r) for r in results]
    persons = [r["person"] for r in rows]
    assert [r["day"] for r in rows] == ["2026-10-03", "2026-10-03", "2026-10-04",
                                        "2026-10-03", "2026-10-03"]
    assert rows[0]["ts"] == day1
    assert persons[0] == persons[1]
    assert len(set([persons[0], persons[2], persons[3], persons[4]])) == 4


def test_options_answers_cors_preflight(tmp_path):
    (res,) = run_cases(tmp_path, [dict(method="OPTIONS", raw="")])
    assert res["status"] == 204
    h = res["headers"]
    assert h["access-control-allow-origin"] == "*"
    assert h["access-control-allow-methods"] == "POST, OPTIONS"
    assert h["access-control-allow-headers"] == "Content-Type"
    assert h["access-control-max-age"] == "86400"
    assert h["cache-control"] == "no-store"


def test_large_counts_are_clamped(tmp_path):
    ev = dict(EVENT, secs=10 ** 9, rc=10 ** 9, seen=10 ** 9, f={"voice": 10 ** 9})
    (res,) = run_cases(tmp_path, [post(ev)])
    assert res["status"] == 204
    row = event_binds(res)
    assert row["secs"] == 604800
    assert row["rc"] == 100000 and row["seen"] == 100000
    assert row["f_voice"] == 100000 and row["f_explorer"] == 0


def test_country_prefers_cf_object_and_is_capped(tmp_path):
    results = run_cases(tmp_path, [
        post(EVENT, cf={"country": "FR"}),
        post(EVENT, headers=dict(IP_HEADERS, **{"CF-IPCountry": "DEU"})),
        post(EVENT, headers=without(IP_HEADERS, "CF-IPCountry")),
    ])
    assert [event_binds(r)["country"] for r in results] == ["FR", "DE", ""]
