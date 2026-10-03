"""pull_cf_stats.py, the daily copy of Cloudflare's analytics into D1.

The metric math runs on small fixtures of GraphQL rows; the day loop runs with
the network functions replaced, so nothing leaves the machine.
"""

import datetime
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("pull_cf_stats", REPO / "pull_cf_stats.py")
pcs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pcs)

TODAY = datetime.date(2026, 10, 3)


def zr(path, ip, country="DE", ua="curl/8.4.0", status=200, count=1):
    return {"count": count, "dimensions": {"clientRequestPath": path, "clientIP": ip, "clientCountryName": country,
                                           "userAgent": ua, "edgeResponseStatus": status}}


BROWSER = "Mozilla/5.0 (Macintosh) Safari/605.1"
ZONE = [
    zr("/install.sh", "1.1.1.1", count=2),
    zr("/install.sh", "1.1.1.1", ua="Wget/1.21", count=3),
    zr("/install.sh", "2.2.2.2", ua="CURL/7.0"),
    zr("/install.sh", "3.3.3.3", ua=BROWSER, count=4),
    zr("/install.sh", "4.4.4.4", ua="python-requests/2.31", count=9),
    zr("/install.sh", "5.5.5.5", status=404, count=7),
    zr("/pockettui.tar.gz", "1.1.1.1", count=2),
    zr("/pockettui.tar.gz", "6.6.6.6", country="US", ua=BROWSER),
    zr("/pockettui.tar.gz", "6.6.6.6", country="US", count=3),
    zr("/version.txt", "7.7.7.7", country="FR", count=10),
    zr("/version.txt", "8.8.8.8", country="FR", status=304, count=5),
    zr("/version.txt", "9.9.9.9", country="", count=1),
    zr("/other", "1.1.1.1", count=50),
]


def as_dict(triples):
    return {(m, k): v for m, k, v in triples}


def test_agent_class_matches_usage_js():
    assert pcs.agent_class("curl/8.4.0") == "curl"
    assert pcs.agent_class("Wget/1.21") == "curl"
    assert pcs.agent_class("CuRl/1") == "curl"
    assert pcs.agent_class("Mozilla/5.0 curl/8") == "browser"
    assert pcs.agent_class("my curl/8") == "other"
    assert pcs.agent_class("") == pcs.agent_class(None) == "other"


def test_metrics_from_zone():
    m = as_dict(pcs.metrics_from_zone(ZONE))
    # Sampled counts are summed; only 2xx count; browser and other are apart.
    assert m[("installer_runs", "")] == 6
    assert m[("installer_ips", "")] == 2
    assert m[("installer_reads", "")] == 4
    assert m[("tarball_fetches", "")] == 6
    assert m[("tarball_ips", "")] == 2
    assert m[("tarball_country", "DE")] == 1 and m[("tarball_country", "US")] == 1
    assert m[("version_checks", "")] == 11
    assert m[("version_ips", "")] == 2
    assert m[("version_ips_country", "FR")] == 1 and m[("version_ips_country", "unknown")] == 1
    assert len(m) == 11


def test_metrics_from_zone_empty_still_has_totals():
    m = as_dict(pcs.metrics_from_zone([]))
    assert set(m) == {(k, "") for k in ("installer_runs", "installer_reads", "tarball_fetches", "version_checks",
                                        "installer_ips", "tarball_ips", "version_ips")}
    assert set(m.values()) == {0}


def rr(views, visits, **dims):
    r = {"count": views, "sum": {"visits": visits}}
    if dims:
        r["dimensions"] = dims
    return r


RUM_ACC = {
    "landing": [rr(16, 12)],
    "country": [rr(9, 7, countryName="DE"), rr(7, 5, countryName="US")],
    "device": [rr(10, 8, deviceType="mobile"), rr(6, 4, deviceType="")],
    "referrer": [rr(4, 3, refererHost=""), rr(2, 2, refererHost=None), rr(10, 7, refererHost="www.google.com")],
    "app": [rr(233, 26)],
}


def test_metrics_from_rum():
    m = as_dict(pcs.metrics_from_rum(RUM_ACC))
    assert m[("landing_visits", "")] == 12 and m[("landing_views", "")] == 16
    assert m[("app_visits", "")] == 26 and m[("app_views", "")] == 233
    assert m[("landing_visits_country", "DE")] == 7
    assert m[("landing_visits_device", "unknown")] == 4
    assert m[("landing_visits_referrer", "direct")] == 5
    assert m[("landing_visits_referrer", "www.google.com")] == 7


def test_upsert_sql_runs_on_the_schema():
    rows = [{"day": "2026-10-01", "metric": "m%d" % i, "key": "", "value": i, "pulled_at": 5} for i in range(45)]
    stmts = pcs.upsert_sql(rows)
    assert [len(p) for _, p in stmts] == [100, 100, 25]
    con = sqlite3.connect(":memory:")
    con.executescript((REPO / "migrations" / "0002_external_daily.sql").read_text())
    for sql, params in stmts:
        con.execute(sql, params)
    rows[3]["value"] = 99
    rows[3]["pulled_at"] = 6
    for sql, params in pcs.upsert_sql(rows[3:4]):
        con.execute(sql, params)
    assert con.execute("SELECT COUNT(*) FROM external_daily").fetchone() == (45,)
    assert con.execute("SELECT value, pulled_at FROM external_daily WHERE metric = 'm3'").fetchone() == (99, 6)


def zone_answer(rows):
    return {"data": {"viewer": {"zones": [{"httpRequestsAdaptiveGroups": rows}]}}}


def rum_answer(acc):
    return {"data": {"viewer": {"accounts": [acc]}}}


@pytest.fixture
def fake(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".cloudflare_analytics_token").write_text("tok-a\n")
    (repo / ".cloudflare_token").write_text("tok-d1\n")
    calls = {"fetch": [], "d1": []}

    def fetch_day(token, day):
        calls["fetch"].append(day.isoformat())
        assert token == "tok-a"
        return zone_answer(ZONE), rum_answer(RUM_ACC)

    def d1_query(token, sql, params):
        assert token == "tok-d1"
        calls["d1"].append((sql, params))
        return {"success": True}

    monkeypatch.setattr(pcs, "fetch_day", fetch_day)
    monkeypatch.setattr(pcs, "d1_query", d1_query)
    return repo, tmp_path / "out", calls


def run(repo, out, *argv):
    return pcs.pull(pcs.parse_args(["--repo", str(repo), "--out-dir", str(out)] + list(argv)), today=TODAY)


def test_days_newest_first_and_today_excluded(fake, capsys):
    repo, out, calls = fake
    assert run(repo, out, "--days", "3") == 0
    assert calls["fetch"] == ["2026-10-02", "2026-10-01", "2026-09-30"]
    assert sorted(p.name for p in out.iterdir()) == ["2026-09-30.json", "2026-10-01.json", "2026-10-02.json",
                                                    "pull.log"]
    n = len(pcs.metrics_from_zone(ZONE)) + len(pcs.metrics_from_rum(RUM_ACC))
    assert capsys.readouterr().out.splitlines() == ["2026-10-02: %d rows" % n, "2026-10-01: %d rows" % n,
                                                    "2026-09-30: %d rows" % n]
    saved = json.loads((out / "2026-10-02.json").read_text())
    assert saved["zone"] == zone_answer(ZONE) and len(saved["rows"]) == n
    # Every row is upserted, then the day's older rows are dropped.
    inserts = [c for c in calls["d1"] if c[0].startswith("INSERT")]
    assert sum(len(p) for _, p in inserts) == 3 * n * 5
    assert sum(1 for c in calls["d1"] if c[0].startswith("DELETE")) == 3


def test_skip_existing_and_force(fake, capsys):
    repo, out, calls = fake
    out.mkdir()
    (out / "2026-10-02.json").write_text("{}")
    assert run(repo, out, "--days", "2") == 0
    assert calls["fetch"] == ["2026-10-01"]
    assert "2026-10-02: skipped" in capsys.readouterr().out
    assert run(repo, out, "--days", "2", "--force") == 0
    assert calls["fetch"] == ["2026-10-01", "2026-10-02", "2026-10-01"]
    assert json.loads((out / "2026-10-02.json").read_text())["day"] == "2026-10-02"


def test_d1_failure_keeps_partial_and_exits_nonzero(fake, monkeypatch):
    repo, out, calls = fake

    def boom(token, sql, params):
        raise pcs.PullError("D1: down")

    monkeypatch.setattr(pcs, "d1_query", boom)
    assert run(repo, out, "--days", "1") == 1
    assert (out / "2026-10-02.json.partial").exists() and not (out / "2026-10-02.json").exists()


def test_dry_run_writes_nothing(fake, capsys):
    repo, out, calls = fake
    assert run(repo, out, "--days", "2", "--dry-run") == 0
    assert not out.exists()
    assert calls["d1"] == []
    assert "2026-10-02: " in capsys.readouterr().out


def test_out_of_window_stops_cleanly(fake, monkeypatch, capsys):
    repo, out, calls = fake
    real = pcs.fetch_day

    def fetch_day(token, day):
        if day < datetime.date(2026, 10, 1):
            raise pcs.OutOfWindow("cannot request data older than 4w3d")
        return real(token, day)

    monkeypatch.setattr(pcs, "fetch_day", fetch_day)
    assert run(repo, out, "--days", "5") == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 3 and lines[2].startswith("2026-09-30: no data") and lines[2].endswith("stopping")
    assert not (out / "2026-09-30.json").exists()


def test_date_allows_today_but_not_future(fake):
    repo, out, calls = fake
    assert run(repo, out, "--date", "2026-10-03") == 0
    assert calls["fetch"] == ["2026-10-03"]
    assert run(repo, out, "--date", "2026-10-04") == 1
