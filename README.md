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
- [ ] Stage 3: organizer and judge interfaces, assignment, normalization
- [ ] Stage 4: public voting, comments, anti-abuse (T3)
- [ ] Stage 5: API, webhooks, certificates, widget, import/export (T4)
- [ ] Stage 6: tests, documentation, demo video

License: MIT.
