# Dog-Food

A self-hostable hackathon submission and judging portal for [DOGFOOD 2026](https://dogfoodhack.com).

**Status: work in progress (build window 26–29 Sep 2026).** This README grows with each stage.

## Run

```bash
docker compose up
```

Open http://localhost:8080. On first boot the portal creates its schema and loads the published
`fixtures.json`. In demo mode every seeded account has the password `dogfood`
(e.g. `organizer@dogfood.local`, `priya1@example.org`).

## Try it

| who | log in as | where |
|-----|-----------|-------|
| organizer | `organizer@dogfood.local` | **Organize**: progress, judges, rubric, planner, results preview, publish, audit log |
| judge | `diego.herrera@example.org` | **Judging**: assigned projects and the score form |
| participant | `priya1@example.org` | the event page: team, invite link, entry |
| admin | `admin@dogfood.local` | everything |

Password for all of them: `dogfood` (demo mode only). New people can sign up at `/signup`.

## Acceptance checker

```bash
python3 run.py .dogfood.toml
```

`run.py` and `fixtures.json` are unmodified copies of the organisers' files.

## Tests

```bash
docker compose exec app pytest -q
```

Tests run against a separate `dogfood_test` database and start a real server.

## Progress

- [x] Stage 1: schema, Docker, fixture import, login, public gallery, entries with a database-enforced deadline (T1 checks pass)
- [x] Stage 2: judging: rubric with weights, assignments, scores, backend score isolation, CSV export (all 7 checks pass)
- [x] Stage 3: organizer dashboard, teams with invite links, judge invitations, assignment planner, judge queue, normalized results, publishing (see [JUDGING.md](JUDGING.md))
- [x] Stage 4: public voting (accounts or one-time email links), sealed tallies, personal ballot order, comments with moderation, rate limits (T3)
- [x] Stage 5: public API with cursor paging and offline docs, personal tokens, signed webhooks with retries, Ed25519 certificates with an offline verifier, embeddable widget, validated import/export (T4)
- [ ] Stage 6: tests, documentation, demo video

License: MIT.
