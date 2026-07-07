from catalyst.enrich import (
    Enrichment,
    LexiconScorer,
    classify_catalyst,
    extract_assets,
    hybrid_enrich,
    score_sentiment,
)
from catalyst.models import Author, Metrics, Post
from catalyst.store import fetch_unenriched, open_store, save_enrichments, save_posts


def test_sentiment_direction():
    assert score_sentiment("Bitcoin surges to record high")[1] == "positive"
    assert score_sentiment("Exchange hacked, funds drained, price crashes")[1] == "negative"
    assert score_sentiment("The conference is scheduled for Tuesday")[1] == "neutral"


def test_sentiment_negation_flips():
    pos, _ = score_sentiment("rally")
    neg, _ = score_sentiment("no rally")
    assert pos > 0 and neg < 0


def test_extract_assets_tickers_and_names():
    assert extract_assets("$BTC and Ethereum pump, $SOL too") == ["BTC", "ETH", "SOL"]
    assert extract_assets("nothing relevant here") == []


def test_classify_catalyst_priority():
    assert classify_catalyst("Exchange hacked overnight") == "hack"
    assert classify_catalyst("SEC approves spot ETF") == "etf"  # etf beats regulation
    assert classify_catalyst("New token listing on Binance") == "listing"
    assert classify_catalyst("just a normal market update") is None


def test_lexicon_scorer_shape():
    e = LexiconScorer().score("JUST IN: $BTC ETF approved, price soars")
    assert e.model == "lexicon"
    assert e.sentiment_label == "positive"
    assert e.assets == ["BTC"]
    assert e.catalyst == "etf"


def test_hybrid_routes_only_candidates_to_llm():
    sentinel = Enrichment(0.9, "positive", ["BTC"], "etf", model="stub-llm")
    calls = []

    def stub(text):
        calls.append(text)
        return sentinel

    items = [
        {"uri": "a", "text": "Bitcoin ETF approved", "author_handle": "reuters.com"},   # catalyst -> LLM
        {"uri": "b", "text": "the weather is mild today", "author_handle": "reuters.com"},  # neutral -> lexicon
        {"uri": "c", "text": "market update", "author_handle": "watcher.guru"},          # primary -> LLM
    ]
    out = dict(hybrid_enrich(items, llm_score=stub, primary_handles=frozenset({"watcher.guru"})))

    assert out["a"].model == "stub-llm"   # catalyst candidate
    assert out["b"].model == "lexicon"    # not a candidate
    assert out["c"].model == "stub-llm"   # primary handle
    assert len(calls) == 2


def test_hybrid_llm_failure_falls_back_to_lexicon():
    def boom(text):
        raise RuntimeError("api down")

    items = [{"uri": "a", "text": "Exchange hacked, funds stolen", "author_handle": "x"}]
    out = dict(hybrid_enrich(items, llm_score=boom))
    assert out["a"].model == "lexicon"
    assert out["a"].catalyst == "hack"


def test_save_and_fetch_unenriched(tmp_path):
    conn = open_store(str(tmp_path / "t.db"))
    try:
        save_posts(
            conn,
            [
                Post(source="bluesky", uri="p1", text="$BTC surges", indexed_at="2026-06-15T10:00:00Z",
                     author=Author(handle="watcher.guru"), metrics=Metrics()),
                Post(source="bluesky", uri="p2", text="quiet day", indexed_at="2026-06-15T09:00:00Z",
                     author=Author(handle="reuters.com"), metrics=Metrics()),
            ],
        )
        pending = fetch_unenriched(conn)
        assert {r["uri"] for r in pending} == {"p1", "p2"}

        results = hybrid_enrich(pending)  # lexicon only
        n = save_enrichments(conn, results)
        assert n == 2

        # Now nothing is pending; values persisted.
        assert fetch_unenriched(conn) == []
        row = conn.execute(
            "SELECT sentiment_label, assets, sentiment_model FROM posts WHERE uri='p1'"
        ).fetchone()
        assert row["sentiment_label"] == "positive"
        assert "BTC" in row["assets"]
        assert row["sentiment_model"] == "lexicon"
    finally:
        conn.close()
