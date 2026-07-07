"""Backtest harness — replay the planner over history and score it on prices.

This is the bridge from "proposals with rationale" to "rules with measured edge"
(the deliverable is a *backtestable* strategy spec). It leans on a property the
whole stack already has: every analytic takes `now=`, so a backtest is just
replaying `now=t` across history with a strict point-in-time cut, then scoring
each proposed buy/sell against what prices actually did over its horizon.

Phase 1 (this module) is an **event study / signal-quality** backtest: each
buy/sell is a unit trade held for its horizon, scored on directional return.
Minimal assumptions — it answers "do these proposals predict moves?". A later
portfolio sim (confidence sizing, fees, equity curve) builds on top.

Honesty notes: the replay only uses posts/biases with `indexed_at <= t`, and it
recomputes signals + every bias layer as-of t from the stored *dated* records —
so it's only as complete as the history that's been accumulated. It does not
execute or assume costs; Phase 1 is signal quality, not net P&L.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .macro import compute_regime
from .flows import compute_flow_bias
from .derivs import compute_derivs_bias
from .market import compute_market_bias
from .onchain import compute_supply_bias
from .planner import plan
from .prices import PriceOracle
from .signals import _assets as _row_assets, compute_signals
from .store import (
    fetch_derivs, fetch_enriched, fetch_flows, fetch_macro, fetch_market, fetch_onchain,
)

DEFAULT_HORIZON_HOURS = {"intraday": 24.0, "short": 72.0, "swing": 168.0}


def _asof(rows: list[dict], t_iso: str) -> list[dict]:
    """Point-in-time cut: only records known at or before t (ISO sorts by time)."""
    return [r for r in rows if (r.get("indexed_at") or "") <= t_iso]


def _steps(start: datetime, end: datetime, step_hours: float):
    t = start
    delta = timedelta(hours=step_hours)
    while t <= end:
        yield t
        t += delta


def replay(
    conn, *, start: datetime, end: datetime, step_hours: float = 24.0,
    signal_kwargs: dict | None = None, flow_scale: dict | None = None,
    supply_kwargs: dict | None = None, market_kwargs: dict | None = None,
    derivs_kwargs: dict | None = None,
    oracle: PriceOracle | None = None, plan_kwargs: dict | None = None,
) -> list:
    """Re-run the real planner as-of each step; collect proposed buy/sell actions.

    Recomputes signals + macro/flow/supply/market/derivs biases from dated stored
    records (and, for technicals, the price `oracle`) at each `t`, so the result
    is what the planner *would have* proposed live.
    """
    enriched = fetch_enriched(conn)
    macro_rows, flow_rows = fetch_macro(conn), fetch_flows(conn)
    onchain_rows, market_rows = fetch_onchain(conn), fetch_market(conn)
    derivs_rows = fetch_derivs(conn)
    price_history = oracle.history() if oracle is not None else {}
    signal_kwargs = signal_kwargs or {}
    plan_kwargs = plan_kwargs or {}

    emitted: list[dict] = []   # doubles as the cooldown history (created_at/asset/action)
    actions: list = []
    for t in _steps(start, end, step_hours):
        t_iso = t.isoformat()
        sigs = compute_signals(_asof(enriched, t_iso), now=t, **signal_kwargs)
        regime = compute_regime(_asof(macro_rows, t_iso), now=t)
        flow_bias = compute_flow_bias(_asof(flow_rows, t_iso), now=t, scale=flow_scale)
        supply_bias = compute_supply_bias(_asof(onchain_rows, t_iso), now=t, **(supply_kwargs or {}))
        market_bias = (compute_market_bias(price_history, _asof(market_rows, t_iso), now=t,
                                           **(market_kwargs or {})) if price_history else None)
        derivs_bias = compute_derivs_bias(_asof(derivs_rows, t_iso), now=t, **(derivs_kwargs or {}))
        proposed = plan(
            sigs, now=t, recent_actions=emitted,
            regime=regime, flow_bias=flow_bias, supply_bias=supply_bias,
            market_bias=market_bias, derivs_bias=derivs_bias, **plan_kwargs,
        )
        for a in proposed:
            if a.action in ("buy", "sell"):
                emitted.append({"asset": a.asset, "action": a.action, "created_at": a.created_at})
                actions.append(a)
    return actions


# ---- Scoring ----------------------------------------------------------------

@dataclass
class Trade:
    asset: str
    action: str
    horizon: str
    entry_at: str
    entry_px: float
    exit_at: str
    exit_px: float
    ret: float            # directional return (buy = +Δ, sell = −Δ)
    confidence: float
    catalysts: list[str] = field(default_factory=list)


def score(actions, oracle: PriceOracle, *, horizon_hours: dict | None = None) -> tuple[list[Trade], int]:
    """Turn proposed actions into scored trades; returns (trades, skipped_no_price)."""
    hh = {**DEFAULT_HORIZON_HOURS, **(horizon_hours or {})}
    trades: list[Trade] = []
    skipped = 0
    for a in actions:
        entry_dt = datetime.fromisoformat(a.created_at.replace("Z", "+00:00"))
        exit_dt = entry_dt + timedelta(hours=hh.get(a.horizon, 72.0))
        entry = oracle.price_at(a.asset, entry_dt)
        exit_ = oracle.price_at(a.asset, exit_dt)
        if entry is None or exit_ is None or entry == 0:
            skipped += 1
            continue
        move = exit_ / entry - 1.0
        ret = move if a.action == "buy" else -move
        trades.append(Trade(
            asset=a.asset, action=a.action, horizon=a.horizon,
            entry_at=entry_dt.isoformat(), entry_px=entry,
            exit_at=exit_dt.isoformat(), exit_px=exit_, ret=ret,
            confidence=a.confidence, catalysts=list(a.catalysts),
        ))
    return trades, skipped


# ---- Metrics ----------------------------------------------------------------

@dataclass
class BacktestResult:
    n: int
    scored: int
    skipped: int
    hit_rate: float
    mean_return: float
    median_return: float
    cum_return: float                 # compounded equal-weight unit returns
    by_horizon: dict
    by_catalyst: dict
    by_confidence: dict
    baseline_btc: float | None
    reliability: list = field(default_factory=list)   # [{bucket, n, stated, realized, gap}]
    calibration_error: float = 0.0                    # n-weighted mean |stated − realized|
    trades: list[Trade] = field(default_factory=list)
    portfolio: "PortfolioResult | None" = None


def _bucket_stats(trades: list[Trade], key) -> dict:
    out: dict[str, dict] = {}
    groups: dict[str, list[float]] = {}
    for t in trades:
        for k in key(t):
            groups.setdefault(k, []).append(t.ret)
    for k, rets in groups.items():
        out[k] = {"n": len(rets), "hit_rate": round(sum(r > 0 for r in rets) / len(rets), 3),
                  "mean_return": round(sum(rets) / len(rets), 4)}
    return out


def _conf_bucket(c: float) -> str:
    return "low(<0.4)" if c < 0.4 else "mid(0.4-0.7)" if c < 0.7 else "high(>=0.7)"


def _reliability(trades: list[Trade]) -> tuple[list, float]:
    """Reliability curve: does stated confidence predict the realized hit-rate?

    Buckets trades by confidence; for each, compares the mean stated confidence
    against the fraction that actually won. A well-calibrated planner has
    stated≈realized (small `gap`). Returns (curve, n-weighted mean |gap|).
    """
    buckets: dict[str, list[Trade]] = {}
    for t in trades:
        buckets.setdefault(_conf_bucket(t.confidence), []).append(t)
    curve: list[dict] = []
    err_num = err_den = 0.0
    for name in ("low(<0.4)", "mid(0.4-0.7)", "high(>=0.7)"):
        grp = buckets.get(name)
        if not grp:
            continue
        stated = sum(t.confidence for t in grp) / len(grp)
        realized = sum(t.ret > 0 for t in grp) / len(grp)
        gap = stated - realized
        curve.append({"bucket": name, "n": len(grp), "stated": round(stated, 3),
                      "realized": round(realized, 3), "gap": round(gap, 3)})
        err_num += abs(gap) * len(grp)
        err_den += len(grp)
    return curve, round(err_num / err_den, 3) if err_den else 0.0


def metrics(
    trades: list[Trade], *, n_actions: int, skipped: int,
    oracle: PriceOracle | None = None, start: datetime | None = None, end: datetime | None = None,
) -> BacktestResult:
    rets = [t.ret for t in trades]
    if rets:
        srt = sorted(rets)
        median = srt[len(srt) // 2]
        cum = 1.0
        for r in rets:
            cum *= (1 + r)
        cum -= 1.0
    else:
        median = cum = 0.0

    baseline = None
    if oracle is not None and start is not None and end is not None:
        p0, p1 = oracle.price_at("BTC", start), oracle.price_at("BTC", end)
        if p0 and p1:
            baseline = round(p1 / p0 - 1.0, 4)

    reliability, cal_err = _reliability(trades)
    return BacktestResult(
        n=n_actions, scored=len(trades), skipped=skipped,
        hit_rate=round(sum(r > 0 for r in rets) / len(rets), 3) if rets else 0.0,
        mean_return=round(sum(rets) / len(rets), 4) if rets else 0.0,
        median_return=round(median, 4),
        cum_return=round(cum, 4),
        by_horizon=_bucket_stats(trades, lambda t: [t.horizon]),
        by_catalyst=_bucket_stats(trades, lambda t: t.catalysts or ["(none)"]),
        by_confidence=_bucket_stats(trades, lambda t: [_conf_bucket(t.confidence)]),
        baseline_btc=baseline,
        reliability=reliability,
        calibration_error=cal_err,
        trades=trades,
    )


# ---- Phase 2: portfolio simulation (sizing + fees → equity curve) -----------

@dataclass
class PortfolioResult:
    deployed: int                     # trades actually entered (capital was available)
    skipped_no_capital: int
    total_return: float               # final equity vs 1.0 start
    sharpe: float                     # annualized, from daily equity returns
    max_drawdown: float               # worst peak-to-trough on the equity curve
    win_rate: float
    avg_win: float
    avg_loss: float
    profit_factor: float              # gross wins / gross losses
    fees_paid: float
    final_equity: float
    equity_curve: list = field(default_factory=list)   # [(iso_date, equity)]


def _daily_curve(events: list[tuple[str, float]], start: datetime) -> list[tuple[str, float]]:
    """Forward-fill the event-time equity onto a daily grid (for Sharpe / drawdown)."""
    last = datetime.fromisoformat(events[-1][0]) if events else start
    out: list[tuple[str, float]] = []
    eq, ci, day = 1.0, 0, start
    while day <= last:
        bound = (day + timedelta(days=1)).isoformat()
        while ci < len(events) and events[ci][0] < bound:
            eq = events[ci][1]
            ci += 1
        out.append((day.date().isoformat(), round(eq, 6)))
        day += timedelta(days=1)
    return out


def simulate_portfolio(
    trades: list[Trade], *, start: datetime, end: datetime,
    base_size: float = 0.2, max_position: float = 0.5, cost_bps: float = 10.0,
    oracle: PriceOracle | None = None,
) -> PortfolioResult:
    """Event-driven portfolio: size each trade by confidence, charge fees per side.

    Sizing = min(max_position, base_size × confidence) of current equity, capped
    by available cash (so concurrent trades compete for capital). Open positions
    are marked at cost until they close — trade-level, not mark-to-market.
    """
    rate = cost_bps / 10_000.0
    events = sorted(
        [(t.entry_at, "open", t) for t in trades] + [(t.exit_at, "close", t) for t in trades],
        key=lambda e: (e[0], e[1] == "open"),  # closes before opens at an equal timestamp
    )
    cash = 1.0
    open_notional: dict[int, float] = {}
    fees = 0.0
    deployed = skipped = 0
    realized: list[float] = []
    curve: list[tuple[str, float]] = []

    def equity() -> float:
        return cash + sum(open_notional.values())

    for ts, kind, tr in events:
        if kind == "open":
            target = min(max_position, base_size * tr.confidence) * equity()
            notional = min(target, cash / (1 + rate))  # leave room for the entry fee
            if notional <= 1e-9:
                skipped += 1
                continue
            fee = notional * rate
            cash -= notional + fee
            fees += fee
            open_notional[id(tr)] = notional
            deployed += 1
        else:
            notional = open_notional.pop(id(tr), None)
            if notional is None:
                continue
            pnl = notional * tr.ret
            gross = notional + pnl
            fee = abs(gross) * rate
            cash += gross - fee
            fees += fee
            realized.append(pnl)
        curve.append((ts, equity()))

    daily = _daily_curve(curve, start)
    rets = [daily[i][1] / daily[i - 1][1] - 1.0 for i in range(1, len(daily)) if daily[i - 1][1]]
    if len(rets) > 1:
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        sd = var ** 0.5
        sharpe = (mean / sd) * (365 ** 0.5) if sd else 0.0
    else:
        sharpe = 0.0
    peak = 1.0
    max_dd = 0.0
    for _, eq in daily:
        peak = max(peak, eq)
        max_dd = min(max_dd, eq / peak - 1.0)

    wins = [p for p in realized if p > 0]
    losses = [p for p in realized if p < 0]
    final_eq = daily[-1][1] if daily else 1.0
    baseline = None
    if oracle is not None:
        p0, p1 = oracle.price_at("BTC", start), oracle.price_at("BTC", end)
        if p0 and p1:
            baseline = round(p1 / p0 - 1.0, 4)

    return PortfolioResult(
        deployed=deployed, skipped_no_capital=skipped,
        total_return=round(final_eq - 1.0, 4), sharpe=round(sharpe, 3),
        max_drawdown=round(max_dd, 4),
        win_rate=round(len(wins) / len(realized), 3) if realized else 0.0,
        avg_win=round(sum(wins) / len(wins), 4) if wins else 0.0,
        avg_loss=round(sum(losses) / len(losses), 4) if losses else 0.0,
        profit_factor=round(sum(wins) / abs(sum(losses)), 3) if losses else float("inf") if wins else 0.0,
        fees_paid=round(fees, 4), final_equity=round(final_eq, 4), equity_curve=daily,
    )


def run_backtest(
    conn, *, start: datetime, end: datetime, step_hours: float = 24.0,
    horizon_hours: dict | None = None, period: str = "1d",
    signal_kwargs: dict | None = None, flow_scale: dict | None = None,
    supply_kwargs: dict | None = None, plan_kwargs: dict | None = None,
    market_kwargs: dict | None = None, derivs_kwargs: dict | None = None,
    indicator_lookback_days: float = 120.0,
    oracle: PriceOracle | None = None, portfolio_cfg: dict | None = None,
) -> BacktestResult:
    """Replay → fetch prices for the involved assets → score → aggregate.

    The oracle is built up front from the store's asset universe (so technicals
    have history during the replay) and reused for scoring. Pass `portfolio_cfg`
    (base_size/max_position/cost_bps) to also run the Phase-2 portfolio sim.
    """
    hh = {**DEFAULT_HORIZON_HOURS, **(horizon_hours or {})}
    max_h = max(hh.values()) if hh else 72.0
    if oracle is None:
        universe = {a for r in fetch_enriched(conn) for a in _row_assets(r)} | {"BTC"}
        oracle = PriceOracle.fetch(
            universe, start - timedelta(days=indicator_lookback_days),
            end + timedelta(hours=max_h), period=period,
        )
    actions = replay(
        conn, start=start, end=end, step_hours=step_hours,
        signal_kwargs=signal_kwargs, flow_scale=flow_scale,
        supply_kwargs=supply_kwargs, market_kwargs=market_kwargs,
        derivs_kwargs=derivs_kwargs, oracle=oracle, plan_kwargs=plan_kwargs,
    )
    trades, skipped = score(actions, oracle, horizon_hours=hh)
    result = metrics(trades, n_actions=len(actions), skipped=skipped,
                     oracle=oracle, start=start, end=end)
    if portfolio_cfg is not None:
        result.portfolio = simulate_portfolio(
            trades, start=start, end=end, oracle=oracle, **portfolio_cfg)
    return result
