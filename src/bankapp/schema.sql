-- bankapp schema. All CREATEs use IF NOT EXISTS so apply is idempotent.
-- Core principle: raw_txn is immutable bank truth (enforced by triggers);
-- every other table is a revisable interpretation/advisor layer on top.

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', '1'), ('hash_version', '1');

CREATE TABLE IF NOT EXISTS accounts (
  id INTEGER PRIMARY KEY,
  key TEXT NOT NULL UNIQUE,             -- config key, e.g. 'td-chequing'
  institution TEXT NOT NULL,            -- 'td' | 'wealthsimple'
  type TEXT NOT NULL CHECK (type IN ('chequing','savings','visa','cash','investment','crypto')),
  currency TEXT NOT NULL DEFAULT 'CAD',
  external_id TEXT,                     -- OFX ACCTID or WS account id
  locked INTEGER NOT NULL DEFAULT 0     -- counted in net worth, but not accessible (e.g. TFSA)
);

CREATE TABLE IF NOT EXISTS raw_txn (
  id INTEGER PRIMARY KEY,
  account_id INTEGER NOT NULL REFERENCES accounts(id),
  posted_date TEXT NOT NULL,            -- 'YYYY-MM-DD' America/Vancouver local
  amount_minor INTEGER NOT NULL,        -- signed minor units
  currency TEXT NOT NULL,
  description_raw TEXT NOT NULL,
  description_norm TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'posted' CHECK (status IN ('pending','posted')),
  dedup_key TEXT NOT NULL,              -- 'fitid:...' | 'wsid:...' | 'plaid:...' | 'sha256:...'
  source TEXT NOT NULL,                 -- 'ofx' | 'csv' | 'ws' | 'plaid'
  imported_at TEXT NOT NULL,            -- ISO-8601 UTC
  UNIQUE (account_id, dedup_key)
);
CREATE INDEX IF NOT EXISTS idx_raw_txn_acct_date ON raw_txn(account_id, posted_date);

-- IMMUTABILITY: the core principle, enforced in the engine.
CREATE TRIGGER IF NOT EXISTS raw_txn_no_update BEFORE UPDATE ON raw_txn
BEGIN SELECT RAISE(ABORT, 'raw_txn is immutable'); END;
CREATE TRIGGER IF NOT EXISTS raw_txn_no_delete BEFORE DELETE ON raw_txn
BEGIN SELECT RAISE(ABORT, 'raw_txn is immutable'); END;

CREATE TABLE IF NOT EXISTS import_log (
  id INTEGER PRIMARY KEY, filename TEXT NOT NULL, file_sha256 TEXT NOT NULL UNIQUE,
  imported_at TEXT NOT NULL, rows_inserted INTEGER NOT NULL, rows_skipped INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS rules (
  id INTEGER PRIMARY KEY,
  match_kind TEXT NOT NULL CHECK (match_kind IN ('substring','regex')),
  pattern TEXT NOT NULL,                -- matched against description_norm (lowercase)
  category TEXT,
  role_hint TEXT CHECK (role_hint IN ('transfer','reimbursement','expense','income') OR role_hint IS NULL),
  counterparty TEXT,
  priority INTEGER NOT NULL DEFAULT 100,    -- lower wins
  source TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('manual','claude','seed')),
  created_at TEXT NOT NULL,
  UNIQUE (match_kind, pattern)
);

-- Interpretation layer: revisable, never touches raw_txn.
CREATE TABLE IF NOT EXISTS txn_interp (
  raw_txn_id INTEGER PRIMARY KEY REFERENCES raw_txn(id),
  category TEXT, role_hint TEXT, counterparty TEXT,
  rule_id INTEGER REFERENCES rules(id),
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recurring_templates (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,            -- upserted from config by name
  kind TEXT NOT NULL CHECK (kind IN ('split_expense')),
  expected_amount_minor INTEGER NOT NULL,   -- FULL expense (e.g. total rent, 2X)
  currency TEXT NOT NULL DEFAULT 'CAD',
  cadence TEXT NOT NULL DEFAULT 'monthly',
  share_numer INTEGER NOT NULL, share_denom INTEGER NOT NULL,  -- my share (50/50 -> 1/2)
  expense_account TEXT NOT NULL,       -- accounts.key
  expense_pattern TEXT NOT NULL,       -- substring on description_norm
  reimburse_account TEXT NOT NULL,
  reimburser_pattern TEXT NOT NULL,    -- Interac SENDER-NAME pattern (config, not code)
  amount_tolerance_minor INTEGER NOT NULL DEFAULT 500,
  day_of_month INTEGER NOT NULL DEFAULT 1,
  window_days INTEGER NOT NULL DEFAULT 45,   -- reimbursement due window
  link_transfer INTEGER NOT NULL DEFAULT 1,  -- claim TD->WS legs into this group
  reimburse_min_minor INTEGER NOT NULL DEFAULT 0,  -- amount gate for anonymized e-transfers
  active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS groups (
  id INTEGER PRIMARY KEY,
  type TEXT NOT NULL CHECK (type IN ('transfer','split_expense')),
  status TEXT NOT NULL,
    -- transfer: 'matched'
    -- split_expense: 'open'|'settled'|'underpaid'|'amount_anomaly'|'missing_expense'
  template_id INTEGER REFERENCES recurring_templates(id),
  period_key TEXT,                     -- 'YYYY-MM' for template groups
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE (template_id, period_key)
);

CREATE TABLE IF NOT EXISTS group_members (
  group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
  raw_txn_id INTEGER NOT NULL REFERENCES raw_txn(id),
  role TEXT NOT NULL CHECK (role IN ('expense','income','transfer_out','transfer_in','reimbursement')),
  share_amount_minor INTEGER,          -- positive; only on 'expense' rows (my share)
  PRIMARY KEY (group_id, raw_txn_id),
  UNIQUE (raw_txn_id)                  -- one group per txn => structurally no double-count
);

-- Analytics: transfers net to 0, reimbursements 0, split expense counts MY SHARE only,
-- lone hinted-transfer legs excluded (pending, not lost).
CREATE VIEW IF NOT EXISTS v_effective AS
SELECT r.id, r.account_id, r.posted_date, r.currency, r.amount_minor, r.description_norm,
  i.category, i.role_hint, gm.role AS group_role, g.type AS group_type,
  CASE
    WHEN gm.role IN ('transfer_in','transfer_out') THEN 0
    WHEN gm.role = 'reimbursement' THEN 0
    WHEN gm.role = 'expense' AND gm.share_amount_minor IS NOT NULL THEN -gm.share_amount_minor
    WHEN gm.raw_txn_id IS NULL AND i.role_hint = 'transfer' THEN 0
    ELSE r.amount_minor
  END AS effective_minor
FROM raw_txn r
LEFT JOIN txn_interp i ON i.raw_txn_id = r.id
LEFT JOIN group_members gm ON gm.raw_txn_id = r.id
LEFT JOIN groups g ON g.id = gm.group_id;

-- Lone transfer legs: NORMAL (TD weekly batches vs WS realtime), surfaced with age.
CREATE VIEW IF NOT EXISTS v_pending_transfers AS
SELECT r.id, r.account_id, r.posted_date, r.amount_minor, r.description_norm,
       CAST(julianday('now') - julianday(r.posted_date) AS INTEGER) AS age_days
FROM raw_txn r
JOIN txn_interp i ON i.raw_txn_id = r.id AND i.role_hint = 'transfer'
LEFT JOIN group_members gm ON gm.raw_txn_id = r.id
WHERE gm.raw_txn_id IS NULL;

-- ADVISOR-LAYER TABLES ------------------------------------------------------

-- Append-only balance snapshots captured on every sync. Liabilities (visa) NEGATIVE.
CREATE TABLE IF NOT EXISTS balance_snapshot (
  id INTEGER PRIMARY KEY,
  account_id INTEGER NOT NULL REFERENCES accounts(id),
  as_of TEXT NOT NULL,                 -- 'YYYY-MM-DD'
  balance_minor INTEGER NOT NULL,      -- signed; visa owed = negative
  currency TEXT NOT NULL,
  source TEXT NOT NULL CHECK (source IN ('ws','plaid','ofx','manual')),
  captured_at TEXT NOT NULL,
  UNIQUE (account_id, as_of, source)   -- one snapshot per account per day per source
);

CREATE TABLE IF NOT EXISTS budgets (   -- upserted from config [budgets] by category
  id INTEGER PRIMARY KEY,
  category TEXT NOT NULL UNIQUE,
  monthly_limit_minor INTEGER NOT NULL,
  currency TEXT NOT NULL DEFAULT 'CAD',
  active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS goals (     -- upserted from config [[goals]] by name
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  target_minor INTEGER NOT NULL,
  currency TEXT NOT NULL DEFAULT 'CAD',
  start_date TEXT NOT NULL,            -- progress = net savings since this date x allocation
  target_date TEXT,
  allocation_pct INTEGER NOT NULL DEFAULT 100,
  note TEXT,
  active INTEGER NOT NULL DEFAULT 1
);

-- Net worth: latest snapshot per account (freshest as_of, any source), summed per currency.
CREATE VIEW IF NOT EXISTS v_net_worth AS
SELECT b.currency, SUM(b.balance_minor) AS net_worth_minor, MAX(b.as_of) AS freshest_as_of
FROM balance_snapshot b
JOIN (SELECT account_id, MAX(as_of) AS as_of FROM balance_snapshot GROUP BY account_id) latest
  ON latest.account_id = b.account_id AND latest.as_of = b.as_of
GROUP BY b.currency;

-- Am I saving? Monthly income/spend/net from effective amounts.
-- Ungrouped reimbursement inflows (a friend repaying their share) are money coming
-- BACK for spend already counted at face value: they reduce spend, they are not
-- income. Group-claimed reimbursements are already zeroed in v_effective, so the
-- offset applies only where group_role IS NULL. `IS` (not `=`) keeps NULL role_hint
-- rows on the income side.
CREATE VIEW IF NOT EXISTS v_monthly_cashflow AS
SELECT substr(posted_date, 1, 7) AS month, currency,
  SUM(CASE WHEN effective_minor > 0
             AND NOT (role_hint IS 'reimbursement' AND group_role IS NULL)
           THEN effective_minor ELSE 0 END) AS income_minor,
  SUM(CASE WHEN effective_minor < 0 THEN -effective_minor ELSE 0 END)
    - SUM(CASE WHEN effective_minor > 0
                 AND role_hint IS 'reimbursement' AND group_role IS NULL
               THEN effective_minor ELSE 0 END) AS spend_minor,
  SUM(effective_minor) AS net_minor
FROM v_effective
GROUP BY month, currency;

-- Receivables (AR-lite) with aging: expected = roommate's share = |expense| - my share.
CREATE VIEW IF NOT EXISTS v_receivables AS
SELECT g.id AS group_id, t.name AS template, g.period_key, g.status,
  exp.expense_minor,
  (ABS(exp.expense_minor) - exp.share_minor) AS expected_minor,
  COALESCE(reimb.received_minor, 0) AS received_minor,
  (ABS(exp.expense_minor) - exp.share_minor) - COALESCE(reimb.received_minor, 0) AS outstanding_minor,
  CAST(julianday('now') - julianday(exp.expense_date) AS INTEGER) AS age_days
FROM groups g
JOIN recurring_templates t ON t.id = g.template_id
LEFT JOIN (SELECT gm.group_id, r.amount_minor AS expense_minor,
                  gm.share_amount_minor AS share_minor, r.posted_date AS expense_date
           FROM group_members gm JOIN raw_txn r ON r.id = gm.raw_txn_id
           WHERE gm.role = 'expense') exp ON exp.group_id = g.id
LEFT JOIN (SELECT gm.group_id, SUM(ABS(r.amount_minor)) AS received_minor
           FROM group_members gm JOIN raw_txn r ON r.id = gm.raw_txn_id
           WHERE gm.role = 'reimbursement' GROUP BY gm.group_id) reimb ON reimb.group_id = g.id
WHERE g.type = 'split_expense';
