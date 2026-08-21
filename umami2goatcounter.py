#!/usr/bin/env python3
"""Import an Umami analytics export into GoatCounter.

Umami's export is a raw per-event stream, which is the good case: it carries a
timestamp, path, title, referrer, country, screen size and visit id per row, and
GoatCounter's /api/v0/count accepts all of those including a backdated
created_at. So a real backfill is possible, not just a daily-totals fudge.

    python3 umami2goatcounter.py export.zip \
        --site https://analytics.example.com --hostname example.com --dry-run

Stdlib only, so it runs anywhere Python 3.9+ does.

WHAT SURVIVES THE TRIP

    Umami column          GoatCounter field   Notes
    --------------------  ------------------  ------------------------------
    created_at            created_at          UTC in the export; backdating
                                              is allowed (never the future)
    url_path              path                verbatim
    page_title            title               verbatim
    url_query             query               feeds campaign/UTM parsing
    referrer_domain/path  ref                 rebuilt into a URL
    country               location            country only, see below
    screen "393x851"      size                width; GoatCounter ignores the
                                              height and scaling anyway
    visit_id              session             see "visit_id, not session_id"
    event_type 2          event=true          path becomes event_name

WHAT DOES NOT

    browser, os           Only reachable by inventing a User-Agent string for
                          GoatCounter to re-parse; --user-agent synth does that,
                          imperfectly. Off by default. See below.
    region                GoatCounter derives the region from a real IP, not
                          from the location string: passing "DE-NW" stores
                          Germany with an empty region. Country survives.
    language              Not a field on the count API at all.
    city, utm_* columns,  No equivalent. UTM values still arrive via `query`
    lcp/inp/cls/fcp/ttfb  if they were in the URL.
    distinct_id, tag

VISIT_ID, NOT SESSION_ID

    Umami has both. session_id is a long-lived visitor identifier — in a real
    11-week export, ~11% of them spanned more than one day. GoatCounter's
    session is a *visit*, so mapping session_id onto it merges separate visits
    into one and undercounts. visit_id is the per-visit id and matches the
    semantics; in that same export exactly one of 29,413 spanned a day.

    GoatCounter derives "first visit" itself from the session id it is given,
    which is why rows are sent oldest-first: out-of-order rows would attribute
    the first visit to whichever hit happened to arrive first.

SYNTHETIC USER-AGENTS (--user-agent synth)

    GoatCounter stores browser and OS by parsing a User-Agent header; there is
    no way to set them directly. Umami only kept the parsed family ("chrome",
    "Windows 10"), so reconstructing them means building a UA string and hoping
    it re-parses to the same thing. Tested against GoatCounter 2.7.0:

        Chrome, Firefox, Safari    survive, with an empty version, by leaving
                                   the version off the UA string
        Edge                       needs a version number to be recognised at
                                   all, so one is invented
        Samsung Internet           not recognised; lands as unknown
        Chrome on iOS (crios)      not recognised; lands as Safari

    So it is approximate, and it puts data in your analytics that no browser
    ever sent. Default is off, which leaves browser and OS empty for the
    imported window — honest, and your Umami history still has the real thing.

GOATCOUNTER WILL REPORT FEWER PAGEVIEWS THAN UMAMI

    This is not data loss, it is a different definition, and it surprises
    everyone once. GoatCounter counts a pageview at most once per path per
    session — a reload, or bouncing between two pages and back, adds nothing.
    Umami counts every event row.

    Verified on an 11-week export of 62,173 Umami pageviews: GoatCounter stored
    36,357, which is exactly the number of distinct (visit_id, url_path) pairs
    in the source. Nothing was dropped; the same traffic simply reads ~42%
    lower. Expect a step change in your charts on the day you cut over.

NOT IDEMPOTENT

    Running twice imports twice; GoatCounter has no key to deduplicate on. Use
    --since/--until to pick up where you left off, and --dry-run first.
"""

import argparse
import csv
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone

# Umami's event_type values.
PAGEVIEW, CUSTOM_EVENT = "1", "2"

# UA templates per Umami browser family. The version is deliberately left off
# where GoatCounter's parser tolerates it, so we don't invent version numbers;
# see the module docstring for which families that works for.
_OS_TOKEN = {
    "Windows 10": "Windows NT 10.0; Win64; x64",
    "Windows 11": "Windows NT 10.0; Win64; x64",
    "Windows 7": "Windows NT 6.1; Win64; x64",
    "Mac OS": "Macintosh; Intel Mac OS X 10_15_7",
    "Linux": "X11; Linux x86_64",
    "Ubuntu": "X11; Ubuntu; Linux x86_64",
    "Chrome OS": "X11; CrOS x86_64",
    "Android OS": "Linux; Android 13",
    "iOS": "iPhone; CPU iPhone OS 17_0 like Mac OS X",
}
_WEBKIT = "AppleWebKit/537.36 (KHTML, like Gecko)"


def synth_user_agent(browser: str, os_name: str) -> str:
    plat = _OS_TOKEN.get(os_name, "X11; Linux x86_64")
    b = (browser or "").lower()
    if b in ("chrome", "chromium", "crios", "samsung", "opera", "yandexbrowser"):
        return f"Mozilla/5.0 ({plat}) {_WEBKIT} Chrome/ Safari/537.36"
    if b in ("firefox", "fxios"):
        return f"Mozilla/5.0 ({plat}; rv:120.0) Gecko/20100101 Firefox/"
    if b in ("safari", "ios", "ios-webview"):
        return (f"Mozilla/5.0 ({plat}) AppleWebKit/605.1.15 (KHTML, like Gecko) "
                f"Version/ Safari/605.1.15")
    if b in ("edge-chromium", "edge", "edge-ios"):
        # Edge is the one family that is dropped entirely without a version.
        return f"Mozilla/5.0 ({plat}) {_WEBKIT} Chrome/120.0.0.0 Safari/537.36 Edge/120.0"
    return f"Mozilla/5.0 ({plat}) {_WEBKIT} Chrome/ Safari/537.36"


def read_events(path):
    """Yield dict rows from an Umami export .zip or a bare website_event.csv."""
    if path == "-":
        yield from csv.DictReader(sys.stdin)
        return
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            names = [n for n in z.namelist() if n.endswith("website_event.csv")]
            if not names:
                sys.exit(f"{path}: no website_event.csv inside the zip")
            with z.open(names[0]) as fh:
                yield from csv.DictReader(io.TextIOWrapper(fh, "utf-8"))
        return
    with open(path, newline="", encoding="utf-8") as fh:
        yield from csv.DictReader(fh)


def parse_ts(value, offset_hours):
    """Umami writes naive UTC, e.g. '2026-08-13 10:33:49'."""
    dt = datetime.strptime(value[:19], "%Y-%m-%d %H:%M:%S")
    dt = dt.replace(tzinfo=timezone.utc)
    if offset_hours:
        dt = dt.fromtimestamp(dt.timestamp() - offset_hours * 3600, timezone.utc)
    return dt


def build_hit(row, args, now):
    created = parse_ts(row["created_at"], args.tz_offset)
    # The API rejects future timestamps outright, which is easy to hit if the
    # export was taken on a machine whose clock ran ahead.
    if created > now:
        return None

    is_event = row.get("event_type") == CUSTOM_EVENT
    path = row.get("event_name") if is_event else row.get("url_path")
    if not path:
        return None

    hit = {"path": path, "created_at": created.strftime("%Y-%m-%dT%H:%M:%SZ")}

    if is_event:
        hit["event"] = True
    if row.get("page_title"):
        hit["title"] = row["page_title"]
    if row.get("url_query"):
        hit["query"] = row["url_query"]
    if row.get("visit_id"):
        hit["session"] = row["visit_id"]
    if row.get("country"):
        hit["location"] = row["country"]

    dom = row.get("referrer_domain")
    if dom:
        ref = "https://" + dom + (row.get("referrer_path") or "")
        if row.get("referrer_query"):
            ref += "?" + row["referrer_query"]
        hit["ref"] = ref

    # GoatCounter's Floats type unmarshals from *text*, not a JSON array, so
    # this has to be the string "1536" and not [1536]. Only the width is kept;
    # the API documents height and scaling as accepted-but-unused.
    screen = row.get("screen") or ""
    if "x" in screen:
        width = screen.split("x")[0]
        if width.isdigit():
            hit["size"] = width

    if args.user_agent == "synth":
        hit["user_agent"] = synth_user_agent(row.get("browser"), row.get("os"))

    return hit


def send(url, token, hits, retries=5):
    body = json.dumps({"no_sessions": False, "hits": hits}).encode()
    req = urllib.request.Request(
        url.rstrip("/") + "/api/v0/count", data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + token})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status
        except urllib.error.HTTPError as e:
            # 429 is the api-count ratelimit; back off and retry rather than
            # dropping a batch on the floor.
            if e.code == 429 and attempt < retries - 1:
                wait = int(e.headers.get("Retry-After") or 2 ** (attempt + 1))
                print(f"  ratelimited, waiting {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            sys.exit(f"HTTP {e.code}: {e.read().decode()[:500]}")
    return None


def main():
    p = argparse.ArgumentParser(
        description="Import an Umami export into GoatCounter.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("export", help="Umami export .zip, a website_event.csv, or -")
    p.add_argument("--site", help="GoatCounter site URL, e.g. https://analytics.example.com")
    p.add_argument("--token", default=os.environ.get("GOATCOUNTER_API_KEY"),
                   help="API token with 'Record pageviews'; or $GOATCOUNTER_API_KEY")
    p.add_argument("--hostname", action="append", default=[],
                   help="only import rows with this hostname (repeatable)")
    p.add_argument("--exclude-hostname", action="append", default=[],
                   help="skip rows with this hostname (repeatable), e.g. a dev domain")
    p.add_argument("--since", help="only rows at or after this UTC date/time")
    p.add_argument("--until", help="only rows strictly before this UTC date/time")
    p.add_argument("--user-agent", choices=("none", "synth"), default="none",
                   help="'synth' invents User-Agents so browser/OS are populated; "
                        "approximate, see --help output header")
    p.add_argument("--tz-offset", type=float, default=0.0,
                   help="hours to subtract if the export is not UTC")
    p.add_argument("--batch-size", type=int, default=500)
    p.add_argument("--rate", type=float, default=25.0,
                   help="max requests/minute; GoatCounter's default limit is 30/min")
    p.add_argument("--list", action="store_true",
                   help="just list the hostnames in the export and exit")
    p.add_argument("--dry-run", action="store_true",
                   help="summarise and print one sample payload; send nothing")
    args = p.parse_args()

    rows = list(read_events(args.export))
    if not rows:
        sys.exit("no rows in export")

    if args.list:
        seen = {}
        for r in rows:
            seen[r["hostname"]] = seen.get(r["hostname"], 0) + 1
        for host, n in sorted(seen.items(), key=lambda kv: -kv[1]):
            print(f"{n:>9}  {host}")
        return

    if args.hostname:
        rows = [r for r in rows if r["hostname"] in args.hostname]
    if args.exclude_hostname:
        rows = [r for r in rows if r["hostname"] not in args.exclude_hostname]
    if args.since:
        rows = [r for r in rows if r["created_at"] >= args.since]
    if args.until:
        rows = [r for r in rows if r["created_at"] < args.until]
    if not rows:
        sys.exit("no rows left after filtering")

    # Oldest first: GoatCounter works out first-visit from the order it sees
    # sessions in, so shuffled input produces wrong unique-visitor numbers.
    rows.sort(key=lambda r: r["created_at"])

    now = datetime.now(timezone.utc)
    hits = [h for h in (build_hit(r, args, now) for r in rows) if h]
    skipped = len(rows) - len(hits)

    print(f"{len(hits)} hits ready"
          f"{f' ({skipped} rows skipped)' if skipped else ''}")
    print(f"  range   : {hits[0]['created_at']} -> {hits[-1]['created_at']}")
    print(f"  paths   : {len({h['path'] for h in hits})}")
    print(f"  sessions: {len({h.get('session') for h in hits})}")
    print(f"  events  : {sum(1 for h in hits if h.get('event'))}")

    batches = [hits[i:i + args.batch_size] for i in range(0, len(hits), args.batch_size)]
    if args.dry_run:
        print(f"  batches : {len(batches)} of up to {args.batch_size}")
        print("\nsample hit:\n" + json.dumps(hits[0], indent=2))
        print("\ndry run: nothing sent")
        return

    if not args.site or not args.token:
        sys.exit("--site and --token (or $GOATCOUNTER_API_KEY) are required")

    delay = 60.0 / args.rate if args.rate > 0 else 0.0
    sent = 0
    for i, batch in enumerate(batches, 1):
        if i > 1 and delay:
            time.sleep(delay)
        send(args.site, args.token, batch)
        sent += len(batch)
        if sys.stdout.isatty():
            print(f"\r  sent {sent}/{len(hits)} ({i}/{len(batches)} batches)",
                  end="", flush=True)
        elif i % 25 == 0 or i == len(batches):
            print(f"  sent {sent}/{len(hits)} ({i}/{len(batches)} batches)", flush=True)
    if sys.stdout.isatty():
        print()
    print(f"done: {sent} hits sent to {args.site}")
    print("GoatCounter persists in the background; expect roughly a minute per "
          "3,000 hits before the dashboard catches up.")


if __name__ == "__main__":
    main()
