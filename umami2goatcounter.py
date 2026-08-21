#!/usr/bin/env python3
"""Import an Umami analytics export into GoatCounter.

    umami2goatcounter.py export.zip --list
    umami2goatcounter.py export.zip --site https://stats.example.com --dry-run
    umami2goatcounter.py export.zip --site https://stats.example.com --hostname example.com

Umami's export is a raw per-event stream, so this is a real backfill rather than
a daily-totals approximation: each row carries a timestamp, path, title,
referrer, country, screen size and visit id, and GoatCounter's /api/v0/count
accepts all of those plus a backdated created_at.

Two things worth knowing before you read the results, both covered at length in
README.md:

  * GoatCounter will report far fewer pageviews than Umami for the same
    traffic. It counts a pageview at most once per path per session; Umami
    counted every row. This is a definition, not data loss.

  * Browser and OS cannot be carried over faithfully. GoatCounter derives them
    by parsing a User-Agent header, which Umami did not keep. --user-agent synth
    reconstructs an approximate one; it is off by default.

No third-party dependencies.
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
from datetime import datetime, timedelta, timezone

__version__ = "1.0.0"

# Umami's website_event.event_type values.
CUSTOM_EVENT = "2"

# Platform tokens for --user-agent synth, keyed by Umami's `os` value.
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

# Browser families, in the form GoatCounter's parser actually recognises. The
# version is left off wherever the parser tolerates it, so that we report an
# empty version rather than inventing a number no browser ever sent. Edge is the
# exception: without a version it is not recognised as Edge at all.
_UA_TEMPLATE = {
    "chrome":  "Mozilla/5.0 ({p}) " + _WEBKIT + " Chrome/ Safari/537.36",
    "firefox": "Mozilla/5.0 ({p}; rv:120.0) Gecko/20100101 Firefox/",
    "safari":  "Mozilla/5.0 ({p}) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/ Safari/605.1.15",
    "edge":    "Mozilla/5.0 ({p}) " + _WEBKIT + " Chrome/120.0.0.0 Safari/537.36 Edge/120.0",
}
_UA_FAMILY = {
    "chrome": "chrome", "chromium": "chrome", "crios": "chrome",
    "samsung": "chrome", "opera": "chrome", "yandexbrowser": "chrome",
    "firefox": "firefox", "fxios": "firefox",
    "safari": "safari", "ios": "safari", "ios-webview": "safari",
    "edge-chromium": "edge", "edge": "edge", "edge-ios": "edge",
}


def synth_user_agent(browser, os_name):
    plat = _OS_TOKEN.get(os_name, "X11; Linux x86_64")
    family = _UA_FAMILY.get((browser or "").lower(), "chrome")
    return _UA_TEMPLATE[family].format(p=plat)


def read_events(path):
    """Yield rows from an Umami export .zip, a bare website_event.csv, or stdin."""
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


def parse_ts(value):
    """Parse Umami's naive-UTC timestamp, e.g. '2026-08-13 10:33:49'."""
    return datetime.strptime(value[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S") \
                   .replace(tzinfo=timezone.utc)


def parse_bound(value, flag):
    """Parse a --since/--until bound; a bare date means midnight UTC."""
    text = value.strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    sys.exit(f"{flag}: cannot parse {value!r}; use YYYY-MM-DD or YYYY-MM-DD HH:MM:SS")


def build_hit(row, user_agent_mode, tz_offset, now):
    """Map one Umami row to a GoatCounter hit, or return (None, reason)."""
    try:
        created = parse_ts(row["created_at"]) - timedelta(hours=tz_offset)
    except (KeyError, ValueError):
        return None, "unparseable created_at"
    # The API rejects future timestamps outright.
    if created > now:
        return None, "timestamp in the future"

    is_event = row.get("event_type") == CUSTOM_EVENT
    path = (row.get("event_name") if is_event else row.get("url_path")) or ""
    if not path:
        return None, "no event_name" if is_event else "no url_path"

    hit = {"path": path, "created_at": created.strftime("%Y-%m-%dT%H:%M:%SZ")}
    if is_event:
        hit["event"] = True
    if row.get("page_title"):
        hit["title"] = row["page_title"]
    if row.get("url_query"):
        hit["query"] = row["url_query"]
    if row.get("visit_id"):
        # visit_id, not session_id — see README.
        hit["session"] = row["visit_id"]
    if row.get("country"):
        # Country only; GoatCounter derives the region from a real IP.
        hit["location"] = row["country"]

    if row.get("referrer_domain"):
        ref = "https://" + row["referrer_domain"] + (row.get("referrer_path") or "")
        if row.get("referrer_query"):
            ref += "?" + row["referrer_query"]
        hit["ref"] = ref

    # GoatCounter's Floats type unmarshals from *text*, so this must be the
    # string "1536" and not [1536]. Only the width is used; the API documents
    # height and scaling as accepted-but-ignored.
    screen = row.get("screen") or ""
    if "x" in screen:
        width = screen.split("x", 1)[0]
        if width.isdigit():
            hit["size"] = width

    if user_agent_mode == "synth":
        hit["user_agent"] = synth_user_agent(row.get("browser"), row.get("os"))

    return hit, None


class Sender:
    def __init__(self, site, token, retries=5):
        self.url = site.rstrip("/") + "/api/v0/count"
        self.token = token
        self.retries = retries

    def send(self, hits):
        # GoatCounter rejects a hit that has neither a session nor a
        # user-agent+IP, unless no_sessions says to skip session tracking for
        # it. Umami rows normally carry visit_id, so this only kicks in for an
        # export missing that column.
        payload = {"hits": hits,
                   "no_sessions": not all(h.get("session") for h in hits)}
        body = json.dumps(payload).encode()
        for attempt in range(self.retries):
            req = urllib.request.Request(
                self.url, data=body,
                headers={"Content-Type": "application/json",
                         "Authorization": "Bearer " + self.token})
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    return r.status
            except urllib.error.HTTPError as e:
                last = e
                # 429 is the api-count ratelimit.
                if e.code == 429 and attempt < self.retries - 1:
                    wait = int(e.headers.get("Retry-After") or 2 ** (attempt + 1))
                    print(f"\n  ratelimited, waiting {wait}s", file=sys.stderr)
                    time.sleep(wait)
                    continue
                detail = e.read().decode("utf-8", "replace")[:500]
                raise SendError(f"HTTP {e.code}: {detail}")
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                # Transient connection trouble; worth another go.
                last = e
                if attempt < self.retries - 1:
                    wait = 2 ** (attempt + 1)
                    print(f"\n  {e}; retrying in {wait}s", file=sys.stderr)
                    time.sleep(wait)
                    continue
                raise SendError(str(e))
        raise SendError(f"gave up after {self.retries} attempts: {last}")


class SendError(Exception):
    pass


def main():
    p = argparse.ArgumentParser(
        description="Import an Umami analytics export into GoatCounter.",
        epilog="Not idempotent: running twice imports twice. Use --dry-run first.")
    p.add_argument("export", help="Umami export .zip, a website_event.csv, or - for stdin")
    p.add_argument("--site", help="GoatCounter site URL, e.g. https://stats.example.com")
    p.add_argument("--token", default=os.environ.get("GOATCOUNTER_API_KEY"),
                   help="API token with 'Record pageviews'; defaults to $GOATCOUNTER_API_KEY")
    p.add_argument("--hostname", action="append", default=[], metavar="HOST",
                   help="only import rows with this hostname (repeatable)")
    p.add_argument("--exclude-hostname", action="append", default=[], metavar="HOST",
                   help="skip rows with this hostname (repeatable)")
    p.add_argument("--since", metavar="WHEN",
                   help="only rows at or after this UTC time (YYYY-MM-DD[ HH:MM:SS])")
    p.add_argument("--until", metavar="WHEN", help="only rows strictly before this UTC time")
    p.add_argument("--user-agent", choices=("none", "synth"), default="none",
                   help="'synth' invents User-Agents so browser/OS populate (approximate)")
    p.add_argument("--tz-offset", type=float, default=0.0, metavar="HOURS",
                   help="hours to subtract if the export is not in UTC")
    p.add_argument("--batch-size", type=int, default=500, metavar="N")
    p.add_argument("--rate", type=float, default=25.0, metavar="N",
                   help="max requests/minute (default 25; GoatCounter allows 30)")
    p.add_argument("--list", action="store_true",
                   help="list the hostnames in the export and exit")
    p.add_argument("--dry-run", action="store_true",
                   help="summarise and show a sample payload; send nothing")
    p.add_argument("--version", action="version", version=__version__)
    args = p.parse_args()

    rows = list(read_events(args.export))
    if not rows:
        sys.exit("no rows in export")

    if args.list:
        counts = {}
        for r in rows:
            counts[r.get("hostname", "")] = counts.get(r.get("hostname", ""), 0) + 1
        for host, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"{n:>9}  {host or '(none)'}")
        return

    if args.hostname:
        rows = [r for r in rows if r.get("hostname") in args.hostname]
    if args.exclude_hostname:
        rows = [r for r in rows if r.get("hostname") not in args.exclude_hostname]
    if args.since:
        since = parse_bound(args.since, "--since")
        rows = [r for r in rows if parse_ts(r["created_at"]) >= since]
    if args.until:
        until = parse_bound(args.until, "--until")
        rows = [r for r in rows if parse_ts(r["created_at"]) < until]
    if not rows:
        sys.exit("no rows left after filtering")

    # Oldest first: GoatCounter decides "first visit" from the order it first
    # sees each session, so shuffled input yields wrong visitor numbers.
    rows.sort(key=lambda r: r["created_at"])

    now = datetime.now(timezone.utc)
    hits, skipped = [], {}
    for row in rows:
        hit, reason = build_hit(row, args.user_agent, args.tz_offset, now)
        if hit is None:
            skipped[reason] = skipped.get(reason, 0) + 1
        else:
            hits.append(hit)
    if not hits:
        sys.exit("no importable rows")

    print(f"{len(hits)} hits ready")
    print(f"  range   : {hits[0]['created_at']} -> {hits[-1]['created_at']}")
    print(f"  paths   : {len({h['path'] for h in hits})}")
    print(f"  sessions: {len({h.get('session') for h in hits})}")
    print(f"  events  : {sum(1 for h in hits if h.get('event'))}")
    for reason, n in sorted(skipped.items()):
        print(f"  skipped : {n} ({reason})")

    batches = [hits[i:i + args.batch_size] for i in range(0, len(hits), args.batch_size)]

    if args.dry_run:
        print(f"  batches : {len(batches)} of up to {args.batch_size}")
        print("\nsample hit:\n" + json.dumps(hits[0], indent=2))
        print("\ndry run: nothing sent")
        return

    if not args.site or not args.token:
        sys.exit("--site and --token (or $GOATCOUNTER_API_KEY) are required")

    sender = Sender(args.site, args.token)
    delay = 60.0 / args.rate if args.rate > 0 else 0.0
    tty = sys.stdout.isatty()
    sent = 0
    for i, batch in enumerate(batches, 1):
        if i > 1 and delay:
            time.sleep(delay)
        try:
            sender.send(batch)
        except SendError as e:
            # Tell the user exactly where to pick up, or they have to choose
            # between importing nothing and importing some rows twice.
            print(f"\nfailed on batch {i}/{len(batches)} after {sent} hits: {e}",
                  file=sys.stderr)
            print(f"resume with:  --since '{batch[0]['created_at']}'", file=sys.stderr)
            sys.exit(1)
        sent += len(batch)
        if tty:
            print(f"\r  sent {sent}/{len(hits)} ({i}/{len(batches)} batches)",
                  end="", flush=True)
        elif i % 25 == 0 or i == len(batches):
            print(f"  sent {sent}/{len(hits)} ({i}/{len(batches)} batches)", flush=True)
    if tty:
        print()

    print(f"done: {sent} hits sent to {args.site}")
    print("GoatCounter persists from an in-memory buffer in the background, at "
          "very roughly 3,000 hits/minute, so give it a while to show up.")


if __name__ == "__main__":
    main()
