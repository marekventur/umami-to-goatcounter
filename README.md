# umami-to-goatcounter

Import an [Umami](https://umami.is) analytics export into
[GoatCounter](https://www.goatcounter.com), preserving history rather than
starting from zero on the day you switch.

Umami's export is a raw per-event stream, which is the good case: every row has
a timestamp, path, title, referrer, country, screen size and visit id. GoatCounter's
`/api/v0/count` accepts all of those *and* a backdated `created_at`, so this is
a real backfill and not a daily-totals approximation.

Single file, standard library only, Python 3.9+.

```
python3 umami2goatcounter.py export.zip --list

python3 umami2goatcounter.py export.zip \
    --site https://analytics.example.com \
    --hostname example.com \
    --dry-run

GOATCOUNTER_API_KEY=... python3 umami2goatcounter.py export.zip \
    --site https://analytics.example.com \
    --hostname example.com
```

Point it at the `.zip` Umami gives you — it reads `website_event.csv` from
inside — or at an already-extracted CSV, or `-` for stdin.

## Read this before you trust the numbers

**GoatCounter will report fewer pageviews than Umami, and that is correct.**
GoatCounter counts a pageview at most once per path per session: a reload, or
bouncing between two pages and back, adds nothing. Umami counts every row.

On the 11-week export this was built against, 62,173 Umami pageviews became
36,357 in GoatCounter — exactly the number of distinct `(visit_id, url_path)`
pairs in the source. Nothing was dropped. The same traffic just reads about 42%
lower, so expect a step change in your charts on cutover day.

## What carries over

| Umami | GoatCounter | Notes |
|---|---|---|
| `created_at` | `created_at` | UTC in the export; backdating is allowed, the future is not |
| `url_path` | `path` | verbatim |
| `page_title` | `title` | verbatim |
| `url_query` | `query` | feeds campaign/UTM parsing |
| `referrer_domain` + `referrer_path` | `ref` | rebuilt into a URL |
| `country` | `location` | country only — see below |
| `screen` | `size` | width; GoatCounter ignores height and scaling |
| `visit_id` | `session` | see below |
| `event_type=2` + `event_name` | `event` + `path` | custom events |

## What does not

- **Browser and OS.** GoatCounter has no field for these; it derives them by
  parsing a `User-Agent` header, and Umami only kept the already-parsed family.
  `--user-agent synth` invents a plausible UA so the columns populate, but it is
  approximate (see below) and off by default. Your Umami history still has the
  real values.
- **Region.** GoatCounter derives the region from a real IP, not from the
  location string — passing `DE-NW` stores Germany with an empty region. The
  country survives.
- **Language.** Not a field on the count API.
- **City, `utm_*` columns, web-vitals (`lcp`/`inp`/`cls`/`fcp`/`ttfb`),
  `distinct_id`, `tag`.** No equivalent. UTM values still arrive via `query` if
  they were in the URL.

### `visit_id`, not `session_id`

Umami has both, and picking the wrong one quietly corrupts your visitor counts.
`session_id` is a long-lived *visitor* identifier — in the reference export, 11%
of them spanned more than one day. GoatCounter's session is a *visit*, so
mapping `session_id` onto it merges separate visits and undercounts. `visit_id`
matches the semantics: exactly one of 29,413 spanned a day.

Rows are sent oldest-first for the same reason. GoatCounter works out "first
visit" from the order it first sees each session, so shuffled input produces
wrong numbers.

### `--user-agent synth`

Tested against GoatCounter 2.7.0:

| Umami browser | Result |
|---|---|
| chrome, firefox, safari | correct, with an empty version |
| edge-chromium | needs an invented version number to be recognised at all |
| samsung | not recognised — lands as unknown |
| crios (Chrome on iOS) | not recognised — lands as Safari |

It puts data in your analytics that no browser ever sent. That is why it is
opt-in.

## Getting an API token

Create it in the GoatCounter **UI**, under *Settings → API tokens*, with the
*Record pageviews* permission.

Not with `goatcounter db create apitoken`: that scopes the token to the
account's own site, so it returns `403 this token does not have access to this
site` against any linked child site. The UI has an "Access to sites" picker.

## Rate limits and pacing

GoatCounter's `api-count` limit defaults to 60 requests per 2 minutes. The
default `--rate 25` (requests/minute) stays under it, and 429s are retried with
backoff, so a large import just takes a few minutes. If you self-host and want
it faster, start the server with `-ratelimit api-count:1000/1`.

Sending is the fast part. GoatCounter persists from an in-memory buffer in the
background at roughly 3,000 hits/minute, so a 60,000-row import returns in
seconds and then takes ~20 minutes to appear. It also costs the server about
45 MB of extra RSS while the backlog drains.

## Not idempotent

Running twice imports twice — GoatCounter has no key to deduplicate on. Use
`--dry-run` first, and `--since`/`--until` to resume or to avoid overlapping
with data your live tracking script has already collected.

## Options

```
--site URL              GoatCounter site, e.g. https://analytics.example.com
--token TOKEN           or $GOATCOUNTER_API_KEY
--hostname HOST         only import this hostname (repeatable)
--exclude-hostname HOST skip this hostname (repeatable) — e.g. a dev domain
--since / --until       UTC bounds, compared against the raw created_at text
--user-agent none|synth default none
--tz-offset HOURS       if your export is not UTC
--batch-size N          default 500
--rate N                requests/minute, default 25; 0 disables pacing
--list                  list hostnames in the export and exit
--dry-run               summarise and show a sample payload, send nothing
```

`--list` is worth running first. Umami exports are per-website but still tend to
contain stray hostnames — a dev subdomain, or a bare IP — that you do not want
in production stats.

## Licence

MIT.
