-- Run this in Supabase SQL Editor

CREATE TABLE IF NOT EXISTS starhela_leads (
    id              SERIAL PRIMARY KEY,
    phone           TEXT NOT NULL UNIQUE,
    name            TEXT,
    status          TEXT DEFAULT 'new',
    -- status values: new, interested, awaiting_screenshot, screenshot_received, opted_out
    conversation_history JSONB DEFAULT '[]',
    last_message_at TIMESTAMPTZ DEFAULT NOW(),
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    followed_up     BOOLEAN DEFAULT FALSE,
    opted_in        BOOLEAN DEFAULT TRUE,
    screenshot_url  TEXT,
    notes           TEXT
);

ALTER TABLE starhela_leads DISABLE ROW LEVEL SECURITY;
