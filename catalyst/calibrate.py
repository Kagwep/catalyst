"""Calibration — tune weights against measured backtest outcomes, not by hand.

Two knobs the operator would otherwise guess at:
  - **Modifier weights** (`macro/flow/supply/market/derivs_weight`): how hard each
    bias layer pushes planner confidence.
  - **Per-source trust weights** (`source_weights`): how much each source's posts
    count in the signal layer.

Rather than hand-pick, this sweeps them over the real backtest and keeps whatever
maximises a chosen objective (portfolio Sharpe by default, or hit-rate / return).
The search is **coordinate ascent** — cheap, deterministic, and good enough for a
handful of near-independent knobs: hold everything fixed, sweep one knob over a
small candidate set, keep the best, repeat until nothing improves.

The optimiser (`coordinate_sweep`) is pure and testable; `run_calibration` is the
thin wrapper that plugs the backtest in as the objective and (optionally) writes
the winners back to `weights.json`.
"""

from __future__ import annotations

import json
from pathlib import Path

MODIFIER_KEYS = ["macro_weight", "flow_weight", "supply_weight", "market_weight", "derivs_weight"]
DEFAULT_CANDIDATES = [0.0, 0.15, 0.25, 0.4]
_DEFAULTS = {"macro_weight": 0.3, "flow_weight": 0.25, "supply_weight": 0.25,
             "market_weight": 0.25, "derivs_weight": 0.25}


def objective(result, metric: str = "sharpe") -> float:
    """Scalar score for a BacktestResult under the chosen metric (higher = better)."""
    p = getattr(result, "portfolio", None)
    if metric == "sharpe":
        return p.sharpe if p is not None else result.cum_return
    if metric == "total_return":
        return p.total_return if p is not None else result.cum_return
    if metric == "hit_rate":
        return result.hit_rate
    if metric == "calibration":       # lower error is better → negate
        return -result.calibration_error
    return result.cum_return


def coordinate_sweep(evaluate, base: dict, grid: dict, *, rounds: int = 2):
    """Maximise `evaluate(cfg)` by coordinate ascent over `grid` candidate values.

    Returns (best_cfg, best_score, n_trials). Deterministic; `evaluate` is any
    cfg→float callable, so tests inject a synthetic objective (no backtest needed).
    """
    best = dict(base)
    best_score = evaluate(best)
    trials = 1
    for _ in range(rounds):
        improved = False
        for key, candidates in grid.items():
            for v in candidates:
                if v == best.get(key):
                    continue
                trial = {**best, key: v}
                score = evaluate(trial)
                trials += 1
                if score > best_score:
                    best, best_score = trial, score
                    improved = True
        if not improved:
            break
    return best, best_score, trials


def _bt_kwargs(cfg: dict, base_bt: dict) -> dict:
    """Map a flat weight cfg into run_backtest kwargs (plan modifier weights +
    source_weights), layered over the caller's base backtest kwargs."""
    kwargs = dict(base_bt)
    plan_kwargs = dict(kwargs.get("plan_kwargs") or {})
    for k in MODIFIER_KEYS:
        if k in cfg:
            plan_kwargs[k] = cfg[k]
    kwargs["plan_kwargs"] = plan_kwargs
    if cfg.get("source_weights"):
        sk = dict(kwargs.get("signal_kwargs") or {})
        sk["source_weights"] = {**(sk.get("source_weights") or {}), **cfg["source_weights"]}
        kwargs["signal_kwargs"] = sk
    return kwargs


def run_calibration(
    conn, *, metric: str = "sharpe", keys: list[str] | None = None,
    candidates: list[float] | None = None, base_weights: dict | None = None,
    run=None, bt_kwargs: dict | None = None, rounds: int = 2,
) -> dict:
    """Sweep the modifier weights over the backtest; return the best config + score.

    `run` defaults to `backtest.run_backtest`; tests pass a stub. `bt_kwargs` are
    the fixed backtest args (start/end/period/…). Result is a dict ready to merge
    into weights.json (`modifier_weights`).
    """
    from .backtest import run_backtest

    run = run or run_backtest
    keys = keys or MODIFIER_KEYS
    candidates = candidates or DEFAULT_CANDIDATES
    base_bt = bt_kwargs or {}
    base = {k: (base_weights or {}).get(k, _DEFAULTS.get(k, 0.25)) for k in keys}

    def evaluate(cfg: dict) -> float:
        return objective(run(conn, **_bt_kwargs(cfg, base_bt)), metric)

    grid = {k: candidates for k in keys}
    best, score, trials = coordinate_sweep(evaluate, base, grid, rounds=rounds)
    return {"metric": metric, "score": round(score, 4), "trials": trials,
            "modifier_weights": {k: best[k] for k in keys}}


def write_weights(path: str, modifier_weights: dict) -> None:
    """Merge tuned modifier weights into a weights.json (creating it if absent)."""
    p = Path(path)
    data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    data["modifier_weights"] = modifier_weights
    p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
