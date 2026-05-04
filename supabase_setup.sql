-- Run this once in Supabase SQL Editor

-- One row per client deployment (business owner)
-- Subscription plan: $200 per 30 days
-- To renew a client: UPDATE bot_clients SET expires_at = NOW() + INTERVAL '30 days' WHERE client_id = 'xxx';
CREATE TABLE IF NOT EXISTS bot_clients (
    id            SERIAL PRIMARY KEY,
    client_id     TEXT NOT NULL UNIQUE,  -- matches CLIENT_ID env var on Railway
    business_name TEXT NOT NULL,
    owner_phone   TEXT NOT NULL,
    status        TEXT DEFAULT 'active', -- active | expired | suspended
    subscribed_at TIMESTAMPTZ DEFAULT NOW(),
    expires_at    TIMESTAMPTZ NOT NULL,  -- update this when client pays to renew
    plan          TEXT DEFAULT 'monthly',
    price_usd     INTEGER DEFAULT 200,   -- $200 per 30 days
    notes         TEXT,
    created_at    TIMESTAMPTZ DEFAULT NOW()
);

-- All leads/customers per client
CREATE TABLE IF NOT EXISTS bot_leads (
    id                   SERIAL PRIMARY KEY,
    client_id            TEXT NOT NULL,
    phone                TEXT NOT NULL,
    status               TEXT DEFAULT 'new',
    -- status: new | interested | awaiting_screenshot | screenshot_received | opted_out
    conversation_history JSONB DEFAULT '[]',
    last_message_at      TIMESTAMPTZ DEFAULT NOW(),
    created_at           TIMESTAMPTZ DEFAULT NOW(),
    followed_up          BOOLEAN DEFAULT FALSE,
    opted_in             BOOLEAN DEFAULT TRUE,
    screenshot_url       TEXT,
    notes                TEXT,
    UNIQUE (client_id, phone)
);

ALTER TABLE bot_clients DISABLE ROW LEVEL SECURITY;
ALTER TABLE bot_leads DISABLE ROW LEVEL SECURITY;

-- Insert Starhela as first client (update expires_at when they pay)
INSERT INTO bot_clients (client_id, business_name, owner_phone, expires_at, price_usd)
VALUES (
    'starhela-001',
    'Starhela',
    '0793482095',
    NOW() + INTERVAL '30 days',
    200
)
ON CONFLICT (client_id) DO NOTHING;
