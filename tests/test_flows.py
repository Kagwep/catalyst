from datetime import datetime, timedelta, timezone

from catalyst.flows import (
    FlowBias,
    _flow_post,
    _parse_date,
    _parse_money,
    compute_flow_bias,
    parse_flow_table,
)

NOW = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)


# ---- parsing ----------------------------------------------------------------

def test_parse_money_parentheses_are_outflows():
    assert _parse_money("(213.9)") == -213.9e6   # parens = redemption
    assert _parse_money("85.9") == 85.9e6
    assert _parse_money("0.0") == 0.0
    assert _parse_money("1,234.5") == 1234.5e6
    assert _parse_money("-") is None and _parse_money("") is None


def test_parse_date():
    assert _parse_date("10 Jun 2026").startswith("2026-06-10T00:00:00+00:00")
    assert _parse_date("not a date") is None


def test_parse_flow_table_takes_total_and_drops_summary_rows():
    html = """<table>
    <tr><th>Date</th><th>IBIT</th><th>Total</th></tr>
    <tr><td>12 Jun 2026</td><td>50.0</td><td>85.9</td></tr>
    <tr><td>11 Jun 2026</td><td>(10.0)</td><td>(22.5)</td></tr>
    <tr><td>Total</td><td>x</td><td>9999</td></tr>
    </table>"""
    rows = parse_flow_table(html)
    # last cell (daily Total) is taken; the cumulative "Total" row is dropped
    assert rows == [
        ("2026-06-12T00:00:00+00:00", 85.9e6),
        ("2026-06-11T00:00:00+00:00", -22.5e6),
    ]


# ---- continuous per-asset bias ----------------------------------------------

def _post(asset, net_m, age_h=6.0):
    iso = (NOW - timedelta(hours=age_h)).isoformat()
    return _flow_post(asset, iso, net_m * 1e6, "u")


def test_inflow_is_accumulation_outflow_is_distribution():
    on = compute_flow_bias([_post("BTC", 800), _post("BTC", 600, 30)], now=NOW)
    assert on["BTC"].bias > 0.15 and on["BTC"].label == "accumulation"
    off = compute_flow_bias([_post("ETH", -300)], now=NOW)
    assert off["ETH"].bias < -0.15 and off["ETH"].label == "distribution"


def test_bias_is_per_asset_isolated():
    out = compute_flow_bias([_post("BTC", 800), _post("ETH", -300)], now=NOW)
    assert set(out) == {"BTC", "ETH"}
    assert out["BTC"].bias > 0 and out["ETH"].bias < 0  # one asset never bleeds into the other


def test_bias_saturates_and_stays_in_range():
    huge = compute_flow_bias([_post("BTC", 50_000)], now=NOW)["BTC"]
    assert 0.15 < huge.bias <= 1.0  # tanh keeps it bounded


def test_empty_and_stale_yield_no_bias():
    assert compute_flow_bias([], now=NOW) == {}
    # a flow far outside the window is ignored
    assert compute_flow_bias([_post("BTC", 800, age_h=500)], now=NOW) == {}


def test_recency_weighting_favours_fresh_flows():
    fresh = compute_flow_bias([_post("BTC", 500, age_h=1)], now=NOW)["BTC"].bias
    old = compute_flow_bias([_post("BTC", 500, age_h=80)], now=NOW)["BTC"].bias
    assert fresh > old > 0  # same size flow counts for more when recent


def test_flow_bias_reads_dict_rows_too():
    row = {"asset": "BTC", "net_usd": 900e6, "indexed_at": (NOW - timedelta(hours=2)).isoformat()}
    out = compute_flow_bias([row], now=NOW)
    assert isinstance(out["BTC"], FlowBias) and out["BTC"].bias > 0


# ---- planner integration ----------------------------------------------------

from catalyst.planner import plan  # noqa: E402
from catalyst.signals import Signal  # noqa: E402


def _sig(asset, score):
    return Signal(asset=asset, sentiment=score, strength=0.7, score=score,
                  direction="bullish" if score > 0 else "bearish", mentions=3, velocity=1.0,
                  catalysts=[], latest_at=(NOW - timedelta(minutes=5)).isoformat(), sample=["t"])


def test_flow_modifier_boosts_aligned_and_damps_divergent():
    accumulation = {"BTC": FlowBias("BTC", 0.8, "accumulation", 900.0, [])}
    base_buy = plan([_sig("BTC", 0.5)], now=NOW)[0]
    boosted = plan([_sig("BTC", 0.5)], now=NOW, flow_bias=accumulation, flow_weight=0.25)[0]
    # a buy with money flowing in is boosted...
    assert boosted.confidence > base_buy.confidence
    assert "flow accumulation" in boosted.rationale

    # ...and a buy while money flows OUT (divergence) is damped.
    distribution = {"BTC": FlowBias("BTC", -0.8, "distribution", 900.0, [])}
    damped = plan([_sig("BTC", 0.5)], now=NOW, flow_bias=distribution, flow_weight=0.25)[0]
    assert damped.confidence < base_buy.confidence


def test_flow_modifier_is_per_asset_and_ignores_unlisted():
    # a flow bias for BTC must not touch an ETH proposal
    btc_only = {"BTC": FlowBias("BTC", 0.8, "accumulation", 900.0, [])}
    base_eth = plan([_sig("ETH", 0.5)], now=NOW)[0]
    eth = plan([_sig("ETH", 0.5)], now=NOW, flow_bias=btc_only, flow_weight=0.25)[0]
    assert eth.confidence == base_eth.confidence and "flow" not in eth.rationale
