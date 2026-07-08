"""Grounded presentation layer — restates computed facts, never adds or alters."""

from __future__ import annotations

from types import SimpleNamespace

from catalyst.present import facts_from_payload, make_anthropic_presenter


class _FakeParse:
    """Stands in for anthropic's messages.parse — returns a canned _Narrative and
    records the exact facts JSON it was handed (to assert grounding)."""

    def __init__(self, narrative):
        self._narrative = narrative
        self.seen_content = None

    @property
    def messages(self):
        return self

    def parse(self, *, model, max_tokens, system, messages, output_format):
        self.seen_content = messages[0]["content"]
        return SimpleNamespace(parsed_output=self._narrative)


def _flat(**over):
    base = {
        "actions": {
            "asset": "BTC", "signal": "alert", "direction": "bullish",
            "confidence": 0.72, "score": 0.34, "horizon": "swing",
            "freshness": 12, "rationale": "BULLISH ALERT BTC | score 0.34 | ...",
        },
        "catalysts": ["etf", "regulation"],
        "layers": {"flow": {"label": "inflow", "bias": 0.2, "effect": "boost", "weight": 0.25}},
        "universe": ["BTC", "ETH"],
    }
    base.update(over)
    return base


def test_presenter_grounds_notes_and_leaves_numbers_untouched():
    """The presenter adds prose only; catalyst/layer notes are filtered to keys
    that actually exist, and a hallucinated tag/layer is dropped."""
    narrative = SimpleNamespace(
        summary="  BTC is a bullish alert driven by ETF flows.  ",
        catalyst_notes=[
            SimpleNamespace(key="etf", note="spot-ETF flow news"),
            SimpleNamespace(key="regulation", note="policy developments"),
            SimpleNamespace(key="hack", note="INVENTED — not in input"),  # must be dropped
        ],
        layer_notes=[
            SimpleNamespace(key="flow", note="inflows nudged bullish"),
            SimpleNamespace(key="macro", note="INVENTED — not in input"),  # must be dropped
        ],
    )
    fake = _FakeParse(narrative)
    present = make_anthropic_presenter(client=fake)

    flat = _flat()
    before = dict(flat["actions"])
    out = present(flat)

    # Prose fields returned, grounded to real keys only.
    assert out["summary"] == "BTC is a bullish alert driven by ETF flows."
    assert out["catalyst_notes"] == {"etf": "spot-ETF flow news",
                                     "regulation": "policy developments"}
    assert out["layer_notes"] == {"flow": "inflows nudged bullish"}
    assert "hack" not in out["catalyst_notes"]
    assert "macro" not in out["layer_notes"]

    # The presenter must not have mutated the source numbers.
    assert flat["actions"] == before

    # It was handed only the computed facts (no raw posts), and the numbers match.
    assert '"score": 0.34' in fake.seen_content
    assert "post" not in fake.seen_content.lower()


def test_presenter_omits_empty_note_sections():
    """A neutral watch with no catalysts/layers yields just a summary."""
    narrative = SimpleNamespace(
        summary="BTC is a low-conviction neutral watch.",
        catalyst_notes=[], layer_notes=[],
    )
    present = make_anthropic_presenter(client=_FakeParse(narrative))
    out = present(_flat(catalysts=[], layers={}))
    assert out == {"summary": "BTC is a low-conviction neutral watch."}


def test_facts_from_payload_is_the_only_surface():
    """facts_from_payload exposes the computed numbers + tags and nothing else."""
    facts = facts_from_payload(_flat())
    assert facts["asset"] == "BTC" and facts["score"] == 0.34
    assert facts["catalysts"] == ["etf", "regulation"]
    assert set(facts["layers"]) == {"flow"}
    assert "text" not in facts and "posts" not in facts
