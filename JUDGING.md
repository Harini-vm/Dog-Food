# How judging works

This document explains every step from "a judge clicks 4" to "a project is ranked 1st", with real
numbers from the published `fixtures.json`. The code lives in `src/dogfood/services/`:
`assign.py`, `scoring.py` and `results.py`. The same numbers are on the organizer's results preview
(`/organize/<event>/results`).

## 1. The rubric

Each event has criteria with a weight and a scale (default 1 to 5). The fixture has no weights, so
the importer applies documented defaults and reports them:

| criterion     | weight | why |
|---------------|--------|-----|
| functionality | 0.40   | "does it work" is the one thing a demo proves |
| quality       | 0.35   | code, docs, tests: what the organizers asked for |
| innovation    | 0.25   | matters, but is the easiest to over-reward |

Weights are relative: 2/1/1 and 0.5/0.25/0.25 mean the same thing. Organizers can change weights
and names any time before results are published; the preview updates at once. The **scale** cannot
change after the first review, because old and new reviews would then be on different scales.
Criteria cannot be added after the first review either, because old reviews would be incomplete.

## 2. Who reviews what

A score can only exist for an **assignment** (`scores.assignment_id` is `NOT NULL UNIQUE`). A judge
cannot score a project nobody gave them, and cannot score it twice.

The planner (`assign.plan`) is greedy and deterministic.

Hard rules:

- A judge never reviews a team they are on. The `conflicts` table is filled automatically when a
  judge is also a team member.
- A judge limited to tracks only gets projects in those tracks. A judge with no tracks set takes any
  track.
- A judge gets a given project at most once. `UNIQUE (judge_id, project_id)` enforces this in the
  database.

Order:

1. **Hardest project first**: projects with the fewest eligible judges are placed first. Otherwise
   easy projects could use up the only judge a hard one could have had. This case has its own test:
   `test_plan_hardest_first`.
2. For each seat, pick the eligible judge with the **lowest current load**. Ties are broken by a
   shuffle seeded with the event's `assignment_seed`. The same inputs always give the same plan, so
   a disputed assignment can be reproduced.
3. **Existing assignments are kept and counted.** Running the planner again only fills gaps. This
   makes it safe after a late judge joins or a judge is removed; a removed judge's *unscored* work
   goes back to the pool.
4. Projects that cannot reach the target are reported as shortfalls, not silently skipped.

On the fixture, the 126 imported reviews become 126 assignments. With the target of 3 reviews, the
planner then adds 8 assignments for the projects that only had 2.

## 3. One review becomes one number (0–100)

Each criterion is rescaled to 0..1 by its own min and max. The rescaled values are combined with the
weights and multiplied by 100:

```
value = 100 * Σ w_c * (v_c - min_c) / (max_c - min_c)  /  Σ w_c
```

Example: project prj_10 "Still Beacon" has two reviews.

| judge  | functionality | quality | innovation | value |
|--------|---------------|---------|------------|-------|
| jdg_15 | 4 → 0.75      | 5 → 1.0 | 5 → 1.0    | 100 × (0.40×0.75 + 0.35×1 + 0.25×1) = **90.0** |
| jdg_29 | 5 → 1.0       | 4 → 0.75| 2 → 0.25   | 100 × (0.40×1 + 0.35×0.75 + 0.25×0.25) = **72.5** |

The raw average is 81.25. The 1–5 values themselves are in the CSV export and are never altered.

## 4. Correcting for strict and lenient judges

Across the fixture, judges' own averages range from 25 to 81 on the same kind of projects. If we
averaged raw values, a project's rank would depend on *who happened to review it*.

We use a per-judge z-score, **shrunk toward the whole panel**. The shrinking matters because a judge
with one or two reviews tells us very little about their habits:

```
M, S         panel mean and standard deviation over all counted reviews
n, m, v      this judge's review count, mean and variance
m' = (n·m + K·M) / (n + K)            judge mean, pulled toward the panel
s' = sqrt((n·v + K·S²) / (n + K))     judge spread, pulled toward the panel
adjusted = M + S · (value - m') / s'
```

`K = 3`: a judge's own habits count fully only once they have clearly more than 3 reviews.

Fixture panel: M = 63.97, S = 16.86, 122 counted reviews.

| judge          | reviews | own mean | m' (shrunk) | shift = M - m' |
|----------------|---------|----------|-------------|----------------|
| Wei Lindqvist  | 6       | 81.25    | 75.49       | **−11.52** (lenient: pulled down) |
| Rafa Okonkwo   | 4       | 77.81    | 71.88       | −7.91 |
| Leila Nasser   | 2       | 49.38    | 58.13       | +5.84 |
| Tomas Varga    | 1       | 25.00    | 54.23       | **+9.74**, not +39: one review is weak evidence |

The last row is why we shrink. Plain z-scores would decide, from a single review, that Tomas is
extremely harsh. They would then give that one project a huge boost.

## 5. From reviews to a project score

```
score = (Σ adjusted + C·M) / (n + C)      with C = 1
```

This adds one "average review" to every project. A project with two enthusiastic reviews should not
beat a project with five solid ones. On the fixture, prj_10 (2 reviews, raw 81.25) moves from raw
rank 3 to rank **5**. prj_33 "Slow Trail" (3 reviews by stricter judges) moves from raw rank 6 to
rank **3**. The organizer preview shows every such move (▲/▼) next to the raw rank. This way the
correction is visible, not hidden.

## 6. Ranking and ties

Projects are sorted by score. Scores equal to two decimals **share a rank** (1, 2, 2, 4). We do not
invent a tie-breaker such as submission time, because nobody agreed to it. Ranks are also computed
within each track.

These projects are never ranked:

- withdrawn and duplicate entries, such as fixture prj_41, a second entry from team tm_07. Their
  reviews stay in the CSV export.
- projects with no complete review. They are listed as "unranked", never dropped silently.

## 7. Flags for the organizers

The preview and dashboard flag the following. Flags never change a score; a person decides.

| flag | rule | fixture example |
|------|------|-----------------|
| flat judge | ≥ 3 reviews with spread < 2 points (0–100 scale) | **jdg_07 Iva Petrova**: 4/4/4 on all three projects |
| straight line | ≥ 3 reviews, every criterion equal in every review | — |
| split panel | adjusted reviews of one project differ by > 40 points | 7 projects |
| under-reviewed | fewer reviews than the event's target | 8 projects with 2 of 3 |

A flat judge needs no special handling in the maths. Their spread is near zero, so their reviews
land near the panel mean and barely move any ranking. The flag tells the organizer why that judge's
opinion "didn't count".

## 8. Privacy while judging

- A judge sees only their own assignments and scores. `authz.score_scope` is the single function
  every score read goes through. Another judge's scores return **403**, not an empty list, so a
  judge cannot even learn whether a peer has started.
- Organizers see the scores of events they organize, and no others. Participants see no scores.
- The audit log records *that* a score was saved, never the values, because organizers read the log
  during judging.
- The public results contain rank, score and review count only. Judge names, individual reviews and
  flags are never published.

## 9. Publishing

Publishing is allowed after submissions close. If projects are missing reviews, the organizer must
confirm explicitly; the audit log records `forced: true`.

1. A snapshot of the public ranking is written to `results` with its SHA-256. The JSON and the hash
   are served at `/api/v1/events/<event>/results`.
2. `events.results_published_at` is set, which locks all scores for the event:
   - the application refuses edits (`judging_closed`);
   - a database trigger refuses them for any code path that forgets to check (SQLSTATE `DF004`).
3. The snapshot itself cannot be edited or deleted (trigger, `DF003`). Recomputing later, with new
   weights for example, cannot silently change what people already saw.

**Retracting** needs a written reason, which goes in the audit log. It marks the snapshot as
retracted, never deletes it, and reopens judging. Publishing again creates version 2. Every version
stays in the database.

## 10. Limits we know about

- With very few reviews per judge, no method can fully separate "strict judge" from "judge who got
  weak projects". Shrinkage makes the correction cautious rather than confident.
- The flags use fixed thresholds (2 points, 40 points). They are documented constants in
  `results.py`, not tuned per event.
