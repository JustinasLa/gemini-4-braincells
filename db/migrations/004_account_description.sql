-- 004: profile bio text (owner: kirill). Additive.
-- signals/ reads it for the "declared automation" guard (accounts that say they are bots, feeds or
-- automated alerts). Only fetched for cluster candidates and the random baseline sample.
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS description TEXT;
