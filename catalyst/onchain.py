"""On-chain tier — token unlocks + ETH staking queue as **supply-side** signals.

This is the supply mirror of the demand-side ETF flows layer. Two sources, two
different time models, merged into one per-asset `SupplyBias` the planner applies
as a confidence modifier (parallel to flows/macro):

  - **Unlocks** (DefiLlama emissions, free CDN): scheduled vesting cliffs hitting
    the market = sell pressure. Time model is **forward/anticipatory** — pressure
    ramps up as the unlock date approaches. Bearish. Unlocks ALSO ride the normal
    enrich→signal pipeline as a `source="onchain"` catalyst (text phrased so the
    lexicon tags `catalyst="unlock"` + a bearish sentiment + the ticker), so a big
    imminent unlock can surface a `sell`/`watch` candidate on its own — not just
    tilt an existing one.
  - **Staking queue** (Ethereum beacon node, direct, free): the validator entry
    queue is ETH being locked up = supply leaving the float (bullish); the exit
    queue is potential supply release (bearish, but small/noisy → down-weighted).
    ETH-only. Time model is a **slow standing level** (no decay). Read straight
    from consensus state, so there's no third-party trust dependency.

Direction convention for the merged bias: -1 = supply pressure (unlock dump),
+1 = supply sink (staking lockup). Pluggable like every other source: another
unlock token is one registry line; the staking source can swap to the
validatorqueue.com fallback behind the same `Post` shape.
"""

from __future__ import annotations

import json
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .flows import HttpCache, _HEADERS
from .models import Author, Post

LLAMA_DATASETS = "https://defillama-datasets.llama.fi"
LLAMA_COINS = "https://coins.llama.fi"
PUBLICNODE_BEACON = "https://ethereum-beacon-api.publicnode.com"
VALIDATORQUEUE = "https://www.validatorqueue.com/"

# Categories whose unlocks are real sell pressure (insiders dumping); the rest
# (community farming, airdrops, already-circulating) are noise for our purposes.
SELL_PRESSURE_CATEGORIES = {"insiders", "privatesale", "team", "advisors"}

DEFAULT_HORIZON_DAYS = 30.0
DEFAULT_UNLOCK_SCALE = 0.05      # ~5% of float of imminent unlock saturates the bearish bias
DEFAULT_STAKE_SCALE = 2_000_000.0  # net ETH entry that saturates the bullish bias
DEFAULT_EXIT_WEIGHT = 0.3        # exit queue is noisy (lots of restaking) → down-weight


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _get_json(url: str, cache: HttpCache | None, *, timeout: float = 60.0):
    """Conditional GET → parsed JSON, with last-good fallback (like flows)."""
    cached = cache.get(url) if cache else None
    headers = dict(_HEADERS)
    headers["Accept"] = "application/json"
    if cached:
        if cached.get("etag"):
            headers["If-None-Match"] = cached["etag"]
        if cached.get("last_modified"):
            headers["If-Modified-Since"] = cached["last_modified"]
    try:
        resp = httpx.get(url, headers=headers, timeout=timeout, follow_redirects=True)
    except httpx.HTTPError as err:
        if cached:
            print(f"onchain {url} network error, using cached: {err}", file=sys.stderr)
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
        print(f"onchain {url} got {resp.status_code}, using cached body", file=sys.stderr)
        return json.loads(cached["body"])
    raise RuntimeError(f"onchain fetch {url} failed: {resp.status_code} {resp.reason_phrase}")


# ---- Unlocks (DefiLlama emissions) ------------------------------------------

def _load_registry(registry: list[dict] | str | None) -> list[dict]:
    """Protocol registry rows with a `defillama` slug — the selective fetch set."""
    if isinstance(registry, (str, Path)):
        registry = json.loads(Path(registry).read_text(encoding="utf-8")).get("protocols", [])
    rows = registry if isinstance(registry, list) else []
    return [r for r in rows if r.get("defillama")]


def _upcoming_unlocks(data: dict, *, now: datetime, horizon_days: float) -> list[dict]:
    """Future sell-pressure CLIFF events within the horizon, with token counts."""
    meta = data.get("metadata") or {}
    sm = data.get("supplyMetrics") or {}
    supply = sm.get("adjustedSupply") or sm.get("maxSupply") or 0
    out: list[dict] = []
    for ev in meta.get("events") or []:
        if ev.get("unlockType") != "cliff":
            continue
        if (ev.get("category") or "").lower() not in SELL_PRESSURE_CATEGORIES:
            continue
        ts = ev.get("timestamp")
        if not ts:
            continue
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        days_until = (dt - now).total_seconds() / 86400.0
        if days_until < 0 or days_until > horizon_days:
            continue
        tokens = sum(ev.get("noOfTokens") or [0])
        if tokens <= 0:
            continue
        out.append({
            "event_ts": dt.isoformat(),
            "days_until": days_until,
            "tokens": tokens,
            "pct_float": (tokens / supply) if supply else 0.0,
            "category": (ev.get("category") or "").lower(),
            "gecko_id": data.get("gecko_id"),
        })
    return out


def _prices(gecko_ids: set[str], cache: HttpCache | None) -> dict[str, dict]:
    ids = [g for g in gecko_ids if g]
    if not ids:
        return {}
    keys = ",".join(f"coingecko:{g}" for g in ids)
    try:
        data = _get_json(f"{LLAMA_COINS}/prices/current/{keys}", cache, timeout=30.0)
    except Exception as err:  # noqa: BLE001 — price is best-effort enrichment of the post text
        print(f"onchain price fetch skipped: {err}", file=sys.stderr)
        return {}
    return {k.split(":", 1)[1]: v for k, v in (data.get("coins") or {}).items()}


def _unlock_post(sym: str, ev: dict, price: dict | None, now: datetime) -> Post:
    pct = ev["pct_float"]
    usd = ev["tokens"] * (price.get("price", 0.0) if price else 0.0)
    days = max(0, int(ev["days_until"]))
    intens = "massive " if pct >= 0.01 else ""
    # Phrased so the lexicon tags catalyst="unlock", a bearish sentiment
    # ("selloff"), and extracts the $TICKER — i.e. a standalone sell catalyst.
    text = (
        f"[UNLOCK] ${sym}: {ev['tokens']:,.0f} tokens ({pct * 100:.1f}% of float) "
        f"unlock in {days}d — {ev['category']} cliff, ${usd / 1e6:.1f}M {intens}selloff"
    )
    return Post(
        source="onchain",
        uri=f"onchain:unlock:{sym}:{ev['event_ts'][:10]}",
        url="https://defillama.com/unlocks",
        text=text,
        created_at=ev["event_ts"],
        indexed_at=now.isoformat(),  # observed now (fresh news about a future event)
        author=Author(handle="unlocks", display_name=f"{sym} unlock"),
        raw={"kind": "unlock", "asset": sym, "tokens": ev["tokens"], "pct_float": pct,
             "usd": usd, "event_ts": ev["event_ts"], "category": ev["category"]},
    )


def fetch_unlocks(
    registry: list[dict] | str | None = "protocols.json",
    *,
    horizon_days: float = DEFAULT_HORIZON_DAYS,
    now: datetime | None = None,
    cache: HttpCache | None | str = ".cache/onchain_http.json",
) -> list[Post]:
    """Upcoming sell-pressure cliff unlocks (per registry token) as `onchain` posts."""
    now = now or datetime.now(timezone.utc)
    if isinstance(cache, (str, Path)):
        cache = HttpCache(cache)
    rows = _load_registry(registry)

    found: list[tuple[str, dict]] = []  # (symbol, event)
    for r in rows:
        slug, sym = r["defillama"], (r.get("symbol") or r["defillama"]).upper()
        try:
            data = _get_json(f"{LLAMA_DATASETS}/emissions/{slug}", cache)
        except Exception as err:  # noqa: BLE001 — one token shouldn't fail the batch
            print(f"onchain unlock {slug} skipped: {err}", file=sys.stderr)
            continue
        for ev in _upcoming_unlocks(data, now=now, horizon_days=horizon_days):
            found.append((sym, ev))

    prices = _prices({ev["gecko_id"] for _, ev in found}, cache)
    return [_unlock_post(sym, ev, prices.get(ev["gecko_id"]), now) for sym, ev in found]


# ---- Staking queue (ETH beacon node, direct) --------------------------------

def fetch_stake_queue(
    *, node_url: str = PUBLICNODE_BEACON, now: datetime | None = None, timeout: float = 90.0
) -> list[Post]:
    """ETH validator entry-queue (supply being locked) as a `staking` post.

    Reads consensus state directly (`pending_deposits`, amounts in Gwei). Exit
    queue is left at 0 in v1 (it's small and noisy; the validatorqueue.com
    fallback can supply it later behind this same shape).
    """
    now = now or datetime.now(timezone.utc)
    data = _get_json(f"{node_url}/eth/v1/beacon/states/head/pending_deposits", None, timeout=timeout)
    entry_eth = sum(int(d.get("amount", 0)) for d in (data.get("data") or [])) / 1e9
    exit_eth = 0.0
    text = f"[STAKE QUEUE] validator entry {entry_eth:,.0f} vs exit {exit_eth:,.0f} (deposits)"
    return [Post(
        source="staking",
        uri=f"staking:eth:{now.date().isoformat()}",
        url="https://www.validatorqueue.com/",
        text=text,
        created_at=now.isoformat(),
        indexed_at=now.isoformat(),
        author=Author(handle="staking", display_name="ETH staking queue"),
        raw={"kind": "stake", "asset": "ETH", "entry_eth": entry_eth, "exit_eth": exit_eth},
    )]


# ---- Bias: forward unlocks + standing staking → merged SupplyBias -----------

@dataclass
class SupplyBias:
    asset: str
    bias: float                        # -1 supply pressure (unlock) .. +1 supply sink (staking)
    label: str                         # supply-pressure | neutral | supply-sink
    evidence: float                    # magnitude behind it (sum |components|)
    drivers: list[str] = field(default_factory=list)


def _raw(row) -> dict:
    if isinstance(row, Post):
        return row.raw or {}
    raw = row.get("raw")
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return raw or {}


def compute_unlock_bias(
    rows, *, now: datetime | None = None, horizon_days: float = DEFAULT_HORIZON_DAYS,
    scale: float = DEFAULT_UNLOCK_SCALE,
) -> dict[str, tuple[float, list[str]]]:
    """Forward-anticipatory bearish bias per asset (ramps up toward the date)."""
    now = now or datetime.now(timezone.utc)
    acc: dict[str, dict] = {}
    for row in rows:
        r = _raw(row)
        if r.get("kind") != "unlock":
            continue
        dt = _parse_dt(r.get("event_ts"))
        if dt is None:
            continue
        days_until = (dt - now).total_seconds() / 86400.0
        if days_until < 0 or days_until > horizon_days:
            continue
        proximity = max(0.0, 1.0 - days_until / horizon_days)  # 0 far out → 1 at the date
        weight = proximity * float(r.get("pct_float") or 0.0)
        if weight <= 0:
            continue
        a = acc.setdefault(r["asset"], {"sum": 0.0, "drivers": []})
        a["sum"] += weight
        a["drivers"].append((weight, f"{r['asset']} {r.get('pct_float', 0)*100:.1f}% in {int(days_until)}d ({r.get('category')})"))
    out: dict[str, tuple[float, list[str]]] = {}
    for asset, a in acc.items():
        bias = -math.tanh(a["sum"] / scale) if scale else 0.0
        drivers = [d for _, d in sorted(a["drivers"], key=lambda x: x[0], reverse=True)[:2]]
        out[asset] = (bias, drivers)
    return out


def compute_stake_bias(
    rows, *, scale: float = DEFAULT_STAKE_SCALE, exit_weight: float = DEFAULT_EXIT_WEIGHT,
) -> dict[str, tuple[float, list[str]]]:
    """Standing bullish bias per asset from the staking entry queue (latest row)."""
    latest: dict[str, tuple[str, dict]] = {}
    for row in rows:
        r = _raw(row)
        if r.get("kind") != "stake":
            continue
        asset = r.get("asset") or "ETH"
        ts = row.indexed_at if isinstance(row, Post) else row.get("indexed_at") or ""
        if asset not in latest or ts > latest[asset][0]:
            latest[asset] = (ts, r)
    out: dict[str, tuple[float, list[str]]] = {}
    for asset, (_, r) in latest.items():
        entry = float(r.get("entry_eth") or 0.0)
        exit_ = float(r.get("exit_eth") or 0.0)
        net = entry - exit_weight * exit_
        bias = math.tanh(net / scale) if scale else 0.0
        out[asset] = (bias, [f"{asset} staking entry {entry:,.0f} vs exit {exit_:,.0f} ETH"])
    return out


def compute_supply_bias(
    rows, *, now: datetime | None = None, horizon_days: float = DEFAULT_HORIZON_DAYS,
    unlock_scale: float = DEFAULT_UNLOCK_SCALE, stake_scale: float = DEFAULT_STAKE_SCALE,
    exit_weight: float = DEFAULT_EXIT_WEIGHT,
) -> dict[str, SupplyBias]:
    """Merge forward unlocks (bearish) + standing staking (bullish) per asset."""
    now = now or datetime.now(timezone.utc)
    rows = list(rows)
    unlocks = compute_unlock_bias(rows, now=now, horizon_days=horizon_days, scale=unlock_scale)
    stakes = compute_stake_bias(rows, scale=stake_scale, exit_weight=exit_weight)

    out: dict[str, SupplyBias] = {}
    for asset in set(unlocks) | set(stakes):
        ub, ud = unlocks.get(asset, (0.0, []))
        sb, sd = stakes.get(asset, (0.0, []))
        bias = max(-1.0, min(1.0, ub + sb))
        label = ("supply-sink" if bias > 0.15 else "supply-pressure" if bias < -0.15 else "neutral")
        out[asset] = SupplyBias(asset, round(bias, 3), label, round(abs(ub) + abs(sb), 3), ud + sd)
    return out
