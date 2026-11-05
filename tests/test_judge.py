"""The blocked-baseline pass rule."""

import numpy as np
import pytest

from roborobo import judge


@pytest.mark.parametrize("blocks, expected", [(4, 7.24), (10, 3.61), (20, 3.07)])
def test_threshold_matches_sequel_table(blocks, expected):
    """Seven quantities: the values sequel first derived."""
    assert judge.threshold(blocks, 7) == pytest.approx(expected, abs=0.05)


def test_identical_candidates_fail_five_percent_of_the_time():
    rng = np.random.default_rng(11)
    blocks, quantities, trials = 10, 5, 4000
    limit = judge.threshold(blocks, quantities)
    fails = 0
    for _ in range(trials):
        base = judge.baseline({f"q{i}": rng.normal(size=blocks) for i in range(quantities)})
        verdict = judge.judge(base, {f"q{i}": rng.normal() for i in range(quantities)})
        fails += not all(row["passes"] for row in verdict.values())
        assert base["threshold"] == limit
    assert 0.035 < fails / trials < 0.065


def test_shifted_candidate_fails():
    rng = np.random.default_rng(3)
    base = judge.baseline({"a": rng.normal(size=10), "b": rng.normal(size=10)})
    sd = base["quantities"]["a"]["sd"]
    verdict = judge.judge(base, {"a": base["quantities"]["a"]["mean"] + 10 * sd,
                                 "b": base["quantities"]["b"]["mean"]})
    assert not verdict["a"]["passes"] and verdict["b"]["passes"]


def test_mismatched_quantities_refused():
    base = judge.baseline({"a": [1.0, 2.0, 3.0, 4.0]})
    with pytest.raises(ValueError, match="different quantities"):
        judge.judge(base, {"b": 1.0})
