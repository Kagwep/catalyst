"""Pipeline freshness — old reposts/pins filtered, searches get a time floor."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from catalyst import pipeline
from catalyst.models import Post
from catalyst.pipeline import fresh_only, run_config

NOW = datetime.now(timezone.utc)


def _post(uri: str, created_at: str | None) -> Post:
    return Post(source="bluesky", uri=uri, text=uri, created_at=created_at,
                indexed_at=NOW.isoformat())


def test_fresh_only_drops_old_keeps_fresh_and_unparseable():
    fresh = _post("fresh", (NOW - timedelta(hours=2)).isoformat())
    old = _post("old", (NOW - timedelta(days=90)).isoformat().replace("+00:00", "Z"))
    unknown = _post("unknown", None)
    out = fresh_only([fresh, old, unknown], max_age_hours=24)
    assert [p.uri for p in out] == ["fresh", "unknown"]


def test_fresh_only_disabled_by_default():
    old = _post("old", "2020-01-01T00:00:00Z")
    assert fresh_only([old], None) == [old]


def test_run_config_applies_age_cap_and_computed_since(tmp_path, monkeypatch):
    cfg = {
        "accounts": [{"actor": "watcher.guru", "max": 10}],
        "accounts_max_age_hours": 24,
        "keywords": [{"q": "mainnet launch", "sort": "latest", "since_hours": 24}],
        "dedupe": {"enabled": False},
    }
    path = tmp_path / "sources.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")

    old_repost = _post("at://old", "2020-01-01T00:00:00Z")
    fresh_post = _post("at://fresh", NOW.isoformat())
    monkeypatch.setattr(pipeline.bluesky, "get_author_feed",
                        lambda actor, **kw: [old_repost, fresh_post])

    seen_since: list[str | None] = []

    def fake_search(q, *, max, sort, since):
        seen_since.append(since)
        return []

    monkeypatch.setattr(pipeline.bluesky, "search_posts", fake_search)

    out = run_config(str(path))
    assert [p.uri for p in out] == ["at://fresh"]  # 90s-old repost filtered
    cutoff = datetime.fromisoformat(seen_since[0])
    assert timedelta(hours=23) < NOW - cutoff < timedelta(hours=25)
