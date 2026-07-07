import json

from catalyst import protocols as pr
from catalyst.models import Author, Post

REGISTRY = [
    {"name": "Uniswap", "symbol": "UNI", "github": ["Uniswap/v4-core"], "snapshot": "uni.eth"},
    {"name": "Ethereum", "symbol": "ETH", "github": ["ethereum/go-ethereum"]},  # no snapshot
]


def test_load_registry_accepts_both_shapes(tmp_path):
    p = tmp_path / "protocols.json"
    p.write_text(json.dumps({"protocols": REGISTRY}), encoding="utf-8")
    reg = pr.load_registry(str(p))
    assert [r["symbol"] for r in reg] == ["UNI", "ETH"]


def test_fetch_releases_relabels_with_symbol(monkeypatch):
    def fake_feed(url, max=None):
        assert url.endswith("/releases.atom")
        return [Post(source="rss", uri="g1", url="https://gh/x", text="v1.17.3",
                     created_at="2026-05-01T00:00:00Z", indexed_at="2026-05-01T00:00:00Z",
                     author=Author(handle="feed"))]

    monkeypatch.setattr(pr.rss, "fetch_feed", fake_feed)
    posts = pr.fetch_releases(REGISTRY)
    assert len(posts) == 2  # one repo each
    uni = next(p for p in posts if p.author.handle == "UNI")
    assert uni.source == "github"
    assert uni.text.startswith("Release: Uniswap $UNI")
    assert "v1.17.3" in uni.text


def test_fetch_governance_collects_spaces_and_symbols(monkeypatch):
    captured = {}

    def fake_fetch(spaces, *, state, first, max=None, symbols=None):
        captured["spaces"] = list(spaces)
        captured["symbols"] = symbols
        return []

    monkeypatch.setattr(pr.snapshot, "fetch_proposals", fake_fetch)
    pr.fetch_governance(REGISTRY, state="active", first=10)
    assert captured["spaces"] == ["uni.eth"]          # only protocols with a snapshot space
    assert captured["symbols"] == {"uni.eth": "UNI"}


def test_fetch_governance_empty_without_spaces():
    assert pr.fetch_governance([{"name": "X", "symbol": "X", "github": ["a/b"]}]) == []
