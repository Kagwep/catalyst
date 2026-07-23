"""Prediction-market layer — Polymarket + Kalshi odds shifts as catalyst events.

Prediction markets price catalyst *probabilities* directly ("ETF approved by
date X", "Fed cuts in September"), so a large odds swing is often the earliest
tradable read on an event — ahead of the news wires that report it. This layer
watches high-volume markets and emits a post only when the 24h probability
shift clears a threshold; the post text carries the market question, so the
normal LLM enrichment reads the event, tags assets/catalyst/severity, and it
flows through scoring exactly like news.

Both APIs are free, keyless reads:
  - **Polymarket** Gamma lists active markets by 24h volume; the CLOB
    prices-history endpoint gives a dated outcome-price series, so the shift
    is measured, not inferred.
  - **Kalshi** public market data, targeted per macro series (Fed, CPI) —
    market-implied odds complementing the FRED/central-bank macro source.

Learning path: `source="predictions"` is in `store.NEWS_SOURCES`, so these
posts are LLM-enriched, feed signals, and land in the score→outcome records
(`score_snapshots` / `score_outcomes`); the odds themselves accrete in the
posts table (`raw` carries probability/shift/volume) as features for the
learned models later.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone

import httpx

from .models import Author, Post

GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
CLOB_HISTORY_URL = "https://clob.polymarket.com/prices-history"
KALSHI_MARKETS_URL = "https://api.elections.kalshi.com/trade-api/v2/markets"
_HEADERS = {"Accept": "application/json", "User-Agent": "Catalyst/0.1 (+https://github.com/catalyst)"}

# Crypto + macro relevance gate for the Polymarket firehose (word-boundary match
# on the market question). Extend via config `predictions.polymarket.terms`.
DEFAULT_TERMS = (
    "bitcoin", "btc", "ethereum", "eth", "solana", "sol", "xrp", "crypto",
    "stablecoin", "etf", "sec", "fed", "fomc", "rate cut", "rate hike",
    "interest rate", "cpi", "inflation", "recession", "tariff",
)

DEFAULT_KALSHI_SERIES = ("KXFEDDECISION", "KXCPIYOY")

DEFAULT_MIN_SHIFT = 0.10          # 10 probability points in 24h = an event
DEFAULT_POLY_MIN_VOLUME = 50_000.0   # USD 24h volume floor (Polymarket)
DEFAULT_KALSHI_MIN_VOLUME = 500      # contracts traded in 24h floor (Kalshi)


def _get(url: str, params: dict, *, timeout: float = 30.0):
    resp = httpx.get(url, params=params, headers=_HEADERS, timeout=timeout, follow_redirects=True)
    if resp.status_code != 200:
        raise RuntimeError(f"predictions {url} failed: {resp.status_code} {resp.reason_phrase}")
    return resp.json()


def _matches_terms(text: str, terms) -> bool:
    low = text.lower()
    return any(re.search(rf"\b{re.escape(t.lower())}\b", low) for t in terms)


def _jlist(v) -> list:
    """Gamma encodes list fields (`outcomes`, `clobTokenIds`) as JSON strings."""
    if isinstance(v, list):
        return v
    try:
        out = json.loads(v or "[]")
        return out if isinstance(out, list) else []
    except (TypeError, json.JSONDecodeError):
        return []


# ---- Polymarket -------------------------------------------------------------

def select_polymarket(markets: list[dict], *, terms=None, min_volume_24h: float = DEFAULT_POLY_MIN_VOLUME,
                      max_markets: int = 20) -> list[dict]:
    """Relevance-filter the volume-ranked market list (pure; testable)."""
    terms = terms or DEFAULT_TERMS
    out = []
    for m in markets:
        if not m.get("active") or m.get("closed"):
            continue
        if float(m.get("volume24hr") or 0.0) < min_volume_24h:
            continue
        if not _matches_terms(m.get("question") or "", terms):
            continue
        out.append(m)
        if len(out) >= max_markets:
            break
    return out


def polymarket_shift_post(market: dict, history: list[dict], *, min_shift: float = DEFAULT_MIN_SHIFT,
                          now: datetime | None = None) -> Post | None:
    """One odds-shift post from a market + its dated price history, or None.

    `history` is the CLOB series for the first outcome's token: [{"t": sec,
    "p": prob}] over ~24h. The shift is last−first — measured, not inferred.
    """
    if len(history) < 2:
        return None
    pts = sorted(history, key=lambda h: h.get("t") or 0)
    first, last = float(pts[0].get("p") or 0.0), float(pts[-1].get("p") or 0.0)
    shift = last - first
    if abs(shift) < min_shift:
        return None
    now = now or datetime.now(timezone.utc)
    iso = datetime.fromtimestamp(int(pts[-1]["t"]), tz=timezone.utc).isoformat() \
        if pts[-1].get("t") else now.isoformat()
    question = market.get("question") or "?"
    outcome = (_jlist(market.get("outcomes")) or ["Yes"])[0]
    vol = float(market.get("volume24hr") or 0.0)
    events = market.get("events") or []
    slug = (events[0].get("slug") if events else None) or market.get("slug") or ""
    return Post(
        source="predictions",
        uri=f"polymarket:{market.get('id')}:{now.date().isoformat()}",
        url=f"https://polymarket.com/event/{slug}" if slug else None,
        text=(f"[PREDICTION] {question} — {outcome} odds moved {first * 100:.0f}%"
              f" -> {last * 100:.0f}% ({shift * 100:+.0f}pp/24h,"
              f" Polymarket ${vol:,.0f} 24h vol)"),
        created_at=iso, indexed_at=iso,
        author=Author(handle="predictions", display_name="Polymarket"),
        raw={"kind": "odds_shift", "platform": "polymarket",
             "market_id": market.get("id"), "question": question, "outcome": outcome,
             "prob": last, "shift_24h": shift, "volume_24h": vol},
    )


def fetch_polymarket(*, terms=None, min_volume_24h: float = DEFAULT_POLY_MIN_VOLUME,
                     max_markets: int = 20, min_shift: float = DEFAULT_MIN_SHIFT) -> list[Post]:
    """Odds-shift posts for relevant high-volume Polymarket markets."""
    markets = _get(GAMMA_MARKETS_URL, {"closed": "false", "order": "volume24hr",
                                       "ascending": "false", "limit": 100})
    out: list[Post] = []
    for m in select_polymarket(markets, terms=terms, min_volume_24h=min_volume_24h,
                               max_markets=max_markets):
        tokens = _jlist(m.get("clobTokenIds"))
        if not tokens:
            continue
        try:
            hist = _get(CLOB_HISTORY_URL, {"market": tokens[0], "interval": "1d",
                                           "fidelity": 60}).get("history") or []
        except Exception as err:  # noqa: BLE001 — one market's history shouldn't sink the rest
            print(f"polymarket history {m.get('id')} skipped: {err}", file=sys.stderr)
            continue
        post = polymarket_shift_post(m, hist, min_shift=min_shift)
        if post:
            out.append(post)
    return out


# ---- Kalshi -----------------------------------------------------------------

def _kalshi_prob(m: dict, field: str) -> float | None:
    """Price as a probability. The API is migrating from integer-cent fields
    (`last_price`) to dollar strings (`last_price_dollars`); read either."""
    v = m.get(field)
    if v is not None:
        return v / 100.0
    d = m.get(f"{field}_dollars")
    try:
        return float(d) if d is not None else None
    except (TypeError, ValueError):
        return None


def _kalshi_volume(m: dict) -> float:
    """24h contract volume across the int (`volume_24h`) / string (`_fp`) forms."""
    v = m.get("volume_24h")
    if v is not None:
        return float(v)
    try:
        return float(m.get("volume_24h_fp") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def kalshi_shift_posts(markets: list[dict], *, min_shift: float = DEFAULT_MIN_SHIFT,
                       min_volume_24h: int = DEFAULT_KALSHI_MIN_VOLUME,
                       now: datetime | None = None) -> list[Post]:
    """Odds-shift posts from Kalshi market rows (pure; testable).

    `previous_price` is the prior day's last, so last−previous is the 24h
    shift without a second call.
    """
    now = now or datetime.now(timezone.utc)
    iso = now.isoformat()
    out: list[Post] = []
    for m in markets:
        last, prev = _kalshi_prob(m, "last_price"), _kalshi_prob(m, "previous_price")
        vol = _kalshi_volume(m)
        if last is None or prev is None or vol < min_volume_24h:
            continue
        shift = last - prev
        if abs(shift) < min_shift:
            continue
        title = m.get("title") or m.get("ticker") or "?"
        event = (m.get("event_ticker") or m.get("ticker") or "").lower()
        out.append(Post(
            source="predictions",
            uri=f"kalshi:{m.get('ticker')}:{now.date().isoformat()}",
            url=f"https://kalshi.com/markets/{event}" if event else None,
            text=(f"[PREDICTION] {title} — Yes odds moved {prev * 100:.0f}%"
                  f" -> {last * 100:.0f}%"
                  f" ({shift * 100:+.0f}pp/24h, Kalshi {vol:,.0f} contracts 24h)"),
            created_at=iso, indexed_at=iso,
            author=Author(handle="predictions", display_name="Kalshi"),
            raw={"kind": "odds_shift", "platform": "kalshi",
                 "market_id": m.get("ticker"), "question": title,
                 "prob": last, "shift_24h": shift, "volume_24h": vol},
        ))
    return out


def fetch_kalshi(*, series=None, min_shift: float = DEFAULT_MIN_SHIFT,
                 min_volume_24h: int = DEFAULT_KALSHI_MIN_VOLUME) -> list[Post]:
    """Odds-shift posts across the configured Kalshi macro series."""
    markets: list[dict] = []
    for s in series or DEFAULT_KALSHI_SERIES:
        try:
            data = _get(KALSHI_MARKETS_URL, {"series_ticker": s, "status": "open", "limit": 100})
            markets.extend(data.get("markets") or [])
        except Exception as err:  # noqa: BLE001 — one series shouldn't sink the rest
            print(f"kalshi series {s} skipped: {err}", file=sys.stderr)
    return kalshi_shift_posts(markets, min_shift=min_shift, min_volume_24h=min_volume_24h)


def fetch_predictions(*, polymarket: dict | None = None, kalshi: dict | None = None) -> list[Post]:
    """Both platforms, each fail-soft (an outage on one shouldn't mute the other)."""
    out: list[Post] = []
    if polymarket is not None:
        try:
            out += fetch_polymarket(**polymarket)
        except Exception as err:  # noqa: BLE001
            print(f"polymarket skipped: {err}", file=sys.stderr)
    if kalshi is not None:
        try:
            out += fetch_kalshi(**kalshi)
        except Exception as err:  # noqa: BLE001
            print(f"kalshi skipped: {err}", file=sys.stderr)
    return out
