"""Phase-8b tuner tests — search loop, ranking, determinism, calibration fit.

The backtest runner is injected as a stub returning canned BacktestResults, so
no real backtest (and no network) runs here — exactly the seam tune.py is built
around.
"""

from dataclasses import dataclass, field

from catalyst.tune import (
    candidate_to_kwargs, default_base_weights, fit_calibration, perturb_candidate, search,
)
import random


@dataclass
class FakeResult:
    hit_rate: float
    calibration_error: float
    scored: int
    reliability: list = field(default_factory=list)


# ---- candidate mapping ------------------------------------------------------

def test_candidate_to_kwargs_splits_signal_and_plan():
    base = default_base_weights()
    sk, pk = candidate_to_kwargs(base)
    assert set(sk) == {"source_weights", "catalyst_weights", "severity_weights",
                       "catalyst_halflives", "primary_boost"}
    assert pk == {"buy_threshold": base["buy_threshold"]}


def test_perturb_is_deterministic_and_clamped():
    base = default_base_weights()
    c1 = perturb_candidate(base, random.Random(7))
    c2 = perturb_candidate(base, random.Random(7))
    assert c1 == c2                                   # same seed → same candidate
    assert 0.05 <= c1["buy_threshold"] <= 0.6         # clamped to sane range
    assert all(v >= 0.5 for v in c1["catalyst_halflives"].values())


# ---- ranking ----------------------------------------------------------------

def test_search_picks_highest_objective_over_min_trades():
    # Trial 0 (base) is mediocre; a later trial has a great hit-rate → must win.
    calls = {"i": 0}

    def run(conn, *, signal_kwargs, plan_kwargs, **_):
        i = calls["i"]; calls["i"] += 1
        # trial 2 is the clear winner (high hit-rate, low cal error, enough trades)
        table = [(0.5, 0.5, 10), (0.6, 0.4, 10), (0.9, 0.1, 20)]
        hr, ce, n = table[i] if i < len(table) else (0.55, 0.5, 10)
        return FakeResult(hit_rate=hr, calibration_error=ce, scored=n)

    res = search(run, None, trials=3, seed=1, min_trades=5)
    assert res.metrics["hit_rate"] == 0.9 and res.metrics["n_trades"] == 20


def test_min_trades_disqualifies_thin_candidates():
    def run(conn, *, signal_kwargs, plan_kwargs, **_):
        # A perfect but 1-trade candidate must NOT beat a solid 20-trade one.
        return run.seq.pop(0)
    run.seq = [FakeResult(1.0, 0.0, 1), FakeResult(0.7, 0.1, 20), FakeResult(0.6, 0.2, 20)]
    res = search(run, None, trials=3, seed=1, min_trades=5)
    assert res.metrics["n_trades"] == 20 and res.metrics["hit_rate"] == 0.7


# ---- determinism ------------------------------------------------------------

def test_search_is_reproducible_for_same_seed():
    def make_run():
        seq = [FakeResult(0.5 + 0.01 * i, 0.2, 10) for i in range(30)]
        def run(conn, *, signal_kwargs, plan_kwargs, **_):
            return seq.pop(0)
        return run

    a = search(make_run(), None, trials=10, seed=42, min_trades=5)
    b = search(make_run(), None, trials=10, seed=42, min_trades=5)
    assert a.params == b.params and a.metrics == b.metrics


# ---- calibration fit --------------------------------------------------------

def test_fit_calibration_is_monotone_and_clamped():
    reliability = [
        {"bucket": "low(<0.4)", "stated": 0.3, "realized": 0.5},
        {"bucket": "mid(0.4-0.7)", "stated": 0.55, "realized": 0.4},   # dip → must not decrease
        {"bucket": "high(>=0.7)", "stated": 0.8, "realized": 0.9},
    ]
    table = fit_calibration(reliability)
    xs = [p[0] for p in table]
    ys = [p[1] for p in table]
    assert xs == sorted(xs)                     # sorted by stated
    assert ys == sorted(ys)                     # non-decreasing (monotone)
    assert all(0.0 <= y <= 1.0 for y in ys)
    assert table[1][1] == 0.5                   # the 0.4 dip is pulled up to the running max


def test_fit_calibration_empty_returns_empty():
    assert fit_calibration([]) == []


# ---- calibration flows into the winning artifact ----------------------------

def test_search_attaches_calibration_from_winning_run():
    rel = [{"bucket": "high(>=0.7)", "stated": 0.8, "realized": 0.6}]

    def run(conn, *, signal_kwargs, plan_kwargs, **_):
        return FakeResult(hit_rate=0.8, calibration_error=0.1, scored=15, reliability=rel)

    res = search(run, None, trials=2, seed=0, min_trades=5)
    assert res.confidence_calibration == [[0.8, 0.6]]


# ---- artifact assembly + file write -----------------------------------------

def test_run_tune_writes_self_describing_artifact(tmp_path):
    import json
    from datetime import datetime, timezone

    from catalyst.tune import run_tune

    def run(conn, *, signal_kwargs, plan_kwargs, **_):
        return FakeResult(hit_rate=0.7, calibration_error=0.05, scored=12,
                          reliability=[{"stated": 0.6, "realized": 0.55}])

    out = tmp_path / "weights.tuned.json"
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    end = datetime(2026, 6, 30, tzinfo=timezone.utc)
    tuned = run_tune(None, start=start, end=end, trials=5, seed=3, min_trades=5,
                     run=run, out=str(out))
    # The tunable params are all present + the metadata block is self-describing.
    assert set(tuned) >= {"source_weights", "catalyst_weights", "severity_weights",
                          "catalyst_halflives", "primary_boost", "buy_threshold",
                          "confidence_calibration", "_tuning"}
    m = tuned["_tuning"]
    assert m["seed"] == 3 and m["trials"] == 5 and m["n_trades"] == 12
    assert m["hit_rate"] == 0.7 and m["window"]["start"] == start.isoformat()
    # It was actually written and round-trips as JSON.
    assert json.loads(out.read_text())["_tuning"]["seed"] == 3
