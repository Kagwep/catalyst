"""Learning data layer: record scores → resolve realized outcomes → log big moves.

All price data is a synthetic in-memory PriceOracle (no network), same trick as
test_backtest.py; storage is a tmp SQLite file via open_store.
"""

import json
from datetime import datetime, timedelta, timezone

from catalyst import learning
from catalyst.planner import Action
from catalyst.prices import PriceOracle
from catalyst.signals import Signal
from catalyst.store import fetch_outcomes, fetch_recent_moves, open_store

START = datetime(2026, 7, 1, tzinfo=timezone.utc)


def _ts(dt):
    return int(dt.timestamp())


def _sig(asset, score=0.5):
    return Signal(asset=asset, sentiment=score, strength=abs(score), score=score,
                  direction="bullish" if score > 0 else "bearish", mentions=3,
                  velocity=1.0, catalysts=["etf"], sources=["bluesky"],
                  latest_at=START.isoformat())


def _cfg(**over):
    moves = {**learning.DEFAULT_LEARNING["moves"], **over.pop("moves", {})}
    return {**learning.DEFAULT_LEARNING, **over, "moves": moves}


def _oracle():
    return PriceOracle({"BTC": [
        (_ts(START), 100.0),
        (_ts(START + timedelta(hours=1)), 105.0),
        (_ts(START + timedelta(hours=24)), 120.0),
    ]}, tolerance=3600)


def test_record_cycle_writes_snapshots_and_pending_outcomes(tmp_path):
    conn = open_store(str(tmp_path / "t.db"))
    try:
        ts = START.isoformat()
        act = Action(asset="BTC", action="buy", direction="bullish", confidence=0.7,
                     horizon="short", score=0.5, rationale="r",
                     layers={"macro": {"label": "risk_on"}})
        n = learning.record_cycle(conn, ts=ts, signals=[_sig("BTC")], actions=[act],
                                  horizons=[1.0, 24.0], cycle=7, oracle=_oracle())
        assert n == 1

        rows = fetch_outcomes(conn)
        assert len(rows) == 2 and all(r["status"] == "pending" for r in rows)
        assert {r["horizon_hours"] for r in rows} == {1.0, 24.0}
        r = rows[0]
        assert r["price_at_score"] == 100.0 and r["action"] == "buy" and r["cycle"] == 7
        assert json.loads(r["layers"])["macro"]["label"] == "risk_on"
        one = next(r for r in rows if r["horizon_hours"] == 1.0)
        assert one["due_at"] == (START + timedelta(hours=1)).isoformat()

        # Re-running the same cycle (crash/retry) writes nothing twice.
        learning.record_cycle(conn, ts=ts, signals=[_sig("BTC")], actions=[],
                              horizons=[1.0, 24.0])
        assert conn.execute("SELECT count(*) FROM score_snapshots").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM score_outcomes").fetchone()[0] == 2
    finally:
        conn.close()


def test_resolve_due_fills_labels_and_is_idempotent(tmp_path):
    conn = open_store(str(tmp_path / "t.db"))
    try:
        learning.record_cycle(conn, ts=START.isoformat(), signals=[_sig("BTC")],
                              actions=[], horizons=[1.0, 24.0], oracle=_oracle())

        # Two hours in: only the 1h horizon is due.
        res = learning.resolve_due(conn, now=START + timedelta(hours=2),
                                   oracle=_oracle(), cfg=_cfg())
        assert res["resolved"] == 1
        r = fetch_outcomes(conn, status="resolved")[0]
        assert r["entry_px"] == 100.0 and r["exit_px"] == 105.0
        assert abs(r["ret"] - 0.05) < 1e-9
        assert abs(r["btc_ret"] - 0.05) < 1e-9          # asset IS the baseline here
        assert len(fetch_outcomes(conn, status="pending")) == 1

        # Nothing newly due → second call is a no-op.
        assert learning.resolve_due(conn, now=START + timedelta(hours=2),
                                    oracle=_oracle(), cfg=_cfg())["resolved"] == 0

        # A day later the 24h label lands on the 120 print.
        assert learning.resolve_due(conn, now=START + timedelta(hours=25),
                                    oracle=_oracle(), cfg=_cfg())["resolved"] == 1
        day = fetch_outcomes(conn, horizon_hours=24.0)[0]
        assert abs(day["ret"] - 0.20) < 1e-9
    finally:
        conn.close()


def test_resolve_missing_price_retries_then_gives_up(tmp_path):
    conn = open_store(str(tmp_path / "t.db"))
    try:
        # ETH has no series in the oracle and no price captured at score time.
        learning.record_cycle(conn, ts=START.isoformat(), signals=[_sig("ETH")],
                              actions=[], horizons=[1.0])
        res = learning.resolve_due(conn, now=START + timedelta(hours=2),
                                   oracle=_oracle(), cfg=_cfg())
        assert res == {"resolved": 0, "gave_up": 0, "pending": 1}
        row = fetch_outcomes(conn)[0]
        assert row["status"] == "pending" and row["attempts"] == 1

        # Past due + give_up_hours the row is retired so the queue can't grow.
        res = learning.resolve_due(conn, now=START + timedelta(hours=50),
                                   oracle=_oracle(), cfg=_cfg())
        assert res["gave_up"] == 1
        assert fetch_outcomes(conn)[0]["status"] == "no_price"
    finally:
        conn.close()


def test_detect_moves_threshold_attribution_and_cooldown(tmp_path):
    conn = open_store(str(tmp_path / "t.db"))
    try:
        now = START + timedelta(hours=24)
        o = PriceOracle({"BTC": [(_ts(START), 100.0), (_ts(now), 110.0)]})
        cfg = _cfg(moves={"assets": ["BTC"]})
        enriched = [{"uri": "p1", "assets": json.dumps(["BTC"]),
                     "indexed_at": (now - timedelta(hours=3)).isoformat(),
                     "catalyst": "etf", "event": "etf approval", "sentiment_score": 0.8}]

        assert learning.detect_moves(conn, now=now, oracle=o,
                                     enriched_rows=enriched, cfg=cfg) == 1
        m = fetch_recent_moves(conn)[0]
        assert abs(m["ret"] - 0.10) < 1e-9
        assert m["explained"] == 1
        assert json.loads(m["catalysts"]) == ["etf"]
        assert json.loads(m["evidence"])[0]["uri"] == "p1"

        # Same drift 15 minutes later: suppressed by the cooldown.
        assert learning.detect_moves(conn, now=now + timedelta(minutes=15), oracle=o,
                                     enriched_rows=enriched, cfg=cfg) == 0
    finally:
        conn.close()


def test_detect_moves_unexplained_and_subthreshold(tmp_path):
    conn = open_store(str(tmp_path / "t.db"))
    try:
        now = START + timedelta(hours=24)
        cfg = _cfg(moves={"assets": ["BTC"]})

        # -7% with no covering news → recorded as unexplained.
        down = PriceOracle({"BTC": [(_ts(START), 100.0), (_ts(now), 93.0)]})
        assert learning.detect_moves(conn, now=now, oracle=down,
                                     enriched_rows=[], cfg=cfg) == 1
        m = fetch_recent_moves(conn)[0]
        assert m["explained"] == 0 and m["ret"] < 0

        # +2% is below the 5% threshold → nothing recorded (fresh store).
        conn2 = open_store(str(tmp_path / "t2.db"))
        try:
            flat = PriceOracle({"BTC": [(_ts(START), 100.0), (_ts(now), 102.0)]})
            assert learning.detect_moves(conn2, now=now, oracle=flat,
                                         enriched_rows=[], cfg=cfg) == 0
        finally:
            conn2.close()
    finally:
        conn.close()
