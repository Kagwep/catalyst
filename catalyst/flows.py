"""Flows layer — ETF in/out as a continuous **per-asset** directional bias.

Unlike the macro layer (one market-wide risk regime), flows is per-asset: each
asset gets its own bias from how much real money is moving in or out. v1 source
is spot-ETF net flows (BTC, ETH) scraped from Farside Investors — free, no key.

Direction convention: net **inflow** (creations / accumulation) = bullish
(positive); net **outflow** (redemptions / distribution) = bearish (negative).
This bias plugs into the planner as a confidence modifier parallel to the macro
regime: a trade aligned with the flow is boosted, one against it is damped — so
"crowd sentiment bullish but money flowing out" naturally fades itself.

Scraping mitigations (Farside is a public HTML page, not a sanctioned API):
  - browser-like headers (the default httpx UA is the #1 bot tell)
  - conditional requests (If-None-Match / If-Modified-Since → cheap 304s)
  - a small on-disk cache that doubles as a last-good-value fallback
  - fail-soft: a block/error reuses cached data or skips, never crashes a batch
  - polite, low-frequency access (flows are daily; one GET returns ~2 weeks)

Pluggability: `FLOW_FEEDS` maps asset → page, so another Farside-shaped asset is
one line. A different *kind* of source (e.g. paid exchange netflow) is one new
fetcher that returns the same `Post` shape — exactly how FRED sits beside the
central-bank RSS in `macro.py`.
"""

from __future__ import annotations

import json
import math
import random
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

import httpx

from .models import Author, Post

# asset -> Farside page. Add a Farside-shaped asset by adding a line here.
FLOW_FEEDS = {
    "BTC": "https://farside.co.uk/btc/",
    "ETH": "https://farside.co.uk/eth/",
}

# Per-asset tanh normalizer for the continuous bias (USD). Bigger = less
# sensitive; BTC flows dwarf ETH's, so it scales higher. Tunable via weights.json.
DEFAULT_FLOW_SCALE = {"BTC": 1.0e9, "ETH": 2.5e8}
_FALLBACK_SCALE = 5.0e8  # used for assets without an explicit scale

# Look like a normal browser, not python-httpx.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_DATE_RE = re.compile(r"^\d{1,2} [A-Za-z]{3} \d{4}$")
_DEFAULT_CACHE = Path(".cache/flows_http.json")


# ---- HTTP cache + conditional fetch (mitigations) ---------------------------

class HttpCache:
    """Tiny JSON-file cache of {url: {etag, last_modified, body, fetched_at}}.

    Backs both conditional requests (304s) and last-good-value fallback. Pass
    `cache=None` to fetch helpers to disable persistence entirely (e.g. tests).
    """

    def __init__(self, path: str | Path = _DEFAULT_CACHE):
        self.path = Path(path)
        self._data: dict = {}
        try:
            self._data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._data = {}

    def get(self, url: str) -> dict | None:
        return self._data.get(url)

    def set(self, url: str, *, etag: str | None, last_modified: str | None, body: str) -> None:
        self._data[url] = {
            "etag": etag,
            "last_modified": last_modified,
            "body": body,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._data), encoding="utf-8")
        except OSError as err:  # cache is best-effort; never fail the fetch over it
            print(f"flows cache write skipped: {err}", file=sys.stderr)


def _fetch_html(url: str, cache: HttpCache | None, *, timeout: float = 20.0) -> str:
    """Conditional GET with last-good fallback.

    304 → cached body; transient error / soft block (403/429/5xx) → cached body
    if we have one, else raise so the caller's `_safe` wrapper can skip it.
    """
    cached = cache.get(url) if cache else None
    headers = dict(_HEADERS)
    if cached:
        if cached.get("etag"):
            headers["If-None-Match"] = cached["etag"]
        if cached.get("last_modified"):
            headers["If-Modified-Since"] = cached["last_modified"]

    try:
        resp = httpx.get(url, headers=headers, timeout=timeout, follow_redirects=True)
    except httpx.HTTPError as err:
        if cached:
            print(f"flows {url} network error, using cached: {err}", file=sys.stderr)
            return cached["body"]
        raise

    if resp.status_code == 304 and cached:
        return cached["body"]
    if resp.status_code == 200 and resp.text:
        if cache:
            cache.set(
                url,
                etag=resp.headers.get("etag"),
                last_modified=resp.headers.get("last-modified"),
                body=resp.text,
            )
        return resp.text
    # Soft block or server error: degrade to last-good rather than hammering.
    if cached:
        print(f"flows {url} got {resp.status_code}, using cached body", file=sys.stderr)
        return cached["body"]
    raise RuntimeError(f"flows fetch {url} failed: {resp.status_code} {resp.reason_phrase}")


# ---- Farside HTML parsing ---------------------------------------------------

class _TableRows(HTMLParser):
    """Collect every table row as a list of cell-text strings."""

    def __init__(self):
        super().__init__()
        self.rows: list[list[str]] = []
        self._cur: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._cur = []
        elif tag in ("td", "th") and self._cur is not None:
            self._cell = []

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None:
            self._cur.append("".join(self._cell).strip())
            self._cell = None
        elif tag == "tr" and self._cur is not None:
            if self._cur:
                self.rows.append(self._cur)
            self._cur = None


def _parse_money(s: str) -> float | None:
    """Farside cell → USD. Parentheses = negative; values are in $millions."""
    s = (s or "").strip()
    if not s or s in ("-", "–", "—"):
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()").replace("$", "").replace(",", "").strip()
    if not s or s == "-":
        return None
    try:
        val = float(s)
    except ValueError:
        return None
    return (-val if neg else val) * 1e6


def _parse_date(s: str) -> str | None:
    """'10 Jun 2026' → ISO at UTC midnight, or None."""
    try:
        d = datetime.strptime(s.strip(), "%d %b %Y").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return d.isoformat()


def parse_flow_table(html: str) -> list[tuple[str, float]]:
    """Extract (iso_date, net_usd) for each day from a Farside flow page.

    Keeps only rows whose first cell is a date; the **last** cell is the daily
    Total across all funds. Summary rows (Total/Average) are dropped by the date
    filter. Returns oldest→newest as found in the page.
    """
    p = _TableRows()
    p.feed(html)
    out: list[tuple[str, float]] = []
    for row in p.rows:
        if not row or not _DATE_RE.match(row[0]):
            continue
        iso = _parse_date(row[0])
        net = _parse_money(row[-1])
        if iso is not None and net is not None:
            out.append((iso, net))
    return out


# ---- Fetch ------------------------------------------------------------------

def _flow_post(asset: str, iso_date: str, net_usd: float, url: str) -> Post:
    direction = "inflow" if net_usd > 0 else "outflow" if net_usd < 0 else "flat"
    millions = net_usd / 1e6
    return Post(
        source="flows",
        uri=f"flows:etf:{asset}:{iso_date[:10]}",
        url=url,
        text=f"[{asset} ETF] net {direction} {millions:+.1f}M",
        created_at=iso_date,
        indexed_at=iso_date,
        author=Author(handle=asset.lower(), display_name=f"{asset} ETF flows"),
        raw={"asset": asset, "net_usd": net_usd, "date": iso_date},
    )


def fetch_etf_flows(
    feeds: dict | None = None,
    *,
    assets: list[str] | None = None,
    max_days: int | None = None,
    cache: HttpCache | None | str = _DEFAULT_CACHE,
    polite_delay: tuple[float, float] | None = (0.4, 1.2),
) -> list[Post]:
    """Spot-ETF net flows as per-asset `flows` posts (newest-first per asset).

    `cache` may be an HttpCache, a path, or None (no caching). One feed failing
    is logged and skipped — it never sinks the batch.
    """
    feeds = feeds or FLOW_FEEDS
    if assets:
        feeds = {a: feeds[a] for a in assets if a in feeds}
    if isinstance(cache, (str, Path)):
        cache = HttpCache(cache)

    out: list[Post] = []
    for i, (asset, url) in enumerate(feeds.items()):
        if polite_delay and i:  # small jitter between feeds; never before the first
            time.sleep(random.uniform(*polite_delay))
        try:
            html = _fetch_html(url, cache)
            days = parse_flow_table(html)
            days.sort(key=lambda t: t[0], reverse=True)  # newest-first
            if max_days:
                days = days[:max_days]
            out.extend(_flow_post(asset, iso, net, url) for iso, net in days)
        except Exception as err:  # noqa: BLE001 — one asset shouldn't fail the batch
            print(f"flows {asset} skipped: {err}", file=sys.stderr)
    return out


# ---- Continuous per-asset bias ----------------------------------------------

@dataclass
class FlowBias:
    asset: str
    bias: float                        # -1 distribution .. +1 accumulation
    label: str                         # accumulation | neutral | distribution
    evidence: float                    # decay-weighted gross flow ($M) behind it
    drivers: list[str] = field(default_factory=list)


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _flow_fields(row) -> tuple[str | None, float | None, str | None]:
    """Pull (asset, net_usd, indexed_at) from a flow Post or a dict row.

    Handles a `Post`, a fetched dict, or a stored row whose `raw` is JSON text.
    """
    if isinstance(row, Post):
        raw = row.raw or {}
        return raw.get("asset"), raw.get("net_usd"), row.indexed_at
    asset = row.get("asset")
    net = row.get("net_usd")
    if (asset is None or net is None) and row.get("raw"):
        raw = row["raw"]
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                raw = {}
        asset = asset if asset is not None else raw.get("asset")
        net = net if net is not None else raw.get("net_usd")
    return asset, net, row.get("indexed_at")


def compute_flow_bias(
    rows,
    *,
    now: datetime | None = None,
    window_hours: float = 96.0,
    halflife_hours: float = 36.0,
    scale: dict | None = None,
) -> dict[str, FlowBias]:
    """Aggregate per-day net flows into a continuous per-asset bias.

    bias = tanh(decay-weighted net_usd / per-asset scale), so big flows saturate
    toward ±1 instead of running away. Accepts flow `Post`s or dict rows.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=window_hours)
    scales = {**DEFAULT_FLOW_SCALE, **(scale or {})}

    acc: dict[str, dict] = {}
    for row in rows:
        asset, net, indexed_at = _flow_fields(row)
        if asset is None or net is None:
            continue
        dt = _parse_dt(indexed_at)
        if dt is None or dt < cutoff:
            continue
        decay = 0.5 ** ((now - dt).total_seconds() / 3600.0 / halflife_hours)
        a = acc.setdefault(asset, {"net": 0.0, "gross": 0.0, "contrib": []})
        a["net"] += decay * float(net)
        a["gross"] += decay * abs(float(net))
        a["contrib"].append((decay * abs(float(net)), asset, float(net), dt))

    out: dict[str, FlowBias] = {}
    for asset, a in acc.items():
        sc = scales.get(asset, _FALLBACK_SCALE)
        bias = math.tanh(a["net"] / sc) if sc else 0.0
        label = "accumulation" if bias > 0.15 else "distribution" if bias < -0.15 else "neutral"
        drivers = [
            f"{asset} {net / 1e6:+.1f}M on {dt.date().isoformat()}"
            for _, _, net, dt in sorted(a["contrib"], key=lambda x: x[0], reverse=True)[:3]
        ]
        out[asset] = FlowBias(asset, round(bias, 3), label, round(a["gross"] / 1e6, 1), drivers)
    return out
