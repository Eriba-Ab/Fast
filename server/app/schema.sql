-- Taraba Central Billing and Collections: payment server schema (PostgreSQL).
-- Applied at start-up; every statement is idempotent. Money is always in kobo (bigint).

-- Workbook-wide facts (revenue head names, months, tax year, dates).
CREATE TABLE IF NOT EXISTS meta (
  key   text PRIMARY KEY,
  value jsonb NOT NULL
);

-- One row per demand notice, loaded from the workbook by `python -m app.cli sync`.
-- A payment never alters an assessment.
CREATE TABLE IF NOT EXISTS assessments (
  dn                   text PRIMARY KEY,
  tax_year             int  NOT NULL,
  reg_id               text NOT NULL,
  ttin                 text,
  name                 text NOT NULL,
  entity_type          text NOT NULL,
  category             text NOT NULL,
  lga                  text NOT NULL,
  business_size        text NOT NULL,
  address              text NOT NULL,
  phone                text,                       -- staff only; never returned publicly
  heads_kobo           jsonb NOT NULL,             -- amounts in meta.heads order
  subtotal_fees_kobo   bigint NOT NULL,
  total_assessed_kobo  bigint NOT NULL,
  assessment_date      date NOT NULL,
  due_date             date NOT NULL,
  source_workbook      text NOT NULL,
  synced_at            timestamptz NOT NULL DEFAULT now(),
  UNIQUE (reg_id, tax_year)
);
CREATE INDEX IF NOT EXISTS assessments_ttin_year ON assessments (upper(ttin), tax_year);

-- The workbook's monthly collections. No payment instrument stands behind these:
-- they never get a transaction reference, channel or day.
CREATE TABLE IF NOT EXISTS legacy_collections (
  dn          text NOT NULL REFERENCES assessments(dn) ON DELETE CASCADE,
  period      date NOT NULL,                        -- first day of the month
  amount_kobo bigint NOT NULL,
  source      text NOT NULL,
  PRIMARY KEY (dn, period)
);

-- One row per checkout attempt; each attempt has its own unique payment reference.
CREATE TABLE IF NOT EXISTS payment_intents (
  payment_reference      text PRIMARY KEY,         -- TIRS-<DN>-<attempt>
  dn                     text NOT NULL REFERENCES assessments(dn),
  attempt                int  NOT NULL,
  expected_amount_kobo   bigint NOT NULL,
  customer_email         text NOT NULL,
  transaction_reference  text UNIQUE,              -- from init-transaction
  checkout_url           text,
  status                 text NOT NULL,            -- CREATED | PAID | PARTIALLY_PAID | OVERPAID | FAILED | EXPIRED | REVERSED | ABANDONED
  created_at             timestamptz NOT NULL DEFAULT now(),
  expires_at             timestamptz NOT NULL,
  last_checked_at        timestamptz,
  UNIQUE (dn, attempt)
);

-- THE LEDGER: one row per Monnify transaction, written only after our server has
-- queried Monnify for it. The primary key is what makes duplicate webhooks harmless.
CREATE TABLE IF NOT EXISTS payments (
  transaction_reference  text PRIMARY KEY,
  payment_reference      text NOT NULL REFERENCES payment_intents(payment_reference),
  dn                     text NOT NULL REFERENCES assessments(dn),
  payment_status         text NOT NULL,            -- PAID | PARTIALLY_PAID | OVERPAID | REVERSED
  amount_paid_kobo       bigint NOT NULL,          -- gross: what settles the notice
  total_payable_kobo     bigint,
  settlement_amount_kobo bigint,                   -- net to the bank (internal only)
  fee_kobo               bigint,
  payment_method         text,
  paid_on                timestamptz,
  payer_account_name     text,
  payer_account_last4    text,
  amount_matches         boolean NOT NULL,         -- amount_paid >= expected for the intent
  verified_at            timestamptz NOT NULL DEFAULT now(),
  raw_verify             jsonb NOT NULL
);
CREATE INDEX IF NOT EXISTS payments_dn ON payments (dn);
CREATE INDEX IF NOT EXISTS payments_paid_on ON payments (paid_on);

-- Every status change to a ledger row, append-only.
CREATE TABLE IF NOT EXISTS payment_events (
  id                    bigserial PRIMARY KEY,
  transaction_reference text NOT NULL,
  from_status           text,
  to_status             text NOT NULL,
  source                text NOT NULL,            -- webhook | return | sweep | recon
  at                    timestamptz NOT NULL DEFAULT now()
);

-- Every inbound Monnify notification, kept raw. The worker verifies each one with
-- the Monnify API before anything is recorded.
CREATE TABLE IF NOT EXISTS webhook_events (
  id                    bigserial PRIMARY KEY,
  event_type            text NOT NULL,
  transaction_reference text,
  payment_reference     text,
  payload               jsonb NOT NULL,
  signature_ok          boolean,                  -- NULL: no signature sent (sandbox)
  source_ip             text,
  received_at           timestamptz NOT NULL DEFAULT now(),
  processed_at          timestamptz,
  process_result        text,
  attempts              int NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS webhook_events_dedupe ON webhook_events (event_type, transaction_reference)
  WHERE transaction_reference IS NOT NULL;
CREATE INDEX IF NOT EXISTS webhook_events_pending ON webhook_events (received_at) WHERE processed_at IS NULL;

-- Public lookups (hashed IP only), for rate limiting and audit.
CREATE TABLE IF NOT EXISTS lookup_audit (
  id         bigserial PRIMARY KEY,
  ip_hash    text NOT NULL,
  kind       text NOT NULL,                       -- notice | pay_start | receipt
  hit        boolean NOT NULL,
  at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS lookup_audit_ip_at ON lookup_audit (ip_hash, at);

-- Daily reconciliation against Monnify's transaction search.
CREATE TABLE IF NOT EXISTS recon_runs (
  id             bigserial PRIMARY KEY,
  ran_for        date NOT NULL,
  counts         jsonb NOT NULL,
  discrepancies  jsonb NOT NULL,
  expected_settlement_kobo bigint,
  at             timestamptz NOT NULL DEFAULT now()
);

-- Named staff accounts for the online staff console.
CREATE TABLE IF NOT EXISTS staff_users (
  id             bigserial PRIMARY KEY,
  username       text NOT NULL UNIQUE,
  full_name      text NOT NULL,
  password_hash  text NOT NULL,
  active         boolean NOT NULL DEFAULT true,
  created_at     timestamptz NOT NULL DEFAULT now(),
  last_login_at  timestamptz
);

CREATE TABLE IF NOT EXISTS staff_sessions (
  token_hash   text PRIMARY KEY,                  -- sha256 of the cookie value
  user_id      bigint NOT NULL REFERENCES staff_users(id) ON DELETE CASCADE,
  created_at   timestamptz NOT NULL DEFAULT now(),
  expires_at   timestamptz NOT NULL,
  ip_hash      text
);

-- Who did what in the staff console (logins, failures, views, exports).
CREATE TABLE IF NOT EXISTS audit_log (
  id         bigserial PRIMARY KEY,
  user_id    bigint REFERENCES staff_users(id),
  username   text,
  action     text NOT NULL,
  detail     jsonb,
  ip_hash    text,
  at         timestamptz NOT NULL DEFAULT now()
);
