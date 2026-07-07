"""Monitors — named, catalyst-scoped watches that alert on their own channels.

A **parallel** delivery layer to `alerts.py`. Where the alert layer runs ONE
global rule over the planner's buy/sell proposals, a *monitor* is a named,
declarative watch an operator sets up — "watch AAVE treasury moves", "ping me on
Telegram when any imminent-unlock SELL fires above 0.6 confidence". Each monitor:

  - is **catalyst-first**: the primary selector is which catalysts to watch;
  - fires on two trigger paths, independently selectable per monitor via `on`:
      * ``proposal`` — a planner ``Action`` matched the monitor's assets / catalysts
        / action / confidence / horizon (the "strategy/actions proposed" trigger);
      * ``event`` — a freshly-enriched post carrying a watched catalyst on a watched
        asset landed, *before* any full buy/sell proposal (the raw-catalyst trigger);
  - routes to its **own sinks** (falling back to the shared default sinks), reusing
    the exact `Sink` classes + canonical payloads the alert layer uses. A Telegram/
    Discord/Slack push is just a `WebhookSink`; a Croo delivery would be one too.

Monitors are operator-owned: they live in a CLI-managed JSON file (default
``monitors.json``) and are evaluated each poll cycle. De-dupe is per-monitor and
per-trigger, persisted in SQLite so it survives restarts:
  - proposals de-dupe on ``(monitor, "asset:action")`` within the monitor cooldown;
  - events de-dupe on ``(monitor, post-uri)`` within the same window — a catalyst
    event, being a one-off, fires once and is not re-announced.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .alerts import _SINK_BUILDERS, Sink, StderrSink
from .payload import build_event_payload, build_payload

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _age_minutes(indexed_at: str | None, now: datetime) -> float:
    """Age of a post in minutes; 0.0 (treated as fresh) when the timestamp is
    missing/unparseable, so we alert rather than silently drop."""
    dt = _parse_dt(indexed_at)
    if dt is None:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (now - dt).total_seconds() / 60.0


def _parse_assets(raw) -> set[str]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return set()
    return {a.upper() for a in (raw or [])}


# ---------------------------------------------------------------------------
# Monitor
# ---------------------------------------------------------------------------


@dataclass
class Monitor:
    """A named, catalyst-scoped watch with its own delivery routing.

    Criteria are AND-combined; a `None`/empty criterion doesn't filter. `assets`
    are compared upper-case, `catalysts`/`actions`/`horizons` lower-case.
    """

    name: str
    catalysts: frozenset[str] | None = None       # the catalyst-first selector; None = any
    assets: frozenset[str] | None = None           # None = any asset
    on: frozenset[str] = frozenset({"proposal", "event"})  # which trigger paths are live
    actions: frozenset[str] = frozenset({"buy", "sell"})   # proposal path: allowed actions
    horizons: frozenset[str] | None = None         # proposal path: allowed horizons
    min_confidence: float = 0.0                    # proposal path only (events have no confidence)
    cooldown_minutes: float = 60.0                 # de-dupe window + event freshness lookback
    quiet_hours: tuple[int, int] | None = None     # [start, end) UTC hours suppressed
    sinks: list[Sink] = field(default_factory=list)  # own routing; empty → the shared defaults

    def in_quiet_hours(self, now: datetime) -> bool:
        if not self.quiet_hours:
            return False
        start, end = self.quiet_hours
        h = now.hour
        return start <= h < end if start <= end else (h >= start or h < end)

    def matches_action(self, a) -> bool:
        """Proposal-path match against a planner `Action`."""
        if a.action not in self.actions:
            return False
        if a.confidence < self.min_confidence:
            return False
        if self.assets is not None and a.asset.upper() not in self.assets:
            return False
        if self.catalysts is not None:
            cats = {c.lower() for c in (a.catalysts or [])}
            if not (cats & self.catalysts):
                return False
        if self.horizons is not None and a.horizon.lower() not in self.horizons:
            return False
        return True

    def matches_event(self, post: dict) -> bool:
        """Event-path match against an enriched post row (from `fetch_enriched`)."""
        cat = (post.get("catalyst") or "").lower()
        if not cat:
            return False
        if self.catalysts is not None and cat not in self.catalysts:
            return False
        if self.assets is not None and not (_parse_assets(post.get("assets")) & self.assets):
            return False
        return True


@dataclass
class MonitorResult:
    monitor: str
    actions_fired: int = 0
    events_fired: int = 0
    suppressed: int = 0            # matched but de-duped
    delivered: bool = False        # at least one sink accepted
    quiet: bool = False            # skipped entirely by quiet hours


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def _deliver(payload: dict, sinks: list[Sink]) -> bool:
    """Send to every sink, fail-soft; return True if any sink accepted it."""
    ok = False
    for sink in sinks:
        try:
            if sink.send(payload):
                ok = True
        except Exception as err:  # noqa: BLE001 — a dead sink must not sink the loop
            print(f"monitor sink {sink.name} failed: {err}", file=sys.stderr)
    return ok


def _recent(conn, monitor: str, kind: str, within: float, now: datetime) -> set[str]:
    if conn is None:
        return set()
    from .store import fetch_recent_monitor_fires

    return fetch_recent_monitor_fires(conn, monitor, kind, within_minutes=within, now=now)


def _record(conn, monitor: str, kind: str, refs: list[str], now: datetime) -> None:
    if conn is None or not refs:
        return
    from .store import save_monitor_fires

    save_monitor_fires(conn, monitor, kind, refs, now=now)


def evaluate(
    monitor: Monitor, *, actions=None, posts=None, conn=None,
    default_sinks: list[Sink] | None = None, now: datetime | None = None,
) -> MonitorResult:
    """Run one monitor over this cycle's `actions` (planner) and `posts` (enriched),
    delivering fresh matches through its sinks and recording the de-dupe refs."""
    now = now or datetime.now(timezone.utc)
    sinks = monitor.sinks or default_sinks or [StderrSink()]
    res = MonitorResult(monitor=monitor.name)

    if monitor.in_quiet_hours(now):
        res.quiet = True
        return res

    # --- proposal path ---
    if "proposal" in monitor.on and actions:
        recent = _recent(conn, monitor.name, "proposal", monitor.cooldown_minutes, now)
        fire = []
        for a in actions:
            if not monitor.matches_action(a):
                continue
            ref = f"{a.asset}:{a.action}"
            if ref in recent:
                res.suppressed += 1
                continue
            fire.append(a)
            recent.add(ref)  # collapse dups within this same batch too
        if fire:
            payload = build_payload(fire, generated_at=now.isoformat(),
                                    meta={"monitor": monitor.name})
            payload["monitor"] = monitor.name  # so StderrSink can tag it
            if _deliver(payload, sinks):
                _record(conn, monitor.name, "proposal", [f"{a.asset}:{a.action}" for a in fire], now)
                res.actions_fired = len(fire)
                res.delivered = True

    # --- event path ---
    if "event" in monitor.on and posts:
        recent = _recent(conn, monitor.name, "event", monitor.cooldown_minutes, now)
        fire = []
        for p in posts:
            if not monitor.matches_event(p):
                continue
            if _age_minutes(p.get("indexed_at"), now) > monitor.cooldown_minutes:
                continue  # too old to be "fresh" for this monitor
            ref = p.get("uri")
            if not ref or ref in recent:
                res.suppressed += 1 if ref else 0
                continue
            fire.append(p)
            recent.add(ref)
        if fire:
            payload = build_event_payload(fire, monitor=monitor.name, generated_at=now.isoformat())
            if _deliver(payload, sinks):
                _record(conn, monitor.name, "event", [p.get("uri") for p in fire], now)
                res.events_fired = len(fire)
                res.delivered = True

    return res


def run_monitors(
    monitors: list[Monitor], *, actions=None, posts=None, conn=None,
    default_sinks: list[Sink] | None = None, now: datetime | None = None,
) -> list[MonitorResult]:
    """Evaluate every monitor for this cycle. One bad monitor never stops the rest."""
    now = now or datetime.now(timezone.utc)
    out: list[MonitorResult] = []
    for m in monitors:
        try:
            out.append(evaluate(m, actions=actions, posts=posts, conn=conn,
                                 default_sinks=default_sinks, now=now))
        except Exception as err:  # noqa: BLE001 — isolate a broken monitor
            print(f"monitor {m.name} failed: {err}", file=sys.stderr)
            out.append(MonitorResult(monitor=m.name))
    return out


# ---------------------------------------------------------------------------
# Config: raw spec dicts (CLI-managed) <-> Monitor objects
# ---------------------------------------------------------------------------


def _build_sinks(spec: dict) -> list[Sink]:
    sinks: list[Sink] = []
    for s in spec.get("sinks", []) or []:
        builder = _SINK_BUILDERS.get(s.get("type"))
        if builder is None:
            print(f"monitor {spec.get('name')}: unknown sink type {s.get('type')}", file=sys.stderr)
            continue
        try:
            sinks.append(builder(s))
        except Exception as err:  # noqa: BLE001 — a misconfigured sink shouldn't drop the monitor
            print(f"monitor {spec.get('name')}: sink {s.get('type')} config error: {err}",
                  file=sys.stderr)
    return sinks


def monitor_from_spec(spec: dict) -> Monitor:
    """Build a `Monitor` from a plain dict (one entry of `monitors.json`)."""
    def _fs(key: str, *, alt: str | None = None) -> frozenset[str] | None:
        v = spec.get(key)
        if v is None and alt:
            v = spec.get(alt)
        if not v:
            return None
        v = [v] if isinstance(v, str) else v
        return frozenset(x.lower() for x in v)

    quiet = spec.get("quiet_hours")
    return Monitor(
        name=spec["name"],
        catalysts=_fs("catalysts"),
        assets=(frozenset(a.upper() for a in spec["assets"]) if spec.get("assets") else None),
        on=frozenset(x.lower() for x in spec.get("on", ["proposal", "event"])),
        actions=frozenset(a.lower() for a in spec.get("actions", ["buy", "sell"])),
        horizons=_fs("horizons", alt="horizon"),
        min_confidence=float(spec.get("min_confidence", 0.0) or 0.0),
        cooldown_minutes=float(spec.get("cooldown_minutes", 60.0)),
        quiet_hours=tuple(quiet) if quiet else None,
        sinks=_build_sinks(spec),
    )


def build_monitors(specs: list[dict]) -> list[Monitor]:
    out: list[Monitor] = []
    for spec in specs or []:
        try:
            out.append(monitor_from_spec(spec))
        except Exception as err:  # noqa: BLE001 — skip a broken spec, keep the good ones
            print(f"skipping bad monitor spec {spec!r}: {err}", file=sys.stderr)
    return out


def load_specs(path: str) -> list[dict]:
    """Read the monitor spec list from `path`. Accepts either a bare JSON array or
    an object with a top-level `monitors` key. Missing file → no monitors."""
    p = Path(path)
    if not p.exists():
        return []
    data = json.loads(p.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("monitors", [])
    return list(data or [])


def save_specs(path: str, specs: list[dict]) -> None:
    Path(path).write_text(json.dumps(specs, indent=2) + "\n", encoding="utf-8")


def load_monitors(path: str) -> list[Monitor]:
    return build_monitors(load_specs(path))
