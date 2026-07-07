"""Price oracle — historical prices for scoring backtested actions.

Source is the free DefiLlama coins API (`coins.llama.fi/chart`): one call per
asset returns a dated price series over the window, which we keep in memory for
nearest-timestamp lookup. No key. Symbol→coingecko-id mapping covers the majors
plus the `protocols.json` tokens; unmapped symbols are skipped (and reported as
uncovered by the backtest) rather than guessed.
"""

from __future__ import annotations

import bisect
import json
import sys
from datetime import datetime, timezone

import httpx

from .flows import HttpCache, _HEADERS

LLAMA_COINS = "https://coins.llama.fi"

# Ticker → coingecko id. Extend for more assets (alts carry a gecko_id in the
# DefiLlama emissions data if you need to wire more in).
SYMBOL_TO_GECKO = {
    "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "XRP": "ripple",
    "DOGE": "dogecoin", "ADA": "cardano", "BNB": "binancecoin", "AVAX": "avalanche-2",
    "LINK": "chainlink", "DOT": "polkadot", "MATIC": "matic-network",
    "ARB": "arbitrum", "OP": "optimism", "UNI": "uniswap", "AAVE": "aave",
    "LDO": "lido-dao", "APT": "aptos", "SUI": "sui", "TIA": "celestia",
}

_PERIOD_SECONDS = {"1h": 3600, "1d": 86400}
_DEFAULT_TOLERANCE = 2 * 86400  # a lookup more than ~2 days from any point = no data


def _to_unix(when) -> int:
    if isinstance(when, (int, float)):
        return int(when)
    if isinstance(when, str):
        when = datetime.fromisoformat(when.replace("Z", "+00:00"))
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return int(when.timestamp())


def _get_json(url: str, cache: HttpCache | None, *, timeout: float = 60.0):
    """Cached conditional GET → JSON, with last-good fallback."""
    cached = cache.get(url) if cache else None
    headers = {**_HEADERS, "Accept": "application/json"}
    if cached and cached.get("etag"):
        headers["If-None-Match"] = cached["etag"]
    try:
        resp = httpx.get(url, headers=headers, timeout=timeout, follow_redirects=True)
    except httpx.HTTPError as err:
        if cached:
            return json.loads(cached["body"])
        raise
    if resp.status_code == 304 and cached:
        return json.loads(cached["body"])
    if resp.status_code == 200 and resp.text:
        if cache:
            cache.set(url, etag=resp.headers.get("etag"),
                      last_modified=resp.headers.get("last-modified"), body=resp.text)
        return resp.json()
    if cached:
        return json.loads(cached["body"])
    raise RuntimeError(f"price fetch {url} failed: {resp.status_code} {resp.reason_phrase}")


class PriceOracle:
    """In-memory dated price series per symbol with nearest-timestamp lookup."""

    def __init__(self, series: dict[str, list[tuple[int, float]]], *, tolerance: int = _DEFAULT_TOLERANCE):
        # series: symbol -> sorted [(unix_ts, price)]
        self._series = {k: sorted(v) for k, v in series.items()}
        self._ts = {k: [t for t, _ in v] for k, v in self._series.items()}
        self.tolerance = tolerance

    @property
    def symbols(self) -> set[str]:
        return set(self._series)

    def history(self, symbols=None) -> dict[str, list[tuple[int, float]]]:
        """The raw {symbol: [(ts, price)]} series — feeds the technicals layer."""
        if symbols is None:
            return dict(self._series)
        return {s.upper(): self._series[s.upper()] for s in symbols if s.upper() in self._series}

    def price_at(self, symbol: str, when) -> float | None:
        """Nearest price to `when`, or None if no point within `tolerance`."""
        series = self._series.get(symbol.upper())
        if not series:
            return None
        ts = self._ts[symbol.upper()]
        target = _to_unix(when)
        i = bisect.bisect_left(ts, target)
        cands = []
        if i < len(ts):
            cands.append(i)
        if i > 0:
            cands.append(i - 1)
        best = min(cands, key=lambda j: abs(ts[j] - target))
        return series[best][1] if abs(ts[best] - target) <= self.tolerance else None

    @classmethod
    def fetch(
        cls, symbols, start, end, *, period: str = "1d", gecko_map: dict | None = None,
        cache: HttpCache | str | None = ".cache/prices_http.json",
    ) -> "PriceOracle":
        """Pull a price series per symbol from DefiLlama coins over [start, end]."""
        if isinstance(cache, str):
            cache = HttpCache(cache)
        gmap = {**SYMBOL_TO_GECKO, **(gecko_map or {})}
        start_u, end_u = _to_unix(start), _to_unix(end)
        span = int((end_u - start_u) / _PERIOD_SECONDS.get(period, 86400)) + 2
        series: dict[str, list[tuple[int, float]]] = {}
        for sym in {s.upper() for s in symbols}:
            gid = gmap.get(sym)
            if not gid:
                print(f"price oracle: no coingecko id for {sym}, skipped", file=sys.stderr)
                continue
            key = f"coingecko:{gid}"
            url = f"{LLAMA_COINS}/chart/{key}?start={start_u}&span={span}&period={period}"
            try:
                data = _get_json(url, cache)
            except Exception as err:  # noqa: BLE001 — one asset shouldn't sink the run
                print(f"price oracle {sym} skipped: {err}", file=sys.stderr)
                continue
            coin = (data.get("coins") or {}).get(key) or {}
            pts = coin.get("prices") or []
            if pts:
                series[sym] = [(int(p["timestamp"]), float(p["price"])) for p in pts]
        return cls(series)
