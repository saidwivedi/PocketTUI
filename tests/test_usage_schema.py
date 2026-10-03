"""migrations/, the D1 schema for the anonymous usage counts and the daily
Cloudflare analytics copy.

D1 is SQLite, so the migrations are applied in order to an in-memory SQLite database: what
is checked is that it parses, creates what the Functions write to, and keeps
the partial index partial.
"""

import sqlite3
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
MIGRATIONS = sorted((REPO / "migrations").glob("*.sql"))


@pytest.fixture
def db():
    con = sqlite3.connect(":memory:")
    for m in MIGRATIONS:
        con.executescript(m.read_text(encoding="utf-8"))
    yield con
    con.close()


def names(db, kind):
    return {r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type = ? AND name NOT LIKE 'sqlite_%'", (kind,))}


def test_tables_and_indexes(db):
    assert [m.name for m in MIGRATIONS] == ["0001_usage.sql", "0002_external_daily.sql"]
    assert names(db, "table") == {"events", "installs", "fetches", "external_daily"}
    assert {"events_day", "events_install_day", "fetches_day_kind"} <= names(db, "index")
    # installs.id is a TEXT primary key, which SQLite backs with an automatic
    # index; together with the three named ones that is the four the schema has.
    auto = {r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'installs'")}
    assert len(auto) == 1
    partial = {r[1]: r[4] for r in db.execute("PRAGMA index_list(events)")}
    assert partial == {"events_day": 0, "events_install_day": 1}
    sql = db.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'events_install_day'").fetchone()[0]
    assert "WHERE install IS NOT NULL" in sql


def test_sample_rows(db):
    db.execute(
        "INSERT INTO events (ts, day, person, install, shell, os, layout, secs)"
        " VALUES (1, '2026-10-01', 'p', NULL, 'hosted', 'ios', 'phone', 30)")
    db.execute(
        "INSERT INTO events (ts, day, person, install, shell, os, layout, secs, f_voice)"
        " VALUES (2, '2026-10-01', 'p', 'abc', 'self', 'mac', 'wide', 60, 1)")
    db.execute("INSERT INTO installs (id, first_day, last_day) VALUES ('abc', '2026-10-01', '2026-10-01')")
    db.execute(
        "INSERT INTO fetches (ts, day, kind, mode, agent, ref_host)"
        " VALUES (3, '2026-10-01', 'version', NULL, 'curl', 'hosted')")
    assert db.execute("SELECT install, app, pwa, f_newsess FROM events ORDER BY ts").fetchall() == [
        (None, "", 0, 0), ("abc", "", 0, 0)]
    assert db.execute("SELECT events FROM installs").fetchone() == (0,)
    assert db.execute("SELECT country, mode FROM fetches").fetchone() == ("", None)


def test_required_columns_are_enforced(db):
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO events (ts, day, person) VALUES (1, '2026-10-01', 'p')")


def test_external_daily_upsert(db):
    up = ("INSERT INTO external_daily (day, metric, key, value, pulled_at) VALUES (?, ?, ?, ?, ?)"
          " ON CONFLICT(day, metric, key) DO UPDATE SET value = excluded.value, pulled_at = excluded.pulled_at")
    db.execute(up, ("2026-10-01", "installer_runs", "", 3, 1))
    db.execute(up, ("2026-10-01", "installer_runs", "", 5, 2))
    db.execute(up, ("2026-10-01", "tarball_country", "DE", 2, 2))
    db.execute("INSERT INTO external_daily (day, metric, value, pulled_at) VALUES ('2026-10-02', 'app_views', 1, 3)")
    assert db.execute("SELECT day, metric, key, value, pulled_at FROM external_daily ORDER BY day, metric").fetchall() == [
        ("2026-10-01", "installer_runs", "", 5.0, 2), ("2026-10-01", "tarball_country", "DE", 2.0, 2),
        ("2026-10-02", "app_views", "", 1.0, 3)]
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO external_daily (day, metric, key, value, pulled_at)"
                   " VALUES ('2026-10-01', 'installer_runs', '', 1, 4)")
