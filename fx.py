#!/usr/bin/env python3
"""Today's reference exchange rates, for display only.

    python3 fx.py            # -> data/fx.json

Eight brands in the index quote something other than dollars, and for six of
them that is the end of the story: their storefronts run no US market, so
?country=US returns the same euros, and there is no dollar figure to fetch
because the seller has never named one. See the `country` note in fetch.py for
the three where there was.

Those brands were still being sorted, filtered and ranked against dollars as
though the numbers were comparable. A GBP 140 Rab pack sorted below a $150 Aer
one and above a $130 one, which is wrong in both directions, and price-per-
litre -- the number this index is arguably for -- was dividing pounds by litres
and printing the result next to dollars per litre.

So: a rate, stated. What it is emphatically NOT is a conversion of the price.

  * The price stays what the seller charges, in what they charge it in. That is
    what the model page shows, what the ledger records, and what a price alert
    fires on. Nothing in this file touches any of them.
  * The converted figure is a second, derived number, kept beside the first and
    labelled approximate everywhere it appears. It exists so that a sort can
    put two bags in a sensible order, and so a reader can see roughly what a
    Danish price means without opening a converter in another tab.

The distinction matters most for the alerts. Storing converted prices would
make every watch fire on the euro moving rather than on the seller discounting,
and turn the price history into a chart of two variables with no way to read
which one moved. That is the failure the "never convert" rule in normalize.py
was written against, and it still holds -- for the stored price. It was never
an argument against telling a reader what the price is roughly worth.

The rates come from the European Central Bank's daily reference rates: free,
no key, no account, published each working day around 16:00 CET and stable
once published. Deliberately not a live-quoting FX API. A reference rate has a
date on it, which is the property that makes it quotable to a reader -- "ECB,
29 August" is a thing they can check, where "the rate at the moment your page
rendered" is not. Weekends and holidays reuse the last publication, and the
date carried through to the page says so.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "data", "fx.json")

ECB = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
CONTACT = os.environ.get("CARRYNERD_CONTACT", "")
UA = (f"carrynerd/0.1 (product catalog indexer; +{CONTACT})" if CONTACT
      else "carrynerd/0.1 (product catalog indexer)")

# The currencies the index actually publishes in. The ECB quotes about thirty
# and there is no cost to keeping them all, but naming these makes the file
# self-documenting about which ones matter and gives the check below something
# to fail on: a missing rate for a currency we publish is a real problem, and a
# missing rate for one we do not is not our business.
NEEDED = ("EUR", "GBP", "AUD", "DKK", "NOK", "HKD", "SEK", "CHF", "CAD",
          "JPY", "PLN", "CZK", "SGD", "NZD")


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def main():
    try:
        raw = fetch(ECB)
    except (urllib.error.URLError, OSError) as e:
        # Not fatal to the nightly. A run with no fresh rates should publish
        # yesterday's catalogue with yesterday's rate rather than fall over, so
        # this leaves any existing data/fx.json alone and lets normalize.py
        # carry on with it. The date it stamps on the page is the rate's own,
        # so a stale rate says how stale it is without anyone maintaining a
        # separate warning.
        sys.stderr.write(f"fx: could not reach the ECB ({e}); leaving "
                         f"data/fx.json as it stands\n")
        return 0

    root = ET.fromstring(raw)
    # The document nests three deep and none of the levels are named
    # distinctly: an outer Cube, a Cube carrying the date, then one Cube per
    # currency. Matched on the attributes rather than the depth, because the
    # attribute is the part that carries meaning.
    date, per_eur = None, {"EUR": 1.0}
    for cube in root.iter():
        if cube.get("time"):
            date = cube.get("time")
        cur, rate = cube.get("currency"), cube.get("rate")
        if cur and rate:
            try:
                per_eur[cur] = float(rate)
            except ValueError:
                continue

    if not date or "USD" not in per_eur:
        sys.stderr.write("fx: ECB response carried no date or no USD rate; "
                         "leaving data/fx.json as it stands\n")
        return 0

    # The ECB quotes everything against the euro and the index reckons in
    # dollars, so this is a change of base rather than a second lookup:
    # 1 USD buys per_eur[C] / per_eur[USD] units of C.
    usd = per_eur["USD"]
    rates = {c: round(v / usd, 6) for c, v in per_eur.items()}
    rates["USD"] = 1.0

    missing = [c for c in NEEDED if c not in rates]
    if missing:
        sys.stderr.write(f"fx: no ECB rate for {', '.join(missing)} — bags in "
                         f"those currencies will keep their native price and "
                         f"sort by it\n")

    payload = {
        "source": "European Central Bank euro foreign exchange reference rates",
        "url": ECB,
        # The ECB's own publication date, not ours. This is the number quoted
        # to the reader, and it is the honest one: on a Sunday the rate is
        # Friday's and saying "today" would be a small lie told daily.
        "date": date,
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "base": "USD",
        "note": ("1 USD buys this many units of each currency. Display only — "
                 "prices are stored and alerted on in what the seller charges."),
        "rates": dict(sorted(rates.items())),
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(payload, f, indent=1, sort_keys=True)
        f.write("\n")
    print(f"fx: ECB {date}, {len(rates)} currencies "
          f"(1 USD = {rates.get('EUR', 0):.4f} EUR, "
          f"{rates.get('GBP', 0):.4f} GBP) -> data/fx.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
