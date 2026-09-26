"""functions/api/report.js, the Pages Function that mails in-app reports.

Run under node with a stubbed global fetch, so what is checked is the payload
the function would hand to Resend: the attachment carrying a debug log, and the
refusals that guard the mailbox. No mail is sent.
"""

import base64
import json
import re
import shutil
import subprocess

import pytest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FUNCTION = REPO / "functions" / "api" / "report.js"

# Each case is posted as a raw body (with an optional Content-Length header);
# the runner prints, per case, the response and every fetch the function made.
RUNNER = """
import { readFileSync } from "node:fs";
const mod = await import(process.argv[2]);
const cases = JSON.parse(readFileSync(process.argv[3], "utf8"));
const out = [];
for (const c of cases) {
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url, body: JSON.parse(init.body) });
    return new Response("{}", { status: 200 });
  };
  const headers = new Headers({ "Content-Type": "application/json" });
  if (c.contentLength !== undefined) headers.set("Content-Length", String(c.contentLength));
  const request = { headers, text: async () => c.raw };
  const r = await mod.onRequestPost({ request, env: { RESEND_API_KEY: "test-key" } });
  out.push({ status: r.status, body: await r.json(), calls });
}
console.log(JSON.stringify(out));
"""


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


def post(fields, **extra):
    return dict(raw=json.dumps(fields), **extra)


BASE = {"message": "The terminal froze", "email": "", "diag": "version: v1", "website": ""}


def test_log_is_attached_as_base64_utf8(tmp_path):
    log = "PocketTUI debug log\n12:00.000  größe café 日本 — done\n12:00.001  last"
    (res,) = run_cases(tmp_path, [post(dict(BASE, log=log))])
    assert res["status"] == 200
    (call,) = res["calls"]
    assert call["url"] == "https://api.resend.com/emails"
    (att,) = call["body"]["attachments"]
    assert re.fullmatch(r"pockettui-debug-\d{8}-\d{4}\.log", att["filename"])
    assert base64.b64decode(att["content"]).decode("utf-8") == log
    assert "Debug log attached: 3 lines, 1 KB" in call["body"]["text"]
    assert "truncated" not in call["body"]["text"]
    assert call["body"]["subject"] == "[report] The terminal froze"


def test_report_without_log_is_unchanged(tmp_path):
    (res,) = run_cases(tmp_path, [post(BASE)])
    assert res["status"] == 200
    (call,) = res["calls"]
    assert "attachments" not in call["body"]
    # The body as it was before logs existed.
    assert call["body"]["text"] == "\n".join(
        ["The terminal froze", "", "-- ", "From: (not given)", "", "version: v1"])
    assert "reply_to" not in call["body"]


def test_oversized_log_keeps_its_tail(tmp_path):
    cap = 256 * 1024
    log = "HEAD\n" + ("x" * 99 + "\n") * 2700 + "the last line"
    assert len(log) > cap
    (res,) = run_cases(tmp_path, [post(dict(BASE, log=log))])
    assert res["status"] == 200
    (call,) = res["calls"]
    sent = base64.b64decode(call["body"]["attachments"][0]["content"]).decode("utf-8")
    assert sent == log[-cap:]
    assert sent.endswith("the last line")
    dropped = len(log) - cap
    assert ("Debug log truncated: the first %d characters were dropped, the last %d kept."
            % (dropped, cap)) in call["body"]["text"]


def test_body_over_the_cap_is_refused(tmp_path):
    big = post(dict(BASE, log="y" * (320 * 1024)))
    assert len(big["raw"]) > 320 * 1024
    under = post(dict(BASE, log="y" * (300 * 1024)))
    results = run_cases(tmp_path, [
        # Refused on the header, before the body is read.
        dict(big, contentLength=len(big["raw"])),
        # A header that lied is caught on the length read.
        dict(big, contentLength=100),
        # Just under the cap goes through.
        dict(under, contentLength=len(under["raw"])),
    ])
    assert [r["status"] for r in results] == [413, 413, 200]
    assert results[0]["calls"] == [] and results[1]["calls"] == []


def test_honeypot_still_answers_ok_with_nothing_sent(tmp_path):
    (res,) = run_cases(tmp_path, [post(dict(BASE, website="bot.example.net", log="x"))])
    assert res["status"] == 200
    assert res["body"] == {"ok": True}
    assert res["calls"] == []
