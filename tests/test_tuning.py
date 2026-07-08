from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from catalyst.enrich import make_anthropic_scorer
from catalyst.signals import compute_signals

NOW = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)


# ---- LLM scorer wiring (offline, via an injected stub client) ----

class _StubMessages:
    def __init__(self, out):
        self._out = out
        self.calls = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(parsed_output=self._out)


class _StubClient:
    def __init__(self, out):
        self.messages = _StubMessages(out)


def test_anthropic_scorer_maps_structured_output():
    out = SimpleNamespace(
        sentiment_label="positive", sentiment_score=1.7, assets=["btc", "eth"], catalyst="etf",
        event="Spot BTC ETF approved by regulator", severity="HIGH",
    )
    client = _StubClient(out)
    score = make_anthropic_scorer(model="claude-haiku-4-5", client=client)

    e = score("Spot BTC ETF approved")
    assert e.model == "claude-haiku-4-5"        # records which model scored it
    assert e.sentiment_label == "positive"
    assert e.sentiment_score == 1.0             # clamped into [-1, 1]
    assert e.assets == ["BTC", "ETH"]           # uppercased
    assert e.catalyst == "etf"
    assert e.event == "Spot BTC ETF approved by regulator"
    assert e.severity == "high"                 # normalized to lowercase enum
    # The model id was passed through to the API call.
    assert client.messages.calls[0]["model"] == "claude-haiku-4-5"


# ---- Weight overrides change signal output ----

def _row(asset, score, catalyst, source="bluesky", handle="x"):
    return {
        "uri": f"{asset}-{catalyst}",
        "source": source,
        "author_handle": handle,
        "indexed_at": (NOW - timedelta(hours=1)).isoformat(),
        "text": "t",
        "sentiment_score": score,
        "catalyst": catalyst,
        "assets": [asset],
        "likes": 0,
        "reposts": 0,
    }


def test_catalyst_weight_override_changes_strength():
    rows = [_row("BTC", 0.5, "hack")]
    base = compute_signals(rows, now=NOW)[0]
    # Zeroing the hack weight down sharply reduces its weighted volume → strength.
    tuned = compute_signals(rows, now=NOW, catalyst_weights={"hack": 0.5})[0]
    assert tuned.strength < base.strength


def test_source_weight_and_primary_boost_overrides():
    rows = [_row("ETH", 0.5, None, source="rss", handle="someblog")]
    base = compute_signals(rows, now=NOW)[0]
    tuned = compute_signals(rows, now=NOW, source_weights={"rss": 2.0})[0]
    assert tuned.strength > base.strength

    prows = [_row("SOL", 0.5, None, handle="watcher.guru")]
    low = compute_signals(prows, now=NOW, primary_handles=frozenset({"watcher.guru"}), primary_boost=1.0)[0]
    high = compute_signals(prows, now=NOW, primary_handles=frozenset({"watcher.guru"}), primary_boost=2.5)[0]
    assert high.strength > low.strength
