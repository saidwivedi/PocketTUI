"""src/console/index.html, the static usage console page.

The page's pure data-shaping helpers sit between the BEGIN SHAPING and END
SHAPING markers in its inline script; the node tests cut that chunk out, run it
against a sample /api/console response and check how the D1 counts and
Cloudflare's nightly copy are merged.
"""

import json
import re
import shutil
import subprocess

import pytest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PAGE = REPO / "src" / "console" / "index.html"

RUNNER = r"""
import { readFileSync } from "node:fs";
const chunk = readFileSync(process.argv[2], "utf8");
const input = JSON.parse(readFileSync(process.argv[3], "utf8"));
const api = new Function(chunk + "\nreturn { mergeInstallDays, historyTotals, peopleRows, peopleRange, peopleRangeText, landingFallbackDays, nearestIndex, readoutLines };")();
const h = input.history.days;
console.log(JSON.stringify({
  merged: api.mergeInstallDays(input.installs.days, h),
  history: api.historyTotals(h),
  people: api.peopleRows(input.app.days, h),
  landing: api.landingFallbackDays(h),
  ranges: (input.ranges || []).map(([days, totals]) => api.peopleRange(days, totals)),
  rangeTexts: (input.rangeTexts || []).map((r) => api.peopleRangeText(r)),
  nearest: (input.nearest || []).map(([xs, x]) => api.nearestIndex(xs, x)),
  lines: (input.lines || []).map(([series, i]) => api.readoutLines(series, i)),
}));
"""

ZERO_D1 = dict(attempts=0, installs=0, updates=0, version_checks=0, active_self=0)
ZERO_H = dict(landing_visits=0, app_visits=0, installer_runs=0, installer_ips=0,
              tarball_fetches=0, tarball_ips=0, version_checks=0, version_ips=0)
DAYS = ["2026-09-30", "2026-10-01", "2026-10-02", "2026-10-03"]


def page():
    return PAGE.read_text(encoding="utf-8")


def shaping_chunk():
    m = re.search(r"// BEGIN SHAPING\n(.*?)\n\s*// END SHAPING", page(), re.S)
    assert m, "BEGIN/END SHAPING markers missing from the console page"
    return m.group(1)


def run(tmp_path, response):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH")
    runner = tmp_path / "runner.mjs"
    runner.write_text(RUNNER, encoding="utf-8")
    chunk = tmp_path / "chunk.js"
    chunk.write_text(shaping_chunk(), encoding="utf-8")
    data = tmp_path / "response.json"
    data.write_text(json.dumps(response), encoding="utf-8")
    p = subprocess.run([node, "--no-warnings", str(runner), str(chunk), str(data)],
                       check=True, capture_output=True, text=True)
    return json.loads(p.stdout)


def sample():
    installs = [
        dict(ZERO_D1, day=DAYS[0]),                                   # nothing anywhere
        dict(ZERO_D1, day=DAYS[1]),                                   # Cloudflare only
        dict(ZERO_D1, day=DAYS[2], attempts=4, installs=2, updates=1),  # both; app wins
        dict(ZERO_D1, day=DAYS[3], version_checks=9),                 # app counted something
    ]
    history = [
        dict(ZERO_H, day=DAYS[0], landing_visits=5),
        dict(ZERO_H, day=DAYS[1], installer_runs=7, tarball_fetches=11, tarball_ips=6, version_ips=30, landing_visits=40),
        dict(ZERO_H, day=DAYS[2], installer_runs=3, tarball_fetches=5, tarball_ips=4, version_ips=25),
        dict(ZERO_H, day=DAYS[3], installer_runs=2, tarball_fetches=2, tarball_ips=2),
    ]
    app = [dict(day=d, people=p) for d, p in zip(DAYS, [0, 0, 12, 20])]
    return {"installs": {"days": installs}, "history": {"days": history}, "app": {"days": app}}


def test_page_is_self_contained():
    s = page()
    assert "<script src=" not in s
    assert "<link" not in s
    assert "http://" not in s


def test_page_strings():
    s = page()
    assert "from Cloudflare" in s
    assert "Forget key" in s
    assert 'id="forget"' in s


def test_merge_install_days_tags_sources(tmp_path):
    out = run(tmp_path, sample())
    rows = out["merged"]["rows"]
    assert [r["day"] for r in rows] == DAYS
    assert [r["source"] for r in rows] == ["app", "cloudflare", "app", "app"]
    cf = rows[1]
    assert (cf["cf_runs"], cf["cf_tarballs"], cf["cf_ips"]) == (7, 11, 6)
    assert (cf["installs"], cf["updates"], cf["attempts"]) == (0, 0, 0)
    # D1 numbers win even though history has numbers for the same day.
    both = rows[2]
    assert (both["installs"], both["updates"], both["attempts"]) == (2, 1, 4)
    assert (both["cf_runs"], both["cf_tarballs"], both["cf_ips"]) == (0, 0, 0)
    # A D1 row with any non-zero column (here version checks only) stays the app's.
    assert rows[3]["cf_tarballs"] == 0


def test_merge_install_days_totals_per_source(tmp_path):
    t = run(tmp_path, sample())["merged"]["totals"]
    assert t["app"] == {"days": 3, "attempts": 4, "installs": 2, "updates": 1}
    assert t["cloudflare"] == {"days": 1, "installer_runs": 7, "tarball_fetches": 11, "tarball_ips": 6}


def test_merge_install_days_without_history(tmp_path):
    resp = sample()
    resp["history"] = {"days": []}
    out = run(tmp_path, resp)
    assert {r["source"] for r in out["merged"]["rows"]} == {"app"}
    assert out["merged"]["totals"]["cloudflare"]["days"] == 0


def test_window_history_and_secondary_series(tmp_path):
    out = run(tmp_path, sample())
    h = out["history"]
    assert (h["installer_runs"], h["tarball_fetches"], h["tarball_ips"]) == (12, 18, 12)
    assert [r["version_ips"] for r in out["people"]] == [0, 30, 25, 0]
    assert [r["people"] for r in out["people"]] == [0, 0, 12, 20]
    assert out["landing"] == [{"day": d, "visits": v} for d, v in zip(DAYS, [5, 40, 0, 0])]


def test_card_head_legend_can_wrap():
    # A legend sits beside the card title; an item kept on one line ("Devices
    # checking for updates (Cloudflare)") is wider than the head on a 320-375
    # px phone and scrolls the whole page sideways. Nothing in the head may
    # forbid wrapping.
    css = re.search(r"<style>(.*?)</style>", page(), re.S).group(1)
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    offenders = [sel.strip() for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)
                 if re.search(r"\.(legend|card-head)\b", sel) and re.search(r"white-space\s*:\s*nowrap", body)]
    assert offenders == [], offenders


def test_people_range(tmp_path):
    days = [dict(day=d, people=p) for d, p in zip(DAYS, [3, 5, 2])]
    resp = sample()
    resp["ranges"] = [
        [[], {}],
        [days, {"installs_seen": 4, "people_sum": 10}],
        [days, {"installs_seen": 9, "people_sum": 10}],
        [days, {"installs_seen": 4}],
    ]
    out = run(tmp_path, resp)["ranges"]
    assert out[0] == {"low": 0, "high": 0}
    assert out[1] == {"low": 5, "high": 10}
    assert out[2] == {"low": 9, "high": 10}
    # Without people_sum the high end is the sum of the days.
    assert out[3] == {"low": 5, "high": 10}


def test_people_range_text(tmp_path):
    resp = sample()
    resp["rangeTexts"] = [{"low": 0, "high": 0}, {"low": 7, "high": 7}, {"low": 21, "high": 146}]
    assert run(tmp_path, resp)["rangeTexts"] == ["0", "7", "21 to 146"]


def test_nearest_index(tmp_path):
    xs = [10, 20, 30, 40]
    resp = sample()
    resp["nearest"] = [
        [[], 5],          # empty
        [xs, -100],       # before the first
        [xs, 10],         # on the first
        [xs, 999],        # after the last
        [xs, 22],         # between, nearer the left
        [xs, 28],         # between, nearer the right
        [xs, 25],         # tie goes to the later index
        [[7], 3],         # one point
        [[0, 1, 2, 3, 4, 5, 6], 4.6],
    ]
    assert run(tmp_path, resp)["nearest"] == [-1, 0, 0, 3, 1, 2, 2, 0, 5]


def test_readout_lines(tmp_path):
    people = [{"name": "People", "sw": "sw-1", "values": [7, 1234]},
              {"name": "Devices checking for updates", "sw": "sw-line3", "values": [19, 1234567]}]
    installs = [{"name": "Installs", "sw": "sw-1", "values": [3, None]},
                {"name": "Updates", "sw": "sw-2", "values": [2, None]},
                {"name": "Tarball downloads", "sw": "sw-3", "est": True, "values": [None, 11]},
                {"name": "Installer runs", "est": True, "values": [None, 7]}]
    resp = sample()
    resp["lines"] = [[people, 0], [people, 1], [installs, 0], [installs, 1], [[], 0],
                     [[{"name": "Per day", "values": [2.25]}], 0]]
    out = run(tmp_path, resp)["lines"]
    assert out[0] == [{"sw": "sw-1", "name": "People", "text": "7"},
                      {"sw": "sw-line3", "name": "Devices checking for updates", "text": "19"}]
    # Full numbers, never the 1.2k short form.
    assert [e["text"] for e in out[1]] == ["1,234", "1,234,567"]
    # An app day lists the app's counts and no Cloudflare note.
    assert out[2] == [{"sw": "sw-1", "name": "Installs", "text": "3"},
                      {"sw": "sw-2", "name": "Updates", "text": "2"}]
    # A hatched day skips the app series and says once that it is an estimate.
    assert out[3] == [{"sw": "sw-3", "name": "Tarball downloads", "text": "11"},
                      {"sw": "", "name": "Installer runs", "text": "7 (Cloudflare estimate)"}]
    assert out[4] == []
    assert out[5][0]["text"] in ("2.3", "2.2")


def test_every_chart_has_a_readout():
    s = page()
    script = re.search(r"<script>(.*?)</script>", s, re.S).group(1)
    roots = re.findall(r"`<svg [^`]*", script)
    assert roots, "no chart svg template found"
    for r in roots:
        assert 'tabindex="0"' in r and "touch-action: pan-y" in r and "aria-label=" in r, r
    # All three draws go through the one root, and none keeps a hover-only <title>.
    for fn in ("barChart", "lineChart", "histChart"):
        body = re.search(r"function " + fn + r"\(.*?\n  }\n", script, re.S).group(0)
        assert "svgRoot(" in body and "<title>" not in body, fn
    # chart() emits a polite readout box next to every chart container.
    chart_fn = re.search(r"function chart\(spec\).*?\n  }\n", script, re.S).group(0)
    assert 'class="readout"' in chart_fn and 'aria-live="polite"' in chart_fn
    # Every chart spec names the series its readout lists.
    specs = re.findall(r"chart\(\{ type: \"(?:bar|line|hist)\".*?\}\) \+|chart\(\{ type: \"line\".*?\}\);", script, re.S)
    assert len(specs) == 5, len(specs)
    for sp in specs:
        assert "ro: { " in sp, sp[:80]
