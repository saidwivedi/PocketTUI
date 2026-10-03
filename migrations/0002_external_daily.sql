-- Daily rollups of Cloudflare's own analytics for pockettui.com, pulled by
-- pull_cf_stats.py once a day so they outlive Cloudflare's ~30-day retention.
-- One row per (UTC day, metric, key); key is '' for a plain total, else the
-- country, device or referrer it splits by.
--
-- Metrics:
--   landing_visits, landing_views          Web Analytics, https://pockettui.com/
--   landing_visits_country|device|referrer  same, split by key
--   app_visits, app_views                  Web Analytics, https://pockettui.com/app/
--   installer_runs, installer_ips          /install.sh 2xx fetched by curl/wget
--   installer_reads                        /install.sh 2xx fetched by a browser
--   tarball_fetches, tarball_ips           /pockettui.tar.gz 2xx
--   tarball_country                        distinct tarball IPs, key = country
--   version_checks, version_ips            /version.txt 2xx
--   version_ips_country                    distinct version.txt IPs, key = country
--
-- The zone numbers (installer_*, tarball_*, version_*) come from
-- httpRequestsAdaptiveGroups, which is sampled on this plan: the request
-- counts are Cloudflare's estimates, not exact. The *_ips and *_country counts
-- are distinct client IPs within one UTC day, so summing them over days counts
-- an IP once per day it appeared.
CREATE TABLE external_daily (
  day TEXT NOT NULL, metric TEXT NOT NULL, key TEXT NOT NULL DEFAULT '',
  value REAL NOT NULL, pulled_at INTEGER NOT NULL,
  PRIMARY KEY (day, metric, key));
