#!/usr/bin/env python3
"""Normalization proof: does our correction recover the *true* ranking better than raw averages?

With real events we never know the true ranking. In a simulation we do. Each trial builds an event
shaped like the fixture (40 projects, 30 judges, 3 reviews per project with some missing) where every
project has a hidden true quality, and every judge has habits: some are lenient, some harsh, some
use the whole scale, some barely move off 4 (like fixture judge jdg_07).

The scores then go through the portal's own code (review_value, adjust, project_score, and the
assignment planner), not a re-implementation, and we measure how close each method's ranking is
to the truth.

    python tools/normalization_proof.py            # 500 trials, writes docs/normalization-proof.md
    python tools/normalization_proof.py --trials 50 --stdout
"""

import argparse
import math
import random
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dogfood.services.assign import plan                          # noqa: E402
from dogfood.services.results import adjust, project_score, review_value  # noqa: E402

RUBRIC = [{"key": "functionality", "weight": 0.40, "min_score": 1, "max_score": 5},
          {"key": "quality", "weight": 0.35, "min_score": 1, "max_score": 5},
          {"key": "innovation", "weight": 0.25, "min_score": 1, "max_score": 5}]


def simulate(rng: random.Random, projects=40, judges=30, per_project=3, missing=0.2):
    """One synthetic event. Returns (true quality per project, list of reviews)."""
    truth = {f"p{i}": rng.gauss(0, 1) for i in range(projects)}
    habits = {}
    for j in range(judges):
        kind = rng.random()
        if kind < 0.1:                                   # flat scorer: gives ~4 to everything
            habits[f"j{j}"] = (0.9, 0.08)
        else:
            habits[f"j{j}"] = (rng.gauss(0, 0.7), math.exp(rng.gauss(0, 0.35)))   # leniency, scale
    pairs, _ = plan([{"id": p, "team_id": "t" + p, "track_id": None} for p in truth], list(habits), {}, set(), [],
                    per_project, seed=rng.randrange(10**6))
    reviews = []
    for j, p in pairs:
        if rng.random() < missing / per_project:       # some reviews never arrive, as in the fixture
            continue
        lean, scale = habits[j]
        values = {}
        for c in RUBRIC:
            signal = truth[p] + rng.gauss(0, 0.6)       # judges see quality through noise
            v = round(3 + lean + scale * signal)
            values[c["key"]] = max(1, min(5, v))
        reviews.append({"judge": j, "project": p, "x": review_value(values, RUBRIC)})
    return truth, reviews


def rank_by(scores: dict) -> list:
    return sorted(scores, key=lambda p: -scores[p])


def spearman(order: list, truth: dict) -> float:
    true_pos = {p: i for i, p in enumerate(rank_by(truth))}
    n = len(order)
    d2 = sum((i - true_pos[p]) ** 2 for i, p in enumerate(order))
    return 1 - 6 * d2 / (n * (n * n - 1))


def methods(reviews: list) -> dict:
    """Four rankings of the same reviews."""
    by_p = {}
    for r in reviews:
        by_p.setdefault(r["project"], []).append(r)
    out = {"raw average": {p: statistics.mean(r["x"] for r in rs) for p, rs in by_p.items()}}
    for name, k, c in (("plain z-score (K=0, C=0)", 0, 0), ("shrunk z-score, no prior (K=3, C=0)", 3, 0),
                       ("DOGFOOD (K=3, C=1)", 3, 1)):
        rs = [dict(r) for r in reviews]
        M, _, _ = adjust(rs, k=k)
        grouped = {}
        for r in rs:
            grouped.setdefault(r["project"], []).append(r["a"])
        out[name] = {p: project_score(a, M, c=c) for p, a in grouped.items()}
    return out


def run(trials: int, seed: int = 2026) -> dict:
    rng = random.Random(seed)
    res = {}
    wins = {"vs raw": 0, "vs plain z": 0}
    k_sweep = {k: [] for k in (0, 1, 2, 3, 5, 10, 1000)}
    c_sweep = {c: {"rho": [], "top3": []} for c in (0, 0.5, 1, 2, 4)}
    for _ in range(trials):
        truth, reviews = simulate(rng)
        ranked = {name: rank_by(scores) for name, scores in methods(reviews).items()}
        truth = {p: q for p, q in truth.items() if p in ranked["raw average"]}
        best3 = set(rank_by(truth)[:3])
        best10 = set(rank_by(truth)[:10])
        for name, order in ranked.items():
            m = res.setdefault(name, {"rho": [], "top3": [], "top10": [], "winner": []})
            m["rho"].append(spearman(order, truth))
            m["top3"].append(len(best3 & set(order[:3])) / 3)
            m["top10"].append(len(best10 & set(order[:10])) / 10)
            m["winner"].append(order[0] == rank_by(truth)[0])
        ours = res["DOGFOOD (K=3, C=1)"]["rho"][-1]
        wins["vs raw"] += ours > res["raw average"]["rho"][-1]
        wins["vs plain z"] += ours > res["plain z-score (K=0, C=0)"]["rho"][-1]
        for k in k_sweep:
            rs = [dict(r) for r in reviews]
            M, _, _ = adjust(rs, k=k)
            g = {}
            for r in rs:
                g.setdefault(r["project"], []).append(r["a"])
            k_sweep[k].append(spearman(rank_by({p: project_score(a, M) for p, a in g.items()}), truth))
        rs = [dict(r) for r in reviews]
        M, _, _ = adjust(rs)
        g = {}
        for r in rs:
            g.setdefault(r["project"], []).append(r["a"])
        for c in c_sweep:
            order = rank_by({p: project_score(a, M, c=c) for p, a in g.items()})
            c_sweep[c]["rho"].append(spearman(order, truth))
            c_sweep[c]["top3"].append(len(best3 & set(order[:3])) / 3)
    return {"trials": trials, "seed": seed, "methods": res, "wins": wins, "k_sweep": k_sweep, "c_sweep": c_sweep}


def ci(xs):
    m = statistics.mean(xs)
    half = 1.96 * statistics.stdev(xs) / math.sqrt(len(xs)) if len(xs) > 1 else 0
    return m, half


def report(r: dict) -> str:
    lines = [
        "# Normalization proof",
        "",
        f"Generated by `python tools/normalization_proof.py` ({r['trials']} simulated events, seed {r['seed']}).",
        "Every number comes from the portal's own scoring code: `review_value`, `adjust`, `project_score` and the",
        "assignment planner `plan`. Nothing here is a re-implementation.",
        "",
        "## Setup",
        "",
        "Each simulated event is shaped like `fixtures.json`: 40 projects, 30 judges, 3 reviews per project",
        "assigned by our planner, and about 20% of third reviews missing. Every project has a hidden true",
        "quality. Every judge has a leniency (their average offset, σ = 0.7 points) and a scale (how much",
        "of the 1–5 range they use). 10% of judges are *flat scorers* who give about 4 to everything, like",
        "fixture judge jdg_07. Each criterion score = truth + reading noise, bent by the judge's habits,",
        "rounded and clipped to 1–5.",
        "",
        "## Result",
        "",
        "Higher is better. ± is a 95% confidence interval over the simulated events.",
        "",
        "| method | rank correlation with truth (Spearman ρ) | top-3 found | top-10 found | true winner ranked 1st |",
        "|---|---|---|---|---|",
    ]
    for name, m in r["methods"].items():
        rho, h = ci(m["rho"])
        bold = "**" if name.startswith("DOGFOOD") else ""
        lines.append(f"| {bold}{name}{bold} | {bold}{rho:.3f} ± {h:.3f}{bold} | {statistics.mean(m['top3']):.0%} | "
                     f"{statistics.mean(m['top10']):.0%} | {statistics.mean(m['winner']):.0%} |")
    t = r["trials"]
    lines += [
        "",
        f"Our method gives a ranking closer to the truth than raw averages in **{r['wins']['vs raw']} of {t}** events,",
        f"and closer than a plain z-score in **{r['wins']['vs plain z']} of {t}**.",
        "",
        "## Why K = 3 (judge shrinkage)",
        "",
        "Same simulations, varying only K, with C = 1. K = 0 trusts every judge's own mean fully, even from 2",
        "reviews. K = 1000 effectively turns the correction off.",
        "",
        "| K | Spearman ρ |",
        "|---|---|",
    ]
    for k, xs in r["k_sweep"].items():
        m, h = ci(xs)
        lines.append(f"| {k}{' (ours)' if k == 3 else ''} | {m:.3f} ± {h:.3f} |")
    best_k = max(r["k_sweep"], key=lambda k: statistics.mean(r["k_sweep"][k]))
    lines += [
        "",
        f"The best K in this sweep is **{best_k}**. The curve is flat around its peak, so any small K is",
        "close; K = 3 sits in that range and is easy to explain (\"a judge's habits count fully once they",
        "have clearly more than 3 reviews\").",
        "",
        "## The project prior C",
        "",
        "Same simulations with K = 3, varying only C (the number of 'average reviews' added to every project).",
        "",
        "| C | Spearman ρ | top-3 found |",
        "|---|---|---|",
    ]
    for c, m in r["c_sweep"].items():
        rho, h = ci(m["rho"])
        lines.append(f"| {c}{' (ours)' if c == 1 else ''} | {rho:.3f} ± {h:.3f} | {statistics.mean(m['top3']):.0%} |")
    lines += [
        "",
        "C barely changes overall accuracy. It is a *fairness* choice more than an accuracy one: it stops a",
        "project with two lucky reviews from outranking one with three or four solid ones, at a small cost when",
        "a project with few reviews really is excellent. Organizers see every such move in the results preview",
        "(the ▲/▼ column), so the prior is never invisible.",
        "",
        "## What this does not prove",
        "",
        "- The simulation assumes judges differ in leniency and scale but agree on *order*. A judge with a",
        "  genuinely different taste is not something any normalization can or should correct.",
        "- With 3 reviews per project, even the best method misses part of the true top 10. More reviews",
        "  per project help more than any formula. The dashboard's \"under-reviewed\" flag exists for that.",
        "",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=500)
    ap.add_argument("--stdout", action="store_true")
    a = ap.parse_args()
    text = report(run(a.trials))
    if a.stdout:
        print(text)
    else:
        out = ROOT / "docs" / "normalization-proof.md"
        out.write_text(text, encoding="utf-8")
        print(f"wrote {out}")
