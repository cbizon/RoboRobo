"""Judging a candidate against a baseline across several quantities.

The rule, first worked out for the sequel program (Nov 2026):

- Measure the baseline once, after settling, as ``blocks`` consecutive
  blocks of the same length as the candidate's measurement, and take each
  quantity's block means.  One long run in blocks stands in for independent
  runs when the system forgets its state within a block (sequel checked
  this: block means within a run predicted the spread between runs).
- The candidate passes a quantity when its mean is within ``threshold``
  standard deviations of the block means of the baseline mean.
- ``threshold`` is set so that a candidate identical to the baseline fails
  any of the quantities with probability ``false_fail`` (5%).  It allows for
  the candidate's own scatter (as large as one block's) and for the
  uncertainty of a standard deviation estimated from few blocks, so it
  shrinks as blocks are added: for 7 quantities, about 7.2 with 4 blocks,
  3.6 with 10, 3.1 with 20.  Testing each quantity at 2 standard deviations
  instead fails identical candidates 30 to 50% of the time.

The threshold comes from a simulation with independent normal quantities
and a fixed seed, so it is reproducible.  Correlated quantities make the
rule slightly conservative.
"""

from __future__ import annotations

from functools import lru_cache

import numpy as np

TRIALS = 400_000
SEED = 2026


@lru_cache(maxsize=None)
def threshold(blocks: int, quantities: int, false_fail: float = 0.05) -> float:
    if blocks < 3:
        raise ValueError(f"need at least 3 baseline blocks, got {blocks}")
    if quantities < 1:
        raise ValueError("need at least one quantity")
    rng = np.random.default_rng(SEED)
    worst = np.empty(TRIALS)
    batch = 50_000
    for start in range(0, TRIALS, batch):
        base = rng.normal(size=(batch, quantities, blocks))
        candidate = rng.normal(size=(batch, quantities))
        z = np.abs(candidate - base.mean(axis=2)) / base.std(axis=2, ddof=1)
        worst[start:start + batch] = z.max(axis=1)
    return round(float(np.quantile(worst, 1.0 - false_fail)), 2)


def baseline(block_means: dict[str, list[float]]) -> dict[str, object]:
    """Mean and standard deviation of each quantity's block means."""
    counts = {len(values) for values in block_means.values()}
    if len(counts) != 1:
        raise ValueError(f"quantities have different numbers of blocks: {sorted(counts)}")
    blocks = counts.pop()
    stats = {name: {"block_means": list(map(float, values)),
                    "mean": float(np.mean(values)),
                    "sd": float(np.std(values, ddof=1))}
             for name, values in block_means.items()}
    return {"blocks": blocks, "threshold": threshold(blocks, len(stats)),
            "quantities": stats}


def judge(base: dict[str, object], candidate: dict[str, float]) -> dict[str, dict[str, object]]:
    """Pass or fail each quantity of the candidate against the baseline."""
    missing = set(base["quantities"]) ^ set(candidate)
    if missing:
        raise ValueError(f"baseline and candidate measure different quantities: {sorted(missing)}")
    limit = threshold(base["blocks"], len(base["quantities"]))
    out = {}
    for name, stats in base["quantities"].items():
        value = float(candidate[name])
        sigmas = (value - stats["mean"]) / stats["sd"] if stats["sd"] > 0 else float("inf")
        out[name] = {"baseline_mean": stats["mean"], "baseline_sd": stats["sd"],
                     "candidate_mean": value, "sigmas": sigmas, "threshold": limit,
                     "passes": bool(abs(sigmas) <= limit)}
    return out
