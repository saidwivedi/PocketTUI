#!/usr/bin/env python3
"""Pull Cloudflare's own analytics for pockettui.com into D1, one UTC day at a time.

Cloudflare keeps zone request data for about a month, so a daily run copies
each finished day into the external_daily table (migrations/0002) and keeps
the raw GraphQL answers on disk as <out-dir>/<day>.json. Two datasets are read:
the zone's httpRequestsAdaptiveGroups for the installer, tarball and
version.txt downloads, and the account's Web Analytics (RUM) page loads for
the landing page and /app/.

Tokens are read from the repo directory: .cloudflare_analytics_token (read
only, both GraphQL datasets) and .cloudflare_token (D1 queries). Standard
library only; runs under Python 3.8.
"""

import argparse
import datetime
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict

ZONE_TAG = "cef4d7d357915887e34c5a8a23c98b98"
ACCOUNT_TAG = "9a7a1de39162fb189ad956c6559fb28d"
SITE_TAG = "5a3a629a4c5f4a1fa4aa7a64f3df91a4"
D1_ID = "42dfe313-5db0-4b3f-bb4d-816ebf8d73d2"
GRAPHQL = "https://api.cloudflare.com/client/v4/graphql"
D1_QUERY = "https://api.cloudflare.com/client/v4/accounts/%s/d1/database/%s/query" % (ACCOUNT_TAG, D1_ID)
HOST = "pockettui.com"
ZONE_LIMIT = 5000
RUM_LIMIT = 1000
# D1 allows at most 100 bound parameters per statement; five per row.
UPSERT_CHUNK = 20

DOWNLOADS = {"/install.sh": "installer", "/pockettui.tar.gz": "tarball", "/version.txt": "version"}

ZONE_QUERY = """query($z: String!, $f: ZoneHttpRequestsAdaptiveGroupsFilter_InputObject!) {
  viewer { zones(filter: {zoneTag: $z}) {
    httpRequestsAdaptiveGroups(limit: %d, filter: $f) {
      count dimensions { clientRequestPath clientCountryName clientIP userAgent edgeResponseStatus }
    }
  } }
}""" % ZONE_LIMIT

RUM_QUERY = """query($a: String!, $land: AccountRumPageloadEventsAdaptiveGroupsFilter_InputObject!,
                    $app: AccountRumPageloadEventsAdaptiveGroupsFilter_InputObject!) {
  viewer { accounts(filter: {accountTag: $a}) {
    landing: rumPageloadEventsAdaptiveGroups(limit: %(n)d, filter: $land) { count sum { visits } }
    country: rumPageloadEventsAdaptiveGroups(limit: %(n)d, filter: $land) { count sum { visits } dimensions { countryName } }
    device: rumPageloadEventsAdaptiveGroups(limit: %(n)d, filter: $land) { count sum { visits } dimensions { deviceType } }
    referrer: rumPageloadEventsAdaptiveGroups(limit: %(n)d, filter: $land) { count sum { visits } dimensions { refererHost } }
    app: rumPageloadEventsAdaptiveGroups(limit: %(n)d, filter: $app) { count sum { visits } }
  } }
}""" % {"n": RUM_LIMIT}


class PullError(Exception):
    pass


class OutOfWindow(PullError):
    """The day is older than Cloudflare keeps, or the API has nothing for it."""


def agent_class(ua):
    """Same rule as agentClass in functions/_lib/usage.js."""
    s = ua or ""
    if s.lower().startswith(("curl/", "wget/")):
        return "curl"
    if "Mozilla/" in s:
        return "browser"
    return "other"


def metrics_from_zone(rows):
    """(metric, key, value) triples from httpRequestsAdaptiveGroups rows.

    count is already Cloudflare's sampled estimate of the request count, so it
    is summed; distinct IPs are counted over the rows given (one UTC day).
    """
    counts = defaultdict(float)
    ips = defaultdict(set)
    country_ips = defaultdict(lambda: defaultdict(set))
    for r in rows:
        d = r.get("dimensions") or {}
        kind = DOWNLOADS.get(d.get("clientRequestPath"))
        status = d.get("edgeResponseStatus") or 0
        if kind is None or not 200 <= status < 300:
            continue
        n = float(r.get("count") or 0)
        ip = d.get("clientIP") or ""
        country = d.get("clientCountryName") or "unknown"
        if kind == "installer":
            agent = agent_class(d.get("userAgent"))
            if agent == "curl":
                counts["installer_runs"] += n
                ips["installer_ips"].add(ip)
            elif agent == "browser":
                counts["installer_reads"] += n
        elif kind == "tarball":
            counts["tarball_fetches"] += n
            ips["tarball_ips"].add(ip)
            country_ips["tarball_country"][country].add(ip)
        else:
            counts["version_checks"] += n
            ips["version_ips"].add(ip)
            country_ips["version_ips_country"][country].add(ip)
    out = []
    for m in ("installer_runs", "installer_reads", "tarball_fetches", "version_checks"):
        out.append((m, "", counts[m]))
    for m in ("installer_ips", "tarball_ips", "version_ips"):
        out.append((m, "", float(len(ips[m]))))
    for m in ("tarball_country", "version_ips_country"):
        for k in sorted(country_ips[m]):
            out.append((m, k, float(len(country_ips[m][k]))))
    return out


def metrics_from_rum(acc):
    """(metric, key, value) triples from the aliased RUM groups of one day."""
    def visits(x):
        return float((x.get("sum") or {}).get("visits") or 0)

    def total(group):
        rows = acc.get(group) or []
        return sum(visits(x) for x in rows), sum(float(x.get("count") or 0) for x in rows)

    out = []
    lv, lc = total("landing")
    av, ac = total("app")
    out += [("landing_visits", "", lv), ("landing_views", "", lc),
            ("app_visits", "", av), ("app_views", "", ac)]
    # Missing keys get the same labels the console uses for the live numbers.
    for group, dim, fallback in (("country", "countryName", "unknown"),
                                 ("device", "deviceType", "unknown"),
                                 ("referrer", "refererHost", "direct")):
        by = defaultdict(float)
        for x in acc.get(group) or []:
            by[(x.get("dimensions") or {}).get(dim) or fallback] += visits(x)
        for k in sorted(by):
            out.append(("landing_visits_" + group, k, by[k]))
    return out


def upsert_sql(rows):
    """(sql, params) statements upserting (day, metric, key, value, pulled_at) rows."""
    stmts = []
    for i in range(0, len(rows), UPSERT_CHUNK):
        chunk = rows[i:i + UPSERT_CHUNK]
        sql = ("INSERT INTO external_daily (day, metric, key, value, pulled_at) VALUES "
               + ", ".join(["(?, ?, ?, ?, ?)"] * len(chunk))
               + " ON CONFLICT(day, metric, key) DO UPDATE SET"
                 " value = excluded.value, pulled_at = excluded.pulled_at")
        params = []
        for r in chunk:
            params += [r["day"], r["metric"], r["key"], r["value"], r["pulled_at"]]
        stmts.append((sql, params))
    return stmts


def post_json(url, token, body, timeout=60):
    """POST JSON, one retry after 10 s on a 5xx. Returns the decoded answer."""
    data = json.dumps(body).encode()
    for attempt in (0, 1):
        req = urllib.request.Request(url, data=data, headers={
            "Content-Type": "application/json", "Authorization": "Bearer " + token})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")[:500]
            if e.code >= 500 and attempt == 0:
                time.sleep(10)
                continue
            raise PullError("HTTP %d from %s: %s" % (e.code, url.split("/v4/")[-1], text))
        except (urllib.error.URLError, OSError) as e:
            raise PullError("request to %s failed: %s" % (url.split("/v4/")[-1], e))
    raise PullError("unreachable")


def graphql(token, query, variables):
    body = post_json(GRAPHQL, token, {"query": query, "variables": variables})
    errs = body.get("errors") or []
    if errs:
        msg = str(errs[0].get("message", "GraphQL error"))
        code = (errs[0].get("extensions") or {}).get("code")
        if code == "quota" and "older than" in msg:
            raise OutOfWindow(msg)
        raise PullError("GraphQL: " + msg[:300])
    return body


def fetch_day(token, day):
    """Both raw GraphQL answers for one UTC day."""
    start = day.isoformat() + "T00:00:00Z"
    end = (day + datetime.timedelta(days=1)).isoformat() + "T00:00:00Z"
    window = {"datetime_geq": start, "datetime_lt": end}
    zone = graphql(token, ZONE_QUERY, {"z": ZONE_TAG, "f": dict(
        window, clientRequestPath_in=sorted(DOWNLOADS))})
    rum_f = dict(window, siteTag=SITE_TAG, requestHost=HOST)
    rum = graphql(token, RUM_QUERY, {"a": ACCOUNT_TAG,
                                     "land": dict(rum_f, requestPath="/"),
                                     "app": dict(rum_f, requestPath="/app/")})
    return zone, rum


def zone_rows(zone):
    try:
        rows = zone["data"]["viewer"]["zones"][0]["httpRequestsAdaptiveGroups"]
    except (KeyError, IndexError, TypeError):
        raise PullError("zone answer has no httpRequestsAdaptiveGroups")
    if len(rows) >= ZONE_LIMIT:
        raise PullError("zone answer hit the %d-row limit; counts would be cut short" % ZONE_LIMIT)
    return rows


def rum_account(rum):
    try:
        acc = rum["data"]["viewer"]["accounts"][0]
    except (KeyError, IndexError, TypeError):
        raise PullError("RUM answer has no account")
    for k, v in acc.items():
        if len(v or []) >= RUM_LIMIT:
            raise PullError("RUM group %s hit the %d-row limit" % (k, RUM_LIMIT))
    return acc


def build_rows(day, zone, rum, pulled_at):
    zr = zone_rows(zone)
    acc = rum_account(rum)
    if not zr and not any(acc.get(k) for k in acc):
        raise OutOfWindow("both datasets returned no rows")
    triples = metrics_from_zone(zr) + metrics_from_rum(acc)
    return [{"day": day.isoformat(), "metric": m, "key": k, "value": v, "pulled_at": pulled_at}
            for m, k, v in triples]


def d1_query(token, sql, params):
    body = post_json(D1_QUERY, token, {"sql": sql, "params": params})
    if not body.get("success") or not all(r.get("success", True) for r in body.get("result") or []):
        raise PullError("D1: " + json.dumps(body.get("errors") or body)[:300])
    return body


def store(token, day, rows, pulled_at):
    for sql, params in upsert_sql(rows):
        d1_query(token, sql, params)
    # A re-pull (--force) replaces the day: keys that are gone now are dropped.
    d1_query(token, "DELETE FROM external_daily WHERE day = ? AND pulled_at < ?", [day, pulled_at])


def read_token(repo, name):
    path = os.path.join(repo, name)
    try:
        with open(path) as f:
            tok = f.read().strip()
    except OSError as e:
        raise PullError("cannot read %s: %s" % (name, e.strerror))
    if not tok:
        raise PullError("%s is empty" % name)
    return tok


def days_to_pull(args, today):
    if args.date:
        day = datetime.date.fromisoformat(args.date)
        if day > today:
            raise PullError("%s is in the future" % args.date)
        return [day]
    if args.days < 1:
        raise PullError("--days must be at least 1")
    # Newest first, so a backfill stops at the edge of Cloudflare's window.
    return [today - datetime.timedelta(days=i) for i in range(1, args.days + 1)]


def log(out_dir, msg):
    stamp = datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    with open(os.path.join(out_dir, "pull.log"), "a") as f:
        f.write("%s %s\n" % (stamp, msg))


def pull(args, today=None):
    """Returns the process exit code."""
    today = today or datetime.datetime.utcnow().date()
    out_dir = os.path.expanduser(args.out_dir)
    write = not args.dry_run
    if write:
        os.makedirs(out_dir, exist_ok=True)

    def note(msg):
        if write:
            log(out_dir, msg)

    try:
        days = days_to_pull(args, today)
        cf_token = read_token(args.repo, ".cloudflare_analytics_token")
        d1_token = read_token(args.repo, ".cloudflare_token") if write else None
    except PullError as e:
        print("error: %s" % e, file=sys.stderr)
        note("error: %s" % e)
        return 1

    for day in days:
        name = day.isoformat()
        final = os.path.join(out_dir, name + ".json")
        if args.skip_existing and not args.force and os.path.exists(final):
            print("%s: skipped (%s exists)" % (name, final))
            continue
        pulled_at = int(time.time())
        try:
            zone, rum = fetch_day(cf_token, day)
            rows = build_rows(day, zone, rum, pulled_at)
        except OutOfWindow as e:
            print("%s: no data from Cloudflare (%s); stopping" % (name, e))
            note("%s: stopped, no data (%s)" % (name, e))
            return 0
        except PullError as e:
            print("%s: error: %s" % (name, e), file=sys.stderr)
            note("%s: error: %s" % (name, e))
            return 1
        if not write:
            for r in rows:
                print("%s %-24s %-24s %g" % (name, r["metric"], r["key"] or "-", r["value"]))
            print("%s: %d rows (dry run, nothing written)" % (name, len(rows)))
            continue
        # The raw copy is kept even if D1 fails, under .partial so that
        # skip-existing does not treat a day missing from D1 as done.
        partial = final + ".partial"
        with open(partial, "w") as f:
            json.dump({"day": name, "pulled_at": pulled_at, "zone": zone, "rum": rum, "rows": rows},
                      f, indent=1, sort_keys=True)
        try:
            store(d1_token, name, rows, pulled_at)
        except PullError as e:
            print("%s: error: %s" % (name, e), file=sys.stderr)
            note("%s: D1 error: %s" % (name, e))
            return 1
        os.replace(partial, final)
        print("%s: %d rows" % (name, len(rows)))
        note("%s: %d rows" % (name, len(rows)))
    return 0


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--days", type=int, default=1,
                   help="finished UTC days to pull, counting back from yesterday (default 1)")
    p.add_argument("--date", help="pull this one UTC day (YYYY-MM-DD); today is allowed only here")
    p.add_argument("--out-dir", default="~/.pockettui-stats", help="raw JSON and pull.log directory")
    p.add_argument("--dry-run", action="store_true", help="print the rows; write nothing")
    p.add_argument("--skip-existing", action="store_true", default=True,
                   help="skip a day whose <day>.json exists (the default)")
    p.add_argument("--force", action="store_true", help="pull days even if their JSON exists")
    p.add_argument("--repo", default=os.path.dirname(os.path.abspath(__file__)),
                   help="directory holding the token files (default: this script's)")
    return p.parse_args(argv)


if __name__ == "__main__":
    sys.exit(pull(parse_args()))
