CREATE TABLE events (
  id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, day TEXT NOT NULL,
  person TEXT NOT NULL, install TEXT,
  app TEXT NOT NULL DEFAULT '', srv TEXT NOT NULL DEFAULT '',
  shell TEXT NOT NULL, os TEXT NOT NULL, layout TEXT NOT NULL, pwa INTEGER NOT NULL DEFAULT 0,
  country TEXT NOT NULL DEFAULT '',
  secs INTEGER NOT NULL, rc INTEGER NOT NULL DEFAULT 0, seen INTEGER NOT NULL DEFAULT 0,
  f_explorer INTEGER NOT NULL DEFAULT 0, f_browser INTEGER NOT NULL DEFAULT 0,
  f_diff INTEGER NOT NULL DEFAULT 0, f_side2 INTEGER NOT NULL DEFAULT 0,
  f_voice INTEGER NOT NULL DEFAULT 0, f_settings INTEGER NOT NULL DEFAULT 0,
  f_reader INTEGER NOT NULL DEFAULT 0, f_editor INTEGER NOT NULL DEFAULT 0,
  f_viewer INTEGER NOT NULL DEFAULT 0, f_search INTEGER NOT NULL DEFAULT 0,
  f_newsess INTEGER NOT NULL DEFAULT 0);
CREATE INDEX events_day ON events(day);
CREATE INDEX events_install_day ON events(install, day) WHERE install IS NOT NULL;
CREATE TABLE installs (id TEXT PRIMARY KEY, first_day TEXT NOT NULL, last_day TEXT NOT NULL,
  events INTEGER NOT NULL DEFAULT 0);
CREATE TABLE fetches (
  id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, day TEXT NOT NULL,
  kind TEXT NOT NULL,            -- install_sh | tarball | version
  mode TEXT,                     -- install | update | NULL
  agent TEXT NOT NULL,           -- curl | browser | other
  ref_host TEXT,                 -- version only: 'hosted', 32-hex HMAC, or NULL
  country TEXT NOT NULL DEFAULT '');
CREATE INDEX fetches_day_kind ON fetches(day, kind);
