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

Data is keyless Binance USDⓈ-M Futures (free, no key): the historical funding
endpoint returns a **dated** series (every 8h), so this layer is backtestable by
dated-input replay like flows; open interest is fetched as dated context. Each
`source="derivs"` post carries its signal in `raw` (read by the bias layer, not
enriched into a signal), and its text uses the exchange symbol (`BTCUSDT`) — not
a bare ticker — so it can never leak into the sentiment/signal layer.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

from .models import Author, Post

FUNDING_URL = "https://fapi.binance.com/fapi/v1/fundingRate"
OI_HIST_URL = "https://fapi.binance.com/futures/data/openInterestHist"
_HEADERS = {"Accept": "application/json", "User-Agent": "Catalyst/0.1 (+https://github.com/catalyst)"}

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

def fetch_funding(assets: list[str] | None = None, *, limit: int = 30,
                  now: datetime | None = None) -> list[Post]:
    """Historical perp funding rate (dated, ~8h cadence) per asset as `derivs` posts."""
    assets = assets or ["BTC", "ETH"]
    out: list[Post] = []
    for asset in assets:
        sym = _symbol(asset)
        rows = _get(FUNDING_URL, {"symbol": sym, "limit": limit})
        for r in rows:
            rate = float(r.get("fundingRate") or 0.0)
            ts = r.get("fundingTime")
            if ts is None:
                continue
            iso = _iso_ms(ts)
            out.append(Post(
                source="derivs",
                uri=f"derivs:funding:{asset.upper()}:{ts}",
                url=f"https://www.binance.com/en/futures/{sym}",
                text=f"[DERIVS] {sym} perp funding {rate * 100:+.4f}% (8h)",
                created_at=iso, indexed_at=iso,
                author=Author(handle="derivs", display_name=f"{sym} funding"),
                raw={"kind": "funding", "asset": asset.upper(), "symbol": sym,
                     "funding_rate": rate},
            ))
    return out


def fetch_open_interest(assets: list[str] | None = None, *, period: str = "1d",
                        limit: int = 30, now: datetime | None = None) -> list[Post]:
    """Historical open interest (dated) per asset as `derivs` posts (context)."""
    assets = assets or ["BTC", "ETH"]
    out: list[Post] = []
    for asset in assets:
        sym = _symbol(asset)
        rows = _get(OI_HIST_URL, {"symbol": sym, "period": period, "limit": limit})
        for r in rows:
            oi_usd = float(r.get("sumOpenInterestValue") or 0.0)
            ts = r.get("timestamp")
            if ts is None:
                continue
            iso = _iso_ms(ts)
            out.append(Post(
                source="derivs",
                uri=f"derivs:oi:{asset.upper()}:{ts}",
                url=f"https://www.binance.com/en/futures/{sym}",
                text=f"[DERIVS] {sym} open interest ${oi_usd / 1e6:,.0f}M",
                created_at=iso, indexed_at=iso,
                author=Author(handle="derivs", display_name=f"{sym} OI"),
                raw={"kind": "oi", "asset": asset.upper(), "symbol": sym, "oi_usd": oi_usd},
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
