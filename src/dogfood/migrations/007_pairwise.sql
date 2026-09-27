-- 007: pairwise mode. Judges compare two entries head to head; a Bradley-Terry model turns the
-- answers into a second, independent ranking that checks the rubric ranking.

CREATE TABLE comparisons (
    id          text PRIMARY KEY,
    event_id    text NOT NULL,
    judge_id    text NOT NULL REFERENCES users ON DELETE CASCADE,
    project_a   text NOT NULL,
    project_b   text NOT NULL,
    winner      text CHECK (winner IN ('a', 'b', 'tie')),   -- NULL until the judge answers
    created_at  timestamptz NOT NULL DEFAULT now(),
    decided_at  timestamptz,
    CHECK (project_a < project_b),                           -- one canonical order per pair...
    UNIQUE (judge_id, project_a, project_b),                 -- ...so a judge never gets the same pair twice
    FOREIGN KEY (project_a, event_id) REFERENCES projects (id, event_id) ON DELETE CASCADE,
    FOREIGN KEY (project_b, event_id) REFERENCES projects (id, event_id) ON DELETE CASCADE,
    CHECK ((winner IS NULL) = (decided_at IS NULL))
);
CREATE INDEX comparisons_judge ON comparisons (judge_id) WHERE winner IS NULL;

-- Like scores: locked once results are published (same trigger function as 003, SQLSTATE DF004).
CREATE TRIGGER comparisons_lock BEFORE INSERT OR UPDATE OR DELETE ON comparisons
    FOR EACH ROW EXECUTE FUNCTION scores_locked();
