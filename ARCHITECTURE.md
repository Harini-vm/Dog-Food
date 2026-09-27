# Architecture

DOGFOOD is one Python web application and one PostgreSQL database, started with
`docker compose up`. There is no JavaScript framework, no message broker, no cache server and
nothing loaded from the internet at runtime.

```
 browser ──HTML forms──▶ ┌──────────────────────────────┐        ┌───────────────────┐
 scripts ──JSON + token─▶│ FastAPI app (uvicorn, :8080) │──SQL──▶│ PostgreSQL 16     │
 checker ──JSON + token─▶│  routes → authz → services   │◀───────│ rules as triggers │
                         │  webhook sender (thread) ────┼──POST──▶ receivers          │
                         └──────────────────────────────┘        └───────────────────┘
                                   │ /data volume: Ed25519 signing key
```

## Why these choices

| choice | reason |
|---|---|
| Python 3.12 + FastAPI | One language, readable, typed request models, OpenAPI for free |
| Server-rendered HTML (Jinja) | Works without JavaScript, is fast on slow networks, is accessible by default. The only script is the API docs page (a local file) |
| PostgreSQL | Real transactions and row locks for deadlines, budgets and races. Triggers enforce rules that must survive bugs |
| Plain SQL (psycopg 3), no ORM | Every query is visible and reviewable. The schema is the migrations, nothing is generated |
| One process | Simple to run and reason about. The webhook sender is a thread in it and uses `SKIP LOCKED`, so a second process would also be safe |

## Layers

```
src/dogfood/
  app.py            wiring: startup (migrate, seed, webhook thread), error rendering, security headers
  auth.py           who is calling: bearer token or session cookie; CSRF for cookie writes
  authz.py          what they may do: roles per event, score_scope (the one gate for score reads)
  routes/           HTTP only: parse input, call authz + a service, render HTML or JSON
    public.py       home, event, gallery, project, entry form, login, results
    people.py       signup, invitations, teams
    judge.py        judge queue and score form
    organize.py     organizer dashboard, settings, rubric, judges, planner, publish, audit
    vote.py         voting, voting links, comments (+ their API)
    api.py          judging API, CSV export
    integrations.py public API, tokens, webhooks, certificates, widget, import/export, API docs
  services/         the rules, one module per domain; every function takes the open transaction
    importer.py submissions.py teams.py events.py assign.py scoring.py results.py
    voting.py comments.py webhooks.py certificates.py
  migrations/       001..006 SQL files, applied in order at boot under an advisory lock
  templates/ static/
```

Rules for the code:

1. **Routes never decide permissions by themselves.** Every protected read or write goes through
   `authz` before any query. The UI hides buttons for convenience only. `tests/test_isolation.py`
   and `tests/test_cross_feature.py` attack the API directly.
2. **One request = one transaction** (`db.tx()`). A request either changes everything it meant to
   (including its audit line and webhook) or nothing.
3. **Rules that matter are also in the database.** The application checks first to give a friendly
   message. The database refuses anyway if a code path forgets:

| rule | where |
|---|---|
| no entry changes after the deadline | trigger `projects_window` (SQLSTATE DF001) |
| one live entry per team | unique partial index `one_live_entry_per_team` |
| scores within the criterion's scale | trigger `score_items_range` (DF002) |
| a score needs an assignment | `scores.assignment_id NOT NULL UNIQUE` |
| published results never change | trigger `results_no_rewrite` (DF003) |
| scores locked once results are published | triggers `scores_lock`, `score_items_lock` (DF004) |
| votes only while voting is open | trigger `votes_in_window` (DF005) |
| one vote per voter per project | primary key `(voter_id, project_id)` |
| certificates never change | trigger `certificates_no_rewrite` (DF006) |
| audit log is append-only | trigger `audit_no_rewrite` (DF009) + hash chain |
| rows never cross events | composite foreign keys `(id, event_id)` |

   Any database rule violation becomes a 4xx with a JSON error, never a 500 (`app.py`).
4. **Time comes from the database clock** (`now()` inside the transaction), not from the client and
   not from Python. At the deadline there is one decision, and it is made once.

## Request flow: a judge saves a score

1. `auth.guard` resolves the caller from the bearer token or cookie. A wrong token gives 401. A
   cookie write without the CSRF token gives 403.
2. `routes/api.put_score` opens a transaction and calls `scoring.save`.
3. `scoring.save` locks the assignment, checks it belongs to this judge and that they are *still*
   a judge. It takes `FOR SHARE` on the event, so publishing cannot slip in between. It checks
   that judging is open and validates every criterion.
4. It writes the score and its items (the database re-checks range and lock), then an audit line
   that says a score changed, without the values.
5. Commit. If anything raised, nothing was written.

## Concurrency, briefly

| situation | mechanism |
|---|---|
| 20 votes at once with 3 left | the voter row is locked `FOR UPDATE`, and the budget is counted from `votes` |
| two people take the last team seat | the team row is locked `FOR UPDATE` before counting members |
| publish while a score or vote is in flight | publish takes `FOR UPDATE` on the event; scores and votes take `FOR SHARE` |
| two organizers run the planner | transaction-scoped advisory lock per event |
| two app processes send webhooks | `FOR UPDATE SKIP LOCKED`, one delivery per transaction |
| migrations on concurrent boots | session advisory lock 4201 |

## Outside the request path

- **Webhooks** are written to `webhook_events` and `webhook_deliveries` inside the business
  transaction (outbox pattern). A background thread sends them with a 5 s timeout and retries 6
  times with backoff (details in `services/webhooks.py`).
- **Mail** (voting links) goes to the `outbox` table. No mail server is required. In demo mode the
  link is shown on screen.

## Interface

Server-rendered pages with one stylesheet (`static/site.css`). The visual language comes from the
judging table:

- **Surfaces:** paper, scorecard and well. Depth is hairline rules only, never shadows.
- **Colour:** one ink-blue accent for actions. Colour otherwise carries meaning only: stamp red for
  refused or closed, seal green for locked or verified, highlighter yellow for "look at this".
- **The Seal** (`templates/_ui.html`) marks everything the system has locked, verified or refused:
  a closed deadline, published results (with their SHA-256), a signed certificate, an intact audit
  chain. It shows what the database guarantees.
- **Fonts:** IBM Plex Sans and Mono are bundled in `static/fonts` (SIL Open Font License), so
  nothing loads from the internet. Mono with tabular figures is used for every score, rank, hash
  and id.
- **Organizer workspace:** a left rail grouped Run · Judge · Publish · Connect. A timeline strip
  shows which phase the event is in (Submissions → Judging → People's choice → Results).
- **Judge scorecard:** proportional weight bars per criterion. A small local script
  (`static/scorecard.js`) shows the live weighted total using the same formula as
  `results.review_value`. The form works without it.
- **Accessibility:** light and dark follow the system setting. Targets are at least 44 px, focus is
  visible, and the layout has no horizontal scroll at 390 px. Reduced motion is respected.

## What we left out, on purpose

- No ORM, no SPA, no Redis. Each would add a moving part without adding a guarantee.
- Rate limits live in process memory (`ratelimit.py`). Several app processes would each count
  separately, which makes limits looser, never stricter. For one self-hosted event this is the right
  trade. THREAT-MODEL.md says what that costs.
- Sessions are server-side rows (revocable), not JWTs.
