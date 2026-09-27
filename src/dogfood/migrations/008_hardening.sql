-- 008: fixes from an adversarial review.

-- One person, one vote budget. A voter is identified by their email address (keyed hash), whether
-- they vote with an account or with an email link, so the two paths meet in the same row. An account
-- voter now carries both user_id and email_key.
ALTER TABLE voters DROP CONSTRAINT voters_check;
ALTER TABLE voters ADD CONSTRAINT voters_identity CHECK (user_id IS NOT NULL OR email_key IS NOT NULL);

-- Team names are unique per event, case-insensitively, in the database too (the app checked first,
-- but two simultaneous requests could both pass that check).
CREATE UNIQUE INDEX teams_name_per_event ON teams (event_id, lower(name));
