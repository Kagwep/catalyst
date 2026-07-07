"""Compare helper — A/B two signal-weight configs on the same enriched data.

Recomputes signals under config A and config B and reports the per-asset score /
strength / rank delta, so you can see exactly how a `weights.json` change moves
the ranking before committing to it. Read-only; pure over the rows.
"""

from __future__ import annotations

from .signals import compute_signals

_WEIGHT_KEYS = ("source_weights", "catalyst_weights", "primary_boost", "strength_saturation")


def _weight_kwargs(weights: dict | None) -> dict:
    if not weights:
        return {}
    return {k: weights[k] for k in _WEIGHT_KEYS if k in weights}


def compare_weights(
    rows: list[dict], *, a: dict | None = None, b: dict | None = None, **signal_kwargs
) -> list[dict]:
    """Per-asset comparison of signals under weights `a` vs `b`, by |score delta|.

    `a`/`b` are loaded weights dicts (None = built-in defaults). `signal_kwargs`
    (window_hours, halflife_hours, primary_handles, …) apply to both sides.
    """
    sa = compute_signals(rows, **signal_kwargs, **_weight_kwargs(a))
    sb = compute_signals(rows, **signal_kwargs, **_weight_kwargs(b))
    rank_a = {s.asset: i + 1 for i, s in enumerate(sa)}
    rank_b = {s.asset: i + 1 for i, s in enumerate(sb)}
    map_a = {s.asset: s for s in sa}
    map_b = {s.asset: s for s in sb}

    out: list[dict] = []
    for asset in set(map_a) | set(map_b):
        A, B = map_a.get(asset), map_b.get(asset)
        score_a = A.score if A else 0.0
        score_b = B.score if B else 0.0
        out.append(
            {
                "asset": asset,
                "score_a": score_a,
                "score_b": score_b,
                "score_delta": round(score_b - score_a, 3),
                "strength_a": A.strength if A else 0.0,
                "strength_b": B.strength if B else 0.0,
                "rank_a": rank_a.get(asset),
                "rank_b": rank_b.get(asset),
                "direction_b": B.direction if B else None,
                "catalysts": sorted(set((A.catalysts if A else []) + (B.catalysts if B else []))),
            }
        )
    out.sort(key=lambda r: abs(r["score_delta"]), reverse=True)
    return out
