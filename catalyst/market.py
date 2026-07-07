"""Market-data layer — price technicals (RSI/MACD) + Fear & Greed.

The price-momentum inputs the news/supply layers don't capture, so the planner
can express a momentum strategy (RSI + MACD + Fear & Greed).

Both inputs are free, no key, and historical (so this layer is backtestable):
  - **Technicals** are computed from the price series fetched via the
    DefiLlama-backed `PriceOracle`.
  - **Fear & Greed** comes dated from alternative.me.

Output is a continuous per-asset **market bias** (-1 bearish momentum .. +1 bullish
momentum): RSI above/below 50 and the MACD histogram, nudged by the market-wide
Fear & Greed reading. It plugs into the planner exactly like flows/supply.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

from .flows import HttpCache, _HEADERS
from .models import Author, Post

ALT_FNG = "https://api.alternative.me/fng/"


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


# ---- Indicators (pure) ------------------------------------------------------

def rsi(prices: list[float], period: int = 14) -> float | None:
    """Wilder's RSI over a chronological price list; None if too few points."""
    if len(prices) < period + 1:
        return None
    deltas = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def _ema(values: list[float], span: int) -> list[float]:
    k = 2.0 / (span + 1)
    e = values[0]
    out = [e]
    for v in values[1:]:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def macd_hist(prices: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> float | None:
    """Last MACD histogram value (macd line - signal line); None if too few points."""
    if len(prices) < slow + signal:
        return None
    line = [f - s for f, s in zip(_ema(prices, fast), _ema(prices, slow))]
    sig = _ema(line, signal)
    return line[-1] - sig[-1]


# ---- Fear & Greed -----------------------------------------------------------

def fetch_fear_greed(
    *, limit: int = 1, cache: HttpCache | str | None = ".cache/market_http.json",
) -> list[Post]:
    """Fear & Greed index as dated `market` posts (free via alternative.me)."""
    if isinstance(cache, str):
        cache = HttpCache(cache)
    data = httpx.get(ALT_FNG, headers={**_HEADERS, "Accept": "application/json"},
                     params={"limit": limit, "format": "json"}, timeout=20.0).json()
    out: list[Post] = []
    for d in data.get("data", []):
        ts = int(d["timestamp"])
        iso = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        val = int(d["value"])
        out.append(Post(
            source="market",
            uri=f"market:fng:{iso[:10]}",
            url="https://alternative.me/crypto/fear-and-greed-index/",
            text=f"[FEAR&GREED] {val} ({d.get('value_classification', '')})",
            created_at=iso, indexed_at=iso,
            author=Author(handle="feargreed", display_name="Fear & Greed Index"),
            raw={"kind": "fng", "value": val, "classification": d.get("value_classification", "")},
        ))
    return out


def _latest_fng(rows, now: datetime) -> int | None:
    """Most recent F&G value at or before `now`, from market posts/rows."""
    best_ts, best_val = "", None
    for row in rows:
        r = row.raw if isinstance(row, Post) else (row.get("raw") if isinstance(row.get("raw"), dict)
                                                   else (json.loads(row["raw"]) if row.get("raw") else {}))
        if (r or {}).get("kind") != "fng":
            continue
        ts = row.indexed_at if isinstance(row, Post) else row.get("indexed_at") or ""
        if ts <= now.isoformat() and ts > best_ts:
            best_ts, best_val = ts, r.get("value")
    return best_val


# ---- Per-asset market bias --------------------------------------------------

@dataclass
class MarketBias:
    asset: str
    bias: float                        # -1 bearish momentum .. +1 bullish momentum
    label: str                         # bullish-momentum | neutral | bearish-momentum
    rsi: float | None = None
    fng: int | None = None
    drivers: list[str] = field(default_factory=list)


def compute_technicals(
    price_history: dict[str, list[tuple[int, float]]], *, now: datetime | None = None,
    macd_scale: float = 20.0,
) -> dict[str, tuple[float, float | None]]:
    """Per-asset momentum component in [-1,1] from RSI + MACD. Returns {asset: (tech, rsi)}."""
    now_ts = int((now or datetime.now(timezone.utc)).timestamp())
    out: dict[str, tuple[float, float | None]] = {}
    for asset, series in price_history.items():
        prices = [p for ts, p in sorted(series) if ts <= now_ts]
        if len(prices) < 15:
            continue
        r = rsi(prices)
        h = macd_hist(prices)
        if r is None and h is None:
            continue
        rsi_c = (r - 50.0) / 50.0 if r is not None else 0.0
        macd_c = math.tanh((h / prices[-1]) * macd_scale) if (h is not None and prices[-1]) else 0.0
        tech = max(-1.0, min(1.0, 0.5 * rsi_c + 0.5 * macd_c))
        out[asset] = (tech, r)
    return out


def compute_market_bias(
    price_history: dict[str, list[tuple[int, float]]], fng_rows=None, *,
    now: datetime | None = None, fng_weight: float = 0.3, macd_scale: float = 20.0,
) -> dict[str, MarketBias]:
    """Per-asset momentum bias (RSI+MACD) nudged by market-wide Fear & Greed."""
    now = now or datetime.now(timezone.utc)
    fng = _latest_fng(fng_rows or [], now)
    fng_c = (fng - 50.0) / 50.0 if fng is not None else 0.0   # greed = bullish, fear = bearish
    tech = compute_technicals(price_history, now=now, macd_scale=macd_scale)
    out: dict[str, MarketBias] = {}
    for asset, (t, r) in tech.items():
        bias = max(-1.0, min(1.0, t + fng_weight * fng_c))
        label = ("bullish-momentum" if bias > 0.15 else "bearish-momentum" if bias < -0.15 else "neutral")
        drivers = []
        if r is not None:
            drivers.append(f"RSI {r:.0f}")
        if fng is not None:
            drivers.append(f"F&G {fng}")
        out[asset] = MarketBias(asset, round(bias, 3), label, round(r, 1) if r is not None else None, fng, drivers)
    return out
