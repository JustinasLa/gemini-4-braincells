-- 003: random baseline sample of active accounts (owner: kirill). Additive.
-- signals/ enriches a few random active accounts per pass and records them here. Their profile
-- and PLC data (in `accounts`) give the base rates the identity test compares clusters against:
-- "10 of 12 members are under 30 days old, while only 7% of active accounts are".
CREATE TABLE IF NOT EXISTS baseline_accounts (
    did         TEXT PRIMARY KEY,
    sampled_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    window_end  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS baseline_accounts_sampled_idx ON baseline_accounts (sampled_at);
