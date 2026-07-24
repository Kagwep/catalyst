"""Price oracle — historical prices for scoring backtested actions.

Primary source is the free DefiLlama coins API (`coins.llama.fi/chart`): one
call per asset returns a dated price series over the window, kept in memory for
nearest-timestamp lookup. No key. Symbol→coingecko-id mapping covers the majors
plus the `protocols.json` tokens.

Fallback source is **Hyperliquid** (`candleSnapshot`). DefiLlama/CoinGecko only
cover mainstream crypto, but a lot of the symbols we need to price arrive *from*
Hyperliquid's listing feed — equity perps (TSLA, COIN, HOOD…), commodity perps
(GLD, COPPER…), leveraged index tokens (HYPE3L, PUMP3S…) and HL-native coins
(HYPE…) — none of which have a coingecko id. Hyperliquid itself has a candle
history for every asset in its universe, so for any symbol we couldn't map to a
coingecko id we fall back to HL candles, gated to HL's committed universe file
(`hyperliquid_universe.json`) so we don't blindly POST for non-HL tickers.

Whatever neither source can price is reported once as a compact summary line
(not one line per symbol per cycle) rather than guessed.
"""

from __future__ import annotations

import bisect
import json
import sys
from datetime import datetime, timezone

import httpx

from .flows import HttpCache, _HEADERS

LLAMA_COINS = "https://coins.llama.fi"
HL_INFO = "https://api.hyperliquid.xyz/info"
DEFAULT_HL_UNIVERSE = "hyperliquid_universe.json"

# Ticker → coingecko id. Extend for more assets (alts carry a gecko_id in the
# DefiLlama emissions data if you need to wire more in). HL-native/leveraged/
# equity symbols deliberately live in the Hyperliquid fallback, not here.
SYMBOL_TO_GECKO = {
    "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "XRP": "ripple",
    "DOGE": "dogecoin", "ADA": "cardano", "BNB": "binancecoin", "AVAX": "avalanche-2",
    "LINK": "chainlink", "DOT": "polkadot", "MATIC": "matic-network",
    "ARB": "arbitrum", "OP": "optimism", "UNI": "uniswap", "AAVE": "aave",
    "LDO": "lido-dao", "APT": "aptos", "SUI": "sui", "TIA": "celestia",
    # Stablecoins + common majors that kept falling through the map.
    "USDC": "usd-coin", "USDT": "tether", "DAI": "dai",
    "HYPE": "hyperliquid", "ONDO": "ondo-finance", "SNX": "havven",
    "LTC": "litecoin", "BCH": "bitcoin-cash", "ETC": "ethereum-classic",
    "XLM": "stellar", "ALGO": "algorand", "ATOM": "cosmos", "NEAR": "near",
    "FIL": "filecoin", "INJ": "injective-protocol", "SEI": "sei-network",
    "PYTH": "pyth-network", "JUP": "jupiter-exchange-solana", "WIF": "dogwifcoin",
    "PEPE": "pepe", "BONK": "bonk", "RENDER": "render-token", "FET": "fetch-ai",
    "MKR": "maker", "CRV": "curve-dao-token", "GRT": "the-graph",
    "IMX": "immutable-x", "ENA": "ethena", "WLD": "worldcoin-wld",
    "STX": "blockstack", "DYDX": "dydx-chain", "STRK": "starknet",
}

_PERIOD_SECONDS = {"1h": 3600, "1d": 86400}
_HL_INTERVAL = {"1h": "1h", "1d": "1d"}
_DEFAULT_TOLERANCE = 2 * 86400  # a lookup more than ~2 days from any point = no data
_HL_HEADERS = {"Accept": "application/json",
               "User-Agent": "Catalyst/0.1 (+https://github.com/catalyst)"}
_DEFAULT_HL_MAX_CALLS = 80       # bound HL POSTs per fetch so a huge universe can't stall a cycle


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


def _hl_candles(coin: str, interval: str, start_ms: int, end_ms: int,
                *, timeout: float = 30.0) -> list[tuple[int, float]]:
    """Hyperliquid candle history for one coin → sorted [(unix_ts, close)].

    Empty list when HL doesn't list the coin (or returns nothing). Raises on a
    non-200 so the caller can fail-soft per symbol.
    """
    resp = httpx.post(
        HL_INFO,
        json={"type": "candleSnapshot",
              "req": {"coin": coin, "interval": interval,
                      "startTime": start_ms, "endTime": end_ms}},
        headers=_HL_HEADERS, timeout=timeout, follow_redirects=True,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"hyperliquid candles {coin}: {resp.status_code} {resp.reason_phrase}")
    rows = resp.json() or []
    out = [(int(r["t"]) // 1000, float(r["c"]))
           for r in rows if r.get("t") is not None and r.get("c") is not None]
    return sorted(out)


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
        hl_fallback: bool = True, hl_universe_file: str = DEFAULT_HL_UNIVERSE,
        hl_max_calls: int = _DEFAULT_HL_MAX_CALLS,
    ) -> "PriceOracle":
        """Price series per symbol over [start, end]: DefiLlama first, Hyperliquid fallback.

        Symbols with no coingecko id, or whose coingecko series came back empty,
        fall through to Hyperliquid candles (gated to HL's committed universe).
        Anything neither source can price is summarized in a single log line.
        """
        if isinstance(cache, str):
            cache = HttpCache(cache)
        gmap = {**SYMBOL_TO_GECKO, **(gecko_map or {})}
        start_u, end_u = _to_unix(start), _to_unix(end)
        span = int((end_u - start_u) / _PERIOD_SECONDS.get(period, 86400)) + 2
        series: dict[str, list[tuple[int, float]]] = {}
        unresolved: list[str] = []  # no gecko id, or gecko returned no data

        for sym in {s.upper() for s in symbols}:
            gid = gmap.get(sym)
            if not gid:
                unresolved.append(sym)
                continue
            key = f"coingecko:{gid}"
            url = f"{LLAMA_COINS}/chart/{key}?start={start_u}&span={span}&period={period}"
            try:
                data = _get_json(url, cache)
            except Exception as err:  # noqa: BLE001 — one asset shouldn't sink the run
                print(f"price oracle {sym} skipped: {err}", file=sys.stderr)
                unresolved.append(sym)
                continue
            coin = (data.get("coins") or {}).get(key) or {}
            pts = coin.get("prices") or []
            if pts:
                series[sym] = [(int(p["timestamp"]), float(p["price"])) for p in pts]
            else:
                unresolved.append(sym)

        if hl_fallback and unresolved:
            unresolved = cls._hl_backfill(
                unresolved, series, period=period, start_u=start_u, end_u=end_u,
                universe_file=hl_universe_file, max_calls=hl_max_calls,
            )

        if unresolved:
            shown = ", ".join(sorted(unresolved)[:20])
            more = f" (+{len(unresolved) - 20} more)" if len(unresolved) > 20 else ""
            print(f"price oracle: {len(unresolved)} symbol(s) unpriced "
                  f"(no coingecko id, not on hyperliquid): {shown}{more}", file=sys.stderr)
        return cls(series)

    @staticmethod
    def _hl_backfill(unresolved: list[str], series: dict, *, period: str,
                     start_u: int, end_u: int, universe_file: str, max_calls: int) -> list[str]:
        """Fill `series` from Hyperliquid candles; return the still-unresolved symbols."""
        interval = _HL_INTERVAL.get(period)
        if interval is None:
            return unresolved
        try:
            from .hl_events import load_baseline
            hl_universe = load_baseline(universe_file)
        except Exception:  # noqa: BLE001 — no baseline just means we can't gate; try best-effort
            hl_universe = None

        start_ms, end_ms = start_u * 1000, end_u * 1000
        still: list[str] = []
        calls = 0
        for sym in unresolved:
            # Only POST for coins HL actually lists (when we know the universe),
            # and never blow the per-cycle call budget.
            if (hl_universe is not None and sym not in hl_universe) or calls >= max_calls:
                still.append(sym)
                continue
            calls += 1
            try:
                pts = _hl_candles(sym, interval, start_ms, end_ms)
            except Exception:  # noqa: BLE001 — one coin shouldn't sink the fallback
                still.append(sym)
                continue
            if pts:
                series[sym] = pts
            else:
                still.append(sym)
        return still
