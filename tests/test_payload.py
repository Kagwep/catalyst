"""Canonical deliverable payload — the shared alert/Croo serialization seam."""

from __future__ import annotations

from catalyst.payload import (
    DISCLAIMER,
    SCHEMA,
    build_payload,
    requirements_to_kwargs,
    select_actions,
)
from catalyst.planner import plan
from catalyst.signals import Signal


def _sig(asset, score, **kw):
    return Signal(asset=asset, sentiment=score, strength=kw.get("strength", 0.6),
                  score=score, direction="bullish" if score > 0 else "bearish",
                  mentions=3, velocity=kw.get("velocity", 1.0),
                  catalysts=kw.get("catalysts", []), latest_at=None, sample=["t"])


def test_build_payload_shape_and_disclaimer():
    actions = plan([_sig("BTC", 0.5), _sig("ETH", -0.6)])
    p = build_payload(actions, meta={"regime": "risk-on"})
    assert p["schema"] == SCHEMA and p["version"] == "2.0"
    assert p["disclaimer"] == DISCLAIMER
    assert p["count"] == len(actions) == len(p["actions"])
    assert p["meta"]["regime"] == "risk-on"
    a0 = p["actions"][0]
    # Fully self-describing: every field a recipient needs, no outside context.
    for key in ("asset", "signal", "direction", "confidence", "horizon", "score",
                "catalysts", "layers", "rationale", "created_at"):
        assert key in a0
    assert a0["signal"] in ("alert", "watch")   # watch vocab, never buy/sell/hold
    assert "action" not in a0


def test_layers_surface_in_payload():
    from dataclasses import dataclass

    @dataclass
    class Bias:
        bias: float
        label: str

    # A buy with money flowing OUT → the flow layer should show as a damp.
    actions = plan([_sig("BTC", 0.5)], flow_bias={"BTC": Bias(-0.8, "distribution")})
    layers = build_payload(actions)["actions"][0]["layers"]
    assert layers["flow"]["effect"] == "damp"
    assert layers["flow"]["label"] == "distribution"


def test_select_actions_filters():
    actions = plan([_sig("BTC", 0.5), _sig("ETH", -0.6), _sig("SOL", 0.25)])
    only_btc = select_actions(actions, assets=["btc"])
    assert {a.asset for a in only_btc} == {"BTC"}
    bullish = select_actions(actions, directions=["bullish"])
    assert all(a.direction == "bullish" for a in bullish)
    alerts = select_actions(actions, signals=["alert"])          # buy/sell → alert
    assert all(a.action in ("buy", "sell") for a in alerts)
    strong = select_actions(actions, min_confidence=0.99)
    assert strong == []


def test_rationale_and_payload_are_self_describing():
    """A recipient (alert or Croo buyer) needs no outside context: the rationale
    names the asset/action/catalysts and every layer that moved confidence, and
    the structured payload mirrors it."""
    from dataclasses import dataclass

    @dataclass
    class Bias:
        bias: float
        label: str

    s = Signal(asset="BTC", sentiment=0.5, strength=0.7, score=0.5, direction="bullish",
               mentions=4, velocity=1.2, catalysts=["etf"], latest_at=None, sample=["t"])
    a = plan([s], flow_bias={"BTC": Bias(0.6, "accumulation")},
             market_bias={"BTC": Bias(0.5, "bullish-momentum")})[0]

    # rationale: watch-signal framing (direction + tier, no buy/sell), asset,
    # the catalyst, and each active layer by name
    for token in ("BULLISH", "ALERT", "BTC", "etf", "flow", "market"):
        assert token in a.rationale
    assert "BUY" not in a.rationale and "SELL" not in a.rationale

    # payload: structured twin carries the same layers + all decision fields
    d = build_payload([a])["actions"][0]
    assert d["catalysts"] == ["etf"]
    assert set(d["layers"]) == {"flow", "market"}
    assert d["layers"]["flow"]["effect"] == "boost"
    assert d["confidence"] == a.confidence and d["horizon"] == a.horizon


def test_flatten_signals_single_object_matches_registered_schema():
    from catalyst.payload import flatten_signals

    # top signal by confidence is the single delivered object
    actions = plan([_sig("BTC", 0.5, catalysts=["etf"]), _sig("ETH", -0.6, catalysts=["unlock"])])
    flat = flatten_signals(build_payload(actions, meta={"universe": ["BTC", "ETH"],
                                                        "requirements": {"assets": ["BTC"]}}))
    a = flat["actions"]
    # actions is ONE flat object (not array, not asset-keyed) with asset as a field
    assert isinstance(a, dict) and "asset" in a and a["signal"] == "alert"
    for key in ("asset", "signal", "direction", "confidence", "score", "horizon",
                "freshness", "rationale", "created_at"):
        assert key in a
    assert "freshness_minutes" not in a                 # registered name is `freshness`
    # catalysts is a flat array of strings; layers a flat object; universe required
    assert isinstance(flat["catalysts"], list) and all(isinstance(c, str) for c in flat["catalysts"])
    assert isinstance(flat["layers"], dict)
    assert flat["universe"] == ["BTC", "ETH"]
    assert flat["requirements"] == {"assets": ["BTC"]}
    assert "meta" not in flat


def test_flatten_signals_empty_delivers_neutral_watch():
    """No signal for the requested asset must still satisfy the registered
    schema's required `actions` fields — a neutral `watch`, not an empty object
    (which would INVALID_DELIVERABLE → SLA-expire)."""
    from catalyst.payload import build_payload, flatten_signals

    flat = flatten_signals(build_payload(
        [], meta={"universe": ["BTC", "ETH"], "requirements": {"assets": ["SOL"]}}))
    a = flat["actions"]
    assert isinstance(a, dict)
    for key in ("asset", "signal", "direction", "confidence", "score", "horizon",
                "freshness", "rationale", "created_at"):
        assert key in a
    assert a["asset"] == "SOL"                # echoes what the buyer asked for
    assert a["signal"] == "watch" and a["direction"] == "neutral"
    assert a["confidence"] == 0.0
    assert flat["count"] == 0                 # count still reflects "no real signal"
    assert flat["catalysts"] == [] and flat["layers"] == {}


def test_requirements_to_kwargs_tolerates_scalars():
    kw = requirements_to_kwargs({"assets": "BTC", "horizon": "intraday",
                                 "signal": "alert", "direction": "bullish",
                                 "min_confidence": 0.3})
    assert kw["assets"] == ["BTC"]
    assert kw["horizons"] == ["intraday"]
    assert kw["signals"] == ["alert"]
    assert kw["directions"] == ["bullish"]
    assert kw["min_confidence"] == 0.3
    # end-to-end: the Croo requirements front door narrows the same Action[].
    actions = plan([_sig("BTC", 0.5), _sig("ETH", -0.6)])
    picked = select_actions(actions, **requirements_to_kwargs({"assets": ["ETH"]}))
    assert {a.asset for a in picked} == {"ETH"}


def test_requirements_assets_comma_string_and_singular_key():
    """Dashboard v2 can't register an array-of-strings requirements field, so
    buyers send `assets` as a comma-separated string — and some forms use the
    singular key `asset`. Both must normalize to a ticker list."""
    assert requirements_to_kwargs({"assets": "BTC, ETH ,SOL"})["assets"] == ["BTC", "ETH", "SOL"]
    assert requirements_to_kwargs({"asset": "DOGE"})["assets"] == ["DOGE"]
    assert requirements_to_kwargs({"assets": ""})["assets"] is None       # empty → no filter
    # Dashboard string field can arrive double-encoded with literal quotes.
    assert requirements_to_kwargs({"assets": '"BTC"'})["assets"] == ["BTC"]
    assert requirements_to_kwargs({"assets": '"BTC","ETH"'})["assets"] == ["BTC", "ETH"]
    actions = plan([_sig("BTC", 0.5), _sig("ETH", -0.6)])
    picked = select_actions(actions, **requirements_to_kwargs({"assets": "ETH"}))
    assert {a.asset for a in picked} == {"ETH"}
