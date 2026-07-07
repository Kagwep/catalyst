"""Batch fetch shared by the `run` and `poll` commands.

Reads sources.json, fetches Bluesky keywords + accounts and RSS/Atom feeds,
de-dupes by URI, and returns newest-first. A failing feed is logged and skipped
rather than sinking the whole batch.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from . import bluesky, rss
from .models import Post


def dedupe_newest(posts: list[Post]) -> list[Post]:
    """De-dupe by URI, then sort newest-first by indexed_at."""
    seen: set[str] = set()
    out: list[Post] = []
    for p in posts:
        if p.uri and p.uri not in seen:
            seen.add(p.uri)
            out.append(p)
    out.sort(key=lambda p: p.indexed_at or "", reverse=True)
    return out


def load_handles(path: str) -> list[str]:
    """Read Bluesky handles from a text file: one per line, `@` optional,
    `#` comments and blank lines ignored."""
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    handles: list[str] = []
    for line in lines:
        line = line.split("#", 1)[0].strip()
        if line:
            handles.append(line.lstrip("@"))
    return handles


def fetch_accounts(handles: list[str], *, max: int = 25) -> list[Post]:
    """Fetch author feeds for a list of handles; a bad handle is skipped."""
    results: list[Post] = []
    for h in handles:
        try:
            results.extend(bluesky.get_author_feed(h, max=max))
        except Exception as err:  # noqa: BLE001 — one bad handle shouldn't fail the batch
            print(f"account @{h} skipped: {err}", file=sys.stderr)
    return dedupe_newest(results)


def _safe(label: str, fn) -> list[Post]:
    """Run a fetch, isolating failures so one bad source can't sink the batch."""
    try:
        return fn()
    except Exception as err:  # noqa: BLE001
        print(f"{label} skipped: {err}", file=sys.stderr)
        return []


def run_config(config_path: str) -> list[Post]:
    cfg = json.loads(Path(config_path).read_text(encoding="utf-8"))
    results: list[Post] = []

    # Primary signal first: the handle file, then any explicit accounts.
    # (fetch_accounts already isolates per-handle failures.)
    if cfg.get("accounts_file"):
        results.extend(
            fetch_accounts(load_handles(cfg["accounts_file"]), max=cfg.get("accounts_max", 25))
        )
    for a in cfg.get("accounts", []):
        results.extend(
            _safe(
                f"account @{a['actor']}",
                lambda a=a: bluesky.get_author_feed(
                    a["actor"], max=a.get("max", 50), filter=a.get("filter")
                ),
            )
        )
    # Depth: keyword searches and RSS feeds. Each isolated.
    for k in cfg.get("keywords", []):
        results.extend(
            _safe(
                f'search "{k["q"]}"',
                lambda k=k: bluesky.search_posts(
                    k["q"], max=k.get("max", 25), sort=k.get("sort"), since=k.get("since")
                ),
            )
        )
    for f in cfg.get("feeds", []):
        url = f if isinstance(f, str) else f["url"]
        cap = f.get("max") if isinstance(f, dict) else None
        results.extend(_safe(f"feed {url}", lambda url=url, cap=cap: rss.fetch_feed(url, max=cap)))

    # DefiLlama protocol signals: hacks, TVL moves, new listings.
    if cfg.get("defillama"):
        results.extend(_defillama(cfg["defillama"]))

    # Protocol registry: GitHub releases + Snapshot governance.
    if cfg.get("protocols_file"):
        results.extend(_protocols(cfg))

    # Macro: central-bank press (+ optional FRED).
    if cfg.get("macro"):
        results.extend(_macro_sources(cfg["macro"]))

    # Flows: spot-ETF net flows (BTC/ETH) as per-asset flow posts.
    if cfg.get("flows"):
        results.extend(_flows_sources(cfg["flows"]))

    # On-chain tier: token unlocks (DefiLlama) + ETH staking queue.
    if cfg.get("onchain"):
        results.extend(_onchain_sources(cfg["onchain"]))

    # On-chain actions: governance/technical events (upgrades/timelock/treasury).
    if cfg.get("onchain_actions"):
        results.extend(_onchain_actions_sources(cfg["onchain_actions"]))

    # Derivatives layer: perp funding + open interest (positioning bias).
    if cfg.get("derivs"):
        results.extend(_derivs_sources(cfg["derivs"]))

    # Market layer: Fear & Greed index (price technicals are computed at plan time).
    if cfg.get("market"):
        results.extend(_market_sources(cfg["market"]))

    results = dedupe_newest(results)  # URI-exact
    return _collapse_cross_source(results, cfg.get("dedupe"))


def _collapse_cross_source(posts: list[Post], dcfg) -> list[Post]:
    """Collapse the same story from multiple sources to the highest-trust one."""
    if dcfg is False or (isinstance(dcfg, dict) and not dcfg.get("enabled", True)):
        return posts
    from .dedupe import collapse_dupes

    opts = dcfg if isinstance(dcfg, dict) else {}
    deduped, n = collapse_dupes(posts, jaccard=opts.get("jaccard", 0.72))
    if n:
        print(f"cross-source de-dupe: collapsed {n} near-duplicate(s)", file=sys.stderr)
    return deduped


def _market_sources(m) -> list[Post]:
    from . import market

    opts = m if isinstance(m, dict) else {}
    if isinstance(m, dict) and not opts.get("fear_greed", True):
        return []
    return _safe("fear & greed", lambda: market.fetch_fear_greed(limit=opts.get("limit", 30)))


def _derivs_sources(d) -> list[Post]:
    from . import derivs as dv

    opts = d if isinstance(d, dict) else {}
    return _safe("derivs funding/OI", lambda: dv.fetch_derivs(
        assets=opts.get("assets"),
        funding_limit=opts.get("funding_limit", 30),
        open_interest=opts.get("open_interest", True),
        oi_limit=opts.get("oi_limit", 30),
    ))


def _onchain_actions_sources(oa) -> list[Post]:
    from . import onchain_actions as oca

    opts = oa if isinstance(oa, dict) else {}
    watch = opts.get("watch") or []
    if not watch:
        return []
    return _safe("onchain actions", lambda: oca.fetch_onchain_actions(
        watch,
        rpc_url=opts.get("rpc_url", oca.DEFAULT_RPC),
        lookback_blocks=opts.get("lookback_blocks", oca.DEFAULT_LOOKBACK_BLOCKS),
        chunk_blocks=opts.get("chunk_blocks", oca.DEFAULT_CHUNK_BLOCKS),
        min_value_usd=opts.get("min_value_usd", 0.0),
    ))


def _onchain_sources(o) -> list[Post]:
    from . import onchain as oc

    opts = o if isinstance(o, dict) else {}
    out: list[Post] = []
    unlocks = opts.get("unlocks", True)
    if unlocks:
        u = unlocks if isinstance(unlocks, dict) else {}
        out.extend(_safe("unlocks", lambda: oc.fetch_unlocks(horizon_days=u.get("horizon_days", 30))))
    if opts.get("staking", True):
        out.extend(_safe("staking queue", oc.fetch_stake_queue))
    return out


def _flows_sources(f) -> list[Post]:
    from . import flows as fl

    opts = f if isinstance(f, dict) else {}
    if isinstance(f, dict) and not opts.get("etf", True):
        return []
    return _safe(
        "etf flows",
        lambda: fl.fetch_etf_flows(assets=opts.get("assets"), max_days=opts.get("max_days", 14)),
    )


def _macro_sources(m) -> list[Post]:
    from . import macro as mc

    opts = m if isinstance(m, dict) else {}
    out: list[Post] = []
    if not isinstance(m, dict) or opts.get("central_banks", True):
        out.extend(_safe("central banks", lambda: mc.fetch_central_banks(max=opts.get("max", 10))))
    fred = opts.get("fred") or {}
    if fred.get("api_key"):
        out.extend(
            _safe("fred", lambda: mc.fetch_fred(
                fred.get("series"), api_key=fred["api_key"], max=fred.get("max"),
                history=fred.get("history", 1),
            ))
        )
    return out


def _protocols(cfg: dict) -> list[Post]:
    from . import protocols as pr

    opts = cfg.get("protocols", {}) if isinstance(cfg.get("protocols"), dict) else {}
    try:
        registry = pr.load_registry(cfg["protocols_file"])
    except Exception as err:  # noqa: BLE001 — bad registry file shouldn't fail the batch
        print(f"protocols registry skipped: {err}", file=sys.stderr)
        return []

    out: list[Post] = []
    out.extend(
        _safe("protocol releases", lambda: pr.fetch_releases(registry, per_repo_max=opts.get("releases_max", 5)))
    )
    out.extend(
        _safe(
            "protocol governance",
            lambda: pr.fetch_governance(
                registry, state=opts.get("governance_state", "active"),
                first=opts.get("governance_first", 20),
            ),
        )
    )
    return out


def _defillama(dl_cfg: dict) -> list[Post]:
    from . import defillama as dl

    out: list[Post] = []
    h = dl_cfg.get("hacks")
    if h:
        h = h if isinstance(h, dict) else {}
        out.extend(
            _safe(
                "defillama hacks",
                lambda: dl.fetch_hacks(
                    since_days=h.get("since_days", 30),
                    min_amount=h.get("min_amount", 1_000_000),
                    max=h.get("max", 50),
                ),
            )
        )

    tvl_cfg, lst_cfg = dl_cfg.get("tvl"), dl_cfg.get("listings")
    if tvl_cfg or lst_cfg:
        protocols = _safe("defillama protocols", dl.fetch_protocols)
        if protocols:
            if tvl_cfg:
                t = tvl_cfg if isinstance(tvl_cfg, dict) else {}
                out.extend(
                    _safe(
                        "defillama tvl",
                        lambda: dl.tvl_changes(
                            protocols,
                            min_tvl=t.get("min_tvl", 50_000_000),
                            min_change_pct=t.get("min_change_pct", 15),
                            window=t.get("window", "1d"),
                            max=t.get("max", 25),
                        ),
                    )
                )
            if lst_cfg:
                ls = lst_cfg if isinstance(lst_cfg, dict) else {}
                out.extend(
                    _safe(
                        "defillama listings",
                        lambda: dl.new_listings(
                            protocols,
                            days=ls.get("days", 7),
                            min_tvl=ls.get("min_tvl", 1_000_000),
                            max=ls.get("max", 25),
                        ),
                    )
                )
    return out
