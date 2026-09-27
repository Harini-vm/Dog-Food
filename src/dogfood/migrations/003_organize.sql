-- 003: running an event (tier 2 UI). Invitations, assignment batches and frozen results.

-- One-time links. An organizer invites a judge (or co-organizer) by email; the link lets that person
-- set a password and lands them in the event. Only the digest is stored, like every other credential.
CREATE TABLE invitations (
    token_hash  text PRIMARY KEY,
    event_id    text NOT NULL REFERENCES events ON DELETE CASCADE,
    user_id     text NOT NULL REFERENCES users ON DELETE CASCADE,
    role        text NOT NULL CHECK (role IN ('organizer', 'judge')),
    created_by  text REFERENCES users ON DELETE SET NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    expires_at  timestamptz NOT NULL,
    used_at     timestamptz
);

-- Published results are a snapshot, not a live query: a score edited after publishing (by a bug,
-- or by an admin at 2am) cannot silently change a ranking people have already seen.
-- Each publish is a new version; the newest non-retracted one is public.
CREATE TABLE results (
    event_id     text NOT NULL REFERENCES events ON DELETE CASCADE,
    version      int NOT NULL,
    method       text NOT NULL,
    body         jsonb NOT NULL,
    body_hash    text NOT NULL,
    published_by text REFERENCES users ON DELETE SET NULL,
    published_at timestamptz NOT NULL DEFAULT now(),
    retracted_at timestamptz,
    PRIMARY KEY (event_id, version)
);

-- A snapshot never changes after it is written; only the retraction time may be set, once.
CREATE FUNCTION results_frozen() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' OR NEW.body IS DISTINCT FROM OLD.body OR NEW.body_hash IS DISTINCT FROM OLD.body_hash
       OR NEW.version IS DISTINCT FROM OLD.version OR OLD.retracted_at IS NOT NULL THEN
        RAISE EXCEPTION 'published results are frozen' USING ERRCODE = 'DF003';
    END IF;
    RETURN NEW;
END $$ LANGUAGE plpgsql;
CREATE TRIGGER results_no_rewrite BEFORE UPDATE OR DELETE ON results
    FOR EACH ROW EXECUTE FUNCTION results_frozen();

-- Scores cannot change once results are published. The application checks first (friendly
-- message); this is the backstop for any code path that forgets.
CREATE FUNCTION scores_locked() RETURNS trigger AS $$
DECLARE ev text; pub timestamptz;
BEGIN
    IF current_setting('dogfood.importing', true) = 'on' THEN RETURN coalesce(NEW, OLD); END IF;
    IF TG_TABLE_NAME = 'score_items' THEN
        SELECT s.event_id INTO ev FROM scores s WHERE s.id = coalesce(NEW.score_id, OLD.score_id);
    ELSE
        ev := coalesce(NEW.event_id, OLD.event_id);
    END IF;
    SELECT results_published_at INTO pub FROM events WHERE id = ev;
    IF pub IS NOT NULL THEN
        RAISE EXCEPTION 'results are published; scores are locked' USING ERRCODE = 'DF004';
    END IF;
    RETURN coalesce(NEW, OLD);
END $$ LANGUAGE plpgsql;
CREATE TRIGGER scores_lock BEFORE INSERT OR UPDATE OR DELETE ON scores
    FOR EACH ROW EXECUTE FUNCTION scores_locked();
CREATE TRIGGER score_items_lock BEFORE INSERT OR UPDATE OR DELETE ON score_items
    FOR EACH ROW EXECUTE FUNCTION scores_locked();

-- Settings the organizer controls.
ALTER TABLE events ADD COLUMN assignment_seed int NOT NULL DEFAULT 2026;
ALTER TABLE events ADD COLUMN created_by text REFERENCES users ON DELETE SET NULL;

-- Moderation after the deadline: besides withdraw / mark duplicate, an organizer may restore a
-- withdrawn entry. Still status-only: the content cannot change once submissions close.
CREATE OR REPLACE FUNCTION submission_window() RETURNS trigger AS $$
DECLARE e events%ROWTYPE;
BEGIN
    IF current_setting('dogfood.importing', true) = 'on' THEN RETURN NEW; END IF;
    IF TG_OP = 'UPDATE' AND (NEW.title, NEW.summary, NEW.repo_url, NEW.track_id, NEW.team_id)
                         IS NOT DISTINCT FROM (OLD.title, OLD.summary, OLD.repo_url, OLD.track_id, OLD.team_id)
       AND (NEW.status IN ('withdrawn', 'duplicate') OR (OLD.status = 'withdrawn' AND NEW.status = 'submitted')) THEN
        RETURN NEW;
    END IF;
    SELECT * INTO e FROM events WHERE id = NEW.event_id;
    IF now() >= e.closes_at OR (e.opens_at IS NOT NULL AND now() < e.opens_at) THEN
        RAISE EXCEPTION 'submissions for % are closed', e.name USING ERRCODE = 'DF001';
    END IF;
    RETURN NEW;
END $$ LANGUAGE plpgsql;
