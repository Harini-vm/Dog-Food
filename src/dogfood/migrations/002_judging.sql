-- 002: judging (tier 2). Rubric, who judges what, and the scores themselves.

-- The rubric. Weights are relative: 2/1/1 and 0.5/0.25/0.25 are the same rubric.
CREATE TABLE criteria (
    id        text PRIMARY KEY,
    event_id  text NOT NULL REFERENCES events ON DELETE CASCADE,
    key       text NOT NULL CHECK (key ~ '^[a-z][a-z0-9_]*$'),
    name      text NOT NULL,
    weight    numeric(6,3) NOT NULL CHECK (weight > 0),
    min_score int NOT NULL DEFAULT 1,
    max_score int NOT NULL DEFAULT 5,
    position  int NOT NULL DEFAULT 0,
    UNIQUE (event_id, key),
    CHECK (min_score < max_score)
);

-- Tracks a judge covers. No rows = any track.
CREATE TABLE judge_tracks (
    event_id text NOT NULL,
    user_id  text NOT NULL REFERENCES users ON DELETE CASCADE,
    track_id text NOT NULL,
    PRIMARY KEY (user_id, track_id),
    FOREIGN KEY (track_id, event_id) REFERENCES tracks (id, event_id) ON DELETE CASCADE
);

-- A judge who is on a team never reviews it.
CREATE TABLE conflicts (
    event_id text NOT NULL,
    judge_id text NOT NULL REFERENCES users ON DELETE CASCADE,
    team_id  text NOT NULL,
    reason   text NOT NULL,
    PRIMARY KEY (judge_id, team_id),
    FOREIGN KEY (team_id, event_id) REFERENCES teams (id, event_id) ON DELETE CASCADE
);

CREATE TABLE assignments (
    id          text PRIMARY KEY,
    event_id    text NOT NULL,
    judge_id    text NOT NULL REFERENCES users ON DELETE CASCADE,
    project_id  text NOT NULL,
    batch       text NOT NULL DEFAULT 'manual',
    assigned_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (judge_id, project_id),
    UNIQUE (id, event_id),
    FOREIGN KEY (project_id, event_id) REFERENCES projects (id, event_id) ON DELETE CASCADE
);

-- A score belongs to exactly one assignment: nobody can score what they were not given.
CREATE TABLE scores (
    id            text PRIMARY KEY,
    assignment_id text NOT NULL UNIQUE,
    event_id      text NOT NULL,
    judge_id      text NOT NULL REFERENCES users ON DELETE CASCADE,
    project_id    text NOT NULL,
    comment       text NOT NULL DEFAULT '' CHECK (length(comment) <= 5000),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (assignment_id, event_id) REFERENCES assignments (id, event_id) ON DELETE CASCADE,
    FOREIGN KEY (project_id, event_id) REFERENCES projects (id, event_id) ON DELETE CASCADE
);
CREATE INDEX scores_judge ON scores (judge_id);

CREATE TABLE score_items (
    score_id     text NOT NULL REFERENCES scores ON DELETE CASCADE,
    criterion_id text NOT NULL REFERENCES criteria ON DELETE CASCADE,
    value        int NOT NULL,
    PRIMARY KEY (score_id, criterion_id)
);

CREATE FUNCTION score_in_range() RETURNS trigger AS $$
DECLARE c criteria%ROWTYPE;
BEGIN
    SELECT * INTO c FROM criteria WHERE id = NEW.criterion_id;
    IF NEW.value NOT BETWEEN c.min_score AND c.max_score THEN
        RAISE EXCEPTION '% must be between % and %', c.name, c.min_score, c.max_score USING ERRCODE = 'DF002';
    END IF;
    RETURN NEW;
END $$ LANGUAGE plpgsql;
CREATE TRIGGER score_items_range BEFORE INSERT OR UPDATE ON score_items
    FOR EACH ROW EXECUTE FUNCTION score_in_range();

-- When judging ends. NULL = open until results are published.
ALTER TABLE events ADD COLUMN judging_closes_at timestamptz;
ALTER TABLE events ADD COLUMN reviews_per_project int NOT NULL DEFAULT 3 CHECK (reviews_per_project BETWEEN 1 AND 20);
ALTER TABLE events ADD COLUMN results_published_at timestamptz;
