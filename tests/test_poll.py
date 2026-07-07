from argparse import Namespace
from datetime import datetime, timezone

import catalyst.cli as cli
from catalyst.models import Author, Metrics, Post
from catalyst.store import open_store


def test_poll_cycle_runs_fetch_enrich_plan(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc).isoformat()
    posts = [
        Post(source="bluesky", uri="p1", text="$BTC ETF approved, price soars",
             indexed_at=now, author=Author(handle="watcher.guru"), metrics=Metrics(likes=10)),
        Post(source="defillama", uri="h1", text="HACK: Foo exploited for $30.0M",
             indexed_at=now, author=Author(handle="defillama"), metrics=Metrics()),
    ]
    # Stub the network fetch; exercise the real save/enrich/signal/plan chain.
    monkeypatch.setattr(cli, "run_config", lambda cfg: posts)

    conn = open_store(str(tmp_path / "t.db"))
    args = Namespace(
        config="x", enrich=True, plan=True, window=24.0, halflife=6.0,
        buy_threshold=0.08, max_age=1e9, cooldown=120.0,
    )
    try:
        health = cli._poll_cycle(conn, args, frozenset({"watcher.guru"}), None)
        summary, notable = health.summary, health.notable_actions

        assert "new" in summary and "enriched" in summary and "actions" in summary
        # Both posts got scored...
        scored = conn.execute("SELECT count(*) FROM posts WHERE sentiment_model IS NOT NULL").fetchone()[0]
        assert scored == 2
        # ...and at least one action was persisted (the bullish $BTC ETF post).
        assert conn.execute("SELECT count(*) FROM actions").fetchone()[0] >= 1
        assert any(a.asset == "BTC" for a in notable)
    finally:
        conn.close()


def test_poll_cycle_cooldown_across_cycles(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc).isoformat()
    posts = [
        Post(source="bluesky", uri="p1", text="$BTC ETF approved, price soars",
             indexed_at=now, author=Author(handle="watcher.guru"), metrics=Metrics(likes=10)),
    ]
    monkeypatch.setattr(cli, "run_config", lambda cfg: posts)
    conn = open_store(str(tmp_path / "t.db"))
    args = Namespace(
        config="x", enrich=True, plan=True, window=24.0, halflife=6.0,
        buy_threshold=0.08, max_age=1e9, cooldown=120.0,
    )
    try:
        first = cli._poll_cycle(conn, args, frozenset({"watcher.guru"}), None).notable_actions
        second = cli._poll_cycle(conn, args, frozenset({"watcher.guru"}), None).notable_actions
        assert any(a.asset == "BTC" for a in first)
        # Second cycle: the BTC buy is within cooldown, so it isn't re-proposed.
        assert all(a.asset != "BTC" for a in second)
    finally:
        conn.close()
