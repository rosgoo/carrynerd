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
