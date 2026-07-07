"""Calibration — coordinate-ascent optimiser + backtest wiring (stubbed)."""

from __future__ import annotations

from dataclasses import dataclass, field

from catalyst.backtest import _reliability, Trade
from catalyst.calibrate import (
    _bt_kwargs,
    coordinate_sweep,
    objective,
    run_calibration,
)


def test_coordinate_sweep_finds_peak():
    # Objective peaks at flow_weight=0.4; ascent should reach it.
    def evaluate(cfg):
        return -abs(cfg["flow_weight"] - 0.4) - abs(cfg["macro_weight"] - 0.15)

    base = {"flow_weight": 0.0, "macro_weight": 0.0}
    grid = {"flow_weight": [0.0, 0.15, 0.25, 0.4], "macro_weight": [0.0, 0.15, 0.25, 0.4]}
    best, score, trials = coordinate_sweep(evaluate, base, grid)
    assert best["flow_weight"] == 0.4
    assert best["macro_weight"] == 0.15
    assert trials > 1


def test_bt_kwargs_maps_weights():
    kw = _bt_kwargs({"flow_weight": 0.4, "source_weights": {"rss": 1.5}},
                    {"plan_kwargs": {"buy_threshold": 0.2}, "start": "s"})
    assert kw["plan_kwargs"]["flow_weight"] == 0.4
    assert kw["plan_kwargs"]["buy_threshold"] == 0.2   # base preserved
    assert kw["signal_kwargs"]["source_weights"]["rss"] == 1.5
    assert kw["start"] == "s"


@dataclass
class _FakePortfolio:
    sharpe: float


@dataclass
class _FakeResult:
    cum_return: float = 0.0
    hit_rate: float = 0.0
    calibration_error: float = 0.0
    portfolio: object = None


def test_objective_metrics():
    r = _FakeResult(cum_return=0.1, hit_rate=0.6, calibration_error=0.2,
                    portfolio=_FakePortfolio(sharpe=1.5))
    assert objective(r, "sharpe") == 1.5
    assert objective(r, "hit_rate") == 0.6
    assert objective(r, "calibration") == -0.2
    # no portfolio → sharpe falls back to cum_return
    assert objective(_FakeResult(cum_return=0.1), "sharpe") == 0.1


def test_run_calibration_picks_best_with_stub_backtest():
    # Stub backtest: reward market_weight=0.4, everything else neutral.
    def fake_run(conn, **kwargs):
        mw = kwargs["plan_kwargs"].get("market_weight", 0.0)
        return _FakeResult(portfolio=_FakePortfolio(sharpe=-abs(mw - 0.4)))

    out = run_calibration(None, metric="sharpe", run=fake_run,
                          bt_kwargs={"start": "s", "end": "e"})
    assert out["modifier_weights"]["market_weight"] == 0.4
    assert out["metric"] == "sharpe"


def test_reliability_curve():
    # High-confidence trades that mostly lose → a positive (over-stated) gap.
    trades = [Trade("BTC", "buy", "short", "", 1, "", 1, ret=(0.05 if i < 2 else -0.05),
                    confidence=0.8) for i in range(10)]
    curve, err = _reliability(trades)
    hi = [c for c in curve if c["bucket"] == "high(>=0.7)"][0]
    assert hi["stated"] == 0.8
    assert hi["realized"] == 0.2          # 2/10 won
    assert hi["gap"] == round(0.8 - 0.2, 3)
    assert err == 0.6
