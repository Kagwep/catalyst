from datetime import datetime, timedelta, timezone

from catalyst.enrich import LexiconScorer
from catalyst.macro import MacroRegime, compute_regime
from catalyst.planner import plan
from catalyst.signals import Signal

NOW = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)


def _macro_row(text, score, age_h=1.0):
    return {
        "uri": text,
        "source": "macro",
        "author_handle": "fed",
        "indexed_at": (NOW - timedelta(hours=age_h)).isoformat(),
        "text": text,
        "sentiment_score": score,
        "catalyst": "macro",
    }


def test_macro_sentiment_direction():
    # easing = risk-on (positive); hiking = risk-off (negative)
    assert LexiconScorer().score("Fed cuts rates, signals dovish stance").sentiment_label == "positive"
    assert LexiconScorer().score("ECB hikes rates, hawkish tightening").sentiment_label == "negative"


def test_macro_text_classifies_as_macro_catalyst():
    e = LexiconScorer().score("[FED] FOMC minutes discuss inflation and monetary policy")
    assert e.catalyst == "macro"


def test_compute_regime_risk_on_vs_off():
    on = compute_regime([_macro_row("Fed cuts rates dovish", 0.5)], now=NOW)
    assert on.label == "risk-on" and on.score > 0
    off = compute_regime([_macro_row("ECB hikes hawkish tightening", -0.5)], now=NOW)
    assert off.label == "risk-off" and off.score < 0
    empty = compute_regime([], now=NOW)
    assert empty.label == "neutral" and empty.score == 0.0


def test_regime_ignores_non_macro_rows():
    rows = [{"uri": "x", "source": "bluesky", "catalyst": "hack", "sentiment_score": -0.9,
             "indexed_at": NOW.isoformat()}]
    assert compute_regime(rows, now=NOW).evidence == 0.0


def _sig(asset, score):
    return Signal(asset=asset, sentiment=score, strength=0.7, score=score,
                  direction="bullish" if score > 0 else "bearish", mentions=3, velocity=1.0,
                  catalysts=[], latest_at=(NOW - timedelta(minutes=5)).isoformat(), sample=["t"])


def test_regime_modifier_boosts_aligned_damps_opposed():
    risk_on = MacroRegime(0.8, "risk-on", 2.0, [])
    base_buy = plan([_sig("BTC", 0.5)], now=NOW)[0]
    boosted_buy = plan([_sig("BTC", 0.5)], now=NOW, regime=risk_on, macro_weight=0.3)[0]
    # A buy in a risk-on regime gets a confidence boost...
    assert boosted_buy.confidence > base_buy.confidence
    assert "macro risk-on" in boosted_buy.rationale

    # ...and a sell (against risk-on) is damped.
    base_sell = plan([_sig("ETH", -0.5)], now=NOW)[0]
    damped_sell = plan([_sig("ETH", -0.5)], now=NOW, regime=risk_on, macro_weight=0.3)[0]
    assert damped_sell.confidence < base_sell.confidence
