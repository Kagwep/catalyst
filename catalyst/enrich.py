"""Enrichment layer — sentiment + asset + catalyst scoring for stored posts.

Hybrid design:
  1. A cheap, zero-dependency LexiconScorer runs on every post (directional
     sentiment, ticker/asset extraction, catalyst classification).
  2. An optional LLM scorer (Claude, via the [llm] extra) re-scores only the
     *candidates* — posts with a detected catalyst, strong lexicon sentiment, or
     from a primary high-signal account (e.g. watcher.guru).

This keeps cost/latency low on the firehose while spending LLM calls where they
matter for short-term, catalyst-driven trade signals. Enrichment is derived data
written back to the store — the Post adapters stay source-agnostic and pure.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from typing import Callable, Iterable


@dataclass
class Enrichment:
    sentiment_score: float          # -1.0 (max negative) .. +1.0 (max positive)
    sentiment_label: str            # "negative" | "neutral" | "positive"
    assets: list[str] = field(default_factory=list)  # e.g. ["BTC", "ETH"]
    catalyst: str | None = None     # listing | hack | etf | mainnet | regulation | …
    model: str = "lexicon"          # which scorer produced this row
    event: str | None = None        # one-line "what happened" (LLM only; None for lexicon)
    severity: str | None = None     # how market-moving: "high" | "medium" | "low" | "none"


# ---- Lexicon ----------------------------------------------------------------

_POSITIVE = {
    "surge", "surges", "surged", "soar", "soars", "soared", "rally", "rallies",
    "jump", "jumps", "jumped", "gain", "gains", "rise", "rises", "rebound",
    "breakout", "bullish", "approve", "approved", "approval", "partnership",
    "launch", "launches", "upgrade", "upgraded", "adopt", "adoption", "record", "ath",
    "beats", "beat", "win", "wins", "boost", "boosts", "green", "up", "high",
    "milestone", "integrate", "integration",
    # macro: easing = risk-on
    "cut", "cuts", "dovish", "ease", "eases", "easing", "stimulus", "cooling",
    "disinflation",
}
_NEGATIVE = {
    "crash", "crashes", "crashed", "plunge", "plunges", "plunged", "dump",
    "dumps", "fall", "falls", "fell", "drop", "drops", "slump", "sink", "tumble",
    "bearish", "hack", "hacked", "exploit", "breach", "rug", "lawsuit", "sue",
    "sued", "ban", "banned", "sanction", "sanctions", "delay", "delayed",
    "reject", "rejected", "liquidated", "liquidation", "selloff", "fear",
    "warning", "warn", "loss", "losses", "down", "red", "scam", "fraud", "halt",
    "collapse", "crisis",
    # macro: tightening = risk-off
    "hike", "hikes", "hawkish", "tighten", "tightens", "tightening",
}
_NEGATORS = {"no", "not", "never", "without", "cant", "cannot", "wont", "isnt", "dont", "fails"}
_INTENSIFIERS = {"very": 1.5, "huge": 1.6, "massive": 1.7, "major": 1.3, "breaking": 1.2}

# name -> canonical ticker (matched as whole words)
_ASSET_NAMES = {
    "bitcoin": "BTC", "btc": "BTC", "ethereum": "ETH", "ether": "ETH", "eth": "ETH",
    "solana": "SOL", "sol": "SOL", "ripple": "XRP", "xrp": "XRP", "dogecoin": "DOGE",
    "doge": "DOGE", "cardano": "ADA", "ada": "ADA", "binance": "BNB", "bnb": "BNB",
    "avalanche": "AVAX", "avax": "AVAX", "chainlink": "LINK", "polkadot": "DOT",
}
_TICKER_RE = re.compile(r"\$([A-Za-z]{2,6})\b")
_WORD_RE = re.compile(r"\$?[a-zA-Z']+")

# (label, keywords) in priority order — first match wins.
_CATALYSTS: list[tuple[str, set[str]]] = [
    ("hack", {"hack", "hacked", "exploit", "breach", "drained", "stolen", "rug"}),
    ("etf", {"etf"}),
    ("liquidation", {"liquidated", "liquidation", "liquidations", "shorts", "longs"}),
    ("macro", {"fed", "fomc", "ecb", "boj", "powell", "lagarde", "inflation", "cpi",
               "disinflation", "hawkish", "dovish", "tightening", "easing", "monetary", "fred"}),
    ("listing", {"listing", "listed", "lists", "delist", "delisted"}),
    ("regulation", {"sec", "lawsuit", "sue", "sued", "ban", "banned", "sanction",
                    "sanctions", "regulation", "court", "ruling", "settlement"}),
    ("upgrade", {"upgrade", "upgraded", "implementation"}),
    ("timelock", {"timelock", "timelocked"}),
    ("treasury", {"treasury", "buyback", "buybacks"}),
    ("mainnet", {"mainnet", "testnet", "launch", "launches", "fork"}),
    ("release", {"release", "released", "version"}),
    ("governance", {"governance", "proposal", "proposals", "dao", "quorum"}),
    ("partnership", {"partner", "partnership", "integration", "acquire",
                     "acquisition", "merger", "collaboration"}),
    ("unlock", {"unlock", "unlocks", "vesting", "emission", "emissions"}),
    ("tvl", {"tvl"}),
]


def extract_assets(text: str) -> list[str]:
    """Pull $TICKERS and known coin names out of text, de-duped and sorted."""
    found = {m.group(1).upper() for m in _TICKER_RE.finditer(text)}
    for tok in _WORD_RE.findall(text.lower()):
        if tok in _ASSET_NAMES:
            found.add(_ASSET_NAMES[tok])
    return sorted(found)


def classify_catalyst(text: str) -> str | None:
    words = set(_WORD_RE.findall(text.lower()))
    for label, keywords in _CATALYSTS:
        if words & keywords:
            return label
    return None


def score_sentiment(text: str) -> tuple[float, str]:
    """Lexicon sentiment with negation + intensifier handling. Returns (score, label)."""
    tokens = [t.lstrip("$") for t in _WORD_RE.findall(text.lower())]
    total = 0.0
    for i, tok in enumerate(tokens):
        polarity = 1 if tok in _POSITIVE else (-1 if tok in _NEGATIVE else 0)
        if not polarity:
            continue
        window = tokens[max(0, i - 3):i]
        if any(t in _NEGATORS for t in window):
            polarity = -polarity
        mult = max((_INTENSIFIERS.get(t, 1.0) for t in window), default=1.0)
        total += polarity * mult

    score = max(-1.0, min(1.0, total / 3.0))
    label = "positive" if score > 0.15 else "negative" if score < -0.15 else "neutral"
    return score, label


class LexiconScorer:
    """Fast, dependency-free scorer."""

    def score(self, text: str) -> Enrichment:
        s, label = score_sentiment(text)
        return Enrichment(
            sentiment_score=round(s, 3),
            sentiment_label=label,
            assets=extract_assets(text),
            catalyst=classify_catalyst(text),
            model="lexicon",
        )


# ---- Hybrid orchestration ---------------------------------------------------

def is_candidate(e: Enrichment, *, is_primary: bool, threshold: float) -> bool:
    """A post worth spending an LLM call on."""
    return bool(e.catalyst) or abs(e.sentiment_score) >= threshold or is_primary


def hybrid_enrich(
    items: Iterable[dict],
    *,
    llm_score: Callable[[str], Enrichment] | None = None,
    primary_handles: frozenset[str] = frozenset(),
    score_threshold: float = 0.3,
    llm_all: bool = False,
) -> list[tuple[str, Enrichment]]:
    """Score items (dicts with uri/text/author_handle). Returns (uri, Enrichment) pairs.

    Every item gets a lexicon score; an LLM score is layered on top when
    `llm_score` is provided. By default only *candidates* get the LLM call (cost
    control on the firehose). `llm_all=True` makes LLM interpretation MANDATORY
    for every post that has text — the lexicon can't read nuance, and posts are
    the catalyst-bearing sentiment source, so their interpretation shouldn't be
    gated. Only new posts are enriched per cycle, so the call volume stays small.
    LLM failures fall back to the lexicon result, per item.
    """
    lex = LexiconScorer()
    out: list[tuple[str, Enrichment]] = []
    for it in items:
        uri = it.get("uri")
        if not uri:
            continue
        text = it.get("text", "") or ""
        e = lex.score(text)
        primary = it.get("author_handle") in primary_handles
        use_llm = bool(llm_score) and bool(text.strip()) and (
            llm_all or is_candidate(e, is_primary=primary, threshold=score_threshold)
        )
        if use_llm:
            try:
                e = llm_score(text)
            except Exception as err:  # noqa: BLE001 — never let one call sink the batch
                print(f"llm enrich failed for {uri}: {err}", file=sys.stderr)
        out.append((uri, e))
    return out


# ---- Optional Claude scorer (the pluggable LLM slot) ------------------------

_SYSTEM = (
    "You score short news/social posts for short-term crypto/financial trading. "
    "Return directional MARKET sentiment (how the news is likely to move price), "
    "not the author's tone. sentiment_score is a float from -1.0 (strongly "
    "bearish) to +1.0 (strongly bullish); sentiment_label is one of negative, "
    "neutral, positive. assets is a list of UPPERCASE tickers the post is about "
    "(e.g. BTC, ETH, SOL); empty if none. catalyst is the event type if any, one "
    "of: listing, hack, etf, mainnet, regulation, partnership, liquidation, macro "
    "— or null. event is a concise (<=12 word) factual statement of WHAT HAPPENED "
    "that could move the market, drawn only from the post — or null if the post "
    "reports no concrete event. severity is how market-moving the event is, one of "
    "high, medium, low, none (none for chatter/opinion with no real event)."
)


def make_anthropic_scorer(model: str = "claude-opus-4-8", client=None) -> Callable[[str], Enrichment]:
    """Build an LLM scorer backed by Claude (requires the [llm] extra + API key).

    The model defaults to claude-opus-4-8; pass model="claude-haiku-4-5" for a
    fast, low-cost pass on a high-volume firehose.
    """
    from pydantic import BaseModel

    if client is None:
        import anthropic  # lazy: only needed to build a real client (the [llm] extra)

        client = anthropic.Anthropic()
    c = client

    class _Out(BaseModel):
        sentiment_label: str
        sentiment_score: float
        assets: list[str]
        catalyst: str | None
        event: str | None
        severity: str

    def score(text: str) -> Enrichment:
        # Omit `thinking` — this is a fast structured classification, not a
        # reasoning task; structured outputs validate the shape for us.
        resp = c.messages.parse(
            model=model,
            max_tokens=512,
            system=_SYSTEM,
            messages=[{"role": "user", "content": text}],
            output_format=_Out,
        )
        o = resp.parsed_output
        sev = (o.severity or "none").strip().lower()
        return Enrichment(
            sentiment_score=max(-1.0, min(1.0, float(o.sentiment_score))),
            sentiment_label=o.sentiment_label,
            assets=[a.upper() for a in o.assets],
            catalyst=o.catalyst,
            event=(o.event.strip() if o.event and o.event.strip() else None),
            severity=sev if sev in ("high", "medium", "low", "none") else "none",
            model=model,
        )

    return score
