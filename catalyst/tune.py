"""Tuner — fit the scorer's constants instead of guessing them (Phase 8b).

Every weight in the scorer is a prior: `CATALYST_WEIGHTS`, the severity map, the
per-catalyst half-lives, source trust, and the planner's `buy_threshold`. The
backtest already *measures* hit-rate and calibration error — this module closes
the loop: a seeded random search perturbs those priors into candidate `weights`
dicts, backtests each, ranks by a composite objective, and emits a
`weights.tuned.json` that is a **self-describing artifact** (winning params + the
measured metrics + the seed/window that produced them).

Design choices:
  - **stdlib only, deterministic.** `random.Random(seed)` drives the search, so
    the same seed + same window + same store reproduce the winner exactly. Trial
    0 is always the un-perturbed base, so the tuner can never pick something worse
    than the starting point.
  - **Injectable backtest.** `search()` takes the backtest runner as an argument,
    so the search loop / ranking / determinism / calibration-fit are unit-testable
    against a stub that returns canned `BacktestResult`s — no real backtest, no
    network, in tests.
  - **Objective** = `hit_rate − calibration_penalty × calibration_error`, subject
    to a hard `min_trades` floor (candidates with too few scored trades are
    disqualified, so a single lucky trade can't win). Ties break deterministically
    (more trades, then lower calibration error, then earliest trial).
  - **Confidence calibration** is fit from the *winning* run's reliability buckets
    (a monotone stated→realized table) and emitted for `planner.plan` to apply — a
    delivered `confidence: 0.7` then means ~70%.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from pathlib import Path

from .signals import (
    CATALYST_WEIGHTS, DEFAULT_CATALYST_HALFLIVES, DEFAULT_SEVERITY_WEIGHTS,
    DEFAULT_SOURCE_WEIGHTS, PRIMARY_BOOST,
)

# Per-parameter perturbation clamps — keep a candidate physically sane so a wild
# multiplier can't push, say, a half-life to zero or a threshold above 1.
_CLAMPS = {
    "source_weights": (0.1, 3.0),
    "catalyst_weights": (0.5, 4.0),
    "severity_weights": (0.05, 4.0),
    "catalyst_halflives": (0.5, 72.0),
    "primary_boost": (1.0, 3.0),
    "buy_threshold": (0.05, 0.6),
}
_MAP_KEYS = ("source_weights", "catalyst_weights", "severity_weights", "catalyst_halflives")
_SCALAR_KEYS = ("primary_boost", "buy_threshold")
_SPREAD = 0.4          # each value × Uniform(1−spread, 1+spread)


def default_base_weights() -> dict:
    """The starting point: the scorer's built-in priors as a flat candidate dict."""
    return {
        "source_weights": dict(DEFAULT_SOURCE_WEIGHTS),
        "catalyst_weights": dict(CATALYST_WEIGHTS),
        "severity_weights": dict(DEFAULT_SEVERITY_WEIGHTS),
        "catalyst_halflives": dict(DEFAULT_CATALYST_HALFLIVES),
        "primary_boost": PRIMARY_BOOST,
        "buy_threshold": 0.2,          # planner default
    }


def _clamp(v: float, key: str) -> float:
    lo, hi = _CLAMPS[key]
    return max(lo, min(hi, v))


def perturb_candidate(base: dict, rng: random.Random) -> dict:
    """One randomly-perturbed candidate: every numeric knob × Uniform spread, clamped.

    Maps are perturbed value-by-value; scalars directly. Deterministic given `rng`.
    """
    out: dict = {}
    for key in _MAP_KEYS:
        out[key] = {
            k: round(_clamp(v * rng.uniform(1 - _SPREAD, 1 + _SPREAD), key), 4)
            for k, v in base[key].items()
        }
    for key in _SCALAR_KEYS:
        out[key] = round(_clamp(base[key] * rng.uniform(1 - _SPREAD, 1 + _SPREAD), key), 4)
    return out


def candidate_to_kwargs(candidate: dict) -> tuple[dict, dict]:
    """Split a candidate into (signal_kwargs, plan_kwargs) for `run_backtest`."""
    signal_kwargs = {
        "source_weights": candidate["source_weights"],
        "catalyst_weights": candidate["catalyst_weights"],
        "severity_weights": candidate["severity_weights"],
        "catalyst_halflives": candidate["catalyst_halflives"],
        "primary_boost": candidate["primary_boost"],
    }
    plan_kwargs = {"buy_threshold": candidate["buy_threshold"]}
    return signal_kwargs, plan_kwargs


def objective(result, *, min_trades: int, calibration_penalty: float) -> float:
    """Composite score (higher = better); `-inf` disqualifies a thin candidate.

    Maximize hit-rate, penalize calibration error. The `min_trades` gate keeps a
    degenerate 1-trade 100%-hit candidate from winning."""
    if getattr(result, "scored", 0) < min_trades:
        return float("-inf")
    return result.hit_rate - calibration_penalty * result.calibration_error


def fit_calibration(reliability) -> list[list[float]]:
    """Fit a monotone piecewise-linear stated→realized table from reliability buckets.

    Each bucket carries a mean `stated` confidence and the `realized` win-rate.
    Sorted by stated and made non-decreasing (a running max — pool-adjacent-violators
    lite) so the correction is monotone; both coords clamped to [0, 1]. Empty in →
    empty out (planner then leaves confidence unchanged)."""
    pts = sorted((float(b["stated"]), float(b["realized"])) for b in reliability or [])
    out: list[list[float]] = []
    run_max = 0.0
    for stated, realized in pts:
        run_max = max(run_max, min(1.0, max(0.0, realized)))
        out.append([round(min(1.0, max(0.0, stated)), 4), round(run_max, 4)])
    return out


@dataclass
class TuneResult:
    params: dict                       # winning flat candidate (source_weights, …, buy_threshold)
    confidence_calibration: list       # [[stated, realized], …]
    metrics: dict                      # hit_rate, calibration_error, n_trades, objective
    trials: int
    seed: int


def search(
    run, conn, base_weights: dict | None = None, *,
    trials: int = 25, seed: int = 0, min_trades: int = 5,
    calibration_penalty: float = 0.5, bt_kwargs: dict | None = None,
) -> TuneResult:
    """Seeded random search over the scorer's priors; return the winning candidate.

    `run(conn, *, signal_kwargs, plan_kwargs, **bt_kwargs) -> BacktestResult` is
    injected so the loop is testable against a stub. Trial 0 is the un-perturbed
    base; trials 1..N−1 are perturbations. Deterministic for a fixed seed/window.
    """
    base = base_weights or default_base_weights()
    bt_kwargs = bt_kwargs or {}
    rng = random.Random(seed)

    best = None   # (objective, tiebreakers…, candidate, result)
    for i in range(max(1, trials)):
        cand = base if i == 0 else perturb_candidate(base, rng)
        sk, pk = candidate_to_kwargs(cand)
        result = run(conn, signal_kwargs=sk, plan_kwargs=pk, **bt_kwargs)
        obj = objective(result, min_trades=min_trades, calibration_penalty=calibration_penalty)
        # Rank key: objective, then more trades, then lower calibration error, then
        # earliest trial — every tiebreak deterministic so the winner is stable.
        rank = (obj, getattr(result, "scored", 0), -getattr(result, "calibration_error", 0.0), -i)
        if best is None or rank > best[0]:
            best = (rank, cand, result)

    _, win_cand, win_result = best
    return TuneResult(
        params=win_cand,
        confidence_calibration=fit_calibration(getattr(win_result, "reliability", [])),
        metrics={
            "hit_rate": getattr(win_result, "hit_rate", 0.0),
            "calibration_error": getattr(win_result, "calibration_error", 0.0),
            "n_trades": getattr(win_result, "scored", 0),
            "objective": round(best[0][0], 4) if best[0][0] != float("-inf") else None,
        },
        trials=max(1, trials),
        seed=seed,
    )


def build_tuned_file(result: TuneResult, *, start, end, step_hours: float,
                     min_trades: int, calibration_penalty: float) -> dict:
    """Assemble the self-describing weights.tuned.json dict (params + `_tuning` block)."""
    return {
        **result.params,
        "confidence_calibration": result.confidence_calibration,
        "_tuning": {
            "seed": result.seed,
            "trials": result.trials,
            "window": {"start": _iso(start), "end": _iso(end), "step_hours": step_hours},
            "min_trades": min_trades,
            "calibration_penalty": calibration_penalty,
            **result.metrics,
        },
    }


def _iso(v) -> str | None:
    return v.isoformat() if hasattr(v, "isoformat") else (str(v) if v is not None else None)


def run_tune(
    conn, *, start, end, step_hours: float = 24.0, trials: int = 25, seed: int = 0,
    min_trades: int = 5, calibration_penalty: float = 0.5,
    base_weights: dict | None = None, run=None, out: str | None = None,
) -> dict:
    """End-to-end tune against the store: search → assemble the artifact → (write).

    `run` defaults to `backtest.run_backtest`; tests inject a stub. Returns the
    tuned-file dict; if `out` is given, also writes it there.
    """
    if run is None:
        from .backtest import run_backtest

        run = run_backtest
    res = search(
        run, conn, base_weights, trials=trials, seed=seed, min_trades=min_trades,
        calibration_penalty=calibration_penalty,
        bt_kwargs={"start": start, "end": end, "step_hours": step_hours},
    )
    tuned = build_tuned_file(res, start=start, end=end, step_hours=step_hours,
                             min_trades=min_trades, calibration_penalty=calibration_penalty)
    if out:
        Path(out).write_text(json.dumps(tuned, indent=2) + "\n", encoding="utf-8")
    return tuned
