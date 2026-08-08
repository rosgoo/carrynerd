#!/usr/bin/env python3
"""Assemble the affiliate-scan note from data/affiliate-programs.json."""
import json, collections, os

SRC = "/Users/rosgoo/Dev/gearherd/data/affiliate-programs.json"
OUT = "/Users/rosgoo/Notes/carrynerd/affiliate-scan-results.md"

rows = json.load(open(SRC))["brands"]

# brands.json and brands-master.json both contribute rows, so a brand can appear
# twice (www vs bare, or two live domains). Keep the most informative verdict.
RANK = {"in-house": 0, "page-only": 1, "network": 2, "none-found": 3,
        "unreachable": 4}
best = {}
dupes = collections.Counter()
for r in rows:
    k = r["brand"].strip().lower()
    dupes[k] += 1
    rank = RANK[r["verdict"].split(":")[0]]
    if k not in best or rank < RANK[best[k]["verdict"].split(":")[0]]:
        best[k] = r
brands = sorted(best.values(), key=lambda r: -r["models"])
dup_names = sorted(n for n, c in dupes.items() if c > 1)

total = sum(r["models"] for r in brands) or 1
kinds = collections.Counter(r["verdict"].split(":")[0] for r in brands)
models = {k: sum(r["models"] for r in brands
                 if r["verdict"].split(":")[0] == k) for k in RANK}
vendors = collections.Counter(r["vendor"] for r in brands if r["vendor"])
networks = collections.Counter(r["network"] for r in brands if r["network"])
reachable = models["in-house"] + models["page-only"]


def esc(s):
    return (s or "").replace("|", "\\|")


def table(kind, cols_url=True):
    sel = [r for r in brands if r["verdict"].split(":")[0] == kind]
    head = "| Models | Brand | Domain | " + (
        "Vendor" if kind == "in-house" else
        "Network" if kind == "network" else "Platform") + " | Programme page |"
    sep = "| -----: | ----- | ------ | ------ | ------ |"
    if not cols_url:
        head = "| Models | Brand | Domain | Platform |"
        sep = "| -----: | ----- | ------ | -------- |"
    out = [head, sep]
    for r in sel:
        third = (r["vendor"] if kind == "in-house" else
                 r["network"] if kind == "network" else r["platform"])
        if cols_url:
            url = r["programme_url"] or "—"
            if url != "—":
                url = f"[link]({url})"
            out.append(f"| {r['models']} | {esc(r['brand'])} | "
                       f"`{r['domain']}` | {third or '—'} | {url} |")
        else:
            out.append(f"| {r['models']} | {esc(r['brand'])} | "
                       f"`{r['domain']}` | {r['platform']} |")
    return "\n".join(out)


doc = f"""# carrynerd — affiliate programme scan

_Written 2026-08-08. Results of `affiliate_scan.py` run across every brand with
a resolved domain. Companion to_ _[affiliate-approval](affiliate-approval.md)_
_— that doc is how the gates work, this is which gate each brand is actually
behind. Every verdict here was **measured**, and the evidence that produced it
is recorded in `data/affiliate-programs.json`. What was **not** measured is
stated in "Known limits" at the end, which is the more important half._

***

## Headline

**{100 * reachable / total:.1f}% of the indexed catalogue is reachable at zero traffic**, through
{kinds['in-house'] + kinds['page-only']} brands running their own programmes rather than a network's.
Sovrn and Skimlinks reach none of them.

| Verdict | Brands | Models | Share of catalogue |
| ------- | -----: | -----: | -----------------: |
| **in-house** — apply today, full rate | {kinds['in-house']} | {models['in-house']:,} | {100 * models['in-house'] / total:.1f}% |
| **page-only** — programme exists, vendor unidentified | {kinds['page-only']} | {models['page-only']:,} | {100 * models['page-only'] / total:.1f}% |
| **network** — gated behind a network's bar | {kinds['network']} | {models['network']:,} | {100 * models['network'] / total:.1f}% |
| **none-found** — no signal either way | {kinds['none-found']} | {models['none-found']:,} | {100 * models['none-found'] / total:.1f}% |
| **unreachable** — blocked or timed out | {kinds['unreachable']} | {models['unreachable']:,} | {100 * models['unreachable'] / total:.1f}% |

{len(brands)} unique brands, {len(rows)} rows scanned.

### The finding that matters most

**AvantLink holds {networks['avantlink']} of the {sum(networks.values())}
detected network memberships — {100 * networks['avantlink'] / sum(networks.values()):.0f}%.**

That is the strictest gate of the four: ~30% acceptance, ~6 months of site age
and ~10k sessions/month in its own guidance, and **denials recorded on file**.
The brands you cannot have yet are concentrated behind precisely the one door
that only opens once.

This settles the sequencing question. AvantLink is not an application to spend
early and retry — it is the last one to file, after the traffic exists. Meanwhile
the {kinds['in-house']} in-house brands need no traffic at all.

Detected networks: {', '.join(f'{k} ×{v}' for k, v in networks.most_common())}.
Detected in-house vendors: {', '.join(f'{k} ×{v}' for k, v in vendors.most_common())}.

***

## 1. In-house — apply today ({kinds['in-house']} brands, {models['in-house']:,} models)

Runs its own programme from a storefront app. **No traffic gate, full
commission, no sub-affiliate haircut, and a direct relationship from day one** —
this is the "re-paper the top brands direct" work in
_[monetisation](monetisation.md)_ available now rather than in 18 months.

A blank programme page means the vendor's script was found on the storefront but
no public signup page was linked; the application route is the app's own portal
or a direct email.

{table('in-house')}

***

## 2. Page-only — apply today, after a look ({kinds['page-only']} brands, {models['page-only']:,} models)

A real programme page, no vendor fingerprint. Some are genuine affiliate
schemes, some are **ambassador programmes that pay in product rather than
commission** — the scan cannot tell those apart, so each needs thirty seconds of
human reading before applying. Treated as apply-now because none showed a
network handoff — which is weaker than proof that none exists.

{table('page-only')}

***

## 3. Network — gated ({kinds['network']} brands, {models['network']:,} models)

Detected either from network tracking on the storefront or from the programme
page naming the network. **The traffic bar applies here**, and it differs
sharply by network: Impact/ShareASale/Awin/Pepperjam are roughly month-3 and
1–5k sessions, AvantLink is month-6 and ~10k.

{table('network')}

***

## 4. None-found ({kinds['none-found']} brands, {models['none-found']:,} models)

**This is the largest bucket and the least conclusive.** It means no evidence
was found, not that no programme exists — see the limits below. Several of these
are large brands that almost certainly run programmes; they simply do not link
them from the homepage or ship network tracking on it.

{table('none-found', cols_url=False)}

***

## 5. Unreachable ({kinds['unreachable']} brands, {models['unreachable']:,} models)

Blocked, timed out, or refused the crawler. **This is a retry bucket, not a
finding** — the first run and the corrected run disagreed on which brands landed
here, so membership is partly transient. Most are large brands with aggressive
bot protection.

{table('unreachable', cols_url=False)}

***

## What this changes

1. **The apply-now list is {kinds['in-house'] + kinds['page-only']} brands, and it exists today.** Ranked by
   models, so working down §1 and §2 in order monetises the largest share of the
   catalogue per application filed.
2. **Do not touch AvantLink yet.** It holds {networks['avantlink']} brands
   including EVERGOODS, GORUCK, Topo Designs, Chrome Industries, Mission
   Workshop and Tortuga. One premature application spends the whole set.
3. **Sovrn/Skimlinks is still worth shipping**, but now with a known ceiling: it
   can only reach some part of the {kinds['network']} network brands, and
   structurally none of the {kinds['in-house']} in-house ones.
4. **The none-found bucket is the next research task**, not a dead end — it is
   {100 * models['none-found'] / total:.0f}% of the catalogue and the scan's
   weakest evidence.

***

## Known limits

Stated plainly, because the table above reads more confident than it is.

- **`none-found` is absence of evidence.** Network membership leaves almost no
  trace on a brand's own storefront unless it ships the network's tracking or
  links its programme page. Dakine (597 models), Db Journey (364), Away (184)
  and Bellroy (148) are all in this bucket and all plausibly have programmes.
  The reliable way to close it is the merchant directories inside each network,
  which needs an account first.
- **The marker table is seeded, not measured.** The vendor fingerprints were
  written from knowledge of the apps, not derived from this catalogue. A miss is
  a gap in the table, not proof of absence. `--unknown-markers` dumps
  unrecognised third-party hosts to grow it from evidence.
- **The first run undercounted networks badly** — it checked network markers
  only on programme pages, never on homepages, filing brands with Awin tracking
  in plain sight as `none-found`. Fixed, and networks went from 22 brands to
  {kinds['network']}. Assume comparable gaps remain.
- **Ambassador ≠ affiliate.** Several §2 and §3 matches are ambassador or
  athlete-sponsorship pages, which often pay in product. Ortlieb's
  `/sponsoring-athletes` and Mountainsmith's `/outing-calendar` are near-certain
  false matches on the URL even where the network verdict is sound.
- **Some programme URLs are wrong even where the verdict is right.** GORUCK
  resolved to `/collections/all`. The verdict came from homepage tracking; the
  link did not.
- **Duplicate rows.** {len(rows)} rows collapsed to {len(brands)} brands; the
  better verdict was kept. Affected: {', '.join(dup_names)}.
- **Model counts are today's index**, so a brand with 0 models is researched but
  not yet crawled — worth applying to anyway if the catalogue is coming.

***

## Re-running

    python3 affiliate_scan.py --apply --unknown-markers --delay 2.5

~15 minutes for {len(rows)} brands at 2.5s spacing. Writes
`data/affiliate-programs.json` (full evidence) and `data/affiliate-todo.csv`
(the worklist, with blank `applied` and `status` columns to fill in by hand).
Deliberately never requests `/products.json`, so it is safe to run during a
Shopify IP block.
"""

os.makedirs(os.path.dirname(OUT), exist_ok=True)
with open(OUT, "w") as f:
    f.write(doc)
print(f"wrote {OUT} ({len(doc):,} chars, {len(brands)} brands)")
