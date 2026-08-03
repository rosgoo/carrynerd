-- gearherd alerts — the only runtime database in the system.
--
-- Email addresses are the one piece of state that cannot live in git: the data
-- plane is public and fully version-controlled, and an address committed once
-- is in the history forever. So addresses live here, in Postgres, and nowhere
-- else. Nothing in data/ ever holds one, and the crawl scripts never see one.
--
-- Plain Postgres, nothing vendor-specific. Written against Supabase; runs
-- unchanged on Neon, RDS or a local instance.
--
-- Apply with:
--     psql "$DATABASE_URL" -f alerts/schema.sql
-- It is idempotent; re-running it is safe.

create table if not exists subscriptions (
  id            bigint generated always as identity primary key,
  -- The only copy of the address we hold.
  email         text        not null,
  -- What is being watched: {"bag_id": "...", "brand_slug": "...",
  -- "max_price": 199}. Any subset; an empty object watches everything.
  criteria      jsonb       not null default '{}'::jsonb,
  -- Null until double opt-in completes. Nothing is ever sent to a null row.
  confirmed_at  timestamptz,
  confirm_token text        not null unique,
  -- One-click unsubscribe, no login. Deleting the row is the unsubscribe.
  unsub_token   text        not null unique,
  created_at    timestamptz not null default now()
);

-- One watch per address per criteria. Makes a repeat signup an idempotent
-- upsert rather than a way to make us mail somebody twice.
create unique index if not exists subscriptions_email_criteria
  on subscriptions (lower(email), md5(criteria::text));

-- The matcher's access pattern: confirmed rows watching this bag or brand.
create index if not exists subscriptions_bag
  on subscriptions ((criteria->>'bag_id')) where confirmed_at is not null;
create index if not exists subscriptions_brand
  on subscriptions ((criteria->>'brand_slug')) where confirmed_at is not null;

create table if not exists sent_alerts (
  id              bigint generated always as identity primary key,
  -- Cascade rather than dangle: unsubscribing should take the send log with
  -- it. The log only exists to stop us mailing the same drop twice, and once
  -- the subscription is gone there is nothing left to deduplicate against.
  subscription_id bigint      not null
                  references subscriptions(id) on delete cascade,
  -- The price-drop event that fired, verbatim.
  event           jsonb       not null,
  sent_at         timestamptz not null default now()
);

create index if not exists sent_alerts_lookup
  on sent_alerts (subscription_id, sent_at desc);

-- Dedupe key: one alert per subscription per bag per day. A brand that
-- restages a sale across forty SKUs overnight should produce one email, not
-- forty.
create index if not exists sent_alerts_dedupe
  on sent_alerts (subscription_id, (event->>'bag_id'), sent_at desc);

-- Referral tracking.
--
-- Three event names, written by /api/click from the browser. The whole point is
-- the ratio between the first two: buy_click over product_view is the referral
-- rate, per brand, per bag or per placement depending on how it is grouped.
--
-- 'brand_click' — a reader leaving for a brand's own site rather than for a
-- product — is deliberately a third name and not a buy_click placement. It is
-- an outbound click worth counting, but it is not an attempt to buy anything,
-- and adding it to the numerator would raise the referral rate without a single
-- extra sale behind it. Every rate query below filters on buy_click explicitly
-- for that reason.
--
-- This is the second reason the alerts Postgres exists, and it holds nothing
-- personal. No address, no IP, no cookie, no session — nothing here identifies
-- a reader, only which bag was looked at and which seller was left for. That
-- is a deliberate ceiling, not an oversight: it means the table needs no
-- retention policy and no mention in the privacy page beyond what it already
-- says. Counting is enough to price the index; knowing who did the counting is
-- not worth the disclosure.
--
-- Typed columns rather than the jsonb that sent_alerts uses, because every
-- query against this table is an aggregate and the shape is fixed by the two
-- senders in scripts/analytics.js.
create table if not exists events (
  id         bigint generated always as identity primary key,
  name       text        not null
             check (name in ('product_view', 'buy_click', 'brand_click')),
  -- Widest useful cut, and the finest: brand groups the report, bag_id joins a
  -- view to the click it produced.
  brand      text,
  bag_id     text,
  -- Which surface: 'product_page', 'offers_table' or 'browse_overlay'. Clicks
  -- arrive from all three, views from only two, so the rate is only comparable
  -- within a surface — see the note in scripts/analytics.js.
  placement  text,
  -- Destination host for a buy_click, null for a view. Today this is always
  -- the brand's own store; it stops being redundant the day retailer offers
  -- land in the table.
  seller     text,
  model      text,
  price      numeric(10, 2),
  created_at timestamptz not null default now()
);

-- The report: counts of both names grouped by brand over a window.
create index if not exists events_rate
  on events (name, brand, created_at desc);

-- Per-bag drilldown, and the join that turns two counts into one rate.
create index if not exists events_bag
  on events (bag_id, name);
