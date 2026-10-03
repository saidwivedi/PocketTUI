"""functions/_lib/usage.js, the hashing and housekeeping the usage Functions share.

Run under node, which has the same Web Crypto the Workers runtime does. Each
case names an export and its arguments; the runner prints what it returned
(or the error it threw), and for purgeMaybe every statement a fake D1 saw.
"""

import json
import re
import shutil
import subprocess
import time

import pytest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "functions" / "_lib" / "usage.js"

RUNNER = """
import { readFileSync } from "node:fs";
const mod = await import(process.argv[2]);
const cases = JSON.parse(readFileSync(process.argv[3], "utf8"));
const out = [];
for (const c of cases) {
  // undefined does not survive JSON, so the cases spell it as a marker.
  const args = c.args.map((a) => (a === "__undefined__" ? undefined : a));
  const calls = [];
  if (c.fn === "purgeMaybe") {
    const db = {
      prepare(sql) {
        const call = { sql, binds: null, ran: false };
        calls.push(call);
        return { bind(...b) { call.binds = b; return { run: async () => { call.ran = true; } }; } };
      },
    };
    args.unshift(db);
  }
  try {
    out.push({ value: await mod[c.fn](...args), calls });
  } catch (e) {
    out.push({ error: String(e && e.message), calls });
  }
}
console.log(JSON.stringify(out));
"""

SALT = {"USAGE_SALT": "0123456789abcdef0123456789abcdef"}
OTHER = {"USAGE_SALT": "fedcba9876543210fedcba9876543210"}
IP = "203.0.113.7"
UA = "Mozilla/5.0 (iPhone)"
HEX32 = re.compile(r"[0-9a-f]{32}")
U = "__undefined__"


def run(tmp_path, cases):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH")
    runner = tmp_path / "runner.mjs"
    runner.write_text(RUNNER, encoding="utf-8")
    data = tmp_path / "cases.json"
    data.write_text(json.dumps([dict(fn=f, args=a) for f, a in cases]), encoding="utf-8")
    p = subprocess.run([node, str(runner), LIB.as_uri(), str(data)],
                       check=True, capture_output=True, text=True)
    return json.loads(p.stdout)


def values(tmp_path, cases):
    res = run(tmp_path, cases)
    for r in res:
        assert "error" not in r, r
    return [r["value"] for r in res]


def test_utc_day(tmp_path):
    assert values(tmp_path, [("utcDay", [0]), ("utcDay", [1790000000000])]) == [
        "1970-01-01", "2026-09-21"]


def test_person_hash(tmp_path):
    a, b, nxt, other, ua2 = values(tmp_path, [
        ("personHash", [SALT, "2026-10-01", IP, UA]),
        ("personHash", [SALT, "2026-10-01", IP, UA]),
        ("personHash", [SALT, "2026-10-02", IP, UA]),
        ("personHash", [OTHER, "2026-10-01", IP, UA]),
        ("personHash", [SALT, "2026-10-01", IP, "curl/8.4.0"]),
    ])
    assert a == b
    assert len({a, nxt, other, ua2}) == 4
    for h in (a, nxt, other, ua2):
        assert HEX32.fullmatch(h)
        assert IP not in h


def test_day_key_is_hmac_of_day(tmp_path):
    import hashlib
    import hmac
    (k,) = values(tmp_path, [("dayKey", [SALT, "2026-10-01"])])
    assert k == hmac.new(SALT["USAGE_SALT"].encode(), b"2026-10-01", hashlib.sha256).hexdigest()
    (p,) = values(tmp_path, [("personHash", [SALT, "2026-10-01", IP, UA])])
    assert p == hashlib.sha256(f"{k}|{IP}|{UA}".encode()).hexdigest()[:32]


def test_missing_salt_throws(tmp_path):
    res = run(tmp_path, [("dayKey", [{}, "2026-10-01"]),
                         ("personHash", [{}, "2026-10-01", IP, UA]),
                         ("refHostHash", [{}, "box.example.net:5560"])])
    for r in res:
        assert "USAGE_SALT" in r["error"]


def test_ref_host_hash(tmp_path):
    hosted, mixed, empty, undef, a, a2, upper, b, a_other = values(tmp_path, [
        ("refHostHash", [SALT, "pockettui.com"]),
        ("refHostHash", [SALT, "PockeTTUI.com"]),
        ("refHostHash", [SALT, ""]),
        ("refHostHash", [SALT, U]),
        ("refHostHash", [SALT, "box.example.net:5560"]),
        ("refHostHash", [SALT, "box.example.net:5560"]),
        ("refHostHash", [SALT, "BOX.example.net:5560"]),
        ("refHostHash", [SALT, "box.example.net:5561"]),
        ("refHostHash", [OTHER, "box.example.net:5560"]),
    ])
    assert hosted == mixed == "hosted"
    assert empty is None and undef is None
    assert HEX32.fullmatch(a)
    assert a == a2 == upper
    assert a != b and a != a_other


def test_agent_class(tmp_path):
    assert values(tmp_path, [
        ("agentClass", ["curl/8.4.0"]),
        ("agentClass", ["Wget/1.21"]),
        ("agentClass", ["Mozilla/5.0 (X11; Linux x86_64)"]),
        ("agentClass", [""]),
        ("agentClass", [U]),
    ]) == ["curl", "curl", "browser", "other", "other"]


def test_purge_maybe(tmp_path):
    now = int(time.time() * 1000)
    skip, hit = run(tmp_path, [("purgeMaybe", [now, 0.5]), ("purgeMaybe", [now, 0.001])])
    assert skip["calls"] == []
    assert [c["sql"] for c in hit["calls"]] == [
        "DELETE FROM events WHERE ts < ?", "DELETE FROM fetches WHERE ts < ?"]
    for c in hit["calls"]:
        assert c["ran"]
        (cutoff,) = c["binds"]
        assert cutoff == now - 400 * 24 * 3600 * 1000


def test_purge_maybe_swallows_db_errors(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH")
    script = tmp_path / "throw.mjs"
    script.write_text(
        "const m = await import(process.argv[2]);\n"
        "const db = { prepare() { throw new Error('d1 down'); } };\n"
        "console.log(String(await m.purgeMaybe(db, Date.now(), 0)));\n", encoding="utf-8")
    p = subprocess.run([node, str(script), LIB.as_uri()], check=True, capture_output=True, text=True)
    assert p.stdout.strip() == "undefined"
