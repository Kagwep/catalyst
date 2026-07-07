"""On-chain actions source — event-log classification into catalyst posts.

The network wrapper (eth_getLogs) is thin like defillama._get; the tested core is
the pure log→post logic plus the keccak that derives event topics. Validating the
keccak against the two universally-known topics (Transfer, Upgraded) proves every
derived topic is correct.
"""

from __future__ import annotations

from datetime import datetime, timezone

from catalyst.enrich import LexiconScorer
from catalyst import onchain_actions as oca
from catalyst.onchain_actions import (
    Watch,
    _addr_topic,
    _load_watch,
    _TRANSFER_TOPIC,
    _UPGRADE_TOPIC,
    _TIMELOCK_TOPICS,
    event_topic,
    fetch_logs_chunked,
    logs_to_posts,
)

NOW = datetime(2026, 7, 1, tzinfo=timezone.utc)

# Canonical, universally-documented topic0 values (Ethereum Keccak-256).
TRANSFER_TOPIC0 = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
UPGRADED_TOPIC0 = "0xbc7cd75a20ee27fd9adebab32041f755214dbc6bffa90cc0225b39da2e5c2d3b"


def test_keccak_derives_known_event_topics():
    # If these match, the pure-Python keccak is correct → all derived topics are.
    assert event_topic("Transfer(address,address,uint256)") == TRANSFER_TOPIC0
    assert event_topic("Upgraded(address)") == UPGRADED_TOPIC0
    assert _TRANSFER_TOPIC == TRANSFER_TOPIC0
    assert _UPGRADE_TOPIC == UPGRADED_TOPIC0


def test_load_watch_normalises_and_drops_bad_entries():
    entries = _load_watch([
        {"address": "0xABC", "asset": "aave", "kinds": ["upgrade"]},
        {"asset": "NOADDR"},                       # no address → dropped
        {"address": "0xdef", "kinds": ["nonsense"]},  # no valid kind → dropped
        {"address": "0x123", "asset": "ARB"},       # defaults to upgrade+timelock
    ])
    assert [e.address for e in entries] == ["0xabc", "0x123"]
    assert entries[0].asset == "AAVE"
    assert entries[1].kinds == ("upgrade", "timelock")


def _log(topic0, *, data="0x", topics_rest=(), tx="0xfeed", idx="0x1"):
    return {"topics": [topic0, *topics_rest], "data": data,
            "transactionHash": tx, "logIndex": idx}


def test_upgrade_log_becomes_upgrade_post():
    w = Watch(address="0xproxy", asset="AAVE", kinds=("upgrade", "timelock"))
    posts = logs_to_posts([_log(_UPGRADE_TOPIC)], w, now=NOW)
    assert len(posts) == 1
    p = posts[0]
    assert p.source == "onchain_actions"
    assert p.raw["kind"] == "upgrade" and p.raw["asset"] == "AAVE"
    assert "$AAVE" in p.text and "upgraded" in p.text
    assert p.uri == "onchain_actions:upgrade:0xfeed:0x1"


def test_timelock_log_becomes_timelock_post():
    w = Watch(address="0xtl", asset="UNI", kinds=("timelock",))
    posts = logs_to_posts([_log(_TIMELOCK_TOPICS[0])], w, now=NOW)
    assert len(posts) == 1 and posts[0].raw["kind"] == "timelock"
    assert "timelocked" in posts[0].text


def test_kind_not_watched_is_skipped():
    # A proxy watched only for treasury shouldn't emit its Upgraded events.
    w = Watch(address="0xproxy", asset="AAVE", kinds=("treasury",))
    assert logs_to_posts([_log(_UPGRADE_TOPIC)], w, now=NOW) == []
    # And an unknown topic is ignored entirely.
    w2 = Watch(address="0xproxy", asset="AAVE", kinds=("upgrade",))
    assert logs_to_posts([_log("0x" + "11" * 32)], w2, now=NOW) == []


def _transfer_log(tokens, decimals=18, *, frm=None):
    raw = int(tokens * (10 ** decimals))
    rest = []
    if frm is not None:
        rest = [_addr_topic(frm), _addr_topic("0x" + "00" * 20)]
    return _log(_TRANSFER_TOPIC, data=hex(raw), topics_rest=tuple(rest))


def test_treasury_usd_gate():
    w = Watch(address="0xtoken", asset="ARB", kinds=("treasury",), decimals=18)
    # 2M tokens @ $1.50 = $3M ≥ $1M gate → kept, with USD attached.
    kept = logs_to_posts([_transfer_log(2_000_000)], w, now=NOW,
                         min_value_usd=1_000_000, price_usd=1.5)
    assert len(kept) == 1
    assert kept[0].raw["kind"] == "treasury"
    assert round(kept[0].raw["usd"]) == 3_000_000
    assert "treasury" in kept[0].text
    # 100 tokens @ $1.50 = $150 < gate → dropped.
    dropped = logs_to_posts([_transfer_log(100)], w, now=NOW,
                            min_value_usd=1_000_000, price_usd=1.5)
    assert dropped == []
    # No price + a gate set → can't verify magnitude → dropped.
    unpriced = logs_to_posts([_transfer_log(2_000_000)], w, now=NOW,
                             min_value_usd=1_000_000, price_usd=None)
    assert unpriced == []


def test_treasury_from_filter():
    treasury = "0x1111111111111111111111111111111111111111"
    other = "0x2222222222222222222222222222222222222222"
    w = Watch(address="0xtoken", asset="ARB", kinds=("treasury",),
              from_address=treasury.lower())
    match = _transfer_log(5_000_000, frm=treasury)
    miss = _transfer_log(5_000_000, frm=other)
    posts = logs_to_posts([match, miss], w, now=NOW, price_usd=1.0, min_value_usd=0)
    assert len(posts) == 1  # only the transfer FROM the watched treasury


def test_fetch_logs_chunked_windows_and_is_fail_soft(monkeypatch):
    """Wide ranges are split into ≤chunk_blocks windows; a failing window is
    skipped, not fatal (keyless nodes cap the block range)."""
    calls = []

    def fake_fetch_logs(address, topic0, start, end, url, *, topic1=None):
        calls.append((start, end))
        if start == 100:  # simulate a node rejecting one window
            raise RuntimeError("403 Forbidden")
        return [{"topics": [topic0], "data": "0x", "transactionHash": f"0x{start}", "logIndex": "0x0"}]

    monkeypatch.setattr(oca, "fetch_logs", fake_fetch_logs)
    logs = fetch_logs_chunked("0xabc", _UPGRADE_TOPIC, 0, 250, "http://x", chunk_blocks=100, pause=0)
    # windows [0-99], [100-199], [200-250]; the middle one failed → 2 logs survive
    assert calls == [(0, 99), (100, 199), (200, 250)]
    assert len(logs) == 2


def test_posts_reenrich_to_matching_catalyst_and_asset():
    """The produced text must classify (via the lexicon) to the intended catalyst
    and carry the right asset — i.e. it really rides enrich→signal end to end."""
    lex = LexiconScorer()
    cases = [
        (Watch("0xa", "AAVE", ("upgrade",)), _log(_UPGRADE_TOPIC), "upgrade", "AAVE"),
        (Watch("0xb", "UNI", ("timelock",)), _log(_TIMELOCK_TOPICS[1]), "timelock", "UNI"),
    ]
    for w, log, cat, asset in cases:
        post = logs_to_posts([log], w, now=NOW)[0]
        e = lex.score(post.text)
        assert e.catalyst == cat
        assert asset in e.assets

    # treasury (priced) too
    wt = Watch("0xc", "ARB", ("treasury",), decimals=18)
    tp = logs_to_posts([_transfer_log(2_000_000)], wt, now=NOW, price_usd=1.0, min_value_usd=0)[0]
    et = lex.score(tp.text)
    assert et.catalyst == "treasury" and "ARB" in et.assets
