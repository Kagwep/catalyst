"""Cross-source de-duplication — collapse the same story to one row.

The same breaking item often arrives from several sources at once (watcher.guru
on Bluesky, a Cointelegraph RSS item, a re-post). Left alone they each add
weighted volume to the signal layer, so one story triple-counts as conviction.
This collapses a near-duplicate cluster to its **highest-trust** member (by the
same source weighting the signal layer uses), dropping the rest before they hit
the store.

Matching is dependency-free and deliberately conservative (precision over
recall — we'd rather keep two near-dups than merge two distinct stories):
  - **Canonical URL** equality (scheme/host-www/query/fragment/trailing-slash
    stripped) — a strong signal two rows are the same article.
  - **Title token Jaccard** over significant words ≥ a threshold, but only when
    both titles have enough tokens to be meaningful.

Runs on a batch (the `run_config` output), so it de-dups within a fetch cycle;
the URI upsert in the store already handles exact re-fetches across cycles.
"""

from __future__ import annotations

import re

from .models import Post
from .signals import DEFAULT_SOURCE_WEIGHTS, PRIMARY_BOOST

_TOKEN_RE = re.compile(r"[a-z0-9]+")
# Common filler that shouldn't drive a title match.
_STOP = {
    "the", "a", "an", "to", "of", "in", "on", "for", "and", "or", "is", "are",
    "as", "at", "by", "it", "its", "be", "was", "has", "have", "with", "from",
    "this", "that", "will", "just", "new", "now", "breaking", "update", "says",
    "after", "over", "amid", "into", "up", "down",
}
_MIN_TOKENS = 3  # titles shorter than this don't near-dup on tokens


def _canon_url(url: str | None) -> str | None:
    if not url:
        return None
    u = url.strip().lower()
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"^www\.", "", u)
    u = u.split("#", 1)[0].split("?", 1)[0]
    return u.rstrip("/") or None


def _sig_tokens(text: str | None) -> frozenset[str]:
    if not text:
        return frozenset()
    toks = [t for t in _TOKEN_RE.findall(text.lower()) if len(t) >= 3 and t not in _STOP]
    return frozenset(toks)


def _jaccard(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / len(a | b) if inter else 0.0


def _engagement(p: Post) -> int:
    m = p.metrics
    return (m.likes or 0) + 2 * (m.reposts or 0)


def trust_of(
    p: Post,
    *,
    source_weights: dict | None = None,
    primary_handles: frozenset[str] = frozenset(),
    primary_boost: float = PRIMARY_BOOST,
) -> float:
    """A post's trust — the same source weighting the signal layer ranks on."""
    weights = {**DEFAULT_SOURCE_WEIGHTS, **(source_weights or {})}
    w = weights.get(p.source, 1.0)
    if p.author.handle and p.author.handle in primary_handles:
        w *= primary_boost
    return w


def _is_dupe(p: Post, rep: Post, url_p: str | None, url_r: str | None,
             toks_p: frozenset, toks_r: frozenset, jaccard: float) -> bool:
    if url_p is not None and url_r is not None:
        return url_p == url_r
    if len(toks_p) >= _MIN_TOKENS and len(toks_r) >= _MIN_TOKENS:
        return _jaccard(toks_p, toks_r) >= jaccard
    return False


def collapse_dupes(
    posts: list[Post],
    *,
    source_weights: dict | None = None,
    primary_handles: frozenset[str] = frozenset(),
    primary_boost: float = PRIMARY_BOOST,
    jaccard: float = 0.72,
) -> tuple[list[Post], int]:
    """Collapse near-duplicate posts to the highest-trust member.

    Returns (deduped, n_collapsed). Preserves the input ordering of the survivors.
    Tie-break within a cluster: trust → engagement → newer `indexed_at`.
    """
    # Precompute the match features once.
    feats = [(_canon_url(p.url), _sig_tokens(p.text)) for p in posts]
    rank = [
        (trust_of(p, source_weights=source_weights, primary_handles=primary_handles,
                  primary_boost=primary_boost), _engagement(p), p.indexed_at or "")
        for p in posts
    ]

    clusters: list[list[int]] = []      # each is a list of post indices
    reps: list[int] = []                # representative index per cluster
    for i, p in enumerate(posts):
        url_i, toks_i = feats[i]
        placed = False
        for ci, r in enumerate(reps):
            url_r, toks_r = feats[r]
            if _is_dupe(p, posts[r], url_i, url_r, toks_i, toks_r, jaccard):
                clusters[ci].append(i)
                # Promote the higher-trust member to representative.
                if rank[i] > rank[r]:
                    reps[ci] = i
                placed = True
                break
        if not placed:
            clusters.append([i])
            reps.append(i)

    keep = set(reps)
    deduped = [p for i, p in enumerate(posts) if i in keep]
    return deduped, len(posts) - len(deduped)
