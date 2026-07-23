"""Hyperliquid platform events — new perp listings + funding regime flips.

`derivs.py` already *reads* Hyperliquid as a fallback funding/OI provider;
this module treats the platform itself as a catalyst source:

  - **New perp listings.** A Hyperliquid listing is a listing catalyst (same
    logic as DefiLlama listings). The live universe is compared against a
    committed baseline file (`hyperliquid_universe.json`) — hosted poll
    runners are ephemeral, so cross-cycle state lives in the repo like
    `protocols.json`; between baseline refreshes an addition re-emits each
    cycle with a stable URI and the posts-table dedup absorbs it. A missing
    baseline emits nothing (never flood the feed with 200+ "new" listings).
    Refresh with `write_baseline()` and commit the file.

  - **Funding regime flips.** The 24h mean funding crossing zero (crowded
    longs ↔ crowded shorts) is stateless: one 48h fundingHistory call per
    asset, comparing the halves. The URI is day-keyed so a flip emits at most
    once per day. Unlike the `derivs` positioning posts, the text names the
    bare asset *with the contrarian read spelled out* — these are meant to be
    LLM-enriched into the signal layer, and the phrasing carries the correct
    direction (negative funding = crowded shorts = squeeze-up risk).

Learning path: `source="hyperliquid"` is in `store.NEWS_SOURCES`, so both
event kinds are LLM-enriched, feed signals, and land in the score→outcome
records (`score_snapshots` / `score_outcomes`) for the learned models later.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .models import Author, Post

INFO_URL = "https://api.hyperliquid.xyz/info"
_HEADERS = {"Accept": "application/json", "User-Agent": "Catalyst/0.1 (+https://github.com/catalyst)"}

DEFAULT_BASELINE_FILE = "hyperliquid_universe.json"
DEFAULT_FLIP_ASSETS = ("BTC", "ETH", "SOL")
DEFAULT_MIN_MEAN = 0.0001        # 8h-equivalent mean funding a flip must clear


def _post(payload: dict, *, timeout: float = 30.0):
    resp = httpx.post(INFO_URL, json=payload, headers=_HEADERS, timeout=timeout,
                      follow_redirects=True)
    if resp.status_code != 200:
        raise RuntimeError(f"hyperliquid info failed: {resp.status_code} {resp.reason_phrase}")
    return resp.json()


# ---- Listings ---------------------------------------------------------------

def load_baseline(path: str) -> set[str] | None:
    """Known-universe baseline, or None when the file is absent/unreadable."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return {str(c).upper() for c in data.get("universe") or []}
    except Exception:  # noqa: BLE001 — no baseline just means seed mode
        return None


def write_baseline(path: str = DEFAULT_BASELINE_FILE) -> int:
    """Snapshot the current universe to the baseline file. Returns coin count."""
    meta, _ = _post({"type": "metaAndAssetCtxs"})
    universe = sorted(u["name"].upper() for u in meta.get("universe") or [] if u.get("name"))
    Path(path).write_text(json.dumps({
        "as_of": datetime.now(timezone.utc).isoformat(), "universe": universe,
    }, indent=1) + "\n", encoding="utf-8")
    return len(universe)


def listing_posts(universe: list[dict], ctxs: list[dict], baseline: set[str],
                  *, now: datetime | None = None) -> list[Post]:
    """[LISTING] posts for universe coins absent from the baseline (pure)."""
    now = now or datetime.now(timezone.utc)
    iso = now.isoformat()
    out: list[Post] = []
    for i, u in enumerate(universe):
        coin = (u.get("name") or "").upper()
        if not coin or coin in baseline:
            continue
        ctx = ctxs[i] if i < len(ctxs) else {}
        vol = float(ctx.get("dayNtlVlm") or 0.0)
        lev = u.get("maxLeverage")
        out.append(Post(
            source="hyperliquid",
            uri=f"hyperliquid:listing:{coin}",
            url=f"https://app.hyperliquid.xyz/trade/{coin}",
            text=(f"[LISTING] Hyperliquid lists {coin} perpetual"
                  + (f" (max {lev}x leverage)" if lev else "")
                  + (f" — ${vol / 1e6:,.1f}M day volume" if vol else "")),
            created_at=iso, indexed_at=iso,
            author=Author(handle="hyperliquid", display_name="Hyperliquid"),
            raw={"kind": "listing", "asset": coin, "max_leverage": lev, "day_volume_usd": vol},
        ))
    return out


def fetch_listings(*, baseline_file: str = DEFAULT_BASELINE_FILE) -> list[Post]:
    """New-listing posts vs the committed baseline; seed mode emits nothing."""
    baseline = load_baseline(baseline_file)
    if baseline is None:
        print(f"hyperliquid listings: no baseline at {baseline_file} — "
              "run hl_events.write_baseline() and commit it", file=sys.stderr)
        return []
    meta, ctxs = _post({"type": "metaAndAssetCtxs"})
    return listing_posts(meta.get("universe") or [], ctxs or [], baseline)


# ---- Funding regime flips ---------------------------------------------------

def flip_post(asset: str, rows: list[dict], *, min_mean: float = DEFAULT_MIN_MEAN,
              now: datetime | None = None) -> Post | None:
    """Flip post when the 24h mean funding sign differs from the prior 24h (pure).

    `rows` are hourly fundingHistory rows covering ~48h; hourly rates are ×8 to
    the 8h-equivalent the derivs scale uses. Returns None without a flip.
    """
    now = now or datetime.now(timezone.utc)
    split_ms = int(now.timestamp() * 1000) - 24 * 3_600_000
    prev = [float(r.get("fundingRate") or 0.0) * 8.0 for r in rows
            if r.get("time") is not None and int(r["time"]) < split_ms]
    recent = [float(r.get("fundingRate") or 0.0) * 8.0 for r in rows
              if r.get("time") is not None and int(r["time"]) >= split_ms]
    if not prev or not recent:
        return None
    m_prev, m_recent = sum(prev) / len(prev), sum(recent) / len(recent)
    if m_prev * m_recent >= 0 or abs(m_recent) < min_mean:
        return None
    iso = now.isoformat()
    asset = asset.upper()
    if m_recent < 0:
        read = ("flipped negative — crowded shorts building; contrarian bullish,"
                " squeeze-up risk")
    else:
        read = ("flipped positive — crowded longs building; contrarian bearish,"
                " over-leveraged upside")
    return Post(
        source="hyperliquid",
        uri=f"hyperliquid:flip:{asset}:{now.date().isoformat()}",
        url=f"https://app.hyperliquid.xyz/trade/{asset}",
        text=(f"[DERIVS-EVENT] {asset} Hyperliquid perp funding {read}"
              f" (24h mean {m_recent * 100:+.4f}%/8h vs prior {m_prev * 100:+.4f}%/8h)"),
        created_at=iso, indexed_at=iso,
        author=Author(handle="hyperliquid", display_name=f"{asset} funding regime"),
        raw={"kind": "funding_flip", "asset": asset,
             "mean_recent": m_recent, "mean_prev": m_prev},
    )


def fetch_funding_flips(assets=None, *, min_mean: float = DEFAULT_MIN_MEAN,
                        now: datetime | None = None) -> list[Post]:
    """Funding regime-flip posts for the watched assets; per-asset fail-soft."""
    now = now or datetime.now(timezone.utc)
    start_ms = int(now.timestamp() * 1000) - 48 * 3_600_000
    out: list[Post] = []
    for asset in assets or DEFAULT_FLIP_ASSETS:
        try:
            rows = _post({"type": "fundingHistory", "coin": asset.upper(),
                          "startTime": start_ms})
        except Exception as err:  # noqa: BLE001 — one asset shouldn't sink the rest
            print(f"hyperliquid funding {asset} skipped: {err}", file=sys.stderr)
            continue
        post = flip_post(asset, rows, min_mean=min_mean, now=now)
        if post:
            out.append(post)
    return out


def fetch_hl_events(*, listings: bool = True, baseline_file: str = DEFAULT_BASELINE_FILE,
                    flip_assets=None, min_mean: float = DEFAULT_MIN_MEAN) -> list[Post]:
    """Listings + funding flips, each fail-soft."""
    out: list[Post] = []
    if listings:
        try:
            out += fetch_listings(baseline_file=baseline_file)
        except Exception as err:  # noqa: BLE001
            print(f"hyperliquid listings skipped: {err}", file=sys.stderr)
    try:
        out += fetch_funding_flips(flip_assets, min_mean=min_mean)
    except Exception as err:  # noqa: BLE001
        print(f"hyperliquid flips skipped: {err}", file=sys.stderr)
    return out
