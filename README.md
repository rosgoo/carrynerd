# bagdex

A searchable, comparable index of bags — normalised across brands from public
product feeds. Three Python scripts and a static front end, no dependencies and
no build step.

The premise: every existing bag resource is one person hand-maintaining a
spreadsheet, which caps coverage at a few hundred models and misses colourways,
current pricing and stock entirely. This pipeline gets that layer automatically.

```
brands.json ──> fetch.py ──> raw/*.json ──> normalize.py ──> data/bags.json
                                                  │                 ▲
                                              enrich.py ────────────┘
                                          (product pages, fills dims)
```

## Run it

```bash
python3 fetch.py                # pull catalogues       (~1 request/brand)
python3 normalize.py            # build data/bags.json
python3 enrich.py               # fill dimensions       (~1 request/product)
python3 -m http.server 8731     # then open /site/
```

Order matters: `normalize.py` rebuilds `data/bags.json` from `raw/`, so run
`enrich.py` after it. Enrichment caches every page it reads in
`data/enrich-cache.json`, so re-running after a fresh normalize is instant and
costs no requests.

**Run this from a residential connection.** Shopify's edge rate-limits
`/products.json` per client IP and throttles datacenter ranges almost
immediately (`429`, `local_rate_limited`, `Retry-After: 60`). On a cloud host
use `--delay 65`; from home the 2s default is fine.

Set a real contact URL before running at any scale — it goes in the
User-Agent so store operators can reach you:

```bash
export BAGDEX_CONTACT="https://yoursite.com/bot"
```

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

`site/` is vanilla JS against `data/bags.json`. Search, faceted filters
(category, volume, price, weight, linear dimension, features, material, brand,
stock, sale), grid and dense-table views, side-by-side comparison of up to six
bags with differing rows highlighted and best-in-column marked, and a detail
drawer listing every colourway with its own SKU, price and stock.

All filter state syncs to the URL, so any view is a shareable link. Press `/`
to search, `Esc` to close panels.

Range filters exclude unknowns rather than treating them as zero: a bag with no
measured volume cannot be asserted to sit inside a volume window.

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
  from JavaScript return nothing and need per-brand adapters.
- Category classification is keyword-based. `data/bags.json` `meta.rejected`
  counts what was dropped as unclassified so it can be audited.
- **Feature and material detection is weak on feed-only text.** Both are
  matched against `body_html`, which for brands like Aer is three sentences of
  marketing copy with no feature vocabulary in it, so those bags land with
  empty feature lists. The fix is cheap and known: `enrich.py` already has the
  full product page in hand: run `detect()` over that text and cache the
  result alongside dimensions. Until then, treat an empty feature list as
  "unknown", not "absent" — and note the filters currently treat it as absent,
  which will wrongly exclude bags.
- Prices are whatever the feed said when fetched, USD, no history yet. Storing
  each fetch instead of overwriting would give price history nearly free.
- No airline carry-on matrix yet. The `linear_cm` field and the two size
  presets are the groundwork for it.
