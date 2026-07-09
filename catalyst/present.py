"""Presentation layer — an OPTIONAL, grounded LLM narration of a computed signal.

Role separation is the whole point: the deterministic pipeline
(signals → biases → planner) is the sole source of truth for every number,
direction, signal, and catalyst. This layer never scores, decides, or adds
facts — it takes the *already-computed* delivery payload and restates it in
short plain language for the agents and humans consuming it.

Safety is structural, not just prompt-deep:
  - the LLM is handed ONLY the structured facts we computed (no posts, no web);
  - we merge back ONLY prose fields (`summary`, `catalyst_notes`, `layer_notes`)
    — every numeric/signal field stays exactly as the pipeline produced it;
  - notes for catalysts/layers not present in the input are DROPPED, so the
    model cannot introduce a catalyst or layer that isn't really there.

Optional + keyed: built only when ANTHROPIC_API_KEY is available (needs the
[llm] extra). Absent → no narrative fields, delivery unchanged.
"""

from __future__ import annotations

import json
from typing import Callable

_PRESENT_SYSTEM = (
    "You are a PRESENTATION layer for a crypto catalyst-signal oracle. You are "
    "given ONE already-computed signal as structured JSON, including "
    "`catalyst_headlines` — the actual recent posts behind the signal. Your ONLY "
    "job is to restate it in short, clear plain language for AI agents and humans.\n"
    "STRICT RULES:\n"
    "- Use ONLY the facts in the input (numbers, tags, and the provided headlines). "
    "Never introduce prices, events, dates, assets, catalysts, or layers not present.\n"
    "- Never change or re-derive the direction, signal, confidence, or score. No "
    "trading advice; do not tell anyone to buy, sell, or hold.\n"
    "- summary: ONE sentence (max two) saying what the signal is and WHY, citing the "
    "concrete developments from `catalyst_headlines` (e.g. what actually happened), "
    "not generic phrasing. Be concise.\n"
    "- catalyst_notes: for EACH tag in the input `catalysts` list, state WHAT "
    "ACTUALLY HAPPENED for that tag, drawn from `catalyst_headlines` — the specific "
    "event(s), NOT a definition of the category. If several headlines share a tag, "
    "summarize the key development in <=15 words. Ground every note in the "
    "headlines; if no headline explains a tag, OMIT that tag rather than guess.\n"
    "- layer_notes: for EACH key in the input `layers` object, a <=8-word phrase on "
    "how that layer influenced the signal (use its label/bias/effect). Add nothing "
    "not present.\n"
    "Neutral, non-prescriptive, no filler."
)


def catalyst_headlines(rows, asset, window_hours, *, limit=8, max_len=200):
    """The actual catalyst-bearing post headlines for `asset` in the window.

    This is the raw material the presenter needs to say WHAT HAPPENED rather than
    gloss a tag: real posts (with their catalyst tag + sentiment), most recent
    first. Grounding stays intact — it's still only data the pipeline ingested."""
    from datetime import datetime, timedelta, timezone

    from .signals import _assets, _parse_dt

    if not asset:
        return []
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=window_hours)
    picked: list[tuple] = []
    for r in rows:
        if not r.get("catalyst") or asset not in _assets(r):
            continue
        d = _parse_dt(r.get("indexed_at"))
        if d is None or d < cutoff:
            continue
        picked.append((d, {
            "catalyst": r.get("catalyst"),
            "sentiment": r.get("sentiment_score"),
            "source": r.get("source"),
            "text": (r.get("text") or "")[:max_len],
        }))
    picked.sort(key=lambda x: x[0], reverse=True)
    return [h for _, h in picked[:limit]]


def facts_from_payload(flat: dict, headlines=None) -> dict:
    """The compact, grounded fact set we hand the model — nothing else.

    Pulled from the flattened delivery payload plus the real `catalyst_headlines`
    behind it, so the model can describe what happened while staying grounded in
    data the pipeline actually ingested."""
    a = flat.get("actions") or {}
    return {
        "asset": a.get("asset"),
        "signal": a.get("signal"),
        "direction": a.get("direction"),
        "confidence": a.get("confidence"),
        "score": a.get("score"),
        "horizon": a.get("horizon"),
        "freshness_minutes": a.get("freshness"),
        "rationale": a.get("rationale"),
        "catalysts": list(flat.get("catalysts") or []),
        "catalyst_headlines": list(headlines or []),
        "layers": flat.get("layers") or {},
        "universe": list(flat.get("universe") or []),
    }


def make_anthropic_presenter(
    model: str = "claude-sonnet-5", client=None
) -> Callable[[dict], dict]:
    """Build a grounded presenter backed by Claude (needs the [llm] extra + key).

    Returns a callable `present(flat_payload) -> {summary, catalyst_notes,
    layer_notes}`. Notes are filtered to keys that actually appear in the input,
    so the model can only ever *describe* what the pipeline already produced."""
    from pydantic import BaseModel, Field

    if client is None:
        import anthropic  # lazy: only needed to build a real client (the [llm] extra)

        client = anthropic.Anthropic()
    c = client

    # Field descriptions aren't just docs — structured outputs feed them to the
    # model, so they reinforce the "restate only, stay grounded" contract at the
    # schema level, not only in the system prompt.
    class _Note(BaseModel):
        key: str = Field(description="The exact catalyst tag or layer name from the "
                                     "input — never one that is not present.")
        note: str = Field(description="A few words (<=8) explaining what it means or "
                                      "how it moved the signal. No numbers, no advice.")

    class _Narrative(BaseModel):
        summary: str = Field(description="One sentence (max two) restating the top "
                                         "signal and why, citing the concrete "
                                         "developments from catalyst_headlines. "
                                         "Neutral and non-prescriptive.")
        catalyst_notes: list[_Note] = Field(description="One note per tag in `catalysts`, "
                                                        "stating WHAT HAPPENED for it from "
                                                        "catalyst_headlines (the event, not "
                                                        "a definition); omit a tag with no "
                                                        "supporting headline.")
        layer_notes: list[_Note] = Field(description="One note per key in the input "
                                                     "`layers` object; empty if none.")

    def present(flat: dict, headlines=None) -> dict:
        facts = facts_from_payload(flat, headlines)
        resp = c.messages.parse(
            model=model,
            max_tokens=512,
            system=_PRESENT_SYSTEM,
            messages=[{"role": "user", "content": json.dumps(facts)}],
            output_format=_Narrative,
        )
        o = resp.parsed_output
        allowed_cat = set(facts["catalysts"])
        allowed_layer = set(facts["layers"])
        # Ground the notes: keep only keys the pipeline actually produced.
        cat_notes = {n.key: n.note for n in o.catalyst_notes if n.key in allowed_cat}
        layer_notes = {n.key: n.note for n in o.layer_notes if n.key in allowed_layer}
        out = {"summary": o.summary.strip()}
        if cat_notes:
            out["catalyst_notes"] = cat_notes
        if layer_notes:
            out["layer_notes"] = layer_notes
        return out

    return present
