-- 001: people, events, teams and submissions (tier 1).
-- Rules that must survive application bugs live here as constraints and triggers.

CREATE TABLE users (
    id            text PRIMARY KEY,
    email         text NOT NULL UNIQUE CHECK (email = lower(email) AND email LIKE '%_@_%'),
    name          text NOT NULL,
    password_hash text,                                -- NULL until the person sets a password
    is_admin      boolean NOT NULL DEFAULT false,
    created_at    timestamptz NOT NULL DEFAULT now()
);

-- Credentials are stored as sha256 digests only.
CREATE TABLE sessions (
    token_hash text PRIMARY KEY,
    user_id    text NOT NULL REFERENCES users ON DELETE CASCADE,
    csrf       text NOT NULL,
    expires_at timestamptz NOT NULL
);
CREATE TABLE api_tokens (
    id         text PRIMARY KEY,
    user_id    text NOT NULL REFERENCES users ON DELETE CASCADE,
    label      text NOT NULL,
    token_hash text NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now(),
    revoked_at timestamptz
);

CREATE TABLE events (
    id             text PRIMARY KEY,
    slug           text NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9][a-z0-9-]*$'),
    name           text NOT NULL,
    description    text NOT NULL DEFAULT '',
    opens_at       timestamptz,
    closes_at      timestamptz NOT NULL,              -- submissions close
    max_team_size  int NOT NULL DEFAULT 4 CHECK (max_team_size BETWEEN 1 AND 20),
    created_at     timestamptz NOT NULL DEFAULT now(),
    CHECK (opens_at IS NULL OR opens_at < closes_at)
);

-- Roles are per event. "visitor" = not logged in; "admin" = users.is_admin.
CREATE TABLE memberships (
    event_id text NOT NULL REFERENCES events ON DELETE CASCADE,
    user_id  text NOT NULL REFERENCES users ON DELETE CASCADE,
    role     text NOT NULL CHECK (role IN ('organizer', 'judge', 'participant')),
    PRIMARY KEY (event_id, user_id, role)
);

CREATE TABLE tracks (
    id       text PRIMARY KEY,
    event_id text NOT NULL REFERENCES events ON DELETE CASCADE,
    name     text NOT NULL,
    UNIQUE (id, event_id)
);
CREATE TABLE prizes (
    id       text PRIMARY KEY,
    event_id text NOT NULL REFERENCES events ON DELETE CASCADE,
    name     text NOT NULL,
    amount   text NOT NULL DEFAULT ''
);

CREATE TABLE teams (
    id          text PRIMARY KEY,
    event_id    text NOT NULL REFERENCES events ON DELETE CASCADE,
    name        text NOT NULL,
    invite_code text NOT NULL UNIQUE,
    UNIQUE (id, event_id)
);
CREATE TABLE team_members (
    team_id  text NOT NULL,
    event_id text NOT NULL,
    user_id  text NOT NULL REFERENCES users ON DELETE CASCADE,
    captain  boolean NOT NULL DEFAULT false,
    PRIMARY KEY (team_id, user_id),
    UNIQUE (event_id, user_id),                          -- one team per person per event
    FOREIGN KEY (team_id, event_id) REFERENCES teams (id, event_id) ON DELETE CASCADE
);

CREATE TABLE projects (
    id           text PRIMARY KEY,
    event_id     text NOT NULL,
    team_id      text NOT NULL,
    track_id     text,
    title        text NOT NULL CHECK (length(title) BETWEEN 1 AND 120),
    summary      text NOT NULL DEFAULT '' CHECK (length(summary) <= 500),
    repo_url     text NOT NULL DEFAULT '',
    status       text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'submitted', 'withdrawn', 'duplicate')),
    duplicate_of text REFERENCES projects,
    submitted_at timestamptz,
    updated_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (id, event_id),
    FOREIGN KEY (team_id, event_id) REFERENCES teams (id, event_id) ON DELETE CASCADE,
    FOREIGN KEY (track_id, event_id) REFERENCES tracks (id, event_id) DEFERRABLE INITIALLY DEFERRED,
    CHECK ((status = 'duplicate') = (duplicate_of IS NOT NULL))
);
-- One live entry per team: a second one cannot even be stored.
CREATE UNIQUE INDEX one_live_entry_per_team ON projects (team_id) WHERE status IN ('draft', 'submitted');

-- The deadline, enforced by the database. Status-only moderation (withdraw / duplicate) is allowed
-- after close; everything else needs the window open. The importer opts out for one transaction.
CREATE FUNCTION submission_window() RETURNS trigger AS $$
DECLARE e events%ROWTYPE;
BEGIN
    IF current_setting('dogfood.importing', true) = 'on' THEN RETURN NEW; END IF;
    IF TG_OP = 'UPDATE' AND (NEW.title, NEW.summary, NEW.repo_url, NEW.track_id, NEW.team_id)
                         IS NOT DISTINCT FROM (OLD.title, OLD.summary, OLD.repo_url, OLD.track_id, OLD.team_id)
       AND NEW.status IN ('withdrawn', 'duplicate') THEN
        RETURN NEW;
    END IF;
    SELECT * INTO e FROM events WHERE id = NEW.event_id;
    IF now() >= e.closes_at OR (e.opens_at IS NOT NULL AND now() < e.opens_at) THEN
        RAISE EXCEPTION 'submissions for % are closed', e.name USING ERRCODE = 'DF001';
    END IF;
    RETURN NEW;
END $$ LANGUAGE plpgsql;
CREATE TRIGGER projects_window BEFORE INSERT OR UPDATE ON projects
    FOR EACH ROW EXECUTE FUNCTION submission_window();

-- Append-only, hash-chained record of every change.
CREATE TABLE audit_log (
    id        bigserial PRIMARY KEY,
    at        timestamptz NOT NULL DEFAULT now(),
    actor     text,
    action    text NOT NULL,
    event_id  text,
    subject   text NOT NULL DEFAULT '',
    detail    jsonb NOT NULL DEFAULT '{}',
    prev_hash text NOT NULL,
    hash      text NOT NULL
);
CREATE FUNCTION audit_is_append_only() RETURNS trigger AS $$
BEGIN RAISE EXCEPTION 'audit_log is append-only' USING ERRCODE = 'DF009'; END $$ LANGUAGE plpgsql;
CREATE TRIGGER audit_no_rewrite BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_is_append_only();
