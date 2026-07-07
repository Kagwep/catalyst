"""Alerts — turn planner proposals into delivered, de-duplicated notifications.

The first delivery subsystem. It sits between the planner and the outside world:
a rule decides which `Action`s are worth sending, an alert-layer cooldown stops
the same call spamming, and pluggable **sinks** ship the result. Every sink emits
the one canonical payload (`payload.build_payload`), so a webhook push and — later
— a Croo `deliver_order` carry byte-for-byte the same structure. A Croo delivery
is, by design, just another `Sink`.

Guarantees that matter operationally:
  - **De-dupe survives restarts** — the delivered history lives in SQLite, so a
    repeat `(asset, action)` inside the cooldown is suppressed across process
    restarts, not just within one run.
  - **A sink failure never sinks the loop** — each `send` is isolated; a dead
    webhook logs and the poll cycle continues.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

from .payload import build_payload

# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass
class AlertRule:
    """Decides whether an `Action` warrants delivery."""

    min_confidence: float = 0.0
    actions: frozenset[str] = frozenset({"buy", "sell"})
    catalysts: frozenset[str] | None = None            # None = any (incl. none)
    per_asset: dict = field(default_factory=dict)       # asset -> {min_confidence, actions}
    quiet_hours: tuple[int, int] | None = None          # [start, end) UTC hours suppressed
    cooldown_minutes: float = 60.0

    def _in_quiet_hours(self, now: datetime) -> bool:
        if not self.quiet_hours:
            return False
        start, end = self.quiet_hours
        h = now.hour
        return start <= h < end if start <= end else (h >= start or h < end)

    def allows(self, action, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        if self._in_quiet_hours(now):
            return False
        override = self.per_asset.get(action.asset) or self.per_asset.get(action.asset.upper()) or {}
        allowed = override.get("actions")
        allowed = frozenset(a.lower() for a in allowed) if allowed else self.actions
        if action.action not in allowed:
            return False
        min_conf = override.get("min_confidence", self.min_confidence)
        if action.confidence < min_conf:
            return False
        if self.catalysts is not None and not (set(action.catalysts or []) & self.catalysts):
            return False
        return True


# ---------------------------------------------------------------------------
# Sinks
# ---------------------------------------------------------------------------


class Sink:
    """A delivery target. `send(payload)` returns True on success. Subclasses must
    never raise for a delivery failure — return False; `dispatch` also guards."""

    name = "sink"

    def send(self, payload: dict) -> bool:  # pragma: no cover - interface
        raise NotImplementedError


class StderrSink(Sink):
    """Human-readable lines to stderr — the always-on default (replaces the old
    inline poll print)."""

    name = "stderr"

    def send(self, payload: dict) -> bool:
        tag = f"[{payload['monitor']}] " if payload.get("monitor") else ""
        for a in payload.get("actions", []):
            print(
                f"    → {tag}{a['direction'].upper()} {a['signal'].upper()} {a['asset']} "
                f"conf={a['confidence']:.2f} [{a['horizon']}] {a['rationale']}",
                file=sys.stderr,
            )
        for e in payload.get("events", []):  # catalyst-event payloads (from monitors)
            assets = ",".join(e.get("assets") or []) or "?"
            print(
                f"    ⚡ {tag}{(e.get('catalyst') or '?')} {assets} "
                f"[{e.get('source') or '?'}] {(e.get('text') or '')[:120]}",
                file=sys.stderr,
            )
        return True


class FileSink(Sink):
    """Append the payload as one JSON line to a file (durable audit / n8n tail)."""

    name = "file"

    def __init__(self, path: str):
        self.path = path

    def send(self, payload: dict) -> bool:
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload) + "\n")
        return True


class WebhookSink(Sink):
    """POST the payload as JSON — Slack/Discord/Telegram bots, n8n, any HTTP hook."""

    name = "webhook"

    def __init__(self, url: str, *, headers: dict | None = None, timeout: float = 10.0):
        self.url = url
        self.headers = {"Content-Type": "application/json", **(headers or {})}
        self.timeout = timeout

    def send(self, payload: dict) -> bool:
        resp = httpx.post(self.url, json=payload, headers=self.headers, timeout=self.timeout)
        return 200 <= resp.status_code < 300


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


@dataclass
class DispatchResult:
    delivered: list = field(default_factory=list)   # actions that went out
    suppressed: int = 0                             # dropped by the alert cooldown
    filtered: int = 0                               # dropped by the rule
    sink_results: dict = field(default_factory=dict)


def dispatch(
    actions,
    *,
    rules: AlertRule,
    sinks: list[Sink],
    conn=None,
    now: datetime | None = None,
    meta: dict | None = None,
) -> DispatchResult:
    """Filter → alert-layer de-dupe → deliver via every sink → record history.

    De-dupe reads/writes the SQLite alert history (when `conn` is given) so a
    repeat inside `rules.cooldown_minutes` is suppressed across restarts. Delivery
    is fail-soft per sink; the delivered actions are recorded only if at least one
    sink accepted them.
    """
    now = now or datetime.now(timezone.utc)
    actions = list(actions)

    passed = [a for a in actions if rules.allows(a, now)]
    filtered = len(actions) - len(passed)

    # Alert-layer de-dupe (separate from the planner cooldown).
    recent: dict[tuple[str, str], datetime] = {}
    if conn is not None:
        from .store import fetch_recent_alerts

        for r in fetch_recent_alerts(conn, within_minutes=rules.cooldown_minutes, now=now):
            dt = _parse_dt(r.get("delivered_at"))
            key = (r.get("asset"), r.get("action"))
            if dt and (key not in recent or dt > recent[key]):
                recent[key] = dt

    candidates, suppressed = [], 0
    for a in passed:
        last = recent.get((a.asset, a.action))
        if last and (now - last).total_seconds() / 60.0 < rules.cooldown_minutes:
            suppressed += 1
            continue
        candidates.append(a)

    result = DispatchResult(delivered=[], suppressed=suppressed, filtered=filtered)
    if not candidates:
        return result

    payload = build_payload(candidates, generated_at=now.isoformat(), meta=meta)
    ok_sinks: list[str] = []
    for sink in sinks:
        try:
            ok = sink.send(payload)
        except Exception as err:  # noqa: BLE001 — a dead sink must not sink the loop
            print(f"alert sink {sink.name} failed: {err}", file=sys.stderr)
            ok = False
        result.sink_results[sink.name] = ok
        if ok:
            ok_sinks.append(sink.name)

    if ok_sinks and conn is not None:
        from .store import save_alerts

        save_alerts(conn, candidates, sinks=",".join(ok_sinks), now=now)
    result.delivered = candidates
    return result


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_SINK_BUILDERS = {
    "stderr": lambda c: StderrSink(),
    "file": lambda c: FileSink(c["path"]),
    "webhook": lambda c: WebhookSink(c["url"], headers=c.get("headers"), timeout=c.get("timeout", 10.0)),
}


def build_alerting(cfg: dict | None) -> tuple[AlertRule, list[Sink]]:
    """Build (rules, sinks) from an `alerts` config block. Defaults: deliver
    buy/sell to a single stderr sink — i.e. the pre-Phase-3 behaviour."""
    cfg = cfg or {}
    quiet = cfg.get("quiet_hours")
    rule = AlertRule(
        min_confidence=cfg.get("min_confidence", 0.0),
        actions=frozenset(a.lower() for a in cfg.get("actions", ["buy", "sell"])),
        catalysts=frozenset(cfg["catalysts"]) if cfg.get("catalysts") else None,
        per_asset=cfg.get("per_asset", {}) or {},
        quiet_hours=tuple(quiet) if quiet else None,
        cooldown_minutes=cfg.get("cooldown_minutes", 60.0),
    )
    sinks: list[Sink] = []
    for s in cfg.get("sinks", [{"type": "stderr"}]):
        builder = _SINK_BUILDERS.get(s.get("type"))
        if builder is None:
            print(f"unknown alert sink type: {s.get('type')}", file=sys.stderr)
            continue
        try:
            sinks.append(builder(s))
        except Exception as err:  # noqa: BLE001 — a misconfigured sink shouldn't kill startup
            print(f"alert sink {s.get('type')} config error: {err}", file=sys.stderr)
    if not sinks:
        sinks = [StderrSink()]
    return rule, sinks
