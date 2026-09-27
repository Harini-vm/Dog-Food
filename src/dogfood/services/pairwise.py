"""Pairwise mode: head-to-head comparisons and a Bradley-Terry ranking.

Why: rubric scores are absolute ("this is a 4"), and people are bad at absolute scales. They are
good at "which of these two is better?". The pairwise ranking is a second opinion collected a
different way. Where it agrees with the rubric ranking, organizers can publish with more
confidence; where it disagrees, the preview shows exactly which pairs to look at.

Which pairs: the ones the rubric ranking is *least sure about*. Entries next to each other in the
current ranking are compared first (neighbours, then one apart), preferring pairs with the fewest
comparisons so far. Hard rules are the same as for reviews: no judge compares an entry from a team
they are on, track limits apply, and the database forbids the same pair twice for one judge.

Model: Bradley-Terry, P(i beats j) = p_i / (p_i + p_j), fitted with the MM algorithm (Hunter 2004).
A tie counts as half a win for each side. Every entry also plays one virtual draw against an
"average entry" (strength 1). That keeps an entry that won everything from getting an infinite
strength, and it pulls entries with few comparisons toward average, the same idea as the
project prior in results.py. An entry's pairwise score = 100 * p / (p + 1): the modelled chance
it beats an average entry.
"""

import math
import random
from collections import defaultdict
from datetime import datetime, timezone

from .. import audit, db
from ..errors import bad, conflict, forbidden, not_found
from ..security import keyed, new_id

PRIOR = 1.0          # virtual draws against an average entry
ITERATIONS = 500


def bradley_terry(items, outcomes, prior: float = PRIOR, iterations: int = ITERATIONS) -> dict:
    """Pure function. outcomes: iterable of (a, b, result) with result in {'a', 'b', 'tie'}.
    Returns {item: strength}, geometric mean 1."""
    items = list(items)
    wins = defaultdict(float)
    games = defaultdict(float)                     # (i, j) -> number of games, both orders
    for a, b, r in outcomes:
        wins[a] += 1.0 if r == "a" else 0.5 if r == "tie" else 0.0
        wins[b] += 1.0 if r == "b" else 0.5 if r == "tie" else 0.0
        games[(a, b)] += 1
        games[(b, a)] += 1
    opponents = defaultdict(list)
    for (i, j), n in games.items():
        opponents[i].append((j, n))
    p = {i: 1.0 for i in items}
    for _ in range(iterations):
        new = {}
        for i in items:
            w = wins[i] + prior * 0.5                 # half of the virtual games against "average" are won
            den = sum(n / (p[i] + p[j]) for j, n in opponents[i]) + prior / (p[i] + 1.0)
            new[i] = w / den if den else 1.0
        g = math.exp(sum(math.log(v) for v in new.values()) / len(new)) if new else 1.0
        change = max((abs(new[i] / g - p[i]) for i in items), default=0)
        p = {i: new[i] / g for i in items}
        if change < 1e-10:
            break
    return p


def kendall_tau(order_a: list, order_b: list) -> float | None:
    """Agreement between two rankings of the same items: 1 = identical, 0 = unrelated, -1 = reversed."""
    common = [x for x in order_a if x in set(order_b)]
    pos = {x: i for i, x in enumerate(order_b)}
    n = len(common)
    if n < 2:
        return None
    concordant = sum(1 for i in range(n) for j in range(i + 1, n) if pos[common[i]] < pos[common[j]])
    pairs = n * (n - 1) / 2
    return (2 * concordant - pairs) / pairs


def generate(conn, user, ev, per_judge: int, rubric_order: list) -> dict:
    """Give every judge up to `per_judge` open comparisons. Safe to run again: it only tops up."""
    if ev["results_published_at"]:
        raise conflict("results are published; judging is over")
    if not 1 <= per_judge <= 50:
        raise bad("comparisons per judge must be between 1 and 50")
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("pairs:" + ev["id"],))
    projects = {p["id"]: p for p in db.rows(conn, """SELECT id, team_id, track_id FROM projects
                                                     WHERE event_id = %s AND status = 'submitted'""", (ev["id"],))}
    order = [p for p in rubric_order if p in projects] + sorted(p for p in projects if p not in rubric_order)
    judges = [r["user_id"] for r in db.rows(conn, """SELECT user_id FROM memberships WHERE event_id = %s
                                                     AND role = 'judge' ORDER BY user_id""", (ev["id"],))]
    if not judges or len(order) < 2:
        raise bad("pairwise mode needs at least one judge and two submitted entries")
    tracks = defaultdict(set)
    for r in db.rows(conn, "SELECT user_id, track_id FROM judge_tracks WHERE event_id = %s", (ev["id"],)):
        tracks[r["user_id"]].add(r["track_id"])
    blocked = {(r["judge_id"], r["team_id"]) for r in db.rows(
        conn, "SELECT judge_id, team_id FROM conflicts WHERE event_id = %s", (ev["id"],))}
    existing = db.rows(conn, "SELECT judge_id, project_a, project_b, winner FROM comparisons WHERE event_id = %s",
                       (ev["id"],))
    done = {(e["judge_id"], e["project_a"], e["project_b"]) for e in existing}
    per_pair = defaultdict(int)
    open_count = defaultdict(int)
    for e in existing:
        per_pair[(e["project_a"], e["project_b"])] += 1
        if e["winner"] is None:
            open_count[e["judge_id"]] += 1

    # Candidate pairs: neighbours in the current ranking first, then one apart, then two apart.
    candidates = []
    for gap in (1, 2, 3):
        for i in range(len(order) - gap):
            a, b = sorted((order[i], order[i + gap]))
            candidates.append((gap, a, b))
    rng = random.Random(ev["assignment_seed"])

    def ok(j, pid):
        p = projects[pid]
        return (j, p["team_id"]) not in blocked and (not tracks.get(j) or p["track_id"] in tracks[j])

    made = 0
    for j in sorted(judges, key=lambda _: rng.random()):
        need = per_judge - open_count[j]
        if need <= 0:
            continue
        pool = [(gap, per_pair[(a, b)], rng.random(), a, b) for gap, a, b in candidates
                if (j, a, b) not in done and ok(j, a) and ok(j, b)]
        pool.sort()                                   # closest in rank, then least compared, then random
        seen = set()
        for _, _, _, a, b in pool:
            if need <= 0:
                break
            if (a, b) in seen:
                continue
            seen.add((a, b))
            conn.execute("INSERT INTO comparisons (id, event_id, judge_id, project_a, project_b) VALUES (%s,%s,%s,%s,%s)",
                         (new_id("cmp"), ev["id"], j, a, b))
            per_pair[(a, b)] += 1
            done.add((j, a, b))
            need -= 1
            made += 1
    audit.log(conn, user, "pairwise.generate", ev["id"], ev["id"], {"created": made, "per_judge": per_judge})
    return {"created": made}


def for_judge(conn, user) -> list:
    return db.rows(conn, """SELECT c.id, c.event_id, e.name AS event, c.winner, a.title AS title_a, b.title AS title_b
                            FROM comparisons c JOIN events e ON e.id = c.event_id
                            JOIN projects a ON a.id = c.project_a JOIN projects b ON b.id = c.project_b
                            JOIN memberships m ON m.event_id = c.event_id AND m.user_id = c.judge_id AND m.role = 'judge'
                            WHERE c.judge_id = %s ORDER BY c.winner IS NOT NULL, c.created_at, c.id""", (user.id,))


def load(conn, user, cid: str) -> dict:
    c = db.one(conn, "SELECT * FROM comparisons WHERE id = %s", (cid,))
    if c is None or c["judge_id"] != user.id:            # same answer for missing and someone else's
        raise forbidden("this is not your comparison")
    return c


def sides(c: dict) -> tuple[str, str]:
    """Which entry is shown on the left. Varies by judge and pair (keyed hash), so no entry is always
    first: people favour whatever they see first."""
    a, b = c["project_a"], c["project_b"]
    return (a, b) if int(keyed(f"{c['judge_id']}:{a}:{b}")[:8], 16) % 2 == 0 else (b, a)


def decide(conn, user, cid: str, choice: str) -> None:
    c = db.one(conn, "SELECT * FROM comparisons WHERE id = %s FOR UPDATE", (cid,))
    if c is None or c["judge_id"] != user.id:
        raise forbidden("this is not your comparison")
    if not db.one(conn, "SELECT 1 FROM memberships WHERE event_id=%s AND user_id=%s AND role='judge'",
                  (c["event_id"], user.id)):
        raise forbidden("you are no longer a judge of this event", "not_a_judge")
    ev = db.one(conn, "SELECT * FROM events WHERE id = %s FOR SHARE", (c["event_id"],))
    from .scoring import judging_open
    if not judging_open(ev):
        raise forbidden("judging is closed", "judging_closed")
    if choice == "tie":
        winner = "tie"
    elif choice == c["project_a"]:
        winner = "a"
    elif choice == c["project_b"]:
        winner = "b"
    else:
        raise bad("choose one of the two entries, or 'tie'")
    conn.execute("UPDATE comparisons SET winner = %s, decided_at = %s WHERE id = %s",
                 (winner, datetime.now(timezone.utc), cid))
    audit.log(conn, user, "pairwise.decide", c["event_id"], cid)       # never which side won


def summary(conn, event_id: str, rubric_ranking: list) -> dict:
    """The Bradley-Terry ranking of submitted entries, and how well it agrees with the rubric."""
    rows = db.rows(conn, """SELECT c.project_a, c.project_b, c.winner FROM comparisons c
                            JOIN projects a ON a.id = c.project_a AND a.status = 'submitted'
                            JOIN projects b ON b.id = c.project_b AND b.status = 'submitted'
                            WHERE c.event_id = %s AND c.winner IS NOT NULL""", (event_id,))
    total = db.val(conn, "SELECT count(*) FROM comparisons WHERE event_id = %s", (event_id,))
    if not rows:
        return {"decided": 0, "total": total, "scores": {}, "tau": None, "enough": False, "agree": 0, "against": 0,
                "disagreements": []}
    items = sorted({r["project_a"] for r in rows} | {r["project_b"] for r in rows})
    p = bradley_terry(items, [(r["project_a"], r["project_b"], r["winner"]) for r in rows])
    order = sorted(items, key=lambda i: -p[i])
    scores = {i: {"score": round(100 * p[i] / (p[i] + 1), 1), "rank": n + 1} for n, i in enumerate(order)}
    counts = defaultdict(int)
    for r in rows:
        counts[r["project_a"]] += 1
        counts[r["project_b"]] += 1
    for i in items:
        scores[i]["comparisons"] = counts[i]
    rubric_order = [pid for pid in rubric_ranking if pid in scores]
    # Pairs where judges, head to head, preferred the entry the rubric ranked lower.
    net = defaultdict(float)
    for r in rows:
        a, b = r["project_a"], r["project_b"]
        net[(a, b)] += 1 if r["winner"] == "a" else -1 if r["winner"] == "b" else 0
    pos = {pid: i for i, pid in enumerate(rubric_ranking)}
    disagreements = []
    for (a, b), v in net.items():
        if v == 0 or a not in pos or b not in pos:
            continue
        preferred, other = (a, b) if v > 0 else (b, a)
        if pos[preferred] > pos[other]:
            disagreements.append({"preferred": preferred, "over": other, "margin": abs(v)})
    # Plain agreement: in how many decided (non-tie) comparisons did judges pick the rubric's higher entry?
    agree = against = 0
    for r in rows:
        a, b = r["project_a"], r["project_b"]
        if r["winner"] == "tie" or a not in pos or b not in pos:
            continue
        higher = a if pos[a] < pos[b] else b
        picked = a if r["winner"] == "a" else b
        agree, against = (agree + 1, against) if picked == higher else (agree, against + 1)
    # A rank correlation only means something once entries have been compared a few times each.
    enough = len(rows) >= 1.5 * len(items)
    return {"decided": len(rows), "total": total, "scores": scores, "agree": agree, "against": against,
            "tau": kendall_tau(rubric_order, order) if enough else None, "enough": enough,
            "disagreements": sorted(disagreements, key=lambda d: -d["margin"])}


def project_pair(conn, c: dict) -> list:
    left, right = sides(c)
    out = []
    for pid in (left, right):
        p = db.one(conn, """SELECT p.id, p.title, p.summary, p.repo_url, t.name AS track, tm.name AS team
                            FROM projects p JOIN teams tm ON tm.id = p.team_id LEFT JOIN tracks t ON t.id = p.track_id
                            WHERE p.id = %s""", (pid,))
        if p is None:
            raise not_found("entry no longer exists")
        out.append(p)
    return out
