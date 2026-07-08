"""Derivatives layer — perp funding + open interest as a **positioning** read.

The demand/supply layers (flows, on-chain) say what money *did*; derivatives say
how *crowded* and *leveraged* the current bet is — the contrarian axis. The core
signal is the **perpetual funding rate**: persistently positive funding means
longs are paying to stay long (crowded longs, squeeze risk) → fade bullishness;
persistently negative funding means crowded shorts → fade bearishness. That's the
same divergence logic as flows, applied to leverage.

Direction convention for the per-asset bias: **+1 = crowded shorts (bullish, room
to squeeze up), −1 = crowded longs (bearish, over-leveraged)**. So in the planner
it damps a buy into crowded longs and boosts a buy into crowded shorts.

Data is keyless perp market data behind a **provider chain**: Binance USDⓈ-M
Futures first (richer history, USD OI series), then Bybit v5, then Kraken
Futures, then Hyperliquid — all free, no key. Binance 451s and Bybit 403s US
datacenter IPs (GitHub Actions runners), so hosted cycles fall through to
Kraken/Hyperliquid automatically; the first provider that answers is
remembered for the rest of the process, and `DERIVS_PROVIDER`
(`binance`|`bybit`|`kraken`|`hyperliquid`) forces one. Kraken and Hyperliquid
pay funding **hourly**, so their rates are normalized ×8 to the 8h-equivalent
the bias scale expects. The historical funding endpoint returns a
**dated** series (every 8h), so this layer is backtestable by dated-input
replay like flows; open interest is fetched as dated context. Each
`source="derivs"` post carries its signal in `raw` (read by the bias layer, not
enriched into a signal), and its text uses the exchange symbol (`BTCUSDT`) — not
a bare ticker — so it can never leak into the sentiment/signal layer.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

from .models import Author, Post

FUNDING_URL = "https://fapi.binance.com/fapi/v1/fundingRate"
OI_HIST_URL = "https://fapi.binance.com/futures/data/openInterestHist"
BYBIT_FUNDING_URL = "https://api.bybit.com/v5/market/funding/history"
BYBIT_TICKERS_URL = "https://api.bybit.com/v5/market/tickers"
KRAKEN_FUNDING_URL = "https://futures.kraken.com/derivatives/api/v4/historicalfundingrates"
KRAKEN_TICKERS_URL = "https://futures.kraken.com/derivatives/api/v3/tickers"
HYPERLIQUID_INFO_URL = "https://api.hyperliquid.xyz/info"
_HEADERS = {"Accept": "application/json", "User-Agent": "Catalyst/0.1 (+https://github.com/catalyst)"}

PROVIDERS = ("binance", "bybit", "kraken", "hyperliquid")
_TRADE_URLS = {"binance": "https://www.binance.com/en/futures/{sym}",
               "bybit": "https://www.bybit.com/trade/usdt/{sym}",
               "kraken": "https://futures.kraken.com/trade/futures/{sym}",
               "hyperliquid": "https://app.hyperliquid.xyz/trade/{sym}"}
_active_provider: str | None = None  # sticky: first provider that answered

# Clean ticker → perp symbol. Extend by adding a line.
_SYMBOLS = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT", "XRP": "XRPUSDT",
            "DOGE": "DOGEUSDT", "BNB": "BNBUSDT", "AVAX": "AVAXUSDT", "LINK": "LINKUSDT",
            "ADA": "ADAUSDT", "ARB": "ARBUSDT"}

DEFAULT_FUNDING_SCALE = 0.0005   # ~0.05%/8h funding saturates the bias
DEFAULT_HALFLIFE_HOURS = 24.0    # recent fundings dominate the read


def _symbol(asset: str) -> str:
    return _SYMBOLS.get(asset.upper(), f"{asset.upper()}USDT")


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _get(url: str, params: dict, *, timeout: float = 30.0):
    resp = httpx.get(url, params=params, headers=_HEADERS, timeout=timeout, follow_redirects=True)
    if resp.status_code != 200:
        raise RuntimeError(f"derivs {url} failed: {resp.status_code} {resp.reason_phrase}")
    return resp.json()


def _iso_ms(ms) -> str:
    return datetime.fromtimestamp(int(ms) / 1000.0, tz=timezone.utc).isoformat()


# ---- Fetch ------------------------------------------------------------------

def _provider_chain() -> tuple[str, ...]:
    forced = os.environ.get("DERIVS_PROVIDER", "").lower()
    if forced:
        return (forced,)
    if _active_provider:
        return (_active_provider, *(p for p in PROVIDERS if p != _active_provider))
    return PROVIDERS


def _with_provider(fn):
    """Run `fn(provider)` down the chain; stick to the first provider that answers."""
    global _active_provider
    errors: list[str] = []
    for provider in _provider_chain():
        try:
            result = fn(provider)
        except Exception as e:  # noqa: BLE001 — any failure means try the next provider
            errors.append(f"{provider}: {e}")
            continue
        _active_provider = provider
        return provider, result
    raise RuntimeError("derivs: all providers failed — " + "; ".join(errors))


def _bybit_list(data: dict, what: str) -> list[dict]:
    if data.get("retCode") != 0:
        raise RuntimeError(f"bybit {what}: {data.get('retCode')} {data.get('retMsg')}")
    return (data.get("result") or {}).get("list") or []


def _post(url: str, payload: dict, *, timeout: float = 30.0):
    resp = httpx.post(url, json=payload, headers=_HEADERS, timeout=timeout, follow_redirects=True)
    if resp.status_code != 200:
        raise RuntimeError(f"derivs {url} failed: {resp.status_code} {resp.reason_phrase}")
    return resp.json()


def _native_symbol(provider: str, asset: str) -> str:
    """Provider's own market symbol (binance/bybit share the USDT-perp names)."""
    a = asset.upper()
    if provider == "kraken":
        return f"PF_{'XBT' if a == 'BTC' else a}USD"
    if provider == "hyperliquid":
        return a
    return _symbol(a)


def _iso_to_ms(s: str) -> int:
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _funding_rows(provider: str, asset: str, limit: int) -> list[tuple[int, float]]:
    """(timestamp_ms, funding_rate per 8h) rows, order not guaranteed.

    Kraken and Hyperliquid pay hourly, so their rates are ×8 to the
    8h-equivalent that DEFAULT_FUNDING_SCALE (and the [DERIVS] text) assumes.
    """
    sym = _native_symbol(provider, asset)
    if provider == "binance":
        rows = _get(FUNDING_URL, {"symbol": sym, "limit": limit})
        return [(int(r["fundingTime"]), float(r.get("fundingRate") or 0.0))
                for r in rows if r.get("fundingTime") is not None]
    if provider == "bybit":
        data = _get(BYBIT_FUNDING_URL, {"category": "linear", "symbol": sym,
                                        "limit": min(limit, 200)})
        return [(int(r["fundingRateTimestamp"]), float(r.get("fundingRate") or 0.0))
                for r in _bybit_list(data, "funding") if r.get("fundingRateTimestamp")]
    if provider == "kraken":
        data = _get(KRAKEN_FUNDING_URL, {"symbol": sym})
        rates = data.get("rates") or []
        return [(_iso_to_ms(r["timestamp"]), float(r["relativeFundingRate"]) * 8.0)
                for r in rates[-limit:]
                if r.get("timestamp") and r.get("relativeFundingRate") is not None]
    if provider == "hyperliquid":
        rows = _post(HYPERLIQUID_INFO_URL, {"type": "fundingHistory", "coin": sym,
                                            "startTime": _now_ms() - limit * 3_600_000})
        return [(int(r["time"]), float(r.get("fundingRate") or 0.0) * 8.0)
                for r in rows if r.get("time") is not None]
    raise RuntimeError(f"unknown provider {provider!r}")


def _oi_rows(provider: str, asset: str, period: str, limit: int) -> list[tuple[int, float]]:
    """(timestamp_ms, oi_usd) rows. Only Binance has a USD-denominated OI
    *history*; the others expose current OI (coin-denominated where noted), so
    there we take one current USD point — the bias only reads the latest, and
    the store accretes a dated series over cycles."""
    sym = _native_symbol(provider, asset)
    if provider == "binance":
        rows = _get(OI_HIST_URL, {"symbol": sym, "period": period, "limit": limit})
        return [(int(r["timestamp"]), float(r.get("sumOpenInterestValue") or 0.0))
                for r in rows if r.get("timestamp") is not None]
    if provider == "bybit":
        data = _get(BYBIT_TICKERS_URL, {"category": "linear", "symbol": sym})
        items = _bybit_list(data, "tickers")
        if not items:
            return []
        ts = int(data.get("time") or _now_ms())
        return [(ts, float(items[0].get("openInterestValue") or 0.0))]
    if provider == "kraken":
        data = _get(KRAKEN_TICKERS_URL, {})
        tick = next((t for t in data.get("tickers") or [] if t.get("symbol") == sym), None)
        if tick is None:
            raise RuntimeError(f"kraken tickers: no symbol {sym}")
        # PF_ contracts are 1 coin, so OI(coin) × mark = USD.
        oi_usd = float(tick.get("openInterest") or 0.0) * float(tick.get("markPrice") or 0.0)
        ts = _iso_to_ms(data["serverTime"]) if data.get("serverTime") else _now_ms()
        return [(ts, oi_usd)]
    if provider == "hyperliquid":
        meta, ctxs = _post(HYPERLIQUID_INFO_URL, {"type": "metaAndAssetCtxs"})
        names = [u.get("name") for u in meta.get("universe") or []]
        if sym not in names:
            raise RuntimeError(f"hyperliquid: no asset {sym}")
        ctx = ctxs[names.index(sym)]
        oi_usd = float(ctx.get("openInterest") or 0.0) * float(ctx.get("markPx") or 0.0)
        return [(_now_ms(), oi_usd)]
    raise RuntimeError(f"unknown provider {provider!r}")


def fetch_funding(assets: list[str] | None = None, *, limit: int = 30,
                  now: datetime | None = None) -> list[Post]:
    """Historical perp funding rate (dated, ~8h cadence) per asset as `derivs` posts."""
    assets = assets or ["BTC", "ETH"]

    def pull(provider: str) -> dict[str, list[tuple[int, float]]]:
        return {asset: _funding_rows(provider, asset, limit) for asset in assets}

    provider, per_asset = _with_provider(pull)
    out: list[Post] = []
    for asset, rows in per_asset.items():
        # Text/raw keep the canonical exchange-style label (BTCUSDT) whatever
        # the provider, so it can never read as a bare ticker downstream.
        sym = _symbol(asset)
        for ts, rate in rows:
            iso = _iso_ms(ts)
            out.append(Post(
                source="derivs",
                uri=f"derivs:funding:{asset.upper()}:{ts}",
                url=_TRADE_URLS[provider].format(sym=_native_symbol(provider, asset)),
                text=f"[DERIVS] {sym} perp funding {rate * 100:+.4f}% (8h)",
                created_at=iso, indexed_at=iso,
                author=Author(handle="derivs", display_name=f"{sym} funding"),
                raw={"kind": "funding", "asset": asset.upper(), "symbol": sym,
                     "funding_rate": rate, "provider": provider},
            ))
    return out


def fetch_open_interest(assets: list[str] | None = None, *, period: str = "1d",
                        limit: int = 30, now: datetime | None = None) -> list[Post]:
    """Historical open interest (dated) per asset as `derivs` posts (context)."""
    assets = assets or ["BTC", "ETH"]

    def pull(provider: str) -> dict[str, list[tuple[int, float]]]:
        return {asset: _oi_rows(provider, asset, period, limit) for asset in assets}

    provider, per_asset = _with_provider(pull)
    out: list[Post] = []
    for asset, rows in per_asset.items():
        sym = _symbol(asset)
        for ts, oi_usd in rows:
            iso = _iso_ms(ts)
            out.append(Post(
                source="derivs",
                uri=f"derivs:oi:{asset.upper()}:{ts}",
                url=_TRADE_URLS[provider].format(sym=_native_symbol(provider, asset)),
                text=f"[DERIVS] {sym} open interest ${oi_usd / 1e6:,.0f}M",
                created_at=iso, indexed_at=iso,
                author=Author(handle="derivs", display_name=f"{sym} OI"),
                raw={"kind": "oi", "asset": asset.upper(), "symbol": sym,
                     "oi_usd": oi_usd, "provider": provider},
            ))
    return out


def fetch_derivs(assets: list[str] | None = None, *, funding_limit: int = 30,
                 open_interest: bool = True, oi_limit: int = 30) -> list[Post]:
    """Funding (the signal) + optional open interest (context) as `derivs` posts."""
    posts = fetch_funding(assets, limit=funding_limit)
    if open_interest:
        try:
            posts += fetch_open_interest(assets, limit=oi_limit)
        except Exception:  # noqa: BLE001 — OI is context; funding is the signal
            pass
    return posts


# ---- Bias -------------------------------------------------------------------

@dataclass
class DerivsBias:
    asset: str
    bias: float                 # -1 crowded-long (bearish) .. +1 crowded-short (bullish)
    label: str                  # crowded-long | neutral | crowded-short
    evidence: float             # magnitude behind it
    drivers: list[str] = field(default_factory=list)


def _raw(row) -> dict:
    import json
    if isinstance(row, Post):
        return row.raw or {}
    raw = row.get("raw")
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return raw or {}


def compute_derivs_bias(
    rows, *, now: datetime | None = None, halflife_hours: float = DEFAULT_HALFLIFE_HOURS,
    scale: float = DEFAULT_FUNDING_SCALE,
) -> dict[str, DerivsBias]:
    """Per-asset positioning bias from decay-weighted perp funding.

    bias = −tanh(weighted-mean funding / scale): positive funding (crowded longs)
    → negative bias (fade); negative funding (crowded shorts) → positive bias.
    """
    now = now or datetime.now(timezone.utc)
    acc: dict[str, dict] = {}
    latest_oi: dict[str, tuple[str, float]] = {}
    for row in rows:
        r = _raw(row)
        kind = r.get("kind")
        asset = r.get("asset")
        if not asset:
            continue
        ts = row.indexed_at if isinstance(row, Post) else row.get("indexed_at") or ""
        if kind == "oi":
            if asset not in latest_oi or ts > latest_oi[asset][0]:
                latest_oi[asset] = (ts, float(r.get("oi_usd") or 0.0))
            continue
        if kind != "funding":
            continue
        dt = _parse_dt(ts)
        if dt is None:
            continue
        age_h = max(0.0, (now - dt).total_seconds() / 3600.0)
        w = 0.5 ** (age_h / halflife_hours)
        a = acc.setdefault(asset, {"wsum": 0.0, "wrate": 0.0})
        a["wsum"] += w
        a["wrate"] += w * float(r.get("funding_rate") or 0.0)

    out: dict[str, DerivsBias] = {}
    for asset, a in acc.items():
        if a["wsum"] <= 0:
            continue
        mean = a["wrate"] / a["wsum"]
        bias = -math.tanh(mean / scale) if scale else 0.0
        label = ("crowded-short" if bias > 0.15 else "crowded-long" if bias < -0.15 else "neutral")
        drivers = [f"{asset} funding {mean * 100:+.4f}%/8h"]
        if asset in latest_oi:
            drivers.append(f"OI ${latest_oi[asset][1] / 1e6:,.0f}M")
        out[asset] = DerivsBias(asset, round(bias, 3), label, round(abs(mean) / scale, 3), drivers)
    return out
