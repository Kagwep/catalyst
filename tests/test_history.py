from catalyst.flows import FlowBias
from catalyst.macro import MacroRegime, _fred_post
from catalyst.onchain import SupplyBias
from catalyst.store import fetch_bias_snapshots, open_store, save_bias_snapshots


def _db():
    return open_store(":memory:")


# ---- bias snapshots (the point-in-time history mechanism) -------------------

def test_save_bias_snapshots_flattens_all_layers():
    conn = _db()
    n = save_bias_snapshots(
        conn, "2026-06-15T12:00:00+00:00",
        regime=MacroRegime(0.4, "risk-on", 2.0, []),
        flow_bias={"BTC": FlowBias("BTC", 0.3, "accumulation", 100.0, [])},
        supply_bias={"ARB": SupplyBias("ARB", -0.5, "supply-pressure", 0.5, [])},
    )
    assert n == 3
    rows = fetch_bias_snapshots(conn)
    layers = {r["layer"]: r for r in rows}
    assert layers["macro"]["asset"] == "*" and layers["macro"]["bias"] == 0.4
    assert layers["flows"]["asset"] == "BTC"
    assert layers["supply"]["asset"] == "ARB" and layers["supply"]["bias"] == -0.5


def test_fetch_bias_snapshots_point_in_time_and_filters():
    conn = _db()
    save_bias_snapshots(conn, "2026-06-10T00:00:00+00:00",
                        supply_bias={"ARB": SupplyBias("ARB", -0.2, "supply-pressure", 0.2, [])})
    save_bias_snapshots(conn, "2026-06-14T00:00:00+00:00",
                        supply_bias={"ARB": SupplyBias("ARB", -0.6, "supply-pressure", 0.6, [])})
    # as-of an earlier time only sees the earlier snapshot (no lookahead)
    asof = fetch_bias_snapshots(conn, layer="supply", asset="ARB", before="2026-06-12T00:00:00+00:00")
    assert len(asof) == 1 and asof[0]["bias"] == -0.2
    # full history is oldest-first
    allrows = fetch_bias_snapshots(conn, layer="supply", asset="ARB")
    assert [r["bias"] for r in allrows] == [-0.2, -0.6]


def test_snapshots_skip_when_nothing_computed():
    conn = _db()
    assert save_bias_snapshots(conn, "2026-06-15T00:00:00+00:00") == 0
    assert fetch_bias_snapshots(conn) == []


# ---- FRED backfill: dated per-observation posts ----------------------------

def test_fred_post_is_dated_and_directional():
    cut = _fred_post("FEDFUNDS", "US Fed Funds Rate", {"date": "2026-05-01", "value": "4.00"},
                     {"date": "2026-04-01", "value": "4.25"})
    assert cut.indexed_at.startswith("2026-05-01")   # dated at the observation → replayable
    assert "eases" in cut.text                        # falling rate = easing = risk-on
    hike = _fred_post("FEDFUNDS", "US Fed Funds Rate", {"date": "2026-05-01", "value": "4.50"},
                      {"date": "2026-04-01", "value": "4.25"})
    assert "tightens" in hike.text
