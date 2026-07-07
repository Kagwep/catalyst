import json
from datetime import datetime, timedelta, timezone

from catalyst.backtest import Trade, metrics, replay, run_backtest, score, simulate_portfolio
from catalyst.planner import Action
from catalyst.prices import PriceOracle
from catalyst.store import open_store, save_enrichments, save_posts
from catalyst.models import Author, Post

START = datetime(2026, 5, 1, tzinfo=timezone.utc)
END = datetime(2026, 5, 10, tzinfo=timezone.utc)


def _ts(dt):
    return int(dt.timestamp())


# ---- price oracle -----------------------------------------------------------

def test_oracle_nearest_lookup_and_tolerance():
    series = {"BTC": [(_ts(START), 100.0), (_ts(START + timedelta(days=1)), 110.0)]}
    o = PriceOracle(series, tolerance=86400)
    assert o.price_at("BTC", START + timedelta(hours=1)) == 100.0      # nearest = day 0
    assert o.price_at("BTC", START + timedelta(hours=20)) == 110.0     # nearest = day 1
    assert o.price_at("BTC", START + timedelta(days=5)) is None        # beyond tolerance
    assert o.price_at("SOL", START) is None                            # unknown symbol


# ---- scoring ----------------------------------------------------------------

def _action(asset, act, when, horizon="short", conf=0.8, catalysts=None):
    return Action(asset=asset, action=act, direction="bullish" if act == "buy" else "bearish",
                  confidence=conf, horizon=horizon, score=0.5, rationale="r",
                  catalysts=catalysts or [], created_at=when.isoformat())


def test_score_directional_return_buy_and_sell():
    # BTC: 100 -> 120 over the 72h short horizon
    o = PriceOracle({"BTC": [(_ts(START), 100.0), (_ts(START + timedelta(hours=72)), 120.0)]},
                    tolerance=2 * 86400)
    buy, _ = score([_action("BTC", "buy", START)], o)
    assert abs(buy[0].ret - 0.20) < 1e-9          # buy gains on the up move
    sell, _ = score([_action("BTC", "sell", START)], o)
    assert abs(sell[0].ret + 0.20) < 1e-9         # sell loses on the up move


def test_score_skips_when_no_price():
    o = PriceOracle({"BTC": [(_ts(START), 100.0)]}, tolerance=3600)
    trades, skipped = score([_action("ARB", "buy", START)], o)  # ARB has no series
    assert trades == [] and skipped == 1


# ---- metrics ----------------------------------------------------------------

def test_metrics_aggregate_and_buckets():
    o = PriceOracle({
        "BTC": [(_ts(START), 100.0), (_ts(START + timedelta(hours=72)), 110.0)],
        "ETH": [(_ts(START), 100.0), (_ts(START + timedelta(hours=72)), 90.0)],
    }, tolerance=2 * 86400)
    actions = [
        _action("BTC", "buy", START, conf=0.8, catalysts=["etf"]),    # +0.10 win
        _action("ETH", "buy", START, conf=0.3, catalysts=["hack"]),   # -0.10 loss
    ]
    trades, skipped = score(actions, o)
    res = metrics(trades, n_actions=len(actions), skipped=skipped, oracle=o, start=START,
                  end=START + timedelta(hours=72))
    assert res.scored == 2 and res.hit_rate == 0.5
    assert abs(res.mean_return) < 1e-9                       # +0.10 and -0.10 average to 0
    assert res.by_confidence["high(>=0.7)"]["hit_rate"] == 1.0
    assert res.by_confidence["low(<0.4)"]["hit_rate"] == 0.0
    assert res.baseline_btc == 0.1                           # BTC 100 -> 110


# ---- replay (point-in-time) -------------------------------------------------

def _post(uri, dt, text, sym, sent):
    return Post(source="bluesky", uri=uri, text=text, created_at=dt.isoformat(),
                indexed_at=dt.isoformat(), author=Author(handle="watcher.guru"))


def _seed(conn, dt, sym, sentiment):
    p = _post(f"p:{sym}:{dt.isoformat()}", dt, f"${sym} bullish", sym, sentiment)
    save_posts(conn, [p])
    from catalyst.enrich import Enrichment
    save_enrichments(conn, [(p.uri, Enrichment(sentiment_score=sentiment, sentiment_label="positive",
                                               assets=[sym], catalyst=None, model="test"))])


def test_replay_is_point_in_time_no_lookahead():
    conn = open_store(":memory:")
    # three daily strongly-bullish BTC posts; a signal exists from each post's date on
    for d in range(3):
        _seed(conn, START + timedelta(days=d), "BTC", 0.9)
    # replay only up to day 1 -> must not see day-2's post
    actions = replay(conn, start=START, end=START + timedelta(days=1), step_hours=24.0,
                     plan_kwargs={"buy_threshold": 0.2, "cooldown_minutes": 0.0})
    assert actions, "expected BTC buys from the bullish posts"
    assert all(datetime.fromisoformat(a.created_at) <= START + timedelta(days=1) for a in actions)
    assert all(a.asset == "BTC" and a.action == "buy" for a in actions)


# ---- Phase 2: portfolio simulation -----------------------------------------

def _trade(asset, ret, day_open, hold_days=1, conf=1.0):
    o = START + timedelta(days=day_open)
    return Trade(asset=asset, action="buy", horizon="short", entry_at=o.isoformat(),
                 entry_px=100.0, exit_at=(o + timedelta(days=hold_days)).isoformat(),
                 exit_px=100.0 * (1 + ret), ret=ret, confidence=conf, catalysts=[])


def test_portfolio_full_size_no_fees_compounds():
    # two sequential winners at full size, no fees → 1.1 * 1.1 = 1.21
    trades = [_trade("BTC", 0.10, 0, 1), _trade("ETH", 0.10, 2, 1)]
    p = simulate_portfolio(trades, start=START, end=END, base_size=1.0, max_position=1.0, cost_bps=0.0)
    assert p.deployed == 2 and p.win_rate == 1.0
    assert abs(p.total_return - 0.21) < 1e-3
    assert p.fees_paid == 0.0 and p.max_drawdown == 0.0


def test_portfolio_fees_reduce_return():
    trades = [_trade("BTC", 0.10, 0, 1)]
    free = simulate_portfolio(trades, start=START, end=END, base_size=1.0, max_position=1.0, cost_bps=0.0)
    costly = simulate_portfolio(trades, start=START, end=END, base_size=1.0, max_position=1.0, cost_bps=100.0)
    assert costly.total_return < free.total_return and costly.fees_paid > 0


def test_portfolio_confidence_sizing_and_position_cap():
    # base_size 1.0 but max_position caps a full-confidence trade at 0.5 of equity
    capped = simulate_portfolio([_trade("BTC", 0.10, 0, 1, conf=1.0)],
                                start=START, end=END, base_size=1.0, max_position=0.5, cost_bps=0.0)
    assert abs(capped.total_return - 0.05) < 1e-3   # 0.5 deployed * 10% = +5%
    # lower confidence → smaller position → smaller return
    small = simulate_portfolio([_trade("BTC", 0.10, 0, 1, conf=0.4)],
                               start=START, end=END, base_size=0.5, max_position=1.0, cost_bps=0.0)
    assert abs(small.total_return - 0.02) < 1e-3    # 0.2 deployed * 10% = +2%


def test_portfolio_drawdown_and_profit_factor():
    # a loss then a recovery → non-zero max drawdown, profit_factor = wins/losses
    trades = [_trade("BTC", -0.20, 0, 1, conf=1.0), _trade("ETH", 0.50, 2, 1, conf=1.0)]
    p = simulate_portfolio(trades, start=START, end=END, base_size=1.0, max_position=1.0, cost_bps=0.0)
    assert p.max_drawdown < 0 and p.win_rate == 0.5
    assert p.profit_factor > 0


def test_run_backtest_attaches_portfolio():
    conn = open_store(":memory:")
    for d in range(3):
        _seed(conn, START + timedelta(days=d), "BTC", 0.9)
    o = PriceOracle({"BTC": [(_ts(START + timedelta(days=d)), 100.0 + 10 * d) for d in range(8)]},
                    tolerance=2 * 86400)
    res = run_backtest(conn, start=START, end=START + timedelta(days=3), step_hours=24.0,
                       plan_kwargs={"buy_threshold": 0.2, "cooldown_minutes": 0.0}, oracle=o,
                       portfolio_cfg={"base_size": 0.2, "max_position": 0.5, "cost_bps": 10.0})
    assert res.portfolio is not None and res.portfolio.deployed >= 1
    from dataclasses import asdict
    json.dumps(asdict(res), default=str)   # still serializable with portfolio attached


def test_run_backtest_end_to_end_with_injected_oracle():
    conn = open_store(":memory:")
    for d in range(3):
        _seed(conn, START + timedelta(days=d), "BTC", 0.9)
    o = PriceOracle({"BTC": [(_ts(START + timedelta(days=d)), 100.0 + 10 * d) for d in range(8)]},
                    tolerance=2 * 86400)
    res = run_backtest(conn, start=START, end=START + timedelta(days=3), step_hours=24.0,
                       plan_kwargs={"buy_threshold": 0.2, "cooldown_minutes": 0.0}, oracle=o)
    assert res.n >= 1 and res.scored >= 1
    assert res.hit_rate == 1.0          # rising price, all buys win
    # result is JSON-serializable (the CLI dumps it)
    from dataclasses import asdict
    json.dumps(asdict(res), default=str)
