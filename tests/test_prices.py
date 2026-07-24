from datetime import datetime, timezone

import catalyst.prices as prices
from catalyst.prices import PriceOracle

START = datetime(2026, 7, 1, tzinfo=timezone.utc)
END = datetime(2026, 7, 5, tzinfo=timezone.utc)


def _no_gecko(monkeypatch):
    """Make every DefiLlama lookup return no data → everything is unresolved."""
    monkeypatch.setattr(prices, "_get_json", lambda url, cache, **kw: {"coins": {}})


def test_hl_fallback_prices_unmapped_symbol(monkeypatch):
    _no_gecko(monkeypatch)
    # HL lists TSLA (an equity perp) and returns candles for it.
    monkeypatch.setattr(prices, "_hl_candles",
                        lambda coin, interval, s, e, **kw: [(1_700_000_000, 250.0)] if coin == "TSLA" else [])
    # Universe gate: pretend the committed baseline contains TSLA.
    monkeypatch.setattr("catalyst.hl_events.load_baseline", lambda path: {"TSLA"})

    o = PriceOracle.fetch(["TSLA"], START, END, cache=None)
    assert o.price_at("TSLA", 1_700_000_000) == 250.0


def test_hl_fallback_gated_to_universe(monkeypatch, capsys):
    _no_gecko(monkeypatch)
    called = []
    monkeypatch.setattr(prices, "_hl_candles",
                        lambda coin, *a, **kw: called.append(coin) or [(1_700_000_000, 1.0)])
    # Baseline only has TSLA; RANDOMTKN must NOT trigger an HL POST.
    monkeypatch.setattr("catalyst.hl_events.load_baseline", lambda path: {"TSLA"})

    o = PriceOracle.fetch(["TSLA", "RANDOMTKN"], START, END, cache=None)
    assert called == ["TSLA"]                      # only the HL-listed coin was queried
    assert "TSLA" in o.symbols and "RANDOMTKN" not in o.symbols
    err = capsys.readouterr().err
    assert "1 symbol(s) unpriced" in err and "RANDOMTKN" in err


def test_summary_line_collapses_spam(monkeypatch, capsys):
    _no_gecko(monkeypatch)
    monkeypatch.setattr(PriceOracle, "_hl_backfill",
                        staticmethod(lambda unresolved, series, **kw: unresolved))  # HL resolves nothing
    syms = [f"TKN{i}" for i in range(25)]
    PriceOracle.fetch(syms, START, END, cache=None)
    err = capsys.readouterr().err
    # One summary line, not 25 per-symbol lines.
    assert err.count("price oracle:") == 1
    assert "25 symbol(s) unpriced" in err and "+5 more" in err


def test_hl_call_budget_capped(monkeypatch):
    _no_gecko(monkeypatch)
    calls = []
    monkeypatch.setattr(prices, "_hl_candles", lambda coin, *a, **kw: calls.append(coin) or [])
    monkeypatch.setattr("catalyst.hl_events.load_baseline", lambda path: None)  # no gate
    PriceOracle.fetch([f"C{i}" for i in range(10)], START, END, cache=None, hl_max_calls=3)
    assert len(calls) == 3
