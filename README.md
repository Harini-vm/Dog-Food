# Dog-Food

A self-hostable hackathon platform: submissions, fair judging, public voting and signed results.
Built for [DOGFOOD 2026](https://dogfoodhack.com) between 26 and 29 September 2026.

```bash
docker compose up --build
```

Open **http://localhost:8080**. On first boot the portal creates its schema, loads the published
`fixtures.json` (41 projects, 40 teams, 30 judges, 126 reviews) and prints demo credentials. No
internet connection is needed at runtime: no CDN, no external fonts or scripts.

## Try it

Password for every seeded account: `dogfood` (demo mode only).

| who | log in as | where to look |
|-----|-----------|---------------|
| organizer | `organizer@dogfood.local` | **Organize** → the fixture event: progress, judges, rubric, planner, results preview, publish, certificates, webhooks, audit log |
| judge | `diego.herrera@example.org` | **Judging**: assigned projects and the score form |
| participant | `priya1@example.org` | the event page: team, invite link, entry |
| admin | `admin@dogfood.local` | everything, plus **Import an event** |

New people can sign up at `/signup`. The API reference is at `/api/docs`.

## Acceptance checker

```bash
python run.py .dogfood.toml
```

`run.py` and `fixtures.json` are unmodified copies of the organisers' files. All 7 checks pass:
`claimed T1 T2 T3 T4, verified T1 T2`. The checker has no tests for T3 and T4, so it prints them as
"claimed but not verified". The evidence for those tiers is
[docs/edge-cases.md](docs/edge-cases.md): every case with its policy and the test that proves it.
Our own run is in `acceptance-report.txt`.

## Tests

```bash
docker compose exec app pytest -q
```

96 tests against a real server and a real Postgres (a separate `dogfood_test` database), including
concurrency races, direct API attacks on every UI-only restriction, webhook retries against a live
receiver, and certificate verification with the offline tool.

## What it does

**T1: core**
- Events with a submission window, tracks, teams with invite links, drafts and submissions.
- The deadline is enforced by the database.
- One live entry per team, so the fixture's duplicate entry (prj_41) is detected.
- Public gallery with search and filters.

**T2: judging**
- Weighted rubric (organizer-editable) and judge invitations with track limits.
- Conflict-of-interest rules and a deterministic assignment planner.
- Judge queue with a 1–5 score form. Strict score privacy: a peer judge gets 403.
- CSV export and a live progress dashboard.

**Fair results** ([JUDGING.md](JUDGING.md))
- Per-judge normalization with shrinkage and a project prior; ties share a rank.
- Flags for flat scorers (jdg_07), split panels and under-reviewed projects.
- Publishing freezes a SHA-256 snapshot and locks scores in the database.
- Retracting needs a reason.

**T3: public**
- People's-choice voting by account or one-time email link, with a budget per voter.
- Sealed tallies until close and a personal ballot order.
- No voting for your own team. Race-safe.
- Comments with moderation.
- Rate limits that don't lock out a shared Wi-Fi.
- Hash-chained audit log with a verification page.

**T4: integrations**
- REST API with cursor paging and offline docs, and personal tokens.
- Signed webhooks (HMAC, stable ids, ordering, retries).
- Ed25519-signed certificates that verify offline (`tools/verify_record.py`).
- Embeddable widget.
- Validated import and export that round-trips.

## Documentation

| file | what |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | components, layers, where each rule is enforced, concurrency |
| [DATA-MODEL.md](DATA-MODEL.md) | every table and why it looks that way |
| [JUDGING.md](JUDGING.md) | the judging maths with real numbers from the fixture |
| [THREAT-MODEL.md](THREAT-MODEL.md) | attackers, defenses, accepted risks, production settings |
| [docs/edge-cases.md](docs/edge-cases.md) | policies and tests for every edge case |
| [DEMO.md](DEMO.md) | script of the 5-minute demo video |

Demo video: **(link added after recording)**

## Configuration

Environment variables (see `src/dogfood/config.py`):

| variable | default | meaning |
|---|---|---|
| `DOGFOOD_DEMO` | `true` | fixed demo tokens and password; **set to false for real use** |
| `DOGFOOD_SECRET_KEY` | `change-me` in compose | keys voter hashes; set a long random value |
| `DOGFOOD_SECURE_COOKIES` | `false` | set `true` behind HTTPS |
| `DOGFOOD_TRUST_PROXY` | `false` | honour `X-Forwarded-For` (only behind your own proxy) |
| `DOGFOOD_SEED` | `true` | import `fixtures.json` on an empty database |
| `DOGFOOD_WEBHOOK_ALLOW_PRIVATE` | `false` | allow webhook receivers on private addresses (local testing) |

Offline use: `docker compose up` needs the `python:3.12-slim` and `postgres:16-alpine` images and the
Python packages once. After the first build it runs with the network off.

## Build log

The project was built in stages inside the hackathon window, one commit per stage:

1. Schema, Docker, fixture import, login, gallery, entries with a database-enforced deadline.
2. Judging: rubric, assignments, scores, score isolation, CSV export.
3. Organizer and judge interfaces, teams, invitations, planner, normalized results, publishing.
4. Public voting, email links, sealed tallies, comments, rate limits (T3).
5. API, tokens, webhooks, certificates, widget, import/export (T4).
6. Documentation, cross-feature tests, cleanup.
7. Interface: paper-and-ink design system, the Seal, organizer rail and timeline, judge scorecard, bundled IBM Plex fonts (SIL OFL).

License: MIT.
