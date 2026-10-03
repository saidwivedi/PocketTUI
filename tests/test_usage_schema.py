"""migrations/0001_usage.sql, the D1 schema for the anonymous usage counts.

D1 is SQLite, so the migration is applied to an in-memory SQLite database: what
is checked is that it parses, creates what the Functions write to, and keeps
the partial index partial.
"""

import sqlite3
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
MIGRATION = REPO / "migrations" / "0001_usage.sql"


@pytest.fixture
def db():
    con = sqlite3.connect(":memory:")
    con.executescript(MIGRATION.read_text(encoding="utf-8"))
    yield con
    con.close()


def names(db, kind):
    return {r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type = ? AND name NOT LIKE 'sqlite_%'", (kind,))}


def test_tables_and_indexes(db):
    assert names(db, "table") == {"events", "installs", "fetches"}
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
