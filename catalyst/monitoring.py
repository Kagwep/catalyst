"""Monitoring — is the *oracle itself* healthy, distinct from the trade signal.

A hired agent must be able to *prove* it's running, not just claim it. Each poll
cycle writes a structured `CycleHealth` row (timing, per-source fetch counts,
enrich/action counts, any error); `detect_issues` reads that history to raise
**ops alerts** when a source goes silent, the loop starts erroring or overrunning
its interval, or the LLM call budget is blown. Ops alerts ride the very same
Phase-3 sinks — they're just `Action`s with `action="ops"` delivered under a
separate rule, so a dead webhook or a de-dupe cooldown works identically.

`status_report` assembles the one-screen operator view (`catalyst status`): last
cycle, per-source freshness, open proposals, alert counts, error streak.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .alerts import AlertRule
from .planner import Action

# Ops alerts reuse the sinks but under their own rule (their own action type +
# a longer cooldown so a persistent problem doesn't spam every cycle).
OPS_RULE = AlertRule(min_confidence=0.0, actions=frozenset({"ops"}), cooldown_minutes=120.0)


@dataclass
class CycleHealth:
    """One poll cycle's structured health — the machine-readable cycle summary."""

    cycle: int = 0
    started_at: str = ""
    duration_ms: float = 0.0
    fetched: int = 0
    inserted: int = 0
    enriched: int = 0
    llm_calls: int = 0
    actions: int = 0
    notable: int = 0
    error: str | None = None
    per_source: dict = field(default_factory=dict)   # {source: fetched_count}
    summary: str = ""
    notable_actions: list = field(default_factory=list)  # not persisted; fed to alert dispatch
    all_actions: list = field(default_factory=list)       # not persisted; fed to the monitors layer


@dataclass
class Issue:
    kind: str      # source_silent | error_streak | slow_cycle | llm_budget
    subject: str   # the source name, or "loop"
    detail: str


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _field(row, name, default=None):
    """Read a health field from either a dict (store row) or a CycleHealth."""
    return row.get(name, default) if isinstance(row, dict) else getattr(row, name, default)


def detect_issues(
    history: list[dict],
    *,
    interval_seconds: float | None = None,
    silence_cycles: int = 3,
    max_error_streak: int = 3,
    llm_call_ceiling: int | None = None,
    slow_factor: float = 1.5,
) -> list[Issue]:
    """Derive ops issues from recent cycle-health rows (newest-first).

    - **source_silent**: a source that has produced before is now at 0 for the
      last `silence_cycles` cycles.
    - **error_streak**: the last `max_error_streak` cycles all errored.
    - **slow_cycle**: the latest cycle ran longer than `slow_factor × interval`.
    - **llm_budget**: the latest cycle's LLM calls exceeded `llm_call_ceiling`.
    """
    if not history:
        return []
    issues: list[Issue] = []
    latest = history[0]

    # error streak (leading run of errored cycles)
    streak = 0
    for h in history:
        if _field(h, "error"):
            streak += 1
        else:
            break
    if streak >= max_error_streak:
        issues.append(Issue("error_streak", "loop",
                            f"{streak} consecutive cycle errors (latest: {_field(latest, 'error')})"))

    # slow cycle
    dur = _field(latest, "duration_ms")
    if interval_seconds and dur and dur > slow_factor * interval_seconds * 1000.0:
        issues.append(Issue("slow_cycle", "loop",
                            f"cycle took {dur / 1000.0:.1f}s vs {interval_seconds:.0f}s interval"))

    # llm budget
    if llm_call_ceiling is not None and (_field(latest, "llm_calls") or 0) > llm_call_ceiling:
        issues.append(Issue("llm_budget", "loop",
                            f"{_field(latest, 'llm_calls')} LLM calls > ceiling {llm_call_ceiling}"))

    # source silence — needs at least `silence_cycles` cycles of history
    if len(history) >= silence_cycles:
        window = history[:silence_cycles]
        known = {s for h in history for s, n in (_field(h, "per_source") or {}).items() if n}
        for src in sorted(known):
            if all(not (_field(h, "per_source") or {}).get(src) for h in window):
                issues.append(Issue("source_silent", src,
                                    f"source '{src}' produced 0 items for {silence_cycles} cycles"))
    return issues


def issues_to_actions(issues: list[Issue], *, now: datetime | None = None) -> list[Action]:
    """Convert ops issues into `Action`s (action='ops') deliverable via the sinks."""
    now = now or datetime.now(timezone.utc)
    out: list[Action] = []
    for i in issues:
        out.append(Action(
            asset=f"OPS:{i.subject}", action="ops", direction="neutral", confidence=1.0,
            horizon="intraday", score=0.0, rationale=f"[{i.kind}] {i.detail}",
            catalysts=["ops", i.kind], created_at=now.isoformat(),
            layers={"ops": {"kind": i.kind, "subject": i.subject}},
        ))
    return out


def status_report(conn, *, now: datetime | None = None, interval_seconds: float | None = None,
                  window_hours: float = 24.0) -> dict:
    """Assemble the operator status screen from the stored health/actions/alerts."""
    from .store import (
        fetch_recent_actions, fetch_recent_alerts, fetch_recent_health, source_freshness,
    )

    now = now or datetime.now(timezone.utc)
    history = fetch_recent_health(conn, limit=max(10, 5))
    last = history[0] if history else None

    streak = 0
    for h in history:
        if h.get("error"):
            streak += 1
        else:
            break

    recent_actions = fetch_recent_actions(conn, within_minutes=window_hours * 60, now=now)
    open_props = [a for a in recent_actions if a.get("action") in ("buy", "sell")]
    alerts = fetch_recent_alerts(conn, within_minutes=window_hours * 60, now=now)
    issues = detect_issues(history, interval_seconds=interval_seconds)

    return {
        "now": now.isoformat(),
        "last_cycle": None if not last else {
            "cycle": last.get("cycle"), "at": last.get("started_at"),
            "duration_ms": last.get("duration_ms"), "error": last.get("error"),
            "summary": last.get("summary"),
        },
        "error_streak": streak,
        "sources": source_freshness(conn),
        "open_proposals": len(open_props),
        "alerts_24h": len(alerts),
        "actions_24h": len(recent_actions),
        "ops_issues": [i.__dict__ for i in issues],
        "healthy": streak == 0 and not issues,
    }
