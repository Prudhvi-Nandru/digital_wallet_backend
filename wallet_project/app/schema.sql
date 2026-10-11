CREATE TABLE IF NOT EXISTS users (
 id BIGSERIAL PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL UNIQUE,
 phone TEXT NOT NULL UNIQUE, kyc_status TEXT NOT NULL DEFAULT 'pending' CHECK (kyc_status IN ('pending','verified','rejected')),
 created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS accounts (
 id BIGSERIAL PRIMARY KEY, user_id BIGINT REFERENCES users(id), name TEXT NOT NULL,
 account_type TEXT NOT NULL CHECK (account_type IN ('wallet','cash_in','cash_out','fee_revenue')),
 currency CHAR(3) NOT NULL DEFAULT 'INR' CHECK(currency='INR'), balance_paise BIGINT NOT NULL DEFAULT 0,
 active BOOLEAN NOT NULL DEFAULT true, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 CHECK (account_type <> 'wallet' OR user_id IS NOT NULL), CHECK (account_type = 'wallet' OR user_id IS NULL)
);
CREATE OR REPLACE FUNCTION enforce_wallet_non_negative() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.account_type = 'wallet' AND NEW.balance_paise < 0 THEN
    RAISE EXCEPTION 'wallet balance cannot be negative';
  END IF;
  RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS wallet_balance_guard ON accounts;
CREATE TRIGGER wallet_balance_guard BEFORE INSERT OR UPDATE OF balance_paise, account_type ON accounts
FOR EACH ROW EXECUTE FUNCTION enforce_wallet_non_negative();
CREATE INDEX IF NOT EXISTS accounts_user_idx ON accounts(user_id, id) WHERE account_type='wallet';
CREATE TABLE IF NOT EXISTS transfers (
 id BIGSERIAL PRIMARY KEY, transfer_type TEXT NOT NULL CHECK(transfer_type IN ('topup','peer','withdrawal','fee','reversal')),
 status TEXT NOT NULL CHECK(status IN ('completed','held','flagged','reversed','rejected')),
 amount_paise BIGINT NOT NULL CHECK(amount_paise > 0), currency CHAR(3) NOT NULL DEFAULT 'INR' CHECK(currency='INR'),
 source_account_id BIGINT REFERENCES accounts(id), destination_account_id BIGINT REFERENCES accounts(id),
 created_by BIGINT REFERENCES users(id), note TEXT NOT NULL DEFAULT '', reversal_of BIGINT REFERENCES transfers(id),
 created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS transfers_source_created_idx ON transfers(source_account_id, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS transfers_destination_created_idx ON transfers(destination_account_id, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS transfers_status_created_idx ON transfers(status, created_at DESC);
CREATE TABLE IF NOT EXISTS ledger_entries (
 id BIGSERIAL PRIMARY KEY, transfer_id BIGINT NOT NULL REFERENCES transfers(id), account_id BIGINT NOT NULL REFERENCES accounts(id),
 direction TEXT NOT NULL CHECK(direction IN ('debit','credit')), amount_paise BIGINT NOT NULL CHECK(amount_paise > 0),
 created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ledger_account_time_idx ON ledger_entries(account_id, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS ledger_transfer_idx ON ledger_entries(transfer_id);
CREATE OR REPLACE FUNCTION forbid_ledger_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'ledger entries are append-only'; END $$;
DROP TRIGGER IF EXISTS ledger_no_update_delete ON ledger_entries;
CREATE TRIGGER ledger_no_update_delete BEFORE UPDATE OR DELETE ON ledger_entries FOR EACH ROW EXECUTE FUNCTION forbid_ledger_mutation();
CREATE TABLE IF NOT EXISTS idempotency_keys (
 id BIGSERIAL PRIMARY KEY, user_id BIGINT REFERENCES users(id), key UUID NOT NULL, request_hash CHAR(64) NOT NULL,
 status TEXT NOT NULL DEFAULT 'running' CHECK(status IN ('running','completed')), response_code INTEGER, response_body JSONB,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), UNIQUE(user_id,key)
);
CREATE TABLE IF NOT EXISTS transfer_status_audit (
 id BIGSERIAL PRIMARY KEY, transfer_id BIGINT NOT NULL REFERENCES transfers(id), old_status TEXT, new_status TEXT NOT NULL,
 changed_by BIGINT REFERENCES users(id), reason TEXT NOT NULL DEFAULT '', created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS risk_rules (
 id BIGSERIAL PRIMARY KEY, rule_key TEXT UNIQUE NOT NULL, enabled BOOLEAN NOT NULL DEFAULT true,
 threshold BIGINT NOT NULL, window_seconds INTEGER NOT NULL DEFAULT 600, action TEXT NOT NULL CHECK(action IN ('hold','flag_allow'))
);
INSERT INTO risk_rules(rule_key,threshold,window_seconds,action) VALUES
 ('velocity',5,600,'hold'),('amount_spike',5,0,'hold'),('new_payee_large',1000000,0,'flag_allow')
ON CONFLICT(rule_key) DO NOTHING;
CREATE TABLE IF NOT EXISTS risk_flags (
 id BIGSERIAL PRIMARY KEY, transfer_id BIGINT NOT NULL REFERENCES transfers(id), rule_key TEXT NOT NULL,
 explanation TEXT NOT NULL DEFAULT '', label TEXT NOT NULL DEFAULT 'review' CHECK(label IN ('likely_ok','review','likely_fraud')),
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), UNIQUE(transfer_id,rule_key)
);
CREATE TABLE IF NOT EXISTS reconciliation_runs (
 id BIGSERIAL PRIMARY KEY, started_at TIMESTAMPTZ NOT NULL, ended_at TIMESTAMPTZ, status TEXT NOT NULL,
 issue_count INTEGER NOT NULL DEFAULT 0, checks JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE TABLE IF NOT EXISTS reconciliation_issues (
 id BIGSERIAL PRIMARY KEY, run_id BIGINT NOT NULL REFERENCES reconciliation_runs(id), check_name TEXT NOT NULL,
 object_id BIGINT, expected TEXT, actual TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS job_locks (job_name TEXT PRIMARY KEY, touched_at TIMESTAMPTZ NOT NULL DEFAULT now());
INSERT INTO job_locks(job_name) VALUES('reconciliation') ON CONFLICT DO NOTHING;
-- System accounts may carry negative balances; wallet overdrafts are prevented in locked transfer transactions.
CREATE UNIQUE INDEX IF NOT EXISTS idempotency_scope_key_unique ON idempotency_keys(COALESCE(user_id,0),key);
