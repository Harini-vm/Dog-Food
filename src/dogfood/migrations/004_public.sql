-- 004: the public side (tier 3). People's-choice voting, comments, and an outbox for mail.

ALTER TABLE events ADD COLUMN voting_opens_at  timestamptz;
ALTER TABLE events ADD COLUMN voting_closes_at timestamptz;
ALTER TABLE events ADD COLUMN votes_per_voter  int NOT NULL DEFAULT 3 CHECK (votes_per_voter BETWEEN 1 AND 50);
ALTER TABLE events ADD COLUMN comments_open    boolean NOT NULL DEFAULT true;
ALTER TABLE events ADD CONSTRAINT voting_window CHECK (voting_opens_at IS NULL OR voting_closes_at IS NULL
                                                       OR voting_opens_at < voting_closes_at);

-- A voter is either an account or an email address. Email addresses are stored only as a keyed hash
-- (HMAC with the server secret): we can tell two votes came from one address, but a database dump
-- does not reveal who voted.
CREATE TABLE voters (
    id         text PRIMARY KEY,
    event_id   text NOT NULL REFERENCES events ON DELETE CASCADE,
    user_id    text REFERENCES users ON DELETE CASCADE,
    email_key  text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (event_id, user_id),
    UNIQUE (event_id, email_key),
    UNIQUE (id, event_id),
    CHECK ((user_id IS NULL) <> (email_key IS NULL))
);

-- One-time voting links (for email voters) and the short sessions they start.
CREATE TABLE voter_links (
    token_hash text PRIMARY KEY,
    voter_id   text NOT NULL REFERENCES voters ON DELETE CASCADE,
    expires_at timestamptz NOT NULL,
    used_at    timestamptz
);
CREATE TABLE voter_sessions (
    token_hash text PRIMARY KEY,
    voter_id   text NOT NULL REFERENCES voters ON DELETE CASCADE,
    csrf       text NOT NULL,
    expires_at timestamptz NOT NULL
);

-- One vote per voter per project: the primary key makes a double vote impossible, however many
-- requests arrive at once. There is no vote counter anywhere: budgets and tallies are counted from
-- this table, so nothing can drift.
CREATE TABLE votes (
    voter_id   text NOT NULL,
    project_id text NOT NULL,
    event_id   text NOT NULL,
    cast_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (voter_id, project_id),
    FOREIGN KEY (voter_id, event_id) REFERENCES voters (id, event_id) ON DELETE CASCADE,
    FOREIGN KEY (project_id, event_id) REFERENCES projects (id, event_id) ON DELETE CASCADE
);
CREATE INDEX votes_project ON votes (project_id);

-- The voting window, enforced by the database with its own clock. Half-open: opens <= now < closes.
CREATE FUNCTION voting_window() RETURNS trigger AS $$
DECLARE e events%ROWTYPE;
BEGIN
    SELECT * INTO e FROM events WHERE id = coalesce(NEW.event_id, OLD.event_id);
    IF e.voting_opens_at IS NULL OR now() < e.voting_opens_at
       OR (e.voting_closes_at IS NOT NULL AND now() >= e.voting_closes_at) THEN
        RAISE EXCEPTION 'voting for % is not open', e.name USING ERRCODE = 'DF005';
    END IF;
    RETURN coalesce(NEW, OLD);
END $$ LANGUAGE plpgsql;
CREATE TRIGGER votes_in_window BEFORE INSERT OR DELETE ON votes
    FOR EACH ROW EXECUTE FUNCTION voting_window();

CREATE TABLE comments (
    id            text PRIMARY KEY,
    event_id      text NOT NULL,
    project_id    text NOT NULL,
    user_id       text NOT NULL REFERENCES users ON DELETE CASCADE,
    body          text NOT NULL CHECK (char_length(body) BETWEEN 1 AND 2000),
    created_at    timestamptz NOT NULL DEFAULT now(),
    hidden_at     timestamptz,
    hidden_by     text REFERENCES users ON DELETE SET NULL,
    hidden_reason text,
    FOREIGN KEY (project_id, event_id) REFERENCES projects (id, event_id) ON DELETE CASCADE
);
CREATE INDEX comments_project ON comments (project_id, created_at);

-- Mail is written here in the same transaction as the action that caused it. With no mail server
-- configured, nothing is lost: the admin can read the outbox, and demo mode shows links on screen.
CREATE TABLE outbox (
    id         bigserial PRIMARY KEY,
    to_address text NOT NULL,
    subject    text NOT NULL,
    body       text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    sent_at    timestamptz
);
