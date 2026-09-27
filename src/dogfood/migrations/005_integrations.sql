-- 005: integrations (tier 4). Webhooks with an outbox, and signed certificates.

CREATE TABLE webhooks (
    id         text PRIMARY KEY,
    event_id   text NOT NULL REFERENCES events ON DELETE CASCADE,
    url        text NOT NULL CHECK (url ~ '^https?://'),
    secret     text NOT NULL,                 -- needed in clear to sign; shown to the organizer once
    active     boolean NOT NULL DEFAULT true,
    created_by text REFERENCES users ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- Every notification is written here in the same transaction as the change it reports, so a crash
-- can never publish results without the webhook, or send a webhook for a rolled-back change.
-- `sequence` is strictly increasing: receivers can put events back in order.
-- `id` is the same on every retry: receivers deduplicate on it.
CREATE TABLE webhook_events (
    sequence    bigserial PRIMARY KEY,
    id          text NOT NULL UNIQUE,
    event_id    text NOT NULL REFERENCES events ON DELETE CASCADE,
    type        text NOT NULL,
    body        text NOT NULL,                -- the exact bytes that are signed and sent
    occurred_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE webhook_deliveries (
    webhook_id   text NOT NULL REFERENCES webhooks ON DELETE CASCADE,
    sequence     bigint NOT NULL REFERENCES webhook_events ON DELETE CASCADE,
    status       text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'delivered', 'failed')),
    attempts     int NOT NULL DEFAULT 0,
    next_at      timestamptz NOT NULL DEFAULT now(),
    last_status  int,
    last_error   text,
    delivered_at timestamptz,
    PRIMARY KEY (webhook_id, sequence)
);
CREATE INDEX deliveries_due ON webhook_deliveries (next_at) WHERE status = 'pending';

-- Signed records (certificates). A record never changes once issued; the signature covers it.
CREATE TABLE certificates (
    id         text PRIMARY KEY,
    event_id   text NOT NULL REFERENCES events ON DELETE CASCADE,
    kind       text NOT NULL CHECK (kind IN ('winner', 'track_winner', 'participant')),
    record     jsonb NOT NULL,
    canonical  text NOT NULL,                 -- the exact signed bytes
    signature  text NOT NULL,
    key_id     text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE FUNCTION certificates_frozen() RETURNS trigger AS $$
BEGIN RAISE EXCEPTION 'certificates cannot be changed' USING ERRCODE = 'DF006'; END $$ LANGUAGE plpgsql;
CREATE TRIGGER certificates_no_rewrite BEFORE UPDATE ON certificates
    FOR EACH ROW EXECUTE FUNCTION certificates_frozen();
