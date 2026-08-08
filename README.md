# carrynerd

A searchable, comparable index of bags — normalised across brands from public
product feeds.

The premise: every existing bag resource is one person hand-maintaining a
spreadsheet, which caps coverage at a few hundred models and misses colourways,
current pricing and stock entirely. This pipeline gets that layer automatically.

## Three planes, only one of which runs

| Plane | What | Runs on |
|---|---|---|
| Data | crawl → normalize → JSONL ledger, committed to git | GitHub Actions, nightly |
| Serving | static site generated from that data, one HTML page per model | Vercel |
| Alerts | `POST /api/watch` + a matcher in the nightly | Supabase + Resend |

**Flat files are the source of truth; everything else is derived.** The JSONL
price ledger diffs cleanly, so git is the database history, the backup and the
audit log for free. The catalog needs no runtime database at all — the only one
in the system holds email addresses, because those are the one piece of state
that cannot live in a public repo.

```
brands.json ──> fetch.py ──> raw/*.json ──> normalize.py ──> data/bags.json
                                                 │                 ▲   │
                                             enrich.py ────────────┘   │
                                         (product pages, fills dims)   │
                                                                       ▼
                                                              track_prices.py
                                                                       │
                                       ┌───────────────────────────────┤
                                       ▼                               ▼
                        data/price-history.jsonl              data/price-state.json
                          (append-only ledger)                  (last known state)
                                       │
                    ┌──────────────────┴──────────────────┐
                    ▼                                     ▼
              Astro build                          alerts/match.py
        (one page per model, static)           (Supabase + Resend)
```

## Run it

```bash
python3 fetch.py                # pull catalogues       (~3 requests/brand)
python3 fetch.py --collections  # category ground truth (~10 requests/brand)
python3 normalize.py            # build data/bags.json
python3 enrich.py               # fill dimensions       (~1 request/product)
python3 image_bg.py             # plate colours         (~1 thumbnail/image)
python3 validate.py             # quality gate — exits 1 on a bad parse

npm install
npm run dev                     # http://localhost:4321
```

`--collections` reads the store's own shelving and only needs re-running when a
brand restructures its site, not nightly.

Order matters: `normalize.py` rebuilds `data/bags.json` from `raw/`, so run
`enrich.py` after it. Enrichment caches every page it reads in
`data/enrich-cache.json`, so re-running after a fresh normalize is instant and
costs no requests. `image_bg.py` works the same way against
`data/image-bg.json`, keyed by image URL, and also rewrites `data/bags.json` —
so it goes after enrich, not before.

The crawl scripts are stdlib-only and stay that way — they have to run
anywhere. `npm` is for the site; the two optional stages have one package each,
`alerts/requirements.txt` for the alert matcher and
`requirements-image-bg.txt` for the plate sampler, which needs an image decoder
the standard library does not have. Skip it and every plate stays white.

## Quality gate

`validate.py` runs after the pipeline and before the nightly commit, because
the commit deploys. It checks two things:

- **Structure**, absolutely: required fields, unique permalinks (a collision
  means two models generate the same page and one silently wins), dimensions
  that parse, values inside physically plausible bounds.
- **Regression**, against the last committed `data/bags.json` — which is the
  copy currently live. Bag and SKU counts must not fall more than 25%, no
  brand may vanish, and no coverage percentage may drop more than 10 points.
- **Per brand**, because the aggregate is the wrong granularity for the failure
  that actually happens. The catalogue is unevenly distributed — aer has 88
  bags, able-carry has 8 — so a paging bug costing aer 40% of its models moves
  the total by 7%, against a 25% tolerance, and nothing fires. Freshness does
  not fire either: the fetch *succeeded*, it just came back short. A brand
  falling more than 25% warns, more than 40% (`--max-brand-drop`) fails.

A single implausible value is a warning; a lot of them at once is a failure,
because that is the difference between a brand publishing a packed-box weight
and a parser reading millimetres as centimetres. A legitimate large change —
a merge fix, a stricter classifier — needs `--max-drop` and a human deciding
it is correct.

Rejected products are quarantined in `data/rejected.json` with their titles,
types and URLs rather than reduced to a count, so the classifier can be
audited by reading a file instead of re-deriving the evidence.

## Nightly crawl

`.github/workflows/nightly.yml` runs the pipeline and commits the diff. Three
jobs: a sharded price/stock pass over `/products.json`, then a single assemble
job that normalizes, tracks prices — banking the ledger immediately, before
anything slow can lose it — crawls an enrichment batch, matches alerts and
commits, then a reporting job that builds the crawl history. Pushing the
commit is what triggers the site rebuild.

Sharding splits the brand *list* across runners so the pass finishes inside one
run. Each shard is an ordinary polite client — paced, honouring `Retry-After`,
never retrying a block another shard hit. Grow the matrix as the brand list
grows; never in response to a 429.

**The enrichment backfill drains through the nightly**, ~800 pages a night.
The old warning here — that a backfill this size belongs on a home machine —
was calibrated for `/products.json`, which throttles datacenter IPs on sight.
Product pages are ordinary CDN-fronted HTML: interleaved across every brand
with work left, a store sees ~10 requests a night, minutes apart, and runners
get clean 200s. `enrich.py` honours robots.txt per store — a disallowed path
is never fetched, a declared `Crawl-delay` paces that store — and the nightly
fails loudly if a night contributes nothing while the backlog is non-empty.

**Run this from a residential connection.** Shopify's edge rate-limits
`/products.json` per client IP and throttles datacenter ranges almost
immediately (`429`, `local_rate_limited`, `Retry-After: 60`). On a cloud host
use `--delay 65`; from home the 2s default is fine.

Set a real contact URL before running at any scale — it goes in the
User-Agent so store operators can reach you:

```bash
export CARRYNERD_CONTACT="https://yoursite.com/bot"
```

### Crawl history

The gate catches a brand falling off a cliff. It cannot catch one shedding 3% a
night, because every step is small and each night's baseline is the previous
night's already-diminished count — so the decline never trips anything and the
baseline follows it all the way down. Three percent a night is 46% gone in three
weeks.

Seeing that needs the whole series, and the series already exists: the data repo
has been committing `data/fetch-log.json` every night. `scripts/crawl_history.py`
walks those commits into a SQLite table of one row per brand per night, then
reports any brand sitting below its own 21-night median. It reports and never
blocks — a slow signal that can turn a build red is one that gets muted.

```bash
python3 scripts/crawl_history.py --repo ../carrynerd-data   # local checkout
python3 scripts/crawl_history.py --out crawl-history.db    # clones DATA_REPO
```

The nightly runs it as a third job and uploads the database as a build artifact.
It is not committed anywhere: it derives entirely from commits already in the
data repo, so it rebuilds in seconds, and a binary blob git cannot delta would
cost more storage than the history it summarises.

Not `git-history`, which is the obvious tool for this and is why the script
exists. Its current release (0.6.1) looks the file up among the blobs at the
*root* of each commit tree and compares them against the full relative path, so
any path containing a directory matches nothing. It exits 0 and writes a
database with no rows in it. Every file in this project lives under `data/`. The
0.7a0 alpha fixes it, but pinning a 2022 pre-release to get a tool whose stable
version silently reports success on an empty result is a bad trade for the ~70
lines actually needed — and `sqlite3` is stdlib.

## Where the data comes from

**`fetch.py`** — the storefront JSON feed Shopify publishes at
`/products.json`. One request returns up to 250 products with every variant:
SKU, colourway, price, `compare_at_price`, and live stock. This is the layer
no existing bag directory has, and it is a public endpoint rather than scraped
HTML. Checks `robots.txt` first and skips stores that disallow the path.

**`enrich.py`** — brands keep dimensions and laptop fit in metafields the feed
does not expose, so stage two reads each product page once for its
schema.org `Product` block and its spec table. This is the expensive stage;
it is cached and resumable.

**`image_bg.py`** — brands all shoot on white, but not the same white: Aer's
house background is `#f2f2f2`, which draws a visible grey rectangle on a `#fff`
plate. This stage samples the four corners of each photo and, when they agree,
records the colour so the plate can match it. It reads a 96px CDN render rather
than the full image, so the whole catalog costs a few megabytes. Photos it
cannot read confidently — cut-out PNGs, lifestyle shots, anything too dark for
the chips to stay legible on — keep the white plate and the CSS edge feather
that covers the seam either way.

## What normalisation actually has to fix

Most of `normalize.py` exists because no two brands describe a bag the same way.

- **Dimensions** arrive as `52 × 33 × 23 cm`, as `20.5" x 13" x 9"`, or
  per-axis as `Length: 21.5" (54.5 cm)  Width: 13.5" (34 cm)` — Aer uses the
  last one. All three are parsed and converted to centimetres, largest axis
  first, since brands disagree about ordering.
- **Volume** hides in the title (`Travel Pack 45L`), in a variant option
  (`21L`), or in prose. Title wins, then option, then description.
- **Weight** — Shopify's `grams` is a *shipping* field. Aer reports 4536 g for
  the 35L Travel Pack, which is exactly 10 lb of packed box against a real
  1.77 kg. A weight stated on the product page overrides it.
- **Model duplication** — Able Carry publishes one Shopify product per
  colourway, so the Stash Pouch appears thirteen times. `merge_models()`
  collapses those into one model with thirteen colourways, stripping trailing
  colour words from titles but deliberately *not* fabric names, because a
  Travel Pack in X-Pac really is a different bag from the nylon one.

Every extracted value stores a sibling `*_source` recording where it came from
(`shopify:grams`, `product-page:labelled`, `title`, …). Fields that could not
be established are `null` and render as `—`. Nothing is estimated — a directory
that is confidently wrong is worse than one that admits a gap, and the coverage
meter in the sidebar shows exactly how complete each field is.

## Front end

Astro, in `src/`. Monetisation is affiliate-first, so the business model is
organic search traffic, and a client-rendered SPA is the worst possible shape
for that. So:

- **A real HTML page per model** at `/bags/<brand>/<model>/` — spec table with
  provenance on every value, colourways with their own SKUs, price history, and
  schema.org `Product` markup. (Ironic, but carrynerd is the site actually filling
  in `width`/`height`/`depth`, which is why nobody's `Product` block is worth
  reading.)
- **Brand pages** at `/brands/<brand>/`, and a sitemap, so every model page has
  a crawl path that does not depend on running JavaScript.
- **The browse UI is a client-side island** with faceted filtering, grid/table
  views, six-way comparison and a detail drawer — but the filtering itself runs
  on the server, at `GET /api/browse`. The island holds the state, serialises it
  into the same query string the address bar shows, and paints a page of results
  at a time; matching, sorting, the facet counts and the totals are all computed
  where the catalogue lives. `?boot=1` returns the rail, the slider bounds and
  the coverage meter alongside the first page, so a shared filtered link costs
  one request. `GET /api/catalog?ids=…` remains as the by-id lookup.
  The reason is that no single URL may return the whole catalogue: the enriched
  specs are the work, and a page cap (60 by default, 100 hard) means acquiring
  them takes ~135 enumerated requests instead of one download — which is
  something a WAF rate limit can see and throttle. The cost is a round trip per
  filter change, paid deliberately, and softened by an hour of `s-maxage` on
  every answer.
- The homepage server-renders a plain list of every model underneath the
  island, so a crawler with no JS still finds all of them.

All filter state syncs to the URL, so any view is a shareable link. Press `/`
to search, `Esc` to close panels.

Range filters exclude unknowns rather than treating them as zero: a bag with no
measured volume cannot be asserted to sit inside a volume window.

## Alerts

The only real infrastructure, and it is still small: a handful of endpoints and
a matcher that runs inside the nightly workflow right after `track_prices.py` —
which has just worked out exactly which prices moved, so no separate scheduler
needs to exist.

```
POST /api/watch            → pending row + double opt-in email
GET  /api/confirm          → sets confirmed_at; nothing is sent before this
GET  /api/unsubscribe      → deletes the row outright
POST /api/webhooks/resend  → what became of the mail we sent
```

Schema in `alerts/schema.sql`, matcher in `alerts/match.py`. Needs
`DATABASE_URL` and `RESEND_API_KEY`; with neither set the matcher
reports what it would have done and exits clean, so the nightly stays green
before the plane is provisioned.

**Addresses live in Postgres and only there.** Never in the repo — the data plane
is public and fully version-controlled, and an address committed once is in the
history forever. Never in logs either; subscription IDs are the identifier
everywhere. Unsubscribe deletes rather than flags, and `sent_alerts` cascades
with it. Resend keeps its own delivery log, as every provider does, which means
the vendor list is also the disclosure list.

### Delivery tracking

Sending is only half of it. Until a message's fate comes back, `sent_alerts`
records intent and nothing records outcome — so a nightly that mails five
hundred addresses and bounces four hundred of them looks exactly like a healthy
one. Three tables and one webhook close that:

| Table | Answers |
|---|---|
| `email_sends` | which message id was which kind of mail, to whom |
| `email_events` | what Resend later said about that message id |
| `suppressions` | which mailboxes must never be written to again |

Both senders — `src/lib/email.ts` for confirmations, `alerts/match.py` for drop
alerts — now keep the message id Resend returns, and check the suppression list
before spending a send. `/api/webhooks/resend` verifies Svix signatures, records
every event idempotently by delivery id, and writes a suppression on a hard
bounce or a spam complaint. A *transient* bounce (full mailbox, greylisting)
deliberately does not suppress; the address is fine.

**The suppression list holds digests, not addresses.** It has to outlive the
subscription row, because a hard bounce is a fact about a mailbox rather than
about a watch — but unsubscribe promises the address is gone, not moved. A
`sha256(lower(trim(address)))` keeps both promises: it can answer whether a
given address is suppressed without being able to enumerate which are. The two
senders must agree on that hash or a bounce recorded by one is invisible to the
other, so it is stated once in each and cross-referenced.

`/internal/email/` reports the rates, over messages *sent* rather than
delivered — a denominator that shrinks when things go wrong flatters exactly
the run you most need to see. It flags the thresholds mailbox providers
actually filter on (0.3% complaints, 2% hard bounces) rather than ones invented
here, and calls out the case where sends are climbing and no events are
arriving at all, which is what an unconfigured webhook looks like from the
inside.

Set `RESEND_WEBHOOK_SECRET` and point a Resend webhook at
`/api/webhooks/resend`, subscribed to at least `email.delivered`,
`email.bounced` and `email.complained`. With the secret unset the route rejects
every request rather than accepting unsigned ones. `email.opened` and
`email.clicked` are recorded too if open/click tracking is enabled on the Resend
side; nothing depends on them.

## Adding brands

Append to `brands.json`. Non-Shopify stores are recorded in
`data/fetch-log.json` as `http_404` and skipped — Bellroy and Tom Bihn are both
custom platforms and need their own adapters.

## Legal posture

Deliberate choices, not incidental ones:

- Only endpoints published for anyone. No login, no accounts, no CAPTCHA
  solving, no proxy rotation to evade blocks. The last one matters most —
  circumventing a technological barrier is what turns "reading public data"
  into a losing legal position.
- `robots.txt` is honoured, requests are paced and identify themselves, and
  `Retry-After` is respected.
- Only facts are extracted — dimensions, weight, price, materials. Marketing
  copy is not copied and images are referenced at source, not rehosted.
- EU/UK stores carry an extra consideration: the sui generis database right
  protects substantial extraction from a database someone invested in
  building. Bulk-lifting a curated dataset is a different act from reading a
  store's own product feed.

Affiliate product feeds (Impact, AWIN, CJ, Rakuten, ShareASale) are the better
source for anything at scale — the same fields, contractually licensed, with
image rights and a revenue model attached. Treat this pipeline as coverage for
brands that have no affiliate programme.

## Known gaps

- Dimension coverage depends on enrichment finishing; brands that render specs
  from JavaScript return nothing and need per-brand adapters. `meta.enrich_gaps`
  in `data/bags.json` splits this per brand into `js-rendered` (the page has no
  spec text at all — only an adapter will help) and `unparsed` (the text is
  there and the regexes missed it — much cheaper to fix). Able Carry is the
  clearest `js-rendered` case: its product pages contain no dimension string
  anywhere in the HTML.
- **The page corpus is paid for and not yet used.** `--cache-pages` keeps the
  compressed HTML (gitignored — it holds brands' full marketing copy — and
  persisted only in the Actions cache), so a parser fix costs
  `enrich.py --reparse` and zero requests. But nothing runs `--reparse`
  automatically, and the pages live only on GitHub's runners, so cache
  entries written by an older parser stay stale until someone re-parses or
  `--refresh`es them. Once in the cache, a product is never revisited — even
  when the parse found nothing.
- Category classification is keyword-first, falling back to the store's own
  collections (`fetch.py --collections`) where the title carries no category
  word — WANDRD's entire PRVKE line, for instance. Collections also supply the
  negative signal that keeps zipper pullers and camera cubes out of the index.
  `meta.rejected` breaks the drops down by reason so they can be audited, and
  `category_source` on each bag records which path classified it.
- Prices are whatever the feed said when fetched, USD. History is recorded from
  the first `track_prices.py` run forward and cannot be backfilled, so the
  charts stay thin for a while.
- **The alerts plane has never run in production.** The schema, the matcher and
  the delivery webhook have been exercised end to end against a local Postgres
  16 — schema applied twice for idempotency, `match.py` run against seeded
  subscriptions with a stubbed Resend, the webhook driven with real Svix
  signatures — but nothing has yet run against the hosted database with live
  mail. `DATABASE_URL`, `RESEND_API_KEY` and `RESEND_WEBHOOK_SECRET` all need
  setting, the sending domain needs verifying in Resend, and
  `alerts/match.py --dry-run` wants a pass against real rows before the first
  night that can actually send.
- `/api/watch` has no rate limiting beyond a unique constraint on
  (address, criteria) and a 15-minute floor on resending a confirmation. That
  bounds the damage but does not stop a determined sender from burning the
  Resend quota.
- `/api/catalog` caps a request at 100 ids, which makes copying the catalogue a
  crawl rather than a download — but nothing in the code counts requests across
  time. Rate limiting and BotID are configured in the Vercel dashboard, not
  here, and until they are set up the cap is the only thing standing in the way.
- No airline carry-on matrix yet. The `linear_cm` field and the two size
  presets are the groundwork for it.
