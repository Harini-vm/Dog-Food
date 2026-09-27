"""The normalization proof (tools/normalization_proof.py) as a test: on simulated events with a known
true ranking, our method must beat raw averages and plain z-scores. Uses the portal's own code."""

import statistics
import sys

from conftest import ROOT

sys.path.insert(0, str(ROOT / "tools"))
import normalization_proof as proof  # noqa: E402


def test_our_ranking_is_closer_to_the_truth():
    r = proof.run(trials=80, seed=7)
    rho = {name: statistics.mean(m["rho"]) for name, m in r["methods"].items()}
    ours = rho["DOGFOOD (K=3, C=1)"]
    assert ours > rho["raw average"] + 0.01
    assert ours > rho["plain z-score (K=0, C=0)"] + 0.05
    assert r["wins"]["vs raw"] > r["trials"] * 0.6


def test_flat_scorer_barely_moves_the_ranking():
    """A judge who gives everyone the same score adds no ordering: all their reviews become one value."""
    from dogfood.services.results import adjust
    reviews = [{"judge": "flat", "project": f"p{i}", "x": 75.0} for i in range(3)] + \
              [{"judge": "real", "project": f"p{i}", "x": x} for i, x in enumerate((20.0, 60.0, 95.0))]
    M, _, _ = adjust(reviews)
    flat = [r["a"] for r in reviews if r["judge"] == "flat"]
    assert max(flat) - min(flat) < 1e-9
