#!/usr/bin/env python3
"""
gearherd alert matcher — the cron that isn't a cron.

There is no separate scheduler. This runs inside the nightly workflow, directly
after track_prices.py, because that script has just finished working out which
prices moved and wrote them to a hand-off file. Anything that ran on its own
timer would have to re-derive that by comparing timestamps against the ledger,
which is a worse answer to a question we already have exactly.

    data/price-events.json  ->  matching confirmed subscriptions  ->  Resend

Only `direction: down` events fire. Only rows with `confirmed_at` set are ever
mailed. Addresses are read from Postgres, used, and never written anywhere
else — not to a file, not to a log line.

Usage:
    python3 alerts/match.py --events data/price-events.json
    python3 alerts/match.py --events data/price-events.json --dry-run

Needs DATABASE_URL and RESEND_API_KEY. With neither set it reports what it
would have done and exits cleanly, so the nightly stays green before the alerts
plane is provisioned.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

SITE_URL = os.environ.get("SITE_URL", "https://gearherd.com").rstrip("/")
ALERT_FROM = os.environ.get("ALERT_FROM", "gearherd <alerts@gearherd.com>")
RESEND_ENDPOINT = "https://api.resend.com/emails"

# One alert per subscription per bag per day. A brand that restages a sale
# across forty SKUs overnight should produce one email, not forty.
DEDUPE_HOURS = 24


def bag_page(bag_id):
    brand, _, model = bag_id.partition("__")
    return f"{SITE_URL}/bags/{brand}/{model}/"


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def render(event, unsub_url):
    """Plain text and HTML for one drop. Kept here rather than shared with the
    site's TypeScript because this is the only place drop emails are sent —
    the confirmation email is the API route's job and they have no overlap."""
    name = f"{event['brand']} {event['name']}"
    now = f"${event['price']:g}"
    was = f"${event['prev_price']:g}" if event.get("prev_price") else None
    page = bag_page(event["bag_id"])

    subject = f"{name} dropped to {now}" + (f" (was {was})" if was else "")
    lowest = event.get("at_lowest")

    text = "\n".join(filter(None, [
        subject,
        "the lowest price we have recorded." if lowest else None,
        "",
        f"Colourway: {event['color']}" if event.get("color") else None,
        f"Specs and price history: {page}",
        f"Buy from {event['brand']}: {event['url']}" if event.get("url") else None,
        "",
        f"Stop these: {unsub_url}",
    ]))

    html = f"""
<div style="font-family:ui-monospace,SFMono-Regular,Menlo,monospace;max-width:520px;color:#16181a">
  <p style="letter-spacing:.1em;text-transform:uppercase;font-size:11px;color:#8e938f">gearherd price drop</p>
  <h1 style="font-size:18px;margin:0 0 6px">{esc(name)}</h1>
  {f'<p style="font-size:12px;color:#5e6360;margin:0 0 12px">{esc(event["color"])}</p>' if event.get("color") else ''}
  <p style="font-size:26px;margin:0 0 4px;color:#e8430a;font-weight:600">{esc(now)}
    {f'<span style="font-size:14px;color:#8e938f;text-decoration:line-through;font-weight:400">{esc(was)}</span>' if was else ''}
  </p>
  {'<p style="font-size:12px;color:#17804a;margin:0 0 16px">Lowest price we have recorded.</p>' if lowest else ''}
  <p style="margin:20px 0">
    <a href="{esc(page)}" style="background:#e8430a;color:#fff;padding:11px 18px;text-decoration:none;font-size:13px">Specs &amp; price history</a>
  </p>
  {f'<p style="font-size:12px"><a href="{esc(event["url"])}" style="color:#5e6360">Buy from {esc(event["brand"])} &rarr;</a></p>' if event.get("url") else ''}
  <p style="font-size:11px;color:#8e938f;margin-top:26px;border-top:1px solid #d9d6cf;padding-top:12px">
    <a href="{esc(unsub_url)}" style="color:#8e938f">Unsubscribe</a> — one click, no login, deletes the watch.
  </p>
</div>""".strip()

    return subject, text, html


def send(to, subject, text, html, unsub_url, api_key):
    payload = json.dumps({
        "from": ALERT_FROM,
        "to": [to],
        "subject": subject,
        "text": text,
        "html": html,
        # RFC 8058: lets mail clients offer unsubscribe in their own chrome.
        "headers": {
            "List-Unsubscribe": f"<{unsub_url}>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        },
    }).encode()

    req = urllib.request.Request(RESEND_ENDPOINT, data=payload, headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status


def wants(criteria, event):
    """Does this subscription's criteria cover this event?"""
    if criteria.get("bag_id") and criteria["bag_id"] != event["bag_id"]:
        return False
    if (criteria.get("brand_slug")
            and criteria["brand_slug"] != event.get("brand_slug")):
        return False
    if not criteria.get("bag_id") and not criteria.get("brand_slug"):
        return False  # an empty watch is not a watch on everything
    ceiling = criteria.get("max_price")
    if ceiling is not None and (event.get("price") is None
                                or event["price"] > ceiling):
        return False
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", default="data/price-events.json")
    ap.add_argument("--dry-run", action="store_true",
                    help="match and report, but send nothing and record nothing")
    args = ap.parse_args()

    try:
        with open(args.events) as f:
            events = json.load(f).get("events", [])
    except FileNotFoundError:
        print(f"no {args.events} — nothing to match")
        return 0

    drops = [e for e in events if e.get("direction") == "down"]
    print(f"{len(events)} events, {len(drops)} price drops")
    if not drops:
        return 0

    database_url = os.environ.get("DATABASE_URL")
    api_key = os.environ.get("RESEND_API_KEY")

    if not database_url:
        # Deliberately not an error. The alerts plane is provisioned separately
        # from the data plane, and a missing subscriber database should never
        # turn the nightly crawl red.
        print("DATABASE_URL not set — skipping alerts "
              f"({len(drops)} drops would have been matched)")
        return 0

    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError:
        sys.exit("psycopg is not installed — pip install -r alerts/requirements.txt")

    bag_ids = sorted({e["bag_id"] for e in drops})
    brand_slugs = sorted({e["brand_slug"] for e in drops if e.get("brand_slug")})

    sent_count = 0
    # prepare_threshold=None disables prepared statements. psycopg3 starts
    # preparing after the fifth execution of a statement, which breaks against
    # a transaction-mode connection pooler (Supabase's port 6543) because the
    # next execution can land on a different backend. Turning them off means
    # one connection string works for both this script and the web functions.
    with psycopg.connect(database_url, row_factory=dict_row,
                         prepare_threshold=None) as conn:
        subs = conn.execute(
            """
            select id, email, criteria, unsub_token
              from subscriptions
             where confirmed_at is not null
               and (criteria->>'bag_id' = any(%s)
                    or criteria->>'brand_slug' = any(%s))
            """,
            (bag_ids, brand_slugs),
        ).fetchall()

        already = {
            (r["subscription_id"], r["bag_id"])
            for r in conn.execute(
                """
                select subscription_id, event->>'bag_id' as bag_id
                  from sent_alerts
                 where sent_at > now() - make_interval(hours => %s)
                """,
                (DEDUPE_HOURS,),
            ).fetchall()
        }

        print(f"{len(subs)} confirmed subscriptions touch tonight's drops")

        for sub in subs:
            criteria = sub["criteria"] or {}
            candidates = [e for e in drops if wants(criteria, e)]
            if not candidates:
                continue

            # One email per bag, for the steepest drop on that bag.
            by_bag = {}
            for event in candidates:
                best = by_bag.get(event["bag_id"])
                if best is None or (event.get("delta") or 0) < (best.get("delta") or 0):
                    by_bag[event["bag_id"]] = event

            for bag_id, event in by_bag.items():
                if (sub["id"], bag_id) in already:
                    continue

                unsub_url = f"{SITE_URL}/api/unsubscribe?token={sub['unsub_token']}"
                subject, text, html = render(event, unsub_url)

                if args.dry_run:
                    # Subscription id, never the address.
                    print(f"  would send to sub={sub['id']}: {subject}")
                    continue
                if not api_key:
                    print(f"  RESEND_API_KEY not set — skipping sub={sub['id']}")
                    continue

                try:
                    send(sub["email"], subject, text, html, unsub_url, api_key)
                except (urllib.error.HTTPError, urllib.error.URLError, OSError) as e:
                    # Never let the error carry the address into the log.
                    reason = getattr(e, "code", None) or type(e).__name__
                    print(f"  send failed for sub={sub['id']}: {reason}")
                    continue

                conn.execute(
                    "insert into sent_alerts (subscription_id, event) "
                    "values (%s, %s)",
                    (sub["id"], json.dumps(event)),
                )
                already.add((sub["id"], bag_id))
                sent_count += 1
                print(f"  sent to sub={sub['id']}: {subject}")

        if not args.dry_run:
            conn.commit()

    print(f"{'would send' if args.dry_run else 'sent'} {sent_count} alerts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
