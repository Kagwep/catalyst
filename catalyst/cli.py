"""Catalyst command-line interface.

  catalyst search "terms" [--max N] [--sort latest|top] [--since ISO]
  catalyst author <handle|did> [--max N] [--filter posts_no_replies]
  catalyst rss <feed-url> [--max N]
  catalyst follow [--file news-crypto-blue-sky.txt] [--max N]
  catalyst defillama [--only hacks|tvl|listings] [--min-hack USD] [--min-change PCT]
  catalyst macro [--fred]                  # Fed/ECB press as macro posts
  catalyst regime [--window 72]            # market-wide risk-on/off score
  catalyst governance --spaces uniswapgovernance.eth,aave.eth [--state active]
  catalyst protocols [--file protocols.json] [--no-releases] [--no-governance]
  catalyst run [--config sources.json]
  catalyst poll [--interval 5m] [--once]   # fetch → enrich → plan each cycle
  catalyst query [--limit N] [--source bluesky]
  catalyst enrich [--limit N] [--source bluesky] [--llm] [--model M] [--primary handles]
  catalyst signals [--window 24] [--halflife 6] [--min-strength 0.05] [--asset BTC]
  catalyst compare [--a weights.json] --b other.json     # A/B weight tuning
  catalyst plan [--buy-threshold 0.2] [--max-age 180] [--cooldown 120] [--save]
  catalyst export --out posts.parquet [--format parquet|csv] [--source rss] [--limit N]

Add --save (with optional --db PATH, default catalyst.db) to any fetch command to
upsert results into SQLite; `run`/`poll` persist by default. Fetch output is a
JSON array on stdout (suppress with --quiet).
"""

from __future__ import annotations

import argparse
import json
import re
import signal
import sys
import threading
from datetime import datetime, timezone

from . import bluesky, rss
from .models import Post
from .pipeline import fetch_accounts, load_handles, run_config
from .store import (
    fetch_derivs,
    fetch_enriched,
    fetch_flows,
    fetch_macro,
    fetch_market,
    fetch_onchain,
    fetch_recent_actions,
    fetch_unenriched,
    open_store,
    query_posts,
    save_actions,
    save_bias_snapshots,
    save_enrichments,
    save_posts,
    to_dataframe,
)

_UNITS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600}


def parse_interval(text: str) -> float:
    """Parse '30s' / '5m' / '1h' (bare number = seconds) into seconds."""
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(ms|s|m|h)?", str(text).strip())
    if not m:
        raise ValueError(f'bad --interval "{text}" (use e.g. 30s, 5m, 1h)')
    return float(m.group(1)) * _UNITS[m.group(2) or "s"]


def _signal_weight_kwargs(weights_path: str | None) -> dict:
    """Build compute_signals weighting overrides from an optional weights file.

    Includes the Phase-8a knobs (severity_weights, catalyst_halflives) — the key
    surface lives in `signals.signal_kwargs_from_weights` so it's defined once."""
    if not weights_path:
        return {}
    from .signals import load_weights, signal_kwargs_from_weights

    return signal_kwargs_from_weights(load_weights(weights_path))


def _buy_threshold_override(weights_path: str | None):
    """A tuner-fitted `buy_threshold` from the weights file, if present (else None)."""
    if not weights_path:
        return None
    from .signals import load_weights

    return load_weights(weights_path).get("buy_threshold")


def _confidence_calibration(weights_path: str | None):
    """The Phase-8b `confidence_calibration` stated→realized table, if the file has one."""
    if not weights_path:
        return None
    from .signals import load_weights

    return load_weights(weights_path).get("confidence_calibration")


def _flow_scale(weights_path: str | None) -> dict | None:
    """Per-asset flow tanh-scale overrides from an optional weights file."""
    if not weights_path:
        return None
    from .signals import load_weights

    return load_weights(weights_path).get("flow_scale")


def _flow_bias(conn, args):
    """Per-asset flow bias for the planner, honouring the --flows toggle."""
    if not getattr(args, "flows", True):
        return None
    from .flows import compute_flow_bias

    return compute_flow_bias(fetch_flows(conn), scale=_flow_scale(getattr(args, "weights", None)))


def _supply_kwargs(weights_path: str | None) -> dict:
    """compute_supply_bias overrides (unlock/stake scales) from a weights file."""
    if not weights_path:
        return {}
    from .signals import load_weights

    w = load_weights(weights_path)
    keys = ("horizon_days", "unlock_scale", "stake_scale", "exit_weight")
    return {k: w[k] for k in keys if k in w}


def _supply_bias(conn, args):
    """Per-asset supply bias (unlocks + staking) for the planner, honouring --supply."""
    if not getattr(args, "supply", True):
        return None
    from .onchain import compute_supply_bias

    return compute_supply_bias(fetch_onchain(conn), **_supply_kwargs(getattr(args, "weights", None)))


def _modifier_weights(weights_path: str | None, args) -> dict:
    """Planner modifier weights: tuned `modifier_weights` from the weights file if
    present, else the CLI flag defaults. Lets `calibrate` close the loop."""
    keys = ("macro_weight", "flow_weight", "supply_weight", "market_weight",
            "derivs_weight", "trend_weight")
    tuned = {}
    if weights_path:
        from .signals import load_weights

        tuned = load_weights(weights_path).get("modifier_weights") or {}
    defaults = {"macro_weight": 0.3, "flow_weight": 0.25, "supply_weight": 0.25,
                "market_weight": 0.25, "derivs_weight": 0.25, "trend_weight": 0.25}
    return {k: tuned.get(k, getattr(args, k, defaults[k])) for k in keys}


def _derivs_kwargs(weights_path: str | None) -> dict:
    """compute_derivs_bias overrides (funding_scale/halflife) from a weights file."""
    if not weights_path:
        return {}
    from .signals import load_weights

    w = load_weights(weights_path)
    out = {}
    if "funding_scale" in w:
        out["scale"] = w["funding_scale"]
    if "derivs_halflife" in w:
        out["halflife_hours"] = w["derivs_halflife"]
    return out


def _derivs_bias(conn, args):
    """Per-asset derivatives positioning bias for the planner, honouring --derivs."""
    if not getattr(args, "derivs", True):
        return None
    from .derivs import compute_derivs_bias

    return compute_derivs_bias(fetch_derivs(conn), **_derivs_kwargs(getattr(args, "weights", None)))


def _market_kwargs(weights_path: str | None) -> dict:
    """compute_market_bias overrides (fng_weight/macd_scale) from a weights file."""
    if not weights_path:
        return {}
    from .signals import load_weights

    w = load_weights(weights_path)
    keys = ("fng_weight", "macd_scale")
    return {k: w[k] for k in keys if k in w}


def _market_bias(conn, args, assets):
    """Per-asset market/momentum bias (RSI/MACD + F&G), honouring --market.

    Fetches a recent price history for the signal assets (best-effort) and blends
    it with the stored Fear & Greed series.
    """
    if not getattr(args, "market", True) or not assets:
        return None
    from datetime import timedelta

    from .market import compute_market_bias
    from .prices import PriceOracle

    now = datetime.now(timezone.utc)
    try:
        oracle = PriceOracle.fetch(assets, now - timedelta(days=120), now)
    except Exception as err:  # noqa: BLE001 — momentum is a modifier; never block the plan
        print(f"market bias skipped: {err}", file=sys.stderr)
        return None
    return compute_market_bias(oracle.history(), fetch_market(conn), now=now,
                               **_market_kwargs(getattr(args, "weights", None)))


def _emit(posts: list[Post], quiet: bool) -> None:
    if not quiet:
        json.dump([p.model_dump(mode="json") for p in posts], sys.stdout, indent=2)
        sys.stdout.write("\n")
    print(f"\n{len(posts)} posts", file=sys.stderr)


def _persist(posts: list[Post], db_path: str) -> None:
    conn = open_store(db_path)
    try:
        r = save_posts(conn, posts)
        print(
            f"Saved to {db_path}: {r['inserted']} new, {r['updated']} updated "
            f"({r['total']} fetched)",
            file=sys.stderr,
        )
    finally:
        conn.close()


def _poll_cycle(conn, args, primary, llm_score):
    """One poll cycle: fetch → save → (enrich) → (signals → plan).

    Returns a `CycleHealth` with structured counts, the one-line `summary`, and
    `notable_actions` (buy/sell) for the alert dispatch."""
    from collections import Counter

    from .enrich import hybrid_enrich
    from .monitoring import CycleHealth
    from .planner import plan as run_plan
    from .signals import compute_signals

    health = CycleHealth()

    posts = run_config(args.config)
    r = save_posts(conn, posts)
    health.fetched = len(posts)
    health.inserted = r["inserted"]
    health.per_source = dict(Counter(p.source for p in posts))
    parts = [f"{r['inserted']} new"]

    if args.enrich:
        results = hybrid_enrich(
            fetch_unenriched(conn), llm_score=llm_score, primary_handles=primary,
            llm_all=getattr(args, "llm_all", False),
        )
        health.enriched = save_enrichments(conn, results)
        health.llm_calls = sum(1 for _, e in results if e.model != "lexicon")
        parts.append(f"{health.enriched} enriched")

    notable: list = []
    if args.plan:
        sigs = compute_signals(
            fetch_enriched(conn), window_hours=args.window,
            halflife_hours=args.halflife, primary_handles=primary,
            **_signal_weight_kwargs(getattr(args, "weights", None)),
        )
        regime = None
        if getattr(args, "macro", True):
            from .macro import compute_regime

            regime = compute_regime(fetch_macro(conn))
        flow_bias = _flow_bias(conn, args)
        supply_bias = _supply_bias(conn, args)
        market_bias = _market_bias(conn, args, [s.asset for s in sigs])
        derivs_bias = _derivs_bias(conn, args)
        # Snapshot what each layer saw this cycle — builds point-in-time history
        # for backtesting (essential for staking, an audit for flows/macro).
        save_bias_snapshots(conn, datetime.now(timezone.utc).isoformat(),
                            regime=regime, flow_bias=flow_bias, supply_bias=supply_bias,
                            market_bias=market_bias, derivs_bias=derivs_bias)
        # Trend layer reads the point-in-time bias history we just extended above,
        # so this cycle's fresh snapshot is the latest point in the slope.
        from .trend import compute_trend_bias
        trend_bias = compute_trend_bias(conn, [s.asset for s in sigs])
        recent = fetch_recent_actions(conn, within_minutes=args.cooldown)
        mods = _modifier_weights(getattr(args, "weights", None), args)
        wpath = getattr(args, "weights", None)
        actions = run_plan(
            sigs,
            buy_threshold=_buy_threshold_override(wpath) or args.buy_threshold,
            max_age_minutes=args.max_age,
            fast_max_age_minutes=getattr(args, "fast_max_age", None),
            swing_max_age_minutes=getattr(args, "swing_max_age", None),
            recent_actions=recent, cooldown_minutes=args.cooldown,
            regime=regime, flow_bias=flow_bias, supply_bias=supply_bias,
            market_bias=market_bias, derivs_bias=derivs_bias, trend_bias=trend_bias,
            confidence_calibration=_confidence_calibration(wpath), **mods,
        )
        save_actions(conn, actions)
        notable = [a for a in actions if a.action in ("buy", "sell")]
        health.actions = len(actions)
        health.notable = len(notable)
        health.notable_actions = notable
        health.all_actions = actions       # monitors can watch any action, incl. 'watch'
        tag = f"{len(actions)} actions" + (f", {len(notable)} trade" if notable else "")
        if regime and regime.label != "neutral":
            tag += f", macro {regime.label}"
        if flow_bias and any(b.label != "neutral" for b in flow_bias.values()):
            tag += ", flows " + ",".join(
                f"{a} {b.label}" for a, b in flow_bias.items() if b.label != "neutral"
            )
        if supply_bias and any(b.label != "neutral" for b in supply_bias.values()):
            tag += ", supply " + ",".join(
                f"{a} {b.label}" for a, b in supply_bias.items() if b.label != "neutral"
            )
        parts.append(tag)

    health.summary = ", ".join(parts)
    return health


def _cmd_monitor(args: argparse.Namespace) -> None:
    """Manage the named catalyst monitors (a CLI-owned monitors.json)."""
    from .monitors import build_monitors, load_specs, run_monitors, save_specs

    def _csv(s):
        return [x.strip() for x in (s or "").split(",") if x.strip()]

    path = args.file

    if args.mon_cmd == "list":
        specs = load_specs(path)
        if not specs:
            print(f"no monitors in {path}", file=sys.stderr)
            return
        for s in specs:
            print(json.dumps(s))
        return

    if args.mon_cmd == "add":
        specs = load_specs(path)
        spec: dict = {"name": args.name, "on": _csv(args.on), "actions": _csv(args.actions)}
        if args.catalysts:
            spec["catalysts"] = _csv(args.catalysts)
        if args.assets:
            spec["assets"] = _csv(args.assets)
        if args.horizons:
            spec["horizons"] = _csv(args.horizons)
        spec["min_confidence"] = args.min_conf
        spec["cooldown_minutes"] = args.cooldown
        sinks = []
        if args.webhook:
            sinks.append({"type": "webhook", "url": args.webhook})
        if args.file_sink:
            sinks.append({"type": "file", "path": args.file_sink})
        if sinks:
            spec["sinks"] = sinks
        specs = [s for s in specs if s.get("name") != args.name] + [spec]
        save_specs(path, specs)
        print(f"saved monitor {args.name!r} to {path} ({len(specs)} total)", file=sys.stderr)
        return

    if args.mon_cmd == "rm":
        specs = load_specs(path)
        kept = [s for s in specs if s.get("name") != args.name]
        if len(kept) == len(specs):
            print(f"no monitor named {args.name!r} in {path}", file=sys.stderr)
            return
        save_specs(path, kept)
        print(f"removed monitor {args.name!r} ({len(kept)} left)", file=sys.stderr)
        return

    if args.mon_cmd == "check":
        monitors = build_monitors(load_specs(path))
        if not monitors:
            print(f"no monitors in {path}", file=sys.stderr)
            return
        if not args.deliver:
            for m in monitors:  # preview: force everything to a stderr sink
                m.sinks = []
        conn = open_store(args.db)
        try:
            # Dry-run of the event path only (proposals are evaluated live in `poll`).
            # conn=None → no de-dupe/record, so every current match is previewed.
            posts = fetch_enriched(conn)
        finally:
            conn.close()
        results = run_monitors(monitors, actions=None, posts=posts, conn=None)
        total = sum(r.events_fired for r in results)
        print(f"\n{total} event match(es) across {len(monitors)} monitor(s) "
              f"over {len(posts)} enriched post(s)", file=sys.stderr)
        return


def _cmd_poll(args: argparse.Namespace) -> None:
    interval = parse_interval(args.interval)
    conn = open_store(args.db)
    primary = frozenset(
        h.strip().lstrip("@") for h in (args.primary or "").split(",") if h.strip()
    )
    llm_score = None
    if args.enrich and (args.llm or getattr(args, "llm_all", False)):
        from .enrich import make_anthropic_scorer

        llm_score = make_anthropic_scorer(model=args.model)  # one client, reused each cycle

    # Alerting: build the rules + sinks once (from the config's `alerts` block).
    # Default is a single stderr sink delivering buy/sell — the prior behaviour.
    from pathlib import Path

    from .alerts import build_alerting, dispatch

    try:
        _acfg = json.loads(Path(args.config).read_text(encoding="utf-8")).get("alerts")
    except Exception:  # noqa: BLE001 — missing/broken config → sane stderr default
        _acfg = None
    rules, sinks = build_alerting(_acfg)

    # Monitors: named, catalyst-scoped watches, evaluated in parallel to the alert
    # layer each cycle. They fall back to the alert sinks when a monitor names none.
    from .monitors import load_monitors, run_monitors
    from .store import fetch_enriched

    monitors = load_monitors(args.monitors)
    if monitors:
        print(f"loaded {len(monitors)} monitor(s) from {args.monitors}", file=sys.stderr)

    wake = threading.Event()
    state = {"stopping": False}

    def stop(_signum=None, _frame=None):
        if state["stopping"]:
            sys.exit(130)  # second Ctrl-C: force quit
        state["stopping"] = True
        print("\nStopping after current cycle…", file=sys.stderr)
        wake.set()

    signal.signal(signal.SIGINT, stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop)

    from time import perf_counter

    from .monitoring import CycleHealth, detect_issues, issues_to_actions, OPS_RULE
    from .store import fetch_recent_health, last_cycle_number, save_cycle_health

    mon = _mon_cfg(args.config)

    cycle = last_cycle_number(conn)   # continue the sequence across restarts / --once
    try:
        while not state["stopping"]:
            cycle += 1
            started = datetime.now(timezone.utc).isoformat()
            t0 = perf_counter()
            try:
                health = _poll_cycle(conn, args, primary, llm_score)
                health.cycle = cycle
                health.started_at = started
                health.duration_ms = round((perf_counter() - t0) * 1000.0, 1)
                print(f"[{started}] cycle {cycle}: {health.summary}", file=sys.stderr)
                # Deliver through the alert layer (sinks print/POST; de-dupe +
                # fail-soft live here). The stderr sink reproduces the old lines.
                res = dispatch(health.notable_actions, rules=rules, sinks=sinks, conn=conn)
                if res.suppressed or res.filtered:
                    print(
                        f"    (alerts: {len(res.delivered)} sent, "
                        f"{res.suppressed} de-duped, {res.filtered} filtered)",
                        file=sys.stderr,
                    )
                # Monitors: evaluate the named watches over this cycle's proposals
                # (any action, incl. 'watch') and freshly-enriched catalyst events.
                if monitors:
                    mres = run_monitors(
                        monitors, actions=health.all_actions, posts=fetch_enriched(conn),
                        conn=conn, default_sinks=sinks,
                    )
                    fired = sum(r.actions_fired + r.events_fired for r in mres)
                    if fired:
                        hit = ",".join(
                            f"{r.monitor}({r.actions_fired + r.events_fired})"
                            for r in mres if (r.actions_fired + r.events_fired)
                        )
                        print(f"    (monitors: {fired} fired — {hit})", file=sys.stderr)
            except Exception as err:  # noqa: BLE001 — keep polling through failures
                print(f"[{started}] cycle {cycle} error: {err}", file=sys.stderr)
                health = CycleHealth(cycle=cycle, started_at=started,
                                     duration_ms=round((perf_counter() - t0) * 1000.0, 1),
                                     error=str(err), summary=f"error: {err}")

            # Persist the health row, then raise ops alerts off the accumulated
            # history (source silence / error streak / slow cycle / LLM budget).
            save_cycle_health(conn, health)
            try:
                history = fetch_recent_health(
                    conn, limit=max(mon["silence_cycles"], mon["max_error_streak"]) + 2)
                issues = detect_issues(
                    history, interval_seconds=interval,
                    silence_cycles=mon["silence_cycles"],
                    max_error_streak=mon["max_error_streak"],
                    llm_call_ceiling=mon["llm_call_ceiling"],
                )
                if issues:
                    ops = dispatch(issues_to_actions(issues), rules=OPS_RULE, sinks=sinks, conn=conn)
                    for a in ops.delivered:
                        print(f"    ⚠ OPS {a.rationale}", file=sys.stderr)
            except Exception as err:  # noqa: BLE001 — monitoring must never break polling
                print(f"[{started}] monitoring skipped: {err}", file=sys.stderr)

            if args.once or state["stopping"]:
                break
            wake.wait(interval)  # interruptible sleep
    finally:
        conn.close()
    print(f"Stopped after {cycle} cycle(s).", file=sys.stderr)


def _mon_cfg(config_path: str) -> dict:
    """Monitoring thresholds from the config's `monitoring` block (with defaults)."""
    from pathlib import Path

    defaults = {"silence_cycles": 3, "max_error_streak": 3, "llm_call_ceiling": None}
    try:
        m = json.loads(Path(config_path).read_text(encoding="utf-8")).get("monitoring") or {}
    except Exception:  # noqa: BLE001
        m = {}
    return {**defaults, **m}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="catalyst", description="Multi-source news ingestion.")

    # Shared flags, attached to each subparser so they can appear *after* the
    # subcommand (argparse parent-level optionals must precede the subcommand).
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default="catalyst.db", help="SQLite path (default: catalyst.db)")
    common.add_argument("--save", action="store_true", help="persist results to SQLite")
    common.add_argument("--quiet", action="store_true", help="suppress JSON stdout")

    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search", parents=[common], help="search all of Bluesky by keyword")
    s.add_argument("query", nargs="+")
    s.add_argument("--max", type=int, default=25)
    s.add_argument("--sort", choices=["latest", "top"])
    s.add_argument("--since")

    a = sub.add_parser("author", parents=[common], help="an account's posts + reposts")
    a.add_argument("actor")
    a.add_argument("--max", type=int, default=50)
    a.add_argument("--filter")

    r = sub.add_parser("rss", parents=[common], help="fetch an RSS/Atom feed")
    r.add_argument("url")
    r.add_argument("--max", type=int, default=None)

    fol = sub.add_parser(
        "follow", parents=[common], help="fetch author feeds for handles in a text file"
    )
    fol.add_argument("--file", default="news-crypto-blue-sky.txt")
    fol.add_argument("--max", type=int, default=25, help="posts per account")

    gov = sub.add_parser(
        "governance", parents=[common], help="Snapshot DAO proposals for given spaces"
    )
    gov.add_argument("--spaces", required=True, help="comma-separated Snapshot space ids")
    gov.add_argument("--state", default="active", help="active | closed | pending | all")
    gov.add_argument("--first", type=int, default=20)
    gov.add_argument("--max", type=int, default=None)

    prot = sub.add_parser(
        "protocols", parents=[common], help="registry-driven: GitHub releases + governance"
    )
    prot.add_argument("--file", default="protocols.json")
    prot.add_argument("--releases", action=argparse.BooleanOptionalAction, default=True)
    prot.add_argument("--governance", action=argparse.BooleanOptionalAction, default=True)
    prot.add_argument("--per-repo-max", type=int, default=5)
    prot.add_argument("--state", default="active", help="governance state filter")
    prot.add_argument("--first", type=int, default=20)

    mac = sub.add_parser(
        "macro", parents=[common], help="central-bank press (Fed/ECB) as macro posts"
    )
    mac.add_argument("--max", type=int, default=10)
    mac.add_argument("--fred", action="store_true", help="also pull FRED series (needs FRED_API_KEY)")
    mac.add_argument("--history", type=int, default=1,
                     help="FRED observations per series to backfill (each dated) for backtest history")

    reg = sub.add_parser(
        "regime", parents=[common], help="compute the market-wide risk regime from macro posts"
    )
    reg.add_argument("--window", type=float, default=72.0)
    reg.add_argument("--halflife", type=float, default=24.0)

    flw = sub.add_parser(
        "flows", parents=[common], help="spot-ETF net flows (BTC/ETH) as per-asset flow posts"
    )
    flw.add_argument("--assets", help="comma-separated subset, e.g. BTC,ETH (default: all)")
    flw.add_argument("--max-days", type=int, default=14)

    fb = sub.add_parser(
        "flowbias", parents=[common], help="compute the per-asset flow bias from stored flow posts"
    )
    fb.add_argument("--window", type=float, default=96.0)
    fb.add_argument("--halflife", type=float, default=36.0)
    fb.add_argument("--weights", help="weighting overrides JSON (flow_scale)")

    unl = sub.add_parser(
        "unlocks", parents=[common], help="upcoming token unlocks (DefiLlama) + ETH staking queue as on-chain posts"
    )
    unl.add_argument("--horizon", type=float, default=30.0, help="days ahead to surface unlocks")
    unl.add_argument("--no-staking", action="store_true", help="skip the ETH staking-queue post")

    spb = sub.add_parser(
        "supplybias", parents=[common], help="compute the per-asset supply bias from stored on-chain posts"
    )
    spb.add_argument("--weights", help="weighting overrides JSON (unlock_scale/stake_scale)")

    oca = sub.add_parser(
        "onchain-actions", parents=[common],
        help="watched-contract governance events (proxy upgrades / timelock / treasury) as on-chain posts",
    )
    oca.add_argument("--config", default="sources.json",
                     help="read the watch list from this config's onchain_actions block")
    oca.add_argument("--address", help="ad-hoc: a single contract to watch (overrides --config)")
    oca.add_argument("--asset", help="ad-hoc: ticker to attribute --address to")
    oca.add_argument("--kinds", default="upgrade,timelock",
                     help="ad-hoc: comma-separated kinds for --address (upgrade|timelock|treasury)")
    oca.add_argument("--from-address", help="ad-hoc: treasury sender filter (for the treasury kind)")
    oca.add_argument("--rpc", help="Ethereum JSON-RPC URL (default: public node)")
    oca.add_argument("--lookback", type=int, default=300, help="blocks to scan back from head")
    oca.add_argument("--chunk", type=int, default=100, help="block window per request (keyless nodes cap the range)")
    oca.add_argument("--min-value-usd", type=float, default=0.0, help="treasury USD gate")

    fng = sub.add_parser(
        "fng", parents=[common], help="Fear & Greed index (free, alternative.me) as market posts"
    )
    fng.add_argument("--limit", type=int, default=30, help="recent days to backfill")

    mkb = sub.add_parser(
        "marketbias", parents=[common], help="per-asset market/momentum bias (RSI/MACD + Fear & Greed)"
    )
    mkb.add_argument("--assets", default="BTC,ETH", help="comma-separated tickers")
    mkb.add_argument("--weights", help="weighting overrides JSON (fng_weight/macd_scale)")

    dv = sub.add_parser(
        "derivs", parents=[common], help="perp funding + open interest (Binance, no key) as derivs posts"
    )
    dv.add_argument("--assets", default="BTC,ETH,SOL", help="comma-separated tickers")
    dv.add_argument("--funding-limit", type=int, default=30, help="funding records per asset (~8h each)")
    dv.add_argument("--no-oi", action="store_true", help="skip the open-interest context posts")

    dvb = sub.add_parser(
        "derivsbias", parents=[common], help="per-asset derivatives positioning bias (perp funding)"
    )
    dvb.add_argument("--weights", help="weighting overrides JSON (funding_scale/derivs_halflife)")

    bt = sub.add_parser(
        "backtest", parents=[common],
        help="replay the planner over history and score it on prices (signal-quality study)",
    )
    bt.add_argument("--from", dest="start", help="start date YYYY-MM-DD (default: 30d before --to)")
    bt.add_argument("--to", dest="end", help="end date YYYY-MM-DD (default: today)")
    bt.add_argument("--step-hours", type=float, default=24.0, help="replay cadence")
    bt.add_argument("--intraday-hours", type=float, default=24.0, help="hold for intraday-horizon trades")
    bt.add_argument("--short-hours", type=float, default=72.0, help="hold for short-horizon trades")
    bt.add_argument("--period", default="1d", help="price granularity (1d|1h)")
    bt.add_argument("--buy-threshold", type=float, default=0.2)
    bt.add_argument("--cooldown", type=float, default=120.0)
    bt.add_argument("--weights", help="weighting overrides JSON")
    bt.add_argument("--trades", action="store_true", help="include the full trade list + equity curve in the JSON")
    bt.add_argument("--portfolio", action=argparse.BooleanOptionalAction, default=True,
                    help="also run the Phase-2 portfolio sim (sizing + fees → equity curve)")
    bt.add_argument("--base-size", type=float, default=0.2, help="position size at full confidence (fraction of equity)")
    bt.add_argument("--max-position", type=float, default=0.5, help="cap on any single position")
    bt.add_argument("--cost-bps", type=float, default=10.0, help="fee + slippage per side, in basis points")

    cal = sub.add_parser(
        "calibrate", parents=[common],
        help="sweep modifier weights over the backtest and keep the winners (no hand-picking)",
    )
    cal.add_argument("--from", dest="start", help="start date YYYY-MM-DD (default: 30d before --to)")
    cal.add_argument("--to", dest="end", help="end date YYYY-MM-DD (default: today)")
    cal.add_argument("--metric", default="sharpe",
                     choices=["sharpe", "total_return", "hit_rate", "calibration"],
                     help="objective to maximise")
    cal.add_argument("--step-hours", type=float, default=24.0)
    cal.add_argument("--rounds", type=int, default=2, help="coordinate-ascent passes")
    cal.add_argument("--write", help="weights.json to write the tuned modifier_weights into")

    tun = sub.add_parser(
        "tune", parents=[common],
        help="Phase-8b: random-search the scorer's weights over the backtest and emit "
             "a self-describing weights.tuned.json (fitted params + measured metrics)",
    )
    tun.add_argument("--from", dest="start", help="start date YYYY-MM-DD (default: --window before --to)")
    tun.add_argument("--to", dest="end", help="end date YYYY-MM-DD (default: today)")
    tun.add_argument("--window", type=float, default=30.0, help="lookback in days if --from is omitted")
    tun.add_argument("--step-hours", type=float, default=24.0, help="replay cadence")
    tun.add_argument("--trials", type=int, default=25, help="candidate weight sets to try")
    tun.add_argument("--seed", type=int, default=0, help="RNG seed (same seed+window → same output)")
    tun.add_argument("--min-trades", type=int, default=5,
                     help="disqualify candidates scoring fewer trades (avoids degenerate winners)")
    tun.add_argument("--calibration-penalty", type=float, default=0.5,
                     help="objective = hit_rate − penalty × calibration_error")
    tun.add_argument("--out", default="weights.tuned.json", help="output artifact path")

    dll = sub.add_parser(
        "defillama", parents=[common], help="protocol signals: hacks, TVL moves, new listings"
    )
    dll.add_argument("--only", choices=["hacks", "tvl", "listings"], help="restrict to one signal type")
    dll.add_argument("--hack-days", type=int, default=30)
    dll.add_argument("--min-hack", type=float, default=1_000_000, help="min hack size USD")
    dll.add_argument("--min-tvl", type=float, default=50_000_000)
    dll.add_argument("--min-change", type=float, default=15.0, help="min |TVL change| %%")
    dll.add_argument("--window", choices=["1h", "1d", "7d"], default="1d")
    dll.add_argument("--listing-days", type=int, default=7)
    dll.add_argument("--max", type=int, default=25, help="cap per signal type")

    run = sub.add_parser("run", parents=[common], help="batch fetch from config")
    run.add_argument("--config", default="sources.json")

    croo = sub.add_parser(
        "croo-provider", parents=[common],
        help="run as a Croo Network provider agent: accept paid orders, deliver Action[] (needs croo SDK + CROO_* env)",
    )
    croo.add_argument("--assets", help="restrict covered universe, e.g. BTC,ETH (default: cover all)")
    croo.add_argument(
        "--no-op", action="store_true",
        help="SDK smoke test: accept every order and deliver a hardcoded probe payload "
             "(no pipeline). Proves auth + WS loop + accept/deliver against the real backend.",
    )
    croo.add_argument(
        "--no-present", action="store_true",
        help="disable the grounded LLM narration even if ANTHROPIC_API_KEY is set "
             "(narration only restates computed numbers; it's on by default when a key exists).",
    )
    croo.add_argument("--present-model", default="claude-opus-4-8",
                      help="model for the narration layer (e.g. claude-haiku-4-5 for cheaper)")

    poll = sub.add_parser(
        "poll", parents=[common],
        help="run the full pipeline (fetch → enrich → plan) on an interval",
    )
    poll.add_argument("--config", default="sources.json")
    poll.add_argument("--interval", default="5m")
    poll.add_argument("--once", action="store_true")
    # Each cycle also enriches new posts and re-plans by default.
    poll.add_argument("--enrich", action=argparse.BooleanOptionalAction, default=True)
    poll.add_argument("--plan", action=argparse.BooleanOptionalAction, default=True)
    poll.add_argument("--llm", action="store_true", help="LLM-score candidates during enrich")
    poll.add_argument("--llm-all", action="store_true",
                      help="MANDATORY LLM scoring of every post with text (bypass candidate gate). Implies --llm.")
    poll.add_argument("--model", default="claude-opus-4-8")
    poll.add_argument("--primary", default="watcher.guru")
    poll.add_argument("--window", type=float, default=24.0)
    poll.add_argument("--halflife", type=float, default=6.0)
    poll.add_argument("--buy-threshold", type=float, default=0.2)
    poll.add_argument("--max-age", type=float, default=180.0)
    poll.add_argument("--cooldown", type=float, default=120.0)
    poll.add_argument("--weights", help="weighting overrides JSON (see weights.json)")
    poll.add_argument("--macro", action=argparse.BooleanOptionalAction, default=True)
    poll.add_argument("--macro-weight", type=float, default=0.3)
    poll.add_argument("--flows", action=argparse.BooleanOptionalAction, default=True)
    poll.add_argument("--flow-weight", type=float, default=0.25)
    poll.add_argument("--supply", action=argparse.BooleanOptionalAction, default=True)
    poll.add_argument("--supply-weight", type=float, default=0.25)
    poll.add_argument("--market", action=argparse.BooleanOptionalAction, default=True)
    poll.add_argument("--market-weight", type=float, default=0.25)
    poll.add_argument("--derivs", action=argparse.BooleanOptionalAction, default=True)
    poll.add_argument("--derivs-weight", type=float, default=0.25)
    poll.add_argument("--fast-max-age", type=float, default=60.0,
                      help="minutes; intraday/fast-catalyst signals go stale faster than --max-age")
    poll.add_argument("--swing-max-age", type=float, default=10080.0,
                      help="minutes; multi-day (swing) trend signals tolerate older data (default 7d)")
    poll.add_argument("--monitors", default="monitors.json",
                      help="path to the monitor definitions (absent file = no monitors)")

    # Named catalyst monitors: list / add / rm / check (a CLI-managed monitors.json).
    monf = argparse.ArgumentParser(add_help=False)
    monf.add_argument("--file", default="monitors.json", help="monitor definitions file")
    mon = sub.add_parser(
        "monitor", help="manage named catalyst monitors (list/add/rm/check)",
    )
    mon_sub = mon.add_subparsers(dest="mon_cmd", required=True)
    mon_sub.add_parser("list", parents=[monf], help="show configured monitors")
    madd = mon_sub.add_parser("add", parents=[monf], help="add or replace a monitor")
    madd.add_argument("name")
    madd.add_argument("--catalysts", help="comma list, e.g. treasury,unlock (default: any)")
    madd.add_argument("--assets", help="comma list, e.g. AAVE,ARB (default: any)")
    madd.add_argument("--on", default="proposal,event", help="trigger paths: proposal,event")
    madd.add_argument("--actions", default="buy,sell", help="proposal-path actions")
    madd.add_argument("--horizons", help="proposal-path horizons, e.g. intraday,short")
    madd.add_argument("--min-conf", type=float, default=0.0, help="proposal-path min confidence")
    madd.add_argument("--cooldown", type=float, default=60.0, help="de-dupe window (minutes)")
    madd.add_argument("--webhook", help="deliver matches to this webhook URL (Slack/Discord/Telegram/n8n)")
    madd.add_argument("--file-sink", help="append matches as JSONL to this path")
    mrm = mon_sub.add_parser("rm", parents=[monf], help="remove a monitor by name")
    mrm.add_argument("name")
    mchk = mon_sub.add_parser(
        "check", parents=[common, monf],
        help="dry-run the event path over the current store (preview to stderr)",
    )
    mchk.add_argument("--deliver", action="store_true",
                      help="actually deliver via each monitor's own sinks (default: stderr preview)")

    st = sub.add_parser(
        "status", parents=[common],
        help="operator health: last cycle, source freshness, open proposals, alerts, ops issues",
    )
    st.add_argument("--interval", default="5m", help="expected poll interval (slow-cycle detection)")
    st.add_argument("--window", type=float, default=24.0, help="hours for proposal/alert counts")

    q = sub.add_parser("query", parents=[common], help="read stored posts, newest-first")
    q.add_argument("--limit", type=int, default=20)
    q.add_argument("--source")

    pl = sub.add_parser(
        "plan", parents=[common], help="propose ranked candidate actions from signals (proposals only)"
    )
    pl.add_argument("--window", type=float, default=24.0)
    pl.add_argument("--halflife", type=float, default=6.0)
    pl.add_argument("--buy-threshold", type=float, default=0.2, help="|score| to propose buy/sell")
    pl.add_argument("--watch-threshold", type=float, default=0.1)
    pl.add_argument("--min-confidence", type=float, default=0.0)
    pl.add_argument("--max-age", type=float, default=180.0, help="minutes; older signals downgrade to watch")
    pl.add_argument("--cooldown", type=float, default=120.0, help="minutes to suppress repeat actions (with --save)")
    pl.add_argument("--limit", type=int, default=20)
    pl.add_argument("--primary", default="watcher.guru")
    pl.add_argument("--weights", help="weighting overrides JSON (see weights.json)")
    pl.add_argument("--macro", action=argparse.BooleanOptionalAction, default=True,
                    help="apply the macro risk-regime modifier")
    pl.add_argument("--macro-weight", type=float, default=0.3)
    pl.add_argument("--flows", action=argparse.BooleanOptionalAction, default=True,
                    help="apply the per-asset ETF-flow modifier")
    pl.add_argument("--flow-weight", type=float, default=0.25)
    pl.add_argument("--supply", action=argparse.BooleanOptionalAction, default=True,
                    help="apply the per-asset supply modifier (unlocks + staking)")
    pl.add_argument("--supply-weight", type=float, default=0.25)
    pl.add_argument("--market", action=argparse.BooleanOptionalAction, default=True,
                    help="apply the per-asset market/momentum modifier (RSI/MACD + Fear & Greed)")
    pl.add_argument("--market-weight", type=float, default=0.25)
    pl.add_argument("--derivs", action=argparse.BooleanOptionalAction, default=True,
                    help="apply the per-asset derivatives positioning modifier (perp funding/OI)")
    pl.add_argument("--derivs-weight", type=float, default=0.25)
    pl.add_argument("--fast-max-age", type=float, default=60.0,
                    help="minutes; intraday/fast-catalyst signals go stale faster than --max-age")
    pl.add_argument("--swing-max-age", type=float, default=10080.0,
                    help="minutes; multi-day (swing) trend signals tolerate older data (default 7d)")

    cmp = sub.add_parser(
        "compare", parents=[common], help="A/B two weight configs on signals (tuning)"
    )
    cmp.add_argument("--a", help="weights JSON for side A (omit = built-in defaults)")
    cmp.add_argument("--b", help="weights JSON for side B (omit = built-in defaults)")
    cmp.add_argument("--window", type=float, default=24.0)
    cmp.add_argument("--halflife", type=float, default=6.0)
    cmp.add_argument("--primary", default="watcher.guru")
    cmp.add_argument("--source")
    cmp.add_argument("--limit", type=int, default=20)

    sig = sub.add_parser(
        "signals", parents=[common], help="rank per-asset trade signals from enriched posts"
    )
    sig.add_argument("--window", type=float, default=24.0, help="lookback window in hours")
    sig.add_argument("--halflife", type=float, default=6.0, help="recency half-life in hours")
    sig.add_argument("--min-strength", type=float, default=0.05)
    sig.add_argument("--limit", type=int, default=20)
    sig.add_argument("--primary", default="watcher.guru", help="comma-separated high-signal handles")
    sig.add_argument("--source")
    sig.add_argument("--asset", help="restrict to one ticker, e.g. BTC")
    sig.add_argument("--weights", help="weighting overrides JSON (see weights.json)")

    en = sub.add_parser(
        "enrich", parents=[common], help="score stored posts (sentiment / assets / catalyst)"
    )
    en.add_argument("--limit", type=int, default=None)
    en.add_argument("--source")
    en.add_argument("--llm", action="store_true", help="LLM-score candidates (needs [llm] extra + API key)")
    en.add_argument("--llm-all", action="store_true",
                    help="MANDATORY LLM scoring of every post with text (bypass the candidate "
                         "gate; posts are the catalyst/sentiment source). Implies --llm.")
    en.add_argument("--model", default="claude-opus-4-8", help="LLM model (e.g. claude-haiku-4-5)")
    en.add_argument("--primary", default="watcher.guru", help="comma-separated high-signal handles")
    en.add_argument("--reenrich", action="store_true", help="re-score already-scored posts")

    ex = sub.add_parser(
        "export", parents=[common], help="export stored posts to Parquet/CSV (needs [ml] extra)"
    )
    ex.add_argument("--out", required=True, help="output file path")
    ex.add_argument("--format", choices=["parquet", "csv"], default="parquet")
    ex.add_argument("--source")
    ex.add_argument("--limit", type=int, default=None)
    return p


def _load_dotenv(path: str = ".env") -> None:
    """Load KEY=VALUE lines from .env into os.environ (real env vars win)."""
    import os

    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def main(argv: list[str] | None = None) -> int:
    _load_dotenv()
    args = build_parser().parse_args(argv)

    if args.cmd == "query":
        conn = open_store(args.db)
        try:
            rows = query_posts(conn, limit=args.limit, source=args.source)
        finally:
            conn.close()
        json.dump(rows, sys.stdout, indent=2)
        sys.stdout.write("\n")
        print(f"\n{len(rows)} posts from {args.db}", file=sys.stderr)
        return 0

    if args.cmd == "status":
        from .monitoring import status_report

        interval_s = parse_interval(args.interval)
        conn = open_store(args.db)
        try:
            rep = status_report(conn, interval_seconds=interval_s, window_hours=args.window)
        finally:
            conn.close()
        json.dump(rep, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
        lc = rep["last_cycle"]
        state = "healthy" if rep["healthy"] else \
            f"{len(rep['ops_issues'])} ops issue(s), error streak {rep['error_streak']}"
        print(
            f"\nstatus: {state}; last cycle {lc['at'] if lc else 'never'}; "
            f"{rep['open_proposals']} open proposals, {rep['alerts_24h']} alerts/24h",
            file=sys.stderr,
        )
        return 0

    if args.cmd == "plan":
        from dataclasses import asdict

        from .planner import plan as run_plan
        from .signals import compute_signals

        primary = frozenset(
            h.strip().lstrip("@") for h in (args.primary or "").split(",") if h.strip()
        )
        conn = open_store(args.db)
        try:
            rows = fetch_enriched(conn)
            sigs = compute_signals(
                rows, window_hours=args.window, halflife_hours=args.halflife,
                primary_handles=primary, **_signal_weight_kwargs(args.weights),
            )
            recent = fetch_recent_actions(conn, within_minutes=args.cooldown) if args.save else None
            regime = None
            if args.macro:
                from .macro import compute_regime

                regime = compute_regime(fetch_macro(conn))
            flow_bias = _flow_bias(conn, args)
            supply_bias = _supply_bias(conn, args)
            market_bias = _market_bias(conn, args, [s.asset for s in sigs])
            derivs_bias = _derivs_bias(conn, args)
            from .trend import compute_trend_bias
            trend_bias = compute_trend_bias(conn, [s.asset for s in sigs])
            mods = _modifier_weights(args.weights, args)
            actions = run_plan(
                sigs,
                buy_threshold=_buy_threshold_override(args.weights) or args.buy_threshold,
                watch_threshold=args.watch_threshold,
                min_confidence=args.min_confidence,
                max_age_minutes=args.max_age,
                fast_max_age_minutes=args.fast_max_age,
                swing_max_age_minutes=getattr(args, "swing_max_age", None),
                recent_actions=recent,
                cooldown_minutes=args.cooldown,
                regime=regime,
                flow_bias=flow_bias,
                supply_bias=supply_bias,
                market_bias=market_bias,
                derivs_bias=derivs_bias,
                trend_bias=trend_bias,
                confidence_calibration=_confidence_calibration(args.weights),
                **mods,
            )
            actions = actions[: args.limit]
            if args.save:
                save_actions(conn, actions)
        finally:
            conn.close()
        json.dump([asdict(a) for a in actions], sys.stdout, indent=2)
        sys.stdout.write("\n")
        saved = " (saved)" if args.save else ""
        print(f"\n{len(actions)} proposed actions{saved} — proposals only, not executed", file=sys.stderr)
        return 0

    if args.cmd == "regime":
        from dataclasses import asdict

        from .macro import compute_regime

        conn = open_store(args.db)
        try:
            r = compute_regime(fetch_macro(conn), window_hours=args.window, halflife_hours=args.halflife)
        finally:
            conn.close()
        json.dump(asdict(r), sys.stdout, indent=2)
        sys.stdout.write("\n")
        print(f"\nmacro regime: {r.label} ({r.score:+.2f}), evidence {r.evidence}", file=sys.stderr)
        return 0

    if args.cmd == "flowbias":
        from dataclasses import asdict

        from .flows import compute_flow_bias

        conn = open_store(args.db)
        try:
            biases = compute_flow_bias(
                fetch_flows(conn), window_hours=args.window, halflife_hours=args.halflife,
                scale=_flow_scale(args.weights),
            )
        finally:
            conn.close()
        json.dump({a: asdict(b) for a, b in biases.items()}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        summary = ", ".join(f"{a} {b.label} ({b.bias:+.2f})" for a, b in biases.items()) or "no flow data"
        print(f"\nflow bias: {summary}", file=sys.stderr)
        return 0

    if args.cmd == "supplybias":
        from dataclasses import asdict

        from .onchain import compute_supply_bias

        conn = open_store(args.db)
        try:
            biases = compute_supply_bias(fetch_onchain(conn), **_supply_kwargs(args.weights))
        finally:
            conn.close()
        json.dump({a: asdict(b) for a, b in biases.items()}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        summary = ", ".join(f"{a} {b.label} ({b.bias:+.2f})" for a, b in biases.items()) or "no on-chain data"
        print(f"\nsupply bias: {summary}", file=sys.stderr)
        return 0

    if args.cmd == "marketbias":
        from dataclasses import asdict
        from datetime import timedelta

        from .market import compute_market_bias
        from .prices import PriceOracle

        assets = [a.strip().upper() for a in args.assets.split(",") if a.strip()]
        now = datetime.now(timezone.utc)
        oracle = PriceOracle.fetch(assets, now - timedelta(days=120), now)
        conn = open_store(args.db)
        try:
            biases = compute_market_bias(oracle.history(), fetch_market(conn), now=now,
                                         **_market_kwargs(args.weights))
        finally:
            conn.close()
        json.dump({a: asdict(b) for a, b in biases.items()}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        summary = ", ".join(f"{a} {b.label} ({b.bias:+.2f})" for a, b in biases.items()) or "no market data"
        print(f"\nmarket bias: {summary}", file=sys.stderr)
        return 0

    if args.cmd == "derivsbias":
        from dataclasses import asdict

        from .derivs import compute_derivs_bias

        conn = open_store(args.db)
        try:
            biases = compute_derivs_bias(fetch_derivs(conn), **_derivs_kwargs(args.weights))
        finally:
            conn.close()
        json.dump({a: asdict(b) for a, b in biases.items()}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        summary = ", ".join(f"{a} {b.label} ({b.bias:+.2f})" for a, b in biases.items()) or "no derivs data"
        print(f"\nderivs bias: {summary}", file=sys.stderr)
        return 0

    if args.cmd == "backtest":
        from dataclasses import asdict
        from datetime import timedelta

        from .backtest import run_backtest

        def _date(s):
            return datetime.fromisoformat(s).replace(tzinfo=timezone.utc) if s else None

        end = _date(args.end) or datetime.now(timezone.utc)
        start = _date(args.start) or (end - timedelta(days=30))
        conn = open_store(args.db)
        try:
            result = run_backtest(
                conn, start=start, end=end, step_hours=args.step_hours,
                horizon_hours={"intraday": args.intraday_hours, "short": args.short_hours},
                period=args.period,
                signal_kwargs=_signal_weight_kwargs(args.weights),
                flow_scale=_flow_scale(args.weights),
                supply_kwargs=_supply_kwargs(args.weights),
                market_kwargs=_market_kwargs(args.weights),
                derivs_kwargs=_derivs_kwargs(args.weights),
                plan_kwargs={
                    "buy_threshold": _buy_threshold_override(args.weights) or args.buy_threshold,
                    "cooldown_minutes": args.cooldown,
                },
                portfolio_cfg=(
                    {"base_size": args.base_size, "max_position": args.max_position,
                     "cost_bps": args.cost_bps} if args.portfolio else None
                ),
            )
        finally:
            conn.close()
        out = asdict(result)
        if not args.trades:
            out.pop("trades", None)
            if out.get("portfolio"):
                out["portfolio"].pop("equity_curve", None)
        json.dump(out, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
        base = f", baseline BTC {result.baseline_btc:+.1%}" if result.baseline_btc is not None else ""
        print(
            f"\n{result.scored} trades scored ({result.skipped} skipped, no price), "
            f"hit-rate {result.hit_rate:.0%}, mean {result.mean_return:+.2%}, "
            f"cumulative {result.cum_return:+.1%}{base} — signal-quality study",
            file=sys.stderr,
        )
        if result.portfolio is not None:
            p = result.portfolio
            print(
                f"portfolio: total return {p.total_return:+.1%}, Sharpe {p.sharpe}, "
                f"max DD {p.max_drawdown:.1%}, win-rate {p.win_rate:.0%}, "
                f"{p.deployed} trades, fees {p.fees_paid:.1%} — sized by confidence, net of fees",
                file=sys.stderr,
            )
        return 0

    if args.cmd == "calibrate":
        from datetime import timedelta

        from .calibrate import run_calibration, write_weights

        def _date(s):
            return datetime.fromisoformat(s).replace(tzinfo=timezone.utc) if s else None

        end = _date(args.end) or datetime.now(timezone.utc)
        start = _date(args.start) or (end - timedelta(days=30))
        conn = open_store(args.db)
        try:
            result = run_calibration(
                conn, metric=args.metric, rounds=args.rounds,
                bt_kwargs={"start": start, "end": end, "step_hours": args.step_hours},
            )
        finally:
            conn.close()
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")
        if args.write:
            write_weights(args.write, result["modifier_weights"])
            print(f"Wrote tuned modifier_weights to {args.write}", file=sys.stderr)
        print(
            f"\nbest {args.metric}={result['score']} over {result['trials']} trials: "
            f"{result['modifier_weights']}", file=sys.stderr,
        )
        return 0

    if args.cmd == "tune":
        from datetime import timedelta

        from .tune import run_tune

        def _date(s):
            return datetime.fromisoformat(s).replace(tzinfo=timezone.utc) if s else None

        end = _date(args.end) or datetime.now(timezone.utc)
        start = _date(args.start) or (end - timedelta(days=args.window))
        conn = open_store(args.db)
        try:
            tuned = run_tune(
                conn, start=start, end=end, step_hours=args.step_hours,
                trials=args.trials, seed=args.seed, min_trades=args.min_trades,
                calibration_penalty=args.calibration_penalty, out=args.out,
            )
        finally:
            conn.close()
        json.dump(tuned, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
        m = tuned["_tuning"]
        print(
            f"\ntuned over {m['trials']} trials (seed {m['seed']}): hit-rate {m['hit_rate']}, "
            f"calibration-error {m['calibration_error']}, {m['n_trades']} trades "
            f"→ wrote {args.out}", file=sys.stderr,
        )
        return 0

    if args.cmd == "compare":
        from .compare import compare_weights
        from .signals import load_weights

        a = load_weights(args.a) if args.a else None
        b = load_weights(args.b) if args.b else None
        primary = frozenset(
            h.strip().lstrip("@") for h in (args.primary or "").split(",") if h.strip()
        )
        conn = open_store(args.db)
        try:
            rows = fetch_enriched(conn, source=args.source)
        finally:
            conn.close()
        diffs = compare_weights(
            rows, a=a, b=b, window_hours=args.window, halflife_hours=args.halflife,
            primary_handles=primary,
        )[: args.limit]
        json.dump(diffs, sys.stdout, indent=2)
        sys.stdout.write("\n")
        moved = sum(1 for d in diffs if d["score_delta"])
        print(
            f"\n{len(diffs)} assets compared ({moved} moved) — A={args.a or 'default'} B={args.b or 'default'}",
            file=sys.stderr,
        )
        return 0

    if args.cmd == "signals":
        from dataclasses import asdict

        from .signals import compute_signals

        conn = open_store(args.db)
        try:
            rows = fetch_enriched(conn, source=args.source)
        finally:
            conn.close()
        primary = frozenset(
            h.strip().lstrip("@") for h in (args.primary or "").split(",") if h.strip()
        )
        sigs = compute_signals(
            rows,
            window_hours=args.window,
            halflife_hours=args.halflife,
            primary_handles=primary,
            min_strength=args.min_strength,
            **_signal_weight_kwargs(args.weights),
        )
        if args.asset:
            sigs = [s for s in sigs if s.asset == args.asset.upper()]
        sigs = sigs[: args.limit]
        json.dump([asdict(s) for s in sigs], sys.stdout, indent=2)
        sys.stdout.write("\n")
        print(f"\n{len(sigs)} signals over {args.window}h from {len(rows)} enriched posts", file=sys.stderr)
        return 0

    if args.cmd == "enrich":
        from collections import Counter

        from .enrich import hybrid_enrich, make_anthropic_scorer

        conn = open_store(args.db)
        try:
            items = fetch_unenriched(
                conn, limit=args.limit, source=args.source, reenrich=args.reenrich
            )
            use_llm = args.llm or args.llm_all
            llm_score = make_anthropic_scorer(model=args.model) if use_llm else None
            primary = frozenset(
                h.strip().lstrip("@") for h in (args.primary or "").split(",") if h.strip()
            )
            results = hybrid_enrich(items, llm_score=llm_score, primary_handles=primary,
                                    llm_all=args.llm_all)
            n = save_enrichments(conn, results)
        finally:
            conn.close()
        labels = Counter(e.sentiment_label for _, e in results)
        llm_used = sum(1 for _, e in results if e.model != "lexicon")
        print(
            f"Enriched {n} posts ({llm_used} via LLM). Labels: {dict(labels)}",
            file=sys.stderr,
        )
        return 0

    if args.cmd == "export":
        conn = open_store(args.db)
        try:
            df = to_dataframe(conn, source=args.source, limit=args.limit)
        finally:
            conn.close()
        if args.format == "parquet":
            df.to_parquet(args.out, index=False)
        else:
            df.to_csv(args.out, index=False)
        print(f"Wrote {len(df)} rows to {args.out} ({args.format})", file=sys.stderr)
        return 0

    if args.cmd == "poll":
        _cmd_poll(args)
        return 0

    if args.cmd == "monitor":
        _cmd_monitor(args)
        return 0

    if args.cmd == "croo-provider":
        import asyncio
        import logging
        import os

        # Surface the provider's accept/deliver/settle events (logger.info) — on
        # stdout locally and in the Railway logs. Without this they're swallowed.
        logging.basicConfig(
            level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )

        from .croo_agent import CrooProvider, build_client_from_env, events_pipeline, make_no_op_provider

        covered = [a.strip().upper() for a in args.assets.split(",")] if args.assets else None
        client = build_client_from_env()
        if args.no_op:
            provider = make_no_op_provider(client, db_path=args.db, covered_assets=covered)
            print("Croo provider starting in NO-OP mode — accepts every order, delivers a "
                  "hardcoded probe (SDK smoke test, no pipeline). Ctrl-C to stop.", file=sys.stderr)
        else:
            # Optional grounded narration: on only when a key is present and not
            # opted out. Absent → deterministic delivery, no LLM, no key needed.
            present = None
            if not args.no_present and os.environ.get("ANTHROPIC_API_KEY"):
                from .present import make_anthropic_presenter

                present = make_anthropic_presenter(model=args.present_model)
                print(f"  narration ON ({args.present_model}) — grounded summary/notes added",
                      file=sys.stderr)
            # Second service: the events feed, keyed by its Croo service_id.
            services = {}
            events_sid = os.environ.get("CROO_EVENTS_SERVICE_ID")
            if events_sid:
                services[events_sid] = lambda req: events_pipeline(args.db, req)
                print(f"  events service ON (service_id={events_sid[:8]}…) — catalyst.events feed",
                      file=sys.stderr)
            provider = CrooProvider(client, db_path=args.db, covered_assets=covered,
                                    present=present, services=services)
            print("Croo provider starting — Ctrl-C to stop (proposals only, not executed)", file=sys.stderr)
        try:
            asyncio.run(provider.run())
        except KeyboardInterrupt:
            print("\nStopped.", file=sys.stderr)
        finally:
            asyncio.run(client.close())
        return 0

    save = args.save
    if args.cmd == "search":
        posts = bluesky.search_posts(
            " ".join(args.query), max=args.max, sort=args.sort, since=args.since
        )
    elif args.cmd == "author":
        posts = bluesky.get_author_feed(args.actor, max=args.max, filter=args.filter)
    elif args.cmd == "rss":
        posts = rss.fetch_feed(args.url, max=args.max)
    elif args.cmd == "follow":
        posts = fetch_accounts(load_handles(args.file), max=args.max)
    elif args.cmd == "macro":
        from . import macro as mc

        posts = mc.fetch_central_banks(max=args.max)
        if args.fred:
            posts += mc.fetch_fred(max=args.max, history=args.history)
    elif args.cmd == "flows":
        from . import flows as fl

        assets = [a.strip().upper() for a in args.assets.split(",")] if args.assets else None
        posts = fl.fetch_etf_flows(assets=assets, max_days=args.max_days)
    elif args.cmd == "unlocks":
        from . import onchain as oc

        posts = oc.fetch_unlocks(horizon_days=args.horizon)
        if not args.no_staking:
            try:
                posts += oc.fetch_stake_queue()
            except Exception as err:  # noqa: BLE001 — staking is optional, never block unlocks
                print(f"staking queue skipped: {err}", file=sys.stderr)
    elif args.cmd == "onchain-actions":
        from pathlib import Path

        from . import onchain_actions as oca

        if args.address:
            watch = [{
                "address": args.address, "asset": args.asset or "",
                "kinds": [k.strip() for k in args.kinds.split(",") if k.strip()],
                "from": args.from_address,
            }]
            min_usd = args.min_value_usd
        else:
            block = json.loads(Path(args.config).read_text(encoding="utf-8")).get("onchain_actions") or {}
            watch = block.get("watch") or []
            min_usd = block.get("min_value_usd", args.min_value_usd)
        posts = oca.fetch_onchain_actions(
            watch, rpc_url=args.rpc or oca.DEFAULT_RPC,
            lookback_blocks=args.lookback, chunk_blocks=args.chunk, min_value_usd=min_usd,
        )
    elif args.cmd == "derivs":
        from . import derivs as dv

        assets = [a.strip().upper() for a in args.assets.split(",") if a.strip()]
        posts = dv.fetch_derivs(assets=assets, funding_limit=args.funding_limit,
                                open_interest=not args.no_oi)
    elif args.cmd == "fng":
        from . import market

        posts = market.fetch_fear_greed(limit=args.limit)
    elif args.cmd == "governance":
        from . import snapshot as sn

        spaces = [s.strip() for s in args.spaces.split(",") if s.strip()]
        posts = sn.fetch_proposals(spaces, state=args.state, first=args.first, max=args.max)
    elif args.cmd == "protocols":
        from . import protocols as pr
        from .pipeline import dedupe_newest

        registry = pr.load_registry(args.file)
        posts = []
        if args.releases:
            posts += pr.fetch_releases(registry, per_repo_max=args.per_repo_max)
        if args.governance:
            posts += pr.fetch_governance(registry, state=args.state, first=args.first)
        posts = dedupe_newest(posts)
    elif args.cmd == "defillama":
        from . import defillama as dl
        from .pipeline import dedupe_newest

        posts = []
        only = args.only
        if only in (None, "hacks"):
            posts += dl.fetch_hacks(since_days=args.hack_days, min_amount=args.min_hack, max=args.max)
        if only in (None, "tvl", "listings"):
            protocols = dl.fetch_protocols()
            if only in (None, "tvl"):
                posts += dl.tvl_changes(
                    protocols, min_tvl=args.min_tvl, min_change_pct=args.min_change,
                    window=args.window, max=args.max,
                )
            if only in (None, "listings"):
                posts += dl.new_listings(protocols, days=args.listing_days, min_tvl=args.min_tvl, max=args.max)
        posts = dedupe_newest(posts)
    elif args.cmd == "run":
        posts = run_config(args.config)
        save = True  # run persists by default
    else:  # pragma: no cover — argparse enforces choices
        raise SystemExit(2)

    if save:
        _persist(posts, args.db)
    _emit(posts, args.quiet)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
