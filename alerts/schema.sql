-- carrynerd alerts — the only runtime database in the system.
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
  -- When the confirmation mail was last *successfully handed to Resend*, which
  -- is a different fact from when the row appeared and has to be stored
  -- separately from it. /api/watch suppresses a repeat confirmation inside a
  -- short window, and it used to read created_at for that — so a row whose
  -- send then failed was indistinguishable from one whose send had just
  -- succeeded, and the reader's immediate retry was suppressed by a mail that
  -- never went. Null means "nothing has been sent for this row yet", which is
  -- exactly the state a failed send has to leave behind.
  confirm_sent_at timestamptz,
  created_at    timestamptz not null default now()
);

-- Existing databases: the table above is only created when absent, so the
-- column has to be added separately for one that predates it. Null is the
-- correct value for every existing row — it means the resend window has
-- lapsed, which for a row created before this column existed it has.
alter table subscriptions add column if not exists confirm_sent_at timestamptz;

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

-- ── Delivery tracking ───────────────────────────────────────────────────────
--
-- Everything above records what we *decided* to send. These three record what
-- became of it, which is a different question and the one that costs money to
-- get wrong: a young sending domain that keeps mailing dead addresses and
-- collecting spam complaints gets throttled, and until these tables existed
-- nothing in the system could have noticed it happening.

create table if not exists email_sends (
  id              bigint generated always as identity primary key,
  -- Resend's message id. The only join key between a send of ours and the
  -- webhook that reports on it, which is why it is captured at the call site
  -- in both senders rather than discarded as it used to be.
  provider_id     text unique,
  -- 'confirm' is the double opt-in mail from /api/watch, 'alert' a price drop
  -- from alerts/match.py. Kept in one table because every question worth
  -- asking of it — bounce rate, complaint rate, volume — is asked of both, and
  -- a confirm that hard-bounces is the more interesting of the two: it is the
  -- reason a subscription sits pending forever with nothing to say why.
  kind            text        not null check (kind in ('confirm', 'alert')),
  -- Cascades, like sent_alerts, because the privacy page promises unsubscribe
  -- takes the record of what we sent with it. Late events for a deleted
  -- subscriber are not lost by this: email_events keys off the provider id and
  -- holds no foreign key, and suppression keys off a digest of the address, so
  -- a bounce arriving after an unsubscribe is still recorded and still honoured.
  subscription_id bigint      not null
                  references subscriptions(id) on delete cascade,
  sent_at         timestamptz not null default now()
);

create index if not exists email_sends_kind on email_sends (kind, sent_at desc);

create table if not exists email_events (
  id           bigint generated always as identity primary key,
  -- Svix's delivery id, which is stable across retries of the same event. The
  -- unique constraint plus `on conflict do nothing` is the whole idempotency
  -- story: Resend retries a webhook that timed out, and without this a slow
  -- response would inflate every count on the dashboard.
  svix_id      text unique,
  -- Deliberately not a foreign key to email_sends. Events arrive for messages
  -- whose send row has been cascaded away by an unsubscribe, and dropping
  -- those would silently understate exactly the rates this table exists to
  -- measure.
  provider_id  text,
  -- Resend's own name, stored verbatim: email.sent, email.delivered,
  -- email.delivery_delayed, email.bounced, email.complained, email.opened,
  -- email.clicked. Not an enum — a provider that adds a type should show up as
  -- an unfamiliar row on the dashboard, not as a rejected webhook.
  type         text        not null,
  -- When it happened, as against when we heard: a delayed bounce separates
  -- these by minutes and sometimes hours.
  occurred_at  timestamptz,
  received_at  timestamptz not null default now(),
  -- The payload with the recipient removed — see redact() in the webhook
  -- route. Resend echoes `to` in every event, and an address stored here would
  -- be an address living outside `subscriptions`, where no unsubscribe would
  -- ever reach it.
  payload      jsonb
);

create index if not exists email_events_provider on email_events (provider_id);
create index if not exists email_events_type on email_events (type, received_at desc);

create table if not exists suppressions (
  -- sha256(lower(trim(address))), and never the address itself.
  --
  -- This table has to outlive the subscription row, because a hard bounce is a
  -- fact about a mailbox rather than about a watch: someone who bounces, is
  -- deleted, and signs up again must not start the cycle over. But unsubscribe
  -- promises the address is *gone*, not moved to another table. A digest keeps
  -- both promises at once — it answers "is this address suppressed" without
  -- being able to answer "which addresses are".
  --
  -- Unpeppered on purpose. The digest defends the deletion promise, not the
  -- database: anyone who can read this table can already read the plaintext
  -- addresses in `subscriptions` next to it, so a pepper would buy nothing and
  -- add a shared secret that both senders must hold and neither may ever lose.
  email_sha256 text        primary key,
  reason       text        not null check (reason in ('bounce', 'complaint', 'manual')),
  detail       text,
  created_at   timestamptz not null default now()
);

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
  -- Which surface: 'product_page', 'offers_table', 'variant_row' or
  -- 'browse_overlay'. Clicks arrive from all four, views from only two, so the
  -- rate is only comparable within a surface — see the note in
  -- scripts/analytics.js.
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

-- Brand requests.
--
-- The one thing a reader can tell us that the crawl cannot work out for itself:
-- which maker is missing. Written by /api/request-brand from three buttons —
-- the Brand filter, the top of /brands/, and the footer — and read back on
-- /internal/requests/, which is the whole point of storing it rather than
-- mailing it: a brand asked for once is an anecdote, and a brand asked for
-- forty times is the next thing to crawl. That count only exists if the
-- requests land in a table.
create table if not exists brand_requests (
  -- What they typed. Deliberately not slugified and not matched against the
  -- catalog: they are naming a brand we do not have, so there is nothing to
  -- match it against, and mangling it would lose the spelling that identifies
  -- it. Reconciling "Tom Bihn" with "tombihn" is the reviewer's job.
  id         bigint generated always as identity primary key,
  name       text        not null,
  -- Where it sells, if they knew. Usually the fastest route from a request to
  -- a crawl, since fetch.py needs a storefront and not a name.
  url        text,
  -- Optional, and not a subscription. It exists so a request can be answered,
  -- and it goes through none of the double opt-in machinery above because it
  -- is not a mailing list: see the note in src/pages/api/request-brand.ts.
  email      text,
  -- sha256 of the UTC date and the requester's IP — see requesterHash() in
  -- src/lib/db.ts. Two jobs, both of which need to tell requesters apart and
  -- neither of which needs to know who they are: the daily cap, and the dedupe
  -- index below. Because the date is inside the digest, the value stops
  -- matching the same person at UTC midnight, which is what keeps this from
  -- being a durable identifier for anybody.
  ip_hash    text        not null,
  created_at timestamptz not null default now()
);

-- One row per requester per brand per day, and ip_hash first so the same index
-- also answers "how many has this requester filed today", which is the daily
-- cap. Asking twice is not two votes; two *people* asking is the signal.
create unique index if not exists brand_requests_once
  on brand_requests (ip_hash, lower(btrim(name)));

-- The queue, newest first.
create index if not exists brand_requests_recent
  on brand_requests (created_at desc);

-- Signup throttle.
--
-- /api/watch is the one unauthenticated endpoint that spends money and
-- reputation on being called: every new watch is a confirmation mail handed to
-- Resend, and a young sending domain that emits a burst of them to addresses
-- nobody typed is a domain that gets throttled. Nothing else in the schema
-- could cap that, because the natural place to count — subscriptions — is
-- deleted on unsubscribe, so a cap read from it resets for anyone willing to
-- unsubscribe. This table is never deleted from by the application, which is
-- the whole reason it is a table of its own and not a column on subscriptions.
--
-- It holds no address and no subscription id. Pairing "this requester" with
-- "this mailbox" is exactly the join this table exists without, so a cap can be
-- enforced without the throttle becoming a second, undeletable record of who
-- signed up for what.
--
-- ip_hash is the same date-mixed digest brand_requests uses — see
-- requesterHash() in src/lib/db.ts. Because the UTC date is inside the digest,
-- yesterday's rows stop matching anybody today, which is both how the window
-- rolls and why the column cannot follow a person across days.
create table if not exists watch_requests (
  id         bigint generated always as identity primary key,
  ip_hash    text        not null,
  created_at timestamptz not null default now()
);

-- The only query: how many has this requester filed today. ip_hash leads
-- because it is the equality, exactly as in brand_requests_once.
create index if not exists watch_requests_ip
  on watch_requests (ip_hash, created_at desc);

-- Belt and braces against the hosted layer. Supabase fronts the public schema
-- with a REST API whose anon key is designed to be handed to browsers, and
-- subscriptions is the one table that must never be readable that way. Row
-- level security with no policies denies the REST roles everything; the app's
-- own connection is the table owner, which RLS does not bind, so nothing in
-- alerts/ or src/ changes behaviour. On plain Postgres this is a no-op with
-- the same shape: deny roles that were never granted anything anyway.
alter table subscriptions   enable row level security;
alter table sent_alerts     enable row level security;
alter table events          enable row level security;
alter table email_sends     enable row level security;
alter table email_events    enable row level security;
alter table suppressions    enable row level security;
alter table brand_requests  enable row level security;
alter table watch_requests  enable row level security;
