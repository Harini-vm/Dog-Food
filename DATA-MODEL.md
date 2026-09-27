# Data model

The schema is plain SQL in `src/dogfood/migrations/` (001 to 006), applied in order at boot. This
file explains each table and **why** it looks the way it does. Every table here is used; one that
was not (`prizes`) was removed in 006.

## Overview

```
users ─┬─ sessions, api_tokens, invitations
       └─ memberships (event, user, role)            role ∈ organizer | judge | participant; admin = users.is_admin
events ─┬─ tracks
        ├─ criteria                                   the rubric: key, weight, min, max
        ├─ teams ── team_members ── users
        ├─ projects (team, track, status, duplicate_of)
        ├─ judge_tracks, conflicts
        ├─ assignments ── scores ── score_items ── criteria
        ├─ results (frozen snapshots) ── certificates
        ├─ voters ── votes, voter_links, voter_sessions
        ├─ comments
        └─ webhooks ── webhook_deliveries ── webhook_events
audit_log (all events), outbox (mail)
```

## Identity and roles

| table | purpose | notes |
|---|---|---|
| `users` | one row per person | `email` unique and lowercase (a CHECK constraint enforces it). `password_hash` is scrypt and NULL until the person activates the account. `is_admin` for the platform admin |
| `sessions` | browser logins | only the **sha256 of the cookie** is stored, plus a per-session CSRF token. Server-side, so logout really ends the session |
| `api_tokens` | bearer tokens | sha256 digest only; revocable. Demo tokens are labelled `demo:*` and revoked at boot when demo mode is off |
| `invitations` | one-time links to activate a judge or organizer account | digest only, 14 days, single use |
| `memberships` | role **per event** | PK `(event_id, user_id, role)`. A judge in one event can be a participant in another. "Visitor" is simply not logged in |

Why roles are per event: a real platform hosts many events. Being an organizer of one must give no
rights in another (`test_other_organizers_are_locked_out`).

## Events, teams, entries

| table | purpose | notes |
|---|---|---|
| `events` | an event and its settings | submission window (`opens_at`, `closes_at`), judging (`judging_closes_at`, `reviews_per_project`, `assignment_seed`), voting (`voting_opens_at`, `voting_closes_at`, `votes_per_voter`), `comments_open`, `results_published_at` |
| `tracks` | categories | `UNIQUE (id, event_id)` so other tables can reference a track *in the same event* |
| `teams` | a team in one event | `invite_code` unique; rotating it kills old links |
| `team_members` | people on a team | `UNIQUE (event_id, user_id)`: one team per person per event. `captain` flag |
| `projects` | an entry | `status ∈ draft, submitted, withdrawn, duplicate`. `duplicate_of` must be set exactly when status is duplicate (CHECK). One live entry per team (partial unique index). Deadline trigger |

**Composite foreign keys.** `projects (team_id, event_id) → teams (id, event_id)` and similar. A
single-column FK would allow a project in event A to point at a team in event B. The pair makes
that impossible. The same pattern is used for tracks, assignments, scores, votes and comments.

**Statuses are never deleted.** A withdrawn or duplicate entry stays, with its reviews, so the CSV
export and the audit trail stay complete. Fixture project `prj_41` (second entry of team `tm_07`) is
imported as `duplicate` of `prj_07`.

## Judging

| table | purpose | notes |
|---|---|---|
| `criteria` | rubric rows | `weight numeric > 0` (relative), `min_score < max_score`, `UNIQUE (event_id, key)` |
| `judge_tracks` | tracks a judge covers | no rows = any track |
| `conflicts` | judge ↔ team pairs that must never meet | filled automatically when a judge is on a team |
| `assignments` | "judge J reviews project P" | `UNIQUE (judge_id, project_id)`, `batch` = import, manual or a planner batch id |
| `scores` | one review | `assignment_id NOT NULL UNIQUE`: **a score cannot exist without an assignment, and there is at most one per assignment** |
| `score_items` | one value per criterion | range checked by trigger against the criterion's own min/max |

Why scores are split into `scores` + `score_items`: the rubric is data, not columns. An organizer
can add a criterion without a schema change, and old reviews stay readable.

`results` holds published snapshots: `(event_id, version)`, the public JSON body, and its sha256.
Snapshots are never updated. A retraction only sets `retracted_at`, once. That is why published
rankings cannot silently change.

## Public participation

| table | purpose | notes |
|---|---|---|
| `voters` | who votes | either `user_id` (an account) or `email_key` (HMAC of the email with the server secret), never both (CHECK). **Plain emails of email voters are not stored** in this table |
| `voter_links` | one-time links sent by email | digest only, 24 h, single use |
| `voter_sessions` | the short session a link opens | digest + its own CSRF token |
| `votes` | one vote | PK `(voter_id, project_id)`. There is **no counter column anywhere**: budgets and tallies are counted from this table, so nothing can drift |
| `comments` | public comments | `char_length(body) BETWEEN 1 AND 2000` (characters, not bytes). Hidden, not deleted, with who and why |
| `outbox` | outgoing mail | written in the same transaction as the action. The only place an email voter's address appears, so the link can be sent |

## Integrations

| table | purpose | notes |
|---|---|---|
| `webhooks` | receivers per event | the secret is kept in clear because it is needed to sign; it is shown to the organizer once |
| `webhook_events` | the outbox of notifications | `sequence` (bigserial) orders them; `id` is stable across retries; `body` is the exact signed bytes |
| `webhook_deliveries` | one row per (receiver, event) | status, attempts, next attempt, last error |
| `certificates` | signed records | `record` (JSON), `canonical` (the signed bytes), `signature`, `key_id`. Never updated (trigger) |

## Audit

`audit_log` is append-only (trigger) and **hash-chained**. Each row stores the previous row's hash
and a hash over its own content. `audit.verify` walks the chain, and the organizer's audit page shows
whether it is intact. Editing any row outside the app breaks the chain from that row on. Deliberately
not logged: score values and individual votes, since organizers read this log while judging and
voting are open.

## Import format

`fixtures.json` is treated as *input*, not as our model. The importer's decisions:

- No weights in the file: defaults 0.40 / 0.35 / 0.25.
- Duplicate team names: the team id is appended.
- Two live entries from one team: the earliest stays, the others become `duplicate`.
- Each fixture review becomes an assignment plus a score.

`GET /api/v1/events/{id}/export` writes a superset of that format, with statuses, weights,
assignments and settings. Importing an export gives the same event back
(`test_export_import_export_round_trip`).
