# Demo video script (5 minutes, one full event lifecycle)

Record at 1280×720 or larger. Before recording, run `docker compose down -v` and then
`docker compose up --build`, so the data is fresh. Use two browser windows: one normal, one private.
Times are targets.

## 0:00 – 0:20 Intro

"This is DOGFOOD, a self-hosted hackathon portal. One `docker compose up`, no internet needed.
I'll run one event from creation to published, signed results."

Show the terminal with `docker compose up` and the banner, then http://localhost:8080.

## 0:20 – 1:10 Create the event (organizer)

1. Log in as `organizer@dogfood.local` / `dogfood`. Go to **Organize**.
2. Create "Demo Hack": submissions close in 10 minutes, tracks `AI, Climate`, 2 reviews per project.
3. On the dashboard, point at the rubric (40/35/25 by default, weights editable) and the settings.
4. Invite a judge: `judge.demo@example.org` (track: AI). Copy the one-time link from the message.

## 1:10 – 2:00 Teams and submissions (participants)

1. In the private window: **Sign up** as a new person. Open Demo Hack and create a team.
2. Show the invite link, then **Start the entry**, fill it in and **Submit**. It appears in the
   public **Gallery**.
3. Say: "After the deadline the database itself refuses edits." (Optional: run
   `python run.py .dogfood.toml` in the terminal and show 7 PASS.)

## 2:00 – 3:10 Judging

1. In the private window, open the judge invitation link and set a password. You land on
   **My reviews**.
2. (Organizer window) Click **Run the assignment planner**. "Hardest projects first, least-busy
   judge, never your own team; running it again only fills gaps."
3. (Judge window) Score a project with the 1–5 buttons and **Save and next**.
4. Show privacy: in the terminal,
   `curl -H "Authorization: Bearer df_judge_b_demo" localhost:8080/api/v1/judges/jdg_24/scores`
   gives **403**. "Judges only ever see their own scores, enforced on the server."
5. On the fixture event (`sample-hack-2026`), open **Full preview**. Point at:
   - jdg_07 flagged as a **flat scorer**;
   - the ▲/▼ column (what normalization changed);
   - prj_41 not ranked (duplicate).

   "JUDGING.md explains every number."

## 3:10 – 4:10 Publish

1. For time, use the fixture event: its deadline has already passed. Click **Publish results**
   (tick "publish anyway": some projects have only 2 reviews).
2. Open the public results page: rank, score, reviews, and the snapshot's SHA-256.
3. Try to change a score as a judge: refused. "Published results are frozen, in the app and in the
   database."
4. Click **Issue signed certificates** and open one: signature valid. In the terminal, run
   `python tools/verify_record.py record.json keys.json` and show "VALID".

## 4:10 – 4:45 Public side and integrations (quick tour)

- **People's choice**: sealed counts, a personal ballot order, email voting links.
- **Webhooks**: signed deliveries with retries.
- **Embed code** on the event page.
- **API docs** at `/api/docs`, which works offline.
- **Audit log**: hash chain verified.

## 4:45 – 5:00 Close

"Everything you saw is in the repo: README, ARCHITECTURE, DATA-MODEL, JUDGING, THREAT-MODEL, and
96 tests. Thanks!"
